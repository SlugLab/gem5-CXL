# Six-Workload CXL Latency Spectrum and Paper Update Design

Date: 2026-08-23

## Goal and scope

Produce a publication-ready comparison of Vanilla CXL, AMU, CIRA, and
M2NDP across six matched workloads and four modeled CXL link latencies, then
replace the paper's scale-4-only comparison with figures, tables, prose, and
raw data generated from that evidence.

The fixed workload set is:

- PageRank SpMV on the frozen g20 GAPBS graph;
- MCF;
- AMG gather;
- LULESH scatter;
- NPB CG; and
- NPB MG.

The fixed latency spectrum is 200 ns, 500 ns, 1 us, and 2 us. Every point uses
four host timing cores and four software workers where a host executes work,
places all workload data in CXL memory, and uses the same canonical dynamic
work and output boundaries. The primary matrix contains 96 system/workload/
latency combinations. Adaptive paired timing windows may create multiple raw
executions for one combination.

This design extends the approved 2026-08-12 scaling-and-breadth contract. It
does not claim coverage of every program in the four benchmark suites. It does
not permit analytical latency scaling, copied speedups, post-hoc tick changes,
or weakening bit-exact validation.

## Current evidence boundary

The r4 g12/1-us qualification root is diagnostic, not publication evidence.
It established the following exact outcomes:

- Vanilla: 1.990498176 ms;
- AMU: 1.430016552 ms, or 1.3919406549638315x;
- CIRA few-shot: 1.392734871 ms, or 1.4292010758449660x; and
- M2NDP: 2.095720500 ms, or 0.9497918143187509x against the shared Vanilla.

All outputs were bit-exact, and M2NDP FuncSim and NDPSim memory matching
passed. Qualification nevertheless failed closed because AMU and M2NDP were
outside the predeclared 1.4x--1.6x interval. No r4 timing may be promoted into
the new matrix.

M2NDP's measured trial contained 160 serialized partition launches. The four
logical partitions of each PageRank phase were emitted as four separate
NDPSim commands, so the simulator's command boundary serialized work that is
independent within a phase. This is an implementation defect, not a license
to rescale the observed latency.

## Selected architecture

Use four independent, immutable latency campaigns underneath one aggregate
publisher. Each campaign owns its state, logs, checkpoints, normalized rows,
and terminal manifest. Content-addressed frozen inputs, reference outputs,
binaries, and canonical traces are shared read-only across campaigns.

This structure is selected over a single monolithic state machine because a
failure at one latency can be repaired and restarted without corrupting the
other identities. It is selected over four fully duplicated trees because
`/mnt/disk0` has limited free space. A purely analytical sweep is rejected
because it would not exercise queueing, coherence, backpressure, completion,
or near-memory execution.

The aggregate publisher accepts results only when all four campaign manifests
are complete and bind the same workload inputs, canonical traces, simulator
binaries, source revision, calibration authorities, and policy revision. The
only intentional identity difference is the declared latency and its derived
configuration.

## M2NDP phase-parallel timing contract

FuncSim retains the complete launch-specific sequence so it continues to
execute and compare every logical partition in canonical order. Timing trace
generation additionally creates one NDPSim command for each PageRank phase.
That command contains the four disjoint partition launch records.

Within a command, NDPSim may issue the four partition launches concurrently.
The existing command-completion boundary remains a hard barrier before the
next phase:

```text
K2 partitions 0..3 concurrently
barrier and drain
K3 partitions 0..3 concurrently
barrier and drain
next iteration
```

The four partitions write disjoint output rows. Each K3 partition retains the
stored CSR neighbor order and scalar float32 accumulation order for every
row. There is no cross-row reduction, reassociation, fused operation, or
changed double-buffer swap. Timing evidence must report all four launch IDs
per phase, phase completion, zero outstanding work at each barrier, and the
measured-trial start/end cycles.

The M2NDP result is accepted only if the unchanged full FuncSim sequence is
bit-exact, the timing execution reports memory-match success, every expected
launch completes exactly once, and the calibrated CXL boundary is within one
M2NDP link cycle of the corresponding gem5 boundary.

## Qualification before the spectrum

Implementation begins with unit and smoke tests for grouped launches,
implicit phase barriers, timing markers, launch cardinality, output-map
binding, and fail-closed result parsing. A fresh evidence root then reruns the
g12/1-us qualification and deterministic replay.

The existing 1.4x--1.6x gate remains unchanged for AMU, fully charged CIRA
few-shot, and M2NDP. AMU's r4 result is below the lower bound and must improve
through modeled execution, batching, queueing, or synchronization behavior;
the runner may not round it up or relax the threshold. Any source, binary,
policy, calibration, or configuration change invalidates the qualification
identity and starts a new root.

No formal six-workload latency campaign starts until qualification and replay
both pass correctness, mechanism, identity, determinism, and performance
gates.

## Per-latency campaign contract

Each latency campaign runs the same six workloads and four systems. The
latency is emitted into gem5 and M2NDP calibration as one of these exact
values:

| Label | gem5 delay ticks |
| --- | ---: |
| 200 ns | 200000 |
| 500 ns | 500000 |
| 1 us | 1000000 |
| 2 us | 2000000 |

Generated `config.ini` and calibration manifests are parsed after execution.
A point fails if the configured delay differs, if memory placement is not
entirely CXL, if the core/thread count differs from four, or if any unmatched
cache, frequency, compiler floating-point, or workload parameter changes.

One complete canonical reference and one full functional execution per
accelerated backend are required for each workload identity. These functional
artifacts may be shared across latencies only because latency cannot affect
functional semantics; their hashes and producing binaries must be identical.
Timing is never shared. Every system/latency pair executes its own backend and
validates its final raw output against the shared reference.

Large-workload timing follows the existing deterministic paired stratified
window contract at levels 8, 16, 32, and 64. All systems at a given workload
and latency use identical coordinates. Each reported speedup has a paired 95%
confidence interval and must meet the existing uncertainty gate. Exhausting
64 windows without meeting that gate yields `inconclusive`, not a numeric
paper bar.

Fixed formation, launch, synchronization, selection/JIT, completion, barrier,
drain, and final-commit costs remain inside reconstructed end-to-end latency.
The publisher recomputes every reconstruction from raw per-window evidence and
complete dynamic counts.

## State, storage, and failure handling

The aggregate root contains:

```text
shared/                 content-addressed inputs, references, traces, binaries
latency/200ns/          immutable campaign and checkpoints
latency/500ns/
latency/1us/
latency/2us/
aggregate/              validated rows and publication artifacts
```

Shared objects are created atomically and verified before linking into a
campaign. A campaign manifest records the shared object hashes rather than
copying the data. Logs, configs, stats, raw output boundaries, and timing
samples remain latency-local. Cleanup or cache eviction is not automatic.

Resume selects the newest valid semantic checkpoint only when all identity
hashes match. A failed point preserves its command, first error, logs, and
partial evidence, and transitions only that latency campaign to a terminal
failure. Fixes require a new campaign identity; a failed or stale root cannot
be continued into publication.

The user's existing modification to `src/mem/cache/base.cc` is outside this
work and must not be changed, staged, committed, or used in a formal simulator
identity unless the user separately authorizes it.

## Raw and normalized data products

The canonical aggregate CSV contains one row for every accepted primary
combination, with at least:

- workload, category, input identity, latency, and system;
- evidence method (`full-e2e` or `paired-stratified`);
- exact absolute latency and speedup over matched Vanilla;
- 95% confidence bounds and window count where applicable;
- fixed cost and per-phase reconstructed contributions;
- thread/core count, all-CXL flag, emitted delay, and output hash;
- mechanism counters and zero-error summary;
- source, binary, config, trace, calibration, policy, and evidence-root hashes.

Companion JSON preserves the complete normalized structure and links each row
to raw evidence. A validation manifest lists every expected coordinate and
file hash. Missing, duplicate, mixed-identity, failed, or inconclusive points
prevent creation of a complete publication manifest. A separate diagnostic
export may include failed/inconclusive rows but is never consumed by the paper
generator.

## Paper update

The paper repository is
`/home/victoryang00/gem5-CXL/6472666535e6f359942ddac6`. Its tracked files
must be clean or non-overlapping before integration; existing untracked PDFs
and GAPBS evidence files are preserved.

The current g4-only table, figure, caption, and associated claims are replaced
only after the aggregate manifest passes. The new publication package contains:

1. a grouped comparison at 1 us with six workloads on the x-axis and AMU,
   CIRA, and M2NDP speedup bars with paired 95% confidence intervals;
2. six latency-sensitivity small multiples covering 200 ns through 2 us, with
   consistent colors and a visible 1.0x reference;
3. a compact table containing exact 1-us end-to-end latency and speedup plus
   latency-spectrum geometric means; and
4. canonical CSV, JSON, PDF, SVG, and LaTeX generated from the same validated
   rows.

The evaluation text identifies PageRank as g20 with 2^20 vertices and limits
the breadth claim to six matched regions spanning graph traversal, network
optimization, gather/scatter, and scientific workloads. It explains full-E2E
versus paired-stratified timing, names any inconclusive result, and reports no
number absent from the canonical CSV. The old scale-4 result may be described
as historical correctness evidence but cannot support the CXL-scale
conclusion.

The paper build must pass with resolved references, no missing figures, no
new LaTeX errors, and an inspected page count/layout. Code and paper changes
are committed and pushed to their corresponding repositories; raw simulator
data remains in the hashed external evidence root.

## Verification and acceptance

The task is complete only when:

1. tests cover latency identity, grouped M2NDP phase launches, phase barriers,
   bit-exact rejection, delay mismatch, shared-artifact hash drift, resume,
   aggregation, and paper generation;
2. all existing cross-system, calibration, checkpoint, and publication tests
   still pass;
3. fresh g12/1-us qualification and deterministic replay pass without
   relaxing the 1.4x--1.6x gate;
4. all 96 primary coordinates have accepted bit-exact evidence and every
   sampled row meets its uncertainty gate;
5. an independent validator recomputes absolute latency, speedup, confidence
   intervals, geometric means, and plot/table rows from raw evidence;
6. generated PDF/SVG figures and LaTeX tables match their recorded hashes;
7. the paper compiles and its revised claims match the validated data; and
8. both repositories are pushed without staging unrelated user files.

Until all applicable gates pass, intermediate results may be inspected and
plotted as diagnostic artifacts but must not replace the paper's formal
figure or support a performance claim.
