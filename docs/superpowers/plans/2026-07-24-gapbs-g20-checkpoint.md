# GAPBS Scale-20 Checkpointed CXL Measurement Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a fail-closed two-phase checkpoint workflow that loads and warms `g20.sg` quickly, then measures trial 1 with a Timing CPU and the entire memory range behind a 1 us CXL link.

**Architecture:** Pure Python helpers own checkpoint event sequencing and content-addressed manifests. The gem5 config saves at trial-0 work-begin before OpenMP workers enter the kernel; after restore, the two-core Timing system runs trial 0 over CXL as warmup and measures trial 1. The comparison runner creates/reuses exact checkpoints and the evidence validator accepts a speedup only when checkpoint provenance, `-f g20.sg`, all-CXL topology, one trial-1 ROI stats section, and bit-exact verification all pass.

**Tech Stack:** Python 3 `unittest`, gem5 standard library SE mode, `CheckpointResource`, GAPBS work-begin/work-end m5ops, CSV/JSON/SHA-256 provenance, systemd transient background service.

---

## File Structure

- Create `scripts/gapbs_checkpoint.py`: deterministic file hashing, checkpoint
  identity construction, manifest loading/writing, and reuse validation.
- Modify `configs/example/gem5_library/gapbs_roi_state.py`: pure save/restore
  event state machines, independent of gem5 objects.
- Modify `configs/example/gem5_library/x86-gapbs-amu-se.py`: checkpoint CLI,
  save/restore board selection, `CheckpointResource`, stats reset/dump, and
  explicit log markers.
- Modify `scripts/compare_gapbs_cxl_amu_cira.py`: `-f` graph arguments,
  checkpoint creation/reuse, restore execution, provenance summary fields, and
  incomplete-run handling.
- Modify `scripts/validate_gapbs_amu_latency_sweep.py`: checkpoint manifest/log
  validation and restore-specific CPU/config checks.
- Create `tests/pyunit/amu/test_gapbs_checkpoint.py`: focused unit tests for
  checkpoint identity, state sequencing, runner commands, and fail-closed
  provenance.
- Modify `tests/pyunit/amu/test_validate_gapbs_amu_latency_sweep.py`: validator
  fixtures and negative tests for checkpoint evidence.
- Modify `docs/amu-gapbs-benchmark.md`: exact foreground smoke and persistent
  background commands plus status checks.

### Task 1: Pure Checkpoint Event State

**Files:**
- Modify: `configs/example/gem5_library/gapbs_roi_state.py`
- Create: `tests/pyunit/amu/test_gapbs_checkpoint.py`

- [ ] **Step 1: Write failing save/restore sequence tests**

```python
class GapbsCheckpointStateTest(unittest.TestCase):
    def test_save_stops_at_measured_begin(self):
        state = roi.GapbsCheckpointState(
            mode="save", iterations=2, measure_trial=1
        )
        self.assertEqual(state.work_begin(), ("checkpoint",))
        self.assertEqual(state.checkpoint_saved(), ("stop",))
        self.assertEqual(state.finish(), ())

    def test_restore_warms_trial_zero_then_measures_trial_one(self):
        state = roi.GapbsCheckpointState(
            mode="restore", iterations=2, measure_trial=1
        )
        self.assertEqual(state.resume_actions(), ())
        self.assertEqual(state.work_end(), ())
        self.assertEqual(
            state.work_begin(), ("reset", "record_start_tick")
        )
        self.assertEqual(state.work_end(), ("dump",))
        self.assertEqual(state.finish(), ("verify",))

    def test_restore_rejects_trial_one_begin_before_trial_zero_end(self):
        state = roi.GapbsCheckpointState(
            mode="restore", iterations=2, measure_trial=1
        )
        state.resume_actions()
        with self.assertRaisesRegex(roi.RoiSequenceError, "begin before"):
            state.work_begin()
        with self.assertRaisesRegex(
            roi.RoiSequenceError, "missing trial 0 end"
        ):
            state.finish()
```

- [ ] **Step 2: Run the tests and verify RED**

Run:

```bash
python3 -m unittest \
  tests/pyunit/amu/test_gapbs_checkpoint.py \
  -v
```

Expected: `ERROR`/`FAIL` because `GapbsCheckpointState` is absent.

- [ ] **Step 3: Implement the minimal pure state machine**

Add a `GapbsCheckpointState` whose constructor rejects modes outside
`{"save", "restore"}`, requires `measure_trial == 1` and `iterations == 2`,
starts save mode before trial 0, and starts restore mode with trial 0 active.
Expose only these methods and action strings:

```python
class GapbsCheckpointState:
    def __init__(self, mode, iterations, measure_trial):
        if mode not in ("save", "restore"):
            raise ValueError(f"invalid checkpoint mode: {mode}")
        if (iterations, measure_trial) != (2, 1):
            raise ValueError(
                "checkpoint mode requires iterations=2 and measure_trial=1"
            )
        self.mode = mode
        self.next_trial = 0 if mode == "save" else 1
        self.active_trial = None if mode == "save" else 0
        self.resumed = False
        self.saved = False
        self.ended = False

    def resume_actions(self):
        if self.mode != "restore" or self.resumed:
            raise RoiSequenceError("invalid or duplicate checkpoint resume")
        self.resumed = True
        return ()
```

Complete `work_begin`, `work_end`, `checkpoint_saved`, and `finish` to produce
exactly the sequences asserted above and reject every other transition.

- [ ] **Step 4: Run RED tests and legacy ROI tests**

```bash
python3 -m unittest \
  tests/pyunit/amu/test_gapbs_checkpoint.py \
  tests/pyunit/amu/pyunit_gapbs_amu_builder.py \
  -v
```

Expected: all tests pass; legacy `GapbsRoiState` behavior is unchanged.

- [ ] **Step 5: Commit**

```bash
git add \
  configs/example/gem5_library/gapbs_roi_state.py \
  tests/pyunit/amu/test_gapbs_checkpoint.py
git commit -m "configs: model GAPBS checkpoint ROI sequencing"
```

### Task 2: Content-Addressed Checkpoint Manifest

**Files:**
- Create: `scripts/gapbs_checkpoint.py`
- Modify: `tests/pyunit/amu/test_gapbs_checkpoint.py`

- [ ] **Step 1: Write failing identity and reuse tests**

```python
class GapbsCheckpointManifestTest(unittest.TestCase):
    def test_identity_covers_every_portability_input(self):
        identity = checkpoint.build_identity(
            binary=self.binary,
            graph=self.graph,
            graph_scale=20,
            arguments=["-f", str(self.graph), "-n", "2", "-v"],
            cores=2,
            memory_size="4GiB",
            gem5=self.gem5,
            config=self.config,
            kind="baseline",
            model_parameters={"cxl_link_delay": "0ns"},
        )
        self.assertEqual(identity["schema"], 1)
        self.assertEqual(identity["graph_scale"], 20)
        self.assertEqual(identity["graph_sha256"], sha256(self.graph))
        self.assertEqual(identity["binary_sha256"], sha256(self.binary))
        self.assertEqual(identity["arguments"][0], "-f")
        self.assertEqual(len(checkpoint.identity_key(identity)), 64)

    def test_reuse_rejects_changed_graph_or_incomplete_checkpoint(self):
        checkpoint.write_manifest(self.root, self.identity)
        self.assertTrue(checkpoint.validate_reuse(self.root, self.identity))
        changed = dict(self.identity, graph_sha256="0" * 64)
        with self.assertRaisesRegex(
            checkpoint.CheckpointError, "identity mismatch"
        ):
            checkpoint.validate_reuse(self.root, changed)
        (self.root / "m5.cpt").unlink()
        with self.assertRaisesRegex(
            checkpoint.CheckpointError, "missing checkpoint payload"
        ):
            checkpoint.validate_reuse(self.root, self.identity)
```

The fixture creates small local files and a checkpoint directory containing
`m5.cpt`; it does not mock hashing or JSON I/O.

- [ ] **Step 2: Run and verify RED**

```bash
python3 -m unittest \
  tests/pyunit/amu/test_gapbs_checkpoint.py \
  -v
```

Expected: import failure for `scripts/gapbs_checkpoint.py`.

- [ ] **Step 3: Implement hashing, canonical identity, and atomic manifest**

Implement:

```python
class CheckpointError(RuntimeError):
    pass

def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()

def identity_key(identity):
    encoded = json.dumps(
        identity, sort_keys=True, separators=(",", ":")
    ).encode()
    return hashlib.sha256(encoded).hexdigest()
```

`build_identity` must resolve paths and include schema, kind, all four file
hashes, graph scale, exact argument array, cores, memory size, and sorted model
parameters. `write_manifest` writes `manifest.json.tmp`, `fsync`s, and replaces
`manifest.json`. `validate_reuse` requires `m5.cpt`, a complete manifest,
exact identity equality, and `checkpoint_id == identity_key(identity)`.

- [ ] **Step 4: Run focused and aggregate Python tests**

```bash
python3 -m unittest \
  tests/pyunit/amu/test_gapbs_checkpoint.py \
  tests/pyunit/amu/pyunit_gapbs_amu_builder.py \
  -v
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add scripts/gapbs_checkpoint.py \
  tests/pyunit/amu/test_gapbs_checkpoint.py
git commit -m "scripts: identify reusable GAPBS checkpoints"
```

### Task 3: gem5 Checkpoint Save and Restore Modes

**Files:**
- Modify: `configs/example/gem5_library/x86-gapbs-amu-se.py`
- Modify: `scripts/build_gapbs_baseline_cxlmemuring.py`
- Modify: `scripts/build_gapbs_amu_cxlmemuring.py`
- Modify: `scripts/build_gapbs_cira_cxlmemuring.py`
- Modify: `tests/pyunit/amu/pyunit_gapbs_amu_builder.py`
- Modify: `tests/pyunit/amu/test_gapbs_checkpoint.py`

- [ ] **Step 1: Write failing config-contract tests**

Add subprocess dry-run/parser tests that require:

```python
self.assertIn("CheckpointResource", config_text)
self.assertIn('"--checkpoint-save"', config_text)
self.assertIn('"--checkpoint-restore"', config_text)
self.assertIn("GAPBS_CHECKPOINT_SAVED", config_text)
self.assertIn("GAPBS_CHECKPOINT_RESTORED", config_text)
self.assertIn("simulator.save_checkpoint", config_text)
```

Add direct parser invocations proving:

- save and restore together are rejected;
- save with `--cxl-memory` is rejected;
- restore without `--cxl-memory`, `--cpu timing`, `--roi-work-events`, or
  `--continue-after-roi` is rejected;
- checkpoint modes reject `--fast-forward-cpu`; and
- both modes require `--iterations 2 --measure-trial 1`.

- [ ] **Step 2: Run and verify RED**

```bash
python3 -m unittest \
  tests/pyunit/amu/test_gapbs_checkpoint.py \
  -v
```

Expected: failures for missing CLI and markers.

- [ ] **Step 3: Add mutually exclusive CLI and board setup**

Add:

```python
checkpoint_group = parser.add_mutually_exclusive_group()
checkpoint_group.add_argument("--checkpoint-save", type=Path)
checkpoint_group.add_argument("--checkpoint-restore", type=Path)
```

Save mode uses `CPUTypes.ATOMIC`, `NoCache()`, local memory, and no
`--cxl-memory`. Restore mode uses the requested Timing CPU, current private
L1/L2 hierarchy, and mandatory CXL memory. Pass:

```python
checkpoint = (
    CheckpointResource(local_path=str(args.checkpoint_restore.resolve()))
    if args.checkpoint_restore else None
)
board.set_se_binary_workload(
    BinaryResource(local_path=str(binary)),
    arguments=workload_arguments,
    env_list=[f"OMP_NUM_THREADS={args.cores}", *args.env],
    checkpoint=checkpoint,
)
```

For AMU save mode, connect its setup transport with delay `0ns`. For CIRA save
mode, attach to the setup memory-facing port without the measured L2 demand
probe. No offload operation executes before trial 0 work-begin.

- [ ] **Step 4: Wire save and restore event actions**

Save mode uses `GapbsCheckpointState("save", 2, 1)`. On `checkpoint`:

```python
simulator.save_checkpoint(args.checkpoint_save)
print(f"GAPBS_CHECKPOINT_SAVED path={args.checkpoint_save.resolve()}")
checkpoint_state.checkpoint_saved()
yield True
```

Restore mode calls `resume_actions()` immediately before `simulator.run()` and
prints:

```text
GAPBS_CHECKPOINT_RESTORED path=<absolute-path>
```

Its exact event order must be trial-0 work-end with no stats action,
trial-1 work-begin with reset/start-tick, then trial-1 work-end with one ROI
dump. It then continues to final GAPBS verification and classifies the final
exit exactly as the current verification path does. Do not print the legacy
CPU-switch marker.

- [ ] **Step 5: Add a post-verification two-core success exit**

First add failing tests requiring every ROI builder to place `m5_exit(0)`
after the complete `for (int iter...)` loop while preserving `m5_fail(0, 1)`
inside each failed verification branch. Require `classify_final_exit` to map
`m5_exit instruction encountered` to `("pass", 0)`.

Then patch each builder's `benchmark.h` rewrite so the generated function ends:

```cpp
  }
  if (cli.do_verify())
    m5_exit(0);
  PrintTime("Average Time", total_seconds / cli.num_trials());
```

The guest never reaches the trailing print under gem5. This exit is valid only
because every enabled verification has already run; any failure exits earlier
through `m5_fail`.

- [ ] **Step 6: Run unit tests and a local x86 checkpoint smoke**

```bash
python3 -m unittest \
  tests/pyunit/amu/test_gapbs_checkpoint.py \
  tests/pyunit/amu/pyunit_gapbs_amu_builder.py \
  -v

build/X86/gem5.opt \
  --outdir=/tmp/gapbs-x86-checkpoint-save \
  tests/gem5/checkpoint_tests/configs/x86-hello-save-checkpoint.py \
  --checkpoint-path /tmp/gapbs-x86-checkpoint
```

Expected: Python tests pass and log contains `Done taking checkpoint`.

- [ ] **Step 7: Commit**

```bash
git add \
  configs/example/gem5_library/x86-gapbs-amu-se.py \
  scripts/build_gapbs_baseline_cxlmemuring.py \
  scripts/build_gapbs_amu_cxlmemuring.py \
  scripts/build_gapbs_cira_cxlmemuring.py \
  tests/pyunit/amu/pyunit_gapbs_amu_builder.py \
  tests/pyunit/amu/test_gapbs_checkpoint.py
git commit -m "configs: save and restore GAPBS measured-trial checkpoints"
```

### Task 4: Runner Creation, Reuse, and Restore Orchestration

**Files:**
- Modify: `scripts/compare_gapbs_cxl_amu_cira.py`
- Modify: `tests/pyunit/amu/test_gapbs_checkpoint.py`

- [ ] **Step 1: Write failing command/provenance tests**

Use temporary executable test files and `--dry-run` to assert the printed
save command contains local Atomic setup and:

```text
--arguments -f <absolute-graph> -n 2 -v
--checkpoint-save <temporary-checkpoint>
--cpu atomic
--cxl-link-delay 0ns
```

Assert the restore command contains:

```text
--arguments -f <absolute-graph> -n 2 -v
--checkpoint-restore <completed-checkpoint>
--cpu timing
--cxl-memory
--cxl-link-delay 1us
```

Assert it contains neither `--fast-forward-cpu` nor `-g 20`. Assert
`SUMMARY_FIELDS` contains:

```python
(
    "graph_path", "graph_scale", "graph_sha256",
    "checkpoint_id", "checkpoint_manifest",
    "checkpoint_binary_sha256", "checkpoint_restores",
)
```

- [ ] **Step 2: Run and verify RED**

```bash
python3 -m unittest \
  tests/pyunit/amu/test_gapbs_checkpoint.py \
  -v
```

Expected: missing `--graph`, `--checkpoint-root`, and summary fields.

- [ ] **Step 3: Add runner CLI and exact workload construction**

Add required `--graph`, `--graph-scale` defaulting to 20,
`--checkpoint-root`, and `--reuse-checkpoints/--no-reuse-checkpoints`.
Construct:

```python
workload_arguments = [
    "-f", str(args.graph.resolve()),
    "-n", str(args.iterations),
]
if args.verify:
    workload_arguments.append("-v")
```

Reject a missing graph, a graph scale other than 20 for publication mode,
iterations other than 2, measured trial other than 1, CPU other than Timing,
and checkpoint mode combined with fast-forward.

- [ ] **Step 4: Implement checkpoint creation and atomic promotion**

For each exact binary/kind, build the identity, derive
`checkpoint_root / checkpoint_id`, and reuse only through
`validate_reuse`. Otherwise run save mode into
`checkpoint_root / f".{checkpoint_id}.tmp-{pid}"`. Require exit code zero,
exactly one save marker, `m5.cpt`, and then write the manifest and atomically
rename the directory to the final checkpoint path. A failed save leaves no
reusable final directory.

- [ ] **Step 5: Restore and summarize fail closed**

Run each measured configuration with `--checkpoint-restore`. Count exact log
markers:

```python
CHECKPOINT_SAVE_MARKER = "GAPBS_CHECKPOINT_SAVED path="
CHECKPOINT_RESTORE_MARKER = "GAPBS_CHECKPOINT_RESTORED path="
```

Status is `ok` only for return code zero, verification pass, one restore
marker, zero CPU-switch markers, one complete stats section, and valid
checkpoint reuse. Populate every provenance field; compute speedup only from
rows whose status is `ok`. Pass
`timeout=None if args.timeout == 0 else args.timeout` to both save and restore
subprocesses so zero means unlimited rather than an immediate timeout.

- [ ] **Step 6: Run runner and legacy suites**

```bash
python3 -m unittest \
  tests/pyunit/amu/test_gapbs_checkpoint.py \
  tests/pyunit/amu/pyunit_gapbs_amu_builder.py \
  -v
```

Expected: all pass and dry-run prints separate save/restore commands.

- [ ] **Step 7: Commit**

```bash
git add \
  scripts/compare_gapbs_cxl_amu_cira.py \
  tests/pyunit/amu/test_gapbs_checkpoint.py
git commit -m "scripts: run GAPBS CXL measurements from checkpoints"
```

### Task 5: Fail-Closed Checkpoint Evidence Validation

**Files:**
- Modify: `scripts/validate_gapbs_amu_latency_sweep.py`
- Modify: `tests/pyunit/amu/test_validate_gapbs_amu_latency_sweep.py`

- [ ] **Step 1: Extend valid fixtures, then add negative tests**

Update canonical summary fixtures with graph/checkpoint fields and manifests.
The valid restore `config.ini` must use a direct Timing CPU section and the
command:

```text
<binary> -f <absolute-g20.sg> -n 2 -v
```

Add one test for each rejection:

- graph hash differs from the canonical g20 hash;
- manifest identity/hash differs from the summary;
- checkpoint payload or manifest is missing;
- restore marker count is zero or two;
- CPU switch marker is present;
- workload uses `-g`, omits `-f`, or points at another graph;
- config has an Atomic/switch CPU rather than one Timing CPU;
- memory controller bypasses CXL; and
- stats contain zero or multiple ROI sections.

- [ ] **Step 2: Run and verify RED**

```bash
python3 -m unittest \
  tests/pyunit/amu/test_validate_gapbs_amu_latency_sweep.py \
  -v
```

Expected: new negative tests fail because old validator accepts the forged
evidence.

- [ ] **Step 3: Update required metadata and config checks**

Set checkpoint-mode expectations:

```python
METADATA_EXPECTED = {
    "scale": "20",
    "iterations": "2",
    "measured_trial": "1",
    "fast_forward_cpu": "",
    "roi_cpu": "timing",
    "cpu_switches": "0",
    "all_memory_cxl": "true",
    "graph_scale": "20",
}
EXPECTED_GRAPH_SHA256 = (
    "ce900a7147a073835a7450e8f1afedf9f13db6833652bf2f9647819be26bedb3"
)
```

Require one `BaseTimingSimpleCPU` and no `processor.start`/`processor.switch`
sections. Parse every workload command with `shlex`, require one `-f` whose
resolved path and SHA-256 match the summary/manifest, one `-n 2`, and no `-g`.
Retain the existing complete-range CXL topology and exact-delay checks.

- [ ] **Step 4: Validate manifest, logs, and stats cardinality**

Load `checkpoint_manifest`, recompute `checkpoint_id`, compare binary/graph/
gem5/config hashes and arguments, require `m5.cpt`, one restore marker, zero
switch markers, verification pass, and exactly one complete stats section.
Return no row on any mismatch.

- [ ] **Step 5: Run all evidence tests**

```bash
python3 -m unittest \
  tests/pyunit/amu/test_validate_gapbs_amu_latency_sweep.py \
  tests/pyunit/amu/test_gapbs_checkpoint.py \
  tests/pyunit/amu/pyunit_gapbs_amu_builder.py \
  -v
```

Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add \
  scripts/validate_gapbs_amu_latency_sweep.py \
  tests/pyunit/amu/test_validate_gapbs_amu_latency_sweep.py
git commit -m "scripts: validate GAPBS checkpoint evidence"
```

### Task 6: Small-Graph Bit-Exact End-to-End Proof

**Files:**
- Modify: `docs/amu-gapbs-benchmark.md`
- Generated only: `m5out/gapbs_graphs/g4.sg`
- Generated only: `m5out/gapbs_checkpoints/g4/`
- Generated only: `m5out/gapbs_cxl_amu_cira/checkpoint_g4_1us_20260724/`

- [ ] **Step 1: Create a deterministic serialized scale-4 graph**

Use the already built GAPBS converter corresponding to the baseline source:

```bash
m5out/gapbs_baseline_bins_latency_g20/src/gapbs/converter \
  -g 4 -b m5out/gapbs_graphs/g4.sg
sha256sum m5out/gapbs_graphs/g4.sg
```

Record converter SHA-256, graph SHA-256, byte size, vertex count, and directed
entry count in `m5out/gapbs_graphs/g4.manifest.json`.

- [ ] **Step 2: Run baseline, AMU, and CIRA save/restore at 1 us**

```bash
python3 scripts/compare_gapbs_cxl_amu_cira.py \
  --baseline-bin-dir m5out/gapbs_baseline_bins_latency_g20/bin \
  --amu-bin-dir m5out/gapbs_amu_bins_latency_g20/bin \
  --cira-bin-dir cira_pgo=m5out/gapbs_cira_bins_g20_future_20260723/bin \
  --benchmarks bfs,bc,pr,sssp \
  --graph m5out/gapbs_graphs/g4.sg \
  --graph-scale 4 \
  --iterations 2 --measure-trial 1 \
  --cpu timing --cores 2 \
  --checkpoint-root m5out/gapbs_checkpoints/g4 \
  --cxl-link-delay 1us --roi-work-events --verify \
  --outdir m5out/gapbs_cxl_amu_cira/checkpoint_g4_1us_20260724
```

Allow graph scale 4 only under an explicit `--smoke-test` flag so this output
cannot enter the publication validator.

- [ ] **Step 3: Prove bit-exactness and event/topology invariants**

Require all 12 rows to have `status=ok`, `verification=pass`, one restore
marker, zero switch markers, positive `simTicks`, and balanced AMU/CIRA
counters. Inspect each `config.ini` for a Timing CPU, `SerialLink` delay
1,000,000 ticks, and no direct memory-controller path.

- [ ] **Step 4: Compare checkpointed answers with direct small reference**

Run the same binaries and `g4.sg` directly with local Atomic memory and
verification, then compare each benchmark's verification result and printed
GAPBS answer/checksum. Any mismatch blocks scale 20.

- [ ] **Step 5: Document exact commands and commit**

Add the save/restore command, status checks, artifact locations, and the
statement that g4 is validation-only to `docs/amu-gapbs-benchmark.md`.

```bash
git add docs/amu-gapbs-benchmark.md
git commit -m "docs: describe checkpointed GAPBS CXL runs"
```

### Task 7: Scale-20 PR Gate in an Unlimited Background Unit

**Files:**
- Generated only: `m5out/gapbs_checkpoints/g20/`
- Generated only: `m5out/gapbs_cxl_amu_cira/checkpoint_g20_pr_1us_20260724/`
- Generated only: `m5out/background/gapbs-g20-pr-1us-20260724.log`

- [ ] **Step 1: Verify graph provenance before launch**

```bash
sha256sum m5out/gapbs_graphs/g20.sg
stat --printf='%s\n' m5out/gapbs_graphs/g20.sg
```

Expected SHA-256:

```text
ce900a7147a073835a7450e8f1afedf9f13db6833652bf2f9647819be26bedb3
```

Expected size: `133986161` bytes.

- [ ] **Step 2: Dry-run the exact PR gate**

```bash
python3 scripts/compare_gapbs_cxl_amu_cira.py \
  --baseline-bin-dir m5out/gapbs_baseline_bins_latency_g20/bin \
  --cira-bin-dir cira_pgo=m5out/gapbs_cira_bins_g20_future_20260723/bin \
  --benchmarks pr \
  --graph m5out/gapbs_graphs/g20.sg --graph-scale 20 \
  --iterations 2 --measure-trial 1 \
  --cpu timing --cores 2 \
  --checkpoint-root m5out/gapbs_checkpoints/g20 \
  --cxl-link-delay 1us --roi-work-events --verify \
  --dry-run \
  --outdir m5out/gapbs_cxl_amu_cira/checkpoint_g20_pr_1us_20260724
```

Expected: save commands use local Atomic/0 ns; restore commands use Timing,
all-CXL, 1 us; guest arguments use the absolute `g20.sg` path through `-f`.

- [ ] **Step 3: Launch one persistent background service without a limit**

```bash
sudo systemd-run \
  --unit=gapbs-g20-pr-1us-20260724 \
  --property=WorkingDirectory=/home/victoryang00/gem5-CXL/.worktrees/gapbs-latency-table \
  --property=StandardOutput=append:/home/victoryang00/gem5-CXL/.worktrees/gapbs-latency-table/m5out/background/gapbs-g20-pr-1us-20260724.log \
  --property=StandardError=inherit \
  /usr/bin/python3 scripts/compare_gapbs_cxl_amu_cira.py \
  --baseline-bin-dir m5out/gapbs_baseline_bins_latency_g20/bin \
  --cira-bin-dir cira_pgo=m5out/gapbs_cira_bins_g20_future_20260723/bin \
  --benchmarks pr \
  --graph m5out/gapbs_graphs/g20.sg --graph-scale 20 \
  --iterations 2 --measure-trial 1 \
  --cpu timing --cores 2 \
  --checkpoint-root m5out/gapbs_checkpoints/g20 \
  --cxl-link-delay 1us --roi-work-events --verify \
  --timeout 0 \
  --outdir m5out/gapbs_cxl_amu_cira/checkpoint_g20_pr_1us_20260724
```

The runner interprets `--timeout 0` as no subprocess timeout. Do not set
`RuntimeMaxSec`.

- [ ] **Step 4: Verify the service is genuinely running**

```bash
systemctl is-active gapbs-g20-pr-1us-20260724.service
systemctl show gapbs-g20-pr-1us-20260724.service \
  -p MainPID -p ActiveEnterTimestamp -p RuntimeMaxUSec -p ExecMainStatus
pgrep -af 'gem5.opt.*g20.sg'
tail -n 80 m5out/background/gapbs-g20-pr-1us-20260724.log
```

Expected: `active`, nonzero PID, `RuntimeMaxUSec=infinity`, and a live gem5
command containing `-f .../g20.sg`.

- [ ] **Step 5: Validate completed evidence before reporting speedup**

After the unit becomes inactive:

```bash
python3 scripts/validate_gapbs_amu_latency_sweep.py \
  --pr-gate \
  m5out/gapbs_cxl_amu_cira/checkpoint_g20_pr_1us_20260724
```

Expected:

```text
PASS: PR@1us scale-20 CIRA discriminator
```

If the service is still active, report only its current phase, PID, checkpoint
status, stats size, and last log activity. Do not report a speedup until this
validator passes.

### Task 8: Final Verification and Branch Handoff

**Files:**
- No additional production files.

- [ ] **Step 1: Run all targeted unit suites**

```bash
python3 -m unittest \
  tests/pyunit/amu/test_gapbs_checkpoint.py \
  tests/pyunit/amu/pyunit_gapbs_amu_builder.py \
  tests/pyunit/amu/test_validate_gapbs_amu_latency_sweep.py \
  tests/pyunit/amu/test_generate_gapbs_amu_latency_table.py \
  -v
```

Expected: all pass.

- [ ] **Step 2: Run syntax and whitespace checks**

```bash
python3 -m py_compile \
  configs/example/gem5_library/gapbs_roi_state.py \
  configs/example/gem5_library/x86-gapbs-amu-se.py \
  scripts/gapbs_checkpoint.py \
  scripts/compare_gapbs_cxl_amu_cira.py \
  scripts/validate_gapbs_amu_latency_sweep.py
git diff --check
```

Expected: no output and exit code zero.

- [ ] **Step 3: Confirm branch scope**

```bash
git status --short
git log --oneline ca04dfbc33..HEAD
```

Expected: only intended source/docs commits; generated `m5out` evidence remains
untracked and is not staged.

- [ ] **Step 4: Request code review before merge**

Use `superpowers:requesting-code-review`, address only actionable findings,
rerun the complete verification command, and merge back to
`codex-gem5-cira-amu-eval` only after tests and the small bit-exact proof pass.
The long scale-20 background unit may continue after the code merge, but its
result remains unpublished until the PR gate validator passes.
