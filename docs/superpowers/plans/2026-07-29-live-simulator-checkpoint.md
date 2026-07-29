# Live Simulator Checkpoint and Reboot Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Checkpoint the exact live AMU/gem5 and M2NDP/NDPSim process trees, reboot the host, and restore both from validated CRIU images.

**Architecture:** A repository Python tool owns provenance, CRIU command construction, image validation, and the reboot gate. Thin systemd units invoke its restore command at boot; the existing application-level resume units are disabled only after both final images validate.

**Tech Stack:** Python 3 standard library, CRIU/crit, systemd, unittest, SHA-256 manifests

---

### Task 1: Model checkpoint manifests and fail-closed validation

**Files:**
- Create: `scripts/live_simulator_checkpoint.py`
- Create: `tests/pyunit/m2ndp/test_live_simulator_checkpoint.py`

- [ ] **Step 1: Write failing manifest tests**

Add tests that construct temporary image files and assert:

```python
manifest = checkpoint.build_manifest(
    name="amu",
    unit="gapbs-matched-pr-spmv-amu-g20-resume.service",
    root_pid=123,
    process_tree=[{"pid": 123, "ppid": 1, "cmdline": ["python3", "runner.py"]}],
    inputs=[binary, graph],
    image_dir=images,
    progress={"kind": "gem5_tick", "value": 2850617862},
    host=host,
)
self.assertEqual(manifest["schema"], 1)
self.assertEqual(manifest["images"]["inventory.img"]["sha256"], expected)
checkpoint.validate_manifest(manifest, require_same_kernel=True)
```

Also assert rejection of a missing image, changed hash, changed executable
input, empty process tree, mismatched kernel, and a transaction containing
fewer than both `amu` and `m2ndp`.

- [ ] **Step 2: Run the focused tests and verify failure**

Run:

```bash
python3 -m unittest tests.pyunit.m2ndp.test_live_simulator_checkpoint -v
```

Expected: import failure because `scripts/live_simulator_checkpoint.py` does
not exist.

- [ ] **Step 3: Implement manifest helpers**

Implement immutable workload definitions, streaming SHA-256, host identity,
process-tree capture, input hashing, atomic JSON writes, image inventory, and
`validate_manifest`. Validation must raise `CheckpointError` on the first
missing or mismatched fact and must never repair evidence implicitly.

- [ ] **Step 4: Run the focused tests**

Run the command from Step 2.

Expected: all manifest and transaction tests pass.

- [ ] **Step 5: Commit**

```bash
git add scripts/live_simulator_checkpoint.py \
  tests/pyunit/m2ndp/test_live_simulator_checkpoint.py
git commit -m "tools: model live simulator checkpoint evidence"
```

### Task 2: Add CRIU preflight, probe, and dump commands

**Files:**
- Modify: `scripts/live_simulator_checkpoint.py`
- Modify: `tests/pyunit/m2ndp/test_live_simulator_checkpoint.py`

- [ ] **Step 1: Write failing command-construction tests**

Use a fake command runner and assert that preflight requires `criu`, `crit`,
32 GiB free space, active source units, unchanged MainPIDs, and a successful
`criu check`. Assert that a probe includes `--leave-running`, while a final
dump omits it:

```python
self.assertIn("--leave-running", checkpoint.criu_dump_command(job, probe=True))
self.assertNotIn("--leave-running", checkpoint.criu_dump_command(job, probe=False))
```

Assert that nonzero CRIU status leaves the transaction state at `blocked` and
never invokes reboot.

- [ ] **Step 2: Run tests and verify failure**

Run:

```bash
python3 -m unittest tests.pyunit.m2ndp.test_live_simulator_checkpoint -v
```

Expected: failures for undefined preflight and CRIU command helpers.

- [ ] **Step 3: Implement preflight and capture**

Add CLI subcommands:

```text
preflight --root SNAPSHOT_ROOT
probe --root SNAPSHOT_ROOT --job amu|m2ndp
dump --root SNAPSHOT_ROOT --job amu|m2ndp
validate --root SNAPSHOT_ROOT [--job amu|m2ndp]
```

Use argument arrays, never a shell string. Create temporary image directories
under the final parent and publish them with `os.replace` only after CRIU exits
zero and image validation passes. Capture progress from the AMU gem5 log and
the latest NDPSim `Launch ID` record.

- [ ] **Step 4: Run tests**

Run the command from Step 2.

Expected: all command, failure, and manifest tests pass.

- [ ] **Step 5: Commit**

```bash
git add scripts/live_simulator_checkpoint.py \
  tests/pyunit/m2ndp/test_live_simulator_checkpoint.py
git commit -m "tools: add fail-closed CRIU capture flow"
```

### Task 3: Add restore units and reboot gate

**Files:**
- Modify: `scripts/live_simulator_checkpoint.py`
- Create: `util/systemd/gapbs-amu-criu-restore.service`
- Create: `util/systemd/m2ndp-criu-restore.service`
- Modify: `tests/pyunit/m2ndp/test_live_simulator_checkpoint.py`

- [ ] **Step 1: Write failing restore and gate tests**

Assert exact unit ordering, absolute manifest paths, `RemainAfterExit=yes`, and
that the reboot gate requires two valid manifests, two enabled restore units,
two disabled source units, no original PIDs, and a dry-run reboot success.
Assert that any single false predicate rejects reboot.

- [ ] **Step 2: Run tests and verify failure**

Run:

```bash
python3 -m unittest tests.pyunit.m2ndp.test_live_simulator_checkpoint -v
```

Expected: failures for missing unit templates and gate functions.

- [ ] **Step 3: Implement restore and gate commands**

Add:

```text
install-units --root SNAPSHOT_ROOT
restore --manifest MANIFEST
verify-restored --manifest MANIFEST
arm-reboot --root SNAPSHOT_ROOT
```

`restore` validates hashes before invoking `criu restore --restore-detached`.
`arm-reboot` writes `ready_for_reboot` only after every predicate passes. It
does not call reboot; reboot remains a separate explicit operational step.

- [ ] **Step 4: Run focused and neighboring tests**

Run:

```bash
python3 -m unittest \
  tests.pyunit.m2ndp.test_live_simulator_checkpoint \
  tests.pyunit.m2ndp.test_run_m2ndp_g20_pr_spmv \
  tests.pyunit.m2ndp.test_run_gapbs_matched_pr_spmv_variants -v
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add scripts/live_simulator_checkpoint.py \
  util/systemd/gapbs-amu-criu-restore.service \
  util/systemd/m2ndp-criu-restore.service \
  tests/pyunit/m2ndp/test_live_simulator_checkpoint.py
git commit -m "tools: restore live simulator trees after reboot"
```

### Task 4: Validate CRIU on a disposable process tree

**Files:**
- Generated only: `m5out/live-reboot-checkpoint-20260729/smoke/`

- [ ] **Step 1: Install CRIU and record its version**

Run the native package-manager install, then:

```bash
criu --version
sudo criu check
```

Expected: a version is printed and CRIU reports that required kernel features
are available. Any error blocks the operation.

- [ ] **Step 2: Run the disposable tree smoke test**

Launch a Python parent with a child and an append-open log, dump it, confirm it
is gone, restore it detached, and confirm both PIDs and the log continue.

Expected: dump and restore return zero, the process tree is present, and the
log grows after restore.

- [ ] **Step 3: Run real non-destructive probes**

Run:

```bash
sudo python3 scripts/live_simulator_checkpoint.py probe \
  --root m5out/live-reboot-checkpoint-20260729 --job amu
sudo python3 scripts/live_simulator_checkpoint.py probe \
  --root m5out/live-reboot-checkpoint-20260729 --job m2ndp
```

Expected: both commands return zero and the original MainPIDs remain unchanged
and CPU-active.

### Task 5: Capture both live workloads and arm boot restore

**Files:**
- Generated: `m5out/live-reboot-checkpoint-20260729/`
- System state: runtime restart drop-ins and `/etc/systemd/system` restore units

- [ ] **Step 1: Disable abnormal restart without stopping workloads**

Install runtime drop-ins with `Restart=no`, reload systemd, and verify both
MainPIDs still match their preflight manifests.

- [ ] **Step 2: Final-dump and validate AMU**

Run:

```bash
sudo python3 scripts/live_simulator_checkpoint.py dump \
  --root m5out/live-reboot-checkpoint-20260729 --job amu
sudo python3 scripts/live_simulator_checkpoint.py validate \
  --root m5out/live-reboot-checkpoint-20260729 --job amu
```

Expected: both return zero, AMU's old process tree is absent, and its manifest
and all image hashes validate.

- [ ] **Step 3: Final-dump and validate M2NDP**

Run the corresponding `dump` and `validate` commands with `--job m2ndp`.

Expected: both return zero, M2NDP's old process tree is absent, and its
manifest and all image hashes validate. If this fails, immediately restore
AMU on the current boot and stop the procedure.

- [ ] **Step 4: Install restore units and arm reboot**

Run:

```bash
sudo python3 scripts/live_simulator_checkpoint.py install-units \
  --root m5out/live-reboot-checkpoint-20260729
sudo python3 scripts/live_simulator_checkpoint.py arm-reboot \
  --root m5out/live-reboot-checkpoint-20260729
```

Expected: source units disabled, restore units enabled, and transaction state
equals `ready_for_reboot`.

### Task 6: Reboot, verify restoration, and resume monitoring

**Files:**
- Generated: restore logs and updated transaction state

- [ ] **Step 1: Reboot**

Run:

```bash
sudo systemctl reboot
```

Expected: the current session disconnects and the host boots normally.

- [ ] **Step 2: Verify both restored process trees**

After reconnecting, run `verify-restored` for both manifests and inspect both
restore unit statuses.

Expected: both process trees match their manifests, consume CPU, and continue
at or after their captured progress markers.

- [ ] **Step 3: Verify benchmark evidence remains fail-closed**

Confirm no summary or table row was published merely because of restoration.
Continue monitoring until the existing AMU and M2NDP validators pass.

- [ ] **Step 4: Commit final operational documentation**

Record the snapshot transaction path, capture markers, restored boot ID, and
verification commands in `docs/amu-gapbs-benchmark.md`, then commit:

```bash
git add docs/amu-gapbs-benchmark.md
git commit -m "docs: record live simulator reboot recovery"
```

