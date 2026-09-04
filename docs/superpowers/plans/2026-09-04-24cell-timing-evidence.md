# 24-Cell Timing Evidence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Produce a hash-bound 24-cell bundle containing M²NDP kernel time, link calibration, host-inline region time, and CIRA device busy time for six workloads at four CXL latencies.

**Architecture:** Extend the generic CIRA prefetch model with ROI-safe first-issue/last-completion span statistics, then teach the matched replay to run the same compiled code as `cira-inline` with no offload. A focused publisher normalizes existing or freshly generated M²NDP, calibration, host-inline, and CIRA evidence into one deterministic 24-row CSV and manifest; a resumable campaign driver runs only missing hash-valid cells.

**Tech Stack:** gem5 C++, C++17/OpenMP matched replay, Python 3 standard library, `unittest`, NDPSim/FuncSim evidence JSON, CSV, SHA-256.

**Spec:** `docs/plans/2026-09-04-24cell-timing-evidence-design.md`

## Global Constraints

- The matrix is exactly six workloads (`pr_spmv`, `gap_bc`, `mcf`, `amg_gather`, `lulesh_scatter`, `npb_cg`) by four latencies (`200ns`, `500ns`, `1us`, `2us`).
- The existing plot uses generic `cira_prefetch()`; PageRank descriptor counters are exported but are not substituted for generic device busy time.
- Primary times remain integer gem5 ticks or integer NDP cycles; derived nanoseconds use exact decimal arithmetic.
- Every accepted cell must bind input, binary, configuration, raw output, and calibration hashes.
- A reused simulator output must be labeled reused and pass all recorded hash checks; otherwise rerun it.
- Host-inline uses the CIRA-capable replay binary, four workers, the same timing window, all-CXL memory, and zero CIRA submissions.
- Host-inline and CIRA region metrics cover the dynamic offloadable region only; fixed control/setup time is linked but excluded.
- Do not modify or commit the unrelated dirty files in `.worktrees/m2ndp-g20-pr-spmv`.
- The full-suite baseline has eight known environment/input errors; targeted tests must pass with zero failures.

---

### Task 1: Add ROI-safe generic CIRA busy-span statistics

**Files:**
- Modify: `src/mem/cira.hh:34-60,330-410`
- Modify: `src/mem/cira.cc:60-245,383-390,1680-1720,2390-2420`
- Modify: `tests/gem5/cira/run_cira_multicore.py`
- Test: `tests/pyunit/amu/test_cira_device_span.py`

**Interfaces:**
- Consumes: accepted generic prefetches in `CIRA::issuePrefetch()` and their terminal completion in `CIRA::completeRequest()`.
- Produces: gem5 statistics `genericPrefetchFirstIssueTick`, `genericPrefetchLastCompletionTick`, `genericPrefetchBusyTicks`, `genericPrefetchSpanValid`, `genericPrefetchResetOutstanding`, and four-element `*PerCore` vectors.

- [ ] **Step 1: Write a live multicore behavior test for the new statistics**

```python
class CiraDeviceSpanTest(unittest.TestCase):
    def test_live_multicore_prefetch_reports_consistent_busy_spans(self):
        gem5 = os.environ.get("CIRA_TEST_GEM5")
        if not gem5:
            self.skipTest("CIRA_TEST_GEM5 is not set")
        stats = self.run_multicore_workload(Path(gem5))
        first = int(stats["board.cira.genericPrefetchFirstIssueTick"])
        last = int(stats["board.cira.genericPrefetchLastCompletionTick"])
        busy = int(stats["board.cira.genericPrefetchBusyTicks"])
        self.assertGreater(first, 0)
        self.assertGreaterEqual(last, first)
        self.assertEqual(busy, last - first)
        self.assertEqual(int(stats["board.cira.genericPrefetchSpanValid"]), 1)
        self.assertEqual(
            int(stats["board.cira.genericPrefetchResetOutstanding"]), 0
        )
```

`run_multicore_workload()` compiles the real
`tests/gem5/cira/cira_multicore_prefetch.cc` against the checked-in `libm5.a`,
runs the real `tests/gem5/cira/run_cira_multicore.py`, parses `stats.txt`, and
also checks every active core has `busy == last - first` and issued equals
completed. It contains no CIRA model mock.

- [ ] **Step 2: Run the new test and confirm the missing-symbol failure**

Run:

```bash
CIRA_TEST_GEM5=/home/victoryang00/gem5-CXL/.worktrees/m2ndp-g20-pr-spmv/build/X86/gem5.opt \
CIRA_TEST_M5_LIBRARY=/home/victoryang00/gem5-CXL/.worktrees/m2ndp-g20-pr-spmv/util/m5/build/x86/out/libm5.a \
python3 -m unittest tests.pyunit.amu.test_cira_device_span -v
```

Expected: FAIL with a missing `board.cira.genericPrefetchFirstIssueTick`
statistic from the pre-change gem5 binary.

- [ ] **Step 3: Declare span state, helpers, and statistics**

Add private state and helper signatures in `cira.hh`:

```cpp
void resetGenericPrefetchSpan();
void noteGenericPrefetchIssue(PortID core);
void noteGenericPrefetchCompletion(PortID core);

bool genericPrefetchSpanStarted = false;
Tick genericPrefetchFirstIssue = 0;
std::vector<bool> genericPrefetchSpanPerCoreStarted;
std::vector<Tick> genericPrefetchFirstIssuePerCore;
```

Add scalar/vector members to `CIRAStats`, initialize four-core vectors using
`num_cores`, and assign `core0` through `core3` subnames exactly as the existing
issued/completed vectors do.

- [ ] **Step 4: Implement reset and issue/completion accounting**

Implement the invariant-preserving helpers:

```cpp
void CIRA::resetGenericPrefetchSpan()
{
    stats.genericPrefetchResetOutstanding = outstanding.size();
    genericPrefetchSpanStarted = false;
    genericPrefetchFirstIssue = 0;
    std::fill(genericPrefetchSpanPerCoreStarted.begin(),
              genericPrefetchSpanPerCoreStarted.end(), false);
    std::fill(genericPrefetchFirstIssuePerCore.begin(),
              genericPrefetchFirstIssuePerCore.end(), 0);
}
```

On the first accepted issue, record the absolute tick globally and per core.
On every completion, update last-completion and busy-span statistics as
`curTick() - first_issue`. Set validity to one only after a completion exists.
Call `resetGenericPrefetchSpan()` from `resetStats()` after the base reset.

- [ ] **Step 5: Exercise the stats in the multicore gem5 smoke harness**

Extend `run_cira_multicore.py` to require:

```python
assert stat("board.cira.genericPrefetchSpanValid") == 1
assert stat("board.cira.genericPrefetchResetOutstanding") == 0
assert stat("board.cira.genericPrefetchBusyTicks") == (
    stat("board.cira.genericPrefetchLastCompletionTick")
    - stat("board.cira.genericPrefetchFirstIssueTick")
)
```

For each active core, impose the equivalent per-core equality and require
issued equals completed.

- [ ] **Step 6: Build the changed model and run focused tests**

Run:

```bash
scons build/X86/gem5.opt -j4
CIRA_TEST_GEM5=build/X86/gem5.opt \
CIRA_TEST_M5_LIBRARY=util/m5/build/x86/out/libm5.a \
python3 -m unittest tests.pyunit.amu.test_cira_device_span -v
python3 -m py_compile tests/gem5/cira/run_cira_multicore.py
```

Expected: all tests PASS.

- [ ] **Step 7: Commit the model change**

```bash
git add src/mem/cira.hh src/mem/cira.cc \
  tests/gem5/cira/run_cira_multicore.py \
  tests/pyunit/amu/test_cira_device_span.py
git commit -m "feat: measure generic CIRA device busy spans"
```

---

### Task 2: Export generic and PageRank per-core CIRA metrics

**Files:**
- Modify: `scripts/run_matched_breadth_gem5.py:43,1335-1405,1660-1810`
- Modify: `tests/pyunit/cross_system/test_matched_breadth_gem5.py`

**Interfaces:**
- Consumes: Task 1 gem5 stat names and existing `prComputeTicks*`, `prQueueStallTicks*`, `issuedPrDescriptors*`, and `completedPrDescriptors*` statistics.
- Produces: `parse_cira_device_metrics(stats: Mapping[str, float]) -> dict` and optional `require_device_timing: bool` validation in `collect_run_evidence()`.

- [ ] **Step 1: Add failing parser tests with a complete four-core stats fixture**

```python
def test_cira_device_metrics_export_global_per_core_and_pr_fields(self):
    stats = self.cira_stats_fixture()
    stats.update({
        "board.cira.genericPrefetchFirstIssueTick": 100,
        "board.cira.genericPrefetchLastCompletionTick": 190,
        "board.cira.genericPrefetchBusyTicks": 90,
        "board.cira.genericPrefetchSpanValid": 1,
        "board.cira.genericPrefetchResetOutstanding": 0,
        "board.cira.prComputeTicks": 0,
        "board.cira.prQueueStallTicks": 0,
        "board.cira.issuedPrDescriptors": 0,
        "board.cira.completedPrDescriptors": 0,
    })
    for core in range(4):
        stats[f"board.cira.genericPrefetchBusyTicksPerCore::{core}"] = 20 + core
        stats[f"board.cira.prComputeTicksPerCore::{core}"] = 0
        stats[f"board.cira.prQueueStallTicksPerCore::{core}"] = 0
        stats[f"board.cira.issuedPrDescriptorsPerCore::{core}"] = 0
        stats[f"board.cira.completedPrDescriptorsPerCore::{core}"] = 0
    observed = replay.parse_cira_device_metrics(stats)
    self.assertEqual(observed["generic_prefetch"]["busy_ticks"], 90)
    self.assertEqual(
        observed["generic_prefetch"]["busy_ticks_per_core"], [20, 21, 22, 23]
    )
    self.assertIs(observed["pr_descriptor_metrics"]["applicable"], False)
```

- [ ] **Step 2: Run the parser test and confirm it fails**

Run: `python3 -m unittest tests.pyunit.cross_system.test_matched_breadth_gem5.MatchedBreadthGem5Test.test_cira_device_metrics_export_global_per_core_and_pr_fields -v`

Expected: FAIL with `AttributeError: parse_cira_device_metrics`.

- [ ] **Step 3: Implement the parser and invariants**

Implement:

```python
def parse_cira_device_metrics(stats):
    vector = lambda base: [
        _stat_integer(stats, f"board.cira.{base}::{core}")
        for core in range(4)
    ]
    first = _stat_integer(stats, "board.cira.genericPrefetchFirstIssueTick")
    last = _stat_integer(stats, "board.cira.genericPrefetchLastCompletionTick")
    busy = _stat_integer(stats, "board.cira.genericPrefetchBusyTicks")
    if last < first or busy != last - first:
        raise ReplayError("CIRA generic prefetch busy span differs")
    if _stat_integer(stats, "board.cira.genericPrefetchSpanValid") != 1:
        raise ReplayError("CIRA generic prefetch busy span is invalid")
    if _stat_integer(stats, "board.cira.genericPrefetchResetOutstanding"):
        raise ReplayError("CIRA generic prefetch span reset with live requests")
    issued_pr = _stat_integer(stats, "board.cira.issuedPrDescriptors")
    completed_pr = _stat_integer(stats, "board.cira.completedPrDescriptors")
    return {
        "generic_prefetch": {
            "first_issue_tick": first,
            "last_completion_tick": last,
            "busy_ticks": busy,
            "busy_ticks_per_core": vector("genericPrefetchBusyTicksPerCore"),
        },
        "pr_descriptor_metrics": {
            "applicable": issued_pr != 0 or completed_pr != 0,
            "compute_ticks": _stat_integer(stats, "board.cira.prComputeTicks"),
            "queue_stall_ticks": _stat_integer(stats, "board.cira.prQueueStallTicks"),
            "compute_ticks_per_core": vector("prComputeTicksPerCore"),
            "queue_stall_ticks_per_core": vector("prQueueStallTicksPerCore"),
            "issued": issued_pr,
            "completed": completed_pr,
        },
    }
```

Also export per-core generic first/last/valid values. For matched replay,
validate descriptor issued/completed are both zero and all PR compute/stall
values are zero.

- [ ] **Step 4: Preserve compatibility with old evidence**

Add `require_device_timing=False` to `collect_run_evidence()`. Parse the new
metrics when all required stat names exist; reject missing fields only when the
flag is true. The new campaign passes true; legacy breadth callers remain
unchanged.

- [ ] **Step 5: Run matched replay tests**

Run: `python3 -m unittest tests.pyunit.cross_system.test_matched_breadth_gem5 -v`

Expected: all tests except the known missing generated `libm5.a` test PASS.

- [ ] **Step 6: Commit the export change**

```bash
git add scripts/run_matched_breadth_gem5.py \
  tests/pyunit/cross_system/test_matched_breadth_gem5.py
git commit -m "feat: export per-core CIRA runtime evidence"
```

---

### Task 3: Add the CIRA-compiled host-inline execution mode

**Files:**
- Modify: `util/amu/matched_workloads/trace_replay.cc:1180-1190,1670-1740,1845-2005`
- Modify: `scripts/run_matched_breadth_gem5.py:43,1180-1300,1335-1405,1520-1810`
- Modify: `tests/pyunit/cross_system/test_matched_breadth_gem5.py`

**Interfaces:**
- Consumes: canonical timing-window work groups and the existing gem5 work-begin/work-end ROI measurement.
- Produces: system `cira-inline`, result fields `offload_disabled`, `host_region_entry_count`, and evidence fields `host_region_cumulative_ticks` plus zero-offload proof.

- [ ] **Step 1: Add a failing native replay equivalence test**

```python
def test_cira_inline_uses_same_binary_and_executes_on_host(self):
    operations = _fixtures()["gather"]
    trace = self.root / "inline"
    canonical.write_bundle(
        trace, _meta("inline", 1), operations, {},
        initial_memory=_initial_memory(operations),
    )
    binary = replay.build_replay_binary(self.root / "build-inline", native=True)
    value = replay.run_native_replay(
        binary, system="cira-inline", trace=trace,
        outdir=self.root / "run-inline",
    )
    self.assertEqual(value["system"], "cira-inline")
    self.assertIs(value["offload_disabled"], True)
    self.assertEqual(value["host_region_entry_count"], 1)
```

- [ ] **Step 2: Run the test and confirm the unsupported-system failure**

Run: `python3 -m unittest tests.pyunit.cross_system.test_matched_breadth_gem5.MatchedBreadthGem5Test.test_cira_inline_uses_same_binary_and_executes_on_host -v`

Expected: FAIL with `unsupported replay system: cira-inline`.

- [ ] **Step 3: Implement `cira-inline` in the replay binary**

Map `cira-inline` to `VanillaAccessor` in `makeAccessor()`. Count selected
work groups with a helper:

```cpp
size_t countWorkGroups(const std::vector<Phase> &phases)
{
    size_t result = 0;
    for (const auto &phase : phases)
        result += phase.groups.size();
    return result;
}
```

Pass this count to `writeResult()` and serialize:

```cpp
stream << ",\"offload_disabled\":"
       << (system == "cira-inline" ? "true" : "false")
       << ",\"host_region_entry_count\":" << measuredWorkGroups;
```

The dynamic timing window uses the selected measured groups, not warmup
groups. Functional mode reports all work groups.

- [ ] **Step 4: Teach the Python runner to launch inline without CIRA**

Add `cira-inline` to `SYSTEMS`; use `--no-asmc` and do not add `--cira` or
`--cira-to-l2`. In `collect_run_evidence()` require:

```python
if system == "cira-inline":
    if result.get("offload_disabled") is not True:
        raise ReplayError("CIRA-inline offload marker is missing")
    row["host_region_cumulative_ticks"] = sim_ticks
    row["host_region_entry_count"] = _integer(
        result, "host_region_entry_count"
    )
```

Reject zero entries. Keep `fixed_sim_ticks` separate; do not add it to
`host_region_cumulative_ticks`.

- [ ] **Step 5: Add command and evidence tests**

Assert `command_for(cira-inline)` contains the same binary path and four-core
all-CXL arguments, contains `--no-asmc`, and omits `--cira`. Add a stats fixture
test proving `host_region_cumulative_ticks == simTicks` and that no fixed ticks
are included.

- [ ] **Step 6: Run focused replay tests**

Run:

```bash
python3 -m unittest \
  tests.pyunit.cross_system.test_matched_breadth_gem5.MatchedBreadthGem5Test.test_cira_inline_uses_same_binary_and_executes_on_host \
  tests.pyunit.cross_system.test_matched_breadth_gem5.MatchedBreadthGem5Test.test_command_for_cira_inline_disables_offload -v
```

Expected: both tests PASS.

- [ ] **Step 7: Commit host-inline mode**

```bash
git add util/amu/matched_workloads/trace_replay.cc \
  scripts/run_matched_breadth_gem5.py \
  tests/pyunit/cross_system/test_matched_breadth_gem5.py
git commit -m "feat: time matched CIRA regions inline on the host"
```

---

### Task 4: Normalize calibration and M²NDP per-cell evidence

**Files:**
- Create: `scripts/timing_evidence_24cell.py`
- Create: `tests/pyunit/cross_system/test_timing_evidence_24cell.py`

**Interfaces:**
- Consumes: existing calibration JSON and `m2ndp_workload_trace.run_ndpsim_package()` evidence JSON.
- Produces: `load_calibration(path: Path) -> CalibrationRow`, `load_m2ndp_cell(path: Path, workload: str, latency: str) -> dict`, `cycles_to_ns(cycles: int, period_ns: str) -> str`, and immutable `WORKLOADS`, `LATENCIES`, `COORDINATES`.

- [ ] **Step 1: Write failing exact-conversion and calibration tests**

```python
def test_cycles_to_ns_is_exact(self):
    self.assertEqual(evidence.cycles_to_ns(102_531_389, "0.5"), "51265694.5")

def test_calibration_rows_match_microprobe_records(self):
    expected = {
        "200ns": (1397, "4"),
        "500ns": (3798, "55"),
        "1us": (7799, "27"),
        "2us": (15801, "25.000002"),
    }
    for label, (cycles, residual_ps) in expected.items():
        row = evidence.load_calibration(self.calibrations[label])
        self.assertEqual(row.selected_link_latency, cycles)
        self.assertEqual(row.residual_ps, residual_ps)
```

- [ ] **Step 2: Run tests and confirm the module import fails**

Run: `python3 -m unittest tests.pyunit.cross_system.test_timing_evidence_24cell -v`

Expected: FAIL with `ImportError` for `timing_evidence_24cell`.

- [ ] **Step 3: Implement exact types and conversions**

Use `decimal.Decimal` and a frozen dataclass:

```python
@dataclasses.dataclass(frozen=True)
class CalibrationRow:
    latency: str
    gem5_round_trip_ns: str
    selected_link_latency: int
    core_period_ns: str
    link_period_ns: str
    m2ndp_round_trip_ns: str
    residual_ns: str
    residual_ps: str
    evidence_path: str
    evidence_sha256: str

def cycles_to_ns(cycles, period_ns):
    if isinstance(cycles, bool) or not isinstance(cycles, int) or cycles < 0:
        raise EvidenceError("cycles must be a nonnegative integer")
    value = decimal.Decimal(cycles) * decimal.Decimal(period_ns)
    return format(value, "f")
```

- [ ] **Step 4: Validate M²NDP evidence in the NPB CG 1 us shape**

Require schema/status, exactly one positive integer `cycles`, a passing
calibration for the requested latency, exact `core_period_ns`, simulator/config
hash records, functional evidence, output/log hashes, and command. Recompute
every referenced file hash. Return a normalized record containing cycles,
period, exact kernel ns, provenance path/hash, and `execution_origin` equal to
`fresh` or `verified_reuse`.

- [ ] **Step 5: Add fail-closed malformed-evidence tests**

Test wrong latency, changed output hash, two completion markers in the log,
missing functional evidence, binary float period, and a path whose content no
longer matches the recorded hash. Each must raise `EvidenceError` with a
specific message.

- [ ] **Step 6: Run the new module tests**

Run: `python3 -m unittest tests.pyunit.cross_system.test_timing_evidence_24cell -v`

Expected: all tests PASS.

- [ ] **Step 7: Commit the normalizer**

```bash
git add scripts/timing_evidence_24cell.py \
  tests/pyunit/cross_system/test_timing_evidence_24cell.py
git commit -m "feat: normalize 24-cell timing evidence"
```

---

### Task 5: Build a resumable 24-cell measurement driver

**Files:**
- Create: `scripts/run_24cell_timing_evidence.py`
- Create: `tests/pyunit/cross_system/test_run_24cell_timing_evidence.py`
- Modify: `scripts/run_matched_breadth_gem5.py`

**Interfaces:**
- Consumes: the frozen breadth `inputs.json` and `prepared/manifest.json`, Task 3 replay runner, Task 4 calibration/M²NDP validators, the instrumented gem5 binary, FuncSim/NDPSim binaries, and a clean repository commit.
- Produces: per-cell `host-inline-evidence.json`, `cira-runtime-evidence.json`, normalized `m2ndp-evidence.json`, `state.json`, and `complete.json`.

- [ ] **Step 1: Write state-machine tests for 24 coordinates and resume**

```python
def test_new_state_has_exact_24_cell_matrix(self):
    state = runner.new_state(self.identity)
    self.assertEqual(set(state["cells"]), {
        f"{workload}:{latency}"
        for workload in evidence.WORKLOADS
        for latency in evidence.LATENCIES
    })
    self.assertTrue(all(row["status"] == "pending" for row in state["cells"].values()))

def test_resume_rejects_changed_binary_hash(self):
    state = runner.new_state(self.identity)
    with self.assertRaisesRegex(runner.CampaignError, "identity differs"):
        runner.resume_state(state, dataclasses.replace(self.identity, gem5_sha256=_digest("changed")))
```

- [ ] **Step 2: Run tests and confirm the driver is missing**

Run: `python3 -m unittest tests.pyunit.cross_system.test_run_24cell_timing_evidence -v`

Expected: FAIL with `ImportError`.

- [ ] **Step 3: Implement immutable identity and atomic state**

Define a frozen `CampaignIdentity` containing repository commit, code digest,
input-manifest digest, replay-binary digest, gem5 digest, M²NDP simulator and
config digests, and the four calibration digests. Serialize with sorted compact
JSON and write through a temporary file plus `os.replace()`.

Expose these exact command-line arguments:

```python
parser.add_argument("--inputs", type=Path, required=True)
parser.add_argument("--prepared", type=Path, required=True)
parser.add_argument("--calibration", action="append", type=Path, required=True)
parser.add_argument("--gem5", type=Path, required=True)
parser.add_argument("--m5-library", type=Path, required=True)
parser.add_argument("--funcsim", type=Path, required=True)
parser.add_argument("--ndpsim", type=Path, required=True)
parser.add_argument("--root", type=Path, required=True)
parser.add_argument("--coordinate", choices=[
    f"{workload}:{latency}"
    for workload in evidence.WORKLOADS
    for latency in evidence.LATENCIES
])
parser.add_argument("--resume", action="store_true")
parser.add_argument("--validate-only", action="store_true")
```

Require exactly four `--calibration` files and index them by their embedded CXL
latency label. Resolve trace roots, timing-window manifests, fixed traces, and
M²NDP translation inputs from the hash-validated prepared manifest using the
same resolver functions as `run_cira_amu_m2ndp_breadth.py`; do not discover
cells by filename globbing.

- [ ] **Step 4: Implement cell execution and validation**

For each coordinate:

1. validate the trace/window/package paths and hashes from the matrix manifest;
2. run `cira-inline` through `run_matched_breadth_gem5.run()`;
3. run `cira` with `require_device_timing=True`;
4. validate or invoke the existing FuncSim/NDPSim package runner;
5. copy only compact evidence JSON into the campaign cell directory while
   retaining hash-bound absolute paths to large raw outputs; and
6. atomically change the cell from `running` to `complete`.

Record a failed cell as `failed` with command, log path, and exact exception.
Resume skips only a `complete` cell whose three evidence files and all
transitive hashes still verify.

- [ ] **Step 5: Add storage and clean-tree preflight**

Require at least 20 GiB free on both the evidence and build filesystems before
new simulation. Reject a dirty campaign checkout. Allow `--validate-only` to
package already complete evidence without launching simulators, and
`--coordinate workload:latency` for qualification.

- [ ] **Step 6: Add mocked launch tests**

Mock the replay and M²NDP runners to assert each is called exactly once per
coordinate, `cira-inline` receives no CIRA flag, CIRA requires device metrics,
calibration labels match, successful resume makes no calls, and a stale hash
forces failure rather than reuse.

- [ ] **Step 7: Run driver tests**

Run: `python3 -m unittest tests.pyunit.cross_system.test_run_24cell_timing_evidence -v`

Expected: all tests PASS.

- [ ] **Step 8: Commit the campaign driver**

```bash
git add scripts/run_24cell_timing_evidence.py \
  scripts/run_matched_breadth_gem5.py \
  tests/pyunit/cross_system/test_run_24cell_timing_evidence.py
git commit -m "feat: run resumable 24-cell timing campaign"
```

---

### Task 6: Publish deterministic CSV, manifest, and shareable tables

**Files:**
- Modify: `scripts/timing_evidence_24cell.py`
- Create: `scripts/publish_24cell_timing_evidence.py`
- Modify: `tests/pyunit/cross_system/test_timing_evidence_24cell.py`
- Create: `tests/pyunit/cross_system/test_publish_24cell_timing_evidence.py`

**Interfaces:**
- Consumes: Task 5 complete campaign root.
- Produces: `timing-24cells.csv`, `calibration.csv`, `README.md`, copied per-cell evidence, and `manifest.json` with hashes.

- [ ] **Step 1: Write a failing deterministic-publication test**

Construct 24 minimal valid cells and assert two publications have byte-identical
CSV, README, and manifest payloads. Assert the main CSV has exactly 24 rows in
workload-major/latency-minor order and columns for all four PR compute/stall
values alongside the aggregates.

- [ ] **Step 2: Run the publisher test and confirm it fails**

Run: `python3 -m unittest tests.pyunit.cross_system.test_publish_24cell_timing_evidence -v`

Expected: FAIL with `ImportError`.

- [ ] **Step 3: Implement the fixed CSV schema**

Use `csv.DictWriter` with an explicit field tuple containing:

```python
FIELDS = (
    "workload", "latency",
    "m2ndp_cycles", "m2ndp_core_period_ns", "m2ndp_kernel_time_ns",
    "selected_link_latency", "calibration_residual_ps",
    "host_region_cumulative_ticks", "host_region_cumulative_ns",
    "host_region_entry_count",
    "cira_device_first_issue_tick", "cira_device_last_completion_tick",
    "cira_device_busy_ticks", "cira_device_busy_ns",
    "cira_busy_ticks_core0", "cira_busy_ticks_core1",
    "cira_busy_ticks_core2", "cira_busy_ticks_core3",
    "cira_issued_core0", "cira_issued_core1",
    "cira_issued_core2", "cira_issued_core3",
    "cira_completed_core0", "cira_completed_core1",
    "cira_completed_core2", "cira_completed_core3",
    "pr_descriptor_applicable", "pr_compute_ticks", "pr_queue_stall_ticks",
    "pr_compute_ticks_core0", "pr_compute_ticks_core1",
    "pr_compute_ticks_core2", "pr_compute_ticks_core3",
    "pr_queue_stall_ticks_core0", "pr_queue_stall_ticks_core1",
    "pr_queue_stall_ticks_core2", "pr_queue_stall_ticks_core3",
    "m2ndp_evidence_path", "m2ndp_evidence_sha256",
    "host_inline_evidence_path", "host_inline_evidence_sha256",
    "cira_runtime_evidence_path", "cira_runtime_evidence_sha256",
)
```

Use exact gem5 tick-frequency conversion from `config.ini`; do not assume one
tick equals one picosecond without validating `simFreq`.

- [ ] **Step 4: Implement calibration CSV and manifest**

Write four rows ordered by `LATENCIES`. Hash every generated file, include all
per-cell evidence hashes, then write `manifest.json` last. Reject an incomplete
coordinate set, duplicate coordinate, nonpassing evidence, or any source hash
drift.

- [ ] **Step 5: Implement concise README tables**

Generate one calibration table and one 24-row timing table showing workload,
latency, M²NDP time, host-inline cumulative time/entries, and CIRA device busy
time. State that PR descriptor fields are not applicable to the generic
prefetch cells.

- [ ] **Step 6: Run publisher and normalizer tests**

Run:

```bash
python3 -m unittest \
  tests.pyunit.cross_system.test_timing_evidence_24cell \
  tests.pyunit.cross_system.test_publish_24cell_timing_evidence -v
```

Expected: all tests PASS.

- [ ] **Step 7: Commit the publisher**

```bash
git add scripts/timing_evidence_24cell.py \
  scripts/publish_24cell_timing_evidence.py \
  tests/pyunit/cross_system/test_timing_evidence_24cell.py \
  tests/pyunit/cross_system/test_publish_24cell_timing_evidence.py
git commit -m "feat: publish auditable 24-cell timing bundle"
```

---

### Task 7: Qualify, run, and verify the full campaign

**Files:**
- Create outside Git: `/mnt/disk0/gem5-CXL-eval/timing-evidence-24cell-20260904/`
- Create outside Git: `/mnt/disk0/gem5-CXL-eval/timing-evidence-24cell-20260904-published/`
- Modify if required by qualification: only files already named in Tasks 1-6, with a new failing regression test first.

**Interfaces:**
- Consumes: all prior task outputs and the live formal input/calibration/package records.
- Produces: a complete validated 24-cell raw evidence root and published bundle.

- [ ] **Step 1: Build generated prerequisites and gem5**

Build the checked-in m5 ABI library and the x86 optimized gem5 binary:

```bash
scons -C util/m5 build/x86/out/libm5.a -j4
scons build/X86/gem5.opt -j4
```

Record both commands and SHA-256 hashes in the campaign identity. Do not reuse
a binary whose hash is absent from the identity.

- [ ] **Step 2: Run all targeted unit tests**

Run:

```bash
python3 -m unittest \
  tests.pyunit.amu.test_cira_device_span \
  tests.pyunit.cross_system.test_matched_breadth_gem5 \
  tests.pyunit.cross_system.test_m2ndp_workload_trace \
  tests.pyunit.cross_system.test_timing_evidence_24cell \
  tests.pyunit.cross_system.test_run_24cell_timing_evidence \
  tests.pyunit.cross_system.test_publish_24cell_timing_evidence -v
```

Expected: zero failures and zero errors.

- [ ] **Step 3: Run one qualification coordinate**

Run `pr_spmv:200ns` into a fresh qualification root:

```bash
python3 scripts/run_24cell_timing_evidence.py \
  --inputs /mnt/disk0/gem5-CXL-eval/cira-amu-m2ndp-spectrum/shared/inputs.json \
  --prepared /mnt/disk0/gem5-CXL-eval/cira-amu-m2ndp-spectrum/shared/prepared/manifest.json \
  --calibration /mnt/disk0/gem5-CXL-eval/cira-amu-m2ndp-spectrum/formal-window-gate-r15/calibration/200ns/calibration.json \
  --calibration /mnt/disk0/gem5-CXL-eval/cira-amu-m2ndp-spectrum/formal-window-gate-r15/calibration/500ns/calibration.json \
  --calibration /mnt/disk0/gem5-CXL-eval/pr-offload-formal-a1e45e2d79-r13/qualification/primary/m2ndp/calibration/calibration.json \
  --calibration /mnt/disk0/gem5-CXL-eval/cira-amu-m2ndp-spectrum/formal-window-gate-r15/calibration/2us/calibration.json \
  --gem5 build/X86/gem5.opt \
  --m5-library util/m5/build/x86/out/libm5.a \
  --funcsim /mnt/disk0/gem5-CXL-eval/pr-offload-formal-a1e45e2d79-r13/qualification/primary/m2ndp/tools/bin/FuncSim \
  --ndpsim /mnt/disk0/gem5-CXL-eval/pr-offload-formal-a1e45e2d79-r13/qualification/primary/m2ndp/tools/bin/NDPSim \
  --root /mnt/disk0/gem5-CXL-eval/timing-evidence-24cell-20260904-qualification \
  --coordinate pr_spmv:200ns
```

Require host-inline
bit-exact output, positive entry count, no CIRA activity in inline mode,
positive and internally consistent CIRA generic busy span, all four active
cores issued/completed equally, matching calibration, and a passing M²NDP
evidence record.

- [ ] **Step 4: Inspect qualification evidence manually**

Use a short Python read-only check to print the three evidence paths and these
fields: M²NDP cycles/period/ns, calibration link cycles/residual, host ticks and
entries, CIRA first/last/busy/per-core spans, and PR applicability. Compare
first/last/busy arithmetic independently.

- [ ] **Step 5: Run or resume all 24 cells**

Run the driver for the full matrix with the same arguments as Step 3, omit
`--coordinate`, change `--root` to
`/mnt/disk0/gem5-CXL-eval/timing-evidence-24cell-20260904`, and add `--resume`
when restarting. Keep large simulator outputs in place and publish only
hash-bound compact evidence. Do not mark the campaign complete until all 24
coordinates validate.

- [ ] **Step 6: Publish the bundle**

Run:

```bash
python3 scripts/publish_24cell_timing_evidence.py \
  --campaign /mnt/disk0/gem5-CXL-eval/timing-evidence-24cell-20260904 \
  --output /mnt/disk0/gem5-CXL-eval/timing-evidence-24cell-20260904-published
```

Verify CSV row counts are 24 and 4, respectively.

- [ ] **Step 7: Perform independent hash and schema verification**

Run the independent validator entry point:

```bash
python3 scripts/publish_24cell_timing_evidence.py \
  --validate /mnt/disk0/gem5-CXL-eval/timing-evidence-24cell-20260904-published
```

It loads `manifest.json`, hashes every listed file, verifies every evidence
`status == "pass"`, checks exact coordinate coverage, and recomputes M²NDP and
gem5 time conversions with `Decimal`.

- [ ] **Step 8: Run final repository verification**

Run:

```bash
git diff --check
git status --short
git log --oneline --decorate -8
```

Expected: no uncommitted repository changes and the design plus implementation
commits visible on `evidence-24cell-timing-contract`.

- [ ] **Step 9: Commit any final documentation-only correction**

If the verified live command paths differ from the plan, update only this plan
and the design document with the executed commands and evidence-root path, then
commit:

```bash
git add docs/plans/2026-09-04-24cell-timing-evidence-design.md \
  docs/superpowers/plans/2026-09-04-24cell-timing-evidence.md
git commit -m "docs: record verified 24-cell timing run"
```
