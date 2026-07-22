# GAPBS AMU CXL-Latency Table Design

## Goal

Replace the Overleaf project's existing `gapbs-vtune-cxl-table.tex` with a
compact, evidence-backed comparison of baseline CXL execution, aggressive AMU
execution, and the existing CIRA profile-guided static rewrite at 200 ns,
500 ns, 1 us, and 2 us link latency.

## Experiment Contract

- Workloads: BFS, BC, PR, and SSSP.
- Graph parameters: GAPBS synthetic graph, scale 4, one iteration.
- Simulation: gem5 timing CPU, one core, ROI work events enabled.
- Compared configurations: CXL baseline, AMU, and CIRA PGO/static rewrite,
  built from the same CXLMemUring/GAPBS commits and current gem5 source. There
  is no CIRA-no-PGO configuration. CIRA PGO builds fail closed when any
  selected workload lacks a usable profile; explicit prefetch-distance
  override builds are labeled non-PGO and are excluded from this experiment.
- Build provenance: each manifest hashes every `.cc` and `.h` file in the
  transformed copied GAPBS tree, the builder script, `libm5.a`, gem5 m5ops
  headers, configuration-specific AMU/CIRA headers, profiles actually used,
  and emitted binaries. Override builds claim no profile inputs.
- Latencies: `200ns`, `500ns`, `1us`, and `2us`.
- Correctness: run GAPBS verification after the timed ROI. Every baseline, AMU,
  and CIRA run must report `status=ok` and `verification=pass` (bit-exact PASS).
- Completion: every AMU run must have
  `board.asmc.issuedLoads == board.asmc.completedLoads > 0`. Every CIRA run
  must independently satisfy both of these invariants in the first ROI stats
  section:
  1. leaf cacheline requests balance as
     `board.cira.issuedPrefetches == board.cira.completedPrefetches > 0`; and
  2. descriptor use is proven by
     `board.cira.issuedIndexedPrefetches + board.cira.issuedCsrPrefetches > 0`.
  CIRA exposes no completed indexed-descriptor or completed CSR-descriptor
  counters. Descriptor counts must never be added to leaf request counts or
  compared with `completedPrefetches`.
- Metrics: AMU and CIRA speedup are each baseline ROI `simTicks` divided by the
  corresponding configuration's ROI `simTicks`.

The experiment produces one machine-readable `summary.csv` per latency and a
combined CSV used to generate the LaTeX table. Failed or missing verification
must suppress the corresponding speedup rather than silently publishing it.

## Table Layout

Use one row per workload plus a geometric-mean row. Use four latency column
groups ordered from 200 ns to 2 us. Each group reports AMU speedup, CIRA
speedup, and a compact verification marker. The caption defines the
normalization and states the
graph scale, CPU configuration, and that verification runs outside the ROI.

The table replaces
`6472666535e6f359942ddac6/gapbs-vtune-cxl-table.tex` without changing its input
site. Raw ticks remain in the generated CSV instead of widening the paper table.

## Validation

Before handoff:

1. Require all 48 runs (4 workloads x 4 latencies x 3 configurations) to pass.
2. Check the configured link delay in every run's `config.ini`.
3. Run `scripts/validate_gapbs_amu_latency_sweep.py` on the sweep root. It
   checks AMU load balance, CIRA leaf request balance, nonzero CIRA descriptor
   use, numeric speedups, and the first ROI stats section without mixing leaf
   and descriptor counter domains.
4. Regenerate the table only from the validated combined CSV.
5. Compile the Overleaf project and inspect the resulting table for overflow,
   clipping, and readable labels.
