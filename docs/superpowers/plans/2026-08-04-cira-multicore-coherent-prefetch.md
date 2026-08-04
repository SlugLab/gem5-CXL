# CIRA Multicore Coherent Prefetch Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Route CIRA prefetches to the issuing thread's private L2, coalesce duplicate cache-line requests per core, bound and fairly schedule CSR descriptors, and require two-core bit-exact and performance evidence.

**Architecture:** Keep one system-level CIRA object, but replace its scalar request port and probe target with per-core vectors. Each descriptor captures a target core, each core owns its send/retry queue and usefulness tracker, and the CSR scheduler round-robins bounded work across per-core descriptor queues. Physical cache-line state remains deduplicated through completion and bounded usefulness history.

**Tech Stack:** gem5 C++17 SimObjects and classic caches, gem5 Python configuration, Python `unittest`, standalone C++ contract tests compiled by Python, SCons X86 build, GAPBS `pr_spmv` verifier and bit-hash evidence pipeline.

---

## File Map

- Modify `src/mem/cira_usefulness_tracker.hh`: expose tracked-line state and bound completed-line history without adding gem5 dependencies.
- Modify `tests/pyunit/amu/test_cira_usefulness_contract.py`: compile and execute tracker transition tests and enforce the multicore port/probe configuration contract.
- Modify `src/mem/CIRA.py`: declare vector ports, vector probe targets, queue/fairness/history parameters, and new statistics-facing controls.
- Modify `src/mem/cira.hh`: add target-core ownership to ports, packets, descriptors, queues, trackers, and probes.
- Modify `src/mem/cira.cc`: implement routing, per-core retry/send paths, physical-line coalescing, fair bounded CSR scheduling, and per-core statistics.
- Modify `configs/example/gem5_library/x86-gapbs-amu-se.py`: connect CIRA port and probe index `i` to private L2 `i` and expose queue parameters.
- Modify `scripts/compare_gapbs_cxl_amu_cira.py`: forward the new parameters, collect per-core/coalescing/queue statistics, and fail formal two-core CIRA rows when either core is inactive or any request is rejected.
- Modify `tests/pyunit/amu/test_compare_gapbs_cxl_amu_cira.py`: cover new command-line arguments and evidence validation.
- Create `tests/gem5/cira/cira_multicore_prefetch.cc`: deterministic two-thread m5op micro-workload for per-core route/coalescing evidence.
- Create `tests/gem5/cira/run_cira_multicore.py`: minimal two-core timing configuration for the integration workload.
- Modify `src/mem/SConscript`: register a focused C++ tracker test if the standalone contract needs behavior not expressible through the existing Python harness.

### Task 1: Make tracked-line lifetime explicit and bounded

**Files:**
- Modify: `tests/pyunit/amu/test_cira_usefulness_contract.py`
- Modify: `src/mem/cira_usefulness_tracker.hh`

- [ ] **Step 1: Add failing tracker assertions**

Extend the compiled C++ program in `test_transition_machine` with:

```cpp
CiraLineUsefulnessTracker bounded(64, 2);
assert(!bounded.tracked(0x9000));
assert(bounded.issueIfAbsent(0x9000));
assert(!bounded.issueIfAbsent(0x903f));
bounded.fill(0x9008, true);
assert(bounded.tracked(0x9010));

assert(bounded.issueIfAbsent(0x9100));
bounded.fill(0x9100, true);
assert(bounded.issueIfAbsent(0x9200));
bounded.fill(0x9200, true);
assert(!bounded.tracked(0x9000));
assert(bounded.tracked(0x9100));
assert(bounded.tracked(0x9200));

assert(bounded.demand(0x9110, true) == Attribution::Useful);
assert(!bounded.tracked(0x9100));
assert(bounded.issueIfAbsent(0x9130));
```

- [ ] **Step 2: Run the focused test and verify RED**

Run:

```bash
python3 -m unittest tests.pyunit.amu.test_cira_usefulness_contract.CiraUsefulnessTrackerContractTest.test_transition_machine -v
```

Expected: compilation fails because the two-argument constructor, `tracked`, and `issueIfAbsent` do not exist.

- [ ] **Step 3: Implement bounded tracked-line state**

In `CiraLineUsefulnessTracker`, retain the existing `issue()` API for compatibility and add:

```cpp
explicit CiraLineUsefulnessTracker(uint64_t line_size,
                                   size_t max_completed_lines = 4096);
bool tracked(uint64_t addr) const;
bool issueIfAbsent(uint64_t addr);
```

Store a monotonically increasing generation in each `LineState` and a FIFO of
`(line, generation)` completed entries. `fill()` marks the current generation
completed and pushes it once. While the completed FIFO exceeds
`maxCompletedLines`, erase only an entry whose saved generation still matches
the current map entry. `demand()` and `clear()` retain the existing attribution
semantics and safely leave stale FIFO records to be ignored by generation.

- [ ] **Step 4: Run the tracker test and verify GREEN**

Run the command from Step 2. Expected: one test passes and the generated C++ program exits zero.

- [ ] **Step 5: Run the full existing CIRA usefulness contract**

Run:

```bash
python3 -m unittest tests.pyunit.amu.test_cira_usefulness_contract -v
```

Expected: all existing transition and integration contract tests pass.

- [ ] **Step 6: Commit Task 1**

```bash
git add src/mem/cira_usefulness_tracker.hh tests/pyunit/amu/test_cira_usefulness_contract.py
git commit -m "test: define bounded CIRA line coalescing"
```

### Task 2: Define the multicore SimObject and board wiring contract

**Files:**
- Modify: `tests/pyunit/amu/test_cira_usefulness_contract.py`
- Modify: `src/mem/CIRA.py`
- Modify: `configs/example/gem5_library/x86-gapbs-amu-se.py`

- [ ] **Step 1: Replace the scalar static assertions with failing multicore assertions**

Update `test_cira_probe_and_stats_integration_contract` to require:

```python
self.assertIn('mem_side_ports = VectorRequestPort(', cira_py)
self.assertIn('demand_probe_targets = VectorParam.SimObject(', cira_py)
self.assertNotIn('demand_probe_target = Param.SimObject(', cira_py)
self.assertIn('max_csr_walk_queue = Param.Unsigned(', cira_py)
self.assertIn('csr_lines_per_turn = Param.Unsigned(', cira_py)
self.assertIn('max_completed_lines = Param.Unsigned(', cira_py)
self.assertIn('for idx, l2bus in enumerate(self.cache_hierarchy.l2buses):', config)
self.assertIn('cira.mem_side_ports = l2bus.cpu_side_ports', config)
self.assertIn('cira.demand_probe_targets = [', config)
self.assertNotIn('"l2-cache-0"', config[config.index('def _connect_things'):config.index('parser =')])
```

- [ ] **Step 2: Run the integration contract and verify RED**

Run:

```bash
python3 -m unittest tests.pyunit.amu.test_cira_usefulness_contract.CiraUsefulnessTrackerContractTest.test_cira_probe_and_stats_integration_contract -v
```

Expected: failure because CIRA still declares and connects one scalar port/probe.

- [ ] **Step 3: Change the Python SimObject interface**

In `src/mem/CIRA.py`, use:

```python
mem_side_ports = VectorRequestPort(
    "Per-core timing prefetch ports toward private L2s"
)
demand_probe_targets = VectorParam.SimObject(
    [], "Private L2s used for target-local CIRA usefulness attribution"
)
max_csr_walk_queue = Param.Unsigned(
    4096, "Maximum total queued CIRA CSR descriptors"
)
csr_lines_per_turn = Param.Unsigned(
    64, "Maximum unique line candidates expanded per core scheduling turn"
)
max_completed_lines = Param.Unsigned(
    65536, "Maximum completed usefulness records retained per core"
)
```

- [ ] **Step 4: Connect every private L2 by index**

Replace the core-0 special case in `CXLSimpleBoard._connect_things()` with:

```python
if self._cira_to_l2 and hasattr(self.cache_hierarchy, "l2buses"):
    cira.demand_probe_targets = [
        getattr(self.cache_hierarchy, f"l2-cache-{idx}")
        for idx in range(len(self.cache_hierarchy.l2buses))
    ]
    for idx, l2bus in enumerate(self.cache_hierarchy.l2buses):
        cira.mem_side_ports = l2bus.cpu_side_ports
else:
    cira.demand_probe_targets = []
    cira.mem_side_ports = self.cache_hierarchy.get_cpu_side_port()
```

Add CLI options `--cira-max-csr-walk-queue`,
`--cira-csr-lines-per-turn`, and `--cira-max-completed-lines`, and pass them to
the `CIRA` constructor.

- [ ] **Step 5: Run the focused contract and verify GREEN**

Run the command from Step 2. Expected: pass.

- [ ] **Step 6: Commit Task 2**

```bash
git add src/mem/CIRA.py configs/example/gem5_library/x86-gapbs-amu-se.py tests/pyunit/amu/test_cira_usefulness_contract.py
git commit -m "config: wire CIRA to every private L2"
```

### Task 3: Implement per-core ports, probes, queues, and physical-line coalescing

**Files:**
- Modify: `tests/pyunit/amu/test_cira_usefulness_contract.py`
- Modify: `src/mem/cira.hh`
- Modify: `src/mem/cira.cc`

- [ ] **Step 1: Add failing source-contract assertions for ownership**

Require the C++ sources to contain the following stable ownership concepts:

```python
self.assertIn('PortID targetCore', cira_hh)
self.assertIn('std::vector<std::unique_ptr<MemoryPort>> memSidePorts', cira_hh)
self.assertIn('std::vector<CiraLineUsefulnessTracker> lineTrackers', cira_hh)
self.assertIn('std::vector<std::deque<CsrWalkState>> csrWalkQueues', cira_hh)
self.assertIn('resolveTargetCore(ThreadContext *tc)', cira_hh)
self.assertIn('params.port_mem_side_ports_connection_count', cira_cc)
self.assertIn('params.demand_probe_targets', cira_cc)
self.assertIn('lineTrackers.at(targetCore).issueIfAbsent', cira_cc)
```

- [ ] **Step 2: Run the source contract and verify RED**

Run the integration-contract command from Task 2. Expected: failure on the missing per-core C++ ownership structures.

- [ ] **Step 3: Add target ownership to data structures**

In `cira.hh`:

```cpp
struct PacketSenderState : public Packet::SenderState {
    PacketSenderState(uint64_t request_id, PortID target_core)
        : id(request_id), targetCore(target_core) {}
    uint64_t id;
    PortID targetCore;
};

struct RequestState {
    uint64_t id = 0;
    PortID targetCore = InvalidPortID;
    // retain tc, vaddr, size, issueTick, pendingPackets
};
```

Make `MemoryPort` store its target index and call
`recvTimingResp(targetCore, pkt)` / `recvReqRetry(targetCore)`. Replace scalar
port/queue/retry/tracker members with vectors sized from
`port_mem_side_ports_connection_count`. Add `resolveTargetCore(tc)` that checks
`tc != nullptr`, uses `tc->contextId()`, and fatals if it is outside the vector.

- [ ] **Step 4: Implement vector getPort/init/probe registration**

`getPort("mem_side_ports", idx)` must reject `InvalidPortID` and out-of-range
indices. `init()` must require equal, nonzero port and probe counts for the
private-L2 mode. `CacheProbeListener` stores `targetCore`; probe callbacks call:

```cpp
handleCacheProbe(targetCore, event, arg);
```

All tracker operations use `lineTrackers.at(targetCore)` and all per-core vector
statistics use the same index.

- [ ] **Step 5: Coalesce translated physical lines**

Change `issuePrefetch` to resolve a target before translation. Decompose every
translation chunk into cache-line-contained candidates. Before allocating a
packet, call:

```cpp
auto &tracker = lineTrackers.at(targetCore);
if (!tracker.issueIfAbsent(paddr)) {
    ++stats.coalescedPrefetches;
    ++stats.coalescedPerCore[targetCore];
    continue;
}
```

Create a request state only when at least one unique packet remains. Store
`targetCore` in its sender state, enqueue it only in that core's send queue, and
use only that core's port for initial send and retry. Roll back newly tracked
lines if packet allocation cannot be completed atomically.

- [ ] **Step 6: Run contract tests and build gem5**

Run:

```bash
python3 -m unittest tests.pyunit.amu.test_cira_usefulness_contract -v
scons build/X86/gem5.opt -j2
```

Expected: all contract tests pass and SCons exits zero.

- [ ] **Step 7: Commit Task 3**

```bash
git add src/mem/cira.hh src/mem/cira.cc tests/pyunit/amu/test_cira_usefulness_contract.py
git commit -m "feat: route and coalesce CIRA prefetches per core"
```

### Task 4: Bound and fairly schedule CSR descriptors

**Files:**
- Modify: `tests/pyunit/amu/test_cira_usefulness_contract.py`
- Modify: `src/mem/cira.hh`
- Modify: `src/mem/cira.cc`

- [ ] **Step 1: Add failing scheduling-contract assertions**

Require `cira.hh`/`cira.cc` to expose `maxCsrWalkQueue`,
`csrLinesPerTurn`, `nextCsrCore`, `queuedCsrWalks()`,
`droppedCsrDescriptors`, and `csrQueueHighWatermark`. Require
`issueCsrPrefetch()` to check total queue capacity before `push_back`.

- [ ] **Step 2: Run the focused contract and verify RED**

Run the integration-contract test. Expected: failure because the existing CSR queue is unbounded and global.

- [ ] **Step 3: Implement atomic admission**

Before accepting a CSR walk:

```cpp
if (queuedCsrWalks() >= maxCsrWalkQueue) {
    ++stats.rejectedQueueFull;
    ++stats.droppedCsrDescriptors;
    return 0;
}
```

Only then increment `issuedCsrPrefetches` and append to
`csrWalkQueues[targetCore]`. Update the high-water mark after insertion. Invalid
descriptors remain rejected before either counter changes.

- [ ] **Step 4: Implement bounded round-robin expansion**

On every CSR event, start at `nextCsrCore`, visit each nonempty core queue, and
expand at most `csrLinesPerTurn` candidate entries for that core. Advance
`nextCsrCore` after every serviced queue. Reschedule when queued work remains
and any target port has capacity; otherwise responses/retries reschedule the
walker. Preserve CSR entry order within each descriptor.

- [ ] **Step 5: Run tests and rebuild**

Run:

```bash
python3 -m unittest tests.pyunit.amu.test_cira_usefulness_contract -v
scons build/X86/gem5.opt -j2
```

Expected: tests pass and build exits zero.

- [ ] **Step 6: Commit Task 4**

```bash
git add src/mem/cira.hh src/mem/cira.cc tests/pyunit/amu/test_cira_usefulness_contract.py
git commit -m "feat: bound and fairly schedule CIRA CSR walks"
```

### Task 5: Make formal evidence reject inactive cores and hidden drops

**Files:**
- Modify: `tests/pyunit/amu/test_compare_gapbs_cxl_amu_cira.py`
- Modify: `scripts/compare_gapbs_cxl_amu_cira.py`

- [ ] **Step 1: Write failing runner tests**

Add synthetic stats for:

```text
board.cira.issuedPrefetchesPerCore::0 100
board.cira.issuedPrefetchesPerCore::1 120
board.cira.completedPrefetchesPerCore::0 100
board.cira.completedPrefetchesPerCore::1 120
board.cira.usefulPrefetchesPerCore::0 10
board.cira.usefulPrefetchesPerCore::1 12
board.cira.coalescedPrefetches 500
board.cira.droppedCsrDescriptors 0
board.cira.csrQueueHighWatermark 8
```

Test that a two-core CIRA row passes with both cores active, becomes
`inactive-cira-core` when either issued/completed pair is zero, and becomes
`cira-rejected-work` when `rejectedQueueFull` or `droppedCsrDescriptors` is
nonzero. Test that `append_kind_args` forwards all three new CIRA parameters.

- [ ] **Step 2: Run runner tests and verify RED**

Run:

```bash
python3 -m unittest tests.pyunit.amu.test_compare_gapbs_cxl_amu_cira -v
```

Expected: new tests fail because the runner neither parses nor gates these fields.

- [ ] **Step 3: Extend command and evidence schemas**

Add CLI defaults matching `CIRA.py`, forward them in `append_kind_args`, and
include them in checkpoint model parameters. Parse aggregate and per-core CIRA
statistics into summary/evidence fields. When `args.cores > 1`, require every
core's issued count to be positive and equal to its completed count. Reject any
formal CIRA run with queue/capacity drops.

- [ ] **Step 4: Run runner tests and verify GREEN**

Run the command from Step 2. Expected: all tests pass.

- [ ] **Step 5: Commit Task 5**

```bash
git add scripts/compare_gapbs_cxl_amu_cira.py tests/pyunit/amu/test_compare_gapbs_cxl_amu_cira.py
git commit -m "test: gate CIRA evidence on both cores"
```

### Task 6: Add a live two-core routing/coalescing proof

**Files:**
- Create: `tests/gem5/cira/cira_multicore_prefetch.cc`
- Create: `tests/gem5/cira/run_cira_multicore.py`
- Modify: `tests/pyunit/amu/test_cira_usefulness_contract.py`

- [ ] **Step 1: Add a failing integration-test launcher**

Add a Python test that skips only when `build/X86/gem5.opt` is absent, builds
the C++ workload with `-fopenmp` and `util/m5`, runs the two-core config, and
requires from `stats.txt`:

```python
assert issued_per_core[0] > 0
assert issued_per_core[1] > 0
assert completed_per_core == issued_per_core
assert coalesced_prefetches > 0
assert dropped_csr_descriptors == 0
```

- [ ] **Step 2: Run the integration test and verify RED**

Run the new test by its full unittest name. Expected: failure because the workload/config files do not exist.

- [ ] **Step 3: Create the deterministic workload**

The workload allocates two page-aligned arrays, enters an OpenMP parallel region
with exactly two threads, and has each thread issue repeated indexed/CSR
prefetches whose values map to repeated lines. Each thread then reads its own
target lines in deterministic order, checks their known contents, drains CIRA,
and calls `m5_exit(0)`. It must not write shared target lines while prefetches
are active.

- [ ] **Step 4: Create the two-core gem5 configuration**

Instantiate two timing CPUs, the same private-L1/private-L2 classic hierarchy,
CIRA vector wiring, and a small memory. Enable work/exit handling and write
stats after CIRA drains. Use a short memory delay so this is a routing proof,
not a performance benchmark.

- [ ] **Step 5: Run the live integration test and verify GREEN**

Run the command from Step 2. Expected: gem5 exits zero and every assertion passes.

- [ ] **Step 6: Commit Task 6**

```bash
git add tests/gem5/cira tests/pyunit/amu/test_cira_usefulness_contract.py
git commit -m "test: prove two-core CIRA routing and coalescing"
```

### Task 7: Rebuild matched PageRank and prove small-graph bit exactness

**Files:**
- Generated artifacts only under `/tmp`; no source files are committed.

- [ ] **Step 1: Run the complete focused test suite**

```bash
python3 -m unittest \
  tests.pyunit.amu.test_cira_usefulness_contract \
  tests.pyunit.amu.test_compare_gapbs_cxl_amu_cira -v
scons build/X86/gem5.opt -j2
```

Expected: zero failures and successful X86 build.

- [ ] **Step 2: Build matched Vanilla/CIRA binaries with an explicit 1 us profile override**

Use `scripts/build_gapbs_matched_pr_spmv_variants.py` with a temporary output
directory, the existing GAPBS/CXLMemUring roots from the formal manifest, and
an explicit CIRA distance supplied by the test matrix rather than silently
reusing the 165 ns PGO profile. Record the exact command and manifest SHA-256.

- [ ] **Step 3: Run deterministic small-graph two-core all-CXL verification**

Run Vanilla and CIRA with `-g 10 -n 20 -v`, two timing cores, all-CXL memory,
1 us link delay, identical cache settings, and ROI work events. Require both
`Verification: PASS` strings, matching output-bit/reference hashes, balanced
per-core CIRA completions, nonzero coalescing, and zero rejections.

- [ ] **Step 4: Preserve evidence and commit any test-only correction**

Save commands, logs, stats paths, hashes, and the selected prefetch distance in
the run evidence JSON. If no source correction was needed, do not create an
empty commit.

### Task 8: Tune distance and rerun formal g20 acceptance

**Files:**
- Modify only generated result, table, figure, and paper artifacts after all acceptance gates pass.

- [ ] **Step 1: Run a small deterministic distance sweep**

Using the repaired two-core path, sweep a bounded set such as
`{4, 8, 16, 32, 64}` on the small graph. Reject points with bit mismatch,
inactive cores, unbalanced completions, or any drops. Select by measured ROI
ticks; do not change caches, MSHRs, CXL latency, or graph placement.

- [ ] **Step 2: Launch fresh matched formal Vanilla and CIRA runs**

Use the existing formal g20 checkpoint runner and the selected explicit
distance with `g20.sg`, two timing cores, 20 iterations, all CXL, 1 us, and the
same measured trial. Do not reuse the old CIRA result or its 165 ns profile.

- [ ] **Step 3: Verify correctness and performance evidence**

Require:

```text
Verification: PASS
reference/output bit hashes equal
issuedPerCore[i] > 0 for i in {0,1}
issuedPerCore[i] == completedPerCore[i]
droppedCsrDescriptors == 0
rejectedQueueFull == 0
coalescedPrefetches > 0
Vanilla simTicks / CIRA simTicks > 1.0
```

If the final condition fails, stop publication and report the diagnostic
statistics; do not weaken the gate.

- [ ] **Step 4: Regenerate the paper artifacts transactionally**

Run the existing evidence-gated g20 table/figure publisher. Confirm the CSV,
evidence JSON, TeX table, PDF, and SVG all carry the new CIRA result and matching
evidence digest. Build the paper and inspect the figure/table page.

- [ ] **Step 5: Run final verification and commit**

Run the focused tests, SCons build, artifact publisher validation, and paper
build once more. Inspect `git diff --check` and commit only the intended source,
tests, evidence, and publication changes with a message describing the verified
multicore CIRA repair.
