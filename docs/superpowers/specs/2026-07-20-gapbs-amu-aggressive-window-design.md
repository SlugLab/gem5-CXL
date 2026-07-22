# GAPBS AMU Aggressive Asynchronous Window Design

## Objective

Improve GAPBS performance with a 1 us CXL link delay by increasing the number
of simultaneously outstanding AMU loads and removing blocking scalar loads
from kernel hot paths. BFS, BC, PR, and SSSP must continue to pass their GAPBS
verifiers with exactly the same computed results as their baseline binaries.

The performance experiment remains a matched comparison between the baseline
and AMU binaries using ROI work markers, one timing CPU, and the same generated
graph. Performance improvement is measured but is not allowed to weaken the
correctness gate.

## Current Problem

The generated `amu_gapbs.h` exposes `load_value()`, which issues one `aload`
and immediately polls `getfin` until that request completes. Several kernel hot
paths therefore serialize dependent-looking loads even when the addresses are
already known and the values can be gathered before computation. Existing
`load_values()` calls expose some concurrency, but kernels frequently finish a
batch and then issue scalar loads one at a time.

The generated sources currently show this problem most clearly in BC and SSSP:

- BC gathers neighbor IDs, then synchronously loads `path_counts[v]` and
  `deltas[v]` for each selected successor.
- SSSP gathers edge records, then synchronously loads `dist[wn.v]` for every
  edge. A failed compare-and-swap also reloads the distance synchronously.
- PR uses two complete batches in sequence: neighbor IDs first, then scores.
  The second batch cannot be addressed before the first completes, but score
  requests within that second stage can all be outstanding together.
- BFS already batches neighbor IDs. Its atomic parent updates remain commit
  operations and are not speculative.

## Selected Architecture

### Fixed-capacity load windows

The generated AMU helper will provide one fixed-capacity heterogeneous
`LoadWindow` whose capacity is `2 * GAPBS_AMU_BATCH_SIZE`. A window owns stable SPM
slots, AMU request IDs, per-slot element sizes, value storage, and completion
state for its entire lifetime. A single window is also a single completion
domain: requests of different value types may overlap without one typed window
accidentally consuming another typed window's completion ID.

The interface has four explicit phases:

1. `add<T>(address)` records a typed source address and returns its stable slot
   index.
2. `issue_all()` configures the element granularity and issues every recorded
   request without polling for completion between requests.
3. `wait_all()` consumes completions in any order and copies each completed SPM
   slot into the corresponding typed value slot.
4. `value<T>(slot)` returns a typed value only after `wait_all()` has completed
   and asserts that `sizeof(T)` matches the recorded slot size.

The helper rejects over-capacity additions in debug builds and does not expose
SPM pointers or request IDs to kernels. A request ID is mapped back to its slot
inside the window. Completion order therefore cannot change kernel iteration
order.

`load_values()` becomes a thin compatibility wrapper around `LoadWindow`.
`load_value()` remains available only for control-dependent retry paths where
the address or required value is not known before the preceding commit.

### Gather, issue, collect, commit

Each transformed kernel follows the same scheduling rule:

1. Gather all addresses whose calculation does not depend on an outstanding
   value.
2. Issue every load in the current dependency stage.
3. Wait for all loads in that stage and restore values to their original slots.
4. Perform computation and state-changing operations in the baseline program's
   original order.

No CAS, atomic bitmap operation, frontier insertion, distance update, or score
accumulation is performed before the required stage has completed. This is the
central bit-exactness rule.

## Kernel Transformations

### BFS

BFS retains batched neighbor-ID loads. Bottom-up early termination is preserved
at batch granularity: completions may arrive out of order, but neighbors are
examined in their original order and the first matching parent is committed.
Top-down parent CAS operations are also performed in original neighbor order.
No speculative CAS or frontier insertion is introduced.

### BC

The forward pass continues to gather neighbor IDs and commits depth, successor,
and path-count updates in original order.

The reverse dependency pass uses three stages per neighbor batch:

1. Gather neighbor IDs.
2. Identify successors using the existing successor bitmap and construct two
   aligned slots for `path_counts[v]` and `deltas[v]` in one heterogeneous
   window.
3. Issue that window once, collect all values, then update `delta_u` in original
   successor order.

Both value types share one completion domain so their requests overlap without
misrouting completion IDs. The accumulation order is unchanged to preserve
floating-point results exactly.

### PR and PR-SPMV

PR keeps its unavoidable two-stage dependency chain. The first window gathers
neighbor IDs. After those IDs arrive, the second window issues every
`outgoing_contrib[v]` request for the batch. Incoming scores are accumulated in
the original neighbor order after the complete second stage arrives. No
reassociation or vector reduction is permitted.

### SSSP

For each edge batch, SSSP first gathers edge records. It then builds one aligned
window containing the initial `dist[wn.v]` reads for all edges in the batch and
issues them together. The source distance `dist[u]` is loaded once before edge
processing because it is invariant within `RelaxEdges`.

After collection, edges are relaxed and compare-and-swap operations are issued
strictly in original edge order. If a compare-and-swap fails, the subsequent
distance reload remains a scalar `load_value()`: its required value depends on
the just-observed competing update and cannot safely be prefetched. Local-bin
resizing and insertion remain in the original commit path.

## Correctness and Failure Handling

- `count == 0` is valid and performs no AMU operations.
- A window never exceeds `GAPBS_AMU_BATCH_SIZE` entries.
- Each nonzero request ID is consumed exactly once.
- Unknown or duplicate completion IDs are treated as invariant failures in a
  debug build rather than silently corrupting a slot.
- Typed values are copied with `memcpy`; no alignment-dependent typed access to
  byte SPM storage is introduced.
- Request storage remains alive until all completions have been collected.
- OpenMP state is thread-local because each window is stack-local to the
  invoking thread and AMU configuration remains `thread_local`.
- Kernel commit order, atomic operations, and floating-point accumulation order
  must match the baseline source order.

## Testing Strategy

### Generator regression tests

Add focused Python tests that copy small representative GAPBS source fixtures,
run the patch functions, and inspect the transformed sources. Tests must prove:

- `LoadWindow<T>` issues all recorded requests before its completion loop.
- BC creates aligned path-count and delta windows without scalar loads in its
  normal reverse-pass path.
- PR retains ordered score accumulation after a batched second-stage load.
- SSSP batches initial destination distances while retaining scalar reloads
  only inside the CAS retry path.
- BFS preserves ordered commit and bottom-up early-exit behavior.

Each behavior change is developed with a failing test before generator code is
modified.

### Build and native correctness

Rebuild baseline and AMU versions of BFS, BC, PR, and SSSP from the same
CXLMemUring GAPBS checkout. Run each binary natively on the same deterministic
small graph parameters and require both the baseline and AMU binaries to report
successful GAPBS verification. Native runs exercise the functional m5op path;
they do not replace timing-model validation.

### gem5 correctness and timing

Run all four matched workloads using
`configs/example/gem5_library/x86-gapbs-amu-se.py` with:

- timing CPU, one core;
- `--cxl-memory --cxl-link-delay 1us`;
- ROI work events;
- identical graph scale and iteration count;
- identical cache and prefetcher configuration.

Every AMU run must exit successfully, report nonzero issued loads, and satisfy
`issuedLoads == completedLoads`. GAPBS verification output must indicate
success. Because GAPBS runs its verifier after the timed kernel, verification
mode dumps ROI stats at `m5_work_end` but continues simulation until the
program exits; stopping at `m5_work_end` is not valid correctness evidence.
The workload receives `-v`, and the runner requires the exact GAPBS output
`Verification: PASS`. The resulting summary records baseline ticks, AMU ticks,
speedup, AMU event counts, verifier status, and run directories for
auditability.

## Acceptance Criteria

The implementation is accepted only when all of the following hold:

1. Generator regression tests pass and demonstrate a red/green transition.
2. BFS, BC, PR, and SSSP build successfully for baseline and AMU variants.
3. All four AMU workloads produce exactly the baseline-verified result; no
   correctness tolerance or skipped verifier is allowed.
4. All gem5 AMU runs complete with issued and completed load counts equal.
5. The generated hot paths no longer call scalar `load_value()` for BC's normal
   reverse-pass values or SSSP's initial per-edge destination distances.
6. SSSP retains scalar reloads only for compare-and-swap retry dependencies.
7. A fresh 1 us CXL comparison is saved and reported for every workload.

Performance is reported honestly per workload rather than hidden behind an
average. If a workload remains slower, its AMU coverage, instruction count,
CXL traffic, and completion behavior are investigated before further changes.

## Out of Scope

- Speculative state-changing operations before load completion.
- Reordering CAS operations or floating-point accumulation.
- Changing GAPBS algorithm definitions, graph inputs, or verifier behavior.
- Claiming the current functional rewrite is equivalent to an unpublished or
  paper-only AMU microarchitecture.
- Tuning CXL delay, cache sizes, MSHRs, or queue sizes to mask a scheduling
  regression.
