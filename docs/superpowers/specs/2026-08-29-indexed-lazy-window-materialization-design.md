# Indexed Lazy Window Materialization Design

Date: 2026-08-29

## Decision and scope

Add a bounded, random-access materialization path for the formal MCF, NPB CG,
and NPB MG traces. The path will let the six-workload breadth runner execute
selected paired timing windows without eagerly expanding the roughly 50 GB MCF
canonical trace or walking the NPB descriptors' trillions of primitive
operations from their beginning for every window.

This design supplies the executable action interface already described by
`prepare_native_verified_breadth_suite.py`. It preserves the six workloads,
four systems, four CXL latencies, four host threads/workers, all-CXL placement,
paired stratified timing, and raw-data requirements in the approved spectrum
designs. It follows the relaxed correctness amendment: bit-exact evidence is
retained when available, but a timing point may instead pass workload-native
numerical verification when every structural and mechanism gate passes.

This work does not change workload algorithms, synthesize performance,
interpolate missing latency points, expand an entire lazy trace, or copy a
timing result across systems or latencies. PageRank, AMG Gather, and LULESH
retain their current materialization implementations.

## Rejected alternatives

Eager expansion is rejected because it exceeds the intended storage boundary
for MCF and is not finite in practice for the formal NPB operation counts.

Repeated sequential lazy replay from primitive zero is rejected because the
formal CG and MG descriptors contain 9,121,454,144,100 and
5,535,553,556,570 primitive operations. It cannot reach stratified late
windows in a bounded preparation interval.

Recapturing a complete native run independently for every timing coordinate is
rejected because the coordinate is latency-independent, duplicates expensive
work, and makes crash recovery and provenance harder. One content-addressed
index and the minimum sparse state needed by selected coordinates are shared
read-only across latency campaigns.

## Architecture

Add one action driver, `scripts/run_prepared_breadth_action.py`. It validates a
prepared manifest, resolves exactly one requested action, and dispatches to
the existing reference, FuncSim, gem5, or NDPSim implementation. It does not
implement workload arithmetic or simulator behavior.

The materialization layer has workload-specific index producers behind one
interface:

- MCF indexes `.mcfreg2` section and event offsets and seeks directly to the
  requested phase and safe window boundary.
- NPB indexes invocation primitive counts using closed-form kernel cardinality
  functions. Prefix counts locate a requested global primitive with binary
  search rather than expanding preceding invocations.
- CG and MG dependency resolvers identify the array elements, scalars, and
  stencil halo needed between a selected warmup boundary and measurement stop.
  A native boundary capture supplies those raw values as a sparse state image.

Sparse-state collection is a two-pass preparation step. The structural pass
resolves all selected coordinates and their dependency closures without
reading or modifying state. One subsequent native execution per workload uses
that immutable dependency list to dump raw values at every selected safe
boundary while also running the official full-workload verifier. This
capture-only binary, its instrumentation source, command, dependency-list
hash, complete stdout, exit status, and final native boundary commitments are
part of the preparation identity. It emits no timing result and is never run
once per system or latency.

Only the requested `warmup_start` through `measure_stop` interval is emitted.
Dynamic and fixed traces are generated as a pair, authenticated, and passed to
the existing system runner. Shared indexes, sparse states, references, and
functional outputs are immutable; timing output remains latency-local.

## Immutable artifacts

### Lazy index

`lazy-index.v1.json` contains:

- workload, phase, descriptor path and SHA-256;
- input, source, binary, configuration, and index-generator SHA-256 values;
- total primitive count;
- ordered invocation segments with primitive begin/end offsets, ordinal,
  phase, iteration, kernel, and work-item count; and
- legal cut points that do not split a reduction, sparse row, stencil update,
  barrier, or commit.

Segments must be strictly ordered, non-overlapping, and cover the declared
primitive range exactly. Every count is an integer derived from a checked
kernel-cardinality function. Unknown kernels or arithmetic overflow fail.

### Window plan

`window-plan.v2.json` binds the timing-plan SHA-256 and records, for every
selected coordinate:

- requested and realized warmup, measure-start, and measure-stop offsets;
- phase, level, stratum, and window index;
- containing invocation segments and safe-cut reasons;
- exact warmup and measured primitive counts; and
- the lazy-index SHA-256.

Alignment may move a requested offset only within its original sampling
stratum. An aligned window that is empty, crosses a phase, or leaves its
stratum is rejected rather than silently replaced.

### Sparse state

Each `sparse-state.v1/` directory contains `manifest.json` and one canonical
binary payload and stores only the dependency closure needed to replay one
realized window. Entries identify array name, logical index/address, element
width, and raw word. Scalar values and stencil halo entries use the same raw
representation. The manifest binds the native boundary identifier, source
binary, capture instrumentation, dependency list, invocation, requested
coordinate, realized coordinate, payload size, and payload SHA-256.

The dependency resolver must prove that every load in warmup and measurement
has an initial raw value or a preceding store in the materialized interval.
Missing or duplicate values, overlapping logical addresses, a changed native
boundary commitment, or an unrecognized data-dependent address rejects the
window.

The total indexes, sparse states, and materialized windows for one workload
have a default 512 MiB retained-storage limit. Temporary files count toward
the limit. Exceeding it fails preparation and never falls back to full-state
copying or eager trace expansion.

### Materialized window

`materialized-window.v2.json` binds the descriptor, index, window plan, sparse
state, dynamic trace, and fixed trace hashes. It records exact operation
cardinality, warmup and measurement boundaries, output-boundary definitions,
and whether functional comparison was bit-exact or numerically verified.

## Action data flow

For `reference`, the driver invokes the prepared workload's existing native
reference path and emits authenticated boundary records.

For `functional`, PageRank, MCF, AMG Gather, and LULESH retain their existing
complete functional path. Expanding every formal CG or MG primitive for four
functional backends is outside the bounded design. Their functional action
instead binds the successful full native execution and official verifier,
then runs every requested Vanilla, AMU, CIRA, or M2NDP FuncSim backend on the
same real qualification window. That window must be bit-exact for all four
systems before NPB timing begins. Functional evidence is latency-independent
and shared only when all input and producer hashes match. The paper artifact
labels NPB timing as paired-window evidence and does not describe it as a full
simulator execution of the Class D operation stream.

For `window`, the driver:

1. validates the prepared manifest and exact action command;
2. loads or atomically creates the workload index;
3. resolves the requested coordinate to legal cut points;
4. loads sparse native state committed by the single preparation capture;
5. materializes and validates the dynamic/fixed trace pair;
6. invokes the selected backend with the declared CXL latency;
7. checks correctness, placement, completion, and error counters; and
8. atomically publishes the action evidence expected by
   `ManifestExecutor`.

An action never mutates a shared object after publication. Resume accepts a
shared artifact only when its complete semantic identity and file hashes
match.

## Failure handling

Artifacts are built in a uniquely named temporary directory and renamed into
place only after validation and file synchronization. A crash leaves no
publishable artifact. Existing invalid or mismatched evidence is preserved
under the runner's retry naming convention and is not reused.

All of these are terminal for the current action:

- descriptor, input, source, binary, configuration, calibration, index, plan,
  state, trace, or evidence hash drift;
- malformed or incomplete prefix coverage;
- an unsafe or cross-stratum coordinate;
- incomplete sparse dependencies or a native-boundary mismatch;
- retained storage above 512 MiB;
- missing native verification, non-finite or out-of-tolerance output;
- unbalanced requests, completions, launches, barriers, or drains;
- nonzero AMU/CIRA queue, descriptor, translation, or pending errors;
- M2NDP FuncSim failure, memory mismatch, or failure of its approved minimum
  performance gate; or
- a core/thread count, all-CXL placement, or emitted-latency mismatch.

No failure produces a numeric publication point. A fix that changes semantic
identity starts a fresh evidence root.

## Correctness and publication gates

Address, opcode, dependency, ordering, phase, cardinality, and output-shape
contracts remain exact for every materialized window. At least one early real
CG and one early real MG qualification window must match bounded sequential
expansion and the native sparse-state boundary word for word on Vanilla, AMU,
CIRA, and M2NDP FuncSim before their timing campaigns can start. Every later
selected window must independently match its native-captured sparse input
words and structural index; it is not compared by replaying trillions of
preceding primitives.

Every formal timing window must pass the workload-native verifier. Floating
output may be accepted through the approved workload tolerance when it is not
bit-exact; the evidence and eventual paper caption must label that condition.
Integer and index output never use a numerical tolerance. Available bit-exact
results remain recorded as stronger evidence.

AMU and CIRA require balanced issued/completed activity, complete drains, and
zero mechanism errors. M2NDP requires FuncSim correctness, expected/completed
launch equality, memory-match success, calibrated latency binding, and the
approved minimum-performance policy. Fixed formation, synchronization,
completion, drain, and final-commit costs remain inside end-to-end latency.

## Test strategy

Tests are written before implementation and cover:

1. exact prefix counts and random seeks against sequential expansion on small
   MCF, CG, and MG fixtures;
2. safe alignment at row, reduction, stencil, phase, barrier, and commit
   boundaries;
3. sparse dependency closure, including CG indirect indexes and MG halos;
4. rejection of gaps, overlaps, overflow, hash drift, missing values,
   duplicate addresses, cross-stratum alignment, and storage-limit overflow;
5. atomic creation, crash residue, invalid-evidence preservation, and exact
   resume identity;
6. action-driver command rendering and evidence compatibility with
   `ManifestExecutor`;
7. existing PageRank, AMG, and LULESH paths as regression cases; and
8. real formal CG and MG qualification windows before any full latency
   collection.

## Acceptance

This feature is complete when:

1. all new unit, property, corruption, resume, and integration tests pass;
2. the actual formal MCF, CG, and MG indexes pass independent validation
   without eager expansion;
3. actual CG and MG qualification windows pass the structural and native
   correctness gates;
4. the complete prepared six-workload manifest contains executable reference,
   functional, and timing actions;
5. a fresh breadth preflight accepts all six workload identities and four
   latency roots;
6. no formal artifact exceeds the retained-storage contract; and
7. the implementation commit excludes the user's unrelated
   `src/mem/cache/base.cc` modification and `.superpowers/` directory.
