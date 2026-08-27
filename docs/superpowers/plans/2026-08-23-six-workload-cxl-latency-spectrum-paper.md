# Six-Workload CXL Latency Spectrum and Paper Update Implementation Plan

Revised: 2026-08-27 to select layout A, bind the fresh qualification manifest,
and add six standalone absolute-latency/speedup figures plus PNG exports.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build and run a fail-closed 96-point Vanilla/AMU/CIRA/M2NDP latency-spectrum campaign over six matched workloads, publish validated raw data and figures, and update the CIRA paper.

**Architecture:** Repair M2NDP PageRank timing by grouping four independent partition launches into one phase command while retaining the sequential FuncSim trace. Add one shared latency contract, run four immutable per-latency breadth campaigns over content-addressed prepared inputs, and aggregate only terminal complete manifests. Qualification remains a hard prerequisite, and paper generation consumes only the validated aggregate.

**Tech Stack:** Python 3 `unittest`, gem5 X86 timing simulation, OpenMP C/C++/Fortran adapters, M2NDP FuncSim/NDPSim, JSON/CSV evidence manifests, Matplotlib, LaTeX.

---

## File map

- Modify `scripts/m2ndp_pagerank_trace.py`: emit separate functional launches and grouped NDPSim timing commands.
- Modify `scripts/m2ndp_results.py`: parse the grouped measured-trial marker and validate phase/partition completion.
- Modify `scripts/run_m2ndp_g20_pr_spmv.py`: run grouped timing with intra-command concurrency and bind the new trace metadata.
- Modify `scripts/gapbs_pr_experiment_profiles.py`: expose the shared four-value latency contract without weakening the 1-us qualification profile.
- Create `scripts/cxl_latency_spectrum.py`: normalize labels/ticks and validate per-latency identity.
- Modify `scripts/run_matched_breadth_gem5.py`: accept and verify an explicit latency instead of hard-coding 1 us.
- Modify `scripts/m2ndp_workload_trace.py`: accept a latency-bound calibration and verify the selected M2NDP configuration.
- Modify `scripts/build_matched_breadth_workloads.py`: include latency templates and shared-object records in the prepared manifest.
- Modify `scripts/run_cira_amu_m2ndp_breadth.py`: record latency in state, commands, checkpoints, and evidence validation.
- Create `scripts/run_cira_amu_m2ndp_latency_spectrum.py`: orchestrate four immutable campaigns and shared artifacts.
- Create `scripts/generate_cira_amu_m2ndp_latency_spectrum.py`: validate 96 coordinates and generate raw CSV/JSON, the selected 2-by-3 figure, six standalone dual-panel figures, PNG previews, and LaTeX.
- Modify `scripts/generate_cira_amu_m2ndp_comparison.py`: reuse shared row/label/color helpers without accepting stale 1-us breadth data.
- Add focused tests under `tests/pyunit/m2ndp/` and `tests/pyunit/cross_system/`.
- Modify the independent paper repository's `sections/evaluation.tex` and `gapbs-vtune-cxl-table.tex` only after complete evidence exists.
- Add generated paper artifacts under `6472666535e6f359942ddac6/fig/` and `6472666535e6f359942ddac6/data/`.

### Task 1: Group M2NDP PageRank partitions by timing phase

**Files:**
- Modify: `scripts/m2ndp_pagerank_trace.py`
- Test: `tests/pyunit/m2ndp/test_m2ndp_trace.py`

- [ ] **Step 1: Write the failing grouped-timing trace test**

Extend `test_formal_trace_is_four_way_double_buffered_and_roi_starts_at_k2` with these assertions:

```python
self.assertEqual(result.funcsim_launches, 165)
self.assertEqual(result.ndpsim_launches, 84)
self.assertEqual(result.measure_marker, "K2_CONTRIB_TRIAL1_GROUP")

timing_names = (
    self.root / "formal-trace/0/kernelslist.g"
).read_text().splitlines()
self.assertEqual(len(timing_names), 84)
self.assertEqual(timing_names[42], "K0_INIT_TRIAL1_GROUP")
self.assertEqual(timing_names[44], "K2_CONTRIB_TRIAL1_GROUP")

launch_lines = (
    self.root
    / "formal-trace/0/K2_CONTRIB_TRIAL1_GROUP_launch.txt"
).read_text().splitlines()
self.assertEqual(len(launch_lines), 4)
self.assertEqual(
    [line.split()[9:11] for line in launch_lines],
    [["0x0", "0x1"], ["0x1", "0x1"],
     ["0x2", "0x1"], ["0x3", "0x0"]],
)
self.assertEqual(meta["timing_commands_per_trial"], 42)
self.assertEqual(meta["timing_launch_records_per_trial"], 165)
```

- [ ] **Step 2: Run the test and confirm the old serialized contract fails**

Run:

```bash
python3 -m unittest \
  tests.pyunit.m2ndp.test_m2ndp_trace.PageRankTraceTest.test_formal_trace_is_four_way_double_buffered_and_roi_starts_at_k2 \
  -v
```

Expected: failure showing 330 timing commands or the missing `*_GROUP` file.

- [ ] **Step 3: Add a timing-group constructor**

Add a focused helper beside `_formal_trial_records`:

```python
def _formal_timing_records(records):
    grouped = []
    cursor = 0
    while cursor < len(records):
        name, kernel, launch = records[cursor]
        if kernel == "K1_META":
            grouped.append((name, kernel, launch, 1))
            cursor += 1
            continue
        prefix = name.rsplit("_PART", 1)[0]
        phase = records[cursor:cursor + 4]
        if (
            len(phase) != 4
            or any(row[1] != kernel for row in phase)
            or [row[0] for row in phase]
            != [f"{prefix}_PART{index}" for index in range(4)]
        ):
            raise artifacts.EvidenceError(
                f"formal timing phase is not four contiguous partitions: {prefix}"
            )
        grouped.append((
            f"{prefix}_GROUP",
            kernel,
            "".join(row[2] for row in phase),
            4,
        ))
        cursor += 4
    return grouped
```

Use the launch-specific records for `funcsim.sequence`; write grouped aliases,
four-line launch files, and grouped names to `kernelslist.g` for timing. Record
both logical launch count and timing command count in `trace.meta.json`.

- [ ] **Step 4: Run the complete trace tests**

Run:

```bash
python3 -m unittest tests.pyunit.m2ndp.test_m2ndp_trace -v
```

Expected: all tests pass, including unchanged strict float32 and memory-map checks.

- [ ] **Step 5: Commit the trace grouping**

```bash
git add scripts/m2ndp_pagerank_trace.py \
  tests/pyunit/m2ndp/test_m2ndp_trace.py
git commit -m "perf: group M2NDP PageRank timing partitions"
```

### Task 2: Bind grouped timing markers and launch cardinality

**Files:**
- Modify: `scripts/m2ndp_results.py`
- Modify: `scripts/run_m2ndp_g20_pr_spmv.py`
- Test: `tests/pyunit/m2ndp/test_m2ndp_results.py`
- Test: `tests/pyunit/m2ndp/test_run_m2ndp_g20_pr_spmv.py`

- [ ] **Step 1: Write failing parser and command tests**

Add an NDPSim log fixture containing:

```text
Launching NDP kernel: K2_CONTRIB_TRIAL1_GROUP.traceg at cycle 1000
Gantt info: host 0 finished NDP kernel K2_CONTRIB launch id 40 at core cycle 1200
Gantt info: host 0 finished NDP kernel K2_CONTRIB launch id 41 at core cycle 1201
Gantt info: host 0 finished NDP kernel K2_CONTRIB launch id 42 at core cycle 1202
Gantt info: host 0 finished NDP kernel K2_CONTRIB launch id 43 at core cycle 1203
EXPR FINISHED 5000
CORE period: 0.5
MEMROY MATCH SUCCESS
```

Assert `parse_ndpsim` returns start 1000 and measured cycles 4000. In the
runner test, assert `_ndpsim_command(paths)` ends with:

```python
self.assertEqual(command[command.index("--serial_launch") + 1], "false")
```

- [ ] **Step 2: Run both focused test modules and confirm failure**

```bash
python3 -m unittest \
  tests.pyunit.m2ndp.test_m2ndp_results \
  tests.pyunit.m2ndp.test_run_m2ndp_g20_pr_spmv -v
```

Expected: grouped marker is not recognized and serial launch remains true.

- [ ] **Step 3: Implement exact grouped-marker parsing and concurrent launch issue**

In `parse_ndpsim`, recognize `K2_CONTRIB_TRIAL1_GROUP` before the legacy
markers. In `_ndpsim_command`, change only the intra-command launch flag:

```python
"--serial_launch", "false",
```

Do not alter `SimulationRunner::check_single_simulation_finished`; that
existing command boundary is the required phase barrier. Validate summary
metadata with:

```python
if meta["timing_commands_per_trial"] != 42:
    raise artifacts.EvidenceError("M2NDP timing phase count differs")
if meta["timing_launch_records_per_trial"] != 165:
    raise artifacts.EvidenceError("M2NDP logical launch count differs")
```

- [ ] **Step 4: Run M2NDP unit tests**

```bash
python3 -m unittest discover -s tests/pyunit/m2ndp -p 'test_*.py' -v
```

Expected: zero failures.

- [ ] **Step 5: Commit parser and runner changes**

```bash
git add scripts/m2ndp_results.py scripts/run_m2ndp_g20_pr_spmv.py \
  tests/pyunit/m2ndp/test_m2ndp_results.py \
  tests/pyunit/m2ndp/test_run_m2ndp_g20_pr_spmv.py
git commit -m "fix: validate phase-parallel M2NDP timing"
```

### Task 3: Prove grouped M2NDP correctness and performance in a fresh diagnostic

**Files:**
- No source changes expected
- Output: `/mnt/disk0/gem5-CXL-eval/pr-offload-m2ndp-grouped-diagnostic-r1/`

- [ ] **Step 1: Build the current simulator and M5 library**

```bash
scons build/X86/gem5.opt -j4
scons build/x86/out/m5 --directory=util/m5 -j4
```

Expected: both commands exit 0.

- [ ] **Step 2: Run the g12 M2NDP pipeline in a fresh root**

Use the same frozen g12 graph and manifest as r4:

```bash
python3 scripts/run_m2ndp_g20_pr_spmv.py \
  --graph /mnt/disk0/gem5-CXL-g14-eval/graphs/g12.sg \
  --graph-scale 12 \
  --graph-manifest /mnt/disk0/gem5-CXL-g14-eval/graphs/g12.manifest.json \
  --profile pr-offload-4thread-1us \
  --cxl-link-delay 1us \
  --gem5 build/X86/gem5.opt \
  --m5-library util/m5/build/x86/out/libm5.a \
  --cxlmemuring /home/victoryang00/CXLMemUring \
  --m2ndp-root /mnt/disk0/M2NDP-public \
  --outdir /mnt/disk0/gem5-CXL-eval/pr-offload-m2ndp-grouped-current
```

Expected: every stage passes, FuncSim is strict bit-exact, NDPSim reports
`MEMROY MATCH SUCCESS`, and metadata records 84 timing commands and 330
logical launch records across both trials. The measured ROI begins at trial-1
K2 and covers 40 timing commands containing 160 logical partition launches.

- [ ] **Step 3: Recompute the result without trusting stored speedup**

```bash
python3 - <<'PY'
import csv
from decimal import Decimal
from pathlib import Path
p = Path('/mnt/disk0/gem5-CXL-eval/pr-offload-m2ndp-grouped-current/summary.csv')
row = next(csv.DictReader(p.open(newline='', encoding='utf-8')))
gem5 = Decimal(row['gem5_seconds'])
m2ndp = Decimal(row['m2ndp_seconds'])
print('verification', row['verification'])
print('speedup', gem5 / m2ndp)
PY
```

Expected: verification is `pass`; the observed speedup is reported exactly,
not assumed to meet the qualification interval.

### Task 4: Add one authoritative CXL latency contract

**Files:**
- Create: `scripts/cxl_latency_spectrum.py`
- Modify: `scripts/gapbs_pr_experiment_profiles.py`
- Create: `tests/pyunit/cross_system/test_cxl_latency_spectrum.py`

- [ ] **Step 1: Write failing exact-label tests**

```python
class LatencyContractTest(unittest.TestCase):
    def test_labels_and_ticks_are_exact(self):
        self.assertEqual(latency.LABELS, ("200ns", "500ns", "1us", "2us"))
        self.assertEqual(latency.ticks("200ns"), 200_000)
        self.assertEqual(latency.ticks("500ns"), 500_000)
        self.assertEqual(latency.ticks("1us"), 1_000_000)
        self.assertEqual(latency.ticks("2us"), 2_000_000)

    def test_unknown_or_noncanonical_labels_fail(self):
        for value in ("1000ns", "1µs", "0ns", "3us"):
            with self.assertRaises(latency.LatencyError):
                latency.ticks(value)
```

- [ ] **Step 2: Run the new module test and confirm import failure**

```bash
python3 -m unittest tests.pyunit.cross_system.test_cxl_latency_spectrum -v
```

Expected: `scripts.cxl_latency_spectrum` is missing.

- [ ] **Step 3: Implement the immutable mapping**

```python
LABELS = ("200ns", "500ns", "1us", "2us")
TICKS = dict(zip(LABELS, (200_000, 500_000, 1_000_000, 2_000_000)))

class LatencyError(RuntimeError):
    pass

def ticks(label):
    try:
        return TICKS[label]
    except (KeyError, TypeError) as error:
        raise LatencyError(f"unsupported CXL latency: {label}") from error
```

Import this mapping from `gapbs_pr_experiment_profiles.py` and retain the
qualification profile's `latencies=("1us",)`.

- [ ] **Step 4: Run profile and latency tests**

```bash
python3 -m unittest \
  tests.pyunit.cross_system.test_cxl_latency_spectrum \
  tests.pyunit.m2ndp.test_gapbs_pr_experiment_profiles -v
```

Expected: zero failures.

- [ ] **Step 5: Commit the shared latency contract**

```bash
git add scripts/cxl_latency_spectrum.py \
  scripts/gapbs_pr_experiment_profiles.py \
  tests/pyunit/cross_system/test_cxl_latency_spectrum.py
git commit -m "feat: define formal CXL latency spectrum"
```

### Task 5: Parameterize matched gem5 and M2NDP breadth timing

**Files:**
- Modify: `scripts/run_matched_breadth_gem5.py`
- Modify: `scripts/m2ndp_workload_trace.py`
- Test: `tests/pyunit/cross_system/test_matched_breadth_gem5.py`
- Test: `tests/pyunit/cross_system/test_m2ndp_workload_trace.py`

- [ ] **Step 1: Write failing 200-ns and 2-us command/config tests**

For matched gem5, create options with `cxl_link_delay="200ns"` and assert:

```python
self.assertEqual(
    command[command.index("--cxl-link-delay") + 1], "200ns"
)
self.assertEqual(evidence["cxl_link_delay_ticks"], 200_000)
```

For M2NDP, pass a 2-us calibration and assert the evidence stores
`cxl_link_delay="2us"`, rejects a 1-us package/calibration mismatch, and still
requires one memory-match marker.

- [ ] **Step 2: Run focused tests and confirm hard-coded 1-us failures**

```bash
python3 -m unittest \
  tests.pyunit.cross_system.test_matched_breadth_gem5 \
  tests.pyunit.cross_system.test_m2ndp_workload_trace -v
```

Expected: selected latency is ignored or rejected by the current code.

- [ ] **Step 3: Thread the explicit latency through both backends**

Add `--cxl-link-delay` with `choices=latency.LABELS` to matched gem5. Replace
the literal command value and topology check with:

```python
"--cxl-link-delay", options.cxl_link_delay,
```

```python
expected_ticks = latency.ticks(options.cxl_link_delay)
if topology["cxl_link_delay_ticks"] != expected_ticks:
    raise ReplayError("gem5 CXL latency differs from the campaign identity")
```

For M2NDP, require calibration fields `cxl_delay` and `cxl_link_delay` to equal
the selected label, then store that label and its exact ticks in timing
evidence. Do not alter the calibrated core period or link-cycle residual gate.

- [ ] **Step 4: Run both full test modules**

```bash
python3 -m unittest \
  tests.pyunit.cross_system.test_matched_breadth_gem5 \
  tests.pyunit.cross_system.test_m2ndp_workload_trace -v
```

Expected: zero failures across all four labels and mismatch rejection cases.

- [ ] **Step 5: Commit backend parameterization**

```bash
git add scripts/run_matched_breadth_gem5.py scripts/m2ndp_workload_trace.py \
  tests/pyunit/cross_system/test_matched_breadth_gem5.py \
  tests/pyunit/cross_system/test_m2ndp_workload_trace.py
git commit -m "feat: parameterize breadth CXL latency"
```

### Task 6: Make each breadth campaign latency-immutable

**Files:**
- Modify: `scripts/run_cira_amu_m2ndp_breadth.py`
- Modify: `scripts/build_matched_breadth_workloads.py`
- Test: `tests/pyunit/cross_system/test_run_cira_amu_m2ndp_breadth.py`
- Test: `tests/pyunit/cross_system/test_matched_region_build.py`

- [ ] **Step 1: Write failing state, resume, and evidence tests**

Assert a new state records both label and ticks:

```python
state = breadth.new_state(
    identity(), specs(), g20_graph_sha256=sha("g20"),
    cxl_link_delay="500ns",
)
self.assertEqual(state["cxl_link_delay"], "500ns")
self.assertEqual(state["cxl_link_delay_ticks"], 500_000)
```

Add cases proving a 500-ns runner rejects 1-us prepared actions, timing
evidence, and resume checkpoints. Add a manifest test proving functional
commands contain no latency-specific output path while timing commands render
`{{cxl_link_delay}}`.

- [ ] **Step 2: Run both tests and confirm the missing latency identity**

```bash
python3 -m unittest \
  tests.pyunit.cross_system.test_run_cira_amu_m2ndp_breadth \
  tests.pyunit.cross_system.test_matched_region_build -v
```

Expected: new state signature or exact latency assertions fail.

- [ ] **Step 3: Implement latency-bound state and templates**

Extend `Action` rendering with `cxl_link_delay` and
`cxl_link_delay_ticks`. Add `--cxl-link-delay` to the breadth CLI and pass it
to `new_state`, `_validate_window_evidence`, and identity construction. Replace
the fixed check with:

```python
if evidence.get("cxl_link_delay_ticks") != state["cxl_link_delay_ticks"]:
    raise BreadthError("timing evidence CXL latency differs")
```

Prepared action commands invoke matched gem5 or M2NDP timing with the rendered
canonical label. Functional actions remain bound to the shared trace and raw
reference hashes rather than a timing value.

- [ ] **Step 4: Run the full breadth state-machine tests**

```bash
python3 -m unittest \
  tests.pyunit.cross_system.test_run_cira_amu_m2ndp_breadth \
  tests.pyunit.cross_system.test_matched_region_build -v
```

Expected: zero failures, including stale checkpoint and delay mismatch tests.

- [ ] **Step 5: Commit campaign identity changes**

```bash
git add scripts/run_cira_amu_m2ndp_breadth.py \
  scripts/build_matched_breadth_workloads.py \
  tests/pyunit/cross_system/test_run_cira_amu_m2ndp_breadth.py \
  tests/pyunit/cross_system/test_matched_region_build.py
git commit -m "feat: bind breadth campaigns to CXL latency"
```

### Task 7: Add the four-campaign spectrum orchestrator

**Files:**
- Create: `scripts/run_cira_amu_m2ndp_latency_spectrum.py`
- Create: `tests/pyunit/cross_system/test_run_cira_amu_m2ndp_latency_spectrum.py`

- [ ] **Step 1: Write failing matrix and shared-object tests**

```python
self.assertEqual(
    spectrum.coordinates(),
    tuple((latency, workload, system)
          for latency in ("200ns", "500ns", "1us", "2us")
          for workload in spectrum.WORKLOADS
          for system in ("vanilla", "amu", "cira", "m2ndp")),
)
self.assertEqual(len(spectrum.coordinates()), 96)
```

Add tests that reject a non-content-addressed shared object, a latency root
whose identity differs, a missing complete manifest, an aggregate attempt
containing one inconclusive campaign, and a qualification manifest whose
performance gate is not `passed`. The accepted qualification fixture must
contain zero offenders and primary/replay rows for all four g12 systems.

- [ ] **Step 2: Run the new test and confirm module import failure**

```bash
python3 -m unittest \
  tests.pyunit.cross_system.test_run_cira_amu_m2ndp_latency_spectrum -v
```

Expected: the spectrum orchestrator module is missing.

- [ ] **Step 3: Implement the aggregate state machine**

Use this immutable top-level shape:

```python
def new_state(shared, qualification, identity):
    return {
        "schema": 1,
        "status": "planned",
        "identity": identity,
        "shared": shared,
        "qualification": qualification,
        "latencies": {
            label: {"status": "pending", "root": f"latency/{label}"}
            for label in latency.LABELS
        },
    }
```

Implement `validate_qualification(path, calibration_sha256)` to require
`performance_gate.status == "passed"`, an empty offender list, all four
primary and replay g12 rows with `verification == "pass"`, and the exact
calibration hash already bound by `shared["calibration"]`. Add required CLI
argument `--qualification`; bind its absolute path and SHA-256 into aggregate
state and identity. For each latency, invoke
`run_cira_amu_m2ndp_breadth.py` with the same accepted inputs, calibration
authority, shared prepared manifest, and that label. Record command hashes and
child `complete.json` hashes. Resume only a child whose identity and bound
qualification hash match. Write aggregate `complete.json` only after all four
children are complete.

- [ ] **Step 4: Run orchestrator tests**

```bash
python3 -m unittest \
  tests.pyunit.cross_system.test_run_cira_amu_m2ndp_latency_spectrum -v
```

Expected: zero failures.

- [ ] **Step 5: Commit the orchestrator**

```bash
git add scripts/run_cira_amu_m2ndp_latency_spectrum.py \
  tests/pyunit/cross_system/test_run_cira_amu_m2ndp_latency_spectrum.py
git commit -m "feat: orchestrate workload latency spectrum"
```

### Task 8: Run fresh g12 qualification and stop on any offender

**Files:**
- Output: `/mnt/disk0/gem5-CXL-eval/pr-offload-formal-latency-spectrum-r1/`

- [ ] **Step 1: Run the full focused test gate**

```bash
python3 -m unittest discover -s tests/pyunit/m2ndp -p 'test_*.py' -v
python3 -m unittest discover -s tests/pyunit/cross_system -p 'test_*.py' -v
git diff --check
```

Expected: zero failures and no whitespace errors.

- [ ] **Step 2: Launch a fresh qualification root**

Use the existing frozen inputs, calibration, policy, and variant build roots,
but a new output root bound to the current source hash:

```bash
PATH=/mnt/disk0/gem5-CXL-eval/toolchains/m2ndp-conan1/bin:/opt/miniconda3/envs/infer_machine/bin:$PATH \
python3 scripts/run_pr_asymmetric_offload.py \
  --inputs /mnt/disk0/gem5-CXL-eval/pr-scaling-5ed1d7369b-bitexact/inputs.json \
  --root /mnt/disk0/gem5-CXL-eval/pr-offload-formal-latency-spectrum-r1 \
  --gem5 build/X86/gem5.opt \
  --m5-library util/m5/build/x86/out/libm5.a \
  --config configs/example/gem5_library/x86-gapbs-amu-se.py \
  --calibration /mnt/disk0/gem5-CXL-eval/pr-offload-calibration-cae49a9b50/amu-cira.json \
  --policy /mnt/disk0/gem5-CXL-eval/pr-offload-builds-4e9b85871d/policy.json \
  --cxlmemuring /home/victoryang00/CXLMemUring \
  --m2ndp-root /mnt/disk0/M2NDP-public \
  --variants-build-root /mnt/disk0/gem5-CXL-eval/pr-offload-builds-4e9b85871d/variants \
  --stop-after qualification
```

Expected: primary and replay each pass bit-exact, mechanism, deterministic
timing, and 1.4x--1.6x speedup gates.

- [ ] **Step 3: Handle an AMU or M2NDP performance hold scientifically**

If `diagnostic-performance-hold.json` exists, do not continue formal work.
Extract exact additive ROI phases and mechanism counters from the primary
summary:

```bash
jq . /mnt/disk0/gem5-CXL-eval/pr-offload-formal-latency-spectrum-r1/diagnostic-performance-hold.json
python3 - <<'PY'
import csv
from pathlib import Path
root = Path('/mnt/disk0/gem5-CXL-eval/pr-offload-formal-latency-spectrum-r1/qualification/primary')
for system in ('amu', 'cira-few-shot', 'm2ndp'):
    row = next(csv.DictReader((root / system / 'summary.csv').open(newline='')))
    print(system, {key: row.get(key) for key in (
        'sim_ticks', 'ndpsim_measured_cycles', 'pr_e2e_formation_ns',
        'pr_e2e_execution_ns', 'pr_e2e_drain_ns', 'pr_queue_stall_ticks',
        'pr_read_packets', 'pr_rejected_descriptors')})
PY
```

Apply `systematic-debugging` and create a new failing regression test for the
single measured bottleneck before changing code. A threshold edit,
post-processing scale factor, or uncharged phase is forbidden. After any fix,
repeat Tasks 1--3 as applicable and start r6 rather than resuming r5.

- [ ] **Step 4: Preserve passed qualification identity**

Record SHA-256 for `qualification.json`, gem5, libm5, calibration, policy,
M2NDP config, and source tree. The spectrum orchestrator must reject any
different identity.

### Task 9: Freeze the six real paper inputs and build the prepared suite

**Files:**
- Existing validator: `scripts/freeze_cross_system_inputs.py`
- Modify after real inputs exist: `scripts/build_matched_breadth_workloads.py`
- Test: `tests/pyunit/cross_system/test_freeze_cross_system_inputs.py`
- Test: `tests/pyunit/cross_system/test_matched_region_build.py`
- Output: `/mnt/disk0/gem5-CXL-eval/cira-amu-m2ndp-spectrum/shared/`

- [ ] **Step 1: Prove current input availability before building**

Validate the current candidate registry at
`/mnt/disk0/gem5-CXL-eval/cira-amu-m2ndp-spectrum/paper-input-candidates.json`.
The accepted record must satisfy
`freeze_cross_system_inputs.validate_bound_inputs`: g20 PR, non-synthetic
MCFREG2, 1 GiB AMG values/index, 1 GiB LULESH values/index, and clean NPB
CG/MG source trees whose parameter records bind their allocated-byte counts.

The 2026-08-27 registry already binds PR g20, the accepted MCFREG2 package
`4230e0db55829be687247021c2936e20eba475160e85bf58e4da6b0613572620`,
and NPB CG/MG class-E source/parameter paths. AMG and LULESH remain empty, and
the NPB records still need validated allocation capacity and clean-tree
identity. Resolve those four record gaps from authoritative workload files;
if any remain unavailable, write `failed-input.json` and stop without
generating values, indexes, parameters, or substitute data.

- [ ] **Step 2: Freeze the authoritative files**

After the exact record exists at
`/mnt/disk0/gem5-CXL-eval/cira-amu-m2ndp-spectrum/paper-input-record.json`, run:

```bash
python3 scripts/freeze_cross_system_inputs.py \
  --paper-input-record /mnt/disk0/gem5-CXL-eval/cira-amu-m2ndp-spectrum/paper-input-record.json \
  --graph-manifest /mnt/disk0/gem5-CXL-g14-eval/graphs/g4.manifest.json \
  --graph-manifest /mnt/disk0/gem5-CXL-g14-eval/graphs/g12.manifest.json \
  --graph-manifest /mnt/disk0/gem5-CXL-g14-eval/graphs/g14.manifest.json \
  --graph-manifest /mnt/disk0/gem5-CXL-g14-eval/graphs/g20.manifest.json \
  --output /mnt/disk0/gem5-CXL-eval/cira-amu-m2ndp-spectrum/shared/inputs.json
```

Expected: exit 0 and `status=accepted`; any missing/hash-drift/size mismatch
produces terminal `failed-input.json`.

- [ ] **Step 3: Validate the accepted MCFREG2 path and finish the remaining formal builders**

Keep the already qualified MCFREG2 package and its validation hash immutable.
Write formal builder tests that consume that package, authoritative AMG and
LULESH value/index files, and clean NPB class-E records. Assert a verified
six-workload manifest with source, input, binary, trace, capacity, and output
hashes. The implementation must call the existing MCFREG2 replay, Spatter
reference, and formal NPB builders; it must not reuse fixture inputs or
regenerate the accepted MCF package.

- [ ] **Step 4: Run input and builder tests**

```bash
python3 -m unittest \
  tests.pyunit.cross_system.test_freeze_cross_system_inputs \
  tests.pyunit.cross_system.test_matched_region_build -v
```

Expected: zero failures, including synthetic and undersized-input rejection.

- [ ] **Step 5: Build the shared prepared suite**

```bash
python3 scripts/build_matched_breadth_workloads.py \
  --formal \
  --inputs /mnt/disk0/gem5-CXL-eval/cira-amu-m2ndp-spectrum/shared/inputs.json \
  --outdir /mnt/disk0/gem5-CXL-eval/cira-amu-m2ndp-spectrum/shared/prepared
```

Expected: verified six-workload manifest, full reference boundary hashes, and
no synthetic inputs.

- [ ] **Step 6: Commit formal builder completion**

```bash
git add scripts/build_matched_breadth_workloads.py \
  tests/pyunit/cross_system/test_matched_region_build.py
git commit -m "feat: build frozen breadth workloads"
```

### Task 10: Run all four latency campaigns

**Files:**
- Output root: `/mnt/disk0/gem5-CXL-eval/cira-amu-m2ndp-spectrum/`

- [ ] **Step 1: Verify storage and shared-object hashes**

```bash
df -B1 /mnt/disk0
python3 -m unittest \
  tests.pyunit.cross_system.test_run_cira_amu_m2ndp_latency_spectrum -v
```

Expected: sufficient free space for latency-local logs/checkpoints and all
shared records match their hashes. Do not automatically delete older roots.

- [ ] **Step 2: Launch the resumable spectrum runner**

```bash
python3 scripts/run_cira_amu_m2ndp_latency_spectrum.py \
  --inputs /mnt/disk0/gem5-CXL-eval/cira-amu-m2ndp-spectrum/shared/inputs.json \
  --prepared /mnt/disk0/gem5-CXL-eval/cira-amu-m2ndp-spectrum/shared/prepared/manifest.json \
  --qualification /mnt/disk0/gem5-CXL-eval/pr-offload-formal-latency-spectrum-r1/qualification.json \
  --calibration /mnt/disk0/gem5-CXL-eval/pr-offload-calibration-cae49a9b50/amu-cira.json \
  --root /mnt/disk0/gem5-CXL-eval/cira-amu-m2ndp-spectrum
```

Expected: each latency moves independently through functional and paired
timing states; aggregate completion is absent until all four are complete.

- [ ] **Step 3: Resume only from valid semantic checkpoints after interruption**

```bash
python3 scripts/run_cira_amu_m2ndp_latency_spectrum.py \
  --inputs /mnt/disk0/gem5-CXL-eval/cira-amu-m2ndp-spectrum/shared/inputs.json \
  --prepared /mnt/disk0/gem5-CXL-eval/cira-amu-m2ndp-spectrum/shared/prepared/manifest.json \
  --qualification /mnt/disk0/gem5-CXL-eval/pr-offload-formal-latency-spectrum-r1/qualification.json \
  --calibration /mnt/disk0/gem5-CXL-eval/pr-offload-calibration-cae49a9b50/amu-cira.json \
  --root /mnt/disk0/gem5-CXL-eval/cira-amu-m2ndp-spectrum \
  --resume
```

Expected: completed windows are hash-validated and skipped; mismatched code,
input, calibration, or latency terminates rather than adopting stale output.

- [ ] **Step 4: Validate the 96 accepted coordinates**

```bash
jq -e '.status == "complete"' \
  /mnt/disk0/gem5-CXL-eval/cira-amu-m2ndp-spectrum/complete.json
```

Expected: true, exactly four complete child manifests, six workloads per
child, four systems per workload, and no inconclusive numeric rows.

### Task 11: Generate canonical data, figures, and LaTeX

**Files:**
- Create: `scripts/generate_cira_amu_m2ndp_latency_spectrum.py`
- Create: `tests/pyunit/cross_system/test_generate_cira_amu_m2ndp_latency_spectrum.py`
- Modify: `scripts/generate_cira_amu_m2ndp_comparison.py`

- [ ] **Step 1: Write failing 96-row and tamper-rejection tests**

Build four tiny complete fixtures and assert:

```python
data = publisher.load_complete(root / "complete.json")
self.assertEqual(len(data.rows), 96)
self.assertEqual(
    {(row.latency, row.workload, row.system) for row in data.rows},
    set(spectrum.coordinates()),
)
```

Add one-bit output-hash corruption, missing latency, duplicate coordinate,
wrong speedup, wrong CI, and mixed identity cases; each must raise
`PublicationError` before writing any PDF/CSV. Assert the accepted fixture
creates exactly these logical chart products: `workloads-1us`,
`latency-spectrum`, and the six named `standalone` workload products. Each
chart product must have PDF, SVG, and PNG records.

- [ ] **Step 2: Run the new publisher test and confirm module import failure**

```bash
python3 -m unittest \
  tests.pyunit.cross_system.test_generate_cira_amu_m2ndp_latency_spectrum -v
```

Expected: publisher module is missing.

- [ ] **Step 3: Implement independent recomputation and atomic outputs**

Define a frozen row with exact Decimal fields:

```python
@dataclasses.dataclass(frozen=True)
class SpectrumRow:
    latency: str
    workload: str
    system: str
    seconds: Decimal
    speedup: Decimal
    ci_low: Decimal | None
    ci_high: Decimal | None
    evidence_type: str
    evidence_sha256: str
```

Recompute speedup from the matched Vanilla seconds, validate stored values
within the existing exact-decimal tolerance, recompute workload and geometric
means, and atomically generate:

```text
raw/cira-amu-m2ndp-latency-spectrum.csv
raw/cira-amu-m2ndp-latency-spectrum.json
raw/cira-amu-m2ndp-latency-spectrum-manifest.json
fig/cira-amu-m2ndp-workloads-1us.pdf
fig/cira-amu-m2ndp-workloads-1us.svg
fig/cira-amu-m2ndp-workloads-1us.png
fig/cira-amu-m2ndp-latency-spectrum.pdf
fig/cira-amu-m2ndp-latency-spectrum.svg
fig/cira-amu-m2ndp-latency-spectrum.png
fig/standalone/pr_spmv-latency-spectrum.{pdf,svg,png}
fig/standalone/mcf-latency-spectrum.{pdf,svg,png}
fig/standalone/amg_gather-latency-spectrum.{pdf,svg,png}
fig/standalone/lulesh_scatter-latency-spectrum.{pdf,svg,png}
fig/standalone/npb_cg-latency-spectrum.{pdf,svg,png}
fig/standalone/npb_mg-latency-spectrum.{pdf,svg,png}
tex/cira-amu-m2ndp-latency-table-data.tex
```

The workload figure uses grouped AMU/CIRA/M2NDP bars at 1 us with paired 95%
confidence-interval whiskers.
The selected spectrum figure uses 2-by-3 small multiples, fixed system colors,
markers and linestyles, paired CI whiskers, and a visible 1.0x line. Each
standalone workload figure has two vertically aligned panels: absolute
end-to-end latency for Vanilla/AMU/CIRA/M2NDP above and normalized speedup for
AMU/CIRA/M2NDP below. Implement the renderer boundary as:

```python
def render_composite(rows, output_stem):
    """Render the six-panel normalized latency spectrum."""

def render_standalone(workload, rows, output_stem):
    """Render absolute latency and normalized speedup for one workload."""

def save_formats(figure, output_stem):
    for suffix in ("pdf", "svg", "png"):
        figure.savefig(output_stem.with_suffix(f".{suffix}"),
                       bbox_inches="tight", dpi=300)
```

Use ordered x positions for `200ns`, `500ns`, `1us`, and `2us`; label them as
`200 ns`, `500 ns`, `1 us`, and `2 us`. Absolute-latency axes start at zero.
Normalized axes include every accepted value and never truncate regressions
below 1.0x. Do not connect through missing or inconclusive coordinates.

- [ ] **Step 4: Run publisher and existing comparison tests**

```bash
python3 -m unittest \
  tests.pyunit.cross_system.test_generate_cira_amu_m2ndp_latency_spectrum \
  tests.pyunit.cross_system.test_generate_cira_amu_m2ndp_comparison -v
```

Expected: zero failures and byte-stable output hashes across two runs.

- [ ] **Step 5: Generate publication artifacts from formal evidence**

```bash
python3 scripts/generate_cira_amu_m2ndp_latency_spectrum.py \
  --complete /mnt/disk0/gem5-CXL-eval/cira-amu-m2ndp-spectrum/complete.json \
  --outdir /mnt/disk0/gem5-CXL-eval/cira-amu-m2ndp-spectrum/publication
```

Expected: publication manifest is complete and binds every generated hash.

- [ ] **Step 6: Inspect every exported chart before publication**

```bash
mkdir -p /tmp/cxl-spectrum-preview
for pdf in \
  /mnt/disk0/gem5-CXL-eval/cira-amu-m2ndp-spectrum/publication/fig/*.pdf \
  /mnt/disk0/gem5-CXL-eval/cira-amu-m2ndp-spectrum/publication/fig/standalone/*.pdf; do
  pdftoppm -png -singlefile -r 150 "$pdf" \
    "/tmp/cxl-spectrum-preview/$(basename "${pdf%.pdf}")"
done
```

Inspect all eight PNG previews at original detail. Reject clipped labels,
inconsistent system colors, unreadable confidence intervals, non-zero absolute
latency baselines, missing 1.0x references, or a legend that depends on color
alone.

- [ ] **Step 7: Commit the publisher**

```bash
git add scripts/generate_cira_amu_m2ndp_latency_spectrum.py \
  scripts/generate_cira_amu_m2ndp_comparison.py \
  tests/pyunit/cross_system/test_generate_cira_amu_m2ndp_latency_spectrum.py
git commit -m "feat: publish CXL latency spectrum evidence"
```

### Task 12: Update and verify the paper

**Files:**
- Modify: `/home/victoryang00/gem5-CXL/6472666535e6f359942ddac6/sections/evaluation.tex`
- Modify: `/home/victoryang00/gem5-CXL/6472666535e6f359942ddac6/gapbs-vtune-cxl-table.tex`
- Create: `/home/victoryang00/gem5-CXL/6472666535e6f359942ddac6/data/cira-amu-m2ndp-latency-spectrum.csv`
- Create: `/home/victoryang00/gem5-CXL/6472666535e6f359942ddac6/data/cira-amu-m2ndp-latency-spectrum.json`
- Create: `/home/victoryang00/gem5-CXL/6472666535e6f359942ddac6/fig/cira-amu-m2ndp-workloads-1us.pdf`
- Create: `/home/victoryang00/gem5-CXL/6472666535e6f359942ddac6/fig/cira-amu-m2ndp-latency-spectrum.pdf`
- Create: `/home/victoryang00/gem5-CXL/6472666535e6f359942ddac6/fig/latency-spectrum/` with six standalone PDF/SVG/PNG figure sets

- [ ] **Step 1: Verify the nested paper repository before copying**

```bash
git -C /home/victoryang00/gem5-CXL/6472666535e6f359942ddac6 status --short --branch
```

Expected: tracked files are clean. Preserve the existing untracked
`WIP_jesun_eurosys.pdf`, `WIP_jf_asplos.pdf`, and GAPBS evidence files.

- [ ] **Step 2: Copy only hash-validated publication files**

Use the publication manifest to verify each source hash before copying the
two composite PDF figures, their SVG/PNG exports, all six standalone
PDF/SVG/PNG figure sets, canonical CSV/JSON, and generated table data into the
paper repository. Abort if any hash differs.

- [ ] **Step 3: Replace the g4-only evaluation text and figure**

In `sections/evaluation.tex`, replace the existing
`fig:gem5_amu_m2ndp_e2e` block with two figures labeled
`fig:cross_system_workloads_1us` and `fig:cross_system_latency_spectrum`.
State that PR uses the frozen g20 graph with 2^20 vertices, that the six rows
are matched regions rather than entire-suite coverage, and that timing uses
full functional bit-exact replay plus paired stratified timing where labeled.

Every numeric sentence must be generated from or manually checked against the
canonical CSV. If any workload is inconclusive, name it and omit a numeric
claim; do not describe the matrix as complete.

- [ ] **Step 4: Replace the table wrapper**

Make `gapbs-vtune-cxl-table.tex` input the new generated table data and update
its caption to identify six workloads, four latency points, all-CXL placement,
four threads, bit-exact validation, and the evidence type/CI columns.

- [ ] **Step 5: Build and inspect the paper**

```bash
cd /home/victoryang00/gem5-CXL/6472666535e6f359942ddac6
latexmk -pdf -interaction=nonstopmode -halt-on-error main.tex
pdfinfo main.pdf | rg '^Pages:'
rg -n 'undefined references|LaTeX Error|Citation.*undefined' main.log
```

Expected: `latexmk` exits 0, figures resolve, and the final `rg` produces no
matches. Inspect the rendered figure pages for clipped labels, unreadable
fonts, and bars/CI outside axes.

- [ ] **Step 6: Commit the paper repository**

```bash
git -C /home/victoryang00/gem5-CXL/6472666535e6f359942ddac6 add \
  sections/evaluation.tex gapbs-vtune-cxl-table.tex \
  data/cira-amu-m2ndp-latency-spectrum.csv \
  data/cira-amu-m2ndp-latency-spectrum.json \
  fig/cira-amu-m2ndp-workloads-1us.pdf \
  fig/cira-amu-m2ndp-workloads-1us.svg \
  fig/cira-amu-m2ndp-workloads-1us.png \
  fig/cira-amu-m2ndp-latency-spectrum.pdf \
  fig/cira-amu-m2ndp-latency-spectrum.svg \
  fig/cira-amu-m2ndp-latency-spectrum.png \
  fig/latency-spectrum
git -C /home/victoryang00/gem5-CXL/6472666535e6f359942ddac6 commit \
  -m "eval: add cross-system CXL latency spectrum"
```

### Task 13: Final verification and push

**Files:**
- Verify all committed code, evidence manifests, and paper files

- [ ] **Step 1: Run the complete relevant unit-test suites**

```bash
python3 -m unittest discover -s tests/pyunit/m2ndp -p 'test_*.py' -v
python3 -m unittest discover -s tests/pyunit/cross_system -p 'test_*.py' -v
git diff --check
```

Expected: zero failures and no whitespace errors.

- [ ] **Step 2: Revalidate formal artifacts from scratch**

Run the aggregate validator against `complete.json` and regenerate publication
artifacts into a fresh temporary directory. Compare its manifest hashes to the
published directory. Expected: exact match for CSV, JSON, LaTeX, PDF, and SVG.

- [ ] **Step 3: Verify repository boundaries**

```bash
git status --short --branch
git diff -- src/mem/cache/base.cc
git -C /home/victoryang00/gem5-CXL/6472666535e6f359942ddac6 status --short --branch
```

Expected: the user's `src/mem/cache/base.cc` remains unstaged and unchanged by
this work; paper untracked pre-existing files remain untracked.

- [ ] **Step 4: Push both corresponding branches**

```bash
git push origin m2ndp-g20-pr-spmv
git -C /home/victoryang00/gem5-CXL/6472666535e6f359942ddac6 push origin master
```

Expected: both remotes accept the commits and remote heads equal local heads.

- [ ] **Step 5: Report exact completion evidence**

Report the two pushed commit IDs, aggregate manifest path/hash, raw CSV/JSON
paths, figure/table paths, test counts, paper page count, and the 96-point
acceptance count. If input, qualification, confidence, or paper gates did not
pass, report the exact terminal artifact instead of claiming completion.
