# PR Scaling g4 Smoke and AMU Line-Cache Design

Date: 2026-08-16

## Goal

Repair the formal four-thread, all-CXL, 1 us PageRank scaling experiment after
the first fresh run exposed two real defects: the calibrated CIRA lead emits no
requests on small per-thread graph partitions, and the AMU transformation
amplifies each scalar value into excessive far-memory and coherent SPM traffic.

The experiment continues to execute all 16 Vanilla, AMU, CIRA, and M2NDP
points at g4, g12, g14, and g20. All 16 points must pass their full bit-exact
and mechanism gates. The publication performance gate applies only to the nine
accelerated points at g12, g14, and g20. g4 is a correctness and fixed-cost
smoke point and cannot support a CXL-scale performance claim.

The inclusive `1.4 <= speedup <= 1.6` interval is an acceptance gate, not a
target-fitting rule. The implementation may reduce real work and overlap real
independent requests, but it may not shorten calibrated latencies, inject a
speedup multiplier, clamp a result, substitute analytical timing, change the
PageRank operation order, or omit an out-of-range point.

## Evidence behind the change

The failed fresh root at
`/mnt/disk0/gem5-CXL-eval/pr-scaling-5ed1d7369b-bitexact` is diagnostic only.
Its g4 Vanilla and AMU points both passed bit-exact verification, but Vanilla
took 37,262,700 ticks and AMU took 655,464,879 ticks. AMU issued and completed
5,360 loads while producing 31,984 CXL packets. The g4 CIRA binary also passed
bit-exact verification, but every CIRA activity counter was zero.

The CIRA failure is deterministic. With four threads, g4 gives each thread
about four rows. The calibrated policy requests 32 blocks of 64 rows, or a
2,048-row lead, so `candidate >= thread_end` is always true. The same fixed
lead also exceeds g12's 1,024 rows per thread. A scale-aware cap is therefore
required for the mechanism to exist at g4 and g12.

The AMU performance failure is not a latency-constant problem. A prior g14
diagnostic took 2,019,356,289 Vanilla ticks and 558,090,910,773 AMU ticks while
issuing 3,870,880 AMU loads and 25,271,517 CXL packets. The current PR
transformation loads contiguous CSR neighbor IDs and random float scores as
individual 4-byte operations, then pays far-memory, coherent SPM, completion,
and polling costs for each value. The optimized path must reduce that dynamic
request and packet amplification before any timing parameter is reconsidered.

## Selected architecture

### Experiment contract

The formal matrix remains g4, g12, g14, and g20 by Vanilla, AMU, CIRA, and
M2NDP. Every point uses four timing cores, four OpenMP workers, 20 synchronous
double-buffered float32 PageRank iterations, complete trial-0 warmup on CXL,
trial-1 timing, no fast math, no floating-point contraction, and exactly 1 us
of modeled CXL link latency. The graph and every workload allocation remain in
the all-CXL range.

The runner defines correctness scales `(4, 12, 14, 20)` and performance scales
`(12, 14, 20)`. It still records the exact g4 absolute times and speedups, but
g4 cannot appear in `performance_gate.offenders`. Publication requires 16/16
correctness-passed points and 9/9 performance-passed accelerated points. A
correctness-complete result outside the nine-point interval writes
`performance-hold.json` and never writes `complete.json`.

### Scale-aware CIRA lead

The hardware-calibrated 32-block, 2,048-row lead remains the maximum policy
distance. The builder derives and records an effective distance from the exact
graph scale and four-thread partition:

1. Compute the minimum rows assigned to any participating thread.
2. If that span is at least two 64-row blocks, use the largest 64-row-aligned
   distance no greater than both 2,048 rows and half the thread span.
3. If the span is smaller than two blocks, use a small-graph correctness
   fallback of at least one future row and at most half the thread span.
4. Never cross the current thread's row interval.

For the frozen scales, this yields a one-row g4 correctness fallback, 512 rows
for g12, and the calibrated 2,048 rows for g14 and g20. The manifest records
the calibrated maximum, effective rows, effective blocks when block-aligned,
derivation inputs, and whether the correctness fallback was used.

All CIRA requests retain their existing coherent L2 destination, bounded
queues, timing CSR index traversal, and per-core accounting. g4 must issue and
complete nonzero work on all four cores even though its speedup is not gated.
At g12, g14, and g20, issued/completed vectors must match on all four cores and
all rejection, drop, translation, and queue-overflow counters must remain zero.

### AMU SPM budget

The formal AMU continues to expose 64 KiB of SPM. The software statically
partitions it across four threads, giving each thread 16 KiB:

- 8 KiB for 128 aligned 64-byte in-flight staging slots; and
- 8 KiB for 128 aligned 64-byte persistent cache-line slots.

Compile-time assertions bind the total data-slot footprint to 64 KiB. Tags,
valid bits, request IDs, and logical-order metadata are ordinary control
metadata and are reported separately. The transformation cannot silently
increase SPM capacity or allocate a second uncharged data store.

The line cache is per thread and direct-mapped by aligned far-memory line
address. A miss loads the complete 64-byte line through `amu_aload`; duplicate
addresses in the same batch share one request. Misses first land in staging so
two in-flight lines mapping to the same persistent slot cannot corrupt one
another. On completion, a line is copied into its selected persistent slot and
the tag becomes valid. Cache hits extract the requested 4-byte NodeID or score
without a new AMU request.

CSR and score lines share the same cache and have full-address tags. The cache
is invalidated at the start of every PageRank iteration because
`outgoing_contrib` changes between iterations. This conservative boundary also
prevents stale aliases between graph and score arrays. No cache hit is allowed
until the supplying AMU request has completed.

### AMU request and commit data flow

The optimized PR pull loop retains a monotonically ordered logical stream of
neighbors. Each batch carries the original neighbor positions, source
addresses, and output positions.

1. Align CSR NodeID addresses to 64-byte lines, satisfy cache hits, coalesce
   duplicate misses, and issue one AMU request per distinct missing line.
2. After the required NodeID lines complete, derive the corresponding
   `outgoing_contrib` addresses without changing logical neighbor order.
3. Align score addresses, satisfy hits, coalesce duplicate misses, and issue
   one AMU request per distinct missing score line.
4. While score lines for batch N are in flight, prepare and issue independent
   CSR lines for batch N+1 within the remaining staging credits.
5. Extract scores into logical batch positions only after their lines are
   ready, then execute the original scalar `incoming_total += score` sequence
   in stored neighbor order.
6. Drain all outstanding lines before the iteration swaps PageRank buffers and
   invalidates the line cache.

The completion path uses bounded slot metadata rather than a dynamically
growing deferred-completion vector. Waiting is permitted only for a true
dependency, a full staging window, or final drain. There is no scalar
`load_value()` call in formal `pr_spmv`, and no per-value `wait_all()` boundary.

This optimization changes memory request granularity and overlap only. It does
not reorder additions, deduplicate logical neighbors, reuse a score across an
iteration boundary, fuse operations, or perform near-memory computation.

### M2NDP

M2NDP is unchanged. Its FuncSim output must remain elementwise bit-exact, its
calibration residual must stay within one link cycle, and its NDPSim time must
remain a measured full end-to-end point. M2NDP is not tuned from AMU or CIRA
results.

## Qualification and formal execution

The old failed root is never resumed as formal evidence because code,
variants, checkpoint identity, and policy manifests change.

Before launching the full matrix, a fresh g12 qualification root runs matched
Vanilla, AMU, and CIRA with the frozen g12 graph. It must prove:

- identical final raw vectors and `Verification: PASS`;
- all-CXL `delay=1000000`, four timing cores, and four workers;
- AMU issued/completed balance, zero errors, nonzero line-cache hits, nonzero
  miss coalescing, and a packet count consistent with distinct line misses;
- CIRA activity and completion on all four cores with zero errors; and
- independently recomputed AMU and CIRA speedups within 1.4x to 1.6x.

If g12 is correct but misses the performance interval, qualification writes a
performance hold with raw data and stops before g14/g20. The implementation is
then diagnosed from real counters; parameters are not fitted to the target.

Only a passed g12 qualification permits a brand-new formal evidence root. The
formal runner executes all 16 points and applies the nine-point performance
gate after 16/16 correctness. Figures and tables consume only terminal
`complete.json`.

## State, provenance, and failure behavior

The evidence identity binds the code digest, graph set, scale-derived CIRA
policy, AMU SPM layout, binary hashes, calibration manifest, gem5, `libm5.a`,
configuration, and checkpoint-save contract. Any change requires a fresh
checkpoint and evidence root.

The runner fails closed on malformed scale policy, SPM budget overflow,
in-flight cache-slot reuse, stale line consumption, logical-order mismatch,
raw-bit mismatch, missing per-core CIRA activity, AMU/CIRA issue-completion
imbalance, queue rejection, translation error, non-1-us delay, wrong core or
thread count, or non-CXL placement. It preserves commands, manifests, configs,
stats, logs, raw vectors, and the terminal reason.

Correctness failure, qualification performance hold, and final performance
hold are separate states. None creates publication artifacts.

## Tests

Tests are written before implementation and cover:

- the performance gate checks exactly nine g12/g14/g20 accelerator points;
- arbitrarily low or high g4 speedup remains diagnostic when all g4
  correctness and mechanism checks pass;
- 1.4x and 1.6x remain inclusive bounds for the nine formal points;
- CIRA lead derivation yields one row, 512 rows, 2,048 rows, and 2,048 rows for
  g4, g12, g14, and g20 respectively;
- CIRA never crosses a thread partition and records fallback provenance;
- the generated AMU header statically fits staging plus cache data in 64 KiB;
- a batch issues one AMU request per distinct missing 64-byte line;
- two logical values on one line preserve separate logical positions;
- colliding persistent slots cannot overwrite in-flight data;
- cache hits avoid new AMU requests, while iteration reset forces fresh score
  data;
- completion order does not alter logical extraction or float32 accumulation
  order;
- formal PR contains no scalar `load_value()` or per-value wait;
- corrupt tags, early consumption, budget overflow, and unmatched completions
  fail; and
- existing AMU, CIRA, M2NDP, cross-system, checkpoint, calibration, and
  publication suites remain green.

Live verification includes a g4 four-core bit-exact smoke run, the g12
qualification, a successful incremental gem5 build, `git diff --check`, and
fresh hash comparison of every result vector. Completion is not claimed until
all required gates pass.

## Alternatives rejected

An ASMC-internal multi-subscriber line coalescer was rejected for this repair.
It is more general, but it expands coherence and completion state across all
AMU workloads when the observed amplification originates in the PR software
transformation.

Reverse-fitting modeled latency or queue capacity to produce 1.5x was rejected
because the calibration sources do not provide a PageRank-specific 1.5x
target. Analytical timing substitution and speedup clamping were rejected for
the same reason.

Dropping g4 entirely was rejected because it remains useful for fast
bit-exact, four-core mechanism validation. Treating g4 as performance evidence
was rejected because 16 vertices cannot support a CXL-scale conclusion.

## Out of scope

This change does not alter the frozen graphs, PageRank algorithm, iteration
count, floating-point order, common CPU/cache hierarchy, M2NDP implementation,
hardware calibration sources, workload-breadth experiment, or paper figures.
Paper integration begins only after the new formal evidence passes.
