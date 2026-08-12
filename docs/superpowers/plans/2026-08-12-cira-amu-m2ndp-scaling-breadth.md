# CIRA, AMU, and M2NDP Scaling and Workload-Breadth Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build and publish a bit-exact, four-thread, all-CXL, 1 us comparison of CIRA, AMU, and M2NDP with full PR-SpMV scaling through g20 and calibrated trace-driven coverage of six workload regions.

**Architecture:** A hash-bound evidence layer freezes real inputs and a backend-neutral canonical trace before timing. Existing matched PageRank, coherent CIRA, queue-credit AMU, and strict M2NDP paths are extended behind focused adapters; full simulation produces the scaling panel, while deterministic paired windows produce the breadth panel. A fail-closed publisher is the only path from raw evidence to CSV, LaTeX, PDF/SVG, or paper prose.

**Tech Stack:** Python 3 standard library, C++17/OpenMP, gem5 X86 Timing CPUs and CXL/ASMC/CIRA models, M2NDP FuncSim/NDPSim, `unittest`, SCons, SHA-256 JSON/CSV provenance, Matplotlib Agg, paired block bootstrap, LaTeX/latexmk.

---

## Scope and file map

This remains one plan because input identity, operation ordering, paired
windows, backend timing, and publication share one evidence schema. Tasks are
ordered so each commit is independently testable. Never promote current
g4/g12/g14/g20 results, including the old implausible g20 M2NDP speedup. Do
not edit the Overleaf repository before canonical evidence passes. Do not add
periodic live checkpointing.

- Create `scripts/cross_system_contract.py` for immutable identities, states,
  checkpoint validation, and terminal rules.
- Create `scripts/freeze_cross_system_inputs.py` to bind four graphs and six
  real paper inputs without substitution.
- Create `scripts/canonical_work_trace.py` and
  `util/amu/matched_workloads/canonical_trace.hh` for a shared trace/result ABI.
- Create `scripts/stratified_timing.py` for deterministic windows,
  reconstruction, paired bootstrap, and the 5% gate.
- Create `scripts/run_cira_amu_m2ndp_scaling.py` for the 16 full-E2E points.
- Create `scripts/build_matched_breadth_workloads.py` plus MCF, Spatter, CG,
  and MG adapters under `util/amu/matched_workloads/`.
- Create `scripts/run_matched_breadth_gem5.py` and
  `scripts/m2ndp_workload_trace.py` for matched backend execution.
- Create `scripts/run_cira_amu_m2ndp_breadth.py` for functional gates, paired
  timing, reconstruction, and identity-safe resume.
- Create `scripts/generate_cira_amu_m2ndp_comparison.py` for atomic data,
  table, and vector-figure publication.
- Add focused tests under `tests/pyunit/cross_system/`; extend existing AMU and
  M2NDP regression tests where current runners change.
- Modify `docs/amu-gapbs-benchmark.md`; modify the independent paper repo only
  after formal publication passes.

### Task 1: Immutable experiment and terminal-state contract

**Files:**
- Create: `scripts/cross_system_contract.py`
- Create: `tests/pyunit/cross_system/__init__.py`
- Create: `tests/pyunit/cross_system/test_cross_system_contract.py`

- [ ] **Step 1: Write failing identity, transition, and root-reuse tests**

```python
class ContractTest(unittest.TestCase):
    def test_identity_binds_every_semantic_input(self):
        value = contract.ExperimentIdentity(
            code_sha256="a" * 64, input_manifest_sha256="b" * 64,
            calibration_manifest_sha256="c" * 64,
            trace_sha256="d" * 64, config_sha256="e" * 64)
        self.assertEqual(len(value.digest()), 64)

    def test_timing_before_functional_pass_is_illegal(self):
        with self.assertRaisesRegex(contract.ContractError, "transition"):
            contract.transition({"status": "planned"}, "timing_in_progress")

    def test_changed_identity_requires_a_fresh_root(self):
        contract.atomic_write_json(ROOT / "identity.json", {"digest": "0" * 64})
        with self.assertRaisesRegex(contract.ContractError, "fresh evidence root"):
            contract.bind_root(ROOT, identity("1"))
```

- [ ] **Step 2: Run tests and verify RED**

Run: `PYTHONPATH=. python3 -m unittest tests.pyunit.cross_system.test_cross_system_contract -v`

Expected: import failure for `scripts.cross_system_contract`.

- [ ] **Step 3: Implement canonical hashing and legal transitions**

```python
TERMINAL = frozenset({"complete", "failed", "inconclusive", "failed_input"})
TRANSITIONS = {
    "planned": frozenset({"functional_pass", "failed", "failed_input"}),
    "functional_pass": frozenset({"timing_in_progress", "failed"}),
    "timing_in_progress": frozenset({"complete", "failed", "inconclusive"}),
}

@dataclasses.dataclass(frozen=True)
class ExperimentIdentity:
    code_sha256: str
    input_manifest_sha256: str
    calibration_manifest_sha256: str
    trace_sha256: str
    config_sha256: str

    def digest(self):
        payload = json.dumps(dataclasses.asdict(self), sort_keys=True,
                             separators=(",", ":")).encode()
        return hashlib.sha256(payload).hexdigest()

def transition(state, target, *, reason=""):
    source = state["status"]
    if target not in TRANSITIONS.get(source, frozenset()):
        raise ContractError(f"illegal transition {source} -> {target}")
    return {**state, "status": target, "reason": reason}
```

Validate every digest with `[0-9a-f]{64}`. Use sibling temporaries, `fsync`,
and `os.replace`. `bind_root()` compares the entire canonical identity before
allowing an existing root.

- [ ] **Step 4: Add newest-valid boundary recovery**

```python
def select_resume_checkpoint(records, identity_digest):
    valid, rejected = [], []
    for record in records:
        ok = (record["identity_sha256"] == identity_digest and
              record["boundary"] in {"phase", "window"} and
              verify_named_hashes(record["outputs"]))
        (valid if ok else rejected).append(record)
    selected = max(valid, key=lambda row: int(row["sequence"])) if valid else None
    return selected, tuple(rejected)
```

Test that a newer corrupt checkpoint is reported but does not hide the newest
valid one, and that code/input/config/trace/calibration drift selects nothing.

- [ ] **Step 5: Run tests and commit**

```bash
PYTHONPATH=. python3 -m unittest tests.pyunit.cross_system.test_cross_system_contract -v
git add scripts/cross_system_contract.py tests/pyunit/cross_system
git commit -m "feat: add immutable cross-system evidence contract"
```

### Task 2: Freeze real paper inputs and formal graph profiles

**Files:**
- Create: `scripts/freeze_cross_system_inputs.py`
- Create: `tests/pyunit/cross_system/test_freeze_cross_system_inputs.py`
- Modify: `scripts/gapbs_pr_experiment_profiles.py`
- Modify: `tests/pyunit/m2ndp/test_gapbs_pr_experiment_profiles.py`

- [ ] **Step 1: Write failing source-authority tests**

```python
def test_missing_paper_record_fails_instead_of_inferring_size(self):
    with self.assertRaisesRegex(freeze.InputError, "paper input record"):
        freeze.freeze_inputs(options(paper_input_record=Path("missing.json")))

def test_npb_requires_parameter_hash_and_allocated_bytes(self):
    value = valid_record()
    value["npb_cg"].pop("parameter_sha256")
    with self.assertRaisesRegex(freeze.InputError, "npb_cg.parameter_sha256"):
        freeze.validate_paper_record(value)

def test_synthetic_mcf_hardware_microbenchmark_is_rejected(self):
    value = valid_record()
    value["mcf"]["synthetic"] = True
    with self.assertRaisesRegex(freeze.InputError, "synthetic"):
        freeze.validate_paper_record(value)
```

- [ ] **Step 2: Run and verify RED**

Run: `PYTHONPATH=. python3 -m unittest tests.pyunit.cross_system.test_freeze_cross_system_inputs -v`

Expected: missing `freeze_cross_system_inputs`.

- [ ] **Step 3: Implement a strict six-workload input record**

```python
WORKLOADS = ("pr_spmv", "mcf", "amg_gather", "lulesh_scatter", "npb_cg", "npb_mg")
REQUIRED = {
    "pr_spmv": {"input", "input_sha256", "allocated_bytes", "scale"},
    "mcf": {"input", "input_sha256", "allocated_bytes", "source", "source_sha256", "synthetic"},
    "amg_gather": {"input", "input_sha256", "allocated_bytes", "index_sha256"},
    "lulesh_scatter": {"input", "input_sha256", "allocated_bytes", "index_sha256"},
    "npb_cg": {"source_root", "source_commit", "parameter_file", "parameter_sha256", "allocated_bytes", "class"},
    "npb_mg": {"source_root", "source_commit", "parameter_file", "parameter_sha256", "allocated_bytes", "class"},
}

def validate_paper_record(value):
    if set(value) != set(WORKLOADS):
        raise InputError("paper input record workload set differs")
    for name, keys in REQUIRED.items():
        missing = keys - set(value[name])
        if missing:
            raise InputError(f"{name}.{sorted(missing)[0]} is required")
        if value[name].get("synthetic") is True:
            raise InputError(f"{name} synthetic input is not paper evidence")
    return value
```

Require absolute existing paths and live hashes. For NPB, require exact source
commit, parameter-file hash, and adapter-measured allocated bytes; never derive
12.8 GB from a class label or fall back to A/B/C.

- [ ] **Step 4: Add a four-scale, four-thread, 1 us profile**

```python
SCALING_SCALES = (4, 12, 14, 20)
SCALING_GRAPH_HASHES = {
    4: G4_SHA256,
    20: m2ndp_artifacts.EXPECTED_G20_SHA256,
}

def load_scaling_graphs(paths):
    rows = tuple(load_any_frozen_graph(path) for path in paths)
    if tuple(row.scale for row in rows) != SCALING_SCALES:
        raise ProfileError("scaling graph manifests must be g4,g12,g14,g20")
    return rows
```

Keep existing profiles unchanged. Reject reordered manifests, `num_nodes !=
1 << scale`, regenerated post-selection graphs, and g4/g20 hash changes.

- [ ] **Step 5: Implement atomic accepted/failed-input output**

The CLI requires `--paper-input-record`, four ordered `--graph-manifest`
arguments, and `--output`. It writes either complete `inputs.json` or terminal
`failed-input.json`; it never writes a partially accepted manifest.

- [ ] **Step 6: Run tests and commit**

```bash
PYTHONPATH=. python3 -m unittest \
  tests.pyunit.cross_system.test_freeze_cross_system_inputs \
  tests.pyunit.m2ndp.test_gapbs_pr_experiment_profiles -v
git add scripts/freeze_cross_system_inputs.py scripts/gapbs_pr_experiment_profiles.py \
  tests/pyunit/cross_system/test_freeze_cross_system_inputs.py \
  tests/pyunit/m2ndp/test_gapbs_pr_experiment_profiles.py
git commit -m "feat: freeze paper-scale comparison inputs"
```

### Task 3: Canonical trace and full raw-bit result ABI

**Files:**
- Create: `scripts/canonical_work_trace.py`
- Create: `util/amu/matched_workloads/canonical_trace.hh`
- Create: `tests/pyunit/cross_system/test_canonical_work_trace.py`

- [ ] **Step 1: Write failing ABI, order, and mismatch tests**

```python
def test_round_trip_preserves_raw_operands_and_sequence(self):
    op = trace.Operation(0, trace.Opcode.F32_ADD, 7, 0, 0x1000,
                         0x3f800000, 0x40000000, 0x40400000)
    trace.write_bundle(ROOT, meta(), [op], {"rank.iter0": [0x40400000]})
    self.assertEqual(trace.read_bundle(ROOT).operations, (op,))

def test_translation_may_not_reorder_commits(self):
    with self.assertRaisesRegex(trace.TraceError, "sequence"):
        trace.validate_translation(reference_ops(), tuple(reversed(reference_ops())))
```

Also cover truncated records, unknown opcodes, address overflow, duplicate
scatter reorder, reduction-tree drift, and a one-bit output mismatch.

- [ ] **Step 2: Run and verify RED**

Run: `PYTHONPATH=. python3 -m unittest tests.pyunit.cross_system.test_canonical_work_trace -v`

Expected: missing module.

- [ ] **Step 3: Implement the Python ABI and raw comparator**

```python
TRACE_STRUCT = struct.Struct("<H H I Q Q Q Q Q Q")

class Opcode(enum.IntEnum):
    LOAD_U32 = 1; LOAD_U64 = 2; LOAD_F32 = 3; LOAD_F64 = 4
    STORE_U32 = 5; STORE_U64 = 6; STORE_F32 = 7; STORE_F64 = 8
    F32_ADD = 9; F32_MUL = 10; F32_DIV = 11; F64_ADD = 12
    I64_ADD = 13; I64_MIN = 14; BARRIER = 15; COMMIT = 16

@dataclasses.dataclass(frozen=True)
class Operation:
    phase: int; opcode: Opcode; work_item: int; sequence: int
    address: int; operand0: int; operand1: int; result: int

def compare_words(expected, actual, label):
    if len(expected) != len(actual):
        raise TraceError(f"{label} length differs")
    for index, (want, got) in enumerate(zip(expected, actual)):
        if want != got:
            raise TraceError(f"{label}[{index}] expected 0x{want:x} actual 0x{got:x}")
```

`trace.meta.json` binds input/source/binary/config hashes, phases, work counts,
output boundaries, and trace hash. Results are raw little-endian words with
per-boundary element counts and hashes.

- [ ] **Step 4: Implement the matching packed C++ record**

```cpp
#pragma pack(push, 1)
struct TraceRecord {
  uint16_t phase, opcode;
  uint32_t reserved;
  uint64_t work_item, sequence, address, operand0, operand1, result;
};
#pragma pack(pop)
static_assert(sizeof(TraceRecord) == 56, "trace ABI drift");
```

Use `memcpy` raw-bit helpers and checked `fwrite`. Compile a C++ fixture, read
it in Python, and require byte-for-byte equality.

- [ ] **Step 5: Run tests and commit**

```bash
PYTHONPATH=. python3 -m unittest tests.pyunit.cross_system.test_canonical_work_trace -v
git add scripts/canonical_work_trace.py util/amu/matched_workloads/canonical_trace.hh \
  tests/pyunit/cross_system/test_canonical_work_trace.py
git commit -m "feat: define canonical matched-work trace ABI"
```

### Task 4: Deterministic paired sampling and bootstrap

**Files:**
- Create: `scripts/stratified_timing.py`
- Create: `tests/pyunit/cross_system/test_stratified_timing.py`

- [ ] **Step 1: Write failing coordinate, reconstruction, and CI tests**

```python
def test_windows_are_nested_and_have_equal_warmup(self):
    plan = timing.make_plan("a" * 64, "pricing", 20_000_000)
    self.assertEqual(plan.length, 65536)
    self.assertTrue(set(plan.coordinates(8)) < set(plan.coordinates(16)))
    self.assertTrue(all(w.measure_start - w.warmup_start == plan.length
                        for w in plan.coordinates(64)))

def test_short_phase_uses_full_timing(self):
    self.assertTrue(timing.make_plan("b" * 64, "short", 100_000).full_phase)

def test_exact_pairs_pass_five_percent_gate(self):
    result = timing.bootstrap_speedup([10] * 8, [5] * 8, seed=7)
    self.assertEqual(result.speedup, Decimal("2"))
    self.assertTrue(result.publishable)
```

- [ ] **Step 2: Run and verify RED**

Run: `PYTHONPATH=. python3 -m unittest tests.pyunit.cross_system.test_stratified_timing -v`

Expected: missing module.

- [ ] **Step 3: Implement frozen nested coordinates**

```python
LEVELS = (8, 16, 32, 64)

def make_plan(trace_sha256, phase, count):
    length = min(65536, count // 128)
    if length < 1024:
        return SamplingPlan(phase, count, count, True, ())
    seed = int(hashlib.sha256(f"{trace_sha256}:{phase}".encode()).hexdigest()[:16], 16)
    rng = random.Random(seed)
    windows = tuple(place_nonoverlapping_pair(i, count, length, rng)
                    for i in range(64))
    return SamplingPlan(phase, count, length, False,
                        bit_reversal_order(windows))
```

Serialize the seed, all coordinates, work-item identities, trace hash, and
phase count before timing. Reject overlap and clipped warmup.

- [ ] **Step 4: Implement reconstruction and 10,000 paired resamples**

```python
def reconstruct(fixed_seconds, phases):
    return Decimal(fixed_seconds) + sum(
        Decimal(p.full_work_items) *
        (sum(map(Decimal, p.seconds_per_item)) / len(p.seconds_per_item))
        for p in phases)

def publication_gate(estimate, low, high):
    return (high - low) / (Decimal(2) * estimate) <= Decimal("0.05")
```

Use a frozen `random.Random(seed)` for 10,000 paired block-bootstrap resamples,
percentile 95% CI, and `Decimal` outputs. Test deterministic bytes and terminal
`inconclusive` after 64 windows miss the gate.

- [ ] **Step 5: Run tests and commit**

```bash
PYTHONPATH=. python3 -m unittest tests.pyunit.cross_system.test_stratified_timing -v
git add scripts/stratified_timing.py tests/pyunit/cross_system/test_stratified_timing.py
git commit -m "feat: add deterministic paired timing estimator"
```

### Task 5: Full g4/g12/g14/g20 PR-SpMV scaling orchestrator

**Files:**
- Create: `scripts/run_cira_amu_m2ndp_scaling.py`
- Create: `tests/pyunit/cross_system/test_run_cira_amu_m2ndp_scaling.py`
- Modify: `scripts/run_gapbs_matched_pr_spmv_variants.py`
- Modify: `scripts/run_m2ndp_g20_pr_spmv.py`
- Modify: `tests/pyunit/m2ndp/test_run_gapbs_matched_pr_spmv_variants.py`
- Modify: `tests/pyunit/m2ndp/test_run_m2ndp_g20_pr_spmv.py`

- [ ] **Step 1: Write failing 16-point and no-sampling tests**

```python
def test_matrix_is_four_scales_by_four_systems_at_1us():
    matrix = scaling.build_matrix()
    self.assertEqual(len(matrix), 16)
    self.assertEqual({row.scale for row in matrix}, {4, 12, 14, 20})
    self.assertEqual({row.system for row in matrix},
                     {"vanilla", "amu", "cira", "m2ndp"})
    self.assertTrue(all(row.latency == "1us" and row.full_e2e for row in matrix))

def test_formal_commands_have_no_sampling_or_smoke_flags():
    command = scaling.command_for(entry(scale=20, system="amu"), options())
    self.assertNotIn("--smoke-test", command)
    self.assertNotIn("--window", " ".join(command))
```

Also reject delay other than 1,000,000 ticks, core/thread count other than 4,
missing full trial-0 CXL warmup, post-warmup cross-system checkpoints, non-20
iterations, and one-bit rank mismatches.

- [ ] **Step 2: Run and verify RED**

Run: `PYTHONPATH=. python3 -m unittest tests.pyunit.cross_system.test_run_cira_amu_m2ndp_scaling -v`

Expected: missing scaling module.

- [ ] **Step 3: Generalize the matched runners without weakening old profiles**

```python
def validate_scaling_profile(profile):
    actual = (profile.cores, profile.threads, profile.latencies,
              profile.trials, profile.measured_trial,
              profile.page_rank_iterations)
    expected = (4, 4, ("1us",), 2, 1, 20)
    if actual != expected:
        raise VariantRunError(f"formal scaling profile differs: {actual}")
```

Accept frozen scales 4/12/14/20 in the new profile while retaining all legacy
profile behavior. Add `warmup_execution=full_cxl_trial0` and an explicit
measured interval to summaries. Reject checkpoints later than trial-0 entry.

- [ ] **Step 4: Implement the full state machine and gates**

```python
SYSTEMS = ("vanilla", "amu", "cira", "m2ndp")
SCALES = (4, 12, 14, 20)

def build_matrix():
    return tuple(MatrixEntry(scale, system, "1us", True)
                 for scale in SCALES for system in SYSTEMS)

def validate_point(entry, outputs, identity):
    require_hashes(outputs, identity)
    require_config(outputs["config"], delay=1_000_000, cores=4,
                   threads=4, all_memory_cxl=True)
    require_full_rank_bits(outputs["reference"], outputs["result"],
                           1 << entry.scale)
    require_mechanism_counters(entry.system, outputs)
```

Run Vanilla, AMU, CIRA, M2NDP for each scale. Write `complete.json` only after
16/16 pass; preserve a terminal failure otherwise.

- [ ] **Step 5: Run focused and legacy tests**

```bash
PYTHONPATH=. python3 -m unittest \
  tests.pyunit.cross_system.test_run_cira_amu_m2ndp_scaling \
  tests.pyunit.m2ndp.test_run_gapbs_matched_pr_spmv_variants \
  tests.pyunit.m2ndp.test_run_m2ndp_g20_pr_spmv -v
```

Expected: all tests pass and legacy commands remain identical.

- [ ] **Step 6: Commit**

```bash
git add scripts/run_cira_amu_m2ndp_scaling.py \
  scripts/run_gapbs_matched_pr_spmv_variants.py scripts/run_m2ndp_g20_pr_spmv.py \
  tests/pyunit/cross_system/test_run_cira_amu_m2ndp_scaling.py \
  tests/pyunit/m2ndp/test_run_gapbs_matched_pr_spmv_variants.py \
  tests/pyunit/m2ndp/test_run_m2ndp_g20_pr_spmv.py
git commit -m "feat: orchestrate full four-scale PageRank comparison"
```

### Task 6: Canonical MCF and Spatter region adapters

**Files:**
- Create: `util/amu/matched_workloads/mcf_regions.cc`
- Create: `util/amu/matched_workloads/spatter_regions.cc`
- Create: `scripts/build_matched_breadth_workloads.py`
- Create: `tests/pyunit/cross_system/test_matched_region_build.py`

- [ ] **Step 1: Write failing fixture-build and order tests**

Compile miniature frozen MCF/AMG/LULESH fixtures, run `--mode reference`, and
assert phase names, work counts, output images, and trace hashes. Reverse two
duplicate scatter stores and require raw comparison failure.

```python
self.assertEqual(mcf.meta["phases"], ["pricing_kernel", "price_out_impl"])
self.assertEqual(scatter.meta["duplicate_policy"], "canonical_program_order")
self.assertEqual(gather.outputs["destination"][17], expected_bits[17])
```

- [ ] **Step 2: Run and verify RED**

Run: `PYTHONPATH=. python3 -m unittest tests.pyunit.cross_system.test_matched_region_build -v`

Expected: missing sources/build module.

- [ ] **Step 3: Implement ordered MCF regions**

```cpp
for (uint64_t invocation = 0; invocation < pricing_calls; ++invocation) {
  Candidate best = initial_candidate();
  for (uint64_t i = row_begin(invocation); i != row_end(invocation); ++i) {
    const Arc& arc = arcs[index[i]];
    const int64_t reduced = arc.cost + potential[arc.tail] - potential[arc.head];
    emit_pricing_read(invocation, i, arc, reduced);
    if (reduced < best.reduced) best = {i, reduced};
  }
  commit_pricing_candidate(invocation, best);
}
for (uint64_t invocation = 0; invocation < price_out_calls; ++invocation)
  price_out_impl_ordered(invocation, arcs, flow, cost, potential, tree, trace);
```

Load only the frozen 345 MB paper input. Dump objective, flow, cost, potential,
predecessor, depth, orientation, and tree arrays after every required boundary.
Record separate complete invocation/work counts for both hotspots.

- [ ] **Step 4: Implement real AMG gather and LULESH scatter**

```cpp
for (uint64_t i = 0; i < count; ++i) {
  destination[i] = values[index[i]];
  emit_gather(i, index[i], raw_bits(destination[i]));
}
for (uint64_t i = 0; i < count; ++i) {
  destination[index[i]] = values[i];
  emit_ordered_scatter(i, index[i], raw_bits(values[i]));
}
```

Do not sort, deduplicate, vector-reduce, or generate indices. Require hashes
from `inputs.json`, the recorded 1 GB allocated working set, and full output.

- [ ] **Step 5: Implement strict build manifests**

Build reference/Vanilla/AMU/CIRA with `-O3 -fopenmp -ffp-contract=off
-fno-fast-math`, exactly four threads, and no `-march=native`. Record compiler,
flags, source, binary, input-manifest, and trace-ABI hashes. Formal builds reject
fixture and synthetic flags.

- [ ] **Step 6: Run tests and commit**

```bash
PYTHONPATH=. python3 -m unittest tests.pyunit.cross_system.test_matched_region_build -v
git add scripts/build_matched_breadth_workloads.py util/amu/matched_workloads \
  tests/pyunit/cross_system/test_matched_region_build.py
git commit -m "feat: add exact MCF and Spatter region adapters"
```

### Task 7: Exact NPB CG and MG instrumentation

**Files:**
- Create: `util/amu/matched_workloads/npb_trace_hooks.h`
- Create: `util/amu/matched_workloads/npb-cg-trace.patch`
- Create: `util/amu/matched_workloads/npb-mg-trace.patch`
- Modify: `scripts/build_matched_breadth_workloads.py`
- Create: `tests/pyunit/cross_system/test_npb_trace_instrumentation.py`

- [ ] **Step 1: Write failing patch-integrity and boundary tests**

Use miniature CG/MG sources with the same function anchors. Require patches to
apply with zero fuzz, original arithmetic lines to remain byte-identical after
hook lines are removed, official fixture verifiers to pass, and one flipped
residual bit to fail.

- [ ] **Step 2: Run and verify RED**

Run: `PYTHONPATH=. python3 -m unittest tests.pyunit.cross_system.test_npb_trace_instrumentation -v`

Expected: missing hooks and patches.

- [ ] **Step 3: Implement observation-only hooks**

```c
void matched_phase_begin(uint16_t phase, uint64_t iteration, uint64_t work_items);
void matched_phase_end(uint16_t phase, uint64_t iteration);
void matched_dump_f64(const char *name, uint64_t iteration,
                      const double *values, uint64_t count);
void matched_reduction_edge(uint16_t phase, uint64_t parent,
                            uint64_t left, uint64_t right);
```

Hooks write raw bits and existing reduction-tree edges through the canonical
ABI; they never recompute or replace a value.

- [ ] **Step 4: Patch exact CG boundaries**

Add hooks around sparse matvec, vector updates, dot products, and every
`conj_grad` iteration. Dump `x`, `z`, `p`, `q`, `r`, residual, and final zeta.
Wrap the existing reduction tree without changing operand grouping.

- [ ] **Step 5: Patch exact MG boundaries**

Add hooks around `resid`, `rprj3`, `interp`, `psinv`, and `norm2u3`. Dump every
allocated grid level and residual after each V-cycle and record existing norm
tree edges without changing boundary handling or level order.

- [ ] **Step 6: Bind source commit, parameters, and allocation size**

Copy the frozen source to the build root, verify commit and parameter hash,
apply with `patch --fuzz=0`, build with four OpenMP threads and strict FP flags,
run an untimed allocation probe, and require its bytes to match `inputs.json`.
Missing 12.8 GB-class identity becomes `failed_input`; never fall back.

- [ ] **Step 7: Run tests and commit**

```bash
PYTHONPATH=. python3 -m unittest tests.pyunit.cross_system.test_npb_trace_instrumentation -v
git add scripts/build_matched_breadth_workloads.py util/amu/matched_workloads/npb* \
  tests/pyunit/cross_system/test_npb_trace_instrumentation.py
git commit -m "feat: instrument exact NPB CG and MG semantics"
```

### Task 8: Common Vanilla, AMU, and coherent CIRA replay in gem5

**Files:**
- Create: `util/amu/matched_workloads/trace_replay.cc`
- Create: `scripts/run_matched_breadth_gem5.py`
- Create: `tests/pyunit/cross_system/test_matched_breadth_gem5.py`
- Modify: `scripts/amu_cira_calibration.py`
- Modify: `tests/pyunit/amu/test_amu_cira_calibration.py`

- [ ] **Step 1: Write failing backend and mechanism-gate tests**

Feed gather, duplicate scatter, pointer-chain, and fixed-reduction fixtures to
Vanilla/AMU/CIRA. Require identical raw outputs and commit order. Reject AMU
per-request drain, issue/completion mismatch, any queue error, CIRA missing-core
activity, descriptor errors, non-CXL allocation, or non-1-us config.

- [ ] **Step 2: Run and verify RED**

Run: `PYTHONPATH=. python3 -m unittest tests.pyunit.cross_system.test_matched_breadth_gem5 -v`

Expected: missing replay runner.

- [ ] **Step 3: Implement one compute/commit engine with access adapters**

```cpp
struct Accessor {
  virtual Request load(uint64_t address, uint32_t bytes, uint64_t slot) = 0;
  virtual uint64_t collect(Request request) = 0;
  virtual void store(uint64_t address, uint32_t bytes, uint64_t bits) = 0;
  virtual void drain() = 0;
};

for (const Phase& phase : trace.phases()) {
  for (const WorkItem& item : phase.items()) {
    issue_independent_loads(item, accessor);
    collect_into_canonical_slots(item, accessor);
    execute_canonical_operations(item);
    commit_canonical_stores(item, accessor);
  }
  accessor.drain();
  write_boundary_bits(phase);
}
```

Vanilla uses demand accesses. AMU uses `AsyncWindow<T>` and waits only at
consumer reach, window full, or drain. CIRA issues coherent core-targeted
prefetches while the CPU keeps canonical compute/commit. Statically partition
work over four threads without changing per-item order.

- [ ] **Step 4: Implement functional and timing-window modes**

The runner accepts `--mode functional|window`, `--system`, `--trace`,
`--window-manifest`, `--phase`, `--window-index`, and `--outdir`. Functional
mode processes the full trace and dumps every boundary. Window mode executes
equal-length phase-local warmup, resets stats, measures its range, drains, and
records fixed events separately. Parse gem5 `config.ini` and allocation logs.

- [ ] **Step 5: Classify calibration evidence by structural match**

Hash/parse the Spatter CSV and label AMG/LULESH rows as direct CIRA policy
evidence. Bind MCF/CG/MG hardware rows only when input/source/ROI hashes match;
otherwise record `component_costs_only`. Explicitly reject the synthetic MCF
microbenchmark as a 345 MB speedup target. Never fit a source speedup.

- [ ] **Step 6: Run tests and commit**

```bash
PYTHONPATH=. python3 -m unittest \
  tests.pyunit.cross_system.test_matched_breadth_gem5 \
  tests.pyunit.amu.test_amu_cira_calibration -v
git add util/amu/matched_workloads/trace_replay.cc \
  scripts/run_matched_breadth_gem5.py scripts/amu_cira_calibration.py \
  tests/pyunit/cross_system/test_matched_breadth_gem5.py \
  tests/pyunit/amu/test_amu_cira_calibration.py
git commit -m "feat: execute matched breadth traces in gem5"
```

### Task 9: General M2NDP lowering with strict FuncSim gates

**Files:**
- Create: `scripts/m2ndp_workload_trace.py`
- Create: `tests/pyunit/cross_system/test_m2ndp_workload_trace.py`
- Modify: `scripts/m2ndp_artifacts.py`
- Modify: `tests/pyunit/m2ndp/test_m2ndp_artifacts.py`

- [ ] **Step 1: Write failing opcode, order, and launch tests**

Generate all six region types. Require every canonical operation to map once,
duplicate scatter stores to retain sequence, CG/MG reductions to retain tree
edges, and no FMA/vector reduction. Reject an unknown opcode or source-trace
hash drift.

- [ ] **Step 2: Run and verify RED**

Run: `PYTHONPATH=. python3 -m unittest tests.pyunit.cross_system.test_m2ndp_workload_trace -v`

Expected: missing translator.

- [ ] **Step 3: Implement strict scalar lowering**

```python
LOWERING = {
    Opcode.LOAD_F32: lambda op: f"LDG.F32 v0, [{op.address:#x}]",
    Opcode.STORE_F32: lambda op: f"STGG.F32 [{op.address:#x}], v0",
    Opcode.F32_ADD: lambda op: "FADD.F32 v2, v0, v1",
    Opcode.F32_MUL: lambda op: "FMUL.F32 v2, v0, v1",
    Opcode.F32_DIV: lambda op: "FDIV.F32 v2, v0, v1",
    Opcode.I64_MIN: lambda op: "MIN.I64 v2, v0, v1",
    Opcode.BARRIER: lambda op: "JOIN",
    Opcode.COMMIT: lambda op: "COMMIT",
}

def lower_operations(operations):
    lines = []
    for sequence, op in enumerate(operations):
        if op.sequence != sequence or op.opcode not in LOWERING:
            raise TraceTranslationError("canonical operation is not lowerable")
        lines.append(LOWERING[op.opcode](op))
    return tuple(lines)
```

Use one launch per canonical phase/window plus explicit fixed launch/completion
events. Preserve one memory map across launches. Bind package metadata to
trace, input, FuncSim, NDPSim, patch, and configuration hashes.

- [ ] **Step 4: Compare every FuncSim output boundary**

Extend `m2ndp_artifacts.py` beyond one PageRank vector. Require raw-word exact
comparison for every named float32/float64/integer boundary and nonzero FuncSim
exit on mismatch. NDPSim timing is legal only after complete FuncSim PASS and
1 us boundary calibration within one link cycle.

- [ ] **Step 5: Run tests and commit**

```bash
PYTHONPATH=. python3 -m unittest \
  tests.pyunit.cross_system.test_m2ndp_workload_trace \
  tests.pyunit.m2ndp.test_m2ndp_artifacts -v
git add scripts/m2ndp_workload_trace.py scripts/m2ndp_artifacts.py \
  tests/pyunit/cross_system/test_m2ndp_workload_trace.py \
  tests/pyunit/m2ndp/test_m2ndp_artifacts.py
git commit -m "feat: lower canonical workload traces to M2NDP"
```

### Task 10: Breadth collection, uncertainty, and safe resume

**Files:**
- Create: `scripts/run_cira_amu_m2ndp_breadth.py`
- Create: `tests/pyunit/cross_system/test_run_cira_amu_m2ndp_breadth.py`

- [ ] **Step 1: Write failing stage-order and terminal-state tests**

```python
def test_functional_pass_precedes_timing(self):
    state = breadth.new_state(identity())
    self.assertEqual(breadth.next_action(state).stage, "reference")
    mark_reference_pass(state)
    self.assertEqual(breadth.next_action(state).stage, "functional")

def test_64_windows_without_ci_gate_is_inconclusive(self):
    state = timing_state(level=64, relative_half_width="0.071")
    self.assertEqual(breadth.finish_timing(state)["status"], "inconclusive")

def test_code_hash_change_requires_new_root(self):
    with self.assertRaisesRegex(breadth.BreadthError, "fresh evidence root"):
        breadth.resume(existing_state(), identity(code="f" * 64))
```

Also test identical coordinates across four systems, phase-local warmup,
complete fixed costs, MCF dynamic phase weighting, error-counter propagation,
newest-valid checkpoint selection, and absence of a periodic timer.

- [ ] **Step 2: Run and verify RED**

Run: `PYTHONPATH=. python3 -m unittest tests.pyunit.cross_system.test_run_cira_amu_m2ndp_breadth -v`

Expected: missing orchestrator.

- [ ] **Step 3: Implement full functional gates**

```python
FUNCTIONAL_SYSTEMS = ("vanilla", "amu", "cira", "m2ndp-funcsim")
TIMING_SYSTEMS = ("vanilla", "amu", "cira", "m2ndp")

def functional_complete(records):
    return set(records) == set(FUNCTIONAL_SYSTEMS) and all(
        row["status"] == "pass" and row["bit_exact"]
        for row in records.values())
```

For each workload, run canonical reference, trace re-read, complete gem5
Vanilla/AMU/CIRA, and complete M2NDP FuncSim. Compare all raw boundaries and
mechanism counters before transition to `functional_pass`.

- [ ] **Step 4: Implement nested paired timing expansion**

Start at eight windows per nontrivial phase. Run identical coordinates for all
systems, reconstruct fixed plus weighted phase time, and bootstrap paired
speedup. If any system misses the gate, extend every system to 16, 32, then 64
so pairing remains complete. Missing the final CI gate is `inconclusive`; a
correctness or mechanism error is `failed`.

- [ ] **Step 5: Implement boundary-only checkpoint records**

Write checkpoints only after full functional completion, a complete paired
window, or a complete phase. Bind named output hashes and a monotonic sequence.
`--resume` requires identity equality and chooses the newest valid boundary.
Do not install a timer, signal-triggered live dump, or checkpoint rotation.

- [ ] **Step 6: Run tests and commit**

```bash
PYTHONPATH=. python3 -m unittest tests.pyunit.cross_system.test_run_cira_amu_m2ndp_breadth -v
git add scripts/run_cira_amu_m2ndp_breadth.py \
  tests/pyunit/cross_system/test_run_cira_amu_m2ndp_breadth.py
git commit -m "feat: collect bit-exact breadth timing evidence"
```

### Task 11: Fail-closed normalization, figure, and table

**Files:**
- Create: `scripts/generate_cira_amu_m2ndp_comparison.py`
- Create: `tests/pyunit/cross_system/test_generate_cira_amu_m2ndp_comparison.py`

- [ ] **Step 1: Write failing completeness and independent-ratio tests**

Fixtures provide 16 scaling rows and six breadth groups. Independently
recompute every speedup and CI. Reject mixed roots, stale hashes, failed
numeric bars, missing scales/systems, wrong latency/evidence labels, and a
manually changed derived file.

```python
self.assertEqual(data.scaling_scales, (4, 12, 14, 20))
self.assertEqual(data.breadth_workloads,
    ("PR", "MCF", "AMG Gather", "LULESH Scatter", "NPB CG", "NPB MG"))
self.assertEqual(data.evidence_labels,
    ("Full E2E, gem5 + NDPSim, 1 us CXL",
     "Calibrated trace-driven E2E estimate, 1 us CXL"))
```

- [ ] **Step 2: Run and verify RED**

Run: `PYTHONPATH=. MPLCONFIGDIR=/tmp/cross-system-mpl python3 -m unittest tests.pyunit.cross_system.test_generate_cira_amu_m2ndp_comparison -v`

Expected: missing publisher.

- [ ] **Step 3: Implement canonical normalized rows**

Use `Decimal`. Scaling speedup is matched Vanilla full-system seconds divided
by system seconds. Breadth speedup is paired reconstructed Vanilla seconds
divided by system seconds. Retain absolute latency, CI, evidence type, output
element counts, window count, mechanism counters, and all provenance hashes.

- [ ] **Step 4: Render deterministic layout A**

Use Matplotlib Agg, a fixed 7.0 by 3.1 inch canvas, deterministic SVG hashsalt,
and evidence hashes in PDF/SVG metadata. Panel (a) plots three scaling lines
over g4/g12/g14/g20. Panel (b) plots grouped bars with 95% CI; an inconclusive
point gets text and no numeric bar. Use fixed colors plus grayscale-safe
styles/hatches, direct labels, a 1.0x line, and an axis that shows regressions.

- [ ] **Step 5: Generate the exact LaTeX table from the same rows**

Replace `gapbs-vtune-cxl-table.tex` with system, scale/workload, absolute
latency, speedup, CI (or `--` for deterministic full runs), and evidence type.
Round only during rendering; retain unrounded CSV data.

- [ ] **Step 6: Publish five files atomically with rollback**

```text
cira-amu-m2ndp-comparison.csv
cira-amu-m2ndp-evidence.json
gapbs-vtune-cxl-table.tex
fig/cira-amu-m2ndp-scaling-breadth.pdf
fig/cira-amu-m2ndp-scaling-breadth.svg
```

Stage/fsync all files, promote with backups, and restore all prior files if
any rename fails. Inject a failure after the second promotion in tests.

- [ ] **Step 7: Run tests, raster QA, and commit**

```bash
PYTHONPATH=. MPLCONFIGDIR=/tmp/cross-system-mpl \
  python3 -m unittest tests.pyunit.cross_system.test_generate_cira_amu_m2ndp_comparison -v
pdfinfo /tmp/cross-system-publication/fig/cira-amu-m2ndp-scaling-breadth.pdf
pdftocairo -singlefile -png -r 180 \
  /tmp/cross-system-publication/fig/cira-amu-m2ndp-scaling-breadth.pdf \
  /tmp/cira-amu-m2ndp-scaling-breadth
git add scripts/generate_cira_amu_m2ndp_comparison.py \
  tests/pyunit/cross_system/test_generate_cira_amu_m2ndp_comparison.py
git commit -m "feat: publish cross-system scaling and breadth figure"
```

Expected: one vector page, correct metadata, readable labels, visible values
below 1.0x, and no bar for an inconclusive fixture.

### Task 12: Formal collection, paper integration, and final proof

**Files:**
- Modify: `docs/amu-gapbs-benchmark.md`
- Modify after evidence PASS:
  `6472666535e6f359942ddac6/sections/evaluation.tex`
- Replace after evidence PASS:
  `6472666535e6f359942ddac6/gapbs-vtune-cxl-table.tex`
- Generate after evidence PASS:
  `6472666535e6f359942ddac6/fig/cira-amu-m2ndp-scaling-breadth.pdf`
- Generate after evidence PASS:
  `6472666535e6f359942ddac6/fig/cira-amu-m2ndp-scaling-breadth.svg`

- [ ] **Step 1: Document exact preflight and collection commands**

```bash
python3 scripts/freeze_cross_system_inputs.py \
  --paper-input-record /mnt/disk0/gem5-CXL-eval/cira-amu-m2ndp-scaling-breadth/paper-input-record.json \
  --graph-manifest /mnt/disk0/gem5-CXL-eval/cira-amu-m2ndp-scaling-breadth/graphs/g4.manifest.json \
  --graph-manifest /mnt/disk0/gem5-CXL-g14-eval/graphs/g12.manifest.json \
  --graph-manifest /mnt/disk0/gem5-CXL-g14-eval/graphs/g14.manifest.json \
  --graph-manifest /mnt/disk0/gem5-CXL-eval/cira-amu-m2ndp-scaling-breadth/graphs/g20.manifest.json \
  --output /mnt/disk0/gem5-CXL-eval/cira-amu-m2ndp-scaling-breadth/inputs.json
python3 scripts/run_cira_amu_m2ndp_scaling.py \
  --inputs /mnt/disk0/gem5-CXL-eval/cira-amu-m2ndp-scaling-breadth/inputs.json \
  --calibration /mnt/disk0/gem5-CXL-eval/cira-amu-m2ndp-scaling-breadth/calibration/amu-cira-m2ndp.json \
  --root /mnt/disk0/gem5-CXL-eval/cira-amu-m2ndp-scaling-breadth/scaling
python3 scripts/run_cira_amu_m2ndp_breadth.py \
  --inputs /mnt/disk0/gem5-CXL-eval/cira-amu-m2ndp-scaling-breadth/inputs.json \
  --calibration /mnt/disk0/gem5-CXL-eval/cira-amu-m2ndp-scaling-breadth/calibration/amu-cira-m2ndp.json \
  --root /mnt/disk0/gem5-CXL-eval/cira-amu-m2ndp-scaling-breadth/breadth
```

Task 2 creates the g4/g20 manifests at these exact paths; the existing g12/g14
manifests remain read-only inputs. The paper-input record is an external source
authority supplied from the actual paper run, not inferred by this tooling. If
it or an exact workload source is absent, preflight records `failed_input` and
formal collection stops without a substitute.
Document `failed_input`, `failed`, `inconclusive`, identity-safe `--resume`,
and why old results cannot continue.

- [ ] **Step 2: Run the complete software proof**

```bash
PYTHONPATH=. MPLCONFIGDIR=/tmp/cross-system-mpl \
  python3 -m unittest discover -s tests/pyunit/cross_system -p 'test_*.py' -v
PYTHONPATH=. python3 -m unittest discover -s tests/pyunit/amu -p 'test_*.py' -v
PYTHONPATH=. MPLCONFIGDIR=/tmp/cross-system-mpl \
  python3 -m unittest discover -s tests/pyunit/m2ndp -p 'test_*.py' -v
scons build/X86/gem5.opt -j2
```

Expected: all tests pass and gem5 builds. Record test count, command/exit
status, gem5 hash, and log hashes. Run tiny fixture traces through all systems
and require the publisher to reject them as non-paper inputs.

- [ ] **Step 3: Launch formal work without a timeout**

After input preflight passes, use an identity-derived fresh root and launch
scaling then breadth through `systemd-run --user --collect` with stdout/stderr
inside that root. Set no wall-clock or simulation timeout. Record the unit
name. The service has no periodic checkpointing; restart only with explicit
identity-validated `--resume` at a phase/window boundary.

- [ ] **Step 4: Independently validate before publication**

Require 16/16 full scaling rows. Recompute all raw-bit comparisons, absolute
latencies, speedups, phase weights, and bootstrap intervals from raw evidence.
Publish numeric breadth bars only for terminal complete rows; retain and name
any inconclusive row.

- [ ] **Step 5: Fast-forward-safe paper integration**

```bash
git -C /home/victoryang00/gem5-CXL/6472666535e6f359942ddac6 status --short
git -C /home/victoryang00/gem5-CXL/6472666535e6f359942ddac6 pull --ff-only origin master
```

Abort on dirty/non-fast-forward state. Copy only validated publication files.
Update `evaluation.tex` to replace the g4-only figure, label g4 as a fixed-cost
correctness point, identify g20 as 2^20 vertices/about 240 MB, distinguish full
and trace-driven evidence, and limit claims to six matched regions. Amend the
approved reviewer response to name any inconclusive workload.

- [ ] **Step 6: Build and visually inspect the paper**

Use the paper's tracked build command; if none exists, run
`latexmk -pdf -interaction=nonstopmode -halt-on-error main.tex`. Require no
missing figure/table/reference or new overfull table. Rasterize the page and
inspect axes, direct labels, CI, caption, and final-size legibility.

- [ ] **Step 7: Commit and push the code branch**

```bash
git add docs/amu-gapbs-benchmark.md
git commit -m "docs: document cross-system evidence collection"
git push origin m2ndp-g20-pr-spmv
test "$(git rev-parse HEAD)" = \
  "$(git ls-remote origin refs/heads/m2ndp-g20-pr-spmv | awk '{print $1}')"
```

- [ ] **Step 8: Commit and push the independent paper repository**

```bash
git -C /home/victoryang00/gem5-CXL/6472666535e6f359942ddac6 add \
  sections/evaluation.tex gapbs-vtune-cxl-table.tex \
  fig/cira-amu-m2ndp-scaling-breadth.pdf \
  fig/cira-amu-m2ndp-scaling-breadth.svg
git -C /home/victoryang00/gem5-CXL/6472666535e6f359942ddac6 commit \
  -m "Replace g4 comparison with cross-system scaling evidence"
git -C /home/victoryang00/gem5-CXL/6472666535e6f359942ddac6 push origin master
```

- [ ] **Step 9: Record acceptance evidence**

Report code/paper commits and remote equality, evidence identity, 16/16
scaling completion, per-workload breadth status/window count/CI, bit-exact
element counts, mechanism error counters, calibration residuals, canonical
CSV/table/figure paths, and every explicitly inconclusive row. Do not call the
work complete if an input is `failed_input`, a numeric bar lacks the CI gate,
or either required repository was not pushed.
