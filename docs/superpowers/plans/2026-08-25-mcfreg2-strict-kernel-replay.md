# MCFREG2 Strict Kernel-Live-In Replay Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace row-consistency validation with independent bit-exact execution of every captured MCF pricing and price-out kernel, then regenerate a formally qualified content-addressed package from an immutable frozen input.

**Architecture:** The native capture separates kernel live-ins from observed results. A streaming C++ replayer reconstructs one active call, derives all decisions and live-outs without copying asserted outputs, and compares canonical pre/post state. The Python generator derives provenance from immutable build/run records, and the formal freezer launches the strict replayer before accepting MCFREG2.

**Tech Stack:** Python 3 standard library, C11, C++17, zlib, SHA-256, `unittest`, Git path-scoped inspection, atomic filesystem publication.

**Spec:** `docs/superpowers/specs/2026-08-25-mcfreg2-strict-kernel-replay-design.md`

## Global Constraints

- Preserve `/home/victoryang00/CXLMemUring/bench/mcf` and use source commit `2b30de22399402d8c44bd74b8ebf743b6a6a55e9`.
- The only approved input SHA-256 is `aceb933893790cd957ec9d03d34660ba756a70d87b65caa9809e3a48443ba849`.
- All three native executions must read one immutable copied input object; none may reopen the original input after freezing.
- Keep external format name `MCFREG2`, advance the binary schema to 3, and reject schema 2 in formal mode.
- Treat captured live-ins as inputs and observed native outcomes only as comparison targets; replay may never copy an observed result into derived state.
- Require authority/capture-primary/capture-replay final-state and `mcf.out` byte equality.
- Require primary/replay package byte equality, strict replay zero mismatches, and exact trace hash equality.
- Recompute allocation timeline and require exact peak `1,757,471,072` bytes for the approved run and at least `345,000,000` bytes for formal qualification.
- Preserve all failed evidence and the old package; quarantine the old SHA root rather than deleting it.
- Never modify, stage, or commit the user's `src/mem/cache/base.cc` change.
- Do not create or edit the six-workload `paper-input-record.json` in this plan.

---

## File map

Create:

- `util/amu/matched_workloads/mcfreg2_state.hh`: decoded stable-reference and mutable call-state types.
- `util/amu/matched_workloads/mcfreg2_state.cc`: streaming normalized-state and call-frame decoders plus canonical state digests.
- `util/amu/matched_workloads/mcfreg2_kernels.hh`: strict pricing and price-out kernel interfaces.
- `util/amu/matched_workloads/mcfreg2_kernels.cc`: independent kernel implementations with no native MCF linkage.

Modify:

- `scripts/mcfreg2.py`: schema-3 section/record validation and lazy section streams.
- `scripts/generate_mcfreg2_state.py`: immutable input, derived identity, schema-3 assembly, strict replay, capacity proof, root reuse, rejection, and failure records.
- `scripts/freeze_cross_system_inputs.py`: schema-3 and strict-replayer qualification.
- `scripts/audit_cross_system_input_record.py`: exact current MCFREG2 template.
- `scripts/build_matched_breadth_workloads.py`: link new replayer sources and preserve streaming.
- `util/amu/matched_workloads/mcfreg2_format.h`: schema-3 constants and typed record roles.
- `util/amu/matched_workloads/mcfreg2.hh`: parser/replay facade.
- `util/amu/matched_workloads/mcfreg2.cc`: offset-backed container parser and replay orchestration.
- `util/amu/matched_workloads/mcf_capture.h`: live-in/result/allocation capture interfaces.
- `util/amu/matched_workloads/mcf_capture.c`: canonical call snapshots, finalized results, remaps, adjacency, boundaries, and allocation timeline.
- `util/amu/matched_workloads/spec_mcf_capture.patch`: correctly placed observation hooks.
- `util/amu/matched_workloads/mcf_regions.cc`: schema-3 dispatch and source list.
- `tests/pyunit/cross_system/test_mcfreg2.py`.
- `tests/pyunit/cross_system/test_generate_mcfreg2_state.py`.
- `tests/pyunit/cross_system/test_freeze_cross_system_inputs.py`.
- `tests/pyunit/cross_system/test_audit_cross_system_input_record.py`.
- `tests/pyunit/cross_system/test_matched_region_build.py`.

Generated evidence remains under:

`/mnt/disk0/gem5-CXL-eval/cira-amu-m2ndp-spectrum/formal-inputs/mcf/`

---

### Task 1: Make the frozen input and build identity authoritative

**Files:**
- Modify: `scripts/generate_mcfreg2_state.py:170-420,1510-1600`
- Modify: `scripts/audit_cross_system_input_record.py:20-70`
- Test: `tests/pyunit/cross_system/test_generate_mcfreg2_state.py`
- Test: `tests/pyunit/cross_system/test_audit_cross_system_input_record.py`

**Interfaces:**
- Produces: `freeze_source(*, source_root, expected_commit, source_subdir, input_path, expected_input_sha256, destination)["copied_input"]`, `run_frozen_native_matrix(*, frozen, authority_binary, capture_binary, work_root) -> dict`, `derive_evidence_identity(source_record, authority_build, capture_build) -> dict`, and a template whose MCF keys exactly equal `freeze.REQUIRED["mcf"]`.
- Consumes: existing tracked-file rows, compiler identity, patch hashes, and native build records.

- [ ] **Step 1: Write failing TOCTOU and template tests**

Add tests equivalent to:

```python
def test_all_native_runs_use_the_frozen_input_after_source_drift(self):
    frozen = generator.freeze_source(
        source_root=self.source,
        expected_commit=self.commit,
        source_subdir="bench/mcf",
        input_path=self.source_input,
        expected_input_sha256=sha256(self.source_input),
        destination=self.root / "frozen",
    )
    Path(self.source_input).write_bytes(b"changed after freeze")
    with mock.patch.object(generator, "run_native") as run:
        generator.run_frozen_native_matrix(
            frozen=frozen,
            authority_binary=self.authority_binary,
            capture_binary=self.capture_binary,
            work_root=self.root / "runs",
        )
    self.assertEqual(
        {call.kwargs["input_path"] for call in run.call_args_list},
        {Path(frozen["copied_input"])},
    )

def test_copied_file_is_checked_against_recorded_source_hash(self):
    with self.assertRaisesRegex(generator.GenerationError, "frozen copy"):
        self.freeze_with_mutation_between_hash_and_copy()

def test_template_mcf_shape_matches_formal_required_shape(self):
    self.assertEqual(
        set(audit.template_record()["mcf"]), freeze.REQUIRED["mcf"]
    )
```

- [ ] **Step 2: Run the focused tests and verify the intended failures**

Run:

```bash
PYTHONPATH=. python3 tests/pyunit/cross_system/test_generate_mcfreg2_state.py GenerateMCFREG2Test.test_all_native_runs_use_the_frozen_input_after_source_drift -v
PYTHONPATH=. python3 tests/pyunit/cross_system/test_audit_cross_system_input_record.py InputAuditTest.test_template_mcf_shape_matches_formal_required_shape -v
```

Expected: the first records the original input path; the second reports missing MCFREG2 identity/validation fields.

- [ ] **Step 3: Return recorded file rows and copied-input identity**

Store immutable rows before copying:

```python
recorded = {
    relative.as_posix(): {
        "size": source.stat().st_size,
        "sha256": _sha256_file(source),
    }
    for relative, source in files
}
```

After each copy, compare target size/hash only with `recorded[name]`. Return:

```python
{
    "schema": 1,
    "source_root": str(source_root),
    "source_subdir": subdir.as_posix(),
    "source_commit": actual_commit,
    "source_tree_sha256": tree_digest,
    "tracked_file_count": len(recorded_rows),
    "tracked_files": sorted(recorded_rows, key=lambda row: row["path"]),
    "copied_input": str(copied_input.resolve()),
    "input_sha256": recorded[input_relative_to_repo.as_posix()]["sha256"],
}
```

Add `verify_frozen_input(frozen)` immediately before each launch.

- [ ] **Step 4: Derive identity from build records**

Implement:

```python
def derive_evidence_identity(source_record, authority_build, capture_build):
    required = (
        "source_commit", "source_tree_sha256", "input_sha256",
        "common_patch_sha256", "capture_patch_sha256",
        "capture_runtime_sha256", "wire_abi_sha256",
        "compiler_sha256", "compiler_version", "compiler_target",
        "authority_command_sha256", "capture_command_sha256",
        "authority_binary_sha256", "capture_binary_sha256",
        "generator_sha256", "python_reader_sha256",
        "cpp_reader_sha256", "cpp_kernel_sha256",
    )
```

Reject missing, extra, or inconsistent fields. Hash canonical command arrays rather than path-dependent stdout.

- [ ] **Step 5: Use one frozen input object for the native matrix**

Clone one canonical frozen tree for authority and capture patch stacks, but pass `Path(frozen["copied_input"])` to all three runs. Recheck its hash before and after every run.

- [ ] **Step 6: Update the audit template**

The MCF row must contain exactly:

```python
{
    "input": "REQUIRED_ABSOLUTE_MCFREG2_PATH",
    "input_sha256": "REQUIRED_SHA256",
    "allocated_bytes": 345_000_000,
    "source": "REQUIRED_ABSOLUTE_SOURCE_RECORD_PATH",
    "source_sha256": "REQUIRED_SHA256",
    "format": "MCFREG2",
    "source_commit": "REQUIRED_EXACT_COMMIT",
    "source_tree_sha256": "REQUIRED_SHA256",
    "validation": "REQUIRED_ABSOLUTE_VALIDATION_PATH",
    "validation_sha256": "REQUIRED_SHA256",
    "synthetic": False,
}
```

- [ ] **Step 7: Run focused modules and commit**

Run:

```bash
python3 -m unittest discover -s tests/pyunit/cross_system -p 'test_generate_mcfreg2_state.py' -v
python3 -m unittest discover -s tests/pyunit/cross_system -p 'test_audit_cross_system_input_record.py' -v
git diff --check
```

Commit:

```bash
git add scripts/generate_mcfreg2_state.py scripts/audit_cross_system_input_record.py tests/pyunit/cross_system/test_generate_mcfreg2_state.py tests/pyunit/cross_system/test_audit_cross_system_input_record.py
git commit -m "fix: bind MCF execution to frozen evidence"
```

### Task 2: Define schema-3 semantic roles and canonical boundaries

**Files:**
- Modify: `scripts/mcfreg2.py`
- Modify: `util/amu/matched_workloads/mcfreg2_format.h`
- Create: `util/amu/matched_workloads/mcfreg2_state.hh`
- Create: `util/amu/matched_workloads/mcfreg2_state.cc`
- Test: `tests/pyunit/cross_system/test_mcfreg2.py`

**Interfaces:**
- Produces: `MCFREG2_SCHEMA = 3`, `RecordRole::{LiveIn,ObservedResult}`, `CallFrameReader`, `CanonicalCallState`, `digestCallState(const CanonicalCallState&)`.
- Consumes: existing container directory and stable-reference ABI.

- [ ] **Step 1: Write failing schema and role-separation tests**

```python
def test_formal_schema_three_separates_inputs_and_observed_results(self):
    package = self.semantic_fixture(schema=3)
    frames = mcfreg2.validate_semantic_roles(package)
    self.assertEqual(frames[0].live_in_roles, {"pricing_scan", "basket"})
    self.assertEqual(frames[0].result_roles, {"candidate", "selection"})

def test_result_field_in_live_in_record_fails_closed(self):
    package = self.semantic_fixture(
        mutation=("live_in_scan", "selected_arc_id", 4)
    )
    with self.assertRaisesRegex(mcfreg2.FormatError, "record role"):
        mcfreg2.validate_semantic_roles(package)
```

Also test schema 2 rejection in formal mode, duplicate roles, missing call entry/exit, and a boundary digest made from JSON rows instead of canonical binary state.

- [ ] **Step 2: Run format tests and verify schema 3 is absent**

Run:

```bash
PYTHONPATH=. python3 tests/pyunit/cross_system/test_mcfreg2.py MCFREG2Test.test_formal_schema_three_separates_inputs_and_observed_results -v
```

Expected: failure because schema 3 and semantic roles are undefined.

- [ ] **Step 3: Add exact record kinds**

Define these schema-3 event kinds:

```text
CALL_BEGIN
PRICING_SCAN_LIVE_IN
PRICING_CANDIDATE_OBSERVED
BASKET_LIVE_IN
BASKET_LIVE_OUT_OBSERVED
PRICING_END_OBSERVED
PRICE_OUT_STATE_LIVE_IN
PRICE_OUT_CANDIDATE_OBSERVED
PRICE_OUT_DECISION_OBSERVED
ARC_FINAL_OBSERVED
REMAP_OBSERVED
ADJACENCY_FINAL_OBSERVED
PRICE_OUT_END_OBSERVED
CALL_END
```

Each kind has an exact allowed-key set. Require `role="live_in"` or `role="observed_result"` and reject a key belonging to the other role.

- [ ] **Step 4: Add canonical binary call-state encoding**

Implement stable, pointer-free encoders for:

```cpp
struct PricingLiveIn;
struct PricingDerivedOut;
struct PriceOutLiveIn;
struct PriceOutDerivedOut;
using CanonicalCallState = std::variant<
    PricingLiveIn, PricingDerivedOut, PriceOutLiveIn, PriceOutDerivedOut>;
```

Encode fixed-width integers in little endian, sorted stable maps by `(kind,generation,index)`, and ordered vectors in native order. `digestCallState()` hashes the bytes directly.

- [ ] **Step 5: Make Python and C++ boundary digests agree**

Add a cross-language probe that prints pre/post digests for the same fixture and compare exact hex strings.

- [ ] **Step 6: Run format/parity tests and commit**

Run:

```bash
python3 -m unittest discover -s tests/pyunit/cross_system -p 'test_mcfreg2.py' -v
```

Commit:

```bash
git add scripts/mcfreg2.py util/amu/matched_workloads/mcfreg2_format.h util/amu/matched_workloads/mcfreg2_state.hh util/amu/matched_workloads/mcfreg2_state.cc tests/pyunit/cross_system/test_mcfreg2.py
git commit -m "feat: define strict MCFREG2 call states"
```

### Task 3: Capture separated native live-ins and finalized results

**Files:**
- Modify: `util/amu/matched_workloads/mcf_capture.h`
- Modify: `util/amu/matched_workloads/mcf_capture.c`
- Modify: `util/amu/matched_workloads/spec_mcf_capture.patch`
- Modify: `scripts/generate_mcfreg2_state.py:650-1050`
- Test: `tests/pyunit/cross_system/test_generate_mcfreg2_state.py`

**Interfaces:**
- Produces: schema-3 journals with exact live-in/result roles, allocation events, and native canonical boundary digests.
- Consumes: schema-3 record definitions and canonical stable references from Task 2.

- [ ] **Step 1: Write failing hook-placement and completeness tests**

Add C harness tests that assert:

```python
def test_arc_final_is_recorded_after_flow_ident_and_links(self):
    frame = self.capture_fixture("price-out-insert").price_out_calls[0]
    final = frame.observed("ARC_FINAL_OBSERVED")
    self.assertEqual(final["flow"], 0)
    self.assertEqual(final["ident"], AT_LOWER)
    self.assertIsNotNone(final["nextout"])
    self.assertIsNotNone(final["nextin"])

def test_remap_contains_every_live_arc_reference(self):
    frame = self.capture_fixture("price-out-resize").price_out_calls[0]
    self.assertEqual(
        len(frame.observed_all("REMAP_OBSERVED")),
        frame.live_in["arena_live_elements"],
    )
```

Also require complete adjacency final state, all changed network counters, and distinct live-in/result key sets.

- [ ] **Step 2: Run focused capture tests and verify they fail**

Run:

```bash
PYTHONPATH=. python3 tests/pyunit/cross_system/test_generate_mcfreg2_state.py GenerateMCFREG2Test.test_arc_final_is_recorded_after_flow_ident_and_links -v
PYTHONPATH=. python3 tests/pyunit/cross_system/test_generate_mcfreg2_state.py GenerateMCFREG2Test.test_remap_contains_every_live_arc_reference -v
```

Expected: current arc-state hooks precede final `flow`/`ident`/link writes and remap emits only a count.

- [ ] **Step 3: Capture pricing inputs before computation**

Replace combined scan records with:

```c
int mcf_capture_pricing_scan_live_in(
    const arc_t *arc, long group_pos, long scan_position);
int mcf_capture_pricing_candidate_observed(
    const arc_t *arc, cost_t reduced_cost, int candidate, long basket_slot);
```

The live-in hook reads stable IDs, cost, ident, and potentials before native reduced-cost/candidate code. The observed hook records only the result and target slot.

- [ ] **Step 4: Capture a complete price-out entry state**

`mcf_capture_price_out_begin()` serializes every field read by `price_out_impl()`, `insert_new_arc()`, `replace_weaker_arc()`, `resize_prob()`, and adjacency refresh. Validate all stable references while serializing.

- [ ] **Step 5: Move result hooks after final native mutations**

Remove intermediate `mcf_capture_price_out_arc_state()` calls from heap helper bodies. After `flow`, `ident`, `nextout`, `nextin`, node first pointers, stop pointer, and counters are finalized, emit one exact observed result per affected object.

- [ ] **Step 6: Emit complete remaps and allocation timeline**

For each resize, emit one mapping for each live old-generation arc plus:

```json
{"kind":"ALLOC","allocation_kind":"arcs","elements":0,
 "element_bytes":96,"old_capacity":0,"new_capacity":0,
 "requested_bytes":0,"current_bytes":0,"peak_bytes":0}
```

Use checked C arithmetic and the actual native `sizeof` value rather than the example zeros.

- [ ] **Step 7: Compute native binary boundary digests**

Hash the same canonical live-in and finalized live-out encodings defined in Task 2. Store those hashes in `BOUNDARIES`; do not hash JSON arrays.

- [ ] **Step 8: Assemble schema-3 sections and run native fixture matrix**

Require authority/primary/replay final state and `mcf.out` equality, journal role validity, complete result sets, and primary/replay byte equality.

- [ ] **Step 9: Run generator tests and commit**

Run:

```bash
python3 -m unittest discover -s tests/pyunit/cross_system -p 'test_generate_mcfreg2_state.py' -v
```

Commit:

```bash
git add util/amu/matched_workloads/mcf_capture.h util/amu/matched_workloads/mcf_capture.c util/amu/matched_workloads/spec_mcf_capture.patch scripts/generate_mcfreg2_state.py tests/pyunit/cross_system/test_generate_mcfreg2_state.py
git commit -m "feat: capture strict MCF kernel contracts"
```

### Task 4: Independently execute the pricing kernel

**Files:**
- Create: `util/amu/matched_workloads/mcfreg2_kernels.hh`
- Create: `util/amu/matched_workloads/mcfreg2_kernels.cc`
- Modify: `util/amu/matched_workloads/mcfreg2.cc:1170-1470`
- Modify: `tests/pyunit/cross_system/test_mcfreg2.py`

**Interfaces:**
- Produces: `PricingDerivedOut replayPricing(const PricingLiveIn&, TraceSink&)`.
- Consumes: `PricingLiveIn`, canonical state digest, and observed-result comparison records.

- [ ] **Step 1: Write coupled-forgery tests**

```python
def test_pricing_rejects_coupled_result_forgery(self):
    package = self.semantic_fixture(mutation="coupled-pricing-output")
    completed = self.run_cpp_replayer(package, check=False)
    self.assertNotEqual(completed.returncode, 0)
    self.assertIn("derived pricing result differs", completed.stderr)

def test_pricing_live_in_change_rederives_result(self):
    package = self.semantic_fixture(mutation="tail-potential-with-rehashed-results")
    completed = self.run_cpp_replayer(package, check=False)
    self.assertIn("pricing pre-boundary", completed.stderr)
```

Mutate reduced cost, candidate, basket order, and selection together so old row-consistency replay would pass.

- [ ] **Step 2: Run tests and verify current replay accepts at least one forgery**

Run:

```bash
PYTHONPATH=. python3 tests/pyunit/cross_system/test_mcfreg2.py MCFREG2Test.test_pricing_rejects_coupled_result_forgery -v
```

Expected: test fails because current replay derives from asserted row operands/results rather than a separate call state.

- [ ] **Step 3: Implement exact scan traversal and reduced cost**

`replayPricing()` derives every scan ID from `m`, group count, and group position; consumes exactly one matching live-in record; computes signed native-width reduced cost; and emits canonical LOAD/I64_ADD operations from computed values.

- [ ] **Step 4: Implement exact persistent basket semantics**

Reimplement retained-basket initialization, candidate insertion, native comparator/tie behavior, sorting, selection, group-position update, and count. Do not call or copy code from native `pbeampp.c`.

- [ ] **Step 5: Compare derived state and canonical digests**

Compare every observed candidate/slot, basket row, selected ID/cost, counts, and the independently serialized post-boundary digest.

- [ ] **Step 6: Run pricing tests and commit**

Run:

```bash
python3 -m unittest discover -s tests/pyunit/cross_system -p 'test_mcfreg2.py' -v
```

Commit:

```bash
git add util/amu/matched_workloads/mcfreg2_kernels.hh util/amu/matched_workloads/mcfreg2_kernels.cc util/amu/matched_workloads/mcfreg2.cc tests/pyunit/cross_system/test_mcfreg2.py
git commit -m "feat: replay MCF pricing from live ins"
```

### Task 5: Independently execute the price-out kernel

**Files:**
- Modify: `util/amu/matched_workloads/mcfreg2_kernels.hh`
- Modify: `util/amu/matched_workloads/mcfreg2_kernels.cc`
- Modify: `util/amu/matched_workloads/mcfreg2.cc`
- Modify: `tests/pyunit/cross_system/test_mcfreg2.py`

**Interfaces:**
- Produces: `PriceOutDerivedOut replayPriceOut(const PriceOutLiveIn&, TraceSink&)`.
- Consumes: complete call-entry network, arena, nodes, arcs, sparse relationships, capacity, and observed results.

- [ ] **Step 1: Write adversarial decision and topology tests**

Add separate failing tests for:

```text
negative reduced cost recorded as NO_CHANGE
INSERT recorded after residual heap capacity is exhausted
REPLACE targeting a non-weakest heap slot
missing final flow or ident
missing nextout or nextin
missing node firstout or firstin
omitted or duplicate remap entry
stale-generation reference after resize
wrong m, m_impl, max_residual_new_m, or stop_arcs
```

- [ ] **Step 2: Run four representative tests and verify failures**

Run the no-change, replacement-slot, remap, and adjacency tests directly. Expected: at least one mutation passes the old replayer or fails for only row-shape reasons rather than derived semantics.

- [ ] **Step 3: Reconstruct the mutable call-entry network**

Decode stable references into index-based mutable objects. Validate every endpoint and link, exactly one arena generation, capacity bounds, stop position, sparse-list source relationship, and network scalar.

- [ ] **Step 4: Implement resize and complete remap derivation**

Derive the native resize predicate, checked new capacity, and a mapping for every live arc. Rewrite all stored arc references and reject any stale generation.

- [ ] **Step 5: Implement sparse traversal and candidate decisions**

Reproduce `price_out_impl()` traversal from reconstructed state, derive tail/head/cost/potentials, reduced cost, and `NO_CHANGE`/`INSERT`/`REPLACE` without reading observed decisions.

- [ ] **Step 6: Implement the residual heap and finalized links**

Reproduce insert/replacement heap movement, then finalize `flow`, `ident`, costs, endpoints, adjacency lists, node first pointers, stop position, `m`, `m_impl`, and remaining capacity.

- [ ] **Step 7: Compare complete derived output**

Require an exact one-to-one observed result for every candidate, decision, remap, affected arc, affected node, and network field. Compare canonical post-state digest.

- [ ] **Step 8: Run semantic tests and commit**

Run:

```bash
python3 -m unittest discover -s tests/pyunit/cross_system -p 'test_mcfreg2.py' -v
```

Commit:

```bash
git add util/amu/matched_workloads/mcfreg2_kernels.hh util/amu/matched_workloads/mcfreg2_kernels.cc util/amu/matched_workloads/mcfreg2.cc tests/pyunit/cross_system/test_mcfreg2.py
git commit -m "feat: replay MCF price out from live ins"
```

### Task 6: Stream the package and enforce semantic replay in qualification

**Files:**
- Modify: `scripts/mcfreg2.py`
- Modify: `util/amu/matched_workloads/mcfreg2.hh`
- Modify: `util/amu/matched_workloads/mcfreg2.cc`
- Modify: `scripts/freeze_cross_system_inputs.py:150-285`
- Modify: `scripts/build_matched_breadth_workloads.py`
- Modify: `util/amu/matched_workloads/mcf_regions.cc`
- Test: `tests/pyunit/cross_system/test_mcfreg2.py`
- Test: `tests/pyunit/cross_system/test_freeze_cross_system_inputs.py`
- Test: `tests/pyunit/cross_system/test_matched_region_build.py`

**Interfaces:**
- Produces: offset-backed `SectionView`, `run_strict_mcfreg2_replay(package, output_root) -> replay_record`, and formal validator replay binding.
- Consumes: schema-3 package and kernel replay facade.

- [ ] **Step 1: Write failing semantic qualification and RSS tests**

```python
def test_structural_but_semantically_arbitrary_package_is_rejected(self):
    row = self.valid_mcf_record(semantic_bytes=b"arbitrary\n")
    with self.assertRaisesRegex(freeze.InputError, "semantic replay"):
        freeze.validate_mcf_record(row)

def test_formal_reader_rss_is_bounded_by_active_call(self):
    small = self.measure_replayer_rss(event_repetitions=1)
    large = self.measure_replayer_rss(event_repetitions=100_000)
    self.assertLessEqual(large - small, 64 * 1024 * 1024)
```

- [ ] **Step 2: Run tests and verify current structural validation passes arbitrary semantics or scales with package size**

Run both focused tests. Record the expected pre-fix failure mode.

- [ ] **Step 3: Replace section vectors with offset-backed views**

Store directory metadata plus an input path/descriptor. Stream section SHA-256 in bounded chunks. `JsonLineReader` reads only the active compressed/raw block and compacts with a cursor, never per row.

- [ ] **Step 4: Link the new state/kernel sources everywhere**

Add `mcfreg2_state.cc` and `mcfreg2_kernels.cc` to probe, generator replayer, matched breadth builder, and formal `mcf_regions` compile commands. Bind all source hashes in manifests.

- [ ] **Step 5: Launch strict replay from the freezer**

`validate_mcf_record()` creates a private temporary replay root, builds or invokes the identity-bound strict replayer, checks zero mismatches/counts/trace hash, and removes only that temporary root. A missing compiler/binary or nonzero exit is a formal input failure.

- [ ] **Step 6: Replace arbitrary test fixtures with executable semantics**

Audit/freezer/builder fixtures use the smallest valid pricing and price-out call frames. Do not weaken production validation to accommodate arbitrary section bytes.

- [ ] **Step 7: Run qualification/build tests and commit**

Run:

```bash
python3 -m unittest discover -s tests/pyunit/cross_system -p 'test_mcfreg2.py' -v
python3 -m unittest discover -s tests/pyunit/cross_system -p 'test_freeze_cross_system_inputs.py' -v
python3 -m unittest discover -s tests/pyunit/cross_system -p 'test_matched_region_build.py' -v
python3 -m unittest discover -s tests/pyunit/cross_system -p 'test_audit_cross_system_input_record.py' -v
```

Commit:

```bash
git add scripts/mcfreg2.py scripts/freeze_cross_system_inputs.py scripts/build_matched_breadth_workloads.py util/amu/matched_workloads/mcfreg2.hh util/amu/matched_workloads/mcfreg2.cc util/amu/matched_workloads/mcf_regions.cc tests/pyunit/cross_system/test_mcfreg2.py tests/pyunit/cross_system/test_freeze_cross_system_inputs.py tests/pyunit/cross_system/test_matched_region_build.py tests/pyunit/cross_system/test_audit_cross_system_input_record.py
git commit -m "feat: enforce strict MCF replay in formal inputs"
```

### Task 7: Recompute capacity and make publication fully fail-closed

**Files:**
- Modify: `scripts/generate_mcfreg2_state.py:1200-1740`
- Modify: `scripts/freeze_cross_system_inputs.py`
- Test: `tests/pyunit/cross_system/test_generate_mcfreg2_state.py`
- Test: `tests/pyunit/cross_system/test_freeze_cross_system_inputs.py`

**Interfaces:**
- Produces: `recompute_allocation_timeline(events) -> AllocationSummary`, `verify_existing_root(root, expected_identity)`, `reject_accepted_root(root, finding)`, and unique failure records.
- Consumes: schema-3 allocation events, published tree manifest, strict replay record.

- [ ] **Step 1: Write failing capacity/root/failure tests**

```python
def test_forged_peak_is_rejected_by_allocation_timeline(self):
    events = self.valid_allocation_events()
    events[-1] = {**events[-1], "peak_bytes": events[-1]["peak_bytes"] + 1}
    with self.assertRaisesRegex(generator.GenerationError, "allocation peak"):
        generator.recompute_allocation_timeline(events)

def test_existing_root_with_corrupt_published_file_is_not_reused(self):
    root, identity = self.publish_valid_root()
    (root / "capture-primary/run.json").write_text("corrupt\n")
    with self.assertRaisesRegex(generator.GenerationError, "published tree"):
        generator.verify_existing_root(root, identity)

def test_failure_records_are_unique_and_preserve_first_gate(self):
    first = generator.write_failure_record(
        self.root, gate="frozen_input", identity=self.identity,
        error="first", diagnostics={"command": ["mcf"]},
    )
    second = generator.write_failure_record(
        self.root, gate="semantic_replay", identity=self.identity,
        error="second", diagnostics={"command": ["replayer"]},
    )
    self.assertNotEqual(first, second)
    self.assertEqual(json.loads(first.read_text())["first_failed_gate"],
                     "frozen_input")

def test_rejected_root_is_preserved_but_not_discoverable_as_accepted(self):
    root, identity = self.publish_valid_root()
    rejected = generator.reject_accepted_root(
        root, {"finding": "weak semantic replay"}
    )
    self.assertTrue((rejected / "rejection.json").is_file())
    self.assertNotIn(root, generator.accepted_roots(self.root))
```

- [ ] **Step 2: Run tests and verify current weaknesses**

Expected: copied peak passes, partial existing-root comparison passes, repeated failure overwrites `failed-input.json`, and no safe rejection primitive exists.

- [ ] **Step 3: Recompute the allocation timeline**

For every allocation event, check `elements * element_bytes`, old/new capacity, requested/current/peak bytes, and kind totals with checked arithmetic. Require exact agreement across primary/replay/run/FINAL/validation.

- [ ] **Step 4: Verify complete existing roots before reuse**

Call the same package, identity, source, published-tree, allocation, and fresh strict-replay gates used by `verify --accepted`. If any check fails, preserve the root and fail; never silently reuse it.

- [ ] **Step 5: Preserve unique failures and the first gate**

Write `output_root / f"failed-input.{identity_digest[:16]}.{attempt_id}.json"` atomically. Store a separate atomic `latest-failure.json` pointer. Propagate an inner `GenerationError.gate` rather than replacing it with `generation`.

- [ ] **Step 6: Add safe accepted-root rejection**

`reject_accepted_root()` validates the exact expected SHA directory, package hash, and absence of `root.parent / f".rejected-{root.name}"`, writes `rejection.json`, fsyncs, then performs that exact rename. It never accepts `/`, a workspace root, a glob, or an unresolved path.

- [ ] **Step 7: Run focused tests and commit**

Run:

```bash
python3 -m unittest discover -s tests/pyunit/cross_system -p 'test_generate_mcfreg2_state.py' -v
python3 -m unittest discover -s tests/pyunit/cross_system -p 'test_freeze_cross_system_inputs.py' -v
```

Commit:

```bash
git add scripts/generate_mcfreg2_state.py scripts/freeze_cross_system_inputs.py tests/pyunit/cross_system/test_generate_mcfreg2_state.py tests/pyunit/cross_system/test_freeze_cross_system_inputs.py
git commit -m "fix: close MCF evidence publication gaps"
```

### Task 8: Run the complete adversarial and integration gate

**Files:**
- Modify only tests or implementation needed by failures from Tasks 1-7.

**Interfaces:**
- Produces: a reviewed schema-3 implementation ready for a fresh evidence run.
- Consumes: all prior task interfaces.

- [ ] **Step 1: Run the focused semantic/adversarial modules**

Run:

```bash
python3 -m unittest discover -s tests/pyunit/cross_system -p 'test_mcfreg2.py' -v
python3 -m unittest discover -s tests/pyunit/cross_system -p 'test_generate_mcfreg2_state.py' -v
python3 -m unittest discover -s tests/pyunit/cross_system -p 'test_freeze_cross_system_inputs.py' -v
python3 -m unittest discover -s tests/pyunit/cross_system -p 'test_audit_cross_system_input_record.py' -v
python3 -m unittest discover -s tests/pyunit/cross_system -p 'test_matched_region_build.py' -v
```

Expected: coupled-forgery, topology, remap, allocation, TOCTOU, existing-root, and arbitrary-semantics tests all fail closed; valid fixtures pass.

- [ ] **Step 2: Run static gates**

```bash
python3 -m compileall -q scripts
git diff --check
```

- [ ] **Step 3: Request an independent code review**

Review the range from `56cde54628` to current HEAD against the strict replay spec. Any Critical or Important semantic/provenance issue blocks evidence generation.

- [ ] **Step 4: Fix review findings one at a time with a red/green test**

For each accepted finding, add the smallest failing test, run it red, implement one fix, run focused tests green, and commit a path-scoped change.

- [ ] **Step 5: Commit any final reviewed test changes**

Use a message describing the actual fixed gate; do not create an empty review commit.

### Task 9: Quarantine weak evidence and generate the formal schema-3 package

**Files:**
- Evidence only under `/mnt/disk0/gem5-CXL-eval/cira-amu-m2ndp-spectrum/formal-inputs/mcf/`

**Interfaces:**
- Produces: one accepted schema-3 SHA root and one preserved `.rejected-2d3ad115b8a83afa7ba94e507c33d65ea7bc8ac811faa8d262828e0c81b1065b` audit root.
- Consumes: reviewed Task-8 code and the approved source/input identity.

- [ ] **Step 1: Reverify the exact old root before rejection**

Require exact path and SHA:

```text
2d3ad115b8a83afa7ba94e507c33d65ea7bc8ac811faa8d262828e0c81b1065b
```

Record its package, manifest, validation, source, and old trace hashes. Refuse rejection if any path or hash differs from the inspected root.

- [ ] **Step 2: Atomically quarantine the old root**

Run the tested `reject-accepted` CLI. Verify the dot-prefixed rejected root exists, contains `rejection.json`, and no accepted-root scan returns the old schema-2 package.

- [ ] **Step 3: Run formal preflight**

```bash
python3 scripts/generate_mcfreg2_state.py preflight \
  --source-root /home/victoryang00/CXLMemUring \
  --source-commit 2b30de22399402d8c44bd74b8ebf743b6a6a55e9 \
  --source-subdir bench/mcf \
  --input /home/victoryang00/CXLMemUring/bench/mcf/data/ref/input/inp.in \
  --input-sha256 aceb933893790cd957ec9d03d34660ba756a70d87b65caa9809e3a48443ba849 \
  --output-root /mnt/disk0/gem5-CXL-eval/cira-amu-m2ndp-spectrum/formal-inputs/mcf
```

Expected: ready, 36 tracked files, exact input identity, LP64 ABI, compiler available, and enough memory/disk.

- [ ] **Step 4: Generate without a timeout**

Run the same `generate` command and wait through authority, two captures, assembly, and strict replay. Do not resume from schema-2 journals or the rejected package.

- [ ] **Step 5: Verify accepted evidence independently**

```bash
python3 scripts/generate_mcfreg2_state.py verify \
  --output-root /mnt/disk0/gem5-CXL-eval/cira-amu-m2ndp-spectrum/formal-inputs/mcf \
  --accepted
```

Require one schema-3 SHA root, exact package-directory hash, complete published trees, allocation peak `1,757,471,072`, `1,208,697` pricing calls, `1` price-out call, zero mismatches, and a fresh strict trace hash.

- [ ] **Step 6: Run formal candidate qualification**

```bash
accepted_root=$(find \
  /mnt/disk0/gem5-CXL-eval/cira-amu-m2ndp-spectrum/formal-inputs/mcf \
  -maxdepth 1 -type d -name '[0-9a-f]*' -print)
test "$(printf '%s\n' "$accepted_root" | sed '/^$/d' | wc -l)" -eq 1
python3 scripts/generate_mcfreg2_state.py validate-candidate \
  --candidate "$accepted_root/candidate-record.json"
```

Expected: candidate validated only after another strict semantic replay.

- [ ] **Step 7: Record final exact artifacts**

Record package size/SHA, manifest/validation/source hashes, hardlink inodes, call/event/operation counts, allocation timeline summary, final-state/output hashes, strict replay binary/source hashes, and all commands/logs.

### Task 10: Final regression, review, and push

**Files:**
- Verify only; preserve `src/mem/cache/base.cc`.

**Interfaces:**
- Produces: pushed `m2ndp-g20-pr-spmv` branch and final evidence handoff.
- Consumes: accepted schema-3 evidence and all committed code.

- [ ] **Step 1: Use verification-before-completion**

Read and follow `/root/.codex/skills/verification-before-completion/SKILL.md`; rerun evidence checks instead of relying on prior output.

- [ ] **Step 2: Run complete regressions**

```bash
python3 -m unittest discover -s tests/pyunit/cross_system -p 'test_*.py' -v
python3 -m unittest discover -s tests/pyunit/m2ndp -p 'test_*.py' -v
python3 -m compileall -q scripts
git diff --check
```

Only the two already documented `M2NDP_REAL_FUNCSIM` integration skips are acceptable unless the environment is explicitly supplied.

- [ ] **Step 3: Verify Git and evidence scope**

```bash
git status --short
git diff -- src/mem/cache/base.cc
git log --oneline origin/m2ndp-g20-pr-spmv..HEAD
git ls-files | rg '^/mnt/disk0|formal-inputs/mcf/.+\.reg2$' && exit 1 || true
```

Expected: only user `base.cc` is dirty and unstaged; no evidence is tracked.

- [ ] **Step 4: Request final independent code review**

Review the full strict-replay range and require `Ready to merge: Yes`. Fix any Critical/Important item with a focused red/green cycle before continuing.

- [ ] **Step 5: Use finishing-a-development-branch**

Read and follow `/root/.codex/skills/finishing-a-development-branch/SKILL.md`. The user has already chosen push for this branch; do not merge into another branch in this task.

- [ ] **Step 6: Push**

```bash
git push origin m2ndp-g20-pr-spmv
```

Report pushed HEAD, schema-3 package/validation paths and hashes, allocation/call/event/operation counts, bit-exact strict replay evidence, test totals/skips, rejected-old-root path, and preservation of the user dirty file.
