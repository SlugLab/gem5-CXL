# GAPBS AMU CXL-Latency Table Design

## Goal

Replace the Overleaf project's existing `gapbs-vtune-cxl-table.tex` with a
compact, evidence-backed comparison of baseline CXL execution and aggressive
AMU execution at 200 ns, 500 ns, 1 us, and 2 us link latency.

## Experiment Contract

- Workloads: BFS, BC, PR, and SSSP.
- Graph parameters: GAPBS synthetic graph, scale 4, one iteration.
- Simulation: gem5 timing CPU, one core, ROI work events enabled.
- Compared configurations: CXL baseline and AMU, built from the same
  CXLMemUring checkout and current gem5 source.
- Latencies: `200ns`, `500ns`, `1us`, and `2us`.
- Correctness: run GAPBS verification after the timed ROI. Every baseline and
  AMU run must report `status=ok` and `verification=pass`.
- Completion: every AMU run must have `issuedLoads == completedLoads`.
- Metric: speedup is baseline ROI `simTicks` divided by AMU ROI `simTicks`.

The experiment produces one machine-readable `summary.csv` per latency and a
combined CSV used to generate the LaTeX table. Failed or missing verification
must suppress the corresponding speedup rather than silently publishing it.

## Table Layout

Use one row per workload plus a geometric-mean row. Use four latency column
groups ordered from 200 ns to 2 us. Each group reports AMU speedup and a compact
verification marker. The caption defines the normalization and states the
graph scale, CPU configuration, and that verification runs outside the ROI.

The table replaces
`6472666535e6f359942ddac6/gapbs-vtune-cxl-table.tex` without changing its input
site. Raw ticks remain in the generated CSV instead of widening the paper table.

## Validation

Before handoff:

1. Require all 32 runs (4 workloads x 4 latencies x 2 configurations) to pass.
2. Check the configured link delay in every run's `config.ini`.
3. Check AMU issued and completed loads match.
4. Regenerate the table only from the validated combined CSV.
5. Compile the Overleaf project and inspect the resulting table for overflow,
   clipping, and readable labels.
