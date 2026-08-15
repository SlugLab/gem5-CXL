# PR Scaling Input Decoupling and Plot Family Design

Date: 2026-08-15

## Goal

Run the complete 16-point PageRank scaling matrix immediately, without
weakening the missing-input gate for the separate six-workload breadth study.
The matrix is CIRA, AMU, M2NDP, and Vanilla at g4, g12, g14, and g20, using
four host threads, all-CXL placement, 1 us modeled CXL latency, synchronous
double buffering, and 20 PageRank iterations. Every accelerated point must be
bit-exact before its timing is accepted.

The completed matrix will publish both machine-readable raw data and several
views of that same data. No plot may contain a manually transcribed value or a
value from the currently blocked breadth evidence root.

## Problem

The existing input freezer requires one paper input record containing both the
four PageRank graphs and all six breadth workloads. The real g4, g12, g14, and
g20 graphs are available, but the required 345 MB MCF, 1 GB AMG/LULESH, and
12 GB-class NPB inputs are not bound to source-authoritative records. The
freezer therefore correctly emits `failed-input.json`.

This gate is too coarse. Missing breadth inputs should prevent breadth timing
and the combined paper figure, but they do not invalidate the independent
PageRank scaling inputs. Reusing the failed breadth root, synthesizing missing
inputs, or marking a partial breadth record accepted would destroy the evidence
boundary and is forbidden.

## Selected architecture

Add an independent, narrowly scoped PR-scaling input manifest and leave the
strict six-workload freezer unchanged. The scaling runner consumes only this
manifest. The breadth runner continues to consume the existing full paper
input manifest and remains fail-closed until the real workload records exist.

The publisher will accept two distinct input-manifest hashes because the two
panels now have distinct source scopes. It will still require:

- the same calibration-manifest SHA-256 for scaling and breadth;
- the same g20 graph SHA-256 in both evidence roots;
- complete formal scaling evidence and terminal breadth evidence; and
- all existing per-row functional, timing, configuration, and output-hash
  gates.

Consequently, decoupling can unblock the scaling run but cannot manufacture a
combined CIRA/AMU/M2NDP paper result while breadth remains unavailable.

## PR-scaling input contract

A focused freezer, `scripts/freeze_pr_scaling_inputs.py`, creates exactly one
canonical JSON object with:

- integer `schema` equal to 1;
- `status` equal to `accepted`;
- `scope` equal to `pr_scaling`;
- `profile` equal to `pr-scaling-4thread-1us`;
- an ordered `graphs` array containing exactly g4, g12, g14, and g20; and
- a `graph_set_sha256` over the canonical ordered graph identities.

Each graph entry binds the scale, resolved graph path, graph SHA-256, resolved
frozen-manifest path, frozen-manifest SHA-256, node count, directed-edge count,
generator path, generator SHA-256, and canonical generator command. The freezer
loads each graph through `gapbs_pr_experiment_profiles.load_scaling_graphs()`;
it does not duplicate or relax header, size, path, generator, node-count, or
endpoint-hash checks.

The freezer requires four ordered `--graph-manifest` arguments and an
`--output`. It uses an exclusive, immutable write. If validation fails it
writes a sibling terminal `failed-input.json` with the error and writes no
accepted manifest. An existing accepted output is reusable only when its bytes
match the newly computed canonical payload exactly.

The scaling runner rejects a general breadth manifest or any accepted manifest
without the exact `scope`, `profile`, ordered graph list, graph-set digest, and
live file hashes. Its evidence identity continues to include the complete
scaling-input manifest hash and calibration hash, so a changed path, graph,
manifest, generator, or calibration requires a fresh evidence root.

## Adopting the existing g4 and g20 graphs

The existing g4 and g20 `.sg` files are real serialized GAPBS graphs but do not
both have the current schema-1 frozen manifest. They must not be regenerated,
because that would create a new input rather than freeze the selected one.

Extend the graph preparation utility with a read-only adoption operation. It
inspects the existing CSR header and exact file size, requires `num_nodes ==
2^scale`, verifies the formal endpoint hash for g4 or g20, binds the existing
converter binary and its SHA-256, and records the canonical command that
created the selected path. It then writes a new immutable schema-1 manifest in
the fresh scaling evidence root. Adoption never changes, copies over, or opens
the graph for writing.

For this run, the g4 and g20 hashes remain:

- g4: `f234532690f6cfc30e993c4d9a1839e65002a618e7da20ea6a4242818b9c6c3d`;
- g20: `ce900a7147a073835a7450e8f1afedf9f13db6833652bf2f9647819be26bedb3`.

The already frozen g12 and g14 manifests are loaded directly. All four
resulting manifests are validated together before the scaling input manifest
is accepted.

## Execution and correctness flow

The collector runs the scale-major matrix in this fixed order: Vanilla, AMU,
CIRA, and M2NDP for each of g4, g12, g14, and g20. It uses no wall-clock
timeout. The process runs as a background service with logs and an explicit
terminal state file. Resume is allowed only between passed matrix points and
only when the entire evidence identity still matches; periodic simulator
checkpointing is not reintroduced.

Every point must prove from generated configuration and result evidence:

- four gem5 timing cores and four workload threads where applicable;
- CXL delay exactly 1,000,000 ticks;
- all workload allocations inside the CXL range;
- exactly two trials, with trial 0 as complete CXL warmup and trial 1 measured;
- synchronous double buffering and exactly 20 PageRank iterations;
- complete mechanism drain and final synchronization inside the timed end;
- identical output element count, full rank-vector raw bits, and checksum;
- AMU parameters selected by the accepted calibration record; and
- all required M2NDP translation, FuncSim, and NDPSim stages passing.

A failed or missing bit-exact proof makes the point failed and excludes its
timing. The runner does not skip ahead and call a partial matrix complete.

## Raw data and plot family

Once all 16 points pass, one fail-closed publication command atomically emits
the following artifacts from the complete evidence JSON:

1. `pr-scaling-raw.json`: the lossless source records for all 16 points,
   including absolute latency, exact native tick/cycle counts, speedup
   numerator and denominator, checksums, output hashes, mechanism evidence,
   configuration identity, input/calibration hashes, and evidence paths;
2. `pr-scaling-raw.csv`: one rectangular row per point with the same numeric
   fields and provenance hashes needed for independent plotting;
3. `pr-scaling-speedup.{pdf,svg}`: speedup versus graph scale for AMU, CIRA,
   and M2NDP, with Vanilla fixed at 1.0x;
4. `pr-scaling-latency.{pdf,svg}`: absolute end-to-end latency versus graph
   scale for all four systems using a logarithmic y-axis;
5. `pr-scaling-grouped.{pdf,svg}`: grouped per-scale normalized comparison;
6. `pr-scaling-heatmap.{pdf,svg}`: system-by-scale speedup heatmap; and
7. `pr-scaling-table.tex`: exact absolute latencies and computed speedups for
   paper inclusion.

The raw CSV stores latency as exact decimal seconds plus integer simulator
ticks where the backend provides ticks. Speedup is recomputed as matched
Vanilla latency divided by accelerated latency; it is never treated as a
source measurement. The JSON manifest records SHA-256 for every emitted file.
Publication uses a temporary sibling directory, validates all outputs, and
renames the complete set atomically so a crash cannot leave a mixed figure/raw
data version.

Only measurements present in the formal evidence may be visualized. The plot
tool may change labels, axes, colors, and layout, but it may not interpolate
missing points, invent confidence intervals, or infer component breakdowns
that the runner did not measure. The eventual combined scaling-plus-breadth
paper figure remains blocked until the breadth evidence independently passes.

## Failure behavior

The pipeline fails closed on malformed JSON, unknown schema/scope/profile,
missing or reordered scales, graph or manifest hash drift, graph header/scale
disagreement, calibration drift, evidence-root reuse with a different
identity, config mismatch, non-CXL placement, incomplete backend stages,
non-finite timing, output-count mismatch, any raw-bit mismatch, or a matrix
other than the exact 16 points.

Failure records contain the stage, deterministic reason, identity digest, and
log paths. They never contain a partial `complete` object. Existing breadth
`failed-input.json` remains untouched and is not referenced as scaling input.

## Test strategy

Tests are written before implementation and cover:

- accepting only an exact scoped four-graph manifest;
- rejecting a breadth manifest, reordered graph list, altered graph-set hash,
  endpoint hash drift, graph-header drift, missing generator, and changed live
  file hash;
- adopting g4/g20 without modifying graph bytes and refusing overwrite or
  unsupported scale/hash combinations;
- preserving strict failure of the six-workload freezer when its paper input
  record is absent;
- requiring a fresh root after input or calibration identity drift;
- rejecting each config, placement, stage, output-count, and bit-exact failure;
- accepting exactly 16 passed formal points;
- recomputing every speedup from absolute latency;
- producing deterministic raw JSON/CSV, LaTeX, PDF, and SVG artifacts; and
- rejecting mixed scaling/breadth calibration or g20 graph identities while
  permitting distinct scoped input-manifest hashes.

The focused unit suite is followed by the complete Python test suite and the
existing C++ static/compile gates touched by the runner. The background formal
run starts only from a clean committed revision pushed to the corresponding
remote branch.

## Out of scope

This change does not synthesize breadth workloads, relax their minimum sizes,
alter CIRA/AMU/M2NDP performance mechanisms, tune parameters from simulator
speedups, add periodic live checkpoints, replace failed points with analytical
estimates, or publish the combined paper figure before breadth evidence passes.
