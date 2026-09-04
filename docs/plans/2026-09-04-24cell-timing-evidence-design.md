# 24-Cell Timing Evidence Design

Date: 2026-09-04

## Goal

Publish an auditable timing bundle for the six workloads and four CXL
latencies used by the current G20 latency-spectrum plots:

- workloads: `pr_spmv`, `gap_bc`, `mcf`, `amg_gather`,
  `lulesh_scatter`, and `npb_cg`;
- CXL latency labels: `200ns`, `500ns`, `1us`, and `2us`.

For every one of the 24 workload/latency cells, the bundle will contain:

1. M²NDP kernel cycles, the core period, and the converted kernel time;
2. the calibration record used to select M²NDP `link_latency`;
3. host-inline cumulative time and region-entry count; and
4. CIRA device-side busy time and per-core activity.

The bundle must be reproducible, fail closed on identity drift, and retain an
individual evidence JSON for every cell in the same general format as the
existing NPB CG 1 us evidence.

## Important Mechanism Boundary

The existing 24-cell spectrum uses the matched canonical replay. Its CIRA
accessor calls the generic `cira_prefetch()` interface. It does not submit
PageRank descriptors, even for the `pr_spmv` trace.

Consequently, the existing PageRank-only statistics
`prComputeTicksPerCore` and `prQueueStallTicksPerCore` do not describe these
24 cells. Exporting them as the requested device-side runtime would silently
publish zeros.

This design preserves the mechanism used by the plot. It adds generic CIRA
prefetch-engine busy-span statistics and exports the PageRank-only statistics
as a separate, explicitly not-applicable field for these cells. It does not
replace the six workloads with newly written descriptor kernels, because that
would change the experiment being explained.

## Evidence Root and Identity

The campaign writes to a fresh, append-only evidence root. Each run directory
contains a command record, source and binary hashes, configuration hashes,
input trace identity, raw simulator output hashes, parsed measurements, and a
status field.

The campaign identity contains:

- repository commit and dirty-state rejection;
- the exact six workload identities and four latency labels;
- trace, fixed-trace, window-manifest, binary, gem5, FuncSim, NDPSim, and
  configuration SHA-256 hashes where applicable;
- four-core, timing-mode, all-CXL topology checks;
- the M²NDP core period and link period;
- calibration evidence hashes; and
- the schema version of every evidence record.

No row is published when an expected input is missing, a source path no longer
exists, a hash differs, a simulator does not emit exactly one completion
marker, or the requested metric cannot be derived from the recorded evidence.

The current uncommitted MCF lazy-map changes in the original worktree are not
modified. If the clean branch needs the same bounded-memory behavior, it will
be implemented and tested independently in this campaign branch.

## 1. M²NDP Kernel Timing

Each cell gets `m2ndp-evidence.json`, based on the existing NPB CG 1 us
schema. At minimum it records:

- `workload` and `latency`;
- `cycles`, parsed from exactly one `EXPR FINISHED <cycles>` marker;
- `core_period_ns` as an exact decimal string;
- `kernel_time_ns = cycles * core_period_ns`, also as an exact decimal;
- the selected calibration record and its hash;
- functional and numeric verification status;
- package, kernel, input, config, simulator binary, output, and log hashes;
- the full NDPSim command; and
- `status: pass` only after all identity and completion checks pass.

The campaign uses a 0.5 ns core period unless the hashed simulator
configuration proves a different period. Conversion is performed with decimal
arithmetic rather than binary floating point.

Existing outputs may be reused only when all required inputs and outputs are
present and their recorded hashes verify. Otherwise that cell is rerun under
the new evidence root. Reconstructed evidence must never claim that an old
output was freshly executed.

## 2. Link-Latency Calibration

Calibration is a four-row contract shared by all workloads. Each row records:

- CXL latency label;
- gem5 microprobe round-trip in ns;
- selected M²NDP `link_latency` in link cycles;
- link period and core period;
- modeled M²NDP boundary round-trip in ns;
- signed and absolute residual in ns and ps;
- commands, raw output paths, and hashes; and
- pass/fail status.

The currently verified selections are:

| CXL label | `link_latency` | residual |
|---|---:|---:|
| 200 ns | 1397 | 4 ps |
| 500 ns | 3798 | 55 ps |
| 1 us | 7799 | 27 ps |
| 2 us | 15801 | approximately 25 ps |

These values are accepted only after the existing raw calibration artifacts
and their hashes are verified. If verification fails, the microprobe is rerun
and a new selection is searched; the expected result is not hard-coded as a
substitute for measurement.

## 3. Host-Inline Region Timing

The matched replay gains a distinct `cira-inline` execution identity. It uses
the same canonical trace, work partition, four OpenMP workers, operation
ordering, compiler flags, memory layout, warmup, and measured window as the
corresponding CIRA cell. Its accessor executes loads and stores on the host and
submits no CIRA operation.

The host-inline path is deliberately separate from the existing `vanilla`
label. This proves that the timed code came from the CIRA-compiled replay with
offload disabled instead of borrowing a possibly different baseline binary.

The existing gem5 work-begin/work-end event contract resets statistics at the
start of the measured window and dumps them at its end. Therefore the primary
host time is the measured ROI's `simTicks`; no unsupported guest wall-clock
source or per-entry timing instruction is introduced.

For this campaign, one region entry is defined as one measured canonical work
item, which is the unit assigned to a replay work group. The entry count is
bound to the timing-window manifest as `measure_stop - measure_start` (or the
full phase's work-item count for a full-phase cell). The result JSON contains:

- `host_region_cumulative_ticks`;
- `host_region_entry_count`;
- the exact manifest coordinates from which the count was derived; and
- a marker proving zero CIRA submissions.

The reported host-inline metric covers the offloadable dynamic region only.
Fixed control/setup evidence remains linked for identity and end-to-end plot
reconstruction, but is not added to the requested region time. Each latency is
run separately because all host memory is routed through the selected CXL
configuration.

## 4. CIRA Device-Side Runtime

### Generic prefetch-engine statistics

The CIRA model adds ROI-reset-safe statistics for the mechanism actually used
by the matched replay:

- first accepted generic prefetch issue tick;
- last generic prefetch completion tick;
- global busy span from first issue to last completion;
- per-core first issue, last completion, and busy span;
- issued and completed generic prefetches per core; and
- a validity bit/count showing whether a span contains activity.

The internal timestamp state is reset together with gem5 statistics at ROI
boundaries. A span is valid only when issued equals completed, the outstanding
queue is empty at region exit, and at least one request was accepted. The
global span is the literal first-issue-to-last-completion wall-clock interval;
per-core spans are diagnostic and are not summed.

### PageRank descriptor statistics

The parser also exports the already implemented aggregates and vectors:

- `prComputeTicks` and `prComputeTicksPerCore`;
- `prQueueStallTicks` and `prQueueStallTicksPerCore`;
- PageRank descriptor issued/completed counts.

For the matched-replay cells these fields are published under a
`pr_descriptor_metrics` object with `applicable: false`, and all descriptor
counts must be zero. They are not used to calculate generic CIRA device time.

For future descriptor-based experiments, the same parser can set
`applicable: true`; in that case it reports both the literal descriptor span
and `max(compute_ticks_per_core + queue_stall_ticks_per_core)` as separate
metrics rather than assuming they are always identical.

## Output Schema

The campaign publisher creates:

- `timing-24cells.csv`: one row per workload/latency cell;
- `calibration.csv`: four calibration rows;
- `manifest.json`: all output hashes and campaign identity;
- `cells/<workload>/<latency>/m2ndp-evidence.json`;
- `cells/<workload>/<latency>/host-inline-evidence.json`;
- `cells/<workload>/<latency>/cira-runtime-evidence.json`; and
- compact README tables suitable for sharing.

The main CSV includes these core columns:

- workload and latency;
- M²NDP cycles, core period, and kernel time;
- calibration selected link cycles and residual ps;
- host-inline cumulative ticks/time and entry count;
- CIRA global device busy ticks/time;
- four per-core CIRA busy ticks;
- four per-core CIRA issued/completed counts;
- PageRank descriptor applicability and per-core compute/stall fields; and
- paths and SHA-256 hashes for all three per-cell evidence files.

Tick-to-time conversion uses the gem5 tick frequency recorded in the run
configuration. Values remain integer ticks in primary evidence and are
converted to exact decimal nanoseconds only in derived columns.

## Execution Flow

1. Validate clean commit, tool binaries, storage headroom, frozen workload
   inputs, and the 24-cell matrix.
2. Verify or rerun the four microprobe calibrations.
3. Build one hashed matched-replay binary containing CIRA and `cira-inline`
   modes, plus the instrumented gem5 binary.
4. Run a small qualification cell and require topology, mechanism, completion,
   and metric checks to pass.
5. Run host-inline and CIRA measurements for all 24 cells.
6. Verify or run FuncSim and NDPSim for all 24 M²NDP cells.
7. Publish only after the matrix is complete and every evidence hash verifies.

Runs are resumable by immutable cell identity. A cell is skipped only when its
complete evidence record passes schema and hash validation. Partial files,
failed records, and records from another commit/configuration are never treated
as complete.

## Error Handling

The campaign stops before publication on:

- missing or mismatched source, input, binary, configuration, or output;
- a calibration residual outside the declared tolerance;
- an NDPSim completion-marker count other than one;
- failed functional/numeric verification when that workload's contract
  requires it;
- non-four-core or non-all-CXL gem5 topology;
- nonzero queue/descriptor errors;
- CIRA issued/completed mismatch or a nonempty engine at ROI exit;
- CIRA activity in host-inline mode;
- absent/invalid host timing entries; or
- any missing cell in the 6-by-4 matrix.

Failed cells keep their logs and a failure record but do not enter the published
CSV.

## Verification Strategy

Unit tests cover:

- exact decimal cycle/tick conversion;
- the four calibration rows and residual computation;
- parsing and validation of global/per-core CIRA spans;
- ROI reset behavior for generic CIRA timestamps;
- `cira-inline` identity, zero-offload enforcement, cumulative timing, and
  entry counts;
- timing-window identity and exclusion of fixed control/setup time;
- M²NDP single-completion parsing and per-cell schema;
- resumability and identity/hash rejection; and
- 24-cell completeness and CSV/manifest determinism.

Integration qualification uses one small representative cell before the full
campaign. Final verification includes targeted Python tests, the CIRA gem5
smoke test, replay-binary compilation, schema validation of every evidence
file, independent SHA-256 verification, and a clean-tree check before commit.

The clean worktree baseline ran 601 cross-system tests: 590 passed, 8 had
pre-existing environment/input errors, and 3 were skipped. Seven errors were
caused by an external MCF source checkout at commit
`001a74db8e16883b9cfd9d24208469076d89f1fa` instead of the frozen expected
commit `2b30de22399402d8c44bd74b8ebf743b6a6a55e9`; one was caused by the linked
worktree not containing the generated `util/m5/build/x86/out/libm5.a`.
Targeted tests that do not depend on these two external conditions form the
regression baseline for this work.
