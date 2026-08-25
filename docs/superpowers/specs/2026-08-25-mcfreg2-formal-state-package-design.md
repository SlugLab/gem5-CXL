# Formal MCFREG2 State Package Design

Date: 2026-08-25

## Goal

Generate a deterministic, non-synthetic state package from the authoritative
SPEC-derived MCF source and reference input. The package must preserve every
pricing and price-out invocation reached by a complete `global_opt()` run and
must support independent, bit-exact validation before it can become a formal
cross-system paper input.

The authoritative source and input are:

- source directory: `/home/victoryang00/CXLMemUring/bench/mcf`;
- enclosing source revision: `2b30de22399402d8c44bd74b8ebf743b6a6a55e9`;
- input: `data/ref/input/inp.in`;
- input size: 3,261,937 bytes; and
- input SHA-256:
  `aceb933893790cd957ec9d03d34660ba756a70d87b65caa9809e3a48443ba849`.

The 36 tracked files under `bench/mcf` are path-clean at design time. Dirty or
untracked content elsewhere in the enclosing CXLMemUring checkout is outside
this source identity and must be preserved. The user's existing modification
to `src/mem/cache/base.cc` in the gem5 worktree is also outside this work and
must not be changed, staged, or committed.

This design covers capture, package generation, independent replay, and
formal-input qualification. It does not itself produce or publish AMU, CIRA,
or M2NDP performance numbers.

## Rejected shortcuts

The current `MCFREG1` format and `mcf_regions.cc` model a small synthetic
pricing/index workload. They omit native arc identity, persistent basket
state, network topology, implicit-arc construction, replacement, resize, and
the simplex-driven state changes between calls. Their reduced-cost convention
also differs from the authoritative MCF expression. A text-to-`MCFREG1`
converter could pass structural checks while representing the wrong program.

The following alternatives are therefore rejected for formal evidence:

- converting `inp.in` directly into the existing `MCFREG1` arrays;
- saving one call or a fixed invocation window and calling it end-to-end MCF;
- saving complete per-call network snapshots while omitting native host work
  from the performance accounting; and
- using the simplified random MCF benchmark in the IA-780I hardware tree.

`MCFREG1` remains supported only for existing unit fixtures and smoke tests.
It is never accepted by the formal input freezer.

## ROI contract

The complete MCF compute ROI begins immediately after
`primal_start_artificial()` returns and ends when `global_opt()` returns. Input
parsing, initial allocation, artificial-tree construction, and `mcf.out` file
I/O are outside the ROI. Every invocation of `primal_bea_mpp()` and
`price_out_impl()` reached within the ROI is captured in native program order.

This boundary has two related uses:

1. Native host execution supplies the full state evolution and accounts for
   all simplex and other non-offloaded work.
2. The state package describes the complete sequence of candidate offload
   regions and their exact live-in/live-out contract.

The package must not insert correctness-only state patches into a timed path.
When later used for system comparison, the real host program must execute the
non-offloaded portions, and formation, transfer, execution, writeback,
completion, and drain must all remain charged inside the full ROI.

## Architecture

### Source freezer and build harness

The generator first freezes the exact tracked file list and per-file hashes
under `bench/mcf`. It records the enclosing Git revision and path-scoped status
without requiring unrelated parts of the enclosing checkout to be clean.

The current MCF entry point hard-codes an input path, so both native builds use
one common, deterministic harness patch that:

- selects the approved input through an explicit command-line argument;
- places ROI markers at the approved boundaries; and
- emits a canonical final-state dump for comparison.

The authority binary receives only this common harness patch. The capture
binary receives the same harness patch plus the capture instrumentation patch.
Both patch contents and hashes are recorded. Builds happen in a
content-addressed temporary source copy; the authoritative checkout is never
edited. Compiler path, version, target, ABI, flags, and linked objects are
recorded, and the two builds use identical optimization and ABI flags.

### Native capture

Capture hooks observe native execution without replacing native decisions.
They assign stable IDs to nodes, active arcs, dummy arcs, and successive arc
arena generations. Raw pointers and structure padding are never serialized.

At the ROI entrance, capture records the complete normalized network state,
including network metadata, nodes, active arcs, dummy arcs, adjacency links,
tree links, arc identities, original costs, flows, potentials, depths, and
the persistent pricing basket state.

For each pricing call, capture records:

- invocation ordinal and entry state digest;
- group count, group position, initialization state, and basket live-in;
- the exact group-stride arc scan order;
- each arc's stable ID and the native reduced-cost inputs;
- basket insertion, retention, sorting, and selection results;
- selected arc ID, reduced cost, and number of arcs priced; and
- live-out delta and exit state digest.

For each price-out call, capture records:

- invocation ordinal and entry state digest;
- sparse-list traversal and ordered candidate `(tail, head)` IDs;
- reduced-cost inputs and branch decision;
- insert, weaker-arc replacement, or no-change outcome;
- resize, arena-generation, and stable-ID remapping events;
- adjacency-list and network-metadata mutations;
- new-arc count and remaining capacity; and
- live-out delta and exit state digest.

Native code continues to make every decision and perform every mutation.
Capture data describes what occurred; it does not drive the authority run.

### Independent reader and replayer

A C++ `MCFREG2` reader/replayer is implemented independently of the native
MCF functions. It reconstructs pointer-free state from stable IDs, calculates
the authoritative reduced-cost expression, reproduces basket behavior, and
executes price-out insert/replace/resize semantics in the recorded order.

The replayer checks every recorded input, decision, live-out delta, and
boundary digest. It must reject an event stream that merely contains the
expected outputs but lacks the inputs needed to recompute them. A separate
Python parser validates the binary container and reconstructs the same
boundary metadata without linking the C++ reader.

## MCFREG2 container

`MCFREG2` is a single little-endian binary container. It begins with a fixed
header and section directory. The header contains:

- magic `MCFREG2\0`, schema version, endian tag, header size, and flags;
- section count and directory offset;
- node, initial-active-arc, dummy-arc, and arena-capacity counts;
- pricing-call, price-out-call, and event counts; and
- reserved fields that must be zero and are covered by validation.

Each directory entry contains a section type, section schema, flags, file
offset, stored byte length, logical element count, logical element size, and
SHA-256. Offsets and lengths use checked 64-bit arithmetic and sections may
not overlap. Unknown mandatory sections are errors; unknown explicitly
optional sections may be skipped after hash validation.

Required sections are:

- `PROVENANCE`: source, input, patches, toolchain, command, and ROI identity;
- `NETWORK`: normalized scalar `network_t` fields;
- `NODES`: pointer-free initial node records;
- `ARCS`: pointer-free active and dummy arc records;
- `BASKET`: persistent pricing state at ROI entry;
- `CALL_INDEX`: phase, ordinal, generation, and event ranges;
- `EVENTS`: ordered pricing and price-out semantic events;
- `DELTAS`: typed live-out mutations referenced by call frames;
- `BOUNDARIES`: pre/post canonical state hashes and key scalar results; and
- `FINAL`: final network and `mcf.out` size/hash evidence.

Stable references contain an object kind, arena generation, and object ID.
One explicit sentinel represents null. Resizing increments the generation and
emits a complete old-to-new remap before any event may reference the new
generation.

The package stores the initial normalized state once. Per-call live-in fields,
semantic events, live-out deltas, and boundary digests avoid duplicating the
entire network for every invocation. Unused allocated arc capacity is encoded
as capacity metadata rather than serialized zero-filled records; newly
activated slots are fully initialized by explicit events.

## Generation flow

The generator performs these steps in order:

1. Validate the source file set, path-scoped cleanliness, source revision,
   input path, size, and input hash.
2. Preflight the output filesystem for required temporary and final space.
3. Create a content-addressed temporary source copy and apply the common
   harness patch.
4. Build and run the authority binary on the approved input.
5. Apply the capture patch to another copy, build the capture binary with the
   same toolchain contract, and run it twice.
6. Require matching exit status, normalized final state, and `mcf.out` between
   authority and both capture runs.
7. Require the two capture runs to have identical call counts, event contents,
   boundary hashes, and final package bytes.
8. Read the completed package through both the independent C++ replayer and
   Python validator and require every boundary to pass.
9. Reopen every emitted artifact, recompute hashes, and write a validation
   manifest.
10. Atomically rename the verified temporary directory into its
    content-addressed final location.

The intended root is:

```text
/mnt/disk0/gem5-CXL-eval/cira-amu-m2ndp-spectrum/formal-inputs/mcf/
  <package-sha256>/
    mcf.reg2
    manifest.json
    validation.json
    authority/
    capture-primary/
    capture-replay/
```

The run directories retain commands, stdout, stderr, binaries or binary
links, canonical final-state dumps, `mcf.out` hashes, and intermediate trace
hashes. The manifest uses resolved absolute paths and hashes for every formal
dependency.

## Capacity evidence

The formal MCF gate is based on real allocated working storage, not the input
file size or an advertised address range. The common harness records every
successful node, dummy-arc, and arc `calloc`/`realloc` request using checked
element-count times native `sizeof` values. It reports current and peak
requested bytes and the exact allocation formula.

The package qualifies only when the observed peak is at least 345,000,000
bytes. Capacity metadata must agree with the normalized network state and
resize events. The validator independently recomputes the requested-byte
totals from recorded counts and ABI sizes.

## Correctness gates

All checks are exact. No numeric tolerance is permitted because the relevant
state uses integer types. Success requires:

- authority and capture exit code zero;
- authority, capture-primary, and capture-replay final-state equality;
- authority, capture-primary, and capture-replay `mcf.out` equality;
- identical primary/replay invocation counts, event streams, deltas, and
  package bytes;
- independent C++ replay equality at every call boundary;
- independent Python validation of every header, section, count, hash, ID,
  generation, offset, and length;
- exact EOF with no overlapping or trailing unaccounted data;
- observed allocated bytes at or above the formal threshold; and
- a manifest whose recomputed artifact hashes all match.

The canonical state digest includes every semantic scalar and stable
relationship but excludes raw addresses, allocator metadata, structure
padding, wall-clock values, and output paths. Provenance contains no volatile
timestamp in the package identity so identical runs can produce identical
package bytes.

## Failure handling

The generator is fail-closed. It rejects source or input drift, non-LP64 ABI,
integer overflow, allocation failure, malformed topology, invalid stable IDs,
generation misuse, unknown mandatory events, short reads or writes, section
overlap, digest mismatch, nondeterministic capture, authority/capture output
drift, replay mismatch, and insufficient capacity.

Artifacts are written beneath a unique temporary directory and fsynced before
publication. A failure removes no prior evidence and never creates the final
content-addressed directory. Instead it atomically writes a diagnostic
`failed-input.json` beside the attempted root with the first failing gate,
command, relevant log paths, and dependency hashes. It must not contain a
package path marked as accepted.

Generation emits a candidate record only. `paper-input-record.json` is not
created or edited automatically. A later formal-freezer step may bind the
verified package only after it rechecks the package, manifest, source, input,
capacity, and validation hashes. Formal builders reject `MCFREG1`, fixtures,
and unqualified `MCFREG2` files.

## Test strategy

Unit fixtures cover:

- empty and retained pricing baskets across calls;
- group-position wraparound and multiple group-stride scans;
- positive and negative reduced costs at each arc identity;
- price-out no-change, insert, weaker replacement, and capacity exhaustion;
- arc-arena resize and generation remapping;
- adjacency and tree-link normalization;
- null references and maximum legal IDs;
- truncated, overlapping, reordered, unknown, or hash-corrupt sections;
- arithmetic overflow, out-of-range IDs, stale generations, and trailing
  bytes; and
- primary/replay nondeterminism detection.

Integration tests build the authority and capture binaries from a small
fixture input, compare all three native outputs, and run both independent
validators. A deliberate instrumentation fault must be detected at the first
divergent boundary.

The final proof run uses the approved reference input. It must pass the full
authority/capture-primary/capture-replay matrix, independently replay every
captured call, prove the 345,000,000-byte allocation threshold, and publish
the content-addressed artifacts. Existing matched-workload and formal-input
tests must continue to pass, with `MCFREG1` restricted to fixture mode.

## Completion boundary

This task is complete only when a content-addressed `mcf.reg2` exists at the
formal evidence root and its validation manifest proves all correctness,
determinism, provenance, and capacity gates. A successful build, a structurally
valid container, or a native run without independent replay is not completion.
