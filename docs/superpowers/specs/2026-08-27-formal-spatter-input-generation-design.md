# Formal Spatter Input Generation Design

## Goal

Create reproducible CXL-scale binary inputs for the paper's AMG Gather and
LULESH Scatter workloads. The artifacts are generated inputs derived from the
official Spatter application traces, not hardware-captured data. They must be
large enough to satisfy the existing 1 GiB formal-input gate and must remain
bit-exact across reference, AMU, CIRA, and M²NDP execution.

## Source authority

The only pattern authority is the Spatter checkout at commit
`ec8923711f8dc21eedff7189f12b02eb06845d2f`:

- `standard-suite/app-traces/amg.json`, SHA-256
  `3ebf359a0976532c04cebd3cb4432589c2c9ec3d7b6fe61661c042f6adc2121c`
- `standard-suite/app-traces/lulesh.json`, SHA-256
  `9073035ecf77e7fde65262f782286207e76cca24312b2e01688b038901d021ee`

The generator rejects a different source hash, malformed records, negative
indices, unsupported kernels, or integer overflow. The resulting paper record
describes the artifacts as deterministically expanded from official Spatter
application traces.

## Expansion semantics

AMG consumes every `Gather` record from `amg.json`. LULESH consumes every
`Scatter` record from `lulesh.json` and ignores its Gather records. Selected
records remain in source order. Each record expands in this order:

1. iterations from zero through `count - 1`;
2. pattern entries in their JSON order;
3. index `record_base + iteration * delta + pattern_value`.

Record bases make selected records non-overlapping without changing their
internal pattern, count, delta, or ordering. If one complete selected trace is
smaller than the required working set, the generator emits complete epochs.
Every epoch receives a non-overlapping address range. It stops at the first
whole-epoch boundary for which the actual resident arrays meet or exceed
1,073,741,824 bytes.

Actual allocation is computed rather than declared:

- AMG: `4 * values_count + 8 * index_count + 4 * index_count` bytes, because
  gather allocates values, index, and one destination per index.
- LULESH: `4 * index_count + 8 * index_count + 4 * (max_index + 1)` bytes,
  because scatter requires one value per index and a destination spanning the
  target range.

The binary index format is little-endian unsigned 64-bit. Values are
little-endian IEEE-754 binary32. Value bits are produced by a fixed,
position-only integer mapping into finite normal positive values. The mapping
uses no PRNG, host floating-point arithmetic, timestamps, or environment state.
It therefore catches address/order mistakes while remaining byte-identical on
every host.

## Artifacts and identity

Each workload is published under a content-addressed directory below
`/mnt/disk0/gem5-CXL-eval/cira-amu-m2ndp-spectrum/formal-inputs/spatter/`.
The directory contains:

- `values.f32le`
- `index.u64le`
- `provenance.json`
- `validation.json`

Provenance binds the source path, source commit, source trace SHA-256,
generator SHA-256, selection rule, expansion version, element encoding,
selected source records, epoch count, element counts, maximum index, computed
allocation, and artifact hashes. The content-address is the canonical SHA-256
of the immutable identity fields and output hashes.

Publication is atomic. Existing accepted content-addressed directories are
never overwritten. A conflicting directory or hash drift fails closed.

## Validation gates

Tests are written before implementation and cover exact small-fixture
expansion, kernel filtering, record and epoch order, non-overlapping epochs,
finite value bits, exact allocation arithmetic, determinism, source-hash
rejection, malformed-record rejection, and atomic publication.

The formal generation run must then pass all of these gates:

1. source files match the pinned hashes and commit;
2. generated files have the declared element widths and hashes;
3. computed resident allocation is at least 1 GiB;
4. an independent second generation has identical file and manifest hashes;
5. the existing Spatter reference binary completes both inputs;
6. replayed destination output is bit-exact;
7. the accepted paper input record contains the generated paths and hashes;
8. `freeze_cross_system_inputs.py` accepts all six workloads.

Any failure writes a terminal failure record and does not update the accepted
paper input record.

## Integration and paper language

The generator updates only the candidate record after generation and
validation. The six-workload `paper-input-record.json` is created only after
AMG, LULESH, PR, MCF, NPB CG, and NPB MG all pass their respective identity and
capacity gates.

The paper describes these two inputs as deterministic CXL-scale expansions of
official Spatter application traces. It must not call them hardware captures
or raw application memory dumps. All four evaluated systems consume the same
frozen bytes.
