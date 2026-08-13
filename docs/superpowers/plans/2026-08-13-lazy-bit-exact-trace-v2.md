# Lazy Bit-Exact Trace v2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace infeasible per-primitive NPB trace files with a lossless, bounded-memory descriptor whose shared lazy iterator reproduces the complete bit-exact operation stream.

**Architecture:** Schema 2 stores hash-bound array images, ordered kernel invocations, exact loop/scalar parameters, fixed four-lane reductions, dynamic counts, and raw-boundary commitments. A backend-neutral Python iterator is the executable reference for Task 7; it memory-maps state copy-on-write and yields the existing `Operation` ABI one record at a time. NPB hooks capture state and invocation evidence without emitting primitive records, and fixture/formal gates compare two reference runs, full lazy replay, expanded-stream hashes, boundary hashes, and official NPB verification.

**Tech Stack:** Python 3 standard library (`dataclasses`, `hashlib`, `json`, `mmap`, `struct`), C++17, fixed-form Fortran/OpenMP, canonical 56-byte operation ABI, `unittest`, SHA-256.

---

## Scope and file map

- Create `scripts/lazy_work_trace.py` for schema-2 bundle validation,
  memory-mapped state, streaming expansion, batching, counts, and hashes.
- Create `scripts/npb_lazy_trace.py` for exact CG/MG kernel descriptors and
  expanders. Kernel arithmetic is isolated here; generic bundle code does not
  know NPB semantics.
- Create `tests/pyunit/cross_system/test_lazy_work_trace.py` for schema,
  corruption, eager-equivalence, batch invariance, and bounded-memory tests.
- Create `tests/pyunit/cross_system/test_npb_lazy_trace.py` for tiny exact CG
  and MG operation/reference-boundary tests.
- Modify `scripts/canonical_work_trace.py` only to dispatch schema 1 versus
  schema 2 and preserve the current eager API.
- Modify `util/amu/matched_workloads/npb_trace_hooks.{h,cc}` to write array
  images, invocation records, reduction records, and streaming SHA-256
  boundary commitments; remove per-primitive buffering.
- Modify `util/amu/matched_workloads/npb-{cg,mg}-trace.patch` and the matching
  transforms in `scripts/build_matched_breadth_workloads.py` to capture exact
  descriptor inputs while preserving non-reduction arithmetic.
- Modify `tests/pyunit/cross_system/test_npb_trace_instrumentation.py` to make
  schema-2 replay, not a multi-gigabyte eager file, the acceptance gate.
- Do not modify gem5, AMU, CIRA, or M2NDP execution in this plan. Those consume
  the stable iterator contract in Tasks 8 and 9 of the parent plan.

### Task 1: Schema-2 bundle and bounded-memory iterator contract

**Files:**
- Create: `scripts/lazy_work_trace.py`
- Create: `tests/pyunit/cross_system/test_lazy_work_trace.py`
- Modify: `scripts/canonical_work_trace.py`

- [x] **Step 1: Write failing schema, corruption, and streaming tests**

Add fixtures that use a two-element `f64` image and a registered `fixture_add`
expander. Require schema-2 round trip, one-bit image corruption rejection,
overlapping logical ranges rejection, unknown kernel rejection, exact expanded
count/hash, and batch sizes 1 and 7 to produce identical encoded operations.

```python
def test_lazy_expansion_matches_eager_and_is_batch_invariant(self):
    bundle = fixture_bundle(self.root)
    eager = tuple(fixture_operations())
    for batch in (1, 7):
        observed = tuple(lazy.iter_operations(
            bundle, {"fixture_add": expand_fixture_add},
            batch_work_items=batch))
        self.assertEqual(observed, eager)
    self.assertEqual(lazy.expanded_fingerprint(bundle, EXPANDERS),
                     (canonical.operations_sha256(eager), len(eager)))

def test_one_bit_image_change_fails_before_iteration(self):
    bundle = fixture_bundle(self.root)
    image = self.root / "images/x.f64"
    payload = bytearray(image.read_bytes())
    payload[0] ^= 1
    image.write_bytes(payload)
    with self.assertRaisesRegex(lazy.LazyTraceError, "SHA-256"):
        lazy.read_bundle(self.root)
```

- [x] **Step 2: Run the new tests and verify RED**

Run:
`PYTHONPATH=. python3 -m unittest discover -v -s tests/pyunit/cross_system -p 'test_lazy_work_trace.py'`

Expected: FAIL because `scripts.lazy_work_trace` does not exist.

- [x] **Step 3: Implement immutable schema-2 types and fail-closed loading**

Define these public types and reject booleans, negative/overflowing integers,
unknown roles/types, duplicate names, path escape, byte-count mismatch, hash
mismatch, overlapping logical ranges, and non-contiguous invocation ordinals.

```python
@dataclasses.dataclass(frozen=True)
class ArrayImage:
    name: str
    role: str                 # "input" or "state"
    element_type: str         # "u32", "u64", "f32", or "f64"
    count: int
    logical_base: int
    path: str
    sha256: str

@dataclasses.dataclass(frozen=True)
class Invocation:
    ordinal: int
    phase: int
    kernel: str
    iteration: int
    work_items: int
    parameters: dict

@dataclasses.dataclass(frozen=True)
class LazyBundle:
    root: pathlib.Path
    meta: dict
    arrays: tuple[ArrayImage, ...]
    invocations: tuple[Invocation, ...]
    dynamic_work: dict
```

Write `write_bundle(root, meta, arrays, invocations, dynamic_work)` and
`read_bundle(root)`. Use canonical JSON, sibling temporary files, `fsync`, and
`os.replace`; schema 1 remains owned by `canonical_work_trace.py`.

- [x] **Step 4: Implement copy-on-write mapped state and streaming expansion**

`MappedState` opens each image read-only and maps mutable arrays with
`mmap.ACCESS_COPY`. It exposes checked `load_raw`, `load_float`, `store_raw`,
and `boundary_sha256` methods. `iter_operations()` assigns global sequence
numbers itself and rejects an expander that emits a wrong phase/work item,
invalid address, or nonzero sequence. Batch size controls only groups of
work-item coordinates and never changes operation order.

```python
def iter_operations(bundle, expanders, *, batch_work_items=1):
    with MappedState(bundle) as state:
        sequence = 0
        for invocation in bundle.invocations:
            expander = expanders.get(invocation.kernel)
            if expander is None:
                raise LazyTraceError(f"unknown kernel {invocation.kernel}")
            for operation in expander(state, invocation, batch_work_items):
                yield dataclasses.replace(operation, sequence=sequence)
                sequence += 1
        if sequence != bundle.dynamic_work["primitive_records"]:
            raise LazyTraceError("dynamic primitive count mismatch")
```

Add `operations_sha256(iterable)` to `canonical_work_trace.py` and
`expanded_fingerprint(bundle, expanders)` to stream encoded records through
SHA-256 without constructing a tuple.

- [x] **Step 5: Prove bounded memory and run canonical regressions**

Use a synthetic descriptor with ten million repeated work items but consume
only its first 100,000 operations under `tracemalloc`; peak Python allocation
must stay below 16 MiB and must not scale with declared work count.

Run:

```bash
PYTHONPATH=. python3 -m unittest discover -v -s tests/pyunit/cross_system -p 'test_lazy_work_trace.py'
PYTHONPATH=. python3 -m unittest discover -v -s tests/pyunit/cross_system -p 'test_canonical_work_trace.py'
```

Expected: all tests PASS.

- [x] **Step 6: Commit the generic lazy-trace layer**

```bash
git add scripts/canonical_work_trace.py scripts/lazy_work_trace.py \
  tests/pyunit/cross_system/test_lazy_work_trace.py
git commit -m "feat: add bounded-memory canonical trace v2"
```

### Task 2: Exact CG lazy kernel

**Files:**
- Create: `scripts/npb_lazy_trace.py`
- Create: `tests/pyunit/cross_system/test_npb_lazy_trace.py`

- [x] **Step 1: Write a tiny CG eager-equivalence test**

Use a three-row CSR matrix, explicit `x/z/p/q/r` images, two conjugate-gradient
steps, and four canonical lane ranges. Hand-build the expected eager operations
for SpMV, `p dot q`, `z/r/rho`, `p` update, residual SpMV, residual norm, and
outer normalization. Require exact equality of every opcode, logical address,
raw operand/result, work item, sequence, barrier, commit, and all vector
boundary SHA-256 values.

```python
def test_tiny_cg_lazy_stream_equals_hand_eager_stream(self):
    bundle = tiny_cg_bundle(self.root)
    observed = tuple(lazy.iter_operations(bundle, npb.EXPANDERS))
    self.assertEqual(observed, tiny_cg_eager_operations())
    self.assertEqual(npb.replay_boundaries(bundle), tiny_cg_boundaries())
```

- [x] **Step 2: Run the CG test and verify RED**

Run:
`PYTHONPATH=. python3 -m unittest discover -v -s tests/pyunit/cross_system -p 'test_npb_lazy_trace.py'`

Expected: FAIL because `scripts.npb_lazy_trace` does not exist.

- [x] **Step 3: Implement raw IEEE-754 helpers and canonical four-lane merge**

Use `struct.pack/unpack`, never decimal serialization. Each multiply, add,
subtract, and divide is a separate Python float operation immediately rounded
to binary64 by packing and unpacking. Reject non-binary64 host behavior at
module import. Partition lane `t` with integer endpoints
`floor(t*n/4):floor((t+1)*n/4)` and merge `(0 op 1) op (2 op 3)`.

```python
def f64(value):
    return struct.unpack("<d", struct.pack("<d", value))[0]

def lane_range(count, lane):
    return count * lane // 4, count * (lane + 1) // 4
```

- [x] **Step 4: Implement `npb_cg` expansion and boundary replay**

Emit explicit load/store records with image logical addresses and explicit
`F64_MUL`, `F64_ADD`, `F64_SUB` records with raw operands/results. Preserve
stored CSR order, execute exactly `cgitmax` steps per invocation, and emit the
three fixed merge edges. Do not use `sum`, NumPy, FMA, vector operations, or a
different reduction order. `replay_boundaries()` streams SHA-256 over raw
little-endian words at each descriptor boundary.

- [x] **Step 5: Add CG corruption and deterministic replay tests**

Flip one CSR index bit and require an out-of-bounds failure. Change one lane
boundary or `cgitmax` and require descriptor/dynamic-count rejection. Expand
twice and require identical stream hashes and boundary maps.

- [x] **Step 6: Run tests and commit CG expansion**

```bash
PYTHONPATH=. python3 -m unittest discover -v -s tests/pyunit/cross_system \
  -p 'test_npb_lazy_trace.py'
git add scripts/npb_lazy_trace.py \
  tests/pyunit/cross_system/test_npb_lazy_trace.py
git commit -m "feat: expand exact NPB CG lazy traces"
```

### Task 3: Exact MG lazy kernels

**Files:**
- Modify: `scripts/npb_lazy_trace.py`
- Modify: `tests/pyunit/cross_system/test_npb_lazy_trace.py`

- [x] **Step 1: Add hand-checked tiny-grid tests for every MG phase**

Use padded `4x4x4` and `6x6x6` grids with non-symmetric binary64 values so
reassociation is visible. Add separate eager operation lists and post-state
raw words for `resid`, `rprj3`, `interp`, `psinv`, and `norm2u3`. Require
boundary handling, level offsets, and every raw arithmetic result to match.

- [x] **Step 2: Run the MG tests and verify RED**

Run the NPB lazy test file. Expected: FAIL naming the first unknown MG kernel.

- [x] **Step 3: Implement MG indexing and expression primitives**

Define checked Fortran-column-major indexing and helpers that emit one
operation for each source expression node. Encode each NPB fixed-form
expression as a named tuple/tree so grouping is explicit in code review; never
rewrite it as a polynomial or combine multiply/add.

```python
def f_index(i1, i2, i3, n1, n2, n3):
    if not (0 <= i1 < n1 and 0 <= i2 < n2 and 0 <= i3 < n3):
        raise LazyTraceError("MG grid index is outside image")
    return i1 + n1 * (i2 + n2 * i3)
```

- [x] **Step 4: Implement the five MG expanders and fixed norm tree**

Implement `npb_mg_resid`, `npb_mg_rprj3`, `npb_mg_interp`, `npb_mg_psinv`,
and `npb_mg_norm2u3` in source loop order. Stores update `MappedState`
immediately. Norm uses four explicit ranges and emits both `F64_ADD` and
`F64_MAX` edges with raw operands/results.

- [x] **Step 5: Add batch invariance and full V-cycle fixture tests**

Run the same tiny V-cycle with batch sizes 1, 2, and 17; require equal encoded
streams, dynamic counts, final grids, residuals, and norm commitments. Flip a
level offset and require bounds failure before a boundary can pass.

- [x] **Step 6: Run tests and commit MG expansion**

```bash
PYTHONPATH=. python3 -m unittest discover -v -s tests/pyunit/cross_system \
  -p 'test_npb_lazy_trace.py'
git add scripts/npb_lazy_trace.py \
  tests/pyunit/cross_system/test_npb_lazy_trace.py
git commit -m "feat: expand exact NPB MG lazy traces"
```

### Task 4: NPB descriptor capture without primitive trace explosion

**Files:**
- Modify: `util/amu/matched_workloads/npb_trace_hooks.h`
- Modify: `util/amu/matched_workloads/npb_trace_hooks.cc`
- Modify: `scripts/build_matched_breadth_workloads.py`
- Modify: `util/amu/matched_workloads/npb-cg-trace.patch`
- Modify: `util/amu/matched_workloads/npb-mg-trace.patch`
- Modify: `tests/pyunit/cross_system/test_npb_trace_instrumentation.py`

- [x] **Step 1: Replace the eager replayability assertion with failing v2 evidence assertions**

Require each Class S run to produce `trace.v2.json`, hash-bound array images,
ordered invocation records, exact dynamic counts, expanded-stream SHA-256,
raw-boundary commitments, and no `*.trace.bin` larger than the small explicit
reduction/control record budget. Keep official verification, two-run raw-bit
comparison, zero-fuzz patch equality, arithmetic fingerprint, allocation
probe, and the flipped-bit failure.

- [x] **Step 2: Run focused instrumentation tests and verify RED**

Run the NPB instrumentation test file. Expected: FAIL because no schema-2
descriptor exists.

- [x] **Step 3: Remove primitive buffering and add fail-closed capture records**

Delete `matched_trace_load_*`, `matched_trace_store_*`, and
`matched_trace_binary_*` from the Fortran-facing API. Add hooks with fixed
binary headers for:

```c
void matched_array_image_(const int64_t *array_id, const int64_t *element_bits,
                          const int64_t *logical_base, const void *data,
                          const int64_t *count);
void matched_invocation_(const int64_t *ordinal, const int64_t *phase,
                         const int64_t *kernel, const int64_t *iteration,
                         const int64_t *work_items, const int64_t *parameters,
                         const int64_t *parameter_count);
void matched_boundary_sha256_(const int64_t *boundary,
                              const int64_t *iteration, const void *data,
                              const int64_t *element_bits,
                              const int64_t *count);
```

Implement SHA-256 locally in the hook object, stream raw bytes without a full
copy, and include boundary id, iteration, element width, count, and digest in
the commitment record. Reject repeated array ids with different metadata,
duplicate invocation ordinals, invalid widths, negative counts, and I/O
errors.

- [x] **Step 4: Make the CG patch capture one initial state plus exact invocations**

Capture `rowstr`, `colidx`, `a`, `x`, `z`, `p`, `q`, and `r` at the declared
canonical start. Emit ordered CG invocation parameters (`firstrow`, `lastrow`,
`firstcol`, `lastcol`, `cgitmax`, outer iteration, and four explicit lane
ranges). Retain transformed arithmetic and raw boundary commitments; remove
all per-primitive hook calls.

- [x] **Step 5: Make the MG patch capture initial grids plus all phase invocations**

Register each allocation once with array id, logical base, dimensions, and
raw bytes. At every `resid`, `rprj3`, `interp`, `psinv`, and `norm2u3` entry,
record kernel id, array ids/offsets, dimensions, coefficients, level,
invocation ordinal, and exact work-item count. Preserve the explicit four-lane
norm transform and boundary commitments.

- [x] **Step 6: Parse capture files into a canonical schema-2 bundle**

Add `_parse_npb_capture`, `_write_npb_lazy_bundle`, and
`_validate_npb_capture_counts`. Copy/hash each image exactly once, translate
numeric kernel/array ids through constant tables, derive no missing semantic
parameter, and compare captured counts with the descriptor manifest.

- [x] **Step 7: Regenerate zero-fuzz patches and run focused tests**

Generate unified diffs from `_transform_cg` and `_transform_mg`, use
`a/CG/cg.f` / `b/CG/cg.f` and `a/MG/mg.f` / `b/MG/mg.f`, then run:

```bash
PYTHONPATH=. python3 -m unittest discover -v -s tests/pyunit/cross_system \
  -p 'test_npb_trace_instrumentation.py'
```

Expected: all tests PASS and Class S produces bounded descriptor/capture files.

- [x] **Step 8: Commit descriptor capture**

```bash
git add scripts/build_matched_breadth_workloads.py \
  util/amu/matched_workloads/npb_trace_hooks.h \
  util/amu/matched_workloads/npb_trace_hooks.cc \
  util/amu/matched_workloads/npb-cg-trace.patch \
  util/amu/matched_workloads/npb-mg-trace.patch \
  tests/pyunit/cross_system/test_npb_trace_instrumentation.py
git commit -m "feat: capture bounded NPB lazy trace descriptors"
```

### Task 5: Fixture and formal proof gates

**Files:**
- Modify: `scripts/build_matched_breadth_workloads.py`
- Modify: `tests/pyunit/cross_system/test_npb_trace_instrumentation.py`
- Modify: `docs/superpowers/plans/2026-08-12-cira-amu-m2ndp-scaling-breadth.md`

- [x] **Step 1: Add failing two-run expansion and formal fail-closed tests**

For CG and MG Class S, require identical descriptor hash, image hashes,
invocation table, dynamic counts, expanded-stream hash, and boundary
commitments across two executions. Require full lazy replay commitments to
equal transformed Vanilla commitments. Mock the formal input identity only at
the file boundary and prove allocation, parameter, source, patch, expander,
and ABI drift each reject publication.

- [x] **Step 2: Run focused tests and verify RED**

Run the instrumentation tests. Expected: FAIL at the first missing expanded
hash or identity field.

- [x] **Step 3: Integrate full functional lazy replay**

`build_and_run_npb_fixture()` and `build_and_run_npb_formal()` must call
`lazy.read_bundle`, exhaust `npb.iter_operations` through
`lazy.expanded_fingerprint`, call `npb.replay_boundaries`, and compare every
commitment before constructing a result manifest. Delete the old
`validate_npb_replayable_trace(path, workload)` eager-opcode heuristic.

- [x] **Step 4: Bind every semantic identity in manifests**

Record schema, descriptor SHA-256, ordered image SHA-256 values, expanded
stream SHA-256/count, boundary map SHA-256, expander source SHA-256, trace ABI
SHA-256, source/patch/parameter/binary/config hashes, exact four-thread proof,
allocation bytes, and build/run commands. Write no manifest on any mismatch.

- [x] **Step 5: Update the parent Task 7 checklist and run full regressions**

Mark parent Task 7 steps complete only after these commands pass freshly:

```bash
PYTHONPATH=. python3 -m unittest discover -v -s tests/pyunit/cross_system \
  -p 'test_lazy_work_trace.py'
PYTHONPATH=. python3 -m unittest discover -v -s tests/pyunit/cross_system \
  -p 'test_npb_lazy_trace.py'
PYTHONPATH=. python3 -m unittest discover -v -s tests/pyunit/cross_system \
  -p 'test_npb_trace_instrumentation.py'
PYTHONPATH=. python3 -m unittest discover -v -s tests/pyunit/cross_system \
  -p 'test_*.py'
```

Expected: all tests PASS; no formal result is claimed unless the clean frozen
12.8 GB inputs themselves were executed.

- [x] **Step 6: Request code review and resolve every Critical/Important finding**

The reviewer must specifically inspect eager/lazy equivalence, sequence and
address generation, floating-point grouping, lane partition/tree, MG boundary
handling, hash coverage, dynamic counts, and formal fail-closed behavior.

- [x] **Step 7: Commit and push the completed Task 7**

```bash
git add scripts/build_matched_breadth_workloads.py \
  docs/superpowers/plans/2026-08-12-cira-amu-m2ndp-scaling-breadth.md \
  tests/pyunit/cross_system/test_npb_trace_instrumentation.py
git commit -m "feat: verify exact NPB CG and MG lazy traces"
git push origin m2ndp-g20-pr-spmv
```

Do not start parent Task 8 until the review gate passes and the branch is
pushed. Formal NPB remains `failed_input` if clean source identity or exact
paper allocations are unavailable; Class S fixture success is not formal
paper evidence.
