# Formal Spatter Input Generation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Generate, independently reproduce, reference-validate, and freeze CXL-scale AMG Gather and LULESH Scatter inputs derived from the pinned official Spatter traces.

**Architecture:** A focused streaming generator parses selected Spatter records into a deterministic tiled index stream and position-derived finite f32 values. It publishes only after two independent generations agree and the existing C++ reference adapter produces the expected destination hash. The input freezer independently revalidates provenance, hashes, and actual resident-byte arithmetic before admitting either artifact to paper evidence.

**Tech Stack:** Python 3 standard library, little-endian `struct` encoding, SHA-256, existing C++17 matched-workload reference adapter, `unittest`, canonical JSON and atomic publication helpers.

---

### Task 1: Define and test trace expansion semantics

**Files:**
- Create: `scripts/generate_formal_spatter_inputs.py`
- Create: `tests/pyunit/cross_system/test_generate_formal_spatter_inputs.py`

- [ ] **Step 1: Write failing parser and selection tests**

Create a temporary source trace containing one Gather, two Scatter, and one
malformed-record fixture. Test that `load_records(path, expected_sha256,
kernel)` keeps only the requested kernel in source order and rejects a hash
mismatch, negative `count`, negative `delta`, empty pattern, negative pattern
entry, bool-as-int fields, and non-integer fields.

```python
records = generator.load_records(trace_path, sha256(trace_path), "Scatter")
self.assertEqual([row.count for row in records], [2, 1])
with self.assertRaisesRegex(generator.GenerationError, "SHA-256"):
    generator.load_records(trace_path, "0" * 64, "Scatter")
```

- [ ] **Step 2: Run the parser tests and observe the required failure**

Run `python3 -m unittest discover -s tests/pyunit/cross_system -p
'test_generate_formal_spatter_inputs.py' -v`.

Expected: import or attribute failure because the generator does not exist.

- [ ] **Step 3: Implement the minimal immutable parser**

Add `GenerationError`, an immutable `TraceRecord`, `_sha256_file`, and
`load_records`. Require a resolved regular file and exact source hash, parse a
top-level JSON list, validate every record, retain selected records without
reordering, reject an empty selection, and reject any index expression above
`2**64 - 1`.

- [ ] **Step 4: Add failing layout, epoch, and value-bit tests**

Use a small trace for which the exact flattened indices are known.

```python
layout = generator.layout(records)
self.assertEqual(list(generator.indices(layout, epochs=2)), expected_indices)
self.assertLess(max(expected_indices[:layout.index_count]),
                min(expected_indices[layout.index_count:]))
self.assertEqual(generator.value_bits(0) & 0x7f800000, 0x3f000000)
```

Test that `required_epochs(layout, "gather", 1024)` and the scatter equivalent
stop at the first whole epoch satisfying `resident_bytes >= minimum`.

- [ ] **Step 5: Run the new tests and observe semantic failures**

Run the focused discovery command from Step 2.

Expected: parser tests pass; layout, value, and allocation tests fail because
those functions are absent.

- [ ] **Step 6: Implement deterministic layout and arithmetic**

Add immutable `RecordLayout` and `TraceLayout`. Record bases are cumulative
`(count - 1) * delta + max(pattern) + 1`; epoch bases are cumulative layout
span. Implement exact integer arithmetic:

```python
def resident_bytes(layout, epochs, mode):
    n = layout.index_count * epochs
    span = layout.index_span * epochs
    if mode == "gather":
        return 4 * span + 8 * n + 4 * n
    if mode == "scatter":
        return 4 * n + 8 * n + 4 * span
    raise GenerationError("mode must be gather or scatter")

def value_bits(position):
    return 0x3f000000 | ((position * 0x9e3779b1) & 0x007fffff)
```

Use integer ceiling division to compute the minimum whole epoch count.

- [ ] **Step 7: Run focused tests and commit the core**

Expected: every Task 1 test passes.

```bash
git add scripts/generate_formal_spatter_inputs.py tests/pyunit/cross_system/test_generate_formal_spatter_inputs.py
git commit -m "feat: define formal Spatter expansion"
```

### Task 2: Stream and independently reproduce binary artifacts

**Files:**
- Modify: `scripts/generate_formal_spatter_inputs.py`
- Modify: `tests/pyunit/cross_system/test_generate_formal_spatter_inputs.py`

- [ ] **Step 1: Write failing streaming-artifact tests**

Generate a tiny workload twice into separate temporary roots. Assert exact
little-endian bytes, equal hashes, finite values, element counts, maximum
index, and exact resident-byte accounting. Test a simulated second-pass hash
drift and an existing conflicting content-address directory.

```python
first = generator.generate_once(spec, root / "first")
second = generator.generate_once(spec, root / "second")
self.assertEqual(first.values_sha256, second.values_sha256)
self.assertEqual(first.index_sha256, second.index_sha256)
```

- [ ] **Step 2: Run tests and verify RED**

Expected: failures for missing `generate_once` and `generate_twice`.

- [ ] **Step 3: Implement bounded-memory writers**

Write `values.f32le` and `index.u64le` with fixed-size byte buffers and
`struct.pack_into`. Never construct the full arrays in Python. Flush, `fsync`,
rehash from disk, and return an immutable `GeneratedArtifacts` containing
`workload`, `mode`, `epochs`, `values_count`, `index_count`, `maximum_index`,
`resident_bytes`, paths, and both artifact hashes.

- [ ] **Step 4: Implement two-pass identity and guarded promotion**

`generate_twice(spec, staging_root)` creates two sibling roots, runs
`generate_once` independently, compares every semantic field and file hash,
and constructs provisional provenance. Compute the artifact id from canonical
JSON containing source identity, generator SHA-256, expansion version,
selection rule, counts, capacity, and output hashes. Add
`promote_validated(generation, validation, output_root)`, which rejects any
validation record other than `status=accepted` and promotes with `os.replace`.
Never overwrite an existing content-addressed directory unless every existing
artifact hash is identical. Task 3 supplies the real validation record; no CLI
can publish before that task is complete.

- [ ] **Step 5: Add guarded-promotion and cleanup tests**

Assert that failed second-pass comparison removes both staging roots, rejected
validation cannot promote, accepted validation promotes exactly one immutable
directory, and an existing conflicting directory fails without modification.

- [ ] **Step 6: Run tests, compile-check, and commit**

```bash
python3 -m py_compile scripts/generate_formal_spatter_inputs.py
python3 -m unittest discover -s tests/pyunit/cross_system -p 'test_generate_formal_spatter_inputs.py' -v
git add scripts/generate_formal_spatter_inputs.py tests/pyunit/cross_system/test_generate_formal_spatter_inputs.py
git commit -m "feat: publish reproducible Spatter inputs"
```

### Task 3: Validate the full destination with the existing adapter

**Files:**
- Modify: `scripts/build_matched_breadth_workloads.py`
- Modify: `scripts/generate_formal_spatter_inputs.py`
- Modify: `tests/pyunit/cross_system/test_generate_formal_spatter_inputs.py`
- Modify: `tests/pyunit/cross_system/test_matched_region_build.py`

- [ ] **Step 1: Write a failing public reference-builder test**

Test that `build_spatter_reference_binary(cxx, output)` compiles the existing
`spatter_regions.cc` with `MATCHED_BACKEND=reference`, the repository's strict
floating-point flags, and no fixture define. Assert that path, binary hash,
compiler identity, source hash, trace ABI hash, and command are returned.

- [ ] **Step 2: Run the builder test and verify RED**

Run `python3 -m unittest discover -s tests/pyunit/cross_system -p
'test_matched_region_build.py' -v`.

Expected: missing `build_spatter_reference_binary`.

- [ ] **Step 3: Add the narrow public wrapper**

Create the output parent, call the existing `_compile` for `spatter` and
`reference`, and return the requested identities. Do not duplicate compiler
flags or compilation logic.

- [ ] **Step 4: Write failing output-oracle tests**

For gather, expected destination word `i` is `value_bits(index[i])`. For
scatter, initialize `maximum_index + 1` zero words and apply stores in program
order so duplicate indices retain the final write. Run the C++ adapter with
`--trace /dev/null`; compare the output SHA-256 and word count with the
streaming oracle. Tamper one value and assert validation fails.

- [ ] **Step 5: Implement and record reference validation**

Add `validate_reference(artifacts, binary, work_root)`. It invokes the adapter
with the generated bytes and `/dev/null` trace, requires exit zero and the
expected `MATCHED_PHASE_WORK` line, hashes the destination, compares it with
the oracle, and returns a validation record binding binary/source/ABI/input
and output hashes. Write `validation.json` before content-address promotion
and include its hash in the candidate record.

The `generate` CLI now becomes available. It requires source root/commit, both
relative trace paths and hashes, minimum bytes, output root, and
candidate-record path. It performs two-pass generation, reference validation,
guarded promotion, then atomically updates only the AMG/LULESH candidate rows.
The `verify` subcommand revalidates a published directory without modifying
it. Source-commit drift writes a terminal failure record and publishes
nothing.

- [ ] **Step 6: Run focused tests and commit**

```bash
python3 -m unittest discover -s tests/pyunit/cross_system -p 'test_generate_formal_spatter_inputs.py' -v
python3 -m unittest discover -s tests/pyunit/cross_system -p 'test_matched_region_build.py' -v
git add scripts/generate_formal_spatter_inputs.py scripts/build_matched_breadth_workloads.py tests/pyunit/cross_system/test_generate_formal_spatter_inputs.py tests/pyunit/cross_system/test_matched_region_build.py
git commit -m "feat: validate generated Spatter outputs"
```

### Task 4: Make the input freezer independently verify provenance

**Files:**
- Modify: `scripts/freeze_cross_system_inputs.py`
- Modify: `scripts/audit_cross_system_input_record.py`
- Modify: `scripts/build_matched_breadth_workloads.py`
- Modify: `tests/pyunit/cross_system/test_freeze_cross_system_inputs.py`
- Modify: `tests/pyunit/cross_system/test_matched_region_build.py`

- [ ] **Step 1: Write failing provenance and capacity tests**

Extend accepted AMG/LULESH fixtures with `synthetic: false`, `provenance`,
`provenance_sha256`, `validation`, and `validation_sha256`. Test rejection of
wrong source trace hashes, mismatched artifact hashes, wrong workload/kernel
selection, failed reference validation, declared allocation drift, allocation
below 1 GiB, and `synthetic: true`.

- [ ] **Step 2: Run freezer tests and verify RED**

Run `python3 -m unittest discover -s tests/pyunit/cross_system -p
'test_freeze_cross_system_inputs.py' -v`.

Expected: at least the provenance-tampering case is accepted by current code.

- [ ] **Step 3: Implement `validate_spatter_record`**

Read and hash both JSON records. Require schema 1, `status=accepted`,
`source_kind=official_spatter_application_trace`, the pinned workload/kernel
mapping, exact artifact hashes/counts, successful independent regeneration,
successful reference validation, and exact allocation arithmetic:

```python
if workload == "amg_gather":
    computed = 4 * values_count + 8 * index_count + 4 * index_count
else:
    computed = 4 * values_count + 8 * index_count + 4 * (maximum_index + 1)
if computed != row["allocated_bytes"]:
    raise InputError(f"{workload} allocated bytes differ")
```

Call this validator from `validate_bound_inputs`. Update the audit template
and observed fields so missing provenance is explicit.

- [ ] **Step 4: Bind provenance into the prepared manifest**

Update `load_formal_inputs` so Spatter rows include provenance and validation
paths/hashes, actual allocation, counts, maximum index, source trace hash,
generator hash, and reference binary hash. Tests assert that all fields
survive into the formal suite manifest.

- [ ] **Step 5: Run input/builder regressions and commit**

```bash
python3 -m unittest discover -s tests/pyunit/cross_system -p 'test_freeze_cross_system_inputs.py' -v
python3 -m unittest discover -s tests/pyunit/cross_system -p 'test_matched_region_build.py' -v
git add scripts/freeze_cross_system_inputs.py scripts/audit_cross_system_input_record.py scripts/build_matched_breadth_workloads.py tests/pyunit/cross_system/test_freeze_cross_system_inputs.py tests/pyunit/cross_system/test_matched_region_build.py
git commit -m "feat: verify formal Spatter provenance"
```

### Task 5: Generate and accept production AMG/LULESH artifacts

**Files:**
- Output: `/mnt/disk0/gem5-CXL-eval/cira-amu-m2ndp-spectrum/formal-inputs/spatter/`
- Modify atomically: `/mnt/disk0/gem5-CXL-eval/cira-amu-m2ndp-spectrum/paper-input-candidates.json`
- Output: `/mnt/disk0/gem5-CXL-eval/cira-amu-m2ndp-spectrum/paper-input-discovery.json`

- [ ] **Step 1: Recheck capacity and source authority**

Run `df -B1 /mnt/disk0`, inspect the source checkout's HEAD with per-command
`safe.directory`, and hash both source traces. Require at least 6 GB free and
exact agreement with the spec's commit and hashes.

- [ ] **Step 2: Run the formal generator**

```bash
python3 scripts/generate_formal_spatter_inputs.py generate --source-root /home/victoryang00/CXLMemUring/bench/spatter --source-commit ec8923711f8dc21eedff7189f12b02eb06845d2f --amg-trace standard-suite/app-traces/amg.json --amg-sha256 3ebf359a0976532c04cebd3cb4432589c2c9ec3d7b6fe61661c042f6adc2121c --lulesh-trace standard-suite/app-traces/lulesh.json --lulesh-sha256 9073035ecf77e7fde65262f782286207e76cca24312b2e01688b038901d021ee --minimum-bytes 1073741824 --output-root /mnt/disk0/gem5-CXL-eval/cira-amu-m2ndp-spectrum/formal-inputs/spatter --candidate-record /mnt/disk0/gem5-CXL-eval/cira-amu-m2ndp-spectrum/paper-input-candidates.json
```

Expected: AMG uses 2 epochs, LULESH uses 6, and both report at least 1 GiB of
computed resident allocation.

- [ ] **Step 3: Verify immutable artifacts and reference evidence**

Run the CLI `verify` subcommand against both published directories. Require
all source, generator, artifact, replay, and reference hashes to pass and no
temporary directories to remain.

- [ ] **Step 4: Re-audit the candidate record**

```bash
python3 scripts/audit_cross_system_input_record.py --candidate-record /mnt/disk0/gem5-CXL-eval/cira-amu-m2ndp-spectrum/paper-input-candidates.json --discovery-output /mnt/disk0/gem5-CXL-eval/cira-amu-m2ndp-spectrum/paper-input-discovery.json --template-output /mnt/disk0/gem5-CXL-eval/cira-amu-m2ndp-spectrum/paper-input-record.template.json
```

Expected: AMG/LULESH have no missing fields, exact hashes,
`synthetic=false`, and successful reference validation. Any remaining failure
is limited to the separately tracked NPB clean-source/allocation closure.

- [ ] **Step 5: Run the complete cross-system unit suite**

Run `python3 -m unittest discover -s tests/pyunit/cross_system -p
'test_*.py' -v`.

Expected: zero failures and only the two pre-existing allowed real FuncSim
integration skips.

- [ ] **Step 6: Commit integration and push**

Do not commit multi-gigabyte binary artifacts. Commit only code, tests, and
documentation. Push `m2ndp-g20-pr-spmv` and verify remote HEAD with
`git ls-remote`. Keep `src/mem/cache/base.cc` and `.superpowers/` unstaged.

### Task 6: Resume the parent six-workload spectrum plan

**Files:**
- Existing plan: `docs/superpowers/plans/2026-08-23-six-workload-cxl-latency-spectrum-paper.md`

- [ ] **Step 1: Return to Task 9 of the parent plan**

Close NPB CG/MG clean-source and measured-allocation records without changing
the accepted Spatter identities. Then create `paper-input-record.json`, run
the freezer, build the prepared suite, execute four latency campaigns, and
publish the 96 validated points with the committed publisher.

- [ ] **Step 2: Preserve fail-closed boundaries**

Do not start a formal latency child until shared inputs, prepared manifest,
qualification, calibration, code identity, and generated Spatter provenance
all pass their content-hash gates.
