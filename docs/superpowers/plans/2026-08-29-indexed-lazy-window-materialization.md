# Indexed Lazy Window Materialization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a bounded action path that selects formal MCF windows in one authenticated gzip scan and materializes formal NPB CG/MG windows through prefix indexes plus native sparse state.

**Architecture:** Keep immutable artifact validation, MCF selection, NPB indexing/slicing, native sparse capture, and action dispatch in separate modules. Reuse the existing reference, gem5, FuncSim, and NDPSim runners; no new component may infer timing or change workload arithmetic.

**Tech Stack:** Python 3 dataclasses/JSON/struct/gzip/hashlib, C++17 capture hooks, canonical/lazy trace schemas, unittest, gem5 SE, M2NDP FuncSim/NDPSim.

---

## File structure

- Create `scripts/indexed_window_contract.py` for immutable artifact schemas,
  hashes, atomic publication, and the 512 MiB limit.
- Create `scripts/mcf_selected_windows.py` for the one-pass MCF selector.
- Modify `scripts/mcfreg2.py` to expose an authenticated streaming EVENTS API.
- Create `scripts/npb_indexed_windows.py` for NPB prefix indexes, realized
  plans, sparse capture formats, and window materialization.
- Modify `scripts/npb_lazy_trace.py` for exact cardinalities, safe cuts,
  dependency closure, and sliced expansion.
- Modify NPB trace hooks and patches for native raw array/scalar capture.
- Create `scripts/run_prepared_breadth_action.py` as the manifest action driver.
- Modify preparation, replay, and breadth scripts only at their existing
  extension points.

### Task 1: Immutable artifact contracts

**Files:**
- Create: `scripts/indexed_window_contract.py`
- Create: `tests/pyunit/cross_system/test_indexed_window_contract.py`

- [ ] **Step 1: Write failing index and budget tests**

```python
def test_index_requires_exact_prefix_coverage(self):
    segments = (
        contract.IndexSegment(0, 10, 0, 101, 1, "npb_cg_spmv", 2),
        contract.IndexSegment(10, 18, 1, 103, 1, "npb_cg_dot", 1),
    )
    index = make_index(primitive_records=18, segments=segments)
    contract.write_lazy_index(self.root / "index.json", index)
    self.assertEqual(contract.read_lazy_index(self.root / "index.json"), index)
    with self.assertRaisesRegex(contract.IndexedWindowError, "coverage"):
        contract.validate_lazy_index(
            dataclasses.replace(index, primitive_records=19)
        )

def test_budget_counts_retained_and_temporary_bytes(self):
    with self.assertRaisesRegex(contract.IndexedWindowError, "512 MiB"):
        contract.require_storage_budget(
            retained_bytes=500 * 1024 * 1024,
            temporary_bytes=13 * 1024 * 1024,
        )
```

- [ ] **Step 2: Run RED test**

```bash
PYTHONPATH=$PWD python3 tests/pyunit/cross_system/test_indexed_window_contract.py
```

Expected: FAIL because the module is absent.

- [ ] **Step 3: Implement strict schemas**

```python
STORAGE_LIMIT_BYTES = 512 * 1024 * 1024

class IndexedWindowError(RuntimeError):
    pass

@dataclasses.dataclass(frozen=True)
class IndexSegment:
    primitive_begin: int
    primitive_end: int
    ordinal: int
    phase: int
    iteration: int
    kernel: str
    work_items: int

@dataclasses.dataclass(frozen=True)
class LazyIndex:
    schema: int
    workload: str
    descriptor_sha256: str
    input_sha256: str
    source_sha256: str
    binary_sha256: str
    config_sha256: str
    generator_sha256: str
    primitive_records: int
    segments: tuple[IndexSegment, ...]
```

Add `RealizedWindow`, `SparseStateRecord`, and `RetainedPackage` dataclasses.
Readers must reject unknown/missing fields, booleans used as integers, bad
hashes, missing files, gaps, overlaps, non-contiguous ordinals, and terminal
count drift. Writers use `cross_system_contract.atomic_write_json`.

- [ ] **Step 4: Run GREEN test**

Run the Step 2 command. Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/indexed_window_contract.py tests/pyunit/cross_system/test_indexed_window_contract.py
git commit -m "eval: add indexed window artifact contracts"
```

### Task 2: Authenticated streaming MCF EVENTS reader

**Files:**
- Modify: `scripts/mcfreg2.py:400-610,1121-1190`
- Create: `tests/pyunit/cross_system/test_mcf_selected_windows.py`

- [ ] **Step 1: Write failing bounded-reader tests**

```python
def test_stream_events_never_reads_the_whole_section(self):
    path, expected = make_gzip_mcfreg2_fixture(self.root)
    with mock.patch.object(
        mcfreg2.SectionView, "read",
        side_effect=AssertionError("EVENTS must stay lazy"),
    ):
        self.assertEqual(
            [item.row for item in mcfreg2.stream_events(path)], expected
        )

def test_stream_events_rejects_corruption(self):
    path, _ = make_gzip_mcfreg2_fixture(self.root)
    corrupt_stored_event_byte(path)
    with self.assertRaisesRegex(mcfreg2.FormatError, "EVENTS SHA-256"):
        list(mcfreg2.stream_events(path))
```

Also test truncated gzip, malformed JSON, wrong event count, and trailing data.

- [ ] **Step 2: Run RED test**

```bash
PYTHONPATH=$PWD python3 tests/pyunit/cross_system/test_mcf_selected_windows.py
```

Expected: FAIL because `stream_events()` is absent.

- [ ] **Step 3: Implement streaming digest verification**

```python
@dataclasses.dataclass(frozen=True)
class StreamedEvent:
    ordinal: int
    row: dict

def stream_events(path):
    package = read_package(path, lazy_section_names=("EVENTS",))
    entry = next(e for e in package.directory
                 if e.section_type == SECTION_TYPES["EVENTS"])
    reader = _DigestingSectionReader(path, entry)
    with gzip.GzipFile(fileobj=io.BufferedReader(reader)) as stream:
        for ordinal, line in enumerate(stream):
            yield StreamedEvent(ordinal, _event_json(line, ordinal))
    reader.finish(entry.sha256)
```

`_DigestingSectionReader.readinto()` must read no more than
`entry.stored_bytes`, hash stored bytes, reject truncation, and require the
yield count to equal `header.event_count` before `finish()` succeeds.

- [ ] **Step 4: Run reader and MCFREG2 regression tests**

```bash
PYTHONPATH=$PWD python3 tests/pyunit/cross_system/test_mcf_selected_windows.py
PYTHONPATH=$PWD python3 tests/pyunit/cross_system/test_mcfreg2.py
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/mcfreg2.py tests/pyunit/cross_system/test_mcf_selected_windows.py
git commit -m "eval: stream authenticated MCF events"
```

### Task 3: One-pass MCF selected-window package

**Files:**
- Create: `scripts/mcf_selected_windows.py`
- Modify: `tests/pyunit/cross_system/test_mcf_selected_windows.py`

- [ ] **Step 1: Write failing one-pass selection tests**

```python
def test_selects_union_of_windows_in_one_scan(self):
    plans = fixture_plans()
    with mock.patch.object(
        mcfreg2, "stream_events", wraps=mcfreg2.stream_events
    ) as stream:
        result = selected.select_windows(
            self.package, plans, self.root / "selected"
        )
    self.assertEqual(stream.call_count, 1)
    self.assertLess(result.retained_event_count, result.source_event_count)
    self.assertEqual(
        selected.read_coordinate(result.root, "pricing", 0).measure_start,
        plans["pricing"].windows[0].measure_start,
    )
```

Add cross-call, cross-phase, cross-stratum, duplicate-coordinate, crash-residue,
and 1-byte-budget rejection tests.

- [ ] **Step 2: Run RED test**

Run Task 2's first command. Expected: FAIL because the selector is absent.

- [ ] **Step 3: Implement interval-union selection**

Expose:

```python
@dataclasses.dataclass(frozen=True)
class SelectedPackage:
    root: Path
    source_event_count: int
    retained_event_count: int
    package_sha256: str
    index_sha256: str

def read_coordinate(root, phase, window_index):
    value = contract.load_json(Path(root) / "index.json")
    matches = [row for row in value["coordinates"]
               if row["phase"] == phase
               and row["window_index"] == window_index]
    if len(matches) != 1:
        raise SelectionError("selected MCF coordinate is absent or duplicate")
    return MaterializedSelection(**matches[0])
```

Implement `select_windows(package_path, plans, outdir) -> SelectedPackage` by
flattening all canonical plan coordinates, merging overlapping phase-local
intervals, then scan EVENTS once while validating CALL_BEGIN/CALL_END nesting
and phase-local work-item ordinals. Store only selected canonical records in
`windows.jsonl.gz`. `index.json` binds requested/realized coordinates, source
ordinals, retained offsets, source/package/plan/generator hashes, and counts.
Publish an fsynced temporary directory with `os.replace()` only after source
digest/count validation and `require_storage_budget()` pass.

- [ ] **Step 4: Run deterministic selector tests**

Run the Task 2 first command. Expected: PASS and two runs produce identical
package/index hashes.

- [ ] **Step 5: Commit**

```bash
git add scripts/mcf_selected_windows.py tests/pyunit/cross_system/test_mcf_selected_windows.py
git commit -m "eval: select MCF windows in one bounded pass"
```

### Task 4: NPB cardinalities and prefix index

**Files:**
- Modify: `scripts/npb_lazy_trace.py:90-1758`
- Create: `scripts/npb_indexed_windows.py`
- Modify: `scripts/build_matched_breadth_workloads.py:1666-2010`
- Create: `tests/pyunit/cross_system/test_npb_indexed_windows.py`

- [ ] **Step 1: Write failing cardinality properties**

```python
def test_cardinality_matches_every_fixture_expander(self):
    for bundle in (self.cg, self.mg):
        with lazy.MappedState(bundle) as state:
            for invocation in bundle.invocations:
                clone = state.clone_for_test()
                actual = sum(1 for _ in npb.EXPANDERS[invocation.kernel](
                    clone, invocation, 4
                ))
                self.assertEqual(npb.primitive_count(invocation), actual)

def test_index_covers_formal_dynamic_count(self):
    index = indexed.build_index(self.cg)
    self.assertEqual(index.segments[0].primitive_begin, 0)
    self.assertEqual(index.segments[-1].primitive_end,
                     self.cg.dynamic_work["primitive_records"])
```

- [ ] **Step 2: Run RED test**

```bash
PYTHONPATH=$PWD python3 tests/pyunit/cross_system/test_npb_indexed_windows.py
```

Expected: FAIL because cardinality/index APIs are absent.

- [ ] **Step 3: Centralize exact formulas and build the index**

```python
def primitive_count(invocation):
    counts = {
        "npb_cg_dot": 4 * invocation.work_items + 6,
        "npb_cg_divide": 4,
        "npb_cg_update_zr": 12 * invocation.work_items + 6,
        "npb_cg_update_p": 5 * invocation.work_items + 2,
        "npb_cg_residual_norm": 5 * invocation.work_items + 7,
        "npb_cg_init": 6 * invocation.work_items + 2,
        "npb_cg_outer_dots": 8 * invocation.work_items + 10,
        "npb_cg_normalize": 3 * invocation.work_items + 8,
        "npb_cg_prepare_iteration": 8,
    }
    if invocation.kernel in counts:
        return counts[invocation.kernel]
    return _structured_kernel_primitive_count(invocation)
```

Move the existing exact MG count functions from
`build_matched_breadth_workloads.py` into `npb_lazy_trace.py`. Add `nonzeros`
to CG SpMV parameters and validate it against row offsets; compute its count as
`3 * rows + 5 * nonzeros + 2`. All arithmetic uses checked uint64 helpers.
`build_index()` cumulatively creates `IndexSegment` rows and rejects a final
count different from `dynamic_work.primitive_records`; `locate()` uses
`bisect_right` and rejects offsets outside `[0, primitive_records)`.

- [ ] **Step 4: Run NPB and builder regression tests**

```bash
PYTHONPATH=$PWD python3 tests/pyunit/cross_system/test_npb_indexed_windows.py
PYTHONPATH=$PWD python3 tests/pyunit/cross_system/test_matched_breadth_gem5.py
PYTHONPATH=$PWD python3 tests/pyunit/cross_system/test_prepare_native_verified_breadth_suite.py
```

Expected: PASS, including exact formal CG/MG totals.

- [ ] **Step 5: Commit**

```bash
git add scripts/npb_lazy_trace.py scripts/npb_indexed_windows.py scripts/build_matched_breadth_workloads.py tests/pyunit/cross_system/test_npb_indexed_windows.py
git commit -m "eval: index NPB lazy invocations"
```

### Task 5: Safe slicing and dependency closure

**Files:**
- Modify: `scripts/npb_lazy_trace.py`
- Modify: `scripts/npb_indexed_windows.py`
- Modify: `tests/pyunit/cross_system/test_npb_indexed_windows.py`

- [ ] **Step 1: Write failing slice-equivalence tests**

```python
def test_safe_slice_equals_sequential_fixture_bytes(self):
    for invocation in all_fixture_invocations(self.cg, self.mg):
        first, stop = indexed.safe_fixture_slice(invocation)
        expected = packed_sequential_subset(invocation, first, stop)
        actual = packed(npb.expand_slice(
            fixture_sparse_state(invocation, first, stop),
            invocation, first, stop, 4,
        ))
        self.assertEqual(actual, expected)

def test_dependency_closure_contains_indirection_and_halo(self):
    cg = npb.dependency_closure(self.cg_spmv, 3, 5)
    self.assertIn(("rowstr", 3), cg.array_words)
    self.assertTrue(any(name == "colidx" for name, _ in cg.array_words))
    self.assertTrue(
        npb.dependency_closure(self.mg_resid, 1, 2).has_complete_halo
    )
```

Add tests rejecting a split sparse row, reduction merge, MG x-row/comm3 step,
empty slice, cross-stratum realignment, and missing sparse input.

- [ ] **Step 2: Run RED test**

Run Task 4's first command. Expected: FAIL because slicing APIs are absent.

- [ ] **Step 3: Implement exact safe slices**

```python
@dataclasses.dataclass(frozen=True)
class DependencyClosure:
    array_words: tuple[tuple[str, int], ...]
    scalar_names: tuple[str, ...]
    has_complete_halo: bool

def safe_work_item_range(invocation, requested_first, requested_stop,
                         *, stratum_first, stratum_stop):
    try:
        resolver = SAFE_RANGE_RESOLVERS[invocation.kernel]
    except KeyError as error:
        raise lazy.LazyTraceError(
            f"unknown NPB kernel {invocation.kernel}"
        ) from error
    realized = resolver(invocation, requested_first, requested_stop)
    if realized[0] < stratum_first or realized[1] > stratum_stop:
        raise lazy.LazyTraceError("safe NPB range leaves its stratum")
    return realized

def dependency_closure(invocation, first, stop):
    try:
        return DEPENDENCY_RESOLVERS[invocation.kernel](
            invocation, first, stop
        )
    except KeyError as error:
        raise lazy.LazyTraceError(
            f"unknown NPB kernel {invocation.kernel}"
        ) from error

def expand_slice(state, invocation, first, stop,
                 batch_work_items=1024):
    try:
        expander = SLICE_EXPANDERS[invocation.kernel]
    except KeyError as error:
        raise lazy.LazyTraceError(
            f"unknown NPB kernel {invocation.kernel}"
        ) from error
    yield from expander(state, invocation, first, stop, batch_work_items)
```

Use complete CG rows, reduction lanes, vector elements, and MG interior x-rows
as safe units. Preserve existing neighbor and floating-point order. The sparse
state adapter rejects an unauthoritative load and overlays preceding stores.
Every expander kernel in `EXPANDERS` must have a slice-equivalence subtest.

- [ ] **Step 4: Run all slice tests**

```bash
PYTHONPATH=$PWD python3 tests/pyunit/cross_system/test_npb_indexed_windows.py
PYTHONPATH=$PWD python3 tests/pyunit/cross_system/test_matched_breadth_gem5.py
```

Expected: PASS for every CG/MG kernel.

- [ ] **Step 5: Commit**

```bash
git add scripts/npb_lazy_trace.py scripts/npb_indexed_windows.py tests/pyunit/cross_system/test_npb_indexed_windows.py
git commit -m "eval: slice NPB lazy kernels at safe cuts"
```

### Task 6: Native sparse-state capture ABI

**Files:**
- Modify: `util/amu/matched_workloads/npb_trace_hooks.h`
- Modify: `util/amu/matched_workloads/npb_trace_hooks.cc`
- Modify: `util/amu/matched_workloads/npb-cg-trace.patch`
- Modify: `util/amu/matched_workloads/npb-mg-trace.patch`
- Modify: `scripts/build_matched_breadth_workloads.py:1520-1665,2140-2760`
- Modify: `scripts/npb_indexed_windows.py`
- Create: `tests/pyunit/cross_system/test_npb_sparse_capture.py`

- [ ] **Step 1: Write failing plan/capture tests**

```python
def test_capture_binds_array_and_scalar_raw_words(self):
    plan = indexed.SparseCapturePlan(
        descriptor_sha256="a" * 64,
        entries=(indexed.ArrayRequest(3, 4, 7),
                 indexed.ScalarRequest(3, 2)),
    )
    indexed.write_sparse_capture_plan(self.plan_path, plan)
    capture = run_native_fixture(self.plan_path, self.capture_path)
    parsed = builder.parse_npb_sparse_capture(capture, plan)
    self.assertEqual(parsed.request_count, 2)
```

Add missing, duplicate, reordered, out-of-range, one-bit raw drift, wrong plan
hash, failed native verifier, and crash-before-rename tests.

- [ ] **Step 2: Run RED test**

```bash
PYTHONPATH=$PWD python3 tests/pyunit/cross_system/test_npb_sparse_capture.py
```

Expected: FAIL because the ABI is absent.

- [ ] **Step 3: Implement binary plan and capture formats**

Add C ABI:

```c
void matched_sparse_scalar_u64_(const int64_t *scalar_id,
                                const uint64_t *raw_word);
void matched_sparse_invocation_(const int64_t *ordinal);
```

Use `MATCHED_NPB_SPARSE_PLAN_FILE` and
`MATCHED_NPB_SPARSE_CAPTURE_FILE`. Plan header `<8sQQ32s>` uses magic
`NPBSPN01`; requests `<QQQQ>` store ordinal, kind, id, index. Capture header
`<8sQQ32s32s>` uses magic `NPBSPC01`; records `<QQQQQ>` add raw word.
Extend `ArrayIdentity` with the registered data pointer. Reject changed repeat
registration, unknown IDs, missing scalars, ordinal regression, unmatched
requests, short writes, and a non-little-endian host.

Update both patches to register every scalar named by descriptor parameters
immediately before `matched_sparse_invocation_`. A checked name-to-ID mapping
is emitted into the prepared manifest. The Python parser requires exact record
equality and binds capture/source/binary/config/plan hashes, native command,
stdout, exit status, official verification, allocation evidence, and final
boundary commitments.

- [ ] **Step 4: Run native fixture and preparation tests**

```bash
PYTHONPATH=$PWD python3 tests/pyunit/cross_system/test_npb_sparse_capture.py
PYTHONPATH=$PWD python3 tests/pyunit/cross_system/test_prepare_native_verified_breadth_suite.py
```

Expected: PASS and all corruption cases fail closed.

- [ ] **Step 5: Commit**

```bash
git add util/amu/matched_workloads/npb_trace_hooks.h util/amu/matched_workloads/npb_trace_hooks.cc util/amu/matched_workloads/npb-cg-trace.patch util/amu/matched_workloads/npb-mg-trace.patch scripts/build_matched_breadth_workloads.py scripts/npb_indexed_windows.py tests/pyunit/cross_system/test_npb_sparse_capture.py
git commit -m "eval: capture native NPB sparse window state"
```

### Task 7: Indexed NPB dynamic/fixed materialization

**Files:**
- Modify: `scripts/npb_indexed_windows.py`
- Modify: `scripts/run_matched_breadth_gem5.py:356-510`
- Modify: `tests/pyunit/cross_system/test_npb_indexed_windows.py`
- Modify: `tests/pyunit/cross_system/test_matched_breadth_gem5.py`

- [ ] **Step 1: Write failing no-prefix-expansion test**

```python
def test_late_window_never_calls_full_expander(self):
    with mock.patch.object(
        npb, "expanded_evidence",
        side_effect=AssertionError("full expansion forbidden"),
    ):
        result = indexed.materialize_window(
            self.bundle, self.index, self.realized,
            self.sparse_state, self.root / "window",
        )
    self.assertEqual(result.source_schema, 2)
    self.assertEqual(result.measured_items, self.realized.measured_records)
```

Also compare dynamic/fixed bytes with sequential fixtures and reject changed
index, plan, state, descriptor, or source hashes.

- [ ] **Step 2: Run RED tests**

Run both files listed under Files. Expected: FAIL because indexed
materialization is not connected.

- [ ] **Step 3: Implement materialization and route schema 2**

`realize_plan()` maps phase-local coordinates to safe invocation-local ranges
without leaving the stratum. `materialize_window()` loads exact sparse state,
calls only `expand_slice()` for intersecting invocations, reuses
`_write_partitioned_payload()` and `_write_sparse_initial()`, and writes
`materialized-window.v2.json` with every parent hash and count.

Keep schema 1 replay unchanged. Schema 2 requires `--indexed-window-root` and
delegates to `npb_indexed_windows`; remove the sequential all-invocation loop.
A missing index fails with `indexed NPB window artifacts are required`.

- [ ] **Step 4: Run materializer regressions**

```bash
PYTHONPATH=$PWD python3 tests/pyunit/cross_system/test_npb_indexed_windows.py
PYTHONPATH=$PWD python3 tests/pyunit/cross_system/test_matched_breadth_gem5.py
```

Expected: PASS and the late-window test proves no prefix expansion.

- [ ] **Step 5: Commit**

```bash
git add scripts/npb_indexed_windows.py scripts/run_matched_breadth_gem5.py tests/pyunit/cross_system/test_npb_indexed_windows.py tests/pyunit/cross_system/test_matched_breadth_gem5.py
git commit -m "eval: materialize indexed NPB windows"
```

### Task 8: Prepared action driver

**Files:**
- Create: `scripts/run_prepared_breadth_action.py`
- Create: `tests/pyunit/cross_system/test_run_prepared_breadth_action.py`
- Modify: `scripts/prepare_native_verified_breadth_suite.py:529-610`

- [ ] **Step 1: Write failing dispatch/evidence tests**

```python
def test_dispatches_exact_stage(self):
    for stage, target in (("reference", "run_reference"),
                          ("functional", "run_functional"),
                          ("window", "run_window")):
        with mock.patch.object(driver, target, return_value=pass_evidence()):
            self.assertEqual(driver.main(argv_for(stage)), 0)

def test_window_binds_latency_and_coordinate(self):
    result = driver.execute(parse_window_args("500ns", 7))
    self.assertEqual(result["cxl_link_delay_ticks"], 500_000)
    self.assertEqual(result["coordinate"]["window_index"], 7)
    self.assertEqual(result["threads"], 4)
    self.assertTrue(result["all_memory_cxl"])
```

Add command mismatch, escaping evidence, unknown identity, latency drift,
mechanism errors, malformed output, and valid relaxed numerical output tests.

- [ ] **Step 2: Run RED test**

```bash
PYTHONPATH=$PWD python3 tests/pyunit/cross_system/test_run_prepared_breadth_action.py
```

Expected: FAIL because the driver is absent.

- [ ] **Step 3: Implement exact CLI and dispatcher**

Require the exact action-layout arguments: prepared manifest, workload, stage,
system, phase/window/level/stratum coordinates, CXL latency, and evidence path.
Re-render the manifest command and require exact `sys.argv` equality. Verify all
input/code/config hashes before dispatch.

Route MCF to `mcf_selected_windows`, NPB to `npb_indexed_windows`, AMG/LULESH
to their prepared trace pair, PageRank to the formal g20 path, gem5 systems to
`run_matched_breadth_gem5`, and M2NDP to existing package functions. Validate
relaxed correctness, named outputs, threads, placement, latency, balanced
activity, and zero errors before atomically writing evidence with its exact
command. Every functional and timing record carries an explicit `bit_exact`
boolean plus native/numerical verification fields; a non-bit-exact float result
is accepted only through the approved tolerance and is never relabeled exact.
M2NDP timing additionally calls the shared correctness-plus-minimum-performance
policy rather than the retired 1.6x maximum.

- [ ] **Step 4: Run driver and executor tests**

```bash
PYTHONPATH=$PWD python3 tests/pyunit/cross_system/test_run_prepared_breadth_action.py
PYTHONPATH=$PWD python3 tests/pyunit/cross_system/test_run_cira_amu_m2ndp_breadth.py
```

Expected: PASS and evidence command equals `ManifestExecutor` rendering.

- [ ] **Step 5: Commit**

```bash
git add scripts/run_prepared_breadth_action.py scripts/prepare_native_verified_breadth_suite.py tests/pyunit/cross_system/test_run_prepared_breadth_action.py
git commit -m "eval: drive prepared breadth actions"
```

### Task 9: Complete six-workload manifest and preflight

**Files:**
- Modify: `scripts/prepare_native_verified_breadth_suite.py:581-650`
- Modify: `scripts/run_cira_amu_m2ndp_breadth.py:970-1150,1500-1580`
- Modify: `tests/pyunit/cross_system/test_prepare_native_verified_breadth_suite.py`
- Modify: `tests/pyunit/cross_system/test_run_cira_amu_m2ndp_breadth.py`

- [ ] **Step 1: Write failing complete-manifest tests**

```python
def test_prepare_writes_six_executable_workloads(self):
    self.assertEqual(prepare.main(preparation_argv(self.root)), 0)
    value = contract.load_json(self.root / "prepared/manifest.json")
    self.assertEqual(set(value["workloads"]), set(prepare.WORKLOADS))
    for workload in prepare.WORKLOADS:
        self.assertTrue(
            value["workloads"][workload]["actions"]["reference"]["command"]
        )

def test_preflight_rejects_missing_npb_index(self):
    remove_index(self.root, "npb_cg")
    with self.assertRaisesRegex(breadth.BreadthError, "npb_cg indexed"):
        breadth.validate_prepared_manifest(self.root / "prepared/manifest.json")
```

- [ ] **Step 2: Run RED tests**

```bash
PYTHONPATH=$PWD python3 tests/pyunit/cross_system/test_prepare_native_verified_breadth_suite.py
PYTHONPATH=$PWD python3 tests/pyunit/cross_system/test_run_cira_amu_m2ndp_breadth.py
```

Expected: FAIL because current CLI only publishes the NPB submanifest.

- [ ] **Step 3: Publish and validate the full manifest**

Add required `--prepared-outdir` and `--action-driver`. Load the accepted
six-input record, PageRank package, MCF package, AMG/LULESH packages, and
native-verified NPB records. Build/validate MCF selected packages and NPB
indexes/sparse capture, then call `prepared_manifest()`.

Every workload row records phase counts, trace/reference boundaries, shared
artifacts, action layout, correctness policy, and index identities. Bind all
producer hashes. Publish only from an fsynced temporary root. Preflight requires
six workloads, four systems, four canonical latency tokens, action-driver hash,
MCF/NPB identities, and storage limits before launching a subprocess.

- [ ] **Step 4: Run GREEN tests**

Run Step 2 commands. Expected: PASS; incomplete/stale manifests fail before
execution.

- [ ] **Step 5: Commit**

```bash
git add scripts/prepare_native_verified_breadth_suite.py scripts/run_cira_amu_m2ndp_breadth.py tests/pyunit/cross_system/test_prepare_native_verified_breadth_suite.py tests/pyunit/cross_system/test_run_cira_amu_m2ndp_breadth.py
git commit -m "eval: publish executable six-workload suite"
```

### Task 10: Formal qualification and handoff

**Files:**
- Create: `scripts/qualify_indexed_breadth_windows.py`
- Create: `tests/pyunit/cross_system/test_qualify_indexed_breadth_windows.py`
- Modify: `docs/amu-gapbs-benchmark.md`

- [ ] **Step 1: Write failing qualification tests**

```python
def test_requires_real_cg_mg_and_four_systems(self):
    self.assertEqual(qualify.validate(valid_evidence())["status"], "pass")
    for workload in ("npb_cg", "npb_mg"):
        for system in ("vanilla", "amu", "cira", "m2ndp-funcsim"):
            broken = without_system(valid_evidence(), workload, system)
            with self.assertRaisesRegex(qualify.QualificationError, system):
                qualify.validate(broken)
```

Add prefix-expansion marker, identity drift, raw-word mismatch, mechanism
error, native-verifier failure, and storage overflow rejection tests.

- [ ] **Step 2: Run RED test**

```bash
PYTHONPATH=$PWD python3 tests/pyunit/cross_system/test_qualify_indexed_breadth_windows.py
```

Expected: FAIL because the validator is absent.

- [ ] **Step 3: Implement independent qualification gate**

Require actual formal input hashes, bounded sequential equivalence for an early
CG/MG window, native sparse raw-word equality for every selected window, exact
structural streams, all four functional systems, native verification,
balanced AMU/CIRA activity, M2NDP FuncSim completion, zero errors, no full
expansion, and storage at or below 512 MiB. Output `qualification.json` with
hashes and no performance number.

- [ ] **Step 4: Run all affected tests**

```bash
set -o pipefail
for test_file in tests/pyunit/cross_system/test_indexed_window_contract.py tests/pyunit/cross_system/test_mcf_selected_windows.py tests/pyunit/cross_system/test_npb_indexed_windows.py tests/pyunit/cross_system/test_npb_sparse_capture.py tests/pyunit/cross_system/test_run_prepared_breadth_action.py tests/pyunit/cross_system/test_prepare_native_verified_breadth_suite.py tests/pyunit/cross_system/test_run_cira_amu_m2ndp_breadth.py tests/pyunit/cross_system/test_matched_breadth_gem5.py tests/pyunit/cross_system/test_qualify_indexed_breadth_windows.py; do
  PYTHONPATH=$PWD python3 "$test_file" || exit 1
done
git diff --check
```

Expected: every test PASS and `git diff --check` is silent.

- [ ] **Step 5: Run fresh live qualification only**

Use fresh root
`/mnt/disk0/gem5-CXL-eval/cira-amu-m2ndp-spectrum/indexed-breadth-qualification-r1`.
Reuse only hash-matched frozen inputs. Do not start latency campaigns until the
independent validator prints:

```text
INDEXED_BREADTH_QUALIFICATION_PASS workloads=npb_cg,npb_mg systems=4
```

- [ ] **Step 6: Document proof boundaries**

Record exact prepared/qualification paths, commands, hashes, storage usage,
correctness policy, and the fact that qualification contains no paper timing
result in `docs/amu-gapbs-benchmark.md`.

- [ ] **Step 7: Commit, verify, and push**

```bash
git add scripts/qualify_indexed_breadth_windows.py tests/pyunit/cross_system/test_qualify_indexed_breadth_windows.py docs/amu-gapbs-benchmark.md
git commit -m "eval: qualify indexed breadth windows"
git push origin m2ndp-g20-pr-spmv
```

Before claiming completion, invoke `superpowers:verification-before-completion`,
rerun Step 4, validate live hashes, and confirm the only unrelated worktree
entries remain user-owned `src/mem/cache/base.cc` and `.superpowers/`.
