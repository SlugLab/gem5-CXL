# G14 Real-CXL AMU/CIRA/M2NDP Evaluation Design

Date: 2026-08-10

## Goal

Replace the cache-resident g4 sensitivity result with a matched PageRank
evaluation whose measured Vanilla denominator performs real demand reads
through the modeled CXL link. The final publication matrix compares Vanilla
CXL, AMU, coherent CIRA, and M2NDP at 200 ns, 500 ns, 1 us, and 2 us with four
gem5 timing cores, four OpenMP threads, 20 synchronous double-buffered
iterations, and raw bit-exact output validation.

Scale 12 is a qualification workload only. Scale 14 is the fixed publication
workload. No g12 latency or speedup may be promoted into the paper's g14
matrix.

## Why the G4 Result Is Invalid as a CXL Performance Baseline

The current formal g4 profile has 16 nodes and 134 directed edges. Its 689-byte
serialized graph and working vectors fit easily in each 256 KiB private L2.
After trial 0, the measured trial-1 Vanilla ROI reports zero memory-controller
reads and writes at every modeled link latency. The nearly flat Vanilla
latency therefore measures a cache-hot CPU computation, even though the
physical memory controller is correctly connected behind the CXL link.

The existing evidence gate checks topology, configured delay, membus packet
cells, and positive ROI ticks. It does not require a demand read to reach the
memory controller. Coherence upgrades and other bus traffic can consequently
satisfy the packet gate while `mem_ctrl.readReqs`, `readBursts`, and
`bytesReadSys` are zero.

The mechanisms are also asymmetric in the current g4 run. AMU flushes source
cache lines before each asynchronous load and therefore forces thousands of
memory transactions that Vanilla does not execute. CIRA uses a one-row lead
on four-node per-thread partitions; its 1 us result records zero useful, 60
late, and 2,160 coalesced candidates. M2NDP is then normalized to the invalid
cache-hot Vanilla denominator.

The old g4 artifacts remain preserved as correctness and small-workload
sensitivity evidence, but they are not a performance baseline and are
replaced in the paper only after the g14 matrix passes every gate in this
design.

## Selected Evaluation Structure

The workflow has two sequential phases.

### G12 qualification

Run one 1 us qualification for Vanilla, AMU, and CIRA on a fixed serialized
g12 graph. This phase validates the repaired measurement and mechanism paths
before spending time on g14 or NDPSim. It is not a parameter-search result and
is not published.

Qualification must prove:

- Vanilla performs nonzero CPU-data memory-controller reads and transfers
  nonzero bytes during the measured ROI;
- AMU uses the same initial cache policy as Vanilla, issues coherent loads,
  and balances every accepted load with one completion;
- CIRA issues from all four cores, drops no descriptors or line requests, and
  records nonzero useful prefetches with more useful than late at 1 us;
- all three gem5 result vectors are raw bit-exact; and
- all topology, checkpoint, thread, graph, and link-delay identities match.

If g12 is still cache-resident after warmup, that is not a failure of the g14
design. The implementation and correctness gates must still pass, but the
workflow proceeds to a single g14/1 us proof rather than weakening the CXL
traffic gate or publishing g12.

### G14 formal sweep

After qualification, generate one deterministic g14 graph, record its
SHA-256, node count, directed-edge count, generator command, and generator
binary hash, and freeze that file for all mechanisms and latencies. Every run
loads the serialized file with `-f`; no mechanism regenerates its own graph.

Run exactly one Vanilla, AMU, CIRA, and M2NDP row at each of 200 ns, 500 ns,
1 us, and 2 us. This produces a 16-row formal matrix. A failed or incomplete
row prevents publication of the aggregate table and figure.

## Fixed G14 Contract

- benchmark: `pr_spmv`;
- graph scale: 14, from one fixed serialized graph and pinned SHA-256;
- host cores: four gem5 TimingSimpleCPU cores;
- application threads: four, with `OMP_NUM_THREADS=4`;
- schedule: static for the pull loop;
- trials: two;
- measured trial: trial 1;
- checkpoint: saved immediately before trial 0 begins;
- warmup: trial 0 re-executes completely after restore through the selected
  CXL/cache topology;
- PageRank iterations per trial: exactly 20;
- algorithm: synchronous double buffering with the same neighbor order;
- arithmetic: float32, contraction disabled, fast math disabled;
- memory placement: the complete 4 GiB physical range behind the modeled CXL
  link, with no direct memory-controller path;
- latencies: 200 ns, 500 ns, 1 us, and 2 us;
- cache hierarchy, CPU frequency, hardware prefetchers, MSHRs, graph,
  checkpoint semantics, and verification boundary matched across Vanilla,
  AMU, and CIRA.

All mechanisms may warm the cache naturally by executing trial 0. No
mechanism may add a source-line flush, artificial eviction, cache-size change,
or uncacheable mapping that is absent from the matched Vanilla execution.

## Symmetric Vanilla and AMU Memory Semantics

Remove AMU's per-request source `clflush` and its mechanism-only graph-page
priming from the formal path. The coherent ASMC implementation reads the
current cache-coherent value and writes the completion through its coherent
I/O cache. It must therefore observe dirty host data without forcing a
writeback solely for AMU.

AMU retains its asynchronous request, scratchpad destination, completion-ID,
and bounded-window semantics. The PageRank rewrite may batch independent
neighbor-index reads and then batch the dependent score reads, but it must
preserve the original neighbor order when accumulating float32 values. The
formal implementation must not call the single-request `load_value()` path in
the pull loop and must not wait after each individual request. Waiting once
per dependency stage of a bounded batch is allowed.

Vanilla and AMU begin measured trial 1 from their naturally warmed trial-0
states. Differences after that boundary arise from their execution
mechanisms, not explicit cache invalidation.

## Timing-Faithful Coherent CIRA

CIRA remains a coherent prefetch sidecar: the host executes the same PageRank
instructions and floating-point additions, while CIRA prepares future CSR and
value lines in the issuing thread's private L2.

The current fixed one-row lead is replaced by a rolling block window. Software
submits a future block only at deterministic block boundaries within each
thread's static partition. The row-block size is fixed at 64 rows. At 1 us,
the qualification phase tests the ordered lead set `{1, 2, 4, 8}` blocks and
selects the first passing value. For the formal latency sweep, the selected
1 us lead is scaled as
`max(1, ceil(selected_1us_lead * latency_ns / 1000))` whole blocks and clamped
only to the issuing thread's remaining partition. The g14 runner records the
formula, block size, selected lead, and generated binary hash for every
latency.

The allowed qualification rule is:

1. use the 64-row block size for all four latency points;
2. choose the smallest lead from `{1, 2, 4, 8}` that makes useful
   prefetches exceed late prefetches at g12/1 us without queue rejection; and
3. scale that lead in proportion to configured link latency, rounding upward
   to a whole block and clamping only to the thread's remaining partition.

If g12 is cache-resident and no candidate can exercise real CXL demand, the
same qualification grid moves to a separate g14/1 us pre-formal run. That run
selects from useful/late and queue evidence only; it does not calculate or
inspect speedup and cannot be promoted into the formal matrix. No formal g14
speedup may be used to select or revise the lead. If the frozen policy does
not accelerate the later g14 sweep, the result is reported rather than
retuned against the formal output.

CSR index generation must no longer use an instantaneous functional
`PortProxy` read in the timing path. The CIRA model uses a bounded timing state
machine for record/index reads and accounts for the near-memory traversal
latency before issuing coherent soft-prefetch packets. Functional access may
remain only for checkpoint/debug inspection outside timed requests. Returned
prefetch lines continue to install through the target core's private-L2 port,
and per-core probes attribute useful and late demand only to that same core.

Descriptor, traversal, packet, retry, outstanding, and completed-line queues
remain bounded. Any queue rejection, dropped descriptor, ownership mismatch,
or incomplete request invalidates the formal row.

## M2NDP Matching

M2NDP consumes a trace generated from the same frozen g14 graph and the same
two-trial, fixed-20 PageRank contract. The four-stage kernel sequence retains
the exact gem5 float32 accumulation order. FuncSim compares every output
element against the raw gem5 Vanilla reference before NDPSim timing is
accepted.

Each latency receives a fresh CXL-boundary calibration. The measured M2NDP
boundary must differ from the corresponding gem5 microprobe by no more than
one 0.125 ns link period. NDPSim latency covers the complete measured trial-1
kernel sequence; graph conversion, trace generation, FuncSim, calibration
search, and validation are excluded.

M2NDP is compared with the four-core Vanilla host but retains its native
internal NDP topology. Artificially changing NDP units to four would alter the
architecture and is outside this evaluation.

## Real-CXL Evidence Gates

The result pipeline adds memory-controller evidence from the first, measured
ROI statistics section. For each Vanilla g14 row it requires:

- `board.memory.mem_ctrl.readReqs > 0`;
- `board.memory.mem_ctrl.readBursts > 0`;
- `board.memory.mem_ctrl.bytesReadSys > 0`;
- nonzero aggregate `requestorReadAccesses` from CPU `.data` requestors;
- exact configured CXL delay and full-range CXL-to-controller topology; and
- no direct membus-to-memory-controller connection.

Membus packet cells remain useful traffic diagnostics but cannot satisfy the
real-CXL gate by themselves. Instruction-only reads, writebacks without data
reads, coherence upgrades, and post-ROI verification traffic do not satisfy
the gate.

Across the four Vanilla rows, CPU-data read counts and bytes should be
identical or explainably close because only latency changes. Vanilla ROI
latency must increase from 200 ns to 2 us. The validator records the absolute
increase and rejects a flat or reversed endpoint. It does not require every
adjacent pair to be strictly ordered because timing overlap can produce small
local variation.

Mechanism-specific gates are:

- AMU: positive issued loads, issued equals completed, no queue loss, no
  source-line flush in the formal binary, and coherent dirty-data tests pass;
- CIRA: positive CSR descriptors and completions, all four target cores issue
  and complete, useful prefetches are nonzero, useful exceeds late at the
  1 us qualification gate, no queue rejection/drop, and timing traversal is
  enabled;
- M2NDP: strict FuncSim mismatch count zero, complete expected launch sequence,
  positive measured cycles, and calibration residual within one link period.

These gates prove that a mechanism is exercised faithfully; they do not
manufacture a speedup requirement. Correct but slower g14 results remain valid
and must not be hidden or retuned after inspection.

## Correctness and Bit-Exact Gate

Vanilla gem5 produces the canonical g14 float32 result vector. AMU, CIRA,
M2NDP FuncSim, and the final M2NDP output must match it element by element and
as raw bytes. Every row records the vector length and SHA-256. A tolerance-only
GAPBS verification pass is necessary but insufficient.

The rewrite may change request timing and batching only. It cannot change:

- the 20-iteration count;
- synchronous double-buffer boundaries;
- neighbor traversal order;
- per-node float32 addition order;
- damping/base-score expressions; or
- thread ownership of output nodes.

Any mismatch prevents timing publication even if the mechanism is faster.

## Execution and Storage

The root filesystem currently has insufficient free space for a new build or
result tree. No old checkpoints or results are deleted. New generated inputs,
checkpoints, traces, simulator logs, and publication staging files live under:

```
/mnt/disk0/gem5-CXL-g14-eval/
```

The filesystem has approximately 485 GiB available at design time. A stable
symlink under this worktree's `m5out/` points to the external run root so
existing scripts can retain repository-relative discovery. Manifests record
both resolved absolute paths and hashes; a missing or retargeted symlink fails
closed.

The runner checks free space before graph generation and before each formal
latency. It requires at least 100 GiB free on `/mnt/disk0` before starting the
formal sweep. It never falls back to the nearly full root filesystem.

Runs execute sequentially as a resumable low-priority systemd service. Resume
accepts a stage only when all input, command, binary, graph, config, and output
hashes match. The design does not use periodic live checkpointing and does not
modify, delete, restart, or reuse the older g20 or g4 result trees.

## Output and Publication

The canonical g14 publication contains:

- a 16-row unrounded CSV with absolute latency, speedup, real-CXL counters,
  mechanism activity, and provenance;
- a JSON evidence bundle hashing the graph, binaries, checkpoints, raw result
  vectors, M2NDP trace/configuration, calibrations, and source summaries;
- a table-first TeX artifact reporting ROI microseconds and speedup;
- a PDF/SVG latency-sensitivity figure generated from the canonical CSV; and
- an independent validation report that recomputes all 16 speedups and audits
  every real-CXL and bit-exact gate.

At each latency:

```
speedup = matched_g14_vanilla_trial1_seconds / mechanism_trial1_seconds
```

No g4, g12, g20, other-latency, or stale Vanilla result may be used as a
denominator. The existing paper table and figure are replaced atomically only
after all 16 g14 rows pass.

## Test Strategy

Implementation follows red-green-refactor.

Unit and static tests cover:

- g12 qualification and g14 formal profile identity;
- deterministic graph command/hash pinning;
- rejection of zero memory-controller reads, bursts, bytes, or CPU-data
  requestor accesses;
- rejection of flat 200 ns versus 2 us Vanilla endpoints;
- exclusion of post-ROI stats sections;
- rejection of AMU formal sources containing per-request source `clflush`;
- AMU batch-stage issue/completion and original-order accumulation;
- CIRA rolling-window ownership, frozen lead calculation, timing CSR reads,
  bounded queues, and four-core usefulness attribution;
- M2NDP g14 trace length, two-trial marker sequence, FuncSim raw equality, and
  per-latency calibration binding;
- 16-row matrix completeness, matched denominator selection, atomic
  publication, and independent Decimal recomputation; and
- unchanged behavior for existing g20 and preserved g4 evidence parsers.

Integration proceeds in this order:

1. coherent ASMC dirty-source microtest without source flushing;
2. multicore CIRA timing-CSR microtest with useful and late attribution;
3. g12/1 us Vanilla, AMU, and CIRA qualification;
4. one g14/1 us Vanilla proof of real CPU-data CXL reads;
5. one g14/1 us AMU/CIRA bit-exact and activity proof;
6. g14 FuncSim bit-exact proof and M2NDP timing smoke;
7. full four-latency, four-mechanism sweep; and
8. independent result and paper-build verification.

## Failure Handling

- Zero Vanilla CPU-data memory reads is a measurement failure, not a zero-CXL
  result.
- AMU source flushing in the formal binary is a build/evidence failure.
- CIRA functional index reads in timing mode, queue drops, inactive cores, or
  zero useful prefetches at qualification are implementation failures.
- A raw result mismatch blocks performance publication immediately.
- A failed M2NDP calibration blocks only that latency's row but prevents the
  aggregate matrix from publishing.
- Insufficient `/mnt/disk0` space stops before launching the next stage and
  preserves completed evidence.
- A performance slowdown after all fidelity and correctness gates pass is a
  valid negative result. It is not repaired by changing the baseline,
  shrinking caches, injecting asymmetric flushes, or selecting a different
  graph after seeing g14 timing.

## Acceptance Criteria

The work is complete only when:

1. g12/1 us qualification passes the implementation, real-CXL, multicore, and
   raw bit-exact gates, or is explicitly recorded as cache-resident before a
   passing g14/1 us proof;
2. the fixed g14 graph identity and generation provenance are recorded and
   reused by all rows;
3. every g14 Vanilla row records real CPU-data reads and bytes at the memory
   controller during measured trial 1;
4. g14 Vanilla 2 us is slower than g14 Vanilla 200 ns;
5. AMU has no source-flush asymmetry and all requests complete coherently;
6. CIRA uses timing CSR traversal, all four cores are active, queues do not
   drop work, and the frozen policy records useful/late evidence;
7. all four M2NDP calibrations are within one 0.125 ns link period;
8. all 16 result vectors share the exact canonical raw SHA-256 with zero
   mismatched elements;
9. all 16 speedups independently recompute from the same-latency g14 Vanilla
   row;
10. the full unit/integration suite and two clean paper PDF builds pass;
11. the final CSV, JSON, TeX, PDF, and SVG are generated atomically and
    visually inspected; and
12. the gem5 branch and paper repository are committed and pushed only after
    the verified g14 artifacts replace the invalid performance claim.

## Out of Scope

- deleting or modifying existing g4/g20 outputs or live checkpoint trees;
- using cache flushes, reduced cache capacity, or uncacheable mappings to force
  a desired speedup;
- selecting a graph or CIRA lead after inspecting formal g14 speedups;
- changing PageRank arithmetic or accepting tolerance-only output equality;
- including graph generation, checkpoint construction, FuncSim, calibration,
  or validation wall time in the ROI; and
- claiming that passing fidelity gates guarantees a speedup.
