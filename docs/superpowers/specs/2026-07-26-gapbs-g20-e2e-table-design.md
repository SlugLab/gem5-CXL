# GAPBS g20 AMU/CIRA/M2NDP End-to-End Table Design

Date: 2026-07-26

## Objective

Replace `6472666535e6f359942ddac6/gapbs-vtune-cxl-table.tex`
with one evidence-generated table containing two explicitly separate panels:

1. the publication comparison of Vanilla CXL, AMU, CIRA, and M2NDP on the
   identical g20 fixed-20 PageRank experiment at a 1 us CXL link delay; and
2. the existing scale-4, single-core AMU/CIRA latency sensitivity results.

The table must never mix the two experiment scales, derive a geometric mean
across them, or publish a formal speedup before every correctness,
configuration, provenance, and calibration gate passes.

## Selected Design

The selected design is a single LaTeX `table` with two labeled panels.
Panel (a) is the primary result and panel (b) is a clearly qualified
sensitivity experiment. This retains the requested multi-latency comparison
without presenting scale-4 measurements as g20 evidence.

Two alternatives were rejected:

- A g20-only table is the cleanest apples-to-apples presentation, but drops
  the requested latency sensitivity.
- Running g20 fixed-20 at 200 ns, 500 ns, and 2 us would make every latency
  directly comparable, but requires additional multi-day simulations and is
  outside the currently approved formal run set.

## Panel (a): Formal g20 End-to-End Comparison

Panel (a) contains four rows in this order:

- Vanilla CXL;
- AMU;
- CIRA; and
- M2NDP.

It contains these columns:

- system;
- matched application end-to-end latency;
- speedup over Vanilla CXL; and
- correctness gate.

The fixed contract is:

- graph: `g20.sg`;
- graph SHA-256:
  `ce900a7147a073835a7450e8f1afedf9f13db6833652bf2f9647819be26bedb3`;
- benchmark: synchronous pull `pr_spmv`;
- PageRank iterations: exactly 20, with early termination disabled;
- trials: two, with trial 0 as a complete CXL warmup and trial 1 measured;
- checkpoint: restored at the entry to trial 0;
- gem5 CPU: two Timing cores;
- gem5 placement: all workload memory on modeled CXL memory;
- gem5 CXL link delay: 1 us, proven by `delay=1000000` in each `config.ini`;
- floating point: scalar float32 operation order with contraction and
  fast-math disabled; and
- graph loading, allocation, trace generation, serialization, and
  post-ROI verification excluded from all timed regions.

For Vanilla CXL, AMU, and CIRA:

```text
latency_seconds = trial1_roi_sim_ticks / 1e12
speedup = vanilla_cxl_trial1_roi_sim_ticks / system_trial1_roi_sim_ticks
```

For M2NDP:

```text
latency_seconds = measured_ndpsim_cycles * ndpsim_core_period_seconds
speedup = vanilla_cxl_latency_seconds / m2ndp_latency_seconds
```

The M2NDP timing is the complete measured trial:
`K0_INIT_TRIAL1`, `K1_META`, and 20 ordered `K2_CONTRIB` /
`K3_PULL_DAMP` pairs. Trial 0 executes first in the same NDPSim process and
must drain before trial 1. The calibrated host-device link must match gem5's
1 us request/response target within one M2NDP link clock.

The caption must call this metric *matched application end-to-end latency*.
It must not call simulator wall-clock runtime, graph conversion, trace
generation, or post-ROI verification part of the measured latency.

## Panel (b): Scale-4 Latency Sensitivity

Panel (b) preserves the four existing latency points:

- 200 ns;
- 500 ns;
- 1 us; and
- 2 us.

It retains BFS, BC, PR, SSSP, and their within-panel geometric mean, with AMU
and CIRA speedup columns at each latency. Its caption and panel heading must
state that these are scale-4, single-Timing-core sensitivity runs and are not
the formal g20 PageRank result.

The publisher recomputes every speedup as baseline ROI ticks divided by
variant ROI ticks. It also recomputes each geometric mean using only the four
workloads at the same latency and for the same mechanism. It rejects a stored
speedup unless its absolute difference from the recomputed value is no more
than `max(1e-12, abs(recomputed) * 1e-12)`.

No value from panel (b) may be averaged with, substituted for, or used to
explain a missing value in panel (a).

## Evidence Inputs

The publisher consumes explicit paths instead of discovering the newest run:

- the completed M2NDP `summary.csv`, `status.json`, `manifest.json`, gem5
  baseline `summary.csv`, calibration artifact, strict FuncSim log, and
  NDPSim log under `m5out/m2ndp_g20_pr_spmv_e2e`;
- AMU `summary.csv` and `evidence.json` under
  `m5out/matched_pr_spmv_g20_e2e/amu/run`;
- CIRA `summary.csv` and `evidence.json` under
  `m5out/matched_pr_spmv_g20_e2e/cira/run`;
- the matched-variant build manifest; and
- the existing scale-4 latency CSV supplied with `--latency-csv`, plus the
  explicit old-run base supplied with `--latency-run-root` so every
  `run_dir/config.ini` can be revalidated.

The implementation must not select artifacts by modification time, glob
order, or a `latest` symlink.

The command-line roots are
`--m2ndp-results-root`, `--variants-results-root`,
`--latency-csv`, `--latency-run-root`, and `--output-dir`.

## Fail-Closed Publication Gates

The publisher must refuse to create or replace the LaTeX table unless all of
the following hold:

1. The M2NDP orchestrator reports every stage passed and has emitted its
   final summary and manifest.
2. The graph hash, fixed iteration count, trial number, two-core Timing CPU,
   all-CXL placement, and 1 us delay match the formal contract.
3. Vanilla CXL, AMU, and CIRA report `Verification: PASS`.
4. AMU has a positive and equal number of issued and completed loads.
5. CIRA has positive descriptor and completion counts.
6. The Vanilla CXL, AMU, CIRA, and FuncSim raw float32 result hashes are
   identical, cover exactly 1,048,576 elements, and are tied to the recorded
   source and binary manifests.
7. FuncSim reports strict bit-exact PASS with zero mismatches.
8. The M2NDP upstream commit, patch, trace, derived configuration, and
   calibration hashes match the final manifest.
9. Calibration passes and its absolute residual is no greater than one link
   clock.
10. All absolute latencies and recomputed speedups are finite and positive.
11. The sensitivity CSV contains exactly one baseline, AMU, and CIRA row for
    every workload/latency pair, all 48 runs pass verification, and every
    configured delay matches its latency label.

On any failure, the command exits nonzero and leaves the existing
`gapbs-vtune-cxl-table.tex` byte-for-byte unchanged.

## Publisher and Outputs

Implementation adds one focused generator:

```text
scripts/generate_gapbs_g20_e2e_table.py
```

The command stages all content before modifying the output directory, then
atomically replaces each file with the LaTeX table replaced last:

- `gapbs-vtune-cxl-table.tex`;
- `gapbs-g20-e2e-results.csv`, containing the four panel-(a) rows; and
- `gapbs-g20-e2e-table-evidence.json`, containing input paths and SHA-256
  hashes, recomputed unrounded values, the graph and run contract, and the
  repository commit.

Thus an evidence or rendering failure leaves all existing outputs unchanged;
the operating system may still expose a partial set only if an I/O failure
occurs during the final sequence of atomic renames. The LaTeX embeds a
generated-file comment with the evidence JSON hash. All four panel-(a)
latencies are displayed in seconds with six digits after the decimal point.
Speedup is displayed to two decimal places, while the CSV and JSON retain
unrounded decimal strings.

The paper directory is currently untracked in the gem5 worktree. The
generator is committed on the experiment branch; replacing the requested
paper file is a separate generated-artifact action and does not silently add
the entire paper directory to the gem5 commit.

## Testing and Validation

Unit tests use synthetic evidence bundles and cover:

- the accepted four-row g20 result;
- every formal contract mismatch;
- missing or failed verifier output;
- a one-bit raw-result mismatch;
- unbalanced AMU and empty CIRA event counts;
- failed or stale M2NDP calibration/provenance;
- wrong baseline ticks or derived speedup;
- missing, duplicate, or failed sensitivity rows;
- separation of panel-(a) and panel-(b) calculations; and
- atomic preservation of an existing output after a rejected input.

After the formal background jobs finish, publication validation is:

1. run the full M2NDP and matched-variant Python unit suites;
2. run the publisher tests;
3. generate the CSV, JSON, and replacement LaTeX from the formal artifacts;
4. independently recompute all four panel-(a) latency/speedup rows;
5. run `git diff --check` for tracked implementation changes; and
6. compile the paper with its existing Makefile when the local TeX toolchain
   is available, otherwise run a structural LaTeX check and report that the
   PDF compilation gate remains unavailable.

No speedup or completed table is reported until these gates pass.
