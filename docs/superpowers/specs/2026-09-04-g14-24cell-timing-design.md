# G14 24-Cell Host and CIRA Timing Design

Date: 2026-09-04

## Goal

Produce a fresh, auditable 24-cell timing bundle for six workloads at four
CXL latencies. Each cell must contain host-inline timing for the offloadable
region and CIRA device-side runtime. M2NDP collection is a separate stage and
must not prevent host or CIRA evidence from being recorded.

The matrix is:

- workloads: `pr_spmv`, `gap_bc`, `mcf`, `amg_gather`,
  `lulesh_scatter`, and `npb_cg`;
- CXL latency labels: `200ns`, `500ns`, `1us`, and `2us`.

## Input Identity

The two graph workloads use the same G14 graph:

- path: `/mnt/disk0/gem5-CXL-g14-eval/graphs/g14.sg`;
- SHA-256: `72fb08147f63112b4ea3fcff8a14b1713fdf8b097b2cf459a1ecdc217baf6524`;
- scale: 14.

PageRank uses the existing G14 graph, CSR, and build provenance only after all
recorded hashes revalidate. GAP BC is regenerated from this exact graph and
gets its own hash-bound prepared trace. No G20 PageRank or BC timing is reused.

The non-graph workloads retain their accepted formal inputs and prepared
traces:

- MCF pricing window;
- AMG Gather;
- LULESH Scatter; and
- NPB CG.

Reusing a non-graph trace means reusing only its immutable input and trace
identity. Host-inline and CIRA timing are rerun with the current binary and
gem5 model at every latency.

## Host-Inline Measurement

The `cira-inline` execution identity uses the same compiled replay binary,
trace, four-thread work partition, memory layout, warmup, and measured window
as the corresponding CIRA cell. Its accessor executes the region on the host
and emits no offload request.

Each cell records:

- `host_region_cumulative_ticks`;
- exact `host_region_cumulative_ns` using the recorded gem5 frequency;
- `host_region_entry_count`;
- zero issued and completed offload operations; and
- command, binary, configuration, trace, stats, result, and log hashes.

Each latency is executed separately with all workload memory on CXL.

## CIRA Device-Side Measurement

For generic matched-replay workloads, device runtime is the wall-clock span
from the first accepted CIRA prefetch to the last completion. The primary
metric is not the sum of per-core spans.

Each cell records:

- global first-issue tick, last-completion tick, and busy ticks;
- per-core first-issue, last-completion, and busy ticks;
- global and per-core issued/completed counts; and
- span validity and reset-outstanding checks.

PageRank descriptor fields are exported separately:

- `prComputeTicks` and `prQueueStallTicks`;
- `prComputeTicksPerCore[0..3]`;
- `prQueueStallTicksPerCore[0..3]`; and
- descriptor issued/completed counts.

For a descriptor-based PageRank cell, the CSV includes both the literal device
busy span and `max(prComputeTicksPerCore[i] +
prQueueStallTicksPerCore[i])`. For generic replay cells, descriptor metrics
are marked not applicable and must remain zero.

## Fixed-Control Replay Semantics

A prepared timing window may include a second fixed-control replay. That
component can legitimately issue no CIRA operation. Device timing is required
and validated for the dynamic offloadable region, but an inactive fixed
component is accepted only when:

- its issued and completed counts are both zero;
- its first, last, and busy ticks are zero;
- all per-core span fields and validity fields are zero; and
- no request was outstanding at reset or exit.

Nonzero partial span state remains a hard failure. Fixed-control timing is
retained for reconstruction but is not substituted for the requested dynamic
device runtime.

## Execution and Resume Model

The campaign is split into independent evidence stages:

1. host-inline;
2. CIRA runtime; and
3. M2NDP timing, when requested.

Host-inline and CIRA can reach `complete` for all 24 cells even when an M2NDP
package is missing. Each stage writes to a fresh attempt directory and commits
its evidence atomically. A completed stage is skipped on resume only after its
identity and every referenced hash revalidate.

The execution order is:

1. validate the G14 graph and all six prepared workload identities;
2. run one small host-inline/CIRA qualification pair;
3. require zero offload for host-inline and a complete dynamic device span for
   CIRA;
4. run the remaining cells in a bounded background queue;
5. publish intermediate CSV rows after each completed stage; and
6. publish the final 24-row bundle only when both host-inline and CIRA are
   complete for every coordinate.

Parallel execution is allowed because gem5 reports simulated time, but the
queue must remain bounded to avoid memory and disk exhaustion. The campaign
records failures without treating a partial attempt as complete.

## Output Contract

The fresh evidence and publication roots use `g14` in their names and never
overwrite the G20 roots. The main CSV has exactly 24 rows and includes:

- workload and latency;
- graph scale and input SHA-256;
- host cumulative ticks, nanoseconds, and entry count;
- CIRA global busy ticks and nanoseconds;
- four per-core CIRA busy-tick columns;
- four per-core issued and completed columns;
- PageRank compute/stall aggregate and per-core columns;
- the max-over-cores compute-plus-stall value; and
- evidence paths and SHA-256 hashes.

A progress CSV may contain incomplete rows, but every missing value is marked
as pending or failed rather than synthesized from old runs.

## Validation

Unit and integration checks cover:

- rejection of G20 or mismatched graph identities;
- independent host/CIRA stage completion and resume;
- host-inline entry count and zero-offload enforcement;
- active dynamic CIRA span validation;
- valid inactive fixed-control span handling;
- rejection of partial or inconsistent fixed-control span state;
- exact tick-to-time conversion;
- PageRank aggregate/per-core consistency; and
- exactly 24 final CSV rows with complete evidence hashes.

No speedup or paper figure is updated until the corresponding raw timing rows
pass this contract.
