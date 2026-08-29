# Three-Category Comparison and CIRA Policy-Overhead Design

Date: 2026-08-29

## Objective

Strengthen the CIRA evaluation beyond PageRank by publishing matched AMU,
CIRA, and M2NDP results for at least one workload from graph analytics,
gather/scatter, and scientific computing, while retaining MCF as a distinct
network-optimization case.  Add a measured breakdown that explains why CIRA
Few-shot and CIRA PGO differ.

The main latency-spectrum figure contains six workloads in this fixed order:

1. GAP PageRank on the frozen g20 graph;
2. GAP Betweenness Centrality on the same frozen g20 graph;
3. MCF on the accepted formal record;
4. Spatter AMG Gather on the accepted formal values/index record;
5. Spatter LULESH Scatter on the accepted formal values/index record; and
6. NPB CG Class D on a freshly generated, hash-bound record.

NPB MG remains valid engineering evidence but moves out of the main 2-by-3
figure.  This substitution gives the main figure two GAP workloads and one
representative from each requested category without expanding an already
dense paper figure.

## Matched-system contract

Every primary coordinate compares Vanilla CXL, AMU, CIRA, and M2NDP at 200 ns,
500 ns, 1 us, and 2 us.  Speedup is recomputed from the latency-matched Vanilla
result; no point may inherit a baseline from a different latency or workload.
Host executions use four timing cores and four software workers.  All workload
arrays and graph storage are placed behind the modeled CXL link.  Each system
uses the same input hash, dynamic-work interval, output boundary, floating-point
mode, and algorithmic iteration count for that workload.

PageRank and BC use the same accepted g20.sg and graph manifest.  PageRank keeps
its synchronous double-buffered 20-iteration contract.  BC keeps the canonical
GAP source-selection and traversal order.  CG uses the native Class D iteration
count and exact NAS data-generation seed; it is not shortened to match PageRank.
The two Spatter workloads use their accepted generated values/index files and
their existing formal dynamic-count manifests.  MCF uses its accepted formal
record and phase-local CALL windows.

The formal identity binds source, binary, input, graph, trace, window manifest,
simulator, configuration, calibration, and policy hashes.  A changed artifact
starts a new evidence root.  Existing diagnostic roots cannot be promoted.

## GAP BC M2NDP path

BC requires a new canonical trace path rather than reusing PageRank arithmetic.
The trace preserves the GAP traversal and dependency order and exposes explicit
phase barriers for source initialization, frontier discovery, dependency
accumulation, and reverse propagation.  Partitioning is allowed only across
disjoint source vertices or other ranges proven independent by the trace
contract.  Within a source traversal, parent/predecessor and floating-point
accumulation order must remain unchanged.

FuncSim verifies every declared output boundary against the matched native
reference before NDPSim timing is accepted.  NDPSim must report every expected
launch exactly once, all phase barriers, an empty outstanding-work set at the
ROI end, and a memory match.  Unsupported or unsafe BC regions fail closed;
they are never replaced with PageRank-derived timing or an analytical speedup.

## Correctness and performance gates

Primary timing points require structural trace exactness, native numerical
verification at the declared output boundary, mechanism counters, all-CXL
placement proof, and simulator identity proof.  Bit-exact comparison is used
where the preserved scalar ordering makes it available.  A numerically valid
but non-bit-exact point must record the comparison rule and maximum error in
raw data.  Missing, failed, mixed-identity, or inconclusive coordinates remain
diagnostic and cannot appear in the paper.

Performance is always observed.  There is no target-speedup fitting, clipping,
or requirement that every accelerator reach 1.5x.  Confidence intervals and
window counts accompany reconstructed results.

## PGO versus Few-shot overhead

The dedicated breakdown compares CIRA PGO and CIRA Few-shot using matched
workload, scale, latency, binary family, and ROI boundaries.  Its primary
stacked bars contain only additive costs charged to the measured execution:

- region/descriptor formation;
- online candidate sampling;
- policy selection;
- JIT or runtime reconfiguration;
- selected-policy execution; and
- final synchronization and drain.

The six components must sum exactly to the reported end-to-end phase total and
must reconcile with native gem5 time under the declared clock conversion.
PGO normally has zero online sampling, selection, and JIT components; these are
true measured zeros, not missing values.  Few-shot must charge all work for
discarded candidates as well as the selected execution.

Offline PGO profile collection and compiler specialization are one-time costs
and are not silently folded into one ROI.  A separate inset/table reports their
measured wall-clock cost, artifact identity, and the break-even invocation count
when both the one-time cost and per-invocation latency are available.  If an
authoritative collection duration is unavailable, the field is explicitly
`not-recorded` and no break-even value is claimed.  Few-shot runtime JIT stays
inside every measured ROI.

The main overhead figure uses PR g12, g14, and g20 at 1 us because those policy
variants already emit the full six-stage ledger.  BC may be added only after it
emits the identical ledger contract; it is not required to complete the first
formal breakdown.

## Data and paper products

One validated aggregate manifest generates:

- canonical CSV and JSON with all four systems and four latencies;
- a 2-by-3 latency-spectrum figure for the six fixed workloads;
- a grouped 1-us AMU/CIRA/M2NDP comparison;
- per-workload absolute-latency and speedup figures;
- a paired PGO/Few-shot additive overhead figure plus one-time-cost table; and
- LaTeX table fragments consumed by `WIP_jf_asplos.tex`.

The raw schema includes workload/category/input identity, latency, system,
absolute time, matched speedup, confidence interval, timing method, output
verification, all-CXL proof, thread count, phase ledger where applicable, and
all provenance hashes.  The paper generator accepts only a complete validated
manifest and updates the paper checkout atomically.  Code and paper are built,
committed, and pushed to their corresponding branches only after the generated
artifacts and LaTeX build pass.
