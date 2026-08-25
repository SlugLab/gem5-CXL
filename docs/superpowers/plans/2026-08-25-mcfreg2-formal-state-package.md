# Formal MCFREG2 State Package Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Generate and independently validate a deterministic `MCFREG2` package from the approved SPEC-derived MCF reference input, then bind it to the formal cross-system input pipeline.

**Architecture:** A Python generator freezes and copies the authoritative source, applies content-hashed common/capture patches, builds authority and capture binaries, and atomically publishes a sectioned binary package. A Python structural validator and an independent C++ semantic replayer both verify the package; matched MCF permits `MCFREG1` only for fixtures and requires `MCFREG2` for formal runs.

**Tech Stack:** Python 3 standard library, C11, C++17, `unittest`, Git path-scoped inspection, SHA-256, and the existing canonical matched-workload trace ABI.

---

## File map

Create:

- `scripts/mcfreg2.py`: wire layouts, parser, writer, canonical digest helpers.
- `scripts/generate_mcfreg2_state.py`: source freezing, builds, runs, evidence
  comparison, failure records, and atomic publication.
- `util/amu/matched_workloads/mcfreg2_format.h`: shared C/C++ wire ABI.
- `util/amu/matched_workloads/mcfreg2.hh`: C++ reader/replayer interface.
- `util/amu/matched_workloads/mcfreg2.cc`: C++ parsing and semantic replay.
- `util/amu/matched_workloads/mcf_capture.h`: C capture interface.
- `util/amu/matched_workloads/mcf_capture.c`: stable IDs, allocation evidence,
  normalized state, and event journal.
- `util/amu/matched_workloads/spec_mcf_common.patch`: input, ROI, allocation,
  and final-state hooks common to authority and capture builds.
- `util/amu/matched_workloads/spec_mcf_capture.patch`: pricing and price-out
  observation hooks used only by the capture build.
- `tests/pyunit/cross_system/test_mcfreg2.py`: format, parity, and replay tests.
- `tests/pyunit/cross_system/test_generate_mcfreg2_state.py`: source, native
  capture, determinism, publication, and failure tests.

Modify:

- `util/amu/matched_workloads/mcf_regions.cc`: magic dispatch and canonical
  trace emission from independently replayed MCFREG2 events.
- `scripts/build_matched_breadth_workloads.py`: MCFREG2 formal reference build.
- `scripts/freeze_cross_system_inputs.py`: validated MCFREG2 paper contract.
- `tests/pyunit/cross_system/test_matched_region_build.py`.
- `tests/pyunit/cross_system/test_freeze_cross_system_inputs.py`.

Generated binaries and evidence remain under `/mnt/disk0`; never add them to
Git. Never modify or stage the user's `src/mem/cache/base.cc` change.

### Task 1: Define the Python MCFREG2 wire format

**Files:**
- Create: `scripts/mcfreg2.py`
- Create: `tests/pyunit/cross_system/test_mcfreg2.py`

- [ ] **Step 1: Write failing round-trip and corruption tests**

```python
class MCFREG2Test(unittest.TestCase):
    def test_minimal_package_round_trips(self):
        path = self.root / "minimal.reg2"
        expected = self.fixture_package()
        digest = mcfreg2.write_package(path, expected)
        actual = mcfreg2.read_package(path)
        self.assertEqual(actual.header.magic, b"MCFREG2\0")
        self.assertEqual(actual.section_names(), mcfreg2.REQUIRED_SECTIONS)
        self.assertEqual(mcfreg2.sha256_file(path), digest)

    def test_overlap_and_trailing_bytes_fail_closed(self):
        overlap = self.corrupt_fixture("overlap")
        with self.assertRaisesRegex(mcfreg2.FormatError, "overlap"):
            mcfreg2.read_package(overlap)
        trailing = self.corrupt_fixture("trailing")
        with self.assertRaisesRegex(mcfreg2.FormatError, "trailing"):
            mcfreg2.read_package(trailing)
```

Also cover truncation, bad section SHA-256, nonzero reserved fields, duplicate
required sections, unknown mandatory sections, the null stable-reference
sentinel, the maximum legal stable ID, and an out-of-range stable ID.

- [ ] **Step 2: Run the test and verify the module is absent**

Run: `python3 -m unittest discover -s tests/pyunit/cross_system -p 'test_mcfreg2.py' -v`

Expected: import failure for `scripts.mcfreg2`.

- [ ] **Step 3: Implement the exact packed layouts**

```python
MAGIC = b"MCFREG2\0"
SCHEMA = 2
ENDIAN_TAG = 0x0102
HEADER = struct.Struct("<8sHHI11Q")
DIRECTORY = struct.Struct("<HHIQQQQ32s")
STABLE_REF = struct.Struct("<IIQ")
OPTIONAL_FLAG = 1
SECTION_TYPES = {
    "PROVENANCE": 1, "NETWORK": 2, "NODES": 3, "ARCS": 4,
    "BASKET": 5, "CALL_INDEX": 6, "EVENTS": 7, "DELTAS": 8,
    "BOUNDARIES": 9, "FINAL": 10,
}
REQUIRED_SECTIONS = tuple(SECTION_TYPES)

class FormatError(RuntimeError):
    pass
```

Interpret the eleven 64-bit header words as `flags`, `section_count`,
`directory_offset`, `nodes`, `active_arcs`, `dummy_arcs`, `arena_capacity`,
`pricing_calls`, `price_out_calls`, `event_count`, and `reserved`. Require the
directory immediately after the 104-byte header; check every arithmetic
operation; require sorted, non-overlapping sections; verify hashes; require all
mandatory sections exactly once and exact EOF.

`write_package()` writes a sibling temporary file, flushes and fsyncs, rereads
with `read_package()`, then uses `os.replace()`. It returns the final SHA-256.

- [ ] **Step 4: Run the format tests**

Run: `python3 -m unittest discover -s tests/pyunit/cross_system -p 'test_mcfreg2.py' -v`

Expected: all format and corruption tests pass.

- [ ] **Step 5: Commit**

```bash
git add scripts/mcfreg2.py tests/pyunit/cross_system/test_mcfreg2.py
git commit -m "feat: define MCFREG2 binary format"
```

### Task 2: Add cross-language wire parity

**Files:**
- Create: `util/amu/matched_workloads/mcfreg2_format.h`
- Create: `util/amu/matched_workloads/mcfreg2.hh`
- Create: `util/amu/matched_workloads/mcfreg2.cc`
- Modify: `tests/pyunit/cross_system/test_mcfreg2.py`

- [ ] **Step 1: Write a failing Python-to-C++ reader test**

```python
def test_cpp_reader_matches_python_directory(self):
    path = self.write_fixture()
    probe = self.compile_cpp_probe()
    completed = subprocess.run(
        [probe, path], text=True, stdout=subprocess.PIPE, check=True
    )
    self.assertEqual(
        json.loads(completed.stdout),
        mcfreg2.read_package(path).directory_json(),
    )
```

- [ ] **Step 2: Run the focused test and verify compilation fails**

Run: `PYTHONPATH=. python3 tests/pyunit/cross_system/test_mcfreg2.py MCFREG2Test.test_cpp_reader_matches_python_directory -v`

Expected: the C++ header/source do not exist.

- [ ] **Step 3: Implement packed ABI and parser**

Declare `McfReg2Header`, `McfReg2DirectoryEntry`, and `McfStableRef` under
`#pragma pack(push, 1)` and require:

```cpp
static_assert(sizeof(McfReg2Header) == 104, "MCFREG2 header drift");
static_assert(sizeof(McfReg2DirectoryEntry) == 72,
              "MCFREG2 directory drift");
static_assert(sizeof(McfStableRef) == 16, "MCFREG2 reference drift");
```

Expose:

```cpp
namespace mcfreg2 {
class Error : public std::runtime_error {
  public:
    using std::runtime_error::runtime_error;
};
struct Package {
    McfReg2Header header;
    std::vector<McfReg2DirectoryEntry> directory;
    std::map<uint16_t, std::vector<uint8_t>> sections;
};
Package readPackage(const std::string &path);
std::string directoryJson(const Package &package);
}
```

Implement SHA-256 locally in `mcfreg2.cc` only if no already-linked repository
implementation is available. Test it against empty-string and `abc` vectors;
do not shell out to another process.

- [ ] **Step 4: Run all format/parity tests**

Run: `python3 -m unittest discover -s tests/pyunit/cross_system -p 'test_mcfreg2.py' -v`

Expected: Python and C++ accept the same fixture and reject all corruptions.

- [ ] **Step 5: Commit**

```bash
git add util/amu/matched_workloads/mcfreg2_format.h util/amu/matched_workloads/mcfreg2.hh util/amu/matched_workloads/mcfreg2.cc tests/pyunit/cross_system/test_mcfreg2.py
git commit -m "feat: parse MCFREG2 in C++"
```

### Task 3: Freeze source/input and add the common native harness

**Files:**
- Create: `scripts/generate_mcfreg2_state.py`
- Create: `tests/pyunit/cross_system/test_generate_mcfreg2_state.py`
- Create: `util/amu/matched_workloads/mcf_capture.h`
- Create: `util/amu/matched_workloads/mcf_capture.c`
- Create: `util/amu/matched_workloads/spec_mcf_common.patch`

- [ ] **Step 1: Write failing identity, ROI, and allocation tests**

```python
def test_source_scope_and_common_harness(self):
    source, commit, input_path = self.make_source_repo()
    (source / "unrelated.log").write_text("dirty", encoding="utf-8")
    frozen = generator.freeze_source(
        source, commit, "bench/mcf", input_path, self.root / "frozen"
    )
    self.assertEqual(frozen["tracked_file_count"], 3)
    run = self.build_and_run_native(frozen, kind="authority")
    self.assertEqual(run["roi_begin"], "after_primal_start_artificial")
    self.assertEqual(run["roi_end"], "after_global_opt")
    self.assertGreater(run["peak_allocated_bytes"], 0)
```

Add cases rejecting a modified tracked MCF file, wrong commit, wrong input
hash, allocation multiplication overflow, malformed stable pointers, capacity
exhaustion, and a patch that does not apply cleanly.

- [ ] **Step 2: Run the module and verify the APIs are absent**

Run: `python3 -m unittest discover -s tests/pyunit/cross_system -p 'test_generate_mcfreg2_state.py' -v`

Expected: import or missing-function failure.

- [ ] **Step 3: Implement path-scoped freezing**

Use command-local safe-directory Git calls:

```python
def git_output(root, *arguments):
    command = (
        "git", "-c", f"safe.directory={root}",
        "-C", str(root), *arguments,
    )
    return subprocess.check_output(
        command, text=True, stderr=subprocess.STDOUT
    ).strip()
```

Require exact revision, empty `git status --porcelain -- bench/mcf`, and a
nonempty `git ls-files -z -- bench/mcf`. Hash relative path, NUL, byte length,
NUL, and bytes for each sorted tracked file. Copy only those files into a fresh
temporary root and recheck every hash. Bind resolved input path, size, and
SHA-256.

- [ ] **Step 4: Implement the common harness**

Expose this C11 API:

```c
int mcf_capture_configure(const char *input, const char *output_root,
                          int capture_enabled);
int mcf_capture_allocation(const char *kind, uint64_t elements,
                           uint64_t element_bytes, uint64_t current_bytes);
int mcf_capture_roi_begin(const network_t *net);
int mcf_capture_roi_end(const network_t *net);
int mcf_capture_finish(const char *mcf_output);
```

The common patch makes `main_wrapper.c` parse `--input` and `--output-root`,
passes the selected path into `mcf.c`, brackets only `global_opt()`, and wraps
node/dummy-arc/arc allocation and resize accounting. Canonical final-state
serialization excludes pointers and padding and uses stable IDs for every
relationship. Return nonzero on overflow, out-of-arena references, malformed
links, or short writes, and propagate failure to process exit.

The generator hashes the patch, runs `git apply --check`, applies only inside
the temporary copy, copies the runtime there, builds with explicit compiler
and `USE_MLIR=0`, and records commands and tool versions.

- [ ] **Step 5: Run tests and commit**

Run: `python3 -m unittest discover -s tests/pyunit/cross_system -p 'test_generate_mcfreg2_state.py' -v`

Expected: unrelated dirt is ignored, path-local drift fails, explicit input is
used, ROI markers are singular, and capacity/final-state evidence is valid.

```bash
git add scripts/generate_mcfreg2_state.py tests/pyunit/cross_system/test_generate_mcfreg2_state.py util/amu/matched_workloads/mcf_capture.h util/amu/matched_workloads/mcf_capture.c util/amu/matched_workloads/spec_mcf_common.patch
git commit -m "feat: capture native MCF ROI identity"
```

### Task 4: Capture exact pricing calls

**Files:**
- Create: `util/amu/matched_workloads/spec_mcf_capture.patch`
- Modify: `util/amu/matched_workloads/mcf_capture.h`
- Modify: `util/amu/matched_workloads/mcf_capture.c`
- Modify: `tests/pyunit/cross_system/test_generate_mcfreg2_state.py`

- [ ] **Step 1: Write a failing retained-basket pricing test**

```python
def test_pricing_capture_preserves_order_and_basket_state(self):
    journal = self.capture_fixture("pricing-retained")
    calls = journal.calls("pricing")
    self.assertEqual(calls[0].scanned_arc_ids, (0, 3, 1, 4, 2, 5))
    self.assertEqual(calls[1].basket_live_in, calls[0].basket_live_out[1:])
    expected = tuple(
        arc.cost - arc.tail_potential + arc.head_potential
        for arc in calls[0].arcs
    )
    self.assertEqual(calls[0].reduced_costs, expected)
    self.assertEqual(calls[0].post_digest, calls[1].pre_digest)
```

Also require group position, initialization state, candidate status, basket
slot, sorted order, selected stable arc ID, returned reduced cost, and priced
arc count.

- [ ] **Step 2: Run the focused test and verify call frames are missing**

Run: `PYTHONPATH=. python3 tests/pyunit/cross_system/test_generate_mcfreg2_state.py GenerateMCFREG2Test.test_pricing_capture_preserves_order_and_basket_state -v`

Expected: the capture journal has no pricing frames.

- [ ] **Step 3: Add observation-only pricing hooks**

```c
int mcf_capture_pricing_begin(long m, const arc_t *arcs,
                              const arc_t *stop_arcs,
                              long nr_group, long group_pos,
                              long initialize, long basket_size);
int mcf_capture_pricing_scan(const arc_t *arc, cost_t reduced_cost,
                             int candidate, long basket_slot);
int mcf_capture_pricing_end(const arc_t *selected, cost_t reduced_cost,
                            long arcs_priced, long nr_group,
                            long group_pos, long initialize,
                            long basket_size);
```

Patch `primal_bea_mpp()` and `remote()` around existing statements. Do not
replace the scan loop, native `cost - tail->potential + head->potential`,
basket mutation, sort, or return. Encode signed values as two's-complement
64-bit words and serialize complete basket live-in/live-out with stable IDs.

- [ ] **Step 4: Run pricing and authority-equivalence tests**

Run: `python3 -m unittest discover -s tests/pyunit/cross_system -p 'test_generate_mcfreg2_state.py' -v`

Expected: native order matches and authority/capture final state plus
`mcf.out` remain bit-exact.

- [ ] **Step 5: Commit**

```bash
git add util/amu/matched_workloads/spec_mcf_capture.patch util/amu/matched_workloads/mcf_capture.h util/amu/matched_workloads/mcf_capture.c tests/pyunit/cross_system/test_generate_mcfreg2_state.py
git commit -m "feat: capture native MCF pricing calls"
```

### Task 5: Capture price-out insert, replace, and resize

**Files:**
- Modify: `util/amu/matched_workloads/spec_mcf_capture.patch`
- Modify: `util/amu/matched_workloads/mcf_capture.h`
- Modify: `util/amu/matched_workloads/mcf_capture.c`
- Modify: `tests/pyunit/cross_system/test_generate_mcfreg2_state.py`

- [ ] **Step 1: Write failing tests for all native branches**

Create small C harness states for no-change, insertion, weakest replacement,
and `resize_prob()`. For resize, require the remap before a new-generation ID:

```python
def test_resize_remap_precedes_new_generation_references(self):
    call = self.capture_fixture("price-out-resize").calls("price_out")[0]
    remap = call.event_kinds.index("ARENA_REMAP")
    first_new = next(
        index for index, event in enumerate(call.events)
        if event.reference is not None and event.reference.generation == 1
    )
    self.assertLess(remap, first_new)
    self.assertEqual(call.new_arcs, 2)
    self.assertEqual(call.live_out["m"], call.live_in["m"] + 2)
```

- [ ] **Step 2: Run the four tests and verify typed events are absent**

Run: `python3 -m unittest discover -s tests/pyunit/cross_system -p 'test_generate_mcfreg2_state.py' -v`

Expected: failure on the first missing price-out event.

- [ ] **Step 3: Instrument native candidate and mutation boundaries**

```c
int mcf_capture_price_out_begin(const network_t *net);
int mcf_capture_price_out_candidate(const node_t *tail, const node_t *head,
                                    cost_t arc_cost, cost_t reduced_cost);
int mcf_capture_price_out_decision(int decision, const arc_t *slot,
                                   const node_t *tail, const node_t *head);
int mcf_capture_arena_remap(const arc_t *old_base, uint64_t old_capacity,
                            const arc_t *new_base, uint64_t new_capacity);
int mcf_capture_price_out_end(const network_t *net, long new_arcs);
```

Use `0=NO_CHANGE`, `1=INSERT`, and `2=REPLACE`. Observe candidate traversal,
all fields changed by insert/replace, neighbour-list refresh, and network
metadata updates. Increment arena generation after realloc and emit a complete
stable-ID remap before any new-generation reference. Native functions still
perform every decision and write.

- [ ] **Step 4: Run branch and authority-equivalence tests**

Run the generator test module. Expected: every branch passes and no native
output divergence occurs.

- [ ] **Step 5: Commit**

```bash
git add util/amu/matched_workloads/spec_mcf_capture.patch util/amu/matched_workloads/mcf_capture.h util/amu/matched_workloads/mcf_capture.c tests/pyunit/cross_system/test_generate_mcfreg2_state.py
git commit -m "feat: capture native MCF price-out calls"
```

### Task 6: Assemble deterministic packages and publish atomically

**Files:**
- Modify: `scripts/mcfreg2.py`
- Modify: `scripts/generate_mcfreg2_state.py`
- Modify: `tests/pyunit/cross_system/test_generate_mcfreg2_state.py`

- [ ] **Step 1: Write failing determinism and failure-record tests**

```python
def test_primary_and_replay_packages_are_identical(self):
    primary = self.generate_fixture("primary")
    replay = self.generate_fixture("replay")
    self.assertEqual(primary.package_sha256, replay.package_sha256)
    self.assertEqual(primary.package.read_bytes(), replay.package.read_bytes())

def test_failure_does_not_publish_an_accepted_root(self):
    result = self.generate_fixture("fault", fault="pricing-result", check=False)
    self.assertFalse(result.final_root.exists())
    failure = json.loads(result.failed_input.read_text(encoding="utf-8"))
    self.assertEqual(failure["status"], "failed_input")
    self.assertEqual(failure["first_failed_gate"], "capture_determinism")
    self.assertNotIn("accepted_package", failure)
```

Also test identity-bound resume, existing accepted-root preservation, short
writes, insufficient disk, and a changed patch/compiler/input hash.

- [ ] **Step 2: Run focused tests and verify deterministic packing is absent**

Run: `python3 -m unittest discover -s tests/pyunit/cross_system -p 'test_generate_mcfreg2_state.py' -v`

Expected: primary/replay package comparison cannot pass.

- [ ] **Step 3: Pack journals into all mandatory sections**

Normalize provenance JSON using sorted keys and compact separators. Exclude
temporary paths, process IDs, timestamps, wall time, capture run labels, and
raw addresses from package identity. Convert initial state, basket, call index, events, deltas,
boundaries, and final evidence into the ten sections. Recompute header counts
from decoded content before calling `write_package()`.

Use exact state names:

```python
STATUSES = (
    "planned", "authority_complete", "capture_primary_complete",
    "capture_replay_complete", "replay_verified", "accepted",
)
```

Each checkpoint binds source tree, input, common/capture patches, compiler,
commands, and all preceding artifact hashes. Resume rejects any mismatch.

- [ ] **Step 4: Implement atomic evidence publication**

Write `mcf.reg2`, `manifest.json`, and `validation.json` under a unique sibling
temporary directory. Reopen and hash all artifacts, fsync files and directory,
then rename to the package SHA-256. On failure preserve logs and atomically
write `failed-input.json`; never delete or overwrite prior evidence.

- [ ] **Step 5: Run tests and commit**

Run the two files:

```bash
python3 -m unittest discover -s tests/pyunit/cross_system -p 'test_mcfreg2.py' -v
python3 -m unittest discover -s tests/pyunit/cross_system -p 'test_generate_mcfreg2_state.py' -v
```

Expected: deterministic bytes, strict resume, and atomic failure pass.

```bash
git add scripts/mcfreg2.py scripts/generate_mcfreg2_state.py tests/pyunit/cross_system/test_mcfreg2.py tests/pyunit/cross_system/test_generate_mcfreg2_state.py
git commit -m "feat: generate deterministic MCFREG2 packages"
```

### Task 7: Independently replay semantics and emit canonical operations

**Files:**
- Modify: `util/amu/matched_workloads/mcfreg2.hh`
- Modify: `util/amu/matched_workloads/mcfreg2.cc`
- Modify: `util/amu/matched_workloads/mcf_regions.cc`
- Modify: `tests/pyunit/cross_system/test_mcfreg2.py`
- Modify: `tests/pyunit/cross_system/test_matched_region_build.py`

- [ ] **Step 1: Write failing semantic and fault-injection tests**

```python
def test_cpp_replayer_recomputes_all_calls(self):
    result = self.run_cpp_replayer(self.write_semantic_fixture())
    self.assertEqual(result["status"], "verified")
    self.assertEqual(result["pricing_calls"], 2)
    self.assertEqual(result["price_out_calls"], 4)
    self.assertEqual(result["boundary_mismatches"], 0)

def test_cpp_replayer_rejects_changed_selected_arc(self):
    completed = self.run_cpp_replayer(
        self.write_semantic_fixture(fault="selected-arc"), check=False
    )
    self.assertNotEqual(completed.returncode, 0)
    self.assertIn("selected arc differs", completed.stderr)
```

- [ ] **Step 2: Run tests and verify the semantic API is absent**

Run: `python3 -m unittest discover -s tests/pyunit/cross_system -p 'test_mcfreg2.py' -v`

Expected: structural parsing succeeds but semantic replay is unavailable.

- [ ] **Step 3: Implement independent replay**

Expose:

```cpp
struct ReplaySummary {
    uint64_t pricingCalls;
    uint64_t priceOutCalls;
    uint64_t operations;
    uint64_t boundaryMismatches;
};
ReplaySummary replay(const Package &package, std::FILE *canonicalTrace,
                     const std::string &outputRoot);
```

Reconstruct links only from stable IDs. Recompute native reduced cost,
dual-infeasibility, persistent basket retention/sort/selection, candidate
decisions, insert/replace, arena remap, adjacency updates, and network counts.
Compare every precondition, delta, and boundary digest. An event containing an
expected result without enough inputs to recompute it is invalid.

Emit canonical loads, integer operations, stores, barriers, and commits in
native order. Extend existing logical address ranges without aliasing them.

- [ ] **Step 4: Dispatch by magic in `mcf_regions.cc`**

Read the first eight bytes. Permit `MCFREG1\0` only with
`MATCHED_FIXTURE=1`; otherwise exit with `formal MCFREG1 is forbidden`.
Route `MCFREG2\0` through `readPackage()` and `replay()`.

- [ ] **Step 5: Run tests and commit**

Run the two files:

```bash
python3 -m unittest discover -s tests/pyunit/cross_system -p 'test_mcfreg2.py' -v
python3 -m unittest discover -s tests/pyunit/cross_system -p 'test_matched_region_build.py' -v
```

Expected: semantic fixtures pass, faults fail closed, and legacy fixture
behavior remains valid.

```bash
git add util/amu/matched_workloads/mcfreg2.hh util/amu/matched_workloads/mcfreg2.cc util/amu/matched_workloads/mcf_regions.cc tests/pyunit/cross_system/test_mcfreg2.py tests/pyunit/cross_system/test_matched_region_build.py
git commit -m "feat: replay MCFREG2 semantics independently"
```

### Task 8: Enforce MCFREG2 in the formal freezer and builder

**Files:**
- Modify: `scripts/freeze_cross_system_inputs.py`
- Modify: `tests/pyunit/cross_system/test_freeze_cross_system_inputs.py`
- Modify: `scripts/build_matched_breadth_workloads.py`
- Modify: `tests/pyunit/cross_system/test_matched_region_build.py`

- [ ] **Step 1: Write failing formal-record tests**

Extend formal MCF rows with:

```python
mcf_row.update({
    "format": "MCFREG2",
    "source_commit": "2b30de22399402d8c44bd74b8ebf743b6a6a55e9",
    "source_tree_sha256": "1" * 64,
    "validation": str(validation_path),
    "validation_sha256": sha256(validation_path),
})
```

Test rejection of `MCFREG1`, missing validation, package/validation hash drift,
status other than `accepted`, nonzero boundary mismatches, primary/replay
inequality, and observed allocation below 345,000,000 bytes.

Add a formal builder test:

```python
def test_formal_mcfreg2_builds_verified_reference_bundle(self):
    record = self.make_verified_formal_record()
    inputs, digest = builder.load_formal_inputs(record)
    manifest = builder.build_suite(
        self.root / "formal-build", inputs=inputs,
        input_manifest_sha256=digest,
    )
    outputs = builder.run_formal_references(
        manifest, self.root / "formal-runs"
    )
    bundle = trace.read_bundle(outputs["mcf"])
    self.assertEqual(bundle.meta["input_format"], "MCFREG2")
    self.assertEqual(bundle.meta["boundary_mismatches"], 0)
```

- [ ] **Step 2: Run freezer/builder tests and observe current failures**

Run the two files:

```bash
python3 -m unittest discover -s tests/pyunit/cross_system -p 'test_freeze_cross_system_inputs.py' -v
python3 -m unittest discover -s tests/pyunit/cross_system -p 'test_matched_region_build.py' -v
```

Expected: the old schema accepts too little and the deliberate formal-MCF stop
blocks the verified package.

- [ ] **Step 3: Validate the formal record and binary together**

Add `format`, `source_commit`, `source_tree_sha256`, `validation`, and
`validation_sha256` to required MCF fields. Require validation schema 2,
accepted status, exact package/input/source/patch binding, zero mismatches,
identical primary/replay package hash, identical authority/capture final-state
and `mcf.out` hashes, and exact peak allocation equality with the row.

Call `mcfreg2.read_package()` from the freezer. Preserve `synthetic is False`.
Expose `validate_mcf_record(row)` for validating a generated candidate before
the full six-workload record exists.

- [ ] **Step 4: Compile and build the formal reference**

For MCF, compile `mcf_regions.cc` with `mcfreg2.cc` and record both sources plus
`mcfreg2_format.h` in binary identity. Replace `_mcf_initial_memory()` with
magic dispatch: preserve its MCFREG1 fixture path; decode MCFREG2 normalized
sections into canonical memory maps for formal mode.

Remove the unconditional formal stop only after validation fields pass.
Include package, validation, source-tree, common-patch, and capture-patch
hashes in the prepared manifest.

- [ ] **Step 5: Run tests and commit**

Run the three files:

```bash
python3 -m unittest discover -s tests/pyunit/cross_system -p 'test_mcfreg2.py' -v
python3 -m unittest discover -s tests/pyunit/cross_system -p 'test_freeze_cross_system_inputs.py' -v
python3 -m unittest discover -s tests/pyunit/cross_system -p 'test_matched_region_build.py' -v
```

Expected: MCFREG1 fixtures still pass; only qualified MCFREG2 enters formal
mode; the formal bundle reports zero boundary mismatches.

```bash
git add scripts/freeze_cross_system_inputs.py tests/pyunit/cross_system/test_freeze_cross_system_inputs.py scripts/build_matched_breadth_workloads.py tests/pyunit/cross_system/test_matched_region_build.py
git commit -m "feat: bind formal MCF to qualified MCFREG2"
```

### Task 9: Generate and verify the real reference package

**Files:**
- Output: `/mnt/disk0/gem5-CXL-eval/cira-amu-m2ndp-spectrum/formal-inputs/mcf/`

- [ ] **Step 1: Run focused tests and static gates**

Run:

```bash
python3 -m unittest discover -s tests/pyunit/cross_system -p 'test_mcfreg2.py' -v
python3 -m unittest discover -s tests/pyunit/cross_system -p 'test_generate_mcfreg2_state.py' -v
python3 -m unittest discover -s tests/pyunit/cross_system -p 'test_freeze_cross_system_inputs.py' -v
python3 -m unittest discover -s tests/pyunit/cross_system -p 'test_matched_region_build.py' -v
python3 -m compileall -q scripts/mcfreg2.py scripts/generate_mcfreg2_state.py scripts/freeze_cross_system_inputs.py scripts/build_matched_breadth_workloads.py
git diff --check
```

Expected: zero failures, no Python compile errors, and no whitespace errors.

- [ ] **Step 2: Run formal preflight**

```bash
python3 scripts/generate_mcfreg2_state.py preflight --source-root /home/victoryang00/CXLMemUring --source-commit 2b30de22399402d8c44bd74b8ebf743b6a6a55e9 --source-subdir bench/mcf --input /home/victoryang00/CXLMemUring/bench/mcf/data/ref/input/inp.in --input-sha256 aceb933893790cd957ec9d03d34660ba756a70d87b65caa9809e3a48443ba849 --output-root /mnt/disk0/gem5-CXL-eval/cira-amu-m2ndp-spectrum/formal-inputs/mcf
```

Expected: `status=ready`, commit matched, path-scoped MCF clean, exactly 36
tracked files, input identity matched, LP64 ABI, compiler available, and
sufficient memory/disk. Any failure stops before build.

- [ ] **Step 3: Generate without a time limit**

```bash
python3 scripts/generate_mcfreg2_state.py generate --source-root /home/victoryang00/CXLMemUring --source-commit 2b30de22399402d8c44bd74b8ebf743b6a6a55e9 --source-subdir bench/mcf --input /home/victoryang00/CXLMemUring/bench/mcf/data/ref/input/inp.in --input-sha256 aceb933893790cd957ec9d03d34660ba756a70d87b65caa9809e3a48443ba849 --output-root /mnt/disk0/gem5-CXL-eval/cira-amu-m2ndp-spectrum/formal-inputs/mcf
```

Expected terminal status: `accepted`, with authority/capture output equality,
primary/replay package equality, zero C++ replay mismatches, and peak allocated
bytes at least 345,000,000. If execution exceeds the interactive session,
launch this exact command detached with persistent logs and monitor the
identity-bound state file; do not impose a timeout.

- [ ] **Step 4: Verify accepted evidence independently**

```bash
python3 scripts/generate_mcfreg2_state.py verify --output-root /mnt/disk0/gem5-CXL-eval/cira-amu-m2ndp-spectrum/formal-inputs/mcf --accepted
```

Expected: the command resolves the single accepted content-addressed root,
recomputes every artifact hash, parses the package in Python and C++, reruns
semantic replay, and confirms the directory name equals the package SHA-256.

Record the printed exact package/manifest/validation paths and hashes,
package size, peak allocation, pricing/price-out/event counts, all three final
state and `mcf.out` hashes, and replay mismatch count.

- [ ] **Step 5: Validate the candidate record without publishing the full paper record**

```bash
python3 scripts/generate_mcfreg2_state.py validate-candidate --candidate /mnt/disk0/gem5-CXL-eval/cira-amu-m2ndp-spectrum/formal-inputs/mcf/candidate-record.json
```

Expected: `MCF_CANDIDATE=validated`. Do not create or edit the six-workload
`paper-input-record.json` in this task.

### Task 10: Run final verification and push

**Files:**
- Verify only; preserve `src/mem/cache/base.cc`

- [ ] **Step 1: Use the verification-before-completion skill**

Before claiming success, read and follow
`/root/.codex/skills/verification-before-completion/SKILL.md`. Re-run evidence
commands rather than relying on earlier output.

- [ ] **Step 2: Run the complete regression gate**

```bash
python3 -m unittest discover -s tests/pyunit/cross_system -p 'test_*.py' -v
python3 -m unittest discover -s tests/pyunit/m2ndp -p 'test_*.py' -v
python3 -m compileall -q scripts
git diff --check
```

Expected: zero failures, no syntax errors, and no whitespace errors. Only
environment-dependent skips already asserted by tests are acceptable.

- [ ] **Step 3: Verify Git scope**

```bash
git status --short
git diff -- src/mem/cache/base.cc
git log --oneline origin/m2ndp-g20-pr-spmv..HEAD
```

Expected: the user file remains modified and unstaged; all planned MCFREG2
changes are committed; no `/mnt/disk0` artifact is tracked.

- [ ] **Step 4: Push the implementation branch**

```bash
git push origin m2ndp-g20-pr-spmv
```

Expected: the remote branch advances to the verified implementation commit.
Report the pushed commit, package/validation paths and hashes, peak allocation,
call/event counts, bit-exact evidence, test totals, and documented skips.
