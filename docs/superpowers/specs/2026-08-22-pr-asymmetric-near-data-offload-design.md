# PR Asymmetric Near-Data Offload Design

## Status and scope

This design replaces the prefetch/load-only mechanism boundary for the formal
PageRank comparison. AMU and CIRA may execute PageRank row blocks near data,
using different system-native mechanisms. It retains the existing fail-closed
correctness, configuration, evidence, and publication gates.

The deliverable is the nine-point comparison of AMU, CIRA, and M2NDP at g12,
g14, and g20, relative to a matched Vanilla gem5 baseline. CIRA policy results
are an additional ablation. Breadth benchmarks, g4 publication results, and
latencies other than 1 us are outside this design.

The current fresh g12 diagnostic is not qualification evidence. Although its
final rank vectors are bit-exact, AMU achieved 0.0088461x and CIRA achieved
0.8079227x. The corrected topology validator does not make those performance
results publishable. No old checkpoint or raw timing is promoted into the new
evidence root.

## Fixed experimental contract

Every formal run uses the same graph bytes and numerical workload:

- GAPBS `pr_spmv` input `g12.sg`, `g14.sg`, or `g20.sg`;
- four host threads for Vanilla, AMU, and CIRA, with the same four-way row
  partition represented by the M2NDP trace;
- CSR, current-rank, and next-rank storage entirely in CXL memory;
- a 1 us CXL latency, emitted as `delay=1000000` in gem5 configuration;
- 20 synchronous PageRank iterations with double buffering; and
- the matched four-thread, all-CXL Vanilla `pr_spmv` run as the denominator.

Graph generation, graph loading, allocation, and common initial-rank setup are
outside the region of interest. They must nevertheless be identical across the
four systems and bound into evidence by content hash.

The nine formal accelerated points are AMU, CIRA Few-shot/JIT, and M2NDP at
g12, g14, and g20. Each point has a performance acceptance target of 1.4x to
1.6x, inclusive. The interval is a publication gate, not permission to clamp,
rescale, subtract time, or tune a completed result.

## Common numerical interface

The host partitions the output vertices into contiguous row blocks across four
threads. A common semantic descriptor identifies:

- CSR row-offset and neighbor/value ranges;
- current-rank and next-rank base addresses;
- the first row and number of rows;
- the iteration number; and
- the completion object for the owning worker.

The descriptor defines what must be calculated, not how a system implements
it. Each executor must visit every neighbor of a row in original CSR order,
perform the original float32 operations in that order, and write exactly one
next-rank result for that row. No executor may use cross-row reassociation,
unordered atomic reduction, fast math, fused contraction that changes the
reference bits, or a host-side functional shortcut.

At the end of every iteration, all descriptors must complete, all writes must
be globally visible, the four workers meet at a barrier, and the input/output
rank buffers swap. Iteration 19 additionally drains every outstanding memory,
coherence, and writeback operation before the ROI ends.

## System-native executors

### AMU

The host statically forms row-block descriptors. The AMU executor consumes a
descriptor, issues batched CSR and rank gathers through its modeled queues,
performs ordered per-row float32 reduction near data, and writes the result to
the CXL-resident next-rank buffer.

This removes the scalar `load_value()` wait wave without pretending that its
work is free. Descriptor formation and issue, queue occupancy, CXL requests,
device reduction, writeback, completion, iteration barriers, and final drain
are all modeled and charged. The design may use AMU-specific batching and
scratchpad organization; it need not mirror CIRA or M2NDP internals.

### CIRA

CIRA uses a coherent runtime hoist to execute the complete row-block loop at
the device. Its executor reads CSR/rank data, performs ordered per-row
reduction, and publishes results through the modeled coherence and writeback
path. It must not bypass cache ownership, invalidation, queue, or backpressure
events merely because the function is hoisted.

CIRA retains three policy candidates:

| Candidate | Row window | Lead blocks |
| --- | ---: | ---: |
| A | 64 | 1 |
| B | 2048 | 32 |
| C | 1024 | 16 |

The formal CIRA result is the fully charged Few-shot/JIT policy. Static,
PGO-selected, and the standalone candidates are policy-ablation results:

- **Static / w/o JIT:** compiler-fixed candidate A. Offline compilation is
  outside the ROI; all runtime descriptor, execution, coherence, and drain
  costs are inside it.
- **PGO-selected:** an offline profile chooses A, B, or C. The profile and
  offline choice are outside the ROI; executing the frozen choice is inside.
- **Few-shot/JIT:** candidate trials, including discarded work, selection,
  JIT/reconfiguration, steady execution, coherence, and drain are inside the
  ROI.
- **Oracle-best:** the post-run best standalone candidate. It is used only to
  report selection regret and never as a formal speedup.

All three named policies and A/B/C are measured at g12, g14, and g20. Only the
fully charged Few-shot/JIT point is subject to the formal CIRA 1.4x--1.6x gate;
the other policies must pass correctness and mechanism gates but have no
required speedup interval.

### M2NDP

M2NDP retains its four-stage PageRank kernel/trace and native scratchpad,
memory-command, execution, and synchronization paths. It implements the same
descriptor semantics and float32 order but is not wrapped in the AMU or CIRA
timing model.

The trace contains the same four disjoint row partitions assigned to the four
gem5 workers. Its launch and synchronization records preserve the work and
barrier semantics of those four partitions even when NDPSim names its physical
execution resources differently. The legacy `g20-2thread-1us` profile is not
compatible with this contract and must not be resumed or cited.

FuncSim must first reproduce the reference vector bit for bit. Only then may
NDPSim produce the M2NDP timing used against the matched gem5 Vanilla baseline.
Trace conversion, kernel execution, synchronization, and final completion
costs are included according to the existing end-to-end bridge contract.

## ROI accounting

The ROI starts immediately before the first iteration-0 descriptor formation
or scheduling action. It ends only after iteration 19 results are visible, all
four workers leave the final barrier, and every accelerator queue, coherence
transaction, and writeback is drained.

Each CIRA policy records mutually exclusive top-level wall-time stages that sum
exactly to E2E latency:

1. descriptor/hoist formation;
2. candidate sampling;
3. policy selection;
4. JIT or device reconfiguration;
5. executor operation; and
6. final drain and barrier.

Static and PGO-selected legitimately report zero for stages that occur
offline. Few-shot/JIT must report positive sampling, selection, and JIT or
reconfiguration cost. Discarded candidate trials remain charged.

Executor-internal counters separately report CSR and rank CXL waits, ordered FP
compute, queue stalls, coherence activity, writeback, useful and ineffective
hoists, and device utilization. These counters may overlap and therefore are
not presented as additive wall time. Policy figures use the additive E2E
stages; mechanism figures use the executor counters or percentages and label
them as non-additive.

AMU and M2NDP expose analogous top-level formation/issue, execution,
synchronization, and drain stages wherever their native models permit. Their
E2E totals, rather than a selected internal counter, determine speedup.

## Hardware calibration and immutability

The AMU and CIRA timing parameters are produced from the approved hardware
sources and frozen before qualification. CIRA PGO data comes from
`/root/ia780i_type2_delay_buffer_new/benchmark_gapbs_workloads_ci_long.csv`
(SHA-256
`4e0297da423cee0a742bc2e10656d022bb27776807f2d2ce4cca43e65c634184`). AMU
calibration is grounded in `/home/victoryang00/gem5-CXL/3663479.pdf`
(SHA-256
`cba178ece7593b3ede868417a031ded3efddd85d5f7c50672b0a93735187790f`). The
calibration manifest records these source hashes, extraction method, fitted
parameters, units, goodness or residual information, and generator revision.
PGO profile identity is also recorded and hashed.

Formal execution binds the graph, source tree, gem5 binary, workload binaries,
M2NDP tools, configurations, calibration manifest, and policy manifest into one
evidence identity. Once g12 qualification starts, no parameter may be adjusted
per graph, per system, or in response to observed speedup. A changed input
requires a fresh evidence root and a complete rerun beginning at g12.

Performance must arise from modeled events, queues, ports, occupancy, and
resource contention. Direct tick rewriting, analytical replacement of a live
result, uncharged host execution, removal of measured overhead, or post-hoc
scaling is rejected.

## Correctness and mechanism gates

Correctness is stricter than performance. Every executor produces raw float32
rank output. The formal final vector is compared element by element with the
matched Vanilla result and must have the same SHA-256. Small-graph tests also
compare every iteration so an early numerical divergence cannot hide behind a
final-only test.

A point also fails if any of the following occurs:

- a CSR, rank, or output range is not routed to CXL;
- the emitted gem5 latency is not exactly 1 us;
- the run does not use four threads and exactly 20 iterations;
- issued and completed requests or descriptors differ;
- a queue overflow, dropped request, pending transaction, incomplete
  coherence action, or unwritten result remains;
- a required mechanism counter is absent, inconsistent, or zero when the
  mechanism requires activity;
- CIRA Few-shot/JIT omits a charged runtime phase; or
- M2NDP NDPSim timing exists without a passing FuncSim result for the same
  trace and binary identity.

## Qualification sequence

Implementation follows test-driven development. The required progression is:

1. Unit tests for descriptor bounds, four-thread row partitioning, double
   buffering, A/B/C policy behavior, ROI-stage accounting, and fail-closed
   propagation.
2. Small-graph functional tests proving ordered float32 results for AMU, CIRA,
   and M2NDP on every iteration.
3. M2NDP FuncSim bit-exact validation followed by an NDPSim smoke run.
4. gem5 smoke runs proving four threads, 20 iterations, all-CXL placement,
   `delay=1000000`, queue completion, and final raw-vector equality.
5. A fresh g12 qualification containing Vanilla, AMU, fully charged CIRA
   Few-shot/JIT, and M2NDP. All three accelerated points must pass correctness,
   mechanism, and 1.4x--1.6x performance gates.
6. A deterministic g12 replay. The replay must reproduce output hashes,
   selected CIRA policy, and exact simulator ticks.
7. Only after both g12 runs pass, execute g14 and g20 formal points and the
   g12/g14/g20 CIRA policy ablation.

Simulator determinism makes one accepted g14 and one accepted g20 execution
sufficient after the g12 replay. A failed g12 gate blocks the larger graphs.

## Evidence and publication outputs

Each run preserves the unmodified simulator logs, stats, configurations,
commands, binaries or their hashes, raw rank vector, graph hash, policy and
calibration manifests, per-phase timing, mechanism counters, absolute E2E
latency, and recomputed speedup. Machine-readable CSV and JSON are primary;
LaTeX tables and figures are generated only from accepted machine-readable
evidence.

The publication outputs are:

- the nine-point AMU/CIRA/M2NDP g12/g14/g20 speedup comparison;
- matching absolute E2E latency data;
- a CIRA Static/PGO/Few-shot policy comparison at all three scales;
- CIRA A/B/C and oracle-regret diagnostics;
- the additive CIRA E2E stage breakdown;
- a separately labeled non-additive executor mechanism breakdown; and
- complete raw CSV/JSON plus provenance manifests.

If correctness passes but a formal speedup is outside the accepted interval,
the runner writes a diagnostic performance-hold artifact with the offending
point and complete raw evidence. It does not create the publishable completion
manifest or update paper figures. Correctness, configuration, mechanism, and
performance failures remain distinct terminal states.

## Failure and resume semantics

Every formal campaign begins in a fresh immutable evidence root. Resume is
allowed only when live code, binary, graph, configuration, calibration, policy,
and completed-point hashes exactly match the root identity. A mismatch fails
closed; it never adopts a checkpoint or timing from a different evidence root.

Failures preserve the exact command, first error, logs, partial counters, and
identity. Fixing an implementation or calibration defect creates a new root
and restarts qualification at g12. The existing pre-offload diagnostic and
topology-validator evidence remain historical diagnostics only.

## Out of scope

This design does not publish g4 as CXL-scale evidence, broaden the comparison
to other GAPBS kernels, run the 200 ns/500 ns/2 us latency sweep, alter the
Vanilla algorithm, weaken bit-exact comparison, treat the topology fix as a
performance result, or guarantee that a mechanism will pass by modifying its
measured value. Additional benchmarks and latency sweeps require separate
approved designs after this nine-point campaign is valid.
