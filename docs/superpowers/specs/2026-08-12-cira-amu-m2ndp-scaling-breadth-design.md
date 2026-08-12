# CIRA, AMU, and M2NDP Scaling and Workload-Breadth Design

Date: 2026-08-12

## Goal

Replace the scale-4-only related-work comparison with defensible evidence for
CIRA, AMU, and M2NDP at CXL scale, then broaden the comparison beyond one
PageRank kernel. The resulting paper artifact has two panels:

1. full end-to-end PageRank scaling at g4, g12, g14, and g20; and
2. a calibrated, trace-driven comparison across PR-SpMV, MCF, Spatter gather,
   Spatter scatter, NPB CG, and NPB MG.

Correctness is a publication hard gate. Every accelerated execution must
reproduce the canonical result element for element and bit for bit before its
timing can enter a table, figure, or speedup claim. Calibration changes timing
parameters only; it may not copy a published speedup or change dynamic work,
operation order, memory placement, or result bits.

The design intentionally distinguishes two evidence types. The scaling panel
uses complete gem5/NDPSim executions. The breadth panel reconstructs complete
latency from deterministic, paired timing windows plus explicitly charged
fixed costs. The paper must label that boundary rather than presenting both
panels as if they used the same timing method.

## Motivation and claim boundary

The current Figure 6 uses g4, which contains only 16 vertices. That run is
useful for mechanism correctness and link-latency sensitivity, but it cannot
support a CXL-scale performance conclusion. The replacement includes g20,
which has 2^20 vertices and is the approximately 240 MB GAPBS input described
by the paper. The g4 point remains in the scaling curve only to show where
fixed costs dominate.

The existing comparison also covers only matched PR-SpMV. The new breadth
panel samples six representative regions from all workload categories named
in the evaluation: graph/pointer traversal, network optimization, irregular
gather/scatter, and scientific bandwidth-intensive computation. Claims remain
limited to these six matched regions and do not imply suite-wide coverage.

## Selected approach

The selected hybrid approach combines:

- full simulation for the four-scale PR-SpMV curve, where the complete g20
  execution is necessary to answer the scale concern directly; and
- deterministic stratified timing for the six-workload breadth panel, where
  full functional execution remains mandatory but full cycle simulation of
  every large input would be impractical.

A g4-only extension was rejected because it would preserve the reviewer's
scale concern. A purely analytical comparison was rejected because it would
not exercise the implemented queueing, coherence, completion, and NDP timing
paths. Full cycle simulation of all six 12.8 GB-class workloads was rejected
as unnecessary when paired sampling can meet a predeclared uncertainty gate.

## Common experiment contract

All formal rows use:

- modeled CXL link latency: exactly 1 us (`delay=1000000` gem5 ticks);
- host CPU: four gem5 timing cores;
- host software: exactly four worker threads;
- placement: graph, indices, values, intermediate arrays, and result arrays
  entirely in CXL-attached memory;
- identical CPU frequency, cache hierarchy, hardware prefetchers, MSHRs,
  memory size, and compiler floating-point flags for Vanilla, AMU, and CIRA;
- no fast math and no floating-point contraction;
- one frozen input and one canonical dynamic-work trace per experiment point;
- Vanilla CXL as the matched 1.0x denominator; and
- complete mechanism drain, final synchronization, and result commit inside
  the timed end boundary.

Generated gem5 `config.ini` files must be parsed after each run. A row fails
if its delay does not equal 1,000,000 ticks, it has fewer or more than four
timing cores, any workload allocation falls outside the CXL range, or an
unmatched cache/memory parameter is detected.

M2NDP uses one complete device with its native internal topology. Four host
threads describe the matched Vanilla denominator and the host components of
AMU/CIRA; they do not reduce M2NDP to four internal execution units.

## Frozen inputs

Before functional execution, the collector creates an input manifest with
absolute path, file size, semantic parameters, and SHA-256 for every graph,
index array, binary input, class file, benchmark source, compiler, and binary.
The manifest is immutable after the first timing stage begins.

### PR-SpMV scaling inputs

The curve uses one serialized GAPBS graph at each scale:

- g4: 2^4 vertices;
- g12: 2^12 vertices;
- g14: 2^14 vertices; and
- g20: 2^20 vertices, approximately 240 MB.

The existing g4 graph hash
`f234532690f6cfc30e993c4d9a1839e65002a618e7da20ea6a4242818b9c6c3d`
and g20 graph hash
`ce900a7147a073835a7450e8f1afedf9f13db6833652bf2f9647819be26bedb3`
are required. The collector resolves and freezes the current g12 and g14
serialized graphs before any formal run. It must load those files directly;
it may not regenerate a graph through `-g` after selection. Missing files,
hash drift, scale/header disagreement, or malformed CSR fails the point.

### Breadth inputs

The right panel uses the exact evaluated inputs behind the paper's workload
table:

- PR-SpMV: the same g20 graph as the scaling panel;
- MCF: the 345 MB network-optimization input used by the paper;
- Spatter Gather: the existing 1 GB AMG gather input and index stream;
- Spatter Scatter: the existing 1 GB LULESH scatter input and index stream;
- NPB CG: the paper's 12.8 GB-class CG configuration; and
- NPB MG: the paper's 12.8 GB-class MG configuration.

For NPB, “12.8 GB” must resolve to a concrete class/parameter file and a
measured allocated-byte count. Both become manifest fields. If the current
paper artifacts cannot identify such a CG or MG input, that workload is
terminally `failed_input`; the runner must not substitute class A/B/C or any
smaller convenient input. The same fail-closed rule applies if the 345 MB MCF
or 1 GB Spatter source input cannot be bound to a concrete hash.

## Canonical trace and data flow

Each workload has a reference adapter with three outputs:

1. a canonical operation trace containing phase, work-item identity,
   addresses, access widths, guards, and commit/reduction order;
2. a complete expected-output image containing raw element bits at every
   required correctness boundary; and
3. a dynamic-work manifest containing phase invocation counts, work-item
   counts, launch/barrier counts, and fixed-runtime events.

The trace is generated by an unaccelerated reference execution over the
frozen real input. CIRA, AMU, and M2NDP consume the same trace contract rather
than independently discovering or simplifying work. A trace re-read check
must reproduce the reference image before any accelerated backend is allowed
to run.

The trace schema is backend-neutral. Backend adapters may translate it to
gem5 markers, AMU operations, CIRA descriptors, or M2NDP launches, but the
translator records a source-trace hash and fails if it drops, duplicates,
reorders, fuses, or changes a work item that affects canonical semantics.

## Matched kernels and exactness boundaries

### PR-SpMV

PR uses synchronous pull PageRank for exactly 20 iterations. Trial 0 executes
the complete initialization and 20-iteration warmup from CXL memory. Trial 1
then reinitializes state and is the measured execution. Both trials use
synchronous double buffering, stored CSR neighbor order, scalar float32
addition, separate multiply and add, and no early convergence. The complete
rank-vector raw bits after every trial-1 iteration and at final completion are
checked against Vanilla.

### MCF

MCF covers `pricing_kernel` and `price_out_impl`. Candidate reads and pure
candidate computation may overlap, but flow, cost, potential, and tree
updates commit in original program order. The verifier compares the objective
and every flow, cost, potential, predecessor, depth, orientation, and tree
array element required to resume the reference program. The two hotspot
latencies are reconstructed separately and combined using invocation and
dynamic-work counts from the complete reference execution; equal weighting
or hotspot-only weighting is forbidden.

### Spatter Gather

The gather region uses the real AMG trace. Every destination element reads
the same index and value stream in the same order. Exactness compares the
entire gathered destination array, not a checksum or sample.

### Spatter Scatter

The scatter region uses the real LULESH trace. Writes retain canonical order,
including duplicate destination indices; last-writer or read-modify-write
behavior must therefore match the reference. Exactness compares the complete
destination image.

### NPB CG

CG preserves sparse-row traversal, vector-update order, and a fixed reduction
tree across all systems. Reassociation, FMA contraction, and vector reductions
with a different tree are forbidden. The verifier compares every iterative
vector, residual, and final zeta value bit for bit, then also requires the NPB
official verifier to pass.

### NPB MG

MG preserves per-grid-point operation order, restriction/prolongation order,
boundary handling, level transitions, and the fixed norm-reduction tree. The
verifier compares every grid level, residual image, and norm boundary bit for
bit, then requires the NPB official verifier to pass.

## Backend boundaries

### Vanilla

Vanilla performs the canonical CPU work with demand accesses over the common
CXL memory hierarchy. It supplies the latency denominator and the raw-bit
reference for all gem5 backends.

### CIRA

CIRA performs coherent multicore prefetch under the common four-core
configuration: descriptors may discover and fetch future
cache lines, and responses install in the requesting core's private L2. The
CPU retains canonical compute and commit order. CIRA may not perform an
uncharged value computation, skip a demand-probe/coherence action, or use
future trace knowledge outside the selected prefetch window.

### AMU

AMU uses asynchronous issue, completion, and SPM-backed buffering. Reads or
stores may overlap only when the trace declares them independent. Values are
restored to canonical slots and commit in reference order. Queue-credit,
cache-line, SPM, pending-state, and completion limits are live modeled
capacities. A per-request synchronous `load_value()` drain, queue overbooking,
or silent retry is a mechanism failure rather than a tunable shortcut.

### M2NDP

M2NDP may execute address generation and computation near memory, but its
kernel sequence must reproduce the same element order, write order, and
reduction tree. FuncSim runs the complete functional trace and performs raw
bit comparison before NDPSim timing. Tolerance-based float comparison, fused
operations, or a kernel that precomputes reference answers is rejected.

## Calibration authorities

Calibration is performed before formal collection and bound into a single
machine-readable manifest.

### CIRA authority

CIRA uses the relevant real IA-780I measurements under `/root/ia780*`, with
the GAPBS PGO source fixed to:

`/root/ia780i_type2_delay_buffer_new/benchmark_gapbs_workloads_ci_long.csv`

and the Spatter source fixed to:

`/root/ia780i_type2_delay_buffer_new/benchmark_spatter_workloads_ci_long.csv`.

Where an exact MCF, CG, or MG hardware ROI is available, its raw log, input
hash, trial rows, and verification status are required. Calibration transfers
mechanism costs and policy behavior only between structurally matched regions;
it never assigns another workload's measured speedup. A workload without a
matched hardware source retains independently calibrated component costs and
is labeled as such in the evidence manifest.

### AMU authority

AMU uses `3663479.pdf` (DOI 10.1145/3663479) for queue depth, SPM capacity,
request granularity, issue/completion behavior, and component timing trends.
The paper's aggregate speedups are validation observations, never fitted
targets for these six workloads. Formal four-core x86 rows retain the common
host configuration instead of importing the paper's single-core RISC-V
baseline.

### M2NDP authority

M2NDP uses the pinned M2NDP revision and maintained strict FuncSim patches.
For every formal latency configuration, a 64-byte request/response microprobe
calibrates the NDPSim CXL boundary against gem5. The measured round trip must
match within one M2NDP link cycle. M2NDP is not retuned from AMU or CIRA
speedups.

The manifest records source hashes, selected observations, direct inputs,
held-out observations, fit parameters, residuals, and rejection reasons.
Changing any source or selected parameter invalidates all dependent timing
rows.

## Full PageRank scaling measurement

For each of g4, g12, g14, and g20, the orchestrator runs Vanilla, AMU, CIRA,
and M2NDP from a fresh evidence root. Functional validation completes before
timing for that scale. No sampling is permitted.

The measured interval begins with trial-1 PageRank-state initialization and
ends after iteration 20, final drain, barrier, and completion. Graph parsing,
binary loading, graph conversion, trace generation, and post-ROI validation
are outside the interval. Trial 0 is a complete CXL warmup and may not be
restored from a post-warmup checkpoint produced by another mechanism.

Gem5 latency is trial-1 ROI ticks divided by 1e12. M2NDP latency is the full
trial-1 launch sequence in NDPSim cycles multiplied by the pinned core period.
At each scale:

```text
speedup(system, scale) = vanilla_seconds(scale) / system_seconds(scale)
```

These deterministic full-simulation points do not receive sampling error
bars. The paper reports the absolute seconds in the companion table so every
speedup can be recomputed.

## Breadth timing and uncertainty

The complete reference and complete accelerated functional executions always
run over the full frozen input. Only cycle timing may use windows.

### Deterministic stratified windows

For each canonical phase with `N` work items, the collector defines:

```text
L = min(65536, floor(N / 128))
```

If `L < 1024`, that phase is timed in full. Otherwise the phase is divided
into 64 equal-work strata. Each stratum contains one non-overlapping pair: an
`L`-item phase-local warmup immediately followed by an `L`-item measured
window. A SHA-256-derived seed from the input hash, trace hash, and phase name
selects a deterministic legal offset within each stratum. The seed, all
coordinates, and all work-item identities are stored before timing begins.

Collection begins with eight evenly distributed strata. If uncertainty is
too high, it expands through nested 16-, 32-, and 64-stratum sets. Every
system uses identical coordinates. Warmup work is excluded from measured
cycles but must execute through the same backend and memory model; no system
may restore a warmed cache image created by another system.

### Reconstruction

For every phase, measured-window time is normalized by completed canonical
work items. The complete phase contribution is the paired estimator over the
manifest's full dynamic work count. Complete end-to-end latency is:

```text
T_system = T_fixed_system
         + sum_over_phases(N_phase * estimated_time_per_work_item_system)
```

`T_fixed_system` includes initialization not represented by a phase,
descriptor/kernel launch, completion handling, drains, barriers, runtime
selection, host/device synchronization, and final commit. It is measured in
the corresponding backend, not copied across systems. MCF hotspot
contributions additionally use their full-reference invocation counts.

For each breadth workload, the plotted ratio is:

```text
speedup(system, workload) = reconstructed_vanilla_seconds(workload)
                          / reconstructed_system_seconds(workload)
```

### Confidence interval and publication rule

Speedups use paired windows against the matched Vanilla windows. The
collector performs 10,000 paired block-bootstrap resamples using the frozen
manifest seed and reports the percentile 95% confidence interval. A result is
publishable only when:

```text
(ci_high - ci_low) / (2 * speedup_estimate) <= 0.05
```

If the condition is not met after 64 windows per nontrivial phase, the result
is terminally `inconclusive`. It appears in the validation report but not as a
numeric main-figure bar. The collector may not change window length, seed,
phase definitions, or exclusion rules after observing performance.

## Evidence roots, checkpointing, and recovery

Every workload/scale receives a new immutable evidence root whose identity
contains the simulator commit, experiment-manifest hash, and input-manifest
hash. Existing g4, partial g12/g14, g20, calibration, or old M2NDP summaries
are diagnostic only and cannot be promoted. In particular, the earlier g20
M2NDP summary with an implausible multi-thousand-fold speedup is excluded and
must be regenerated from the common contract.

Each point follows this state machine:

```text
planned -> functional_pass -> timing_in_progress -> complete
                                              \-> failed
                                              \-> inconclusive
```

There is no periodic live checkpoint rotation. A backend may checkpoint only
at a deterministic phase or timing-window boundary after flushing the
checkpoint manifest. Resume is permitted only when code, binary, input,
trace, configuration, CXL map, calibration, completed-window output, and
checkpoint hashes all match. The newest valid boundary is selected by a
monotonic sequence number; a newer invalid checkpoint is ignored and
reported, not silently loaded.

A code, binary, configuration, input, trace, calibration, or phase-definition
change requires a fresh evidence root. Resume never crosses mechanisms,
latencies, scales, workloads, or thread counts.

## Failure gates

The following conditions make a point `failed`:

- any full-output raw-bit mismatch or official-verifier failure;
- missing, malformed, substituted, or hash-drifted input;
- non-1-us gem5 delay, wrong core/thread count, or non-CXL allocation;
- AMU issue/completion imbalance, queue or SPM overflow, invalid queue-credit
  reservation, translation error, pending-state error, SPM-flag error, or
  cache-line granularity mismatch;
- CIRA issue/completion imbalance, descriptor rejection, dropped response,
  unregistered demand-probe target, queue overflow, coherence error, or
  activity missing from any participating core;
- M2NDP FuncSim mismatch, missing launch, nonzero strict-comparison count,
  invalid pinned revision, or CXL calibration outside one link cycle;
- missing expected phases/windows, different paired coordinates, dynamic-work
  disagreement, or a fixed cost omitted from reconstruction; or
- provenance hash mismatch at normalization or publication time.

Failure preserves raw logs and the terminal reason. It never creates a
canonical performance row.

## Data products and atomic publication

Raw simulator artifacts, normalized system rows, and publication artifacts
are separate layers:

1. `raw/` contains commands, configs, logs, stats, vectors, traces,
   checkpoints, calibration samples, and hashes;
2. `normalized/` contains one schema-validated row per complete system point,
   plus bootstrap inputs and reconstruction terms; and
3. `publication/` contains canonical CSV/JSON, LaTeX, PDF, and SVG generated
   atomically from terminal `complete` rows.

The publisher rejects mixed evidence roots, missing expected rows, failed or
inconclusive numeric rows, unpaired windows, stale hashes, and manually edited
derived files. Exact unrounded latency, speedup, CI, evidence type, and source
row identity remain in the canonical CSV.

## Figure and table design

The replacement figure uses layout A.

### Left panel: PR-SpMV scaling

The x-axis is graph scale g4, g12, g14, and g20, with vertex counts included
in tick labels or a secondary label. The y-axis is end-to-end speedup over the
matched four-thread Vanilla CXL run. AMU, CIRA, and M2NDP are distinct lines;
Vanilla is a neutral 1.0x reference. The panel subtitle says “Full E2E, gem5 +
NDPSim, 1 us CXL.”

### Right panel: workload breadth

The x-axis contains PR, MCF, AMG Gather, LULESH Scatter, NPB CG, and NPB MG.
Each group contains AMU, CIRA, and M2NDP bars with 95% CI whiskers. The panel
subtitle says “Calibrated trace-driven E2E estimate, 1 us CXL.” A workload
that exhausts 64 windows without meeting the uncertainty gate is labeled
“inconclusive” and has no numeric bar.

AMU, CIRA, and M2NDP use fixed colors across both panels, plus distinct line
styles/hatches for grayscale and accessibility. A horizontal 1.0x line is
visible in both panels. Direct labels are preferred over a distant legend.
The plot must remain legible at the paper's one-column or approved two-column
width and must not truncate regressions below 1.0x.

The canonical figure files are:

- `fig/cira-amu-m2ndp-scaling-breadth.pdf`; and
- `fig/cira-amu-m2ndp-scaling-breadth.svg`.

The existing `gapbs-vtune-cxl-table.tex` is regenerated from the same
canonical data and replaced atomically. It reports absolute latency, speedup,
95% CI where applicable, and `full E2E` versus `trace-driven` evidence type.
No value exists only in the plot or only in prose.

## Paper integration

Before editing, the independent Overleaf repository at
`/home/victoryang00/gem5-CXL/6472666535e6f359942ddac6` must be clean and
fast-forwarded from `origin/master`. A non-fast-forward or local overlapping
change stops paper integration for review.

`sections/evaluation.tex` will:

- replace the g4-only Figure 6 and caption;
- state explicitly that g4 is a correctness/fixed-cost point, not the basis
  of the CXL-scale conclusion;
- identify g20 as 2^20 vertices and approximately 240 MB;
- explain the distinct full-E2E and sampled evidence boundaries;
- describe the six matched regions without claiming all-suite coverage;
- report only values present in the canonical CSV; and
- revise the related-work conclusion to match the validated results rather
  than presupposing that any mechanism wins.

The proposed reviewer response is:

> Thank you—we agree that scale 4 alone is too small to support a CXL-scale
> conclusion. We replaced the original experiment with full end-to-end
> PR-SpMV scaling from g4 through g20 at 1 us CXL latency, including a 2^20-
> vertex, approximately 240 MB graph. We also expanded the comparison among
> CIRA, AMU, and M2NDP to six representative workload regions covering
> graph/pointer traversal, irregular gather/scatter, and scientific
> bandwidth-intensive computation. All systems use identical inputs and
> dynamic work, pass full bit-exact validation before timing, and sampled
> trace-driven results report paired 95% confidence intervals. We revised the
> claims to distinguish the small-scale correctness point from the CXL-scale
> performance evidence.

The response is inserted only after the evidence passes; final wording must
name any inconclusive workload rather than implying a complete six-point
result.

## Verification and acceptance criteria

Implementation follows test-driven development. It is complete only when:

1. schema/unit tests reject every input, hash, scale, latency, placement,
   thread, phase, window, pairing, mechanism-counter, and provenance mismatch
   described above;
2. all existing AMU, CIRA, M2NDP, checkpoint, calibration, and publication
   tests continue to pass;
3. each frozen input passes structural validation and is re-readable from its
   recorded hash;
4. all four PR scales complete full functional and timing execution for
   Vanilla, AMU, CIRA, and M2NDP;
5. all six breadth workloads complete full functional execution and full
   output bit-exact validation for all three accelerated mechanisms;
6. every published breadth point meets the paired 95% CI relative-half-width
   threshold, with unsuccessful points explicitly inconclusive;
7. all AMU/CIRA mechanism counters and M2NDP calibration/FuncSim gates pass;
8. an independent validator recomputes every absolute latency, speedup,
   bootstrap interval, phase weight, and figure/table row from raw evidence;
9. the figure and table are generated only from the validated canonical CSV;
10. the paper builds successfully after a fast-forward-safe integration; and
11. code/evidence tooling and paper changes are committed and pushed to their
    corresponding branches without mixing the two repositories.

No intermediate, stale, failed, or merely plausible result satisfies these
criteria. Until every applicable gate passes, outputs may be inspected for
debugging but may not replace Figure 6 or support a performance claim.
