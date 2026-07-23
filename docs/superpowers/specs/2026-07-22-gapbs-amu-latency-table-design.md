# GAPBS AMU/CIRA CXL-Latency Table Design

## Goal

Replace the Overleaf project's existing `gapbs-vtune-cxl-table.tex` with a
compact, evidence-backed comparison of baseline CXL execution, aggressive AMU
execution, and the CIRA profile-guided static rewrite at 200 ns, 500 ns, 1 us,
and 2 us link latency.

The previously generated scale-4 table is not publication evidence. A
$2^4$-vertex graph fits in the cache hierarchy, so its ROI is dominated by
warm-cache and instruction-miss effects instead of graph-data CXL latency. The
old scale-4 table and provenance CSV must not be published. Both artifacts are
regenerated only after the scale-20 experiment below passes its gates.

## Experiment Contract

- Workloads: BFS, BC, PR, and SSSP.
- Graph parameters: GAPBS synthetic graph, scale 20 (`-g 20`), two kernel
  trials (`-n 2`), one process, and one core.
- CPU phases: an Atomic CPU performs program startup and graph construction.
  At the first `m5_work_begin`, gem5 switches exactly once to a Timing CPU.
  Trial 0 then runs on the Timing CPU as an unmeasured cache warmup. Trial 1
  runs on the same Timing CPU as the sole measured ROI. Stats reset at trial
  1's `m5_work_begin` and dump at trial 1's `m5_work_end`; startup, graph
  generation, CPU switching, trial 0, and verification are outside the ROI.
- Memory placement: the complete physical address space, including every
  graph array and kernel value array, is reachable from the CPUs and CIRA only
  through `board.cxl_mem_link0`, the configured CXL `SerialLink`. There is no
  host-DRAM address range, direct memory-controller bypass, or pre-ROI graph
  copy into a non-CXL tier. CIRA continues to install through the private L2,
  whose misses traverse the same CXL link. This is a CXL-memory experiment;
  kernel computation remains on the Timing CPU and is not represented as a
  timing-only device-compute shortcut.
- Compared configurations: CXL baseline, AMU, and CIRA PGO/static rewrite,
  built from the same CXLMemUring/GAPBS commits and current gem5 source. There
  is no CIRA-no-PGO or device-offload configuration in the paper experiment.
- Latencies: `200ns`, `500ns`, `1us`, and `2us`.
- Correctness: GAPBS verification runs after each kernel trial. Every baseline,
  AMU, and CIRA run must report `status=ok` and `verification=pass`. Any failure
  suppresses the speedup and blocks publication.
- Metrics: AMU and CIRA speedup are baseline trial-1 ROI `simTicks` divided by
  the matched configuration's trial-1 ROI `simTicks`.

## CIRA Lookahead Contract

The current coarse CIRA CSR call starts at the row being consumed and can
complete too late to reduce demand misses. The corrected rewrite issues a CSR
descriptor for a future row, clamped to the graph's row count:

```text
prefetch_row = current_row + workload_profile_distance
```

`workload_profile_distance` comes from the selected workload's
`*_twopass_profile.json`, not from one global override. The current profile
inputs resolve to BFS 16, BC 24, PR 8, and SSSP 20 rows. The builder fails
closed if a selected profile is absent, has no positive
`optimal_prefetch_depth`, or resolves to a different value than the manifest.
The manifest records the profile path, hash, and resolved distance for each
binary. A manual `--prefetch-distance` build is labeled non-PGO and is excluded
from the paper experiment.

CIRA drains outstanding requests only at correctness or phase boundaries; the
kernel must not synchronously drain after each future-row descriptor. Queue
admission and completion accounting remain bounded by the configured CIRA
outstanding and send-queue limits.

## Evidence and Counter Contract

Issued/completed equality is an integrity check, not evidence of benefit.
Every measured row records the following values from the first and only
trial-1 ROI stats section:

- L1D and L2 demand-data misses for the Timing CPU requestor;
- CIRA `issuedPrefetches`, `completedPrefetches`, `readPackets`, `readBytes`,
  future-row/CSR descriptor counts, and new CIRA-specific `usefulPrefetches`
  and `latePrefetches` counters;
- AMU issued and completed loads; and
- the exact CXL request/response packet-count and byte statistics associated
  with `board.cxl_mem_link0.cpu_side_port`.

The summary code must select the explicit `pktCount_*` statistics for packet
counts and the corresponding `pktSize_*` statistics for bytes. It must never
sum every stat whose name merely contains `cxl_mem_link0.cpu_side_port`, since
that mixes counts, bytes, and unrelated formulas.

CIRA-specific usefulness is attributed at the private L2 cacheline level. A
later Timing-CPU demand for a line whose CIRA fill completed first increments
`usefulPrefetches`; a demand that reaches the L2 while that CIRA line is still
outstanding increments `latePrefetches`. The same demand must not increment
both counters, and duplicate CIRA requests for a line must not double-count a
single demand.

The first implementation discriminator is PR at 1 us:

1. baseline and CIRA both pass bit-exact verification;
2. the baseline L2 demand-data misses exceed one complete 256 KiB L2 working
   set (4096 cachelines at 64 B), rejecting the scale-4 warm-cache pathology;
3. CIRA L2 demand-data misses are strictly lower than baseline;
4. `board.cira.usefulPrefetches > 0` and all CIRA integrity counters balance;
5. baseline ROI ticks divided by CIRA ROI ticks is strictly greater than 1.

The four-workload/four-latency matrix starts only after this discriminator
passes. For the full matrix, all counters and miss deltas are reported, but not
every workload is required to improve. A cell at or below 1.00x is published
as measured and must not be described as a benefit. `latePrefetches` may be
zero; it must be present and numeric so lateness is distinguishable from
missing instrumentation.

AMU completion integrity requires
`board.asmc.issuedLoads == board.asmc.completedLoads > 0`. CIRA completion
integrity requires
`board.cira.issuedPrefetches == board.cira.completedPrefetches > 0` and
positive CSR/future-row descriptor use. Descriptor counts and cacheline
request counts are distinct domains and must never be added together.

## Artifacts and Table Layout

The experiment produces one machine-readable `summary.csv` per latency and a
combined provenance CSV. Each workload row contains raw ticks, verification,
L1D/L2D misses, AMU counters, CIRA issued/completed/useful/late counters, CIRA
read packets/bytes, and true CXL packet/byte totals.

The LaTeX table uses one row per workload plus a geometric-mean row. It uses
four latency groups ordered from 200 ns to 2 us; each group reports AMU
speedup, CIRA speedup, and a compact verification marker. The caption defines
normalization and states scale 20, Atomic pre-ROI generation, Timing CPU ROI,
trial-0 warmup, and trial-1 measurement. Raw evidence remains in the provenance
CSV instead of widening the paper table.

The generated table replaces
`6472666535e6f359942ddac6/gapbs-vtune-cxl-table.tex` without changing its input
site.

## Validation Sequence

1. Unit-test the Atomic-to-Timing one-shot switch and two-trial state machine.
2. Prove from `config.ini` that all memory ranges terminate behind
   `board.cxl_mem_link0` and that baseline, AMU, and CIRA use identical CPU,
   cache, link, graph, and trial settings.
3. Run and validate the PR@1us baseline/CIRA discriminator above.
4. Only then run all 48 configurations: 4 workloads x 4 latencies x baseline,
   AMU, and CIRA.
5. Require all 48 runs to pass bit-exact verification and configuration
   checks. Do not require all 32 accelerated rows to exceed 1.00x.
6. Run `scripts/validate_gapbs_amu_latency_sweep.py` on the sweep root. It must
   reject missing/duplicate rows, stale scale-4 results, the wrong measured
   trial, a CXL bypass, false packet totals, absent usefulness/lateness
   instrumentation, and unbalanced integrity counters.
7. Regenerate the provenance CSV and LaTeX table only from the validated
   scale-20 combined CSV.
8. Compile the Overleaf project and inspect the result for overflow, clipping,
   readable labels, and claims that match the measured speedups.
