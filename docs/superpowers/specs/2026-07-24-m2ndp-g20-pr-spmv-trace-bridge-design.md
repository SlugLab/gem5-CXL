# M2NDP g20 PageRank Trace Bridge Design

Date: 2026-07-24

## Objective

Produce a reproducible, apples-to-apples speedup measurement between:

- a two-core gem5 Timing CPU running GAPBS `pr_spmv` with all workload memory
  on the modeled CXL device and a `1us` CXL link delay; and
- the M2NDP cycle-level timing simulator running a four-stage PageRank
  offload over the exact same `g20.sg` graph.

Correctness is a hard gate. The M2NDP functional result must match the gem5
`pr_spmv` result bit-for-bit for every PageRank `float32` element. A result
without both the normal GAPBS verifier and the strict bit comparison passing
must not report a speedup.

## Fixed Experiment Contract

The experiment is fixed to:

- graph:
  `m5out/gapbs_graphs/g20.sg`;
- graph SHA-256:
  `ce900a7147a073835a7450e8f1afedf9f13db6833652bf2f9647819be26bedb3`;
- benchmark: GAPBS synchronous pull PageRank, `pr_spmv`;
- PageRank iterations: exactly 20, with early termination disabled;
- benchmark trials: trial 0 is warmup and trial 1 is measured;
- gem5 CPU: Timing, two cores;
- gem5 memory placement: all workload memory on CXL;
- gem5 CXL link delay: `1us`;
- M2NDP count: one M2NDP device unless a later experiment explicitly defines
  a scaling study;
- M2NDP upstream revision:
  `fe418e8c30d7c3821f7c91293c74c5c34939a063`.

Graph loading, trace generation, memory allocation, result serialization, and
verification are outside both timed regions. Initialization of already
allocated PageRank state and all 20 iterations are inside both timed regions.
The allocated CPU buffers still reside in CXL memory.

## Why a Semantic Bridge Is Required

gem5's existing `MemTraceProbe` records packet time, command, address, size,
flags, and an optional PC. It does not record the M2NDP instruction stream,
kernel arguments, scratchpad layout, or launch boundaries required by
NDPSim.

M2NDP consumes a semantic trace package:

- one or more `.traceg` NDP instruction files;
- per-kernel launch records;
- `kernelslist.g`;
- input and expected-output memory maps; and
- a timing configuration.

Consequently, a raw protobuf-to-`.traceg` conversion would only replay memory
requests and could not support a claim about M2NDP PageRank execution. The
bridge will instead export the graph and PageRank semantics used by gem5,
then generate native M2NDP kernels and launch records. The manifest binds both
runs to the same graph, algorithm, operation order, and iteration count.

## Alternatives Considered

### Raw packet trace conversion

Convert gem5 packet records into synthetic M2NDP memory requests.

This is rejected because it lacks NDP instructions and launch semantics. It
could be useful for a link-only sensitivity test, but it cannot produce the
requested M2NDP application speedup.

### Full gem5/NDPSim co-simulation

Connect gem5 to NDPSim at runtime and forward launch and completion events.

This would model host/device overlap most directly, but it introduces a new
runtime protocol, synchronization model, and checkpoint boundary. It is
unnecessary for the requested single-kernel comparison.

### Semantic trace bridge

Export the exact GAPBS graph representation, generate native M2NDP PageRank
kernels, validate with FuncSim, then execute the same launch sequence in
NDPSim.

This is the selected approach. It is the smallest design that preserves
application semantics, enables strict correctness checking, and yields a
reproducible cycle comparison.

## Repository and Upstream Layout

Implementation will remain in an isolated gem5 worktree and branch based on
`codex-gem5-cira-amu-eval`. The existing g20 service and its
`gapbs-latency-table` worktree must not be modified.

The gem5 repository will contain:

- graph/reference export tooling;
- M2NDP PageRank kernel generators;
- a pinned patch series for M2NDP;
- the orchestration and result-validation scripts;
- unit and integration tests; and
- documentation.

M2NDP itself remains an external checkout supplied through `--m2ndp-root`.
The runner must reject any checkout whose commit is not the pinned revision,
unless an explicit development-only override is used. The patch files and
their hashes are part of the experiment manifest.

Large generated traces, memory maps, binaries, and logs remain under
`m5out/` and are not committed.

## Data Export

A small C++ exporter will be built against the same copied GAPBS source used
to build the gem5 baseline. It will load `.sg` through GAPBS's own graph
reader rather than reverse engineering the serialized file.

The exporter will preserve:

- vertex count;
- directedness;
- incoming CSR row offsets;
- incoming neighbor IDs in their stored order;
- outgoing degree for every vertex; and
- graph SHA-256.

It will emit a compact binary intermediate representation plus
`graph.meta.json`. A second step converts that representation to M2NDP memory
maps at fixed, non-overlapping addresses.

The generator must fail closed if:

- the graph hash differs;
- CSR offsets are non-monotonic;
- the final row offset does not equal the edge count;
- a neighbor is outside the vertex range;
- integer widths would truncate a value; or
- serializing and re-reading any `float32` changes its bit pattern.

The graph is already resident in CXL memory before the gem5 ROI. Therefore,
graph-file parsing and copying the graph into the M2NDP input map are also
outside the M2NDP timed region.

## Matched PageRank Semantics

The matched algorithm is GAPBS `PageRankPull` from `pr_spmv.cc`:

1. Initialize every score to `float32(1 / num_nodes)`.
2. For every iteration, calculate
   `outgoing[n] = score[n] / out_degree[n]`.
3. For each destination vertex, traverse its incoming neighbors in stored CSR
   order and perform one scalar `float32` addition per neighbor.
4. Calculate
   `score[u] = base_score + float32(0.85) * incoming_total`.
5. Repeat for exactly 20 iterations.

The experiment-specific fixed-20 implementation omits the convergence-error
reduction on both systems. Once early termination is disabled, that reduction
has no effect on PageRank state and retaining it only on the CPU would add
unmatched timed work. The normal GAPBS verifier remains unchanged and runs
after the ROI.

The M2NDP implementation uses four unique kernels:

- `K0_INIT`: initialize PageRank arrays;
- `K1_META`: install integer graph pointers, counts, and launch metadata
  without precomputing floating-point reciprocals;
- `K2_CONTRIB`: perform the per-vertex scalar division for the current
  iteration; and
- `K3_PULL_DAMP`: traverse each incoming CSR row in order using scalar
  `fadd`, followed by separate scalar `fmul` and `fadd` operations.

The launch sequence for one PageRank trial is:

```text
K0_INIT
K1_META
repeat 20 times:
    K2_CONTRIB
    K3_PULL_DAMP
```

M2NDP vector reduction and fused multiply-add instructions are forbidden in
the strict kernel because they alter the floating-point operation order.
Compilation of the matched GAPBS binary must disable floating-point
contraction and fast-math transformations. The build manifest records the
compiler, flags, source hash, and binary hash.

## Fixed Iterations and Reference Output

GAPBS normally permits early convergence. The matched baseline build will add
an experiment-specific `PageRankPullFixed` mode that executes exactly the
state-changing operations above, omits the now-unused convergence-error
reduction, and leaves the normal verifier tolerance unchanged. This avoids
the invalid alternative of setting tolerance to zero, which would also make
the existing verifier fail by construction. The generated source manifest
must prove that fixed mode was selected.

The dedicated trial wrapper allocates its score and contribution arrays before
`m5_work_begin`, then initializes and computes them after the marker. M2NDP
memory-map allocation is likewise untimed and `K0_INIT` performs the matched
timed initialization. This prevents CPU allocator work, which NDPSim does not
model, from inflating the gem5 denominator.

On the measured trial, the baseline writes the final PageRank vector as raw
little-endian `uint32_t` words after `m5_work_end`. The dump is outside the
timed ROI. The normal GAPBS verifier still runs and must report
`Verification: PASS`.

The reference file includes a header containing:

- graph SHA-256;
- vertex count;
- iteration count;
- PageRank source hash;
- baseline binary hash; and
- trial number.

## Strict FuncSim Validation

The pinned M2NDP revision is not strict enough for this experiment:

- `HashMemoryMap::Match` accepts approximately one percent relative error for
  `float32`; and
- FuncSim returns exit status zero even when memory comparison fails.

The maintained patch series will add:

- an opt-in exact `float32` comparison based on the underlying 32-bit word;
- a nonzero FuncSim exit status for any mismatch;
- mismatch reporting with address, element index, expected bits, and actual
  bits;
- a sequential multi-kernel mode that retains one memory map across
  `K0/K1/20*(K2/K3)`; and
- an optional final raw-bit dump for independent comparison.

Strict mode must be explicitly visible in the FuncSim command and log. The
runner rejects a log lacking its strict-mode marker.

The FuncSim correctness gate requires all of:

- process exit status zero;
- `M2NDP_STRICT_MATCH=PASS`;
- zero mismatched elements;
- exactly the expected vertex count compared; and
- equality between the independent final dump and the gem5 reference file.

A fault-injection test must flip one result bit and prove that FuncSim exits
nonzero and the orchestration script suppresses speedup.

## Warmup and Timing Sequence

The gem5 experiment uses two benchmark trials and measures trial 1. Its
checkpoint is restored at the entry to trial 0, so trial 0 executes as a
complete CXL warmup.

NDPSim will run two complete PageRank trials in one simulator process. The
second trial uses a uniquely named `K0_INIT_TRIAL1` launch record so its launch
cycle can be identified in the log. All requests from trial 0 must drain
before trial 1 begins. Device caches and memory remain live across the
boundary, while `K0_INIT_TRIAL1` resets the PageRank arrays just as GAPBS
creates fresh arrays for the second trial.

The measured NDPSim cycle count is:

```text
measured_cycles = final_expr_cycle - trial1_k0_launch_cycle
```

The parser rejects missing, duplicated, or out-of-order timing markers.

## CXL and Device Timing

The M2NDP workload arrays reside in the M2NDP-attached memory. PageRank data
accesses therefore use the emulator's internal device-memory path; only host
launch, synchronization, and bidirectional requests traverse its CXL link.
This models the requested full offload rather than making each NDP load cross
the host link.

The derived M2NDP configuration will retain the official device core, cache,
and DRAM parameters. Its host-device link will be calibrated with matched
single-request microbenchmarks. First, gem5 measures the host-visible
request/response latency for an uncached packet with `--cxl-link-delay 1us`.
Then the M2NDP link microtrace uses the same payload size and request direction,
and only the M2NDP link-latency parameter is adjusted until its host-visible
request/response latency matches the gem5 measurement. This defines the
one-way versus round-trip boundary empirically instead of inferring it from
either simulator's parameter name.

The calibration artifact records:

- source and derived link configuration hashes;
- link and core periods;
- request size;
- gem5 request/response ticks and nanoseconds;
- M2NDP request/response cycles and nanoseconds;
- the residual calibration error; and
- the selected M2NDP link-latency value.

The runner must not infer `1us` from a configuration filename or silently use
the official default `35ns` link. The residual error must be within one M2NDP
link clock period or the application timing run is blocked.

## Speedup Definition

gem5 uses a `10^12` tick-per-second timebase. NDPSim's reported
`EXPR FINISHED` value is in core-domain cycles; the core period is parsed from
the exact configuration/output rather than hardcoded.

```text
gem5_seconds  = gem5_trial1_roi_sim_ticks / 1e12
m2ndp_seconds = measured_ndpsim_cycles * ndpsim_core_period_seconds
speedup       = gem5_seconds / m2ndp_seconds
```

The gem5 denominator must come from the `pr_spmv`, two-core, Timing,
all-CXL, `1us`, trial-1 row generated by this experiment. An older `pr`
Gauss-Seidel result or a result from another graph/configuration is invalid.

No speedup is emitted unless:

- gem5 status is `ok`;
- GAPBS verification passes;
- the graph, source, binary, and configuration hashes match the manifest;
- FuncSim strict validation passes;
- the NDPSim run completes normally;
- both timing markers are present exactly once; and
- the parsed times are positive and finite.

## Orchestration and Artifacts

One top-level runner will perform or resume these stages:

1. validate the gem5 and M2NDP checkouts;
2. build the matched `pr_spmv` baseline and graph exporter;
3. export and validate g20 CSR;
4. generate M2NDP kernels, launch files, and memory maps;
5. run the matched gem5 baseline;
6. run strict FuncSim;
7. calibrate and run NDPSim;
8. validate provenance and timing;
9. write the final summary.

Full g20 FuncSim and NDPSim stages run without a simulator timeout and support
detached/background execution. Small-graph validation must finish before the
g20 job is launched.

The run directory is:

```text
m5out/m2ndp_g20_pr_spmv/<run-id>/
```

It contains:

- `manifest.json`;
- `graph.meta.json`;
- `trace/`;
- `reference/`;
- `gem5/`;
- `funcsim/`;
- `ndpsim/`;
- `calibration/`;
- `summary.csv`; and
- `status.json`.

`summary.csv` includes at least:

- graph path and SHA-256;
- vertex and edge counts;
- gem5 and M2NDP revisions;
- patch/config/binary hashes;
- CPU, core count, memory placement, and CXL delay;
- PageRank iterations and measured trial;
- gem5 verification status;
- FuncSim strict status and compared-element count;
- gem5 ROI ticks;
- NDPSim start, end, and measured cycles;
- both times in nanoseconds;
- speedup; and
- paths to the evidence logs.

Writes use temporary files followed by atomic rename. Interrupted stages keep
their logs but cannot create a publishable `summary.csv` row.

## Tests and Proof Gates

### Unit tests

- graph-hash and CSR structural validation;
- memory-map address allocation and round-trip parsing;
- exact `float32` bit serialization;
- PageRank launch ordering and iteration count;
- NDPSim timing-marker parsing;
- speedup calculation;
- manifest hash validation; and
- suppression of speedup for every failed gate.

### Small-graph integration tests

- export a deterministic graph with dangling and non-dangling vertices;
- compare the native matched PageRank reference with FuncSim bit-for-bit;
- run all four unique kernels for multiple iterations;
- prove warmup and measured-trial marker selection;
- flip one expected bit and require a nonzero failure; and
- prove that fixed-20 mode omits convergence-error reduction on both sides;
- calibrate a matched request/response microtrace within one link clock;
- run a short NDPSim timing smoke test.

### Full g20 acceptance

- exact graph SHA-256;
- gem5 `pr_spmv` trial-1 verifier pass;
- exactly 20 PageRank iterations in both systems;
- exactly one warmup and one measured trial;
- strict FuncSim comparison of every vertex with zero mismatches;
- completed NDPSim run using the calibrated `1us` link configuration; and
- a machine-readable speedup row with complete provenance.

## Non-goals

This work does not:

- claim that raw gem5 memory packets are M2NDP instructions;
- compare M2NDP's stock 299,067-node PageRank input with g20;
- use the existing GAPBS `pr` Gauss-Seidel result as the denominator;
- generalize the bridge to BFS, BC, or SSSP in this change;
- report results from a timing run that failed strict functional validation;
  or
- modify or stop the existing background g20 `pr` service.

## Integration

The design and implementation live on `m2ndp-g20-pr-spmv`, based on
`codex-gem5-cira-amu-eval`. After all proof gates pass, the branch may be
merged locally back into `codex-gem5-cira-amu-eval`. The live
`gapbs-latency-table` worktree remains separate until its existing service has
finished.
