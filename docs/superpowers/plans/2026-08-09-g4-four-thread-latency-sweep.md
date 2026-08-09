# G4 Four-Thread Latency Sweep Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build and run a fail-closed 16-point PageRank matrix comparing four-thread Vanilla CXL, AMU, coherent CIRA, and M2NDP on one fixed g4 graph at 200 ns, 500 ns, 1 us, and 2 us.

**Architecture:** Add explicit immutable experiment profiles, then make the existing matched-variant and M2NDP runners consume a selected profile and latency without weakening the g20 defaults. A small top-level sweep orchestrator runs each latency sequentially into isolated directories, while a separate publication module validates the entire 4-by-4 matrix and atomically emits CSV, evidence JSON, TeX, PDF, and SVG only after every correctness and provenance gate passes.

**Tech Stack:** Python 3.13, `unittest`, gem5 X86 timing simulation, GAPBS/OpenMP, CRIU-independent gem5 application checkpoints, M2NDP FuncSim/NDPSim, Matplotlib, systemd transient services.

---

## File structure

- Create `scripts/gapbs_pr_experiment_profiles.py`: immutable g20 and g4 profile definitions plus graph, latency, core, and thread validation.
- Create `tests/pyunit/m2ndp/test_gapbs_pr_experiment_profiles.py`: focused profile contract tests.
- Modify `scripts/run_gapbs_matched_pr_spmv_variants.py`: select a formal profile and latency; retain the old g20 defaults.
- Modify `tests/pyunit/m2ndp/test_run_gapbs_matched_pr_spmv_variants.py`: prove g4/four-thread argument construction and fail-closed row validation.
- Modify `scripts/m2ndp_artifacts.py`: validate graph metadata against a selected formal profile.
- Modify `scripts/m2ndp_results.py`: validate gem5 and provenance evidence against a selected profile and latency.
- Modify `scripts/compare_gapbs_cxl_amu_cira.py`: accept the same formal profile at the checkpoint CLI boundary so g4/four-core baselines are launchable without smoke mode.
- Modify `scripts/run_m2ndp_g20_pr_spmv.py`: make cores, graph contract, latency, state, calibration, and manifest profile-aware without changing default g20 behavior.
- Modify `tests/pyunit/amu/test_compare_gapbs_cxl_amu_cira.py`: cover the formal four-thread checkpoint profile gate.
- Modify `tests/pyunit/m2ndp/test_m2ndp_artifacts.py`, `tests/pyunit/m2ndp/test_m2ndp_results.py`, and `tests/pyunit/m2ndp/test_run_m2ndp_g20_pr_spmv.py`: cover both formal profiles and cross-profile rejection.
- Create `scripts/run_gapbs_g4_4thread_latency_sweep.py`: resumable sequential 4-latency orchestrator.
- Create `tests/pyunit/m2ndp/test_run_gapbs_g4_4thread_latency_sweep.py`: matrix, command, resume, and failure tests.
- Create `scripts/generate_gapbs_g4_4thread_latency_results.py`: aggregate validator and atomic CSV/evidence/TeX publisher.
- Create `tests/pyunit/m2ndp/test_generate_gapbs_g4_4thread_latency_results.py`: 16-row completeness, bit-exactness, calibration, and speedup recomputation tests.
- Create `scripts/generate_gapbs_g4_4thread_latency_figure.py`: deterministic PDF/SVG rendering from the validated aggregate rows.
- Create `tests/pyunit/m2ndp/test_generate_gapbs_g4_4thread_latency_figure.py`: chart contract and deterministic export tests.
- Modify `docs/amu-gapbs-benchmark.md`: document the exact g4 build, launch, resume, validation, and publication commands.

### Task 1: Add immutable experiment profiles

**Files:**
- Create: `scripts/gapbs_pr_experiment_profiles.py`
- Create: `tests/pyunit/m2ndp/test_gapbs_pr_experiment_profiles.py`

- [ ] **Step 1: Write the failing profile tests**

```python
from pathlib import Path
import tempfile
import unittest

from scripts import gapbs_pr_experiment_profiles as profiles


class ExperimentProfileTest(unittest.TestCase):
    def test_g4_profile_is_four_thread_four_latency_contract(self):
        profile = profiles.get_profile("g4-4thread-sweep")
        self.assertEqual(profile.graph_scale, 4)
        self.assertEqual(profile.graph_sha256, profiles.G4_SHA256)
        self.assertEqual(profile.num_nodes, 16)
        self.assertEqual(profile.cores, 4)
        self.assertEqual(profile.threads, 4)
        self.assertEqual(
            profile.latencies,
            ("200ns", "500ns", "1us", "2us"),
        )

    def test_graph_hash_mismatch_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            graph = Path(tmp) / "g4.sg"
            graph.write_bytes(b"wrong graph")
            with self.assertRaisesRegex(
                profiles.ProfileError, "graph SHA-256"
            ):
                profiles.validate_graph(
                    profiles.get_profile("g4-4thread-sweep"), graph
                )

    def test_latency_outside_profile_is_rejected(self):
        with self.assertRaisesRegex(
            profiles.ProfileError, "latency 3us"
        ):
            profiles.require_latency(
                profiles.get_profile("g4-4thread-sweep"), "3us"
            )
```

- [ ] **Step 2: Run the tests and verify the module is absent**

Run:

```bash
python3 -m unittest discover -s tests/pyunit/m2ndp -p 'test_gapbs_pr_experiment_profiles.py' -v
```

Expected: FAIL with an import error for `gapbs_pr_experiment_profiles`.

- [ ] **Step 3: Implement the profile module**

```python
import dataclasses
from pathlib import Path

from scripts import m2ndp_artifacts

G4_SHA256 = "f234532690f6cfc30e993c4d9a1839e65002a618e7da20ea6a4242818b9c6c3d"
LATENCY_TICKS = {
    "200ns": 200_000,
    "500ns": 500_000,
    "1us": 1_000_000,
    "2us": 2_000_000,
}


class ProfileError(RuntimeError):
    pass


@dataclasses.dataclass(frozen=True)
class ExperimentProfile:
    name: str
    graph_scale: int
    graph_sha256: str
    num_nodes: int
    cores: int
    threads: int
    latencies: tuple[str, ...]
    trials: int = 2
    measured_trial: int = 1
    page_rank_iterations: int = 20


PROFILES = {
    "g20-2thread-1us": ExperimentProfile(
        name="g20-2thread-1us",
        graph_scale=20,
        graph_sha256=m2ndp_artifacts.EXPECTED_G20_SHA256,
        num_nodes=1 << 20,
        cores=2,
        threads=2,
        latencies=("1us",),
    ),
    "g4-4thread-sweep": ExperimentProfile(
        name="g4-4thread-sweep",
        graph_scale=4,
        graph_sha256=G4_SHA256,
        num_nodes=1 << 4,
        cores=4,
        threads=4,
        latencies=("200ns", "500ns", "1us", "2us"),
    ),
}


def get_profile(name):
    try:
        return PROFILES[name]
    except KeyError as error:
        raise ProfileError(f"unknown experiment profile: {name}") from error


def validate_graph(profile, graph):
    graph = Path(graph)
    if m2ndp_artifacts.sha256_file(graph) != profile.graph_sha256:
        raise ProfileError("graph SHA-256 does not match experiment profile")
    return graph


def require_latency(profile, latency):
    if latency not in profile.latencies:
        raise ProfileError(
            f"latency {latency} is outside profile {profile.name}"
        )
    return LATENCY_TICKS[latency]
```

- [ ] **Step 4: Run focused tests**

Run:

```bash
python3 -m unittest discover -s tests/pyunit/m2ndp -p 'test_gapbs_pr_experiment_profiles.py' -v
```

Expected: all profile tests PASS.

- [ ] **Step 5: Commit the profile layer**

```bash
git add scripts/gapbs_pr_experiment_profiles.py tests/pyunit/m2ndp/test_gapbs_pr_experiment_profiles.py
git commit -m "feat: define formal PageRank experiment profiles"
```

### Task 2: Make the matched AMU/CIRA runner profile-aware

**Files:**
- Modify: `scripts/run_gapbs_matched_pr_spmv_variants.py`
- Modify: `tests/pyunit/m2ndp/test_run_gapbs_matched_pr_spmv_variants.py`

- [ ] **Step 1: Add failing four-thread argument and row tests**

```python
def test_g4_profile_builds_four_core_latency_specific_args(self):
    options = SimpleNamespace(
        profile="g4-4thread-sweep",
        gem5=Path("gem5.opt"),
        config=Path("config.py"),
        graph=Path("g4.sg"),
        graph_scale=4,
        cxl_link_delay="500ns",
        checkpoint_root=Path("checkpoints"),
        outdir=Path("run"),
        timeout=0,
    )
    args = runner.make_compare_args(options)
    self.assertEqual(args.cores, 4)
    self.assertEqual(args.cxl_link_delay, "500ns")
    self.assertIn("OMP_NUM_THREADS=4", args.env)


def test_g4_row_rejects_two_core_result(self):
    row = self.valid_row("cira")
    row.update(scale=4, cores=2, cxl_link_delay="500ns")
    with self.assertRaisesRegex(runner.VariantRunError, "cores"):
        runner.validate_row(
            row,
            "cira",
            profile_name="g4-4thread-sweep",
            latency="500ns",
        )
```

- [ ] **Step 2: Verify the new tests fail**

Run:

```bash
python3 -m unittest discover -s tests/pyunit/m2ndp -p 'test_run_gapbs_matched_pr_spmv_variants.py' -v
```

Expected: FAIL because profile and latency arguments are unsupported.

- [ ] **Step 3: Replace hard-coded publication values with the selected profile**

Add CLI arguments with compatibility-preserving defaults:

```python
parser.add_argument(
    "--profile",
    choices=("g20-2thread-1us", "g4-4thread-sweep"),
    default="g20-2thread-1us",
)
parser.add_argument("--cxl-link-delay", default="1us")
```

Resolve the contract once and use it in `make_compare_args`:

```python
profile = profiles.get_profile(options.profile)
profiles.require_latency(profile, options.cxl_link_delay)
return SimpleNamespace(
    gem5=Path(options.gem5).resolve(),
    config=Path(options.config).resolve(),
    scale=profile.graph_scale,
    iterations=profile.trials,
    cpu="timing",
    fast_forward_cpu=None,
    measure_trial=profile.measured_trial,
    cores=profile.cores,
    mem_size="4GiB",
    graph=Path(options.graph).resolve(),
    graph_scale=profile.graph_scale,
    checkpoint_root=Path(options.checkpoint_root).resolve(),
    reuse_checkpoints=True,
    smoke_test=False,
    cxl_link_delay=options.cxl_link_delay,
    disable_hw_prefetchers=False,
    l1_mshrs=None,
    l1_tgts_per_mshr=None,
    l2_mshrs=None,
    l2_tgts_per_mshr=None,
    asmc_spm_size="256KiB",
    asmc_granularity=8,
    asmc_max_outstanding=256,
    asmc_max_send_queue=512,
    asmc_issue_latency="1ns",
    asmc_completion_latency="0ns",
    asmc_latency="0ns",
    cira_max_outstanding=256,
    cira_max_send_queue=1024,
    cira_max_csr_walk_queue=4096,
    cira_csr_lines_per_turn=64,
    cira_max_completed_lines=65536,
    cira_issue_latency="1ns",
    cira_completion_latency="0ns",
    roi_work_events=True,
    verify=True,
    env=[f"OMP_NUM_THREADS={profile.threads}"],
    allow_zero_cira=False,
    timeout=options.timeout,
    dry_run=False,
    outdir=Path(options.outdir).resolve(),
)
```

Change `validate_row` to accept `profile_name` and `latency`, require the
profile's graph scale, hash, core count, and exact latency, and keep the
existing AMU issued/completed and CIRA positive-activity gates. Remove the
`--smoke-test` path from formal g4 execution; retain it only for existing unit
and developer smoke callers.

- [ ] **Step 4: Run matched-runner tests**

Run:

```bash
python3 -m unittest discover -s tests/pyunit/m2ndp -p 'test_*matched_pr_spmv_variants.py' -v
```

Expected: PASS, including unchanged g20 defaults and new g4/four-thread cases.

- [ ] **Step 5: Commit the matched runner**

```bash
git add scripts/run_gapbs_matched_pr_spmv_variants.py tests/pyunit/m2ndp/test_run_gapbs_matched_pr_spmv_variants.py
git commit -m "feat: run matched variants under formal profiles"
```

### Task 3: Generalize M2NDP evidence and orchestration contracts

**Files:**
- Modify: `scripts/m2ndp_artifacts.py`
- Modify: `scripts/m2ndp_results.py`
- Modify: `scripts/compare_gapbs_cxl_amu_cira.py`
- Modify: `scripts/run_m2ndp_g20_pr_spmv.py`
- Modify: `tests/pyunit/amu/test_compare_gapbs_cxl_amu_cira.py`
- Modify: `tests/pyunit/m2ndp/test_m2ndp_artifacts.py`
- Modify: `tests/pyunit/m2ndp/test_m2ndp_results.py`
- Modify: `tests/pyunit/m2ndp/test_run_m2ndp_g20_pr_spmv.py`

- [ ] **Step 1: Add failing profile-specific evidence tests**

```python
def test_g4_metadata_passes_formal_profile(self):
    meta = artifacts.GraphMeta(
        graph_sha256=profiles.G4_SHA256,
        num_nodes=16,
        num_directed_edges=64,
        directed=False,
    )
    artifacts.validate_profile_graph(
        meta, profiles.get_profile("g4-4thread-sweep")
    )


def test_g4_gem5_summary_requires_four_cores_and_selected_latency(self):
    row = valid_gem5_row()
    row.update(
        graph_sha256=profiles.G4_SHA256,
        scale="4",
        cores="4",
        cxl_link_delay="2us",
    )
    ticks = results.validate_gem5_row(
        row,
        profile=profiles.get_profile("g4-4thread-sweep"),
        latency="2us",
    )
    self.assertGreater(ticks, 0)


def test_g4_m2ndp_command_is_four_core_two_trial_fixed_twenty(self):
    self.options.profile = "g4-4thread-sweep"
    self.options.graph_scale = 4
    self.options.cxl_link_delay = "200ns"
    command = runner.gem5_command(self.options, self.paths)
    self.assertEqual(command[command.index("--cores") + 1], "4")
    self.assertEqual(
        command[command.index("--cxl-link-delay") + 1], "200ns"
    )
```

- [ ] **Step 2: Run the three suites and verify failure**

Run:

```bash
python3 -m unittest discover -s tests/pyunit/m2ndp -p 'test_*m2ndp*.py' -v
```

Expected: FAIL on missing profile-aware APIs and option fields.

- [ ] **Step 3: Implement profile-aware graph and result validation**

In `m2ndp_artifacts.py`, add:

```python
def validate_profile_graph(meta, profile):
    if meta.graph_sha256 != profile.graph_sha256:
        raise EvidenceError("graph SHA-256 does not match profile")
    if meta.num_nodes != profile.num_nodes:
        raise EvidenceError("graph node count does not match profile")
    if meta.num_directed_edges < 0:
        raise EvidenceError("graph directed edge count is negative")
```

In `m2ndp_results.py`, replace `_GEM5_CONTRACT` with:

```python
def expected_gem5_contract(profile, latency):
    return {
        "benchmark": "pr_spmv",
        "kind": "baseline",
        "status": "ok",
        "verification": "pass",
        "roi_cpu": "timing",
        "cores": str(profile.cores),
        "cxl_link_delay": latency,
        "all_memory_cxl": "True",
        "graph_sha256": profile.graph_sha256,
        "iterations": str(profile.trials),
        "measured_trial": str(profile.measured_trial),
        "checkpoint_restores": "1",
    }
```

Thread `profile` and `latency` through `parse_gem5_summary`,
`_validate_provenance`, and `build_summary`. Default them to the existing
g20/1us profile only at public CLI boundaries, not inside validators.

- [ ] **Step 4: Implement profile-aware M2NDP state and commands**

Extend `Options` and CLI parsing:

```python
@dataclasses.dataclass(frozen=True)
class Options:
    graph: Path
    graph_scale: int
    cxlmemuring: Path
    m2ndp_root: Path
    gem5: Path
    outdir: Path
    smoke_test: bool
    resume: bool
    timeout: int
    stop_after: str | None
    profile: str = "g20-2thread-1us"
    cxl_link_delay: str = "1us"

parser.add_argument(
    "--profile",
    choices=("g20-2thread-1us", "g4-4thread-sweep"),
    default="g20-2thread-1us",
)
parser.add_argument("--cxl-link-delay", default="1us")
```

Resolve the profile in `new_state`, `gem5_command`, trace validation,
calibration command construction, and final manifest construction. Record
`profile`, `cores`, `threads`, and `cxl_link_delay` in the immutable state
contract. Call calibration with the selected latency, and pass the selected
profile and latency into `m2ndp_results`. Thread the profile through the
lower-level comparison CLI as well; otherwise its legacy two-core/scale-20
checkpoint gate rejects the generated formal g4 command before gem5 starts.

- [ ] **Step 5: Run the M2NDP suites**

Run:

```bash
python3 -m unittest discover -s tests/pyunit/m2ndp -p 'test_*m2ndp*.py' -v
```

Expected: all tests PASS for g20 defaults and g4 formal profile.

- [ ] **Step 6: Commit the M2NDP generalization**

```bash
git add scripts/m2ndp_artifacts.py scripts/m2ndp_results.py scripts/run_m2ndp_g20_pr_spmv.py tests/pyunit/m2ndp/test_m2ndp_artifacts.py tests/pyunit/m2ndp/test_m2ndp_results.py tests/pyunit/m2ndp/test_run_m2ndp_g20_pr_spmv.py
git commit -m "feat: support formal M2NDP experiment profiles"
```

### Task 4: Build the resumable four-latency sweep orchestrator

**Files:**
- Create: `scripts/run_gapbs_g4_4thread_latency_sweep.py`
- Create: `tests/pyunit/m2ndp/test_run_gapbs_g4_4thread_latency_sweep.py`

- [ ] **Step 1: Write failing matrix and failure-propagation tests**

```python
class SweepRunnerTest(unittest.TestCase):
    def test_matrix_has_four_latencies_and_four_systems(self):
        matrix = runner.build_matrix()
        self.assertEqual(len(matrix), 16)
        self.assertEqual(
            {item.latency for item in matrix},
            {"200ns", "500ns", "1us", "2us"},
        )
        self.assertEqual(
            {item.system for item in matrix},
            {"vanilla", "amu", "cira", "m2ndp"},
        )

    def test_failure_blocks_later_latency_and_publication(self):
        state = runner.new_state()
        state["latencies"]["500ns"]["cira"] = "failed"
        with self.assertRaisesRegex(runner.SweepError, "500ns/cira"):
            runner.next_action(state)

    def test_resume_requires_matching_output_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root / "summary.csv"
            output.write_text("original\n")
            state = runner.new_state()
            runner.record_pass(state, "200ns", "amu", output)
            output.write_text("changed\n")
            self.assertTrue(
                runner.invalidate_changed_outputs(state, root)
            )
```

- [ ] **Step 2: Run the new test module and verify failure**

Run:

```bash
python3 -m unittest discover -s tests/pyunit/m2ndp -p 'test_run_gapbs_g4_4thread_latency_sweep.py' -v
```

Expected: FAIL because the sweep module does not exist.

- [ ] **Step 3: Implement sequential resumable orchestration**

Define immutable entries and ordered execution:

```python
LATENCIES = ("200ns", "500ns", "1us", "2us")
SYSTEMS = ("vanilla", "amu", "cira", "m2ndp")


@dataclasses.dataclass(frozen=True)
class MatrixEntry:
    latency: str
    system: str


def build_matrix():
    return tuple(
        MatrixEntry(latency, system)
        for latency in LATENCIES
        for system in SYSTEMS
    )
```

For each latency, invoke the M2NDP orchestrator once; its gem5 baseline stage
produces Vanilla and its remaining stages produce M2NDP. Invoke the matched
runner separately for AMU and CIRA using the same profile, graph, latency, and
variant build. Persist `status.json` after each system using atomic JSON and
hash every completed output. Resume only hashed passed entries. Store each
latency under `runs/<latency>/` and never write to existing g20 roots.

- [ ] **Step 4: Run sweep-runner tests**

Run:

```bash
python3 -m unittest discover -s tests/pyunit/m2ndp -p 'test_run_gapbs_g4_4thread_latency_sweep.py' -v
```

Expected: all matrix, command, resume, and failure tests PASS.

- [ ] **Step 5: Commit the sweep orchestrator**

```bash
git add scripts/run_gapbs_g4_4thread_latency_sweep.py tests/pyunit/m2ndp/test_run_gapbs_g4_4thread_latency_sweep.py
git commit -m "feat: orchestrate g4 four-thread latency sweep"
```

### Task 5: Add fail-closed aggregate publication

**Files:**
- Create: `scripts/generate_gapbs_g4_4thread_latency_results.py`
- Create: `tests/pyunit/m2ndp/test_generate_gapbs_g4_4thread_latency_results.py`

- [ ] **Step 1: Write failing completeness and matched-baseline tests**

```python
def test_publication_requires_exact_16_row_matrix(self):
    rows = make_valid_rows()[:-1]
    with self.assertRaisesRegex(publisher.PublicationError, "16 rows"):
        publisher.validate_matrix(rows)


def test_speedup_uses_same_latency_vanilla(self):
    rows = make_valid_rows()
    cira = next(
        row for row in rows
        if row["latency"] == "500ns" and row["system"] == "cira"
    )
    cira["speedup_vs_vanilla_cxl"] = "9.0"
    with self.assertRaisesRegex(
        publisher.PublicationError, "speedup mismatch"
    ):
        publisher.validate_matrix(rows)


def test_raw_hash_mismatch_blocks_publication(self):
    rows = make_valid_rows()
    rows[3]["result_sha256"] = "f" * 64
    with self.assertRaisesRegex(
        publisher.PublicationError, "bit-exact"
    ):
        publisher.validate_matrix(rows)
```

- [ ] **Step 2: Run the publication tests and verify failure**

Run:

```bash
python3 -m unittest discover -s tests/pyunit/m2ndp -p 'test_generate_gapbs_g4_4thread_latency_results.py' -v
```

Expected: FAIL because the publisher does not exist.

- [ ] **Step 3: Implement canonical aggregation and atomic output**

Use exact decimal arithmetic:

```python
TICKS_PER_SECOND = Decimal(10**12)


def gem5_seconds(row):
    return Decimal(row["sim_ticks"]) / TICKS_PER_SECOND


def m2ndp_seconds(row):
    return (
        Decimal(row["measured_cycles"])
        * Decimal(row["core_period_seconds"])
    )


def recompute_speedup(vanilla_seconds, mechanism_seconds):
    if vanilla_seconds <= 0 or mechanism_seconds <= 0:
        raise PublicationError("latency must be positive")
    return vanilla_seconds / mechanism_seconds
```

Require exactly one row for every latency/system key, exact profile and graph
hash, four cores/threads for host rows, all-CXL placement, two trials, fixed
20 iterations, per-mechanism activity gates, four passed calibrations, and one
identical raw reference hash per latency. Write the aggregate CSV, evidence
JSON, and TeX into a staging directory, fsync them, and rename the directory
to `published/` only after reloading and revalidating all outputs.

- [ ] **Step 4: Run publication tests**

Run:

```bash
python3 -m unittest discover -s tests/pyunit/m2ndp -p 'test_generate_gapbs_g4_4thread_latency_results.py' -v
```

Expected: all completeness, correctness, arithmetic, and atomic-publication
tests PASS.

- [ ] **Step 5: Commit publication support**

```bash
git add scripts/generate_gapbs_g4_4thread_latency_results.py tests/pyunit/m2ndp/test_generate_gapbs_g4_4thread_latency_results.py
git commit -m "feat: publish validated g4 latency results"
```

### Task 6: Generate the paper-quality latency figure

**Files:**
- Create: `scripts/generate_gapbs_g4_4thread_latency_figure.py`
- Create: `tests/pyunit/m2ndp/test_generate_gapbs_g4_4thread_latency_figure.py`

- [ ] **Step 1: Write failing chart-contract tests**

```python
def test_figure_has_four_latency_points_per_mechanism(self):
    data = figure.prepare_figure_data(
        make_valid_rows(), evidence_sha256="a" * 64
    )
    self.assertEqual(data.latency_ns, (200, 500, 1000, 2000))
    self.assertEqual(set(data.series), {"AMU", "CIRA", "M2NDP"})
    self.assertTrue(all(len(values) == 4 for values in data.series.values()))


def test_vanilla_is_explicit_one_x_reference(self):
    data = figure.prepare_figure_data(
        make_valid_rows(), evidence_sha256="a" * 64
    )
    self.assertEqual(data.vanilla_reference, (1.0, 1.0, 1.0, 1.0))
```

- [ ] **Step 2: Verify the new figure tests fail**

Run:

```bash
python3 -m unittest discover -s tests/pyunit/m2ndp -p 'test_generate_gapbs_g4_4thread_latency_figure.py' -v
```

Expected: FAIL because the figure module does not exist.

- [ ] **Step 3: Implement deterministic PDF/SVG rendering**

Render one line per non-baseline mechanism, a neutral 1.0x Vanilla reference,
ordered latency ticks `(200, 500, 1000, 2000)`, zero-based y-axis unless the
data require a log scale, direct end labels, distinct markers and line styles,
and evidence SHA-256 in PDF/SVG metadata. Use fixed colors and
`svg.hashsalt = "gapbs-g4-4thread-latency"`. Write both formats atomically.

- [ ] **Step 4: Run figure tests and inspect generated fixtures**

Run:

```bash
python3 -m unittest discover -s tests/pyunit/m2ndp -p 'test_generate_gapbs_g4_4thread_latency_figure.py' -v
```

Expected: PASS and deterministic non-empty PDF/SVG byte streams.

- [ ] **Step 5: Commit figure generation**

```bash
git add scripts/generate_gapbs_g4_4thread_latency_figure.py tests/pyunit/m2ndp/test_generate_gapbs_g4_4thread_latency_figure.py
git commit -m "feat: plot g4 four-thread latency sweep"
```

### Task 7: Document commands and run the full regression gate

**Files:**
- Modify: `docs/amu-gapbs-benchmark.md`

- [ ] **Step 1: Add exact build and foreground proof commands**

Document these paths and parameters:

```bash
python3 scripts/build_gapbs_m2ndp_pr_spmv.py \
  --cxlmemuring /home/victoryang00/CXLMemUring \
  --outdir m5out/g4_4thread_latency_sweep_20260809/build/baseline \
  --reference-raw m5out/g4_4thread_latency_sweep_20260809/build/baseline-unused.u32 \
  --m5-library util/m5/build/x86/out/libm5.a

python3 scripts/build_gapbs_matched_pr_spmv_variants.py \
  --baseline-build m5out/g4_4thread_latency_sweep_20260809/build/baseline \
  --cxlmemuring /home/victoryang00/CXLMemUring \
  --m5-library util/m5/build/x86/out/libm5.a \
  --outdir m5out/g4_4thread_latency_sweep_20260809/build/variants

python3 scripts/run_gapbs_g4_4thread_latency_sweep.py \
  --graph /home/victoryang00/gem5-CXL/.worktrees/gapbs-latency-table/m5out/gapbs_graphs/g4.sg \
  --variants-build m5out/g4_4thread_latency_sweep_20260809/build/variants \
  --cxlmemuring /home/victoryang00/CXLMemUring \
  --m2ndp-root m5out/m2ndp/source \
  --gem5 build/X86/gem5.opt \
  --outdir m5out/g4_4thread_latency_sweep_20260809 \
  --timeout 0
```

- [ ] **Step 2: Run focused and full Python tests**

Run:

```bash
python3 -m unittest discover -s tests/pyunit/m2ndp -p 'test_*.py' -v
python3 -m unittest discover -s tests/pyunit/amu -p 'test_*.py' -v
```

Expected: all listed tests PASS with zero failures and zero errors.

The repository-wide `tests/pyunit/test_run.py` file is a gem5 TestLib
registration entrypoint, not a standalone `unittest` module; do not include
it in ordinary discovery without initializing TestLib configuration.

- [ ] **Step 3: Run static and compatibility checks**

Run:

```bash
python3 -m py_compile \
  scripts/gapbs_pr_experiment_profiles.py \
  scripts/run_gapbs_matched_pr_spmv_variants.py \
  scripts/run_m2ndp_g20_pr_spmv.py \
  scripts/run_gapbs_g4_4thread_latency_sweep.py \
  scripts/generate_gapbs_g4_4thread_latency_results.py \
  scripts/generate_gapbs_g4_4thread_latency_figure.py
```

Expected: exit status 0 with no output.

- [ ] **Step 4: Commit documentation**

```bash
git add docs/amu-gapbs-benchmark.md
git commit -m "docs: add g4 four-thread sweep workflow"
```

### Task 8: Run proof, launch the persistent sweep, and publish results

**Files:**
- Generate under: `m5out/g4_4thread_latency_sweep_20260809/`
- Do not modify: existing `m5out/cira_multicore_g20_e2e_20260804/` or `m5out/m2ndp_g20_pr_spmv_e2e/`

- [ ] **Step 1: Verify live g20 process identities before launch**

Run:

```bash
sudo ps -o pid,ppid,stat,cmd -p 1425077,1426962
```

Expected: the restored CIRA runner and gem5 process are alive. Record the
output in `m5out/g4_4thread_latency_sweep_20260809/prelaunch-processes.txt`.

- [ ] **Step 2: Run one isolated proof through all four mechanisms**

Run the sweep with `--stop-after-latency 200ns` and a separate output root:

```bash
python3 scripts/run_gapbs_g4_4thread_latency_sweep.py \
  --graph /home/victoryang00/gem5-CXL/.worktrees/gapbs-latency-table/m5out/gapbs_graphs/g4.sg \
  --variants-build m5out/g4_4thread_latency_sweep_20260809/build/variants \
  --cxlmemuring /home/victoryang00/CXLMemUring \
  --m2ndp-root m5out/m2ndp/source \
  --gem5 build/X86/gem5.opt \
  --outdir m5out/g4_4thread_latency_sweep_20260809-proof \
  --stop-after-latency 200ns \
  --timeout 0
```

Expected: Vanilla, AMU, CIRA, and M2NDP all PASS at 200 ns, with four-thread
host evidence and bit-exact hashes. The proof output is never copied into the
formal result root.

- [ ] **Step 3: Start the full sweep as a low-priority transient service**

Run:

```bash
sudo systemd-run \
  --unit=gapbs-g4-4thread-sweep-20260809 \
  --property=WorkingDirectory=/home/victoryang00/gem5-CXL/.worktrees/m2ndp-g20-pr-spmv \
  --property=Nice=10 \
  --property=CPUWeight=10 \
  --property=StandardOutput=append:/home/victoryang00/gem5-CXL/.worktrees/m2ndp-g20-pr-spmv/m5out/g4_4thread_latency_sweep_20260809/service.log \
  --property=StandardError=append:/home/victoryang00/gem5-CXL/.worktrees/m2ndp-g20-pr-spmv/m5out/g4_4thread_latency_sweep_20260809/service.log \
  /usr/bin/python3 scripts/run_gapbs_g4_4thread_latency_sweep.py \
  --graph /home/victoryang00/gem5-CXL/.worktrees/gapbs-latency-table/m5out/gapbs_graphs/g4.sg \
  --variants-build m5out/g4_4thread_latency_sweep_20260809/build/variants \
  --cxlmemuring /home/victoryang00/CXLMemUring \
  --m2ndp-root m5out/m2ndp/source \
  --gem5 build/X86/gem5.opt \
  --outdir m5out/g4_4thread_latency_sweep_20260809 \
  --timeout 0
```

Expected: unit is active, output paths are separate from g20, and the g20 PIDs
remain alive.

- [ ] **Step 4: Validate the completed 16-row matrix**

After the service exits successfully, run:

```bash
python3 scripts/generate_gapbs_g4_4thread_latency_results.py \
  --sweep-root m5out/g4_4thread_latency_sweep_20260809
```

Expected: exactly 16 rows, four passed calibrations, zero bit mismatches, and
independently recomputed speedups.

- [ ] **Step 5: Inspect the final table and figure**

Run:

```bash
pdfinfo m5out/g4_4thread_latency_sweep_20260809/published/gapbs-g4-4thread-latency-sweep.pdf
```

Expected: one valid PDF page.

Render the PDF to PNG with `pdftocairo`, inspect the image, and verify labels,
line styles, the 1.0x reference, and all four latency ticks are visible. Run an
independent Decimal-based recomputation of all speedups from the canonical CSV
and require exact agreement with the evidence JSON.

- [ ] **Step 6: Commit only source and documentation, not generated m5out data**

Run:

```bash
git status --short
```

Expected: no uncommitted source changes and no tracked generated result files.
Do not add `m5out/` outputs to git.
