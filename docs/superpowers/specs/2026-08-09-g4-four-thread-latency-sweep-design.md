# G4 Four-Thread AMU/CIRA/M2NDP Latency Sweep Design

## Goal

Produce a complete, bit-exact PageRank comparison for Vanilla CXL, AMU,
coherent CIRA, and M2NDP on one fixed scale-4 graph at 200 ns, 500 ns, 1 us,
and 2 us CXL link latency. The host-side configurations use four timing cores
and four application threads. The result is a 16-row formal matrix suitable
for a paper table and latency-sensitivity figure.

This experiment is separate from the running g20 evaluations. It must not
stop, restart, overwrite, or change the checkpoint policy of any existing g20
gem5 or NDPSim process.

## Fixed experiment contract

The graph is the existing serialized GAPBS graph:

```
/home/victoryang00/gem5-CXL/.worktrees/gapbs-latency-table/m5out/gapbs_graphs/g4.sg
```

Its required SHA-256 is
`f234532690f6cfc30e993c4d9a1839e65002a618e7da20ea6a4242818b9c6c3d`.
Every run, reference, trace, and evidence record must name this absolute path
and hash. No mechanism may regenerate its own graph or use `-g 4` internally
after the fixed file has been selected.

The application contract is:

- benchmark: fixed-float32 `pr_spmv`;
- trials: two, with trial 0 as warmup and trial 1 measured;
- PageRank iterations per trial: exactly 20;
- update scheme: synchronous double buffering;
- floating-point contraction and fast math: disabled;
- host CPU: four gem5 timing cores;
- host application threads: exactly four, enforced in both the runner and
  environment;
- memory size: 4 GiB;
- graph and all application data: entirely in CXL-attached memory;
- CXL link latencies: 200 ns, 500 ns, 1 us, and 2 us;
- hardware prefetchers, cache sizes, MSHRs, and CPU frequency: identical
  across Vanilla, AMU, and CIRA at a given latency;
- AMU and CIRA queue/latency parameters: the validated aggressive AMU and
  coherent multicore CIRA settings already used by the current g20 branch.

The four host threads apply to Vanilla CXL, AMU, and CIRA. M2NDP remains one
complete M2NDP device with its normal internal NDP topology; it is compared
against the four-thread Vanilla CXL baseline at the same latency. Artificially
reducing the M2NDP device to four internal units would change the architecture
and is outside this experiment.

## Formal profile rather than smoke mode

The current runners treat scale 20 and two cores as the only publication
profile. Scale 4 is accepted only through `--smoke-test`, which weakens graph
and output-size gates. The new experiment must not publish smoke-test output.

Implementation introduces an explicit, fail-closed formal profile for this
experiment. The profile binds:

- graph scale 4 and the fixed graph SHA-256 above;
- four timing cores and four threads;
- two trials and 20 iterations;
- the four allowed latency labels and their exact tick values;
- all-memory-CXL placement; and
- the expected result vector length derived from the graph.

The existing g20/two-core profile remains byte-for-byte compatible at its CLI
and evidence boundaries. Generic command-line values may not silently relax a
formal profile. A mismatch between profile, graph, core count, thread count,
latency, binary manifest, checkpoint, trace, or output length fails before a
result row is published.

## Execution matrix

For each latency, the orchestrator runs the following configurations
sequentially so that each result has an unambiguous provenance record:

1. Vanilla CXL on four timing cores and four threads;
2. aggressive AMU on the same four-core hierarchy;
3. coherent CIRA with one routed request path per private L2 and all four
   cores registered as demand-probe targets; and
4. one M2NDP device using a latency-specific calibrated link configuration.

This produces exactly 16 canonical result rows. The runner may build common
binaries once, but each latency receives a fresh gem5 baseline run and a
separate M2NDP calibration. Checkpoints may be reused only within runs whose
graph, binary, core count, memory topology, and pre-ROI state hashes are
identical. A checkpoint from another latency or thread count is rejected.

The complete sweep runs as one low-priority persistent background service in
a new output root:

```
m5out/g4_4thread_latency_sweep_20260809/
```

It does not reuse result directories from the old single-core scale-4 table.
CPU contention may extend host wall-clock time but cannot substitute wall time
for simulated latency. All reported gem5 performance uses ROI simulation
ticks.

## M2NDP calibration and timing

Each of the four latency points runs the existing 64-byte request/response
calibration against a gem5 microprobe configured with that latency. The
derived M2NDP link parameter must match the measured gem5 round trip within
one M2NDP link clock. Calibration samples, the selected parameter, target and
measured nanoseconds, residual, source hashes, and derived configuration hashes
are saved under the latency-specific result directory.

M2NDP consumes a trace generated from the same fixed g4 graph and the same
two-trial, fixed-20 PageRank contract. FuncSim executes the full functional
sequence before NDPSim timing is accepted. M2NDP latency is the complete
measured-trial interval beginning at `K0_INIT_TRIAL1` and ending when the final
trial-1 `K3_PULL_DAMP` completes:

```
m2ndp_seconds = measured_ndpsim_cycles * ndpsim_core_period_seconds
```

No graph conversion, trace generation, calibration search, FuncSim execution,
or post-run validation time is included in the timed interval.

## Correctness and evidence gates

Correctness is a hard gate at every latency. The orchestrator first produces a
Vanilla float32 reference vector for the fixed graph. AMU, CIRA, M2NDP
FuncSim, and the measured host runs must match it element-by-element and
bit-for-bit. The evidence records include raw vector hashes and the compared
element count. A tolerance-based match is not accepted.

Additional mechanism gates are:

- Vanilla: verification PASS, positive ROI ticks, four timing cores, four
  threads, and all-memory-CXL configuration;
- AMU: positive issued-load count, issued equals completed, no queue overflow,
  and activity attributable to the four-thread execution;
- CIRA: positive descriptors and completions, all four routed ports active,
  per-core issued/completed balance, zero rejected descriptors, bounded queues,
  and coherent demand-probe registration for all four private L2s;
- M2NDP: all preparation stages PASS, FuncSim mismatch count zero, strict
  float32 match PASS, latency calibration PASS, all expected kernel launches
  complete, and positive measured-trial cycles.

For every latency, the configured gem5 delay must equal its label in ticks.
The M2NDP calibration record must refer to the same latency. Cross-latency
reuse or stale configuration hashes fail closed.

## Metrics and comparison

For Vanilla CXL, AMU, and CIRA:

```
latency_seconds = trial1_roi_sim_ticks / 1e12
```

For M2NDP, latency is calculated from measured cycles as defined above.
Speedup at each latency uses the freshly measured four-thread Vanilla CXL row
from that latency:

```
speedup = vanilla_latency_seconds / mechanism_latency_seconds
```

No result may use the old single-core scale-4 baseline, the g20 baseline, or a
baseline from another latency. Absolute latency and speedup are both retained
so the comparison can be independently recomputed.

## Outputs and publication

The output root contains per-latency manifests, commands, logs, raw vectors,
gem5 summaries, M2NDP stage state, calibration evidence, and one canonical
aggregate dataset. Publication occurs atomically only after all 16 rows pass.
The final artifacts are:

- `gapbs-g4-4thread-latency-results.csv`: 16 unrounded rows with absolute
  latency, speedup, configuration, correctness, activity, and provenance;
- `gapbs-g4-4thread-latency-evidence.json`: source, binary, graph, checkpoint,
  trace, calibration, result-vector, and per-run hashes;
- `gapbs-g4-4thread-latency-table.tex`: exact latency and speedup table;
- `gapbs-g4-4thread-latency-sweep.pdf` and `.svg`: a latency-sensitivity
  figure generated from the canonical CSV; and
- a validation report recording independent recomputation of all 16 latency
  and speedup values.

The figure uses CXL latency as the ordered x-axis and speedup over the matched
Vanilla row as the y-axis. Vanilla appears as a neutral 1.0x reference line;
AMU, CIRA, and M2NDP use direct labels plus distinct line styles so the figure
remains legible in grayscale. Its caption states scale 4, four host threads,
four timing cores, all-CXL memory, two trials, 20 iterations, and bit-exact
verification.

Until all gates pass, temporary summaries may be inspected for debugging but
the aggregate CSV, TeX table, and figure are not replaced or described as
complete results.

## Testing and failure behavior

Unit tests cover profile selection, graph/core/thread/latency mismatches,
result-vector length, per-core CIRA evidence, AMU balance, M2NDP calibration
binding, 16-row matrix completeness, matched-baseline selection, and atomic
publication. Existing g20 tests must continue to pass unchanged.

A short scale-4 proof run exercises all four mechanisms before the persistent
sweep starts. Because scale 4 is itself the target dataset, this proof uses a
separate output root and cannot be promoted into the formal aggregate.

The orchestrator stops publication on the first failed gate while preserving
the failing run directory and logs. It may resume already passed stages only
when their input and output hashes still match. It never kills or restarts the
existing g20 services. A failed g4 service can be restarted with resume mode
after the specific blocker is corrected.

## Acceptance criteria

The experiment is complete only when:

1. the canonical CSV has exactly one Vanilla, AMU, CIRA, and M2NDP row for
   each of the four latency points;
2. all 16 rows use the fixed g4 graph hash, four-thread contract, all-CXL
   placement, two trials, and 20 iterations;
3. every raw result vector is bit-exact with the same Vanilla reference at its
   latency and reports zero mismatches;
4. AMU and four-port CIRA activity gates pass at every latency;
5. all four M2NDP calibrations and NDPSim timing runs pass;
6. every speedup independently recomputes from the matching latency's fresh
   Vanilla absolute latency;
7. the complete unit and integration test set passes;
8. the TeX table, PDF, and SVG are regenerated from the canonical CSV and
   visually inspected; and
9. the current g20 processes and their result/checkpoint directories remain
   untouched.
