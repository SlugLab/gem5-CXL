# MCFREG2 Strict Kernel-Live-In Replay Design

Date: 2026-08-25

Status: approved approach; written-spec review pending

## Purpose and supersession

This document strengthens the semantic-proof boundary in
`2026-08-25-mcfreg2-formal-state-package-design.md`. The original source,
input, ROI, capacity, determinism, atomic-publication, and formal-input
requirements remain in force. Where the earlier document permits a replay
check to be satisfied from self-consistent journal rows, this revision
supersedes it.

The formal claim is deliberately scoped to the kernels proposed for
offload. Native MCF remains the authority for non-offloaded simplex work and
for the state evolution between kernel calls. The package supplies native
kernel live-ins; an independent implementation must derive every kernel
decision and live-out from those live-ins. This is not a second
implementation of the complete MCF solver.

The resulting proof must support the following statement:

> For every `primal_bea_mpp()` and `price_out_impl()` invocation reached by
> the approved native ROI, an independently implemented replayer consumed
> the captured native live-in state, reproduced the kernel's ordered
> decisions and complete live-out state bit-for-bit, and emitted the
> canonical operation trace used by the cross-system comparison.

It must not claim that a package is cryptographically unforgeable by an
actor who can replace every evidence file and every trusted hash record.
Artifact authenticity comes from the frozen generator revision and retained
native evidence tree. Semantic validation prevents a journal from passing by
copying its asserted outputs into its validator.

## Rejected alternatives

Three alternatives are rejected:

1. **Current row-consistency replay.** Recomputing reduced cost from operands
   stored in the same `SCAN` row, checking only that a basket is sorted, or
   hashing selected journal rows does not execute the native kernel.
2. **Full solver replay.** Independently reimplementing every simplex pivot
   and all state evolution between calls would prove a different and much
   larger system than the offload trace under evaluation.
3. **Post-state snapshots only.** Comparing captured pre/post snapshots
   without independently applying the kernel admits an output trace with no
   executable semantics.

The selected design is strict kernel-live-in replay.

## Evidence and trust boundaries

The evidence chain has four distinct layers:

1. **Immutable source/input authority.** One canonical frozen tracked-tree
   snapshot is cloned for the two patch stacks, and one copied approved input
   object is the only input used by all native runs.
2. **Native authority equivalence.** The uninstrumented authority build and
   two instrumented capture runs must produce identical canonical final
   state and `mcf.out` bytes.
3. **Kernel semantic replay.** A separately compiled C++ implementation uses
   captured live-ins but never captured decisions as inputs. It derives and
   compares all decisions and live-outs.
4. **Formal qualification.** The freezer reruns that semantic replayer and
   independently checks the package, native trees, identities, capacity,
   and hashes before accepting a record.

A failure in any layer prevents qualification. A structural package parse
is never a substitute for layers 2 through 4.

## Immutable source and build identity

`freeze_source()` records each tracked relative path, size, and SHA-256
before copying. Every copied file is checked against that recorded row, not
against a second read of the mutable source. It returns the resolved copied
input path and its already-recorded SHA-256.

The authority, primary-capture, and replay-capture processes all receive that
same copied input. No process may reopen the original checkout's input after
the freeze gate. A post-freeze mutation of the original file therefore has
no effect on execution; a mutation of the copied input fails before launch.

Package identity is derived from build and run records, not supplied as an
unchecked caller dictionary. It binds:

- source commit, tracked-tree digest, tracked rows, and copied-input digest;
- common patch, capture patch, `mcf_capture.c`, `mcf_capture.h`, and wire ABI;
- compiler executable hash, version, target, ABI, flags, complete command,
  linked source/object hashes, and produced binary hash;
- authority/capture mode and ROI marker contract; and
- generator, Python parser, C++ reader/replayer, and formal-validator hashes.

The generator reconciles all three run records with this derived identity
before assembly.

## Call-frame model

Each call frame separates four kinds of data. A typed record cannot serve in
more than one role.

### Live-in records

Live-in records are native observations made before the kernel computes the
corresponding result. They include stable identities and raw integer words,
never raw pointers.

For pricing, the live-in stream contains:

- call ordinal, `m`, group count/position, initialization state;
- complete retained basket live-in in slot order;
- for every native scan, stable arc, tail, and head IDs plus the live
  `cost`, `ident`, tail potential, and head potential words; and
- the exact scan position needed to validate group-stride traversal.

The per-scan values are necessary because non-offloaded simplex work may
change potentials and identities between pricing calls. They are kernel
inputs, not asserted kernel outputs.

For price-out, the live-in stream contains one complete normalized call-entry
network view sufficient to execute `price_out_impl()`: network scalars,
relevant node fields, all referenced arc fields and links, sparse-list source
relationships, arena generation/capacity, and residual-new-arc heap state.
The reference input reaches one price-out invocation, so this complete
call-entry snapshot does not multiply across millions of calls.

### Observed-result records

Observed-result records contain the native outcome to be compared, including:

- pricing candidate flags, basket insertions/retention/order, selected arc,
  selected reduced cost, group-position update, and priced count;
- price-out candidate order, no-change/insert/replace decisions;
- complete finalized arc records after heap movement and after `flow` and
  `ident` initialization;
- complete old-to-new arena remap entries;
- all finalized `nextout`, `nextin`, node `firstout`/`firstin`, and affected
  tree/reference relationships; and
- final network counters and remaining capacity.

Capture hooks for results execute only after native code has finalized the
recorded fields. Observation hooks never drive native control flow.

### Derived live-out records

The C++ replayer builds derived live-out state solely by executing its own
kernel implementation over live-in state. It may not copy a field from an
observed-result record into derived state. Observed results are used only in
comparisons after derivation.

### Boundary records

The native pre-boundary digest is over the typed live-in state, including all
stable relationships and raw words. The native post-boundary digest is over
the complete finalized observed live-out state. It is not a digest of a
selected JSON row list.

The replayer independently serializes its reconstructed live-in and derived
live-out with the canonical binary state encoder and compares both digests.
It also compares every typed result field, so digest agreement is defense in
depth rather than the sole semantic check.

## Independent pricing execution

For each pricing call, the replayer:

1. reconstructs the retained basket from live-in records;
2. derives the required group-stride scan order from `m`, group count, and
   starting group position;
3. requires one and only one live-in scan record for each derived position;
4. computes `cost - tail_potential + head_potential` with checked native
   signed-width behavior;
5. derives eligibility from the computed sign and live-in arc identity;
6. executes native basket insertion, retention, comparator, sort, and
   selection behavior in native order; and
7. compares all derived outputs with observed-result records and the complete
   post-boundary digest.

Changing an asserted reduced cost, candidate flag, basket order, selected
arc, or count must fail even if the attacker updates the other asserted
outputs. Changing a live-in operand changes the pre-boundary digest and must
also produce a newly derived result; it cannot be hidden by copying an
observed result into replay state.

## Independent price-out execution

For each price-out call, the replayer reconstructs the call-entry network and
executes the native algorithm independently, including:

- resize predicate and checked capacity growth;
- complete stable-reference remapping after arena relocation;
- sparse-list construction and traversal order;
- candidate tail/head selection and reduced cost;
- `NO_CHANGE`, `INSERT`, and `REPLACE` decisions;
- the exact residual-new-arc heap insertion/replacement operations;
- finalized `flow`, `ident`, costs, endpoints, and heap contents;
- adjacency refresh or incremental adjacency insertion; and
- updates to `stop_arcs`, `m`, `m_impl`, and `max_residual_new_m`.

The replayer then compares its candidate sequence, every decision, every
finalized arc/link field, network counters, and canonical post-boundary digest
with native observations. A missing remap entry, stale generation, wrong heap
slot, incomplete adjacency link, or plausible but incorrect `NO_CHANGE`
decision is fatal.

## Container and streaming

The external format remains `MCFREG2`; the binary schema and affected section
schemas advance so old packages fail closed. Formal `MCFREG1` and the earlier
weak MCFREG2 schema remain fixture-only and cannot qualify.

The section model retains `PROVENANCE`, `NETWORK`, `NODES`, `ARCS`, `BASKET`,
`CALL_INDEX`, `EVENTS`, `DELTAS`, `BOUNDARIES`, and `FINAL`. Record schemas
inside `EVENTS` and `DELTAS` distinguish live-in, observed-result, remap, and
finalized-state roles. No new section is needed unless implementation tests
show that role separation cannot be enforced with section schemas alone.

Python and C++ readers are offset-backed. Header, directory, section hashes,
and large semantic streams are processed incrementally. Formal qualification
must not read the 14+ GB package twice or retain the complete event section in
memory. The replayer may retain the bounded mutable state needed for one
active call, but memory use may not scale with total event count.

## Capacity proof

Allocation events record allocation kind, element count, native element size,
old/new capacity where applicable, requested bytes, and the resulting current
and peak totals. The validator recomputes checked products and the complete
allocation timeline. It requires:

- exact agreement with native run records and `FINAL`;
- consistent arena capacity and resize events;
- exact primary/replay equality; and
- peak requested bytes of 1,757,471,072 for the approved reference run and at
  least the 345,000,000-byte paper threshold.

A copied peak number without a valid allocation timeline cannot qualify.

## Formal qualification and accepted-root handling

`validate_mcf_record()` performs structural checks and launches the strict
C++ replayer in hash-only mode. It requires exact call counts, zero boundary
mismatches, and the same canonical trace SHA-256 recorded by generation. Test
fixtures used by audit/freezer tests must contain valid executable semantics;
arbitrary bytes in semantic sections are rejected.

An existing content-addressed root is reusable only after the same complete
verification used by `verify --accepted`: package-name hash, all manifest and
source identities, every published-tree hash, capacity timeline, and a fresh
strict replay. Partial identity/package/validation comparison is insufficient.

The current root
`2d3ad115b8a83afa7ba94e507c33d65ea7bc8ac811faa8d262828e0c81b1065b`
was produced by the weaker replay. It must never enter a formal paper record.
Before regeneration it is atomically renamed to a dot-prefixed rejected root
and accompanied by a rejection record containing the code-review finding,
old hashes, and reason. It is preserved for audit rather than deleted.

## Failure evidence

Every attempt writes a unique content-addressed or UUID-suffixed failure
record. The record preserves the first failing gate, derived identity,
commands, exit status, log paths, and hashes of all dependencies reached by
that gate. A summary pointer may identify the newest failure but may not
overwrite earlier records.

The outer generator preserves an inner gate such as `semantic_replay`,
`capture_determinism`, or `frozen_input` instead of replacing it with generic
`generation`.

## Adversarial tests

In addition to existing format and native-equivalence tests, the following
mutations must fail at the strict semantic gate:

- coupled changes to reduced cost, candidate flag, basket output, and selected
  arc that remain internally self-consistent;
- changed live-in potential or identity with recomputed asserted outputs;
- wrong scan order or retained-basket slot;
- negative-cost `NO_CHANGE`, wrong insert/replace threshold, and wrong heap
  replacement slot;
- omitted or duplicated finalized arc, adjacency, or remap entries;
- stale-generation references after resize;
- forged allocation peak or inconsistent allocation timeline;
- source mutation after freeze and copied-input mutation before launch;
- corruption of any file in an existing accepted root; and
- a structurally valid package with arbitrary semantic section bytes.

Tests also measure formal-sized reader RSS and require it to be bounded by
active-call state rather than package/event size.

## Completion boundary

The revised work is complete only after:

1. all blocking provenance, semantic replay, capacity, root-reuse, failure,
   template, and streaming fixes pass their focused and adversarial tests;
2. authority, primary, and replay native runs are repeated from the immutable
   copied input;
3. a new content-addressed package is generated from the stronger schema;
4. generation and a separate formal-freezer invocation both run the strict
   replayer and obtain identical canonical trace hashes with zero boundary
   mismatches;
5. the complete cross-system and M2NDP regression suites pass; and
6. the implementation branch is reviewed and pushed while the user's
   `src/mem/cache/base.cc` remains unstaged and unchanged.

The current accepted root, the current zero-mismatch result, or passing
structural tests alone do not satisfy this boundary.
