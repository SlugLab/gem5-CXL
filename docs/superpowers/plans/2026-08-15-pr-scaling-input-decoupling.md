# PR Scaling Input Decoupling and Plot Family Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run and publish a bit-exact 16-point Vanilla/AMU/CIRA/M2NDP PageRank scaling matrix from real g4/g12/g14/g20 inputs without relaxing the blocked six-workload breadth gate.

**Architecture:** A new scoped freezer binds only the four immutable PageRank graph manifests. The existing scaling runner validates that manifest and carries its graph identity into terminal evidence; a focused publisher emits lossless raw data, LaTeX, and four deterministic plot families. The strict breadth freezer stays unchanged, while the eventual combined publisher joins independent roots only when calibration and g20 graph identities match.

**Tech Stack:** Python 3 standard library, `unittest`, SHA-256 canonical JSON, Matplotlib Agg, gem5 X86 timing mode, CXL/AMU/CIRA models, M2NDP FuncSim and NDPSim, systemd transient services.

---

## File map

- Modify `scripts/prepare_gapbs_pr_graph.py` to freeze an existing g4 or g20 graph without rewriting it.
- Modify `tests/pyunit/m2ndp/test_prepare_gapbs_pr_graph.py` for adoption and immutability tests.
- Create `scripts/freeze_pr_scaling_inputs.py` as the sole producer and validator of scoped PR-scaling manifests.
- Create `tests/pyunit/cross_system/test_freeze_pr_scaling_inputs.py` for manifest, live-hash, and terminal failure tests.
- Modify `scripts/run_cira_amu_m2ndp_scaling.py` to require the scoped manifest and record graph identities.
- Modify `tests/pyunit/cross_system/test_run_cira_amu_m2ndp_scaling.py` for scope and identity tests.
- Create `scripts/generate_pr_scaling_artifacts.py` to validate 16 points and atomically publish raw data, LaTeX, PDF, and SVG.
- Create `tests/pyunit/cross_system/test_generate_pr_scaling_artifacts.py` for exact ratios, deterministic artifacts, and rollback tests.
- Modify `scripts/run_cira_amu_m2ndp_breadth.py` to carry the breadth input's g20 graph identity into terminal state.
- Modify `tests/pyunit/cross_system/test_run_cira_amu_m2ndp_breadth.py` for that identity field.
- Modify `scripts/generate_cira_amu_m2ndp_comparison.py` to join distinct input roots by calibration and g20 graph identity.
- Modify `tests/pyunit/cross_system/test_generate_cira_amu_m2ndp_comparison.py` for permitted and rejected joins.
- Modify `docs/amu-gapbs-benchmark.md` with the formal command, evidence paths, raw-data schema, and publication commands.

### Task 1: Read-only adoption of existing g4 and g20 graphs

**Files:**
- Modify: `scripts/prepare_gapbs_pr_graph.py`
- Test: `tests/pyunit/m2ndp/test_prepare_gapbs_pr_graph.py`

- [ ] **Step 1: Write failing adoption tests**

Add tests that patch the endpoint hash contract to the fixture hash, call
`adopt_existing_graph()`, and compare graph bytes before and after:

```python
from unittest import mock

def test_adopt_existing_endpoint_graph_is_read_only_and_frozen(self):
    graph_prep = self.load_module()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        graph = root / "g4.sg"
        generator = root / "converter"
        output = root / "g4.manifest.json"
        write_serialized_graph(graph, nodes=16, edges=7)
        generator.write_bytes(b"fixed generator")
        os.chmod(generator, 0o755)
        before = graph.read_bytes()
        digest = artifacts.sha256_file(graph)
        with mock.patch.dict(
            graph_prep.profiles.SCALING_GRAPH_HASHES, {4: digest}, clear=True
        ):
            manifest = graph_prep.adopt_existing_graph(
                graph=graph, scale=4, generator=generator, output=output
            )
        self.assertEqual(graph.read_bytes(), before)
        self.assertEqual(manifest["graph_sha256"], digest)
        self.assertEqual(manifest["generator_command"], [
            str(generator.resolve()), "-g", "4", "-b", str(graph.resolve())
        ])
        self.assertEqual(output.stat().st_mode & 0o777, 0o444)

def test_adoption_rejects_nonendpoint_scale_and_hash_drift(self):
    graph_prep = self.load_module()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        graph = root / "g12.sg"
        generator = root / "converter"
        write_serialized_graph(graph, nodes=1 << 12, edges=2)
        generator.write_bytes(b"fixed generator")
        os.chmod(generator, 0o755)
        with self.assertRaisesRegex(
            graph_prep.GraphPreparationError, "g4 or g20"
        ):
            graph_prep.adopt_existing_graph(
                graph=graph, scale=12, generator=generator,
                output=root / "manifest.json"
            )
```

- [ ] **Step 2: Run the focused test and verify RED**

Run:

```bash
PYTHONPATH=. python3 -m unittest \
  tests.pyunit.m2ndp.test_prepare_gapbs_pr_graph -v
```

Expected: `AttributeError` for missing `adopt_existing_graph`.

- [ ] **Step 3: Implement endpoint adoption**

Generalize `write_graph_manifest()` to accept `profiles.SCALING_SCALES`, keep
`prepare_graph()` restricted to g12/g14, and add:

```python
def adopt_existing_graph(*, graph: Path, scale: int, generator: Path,
                         output: Path) -> dict:
    if scale not in (4, 20):
        raise GraphPreparationError("existing graph adoption supports g4 or g20")
    graph = Path(graph).resolve()
    generator = Path(generator).resolve()
    if not graph.is_file():
        raise GraphPreparationError(f"graph does not exist: {graph}")
    if not generator.is_file() or not os.access(generator, os.X_OK):
        raise GraphPreparationError(f"generator is not executable: {generator}")
    nodes, edges, _ = inspect_serialized_graph(graph)
    if nodes != 1 << scale:
        raise GraphPreparationError("node count does not match scale")
    expected = profiles.SCALING_GRAPH_HASHES[scale]
    if artifacts.sha256_file(graph) != expected:
        raise GraphPreparationError(f"g{scale} graph SHA-256 differs")
    command = [str(generator), "-g", str(scale), "-b", str(graph)]
    return write_graph_manifest(
        graph=graph, scale=scale, generator=generator,
        generator_command=command, num_nodes=nodes, directed_edges=edges,
        output=output,
    )
```

Extend the CLI with `--existing-graph` and `--output`. When
`--existing-graph` is present, require `--output` and call the function above;
otherwise require `--root` and retain the current g12/g14 generation path.

- [ ] **Step 4: Run adoption and existing profile tests**

Run:

```bash
PYTHONPATH=. python3 -m unittest \
  tests.pyunit.m2ndp.test_prepare_gapbs_pr_graph \
  tests.pyunit.m2ndp.test_gapbs_pr_experiment_profiles -v
```

Expected: all tests pass, including reuse and graph-size checks.

- [ ] **Step 5: Commit graph adoption**

```bash
git add scripts/prepare_gapbs_pr_graph.py \
  tests/pyunit/m2ndp/test_prepare_gapbs_pr_graph.py
git commit -m "feat: freeze existing GAPBS endpoint graphs"
```

### Task 2: Independent PR-scaling input freezer

**Files:**
- Create: `scripts/freeze_pr_scaling_inputs.py`
- Create: `tests/pyunit/cross_system/test_freeze_pr_scaling_inputs.py`

- [ ] **Step 1: Write failing scoped-manifest tests**

Use four mocked `FrozenGraphManifest` rows and assert the exact contract. The
test fixture is fully live-hashable:

```python
def setUp(self):
    self.temporary = tempfile.TemporaryDirectory()
    self.addCleanup(self.temporary.cleanup)
    self.root = Path(self.temporary.name)
    self.rows = []
    self.manifest_paths = []
    for scale in (4, 12, 14, 20):
        graph = self.root / f"g{scale}.sg"
        generator = self.root / f"converter-{scale}"
        manifest = self.root / f"g{scale}.manifest.json"
        graph.write_bytes(f"graph-{scale}".encode())
        generator.write_bytes(f"generator-{scale}".encode())
        manifest.write_text("{}\n")
        self.manifest_paths.append(manifest)
        self.rows.append(freeze.profiles.FrozenGraphManifest(
            schema=1, scale=scale, graph=str(graph.resolve()),
            graph_sha256=freeze._sha256_file(graph),
            generator=str(generator.resolve()),
            generator_sha256=freeze._sha256_file(generator),
            generator_command=(str(generator.resolve()), "-g", str(scale),
                               "-b", str(graph.resolve())),
            num_nodes=1 << scale, directed_edges=scale,
        ))

def valid_payload(self):
    with mock.patch.object(
        freeze.profiles, "load_scaling_graphs", return_value=self.rows
    ):
        return freeze.freeze_inputs(self.manifest_paths)

def test_freezer_emits_only_scoped_ordered_graphs(self):
    with mock.patch.object(
        freeze.profiles, "load_scaling_graphs", return_value=self.rows
    ):
        value = freeze.freeze_inputs(self.manifest_paths)
    self.assertEqual(value["schema"], 1)
    self.assertEqual(value["status"], "accepted")
    self.assertEqual(value["scope"], "pr_scaling")
    self.assertEqual(value["profile"], "pr-scaling-4thread-1us")
    self.assertEqual([row["scale"] for row in value["graphs"]], [4, 12, 14, 20])
    self.assertRegex(value["graph_set_sha256"], r"^[0-9a-f]{64}$")

def test_live_manifest_or_graph_hash_drift_is_rejected(self):
    value = self.valid_payload()
    Path(value["graphs"][0]["path"]).write_bytes(b"changed")
    with self.assertRaisesRegex(freeze.ScalingInputError, "SHA-256 changed"):
        freeze.validate_manifest(value)

def test_cli_writes_failed_input_without_accepted_output(self):
    output = self.root / "inputs.json"
    status = freeze.main([
        "--graph-manifest", str(self.root / "missing.json"),
        "--output", str(output),
    ])
    self.assertEqual(status, 2)
    self.assertFalse(output.exists())
    self.assertEqual(
        json.loads((self.root / "failed-input.json").read_text())["status"],
        "failed_input",
    )
```

- [ ] **Step 2: Run the new test and verify RED**

Run:

```bash
PYTHONPATH=. python3 -m unittest \
  tests.pyunit.cross_system.test_freeze_pr_scaling_inputs -v
```

Expected: import failure for `scripts.freeze_pr_scaling_inputs`.

- [ ] **Step 3: Implement canonical freezing and validation**

Create the focused module with these public boundaries:

```python
SCOPE = "pr_scaling"
PROFILE = profiles.SCALING_PROFILE_NAME

class ScalingInputError(RuntimeError):
    pass

def graph_set_sha256(graphs):
    identities = [
        {
            "scale": row["scale"],
            "sha256": row["sha256"],
            "manifest_sha256": row["manifest_sha256"],
        }
        for row in graphs
    ]
    return hashlib.sha256(contract.canonical_json(identities)).hexdigest()

def freeze_inputs(manifest_paths):
    paths = tuple(Path(path).resolve() for path in manifest_paths)
    if len(paths) != 4:
        raise ScalingInputError("exactly four graph manifests are required")
    try:
        frozen = profiles.load_scaling_graphs(paths)
    except profiles.ProfileError as error:
        raise ScalingInputError(str(error)) from error
    graphs = [graph_record(path, row) for path, row in zip(paths, frozen)]
    return {
        "schema": 1, "status": "accepted", "scope": SCOPE,
        "profile": PROFILE, "graphs": graphs,
        "graph_set_sha256": graph_set_sha256(graphs),
    }
```

`graph_record()` must include and live-verify `path`, `sha256`, `manifest`,
`manifest_sha256`, `num_nodes`, `directed_edges`, `generator`,
`generator_sha256`, and `generator_command`. `validate_manifest()` requires the
exact top-level keys, exact graph-entry keys, exact scale order, recomputed
graph-set digest, absolute resolved paths, and current file hashes.

Write accepted output with `O_CREAT | O_EXCL`, mode `0444`, `fsync`, and byte
comparison on reuse. On any error, remove accepted output and atomically write
`failed-input.json` with schema 1, status `failed_input`, and the deterministic
error string.

- [ ] **Step 4: Run freezer tests including the unchanged breadth failure**

Run:

```bash
PYTHONPATH=. python3 -m unittest \
  tests.pyunit.cross_system.test_freeze_pr_scaling_inputs \
  tests.pyunit.cross_system.test_freeze_cross_system_inputs -v
```

Expected: all tests pass; the existing missing paper record still produces
`failed_input`.

- [ ] **Step 5: Commit the scoped freezer**

```bash
git add scripts/freeze_pr_scaling_inputs.py \
  tests/pyunit/cross_system/test_freeze_pr_scaling_inputs.py
git commit -m "feat: freeze independent PR scaling inputs"
```

### Task 3: Enforce scoped inputs in the scaling runner

**Files:**
- Modify: `scripts/run_cira_amu_m2ndp_scaling.py`
- Modify: `tests/pyunit/cross_system/test_run_cira_amu_m2ndp_scaling.py`

- [ ] **Step 1: Change the fixture and add failing scope/identity tests**

Extend every fixture row with a live generator file, `generator`,
`generator_sha256`, and canonical `generator_command`; set top-level `scope`
and `profile`; then compute `graph_set_sha256` through
`freeze.graph_set_sha256(graphs)` and add:

```python
def test_runner_rejects_general_breadth_manifest(self):
    value = json.loads(self.inputs.read_text())
    value["scope"] = "scaling_and_breadth"
    self.inputs.write_text(json.dumps(value) + "\n")
    with self.assertRaisesRegex(scaling.ScalingError, "scope"):
        scaling.load_inputs(self.inputs)

def test_state_records_graph_set_and_g20_identity(self):
    inputs = scaling.load_inputs(self.inputs)
    state = scaling.new_state(self.options)
    self.assertEqual(state["graph_set_sha256"], inputs["graph_set_sha256"])
    self.assertEqual(
        state["g20_graph_sha256"],
        next(row for row in inputs["graphs"] if row["scale"] == 20)["sha256"],
    )
```

- [ ] **Step 2: Run the runner test and verify RED**

Run:

```bash
PYTHONPATH=. python3 -m unittest \
  tests.pyunit.cross_system.test_run_cira_amu_m2ndp_scaling -v
```

Expected: the old runner accepts the wrong scope or omits the new identity
fields.

- [ ] **Step 3: Delegate manifest validation to the scoped freezer**

Import `freeze_pr_scaling_inputs as scaling_inputs`, include that module in
`_code_sha256()`, and replace `load_inputs()` with:

```python
def load_inputs(path):
    try:
        return scaling_inputs.load_and_validate(path)
    except scaling_inputs.ScalingInputError as error:
        raise ScalingError(str(error)) from error
```

In `new_state()`, load the manifest once and add:

```python
"graph_set_sha256": inputs["graph_set_sha256"],
"g20_graph_sha256": next(
    row["sha256"] for row in inputs["graphs"] if row["scale"] == 20
),
```

Add both fields to the resume identity comparison. Preserve the current fixed
matrix, no-timeout semantics, mechanism checks, full rank-image comparison,
and Vanilla-before-accelerator ordering.

- [ ] **Step 4: Run scaling, freezer, and profile tests**

Run:

```bash
PYTHONPATH=. python3 -m unittest \
  tests.pyunit.cross_system.test_run_cira_amu_m2ndp_scaling \
  tests.pyunit.cross_system.test_freeze_pr_scaling_inputs \
  tests.pyunit.m2ndp.test_gapbs_pr_experiment_profiles -v
```

Expected: all tests pass.

- [ ] **Step 5: Commit runner enforcement**

```bash
git add scripts/run_cira_amu_m2ndp_scaling.py \
  tests/pyunit/cross_system/test_run_cira_amu_m2ndp_scaling.py
git commit -m "fix: bind scaling runs to scoped graph evidence"
```

### Task 4: Raw-data and multi-plot scaling publisher

**Files:**
- Create: `scripts/generate_pr_scaling_artifacts.py`
- Create: `tests/pyunit/cross_system/test_generate_pr_scaling_artifacts.py`

- [ ] **Step 1: Write failing validation and publication tests**

Create a complete 16-point fixture with `sim_ticks` for Vanilla/AMU/CIRA and
`ndpsim_measured_cycles` for M2NDP, then add:

```python
def sha(label):
    return hashlib.sha256(label.encode()).hexdigest()

def setUp(self):
    self.temporary = tempfile.TemporaryDirectory()
    self.addCleanup(self.temporary.cleanup)
    self.root = Path(self.temporary.name)
    self.complete = self.root / "complete.json"
    points = {}
    for scale in (4, 12, 14, 20):
        for index, system in enumerate(("vanilla", "amu", "cira", "m2ndp")):
            divisor = Decimal(1 if system == "vanilla" else index + 1)
            latency = Decimal(scale) / divisor
            mechanism = {"verification": "pass"}
            if system == "m2ndp":
                mechanism["ndpsim_measured_cycles"] = str(scale * 100)
            else:
                mechanism["sim_ticks"] = str(int(latency * Decimal(10**12)))
            points[f"g{scale}:{system}"] = {
                "scale": scale, "system": system, "status": "passed",
                "latency": "1us", "full_e2e": True,
                "latency_seconds": str(latency), "speedup": str(divisor),
                "output_elements": 1 << scale, "mechanism": mechanism,
                "outputs": {
                    "rank": sha(f"rank-{scale}-{system}"),
                    "summary": sha(f"summary-{scale}-{system}"),
                },
            }
    self.complete.write_text(json.dumps({
        "schema": 1, "status": "complete",
        "profile": "pr-scaling-4thread-1us",
        "graph_set_sha256": sha("graph-set"),
        "g20_graph_sha256": sha("g20"),
        "inputs_sha256": sha("inputs"),
        "calibration_sha256": sha("calibration"),
        "code_sha256": sha("code"), "gem5_sha256": sha("gem5"),
        "config_sha256": sha("config"), "points": points,
    }, sort_keys=True) + "\n")

def test_load_recomputes_speedup_and_native_counts(self):
    data = artifacts.load_data(self.complete)
    rows = {(row.scale, row.system): row for row in data.rows}
    self.assertEqual(rows[(20, "amu")].speedup, Decimal("2"))
    self.assertEqual(rows[(20, "amu")].native_time_kind, "gem5_ticks")
    self.assertEqual(rows[(20, "m2ndp")].native_time_kind, "ndpsim_cycles")

def test_publish_emits_raw_data_table_and_four_plot_families(self):
    result = artifacts.publish(
        artifacts.load_data(self.complete), self.root / "publication"
    )
    expected = {
        "pr-scaling-raw.json", "pr-scaling-raw.csv",
        "pr-scaling-evidence.json", "pr-scaling-table.tex",
        "fig/pr-scaling-speedup.pdf", "fig/pr-scaling-speedup.svg",
        "fig/pr-scaling-latency.pdf", "fig/pr-scaling-latency.svg",
        "fig/pr-scaling-grouped.pdf", "fig/pr-scaling-grouped.svg",
        "fig/pr-scaling-heatmap.pdf", "fig/pr-scaling-heatmap.svg",
    }
    self.assertEqual(set(result), expected)

def test_publish_rolls_back_every_file_after_injected_failure(self):
    output = self.root / "publication"
    output.mkdir()
    old = output / "pr-scaling-raw.csv"
    old.write_text("old\n")
    with self.assertRaisesRegex(artifacts.ArtifactError, "injected"):
        artifacts.publish(
            artifacts.load_data(self.complete), output,
            fail_after_promotions=3,
        )
    self.assertEqual(old.read_text(), "old\n")
```

Add explicit rejection and determinism coverage:

```python
def test_rejects_incomplete_or_unverified_matrix(self):
    original = json.loads(self.complete.read_text())
    missing = copy.deepcopy(original)
    missing["points"].pop("g20:m2ndp")
    self.complete.write_text(json.dumps(missing))
    with self.assertRaisesRegex(artifacts.ArtifactError, "16 points"):
        artifacts.load_data(self.complete)
    unverified = copy.deepcopy(original)
    unverified["points"]["g14:cira"]["mechanism"]["verification"] = "fail"
    self.complete.write_text(json.dumps(unverified))
    with self.assertRaisesRegex(artifacts.ArtifactError, "verification"):
        artifacts.load_data(self.complete)

def test_rejects_ratio_time_and_native_counter_drift(self):
    original = json.loads(self.complete.read_text())
    cases = (
        ("speedup", "99", "stored speedup"),
        ("latency_seconds", "0", "positive"),
    )
    for field, value, message in cases:
        changed = copy.deepcopy(original)
        changed["points"]["g12:amu"][field] = value
        self.complete.write_text(json.dumps(changed))
        with self.subTest(field=field):
            with self.assertRaisesRegex(artifacts.ArtifactError, message):
                artifacts.load_data(self.complete)
    changed = copy.deepcopy(original)
    changed["points"]["g12:amu"]["mechanism"]["sim_ticks"] = "1.5"
    self.complete.write_text(json.dumps(changed))
    with self.assertRaisesRegex(artifacts.ArtifactError, "positive integer"):
        artifacts.load_data(self.complete)

def test_publication_bytes_are_deterministic(self):
    data = artifacts.load_data(self.complete)
    first = artifacts.publish(data, self.root / "first")
    second = artifacts.publish(data, self.root / "second")
    self.assertEqual(
        {name: row["sha256"] for name, row in first.items()},
        {name: row["sha256"] for name, row in second.items()},
    )
```

- [ ] **Step 2: Run the publisher test and verify RED**

Run:

```bash
PYTHONPATH=. python3 -m unittest \
  tests.pyunit.cross_system.test_generate_pr_scaling_artifacts -v
```

Expected: import failure for `scripts.generate_pr_scaling_artifacts`.

- [ ] **Step 3: Implement exact raw rows and validation**

Define immutable rows and validate the terminal evidence:

```python
@dataclasses.dataclass(frozen=True)
class RawRow:
    scale: int
    system: str
    latency_seconds: Decimal
    speedup: Decimal
    native_time_kind: str
    native_time_count: int
    output_elements: int
    outputs: dict
    mechanism: dict

def native_count(system, mechanism):
    field, kind = (
        ("ndpsim_measured_cycles", "ndpsim_cycles")
        if system == "m2ndp" else ("sim_ticks", "gem5_ticks")
    )
    value = mechanism.get(field)
    if not isinstance(value, str) or not value.isdigit() or int(value) <= 0:
        raise ArtifactError(f"{system} {field} must be a positive integer")
    return kind, int(value)
```

Require schema 1, status `complete`, the exact profile, exact graph-set and g20
hashes, exactly 16 passed points, verification `pass`, nonempty output hashes,
and output count `2^scale`. Recompute each speedup from the same-scale Vanilla
`Decimal` latency and reject a different stored ratio.

- [ ] **Step 4: Implement deterministic CSV/JSON/LaTeX and four plots**

Use sorted scale-major/system-major rows. The CSV columns are:

```python
FIELDS = (
    "scale", "system", "latency_seconds", "speedup",
    "native_time_kind", "native_time_count", "output_elements",
    "verification", "rank_sha256", "summary_sha256",
    "graph_set_sha256", "g20_graph_sha256", "inputs_sha256",
    "calibration_sha256", "code_sha256", "gem5_sha256", "config_sha256",
    "outputs_json", "mechanism_json",
)
```

`pr-scaling-raw.json` stores the complete source evidence plus normalized raw
rows. `pr-scaling-table.tex` reports latency and speedup. Matplotlib Agg renders
speedup lines, log-latency lines, grouped speedup bars, and a labeled heatmap;
set fixed fonts, colors, `svg.hashsalt`, and PDF metadata dates to `None`.

Stage all 12 artifacts in a sibling temporary directory, hash every artifact
except the manifest, write `pr-scaling-evidence.json`, then promote with backup
and rollback using `os.replace()` and directory `fsync`, matching the existing
combined publisher's atomic behavior.

- [ ] **Step 5: Run publisher tests twice for deterministic output**

Run:

```bash
PYTHONPATH=. python3 -m unittest \
  tests.pyunit.cross_system.test_generate_pr_scaling_artifacts -v
```

Expected: all tests pass and both publication roots have identical artifact
SHA-256 values.

- [ ] **Step 6: Commit the plot family**

```bash
git add scripts/generate_pr_scaling_artifacts.py \
  tests/pyunit/cross_system/test_generate_pr_scaling_artifacts.py
git commit -m "feat: publish PR scaling raw data and plots"
```

### Task 5: Preserve the final cross-system join contract

**Files:**
- Modify: `scripts/run_cira_amu_m2ndp_breadth.py`
- Modify: `tests/pyunit/cross_system/test_run_cira_amu_m2ndp_breadth.py`
- Modify: `scripts/generate_cira_amu_m2ndp_comparison.py`
- Modify: `tests/pyunit/cross_system/test_generate_cira_amu_m2ndp_comparison.py`

- [ ] **Step 1: Write failing breadth and join tests**

Update `new_state()` calls to supply an exact g20 hash and add:

```python
def test_breadth_state_records_g20_graph_identity(self):
    state = breadth.new_state(
        identity(), specs(), g20_graph_sha256=sha("g20")
    )
    self.assertEqual(state["g20_graph_sha256"], sha("g20"))
```

In the combined publisher fixture, make the scaling and breadth input hashes
different while keeping calibration and g20 equal. Assert `load_data()` passes;
then change only breadth g20 and assert:

```python
with self.assertRaisesRegex(comparison.ComparisonError, "g20 graph"):
    comparison.load_data(self.scaling, self.breadth)
```

- [ ] **Step 2: Run breadth and publisher tests and verify RED**

Run:

```bash
PYTHONPATH=. python3 -m unittest \
  tests.pyunit.cross_system.test_run_cira_amu_m2ndp_breadth \
  tests.pyunit.cross_system.test_generate_cira_amu_m2ndp_comparison -v
```

Expected: `new_state()` lacks the g20 field and the publisher still rejects
distinct input-manifest hashes.

- [ ] **Step 3: Bind g20 into breadth terminal evidence**

Change the signature to:

```python
def new_state(identity, workload_specs, *, g20_graph_sha256):
```

Validate the digest with the existing SHA-256 regex and store it at top level.
During preflight, extract g20 from `inputs["graphs"]`, require the breadth
`workloads["pr_spmv"]["input_sha256"]` to match it, and return the digest with
the identity/specifications. Pass it to `new_state()` before any functional or
timing stage begins.

- [ ] **Step 4: Join independent roots by common facts**

Replace the old input-hash equality condition in `load_data()` with:

```python
if identity.get("calibration_manifest_sha256") != scaling_calibration:
    raise ComparisonError("scaling and breadth calibration identities differ")
if breadth.get("g20_graph_sha256") != scaling.get("g20_graph_sha256"):
    raise ComparisonError("scaling and breadth g20 graph identities differ")
```

Keep each evidence file's own input hash in the publication provenance. Do not
weaken matrix completeness, functional records, CI, or output-hash gates.

- [ ] **Step 5: Run the focused cross-system suite**

Run:

```bash
PYTHONPATH=. python3 -m unittest discover \
  -s tests/pyunit/cross_system -p 'test_*.py' -v
```

Expected: all cross-system tests pass, including the unchanged breadth missing
input failure.

- [ ] **Step 6: Commit the join contract**

```bash
git add scripts/run_cira_amu_m2ndp_breadth.py \
  scripts/generate_cira_amu_m2ndp_comparison.py \
  tests/pyunit/cross_system/test_run_cira_amu_m2ndp_breadth.py \
  tests/pyunit/cross_system/test_generate_cira_amu_m2ndp_comparison.py
git commit -m "fix: join independent evidence roots by common identity"
```

### Task 6: Full verification, immutable inputs, push, and background run

**Files:**
- Modify: `docs/amu-gapbs-benchmark.md`
- Create outside Git: `/mnt/disk0/gem5-CXL-eval/pr-scaling-<commit>/`

- [ ] **Step 1: Document the exact evidence and publication flow**

Add a section naming the profile, four graph hashes/manifests, calibration
path/hash, 16-point pass rule, raw fields, plot filenames, background unit,
and the statement that breadth remains `failed_input` until real paper inputs
are provided.

- [ ] **Step 2: Run the complete Python suite**

Run:

```bash
PYTHONPATH=. python3 -m unittest discover -s tests/pyunit/amu -p 'test_*.py' -v
PYTHONPATH=. python3 -m unittest discover -s tests/pyunit/m2ndp -p 'test_*.py' -v
PYTHONPATH=. python3 -m unittest discover -s tests/pyunit/cross_system -p 'test_*.py' -v
```

Expected: all tests pass with no failure or error.

Do not run discovery from `tests/pyunit`: its `test_run.py` is a gem5 TestLib
registration entrypoint and requires TestLib initialization.

- [ ] **Step 3: Run static compile gates for touched Python**

Run:

```bash
python3 -m py_compile \
  scripts/prepare_gapbs_pr_graph.py \
  scripts/freeze_pr_scaling_inputs.py \
  scripts/run_cira_amu_m2ndp_scaling.py \
  scripts/generate_pr_scaling_artifacts.py \
  scripts/run_cira_amu_m2ndp_breadth.py \
  scripts/generate_cira_amu_m2ndp_comparison.py
```

Expected: exit status 0 and no output.

- [ ] **Step 4: Commit documentation and verify a clean tree**

```bash
git add docs/amu-gapbs-benchmark.md
git commit -m "docs: describe formal PR scaling artifacts"
git status --short
```

Expected: no status output.

- [ ] **Step 5: Push the corresponding branch**

```bash
git push origin m2ndp-g20-pr-spmv
git rev-parse HEAD
git rev-parse origin/m2ndp-g20-pr-spmv
```

Expected: the two full commit IDs are identical.

- [ ] **Step 6: Freeze the existing g4 and g20 endpoints**

```bash
SCALING_SHA=$(git rev-parse --short=12 HEAD)
SCALING_ROOT=/mnt/disk0/gem5-CXL-eval/pr-scaling-${SCALING_SHA}
CONVERTER=/home/victoryang00/gem5-CXL/.worktrees/gapbs-latency-table/m5out/gapbs_baseline_bins_latency_g20/src/gapbs/converter
mkdir -p "${SCALING_ROOT}/graphs"
python3 scripts/prepare_gapbs_pr_graph.py --scale 4 \
  --existing-graph /home/victoryang00/gem5-CXL/.worktrees/gapbs-latency-table/m5out/gapbs_graphs/g4.sg \
  --generator "${CONVERTER}" \
  --output "${SCALING_ROOT}/graphs/g4.manifest.json"
python3 scripts/prepare_gapbs_pr_graph.py --scale 20 \
  --existing-graph /home/victoryang00/gem5-CXL/.worktrees/gapbs-latency-table/m5out/gapbs_graphs/g20.sg \
  --generator "${CONVERTER}" \
  --output "${SCALING_ROOT}/graphs/g20.manifest.json"
```

Expected: each command prints schema 1 with the formal endpoint hash, correct
node count, and the new read-only manifest path.

- [ ] **Step 7: Freeze the four-graph scoped input**

```bash
SCALING_SHA=$(git rev-parse --short=12 HEAD)
SCALING_ROOT=/mnt/disk0/gem5-CXL-eval/pr-scaling-${SCALING_SHA}
python3 scripts/freeze_pr_scaling_inputs.py \
  --graph-manifest "${SCALING_ROOT}/graphs/g4.manifest.json" \
  --graph-manifest /mnt/disk0/gem5-CXL-g14-eval/graphs/g12.manifest.json \
  --graph-manifest /mnt/disk0/gem5-CXL-g14-eval/graphs/g14.manifest.json \
  --graph-manifest "${SCALING_ROOT}/graphs/g20.manifest.json" \
  --output "${SCALING_ROOT}/inputs.json"
```

Expected: `PR_SCALING_INPUTS_ACCEPTED` and no `failed-input.json`.

- [ ] **Step 8: Start the unlimited formal matrix as a background service**

```bash
SCALING_SHA=$(git rev-parse --short=12 HEAD)
SCALING_ROOT=/mnt/disk0/gem5-CXL-eval/pr-scaling-${SCALING_SHA}
systemd-run --unit=cira-amu-m2ndp-pr-scaling-formal --collect \
  --description='Formal four-thread all-CXL 1us PR scaling' \
  --property=WorkingDirectory=/home/victoryang00/gem5-CXL/.worktrees/m2ndp-g20-pr-spmv \
  /usr/bin/python3 scripts/run_cira_amu_m2ndp_scaling.py \
  --inputs "${SCALING_ROOT}/inputs.json" \
  --calibration /mnt/disk0/gem5-CXL-g14-eval/amu-paper-full-2c07da6b73.calibration.json \
  --root "${SCALING_ROOT}/run" \
  --gem5 /mnt/disk0/gem5-CXL-g14-eval/amu-paper-full-2c07da6b73/inputs/gem5 \
  --config /home/victoryang00/gem5-CXL/.worktrees/m2ndp-g20-pr-spmv/configs/example/gem5_library/x86-gapbs-amu-se.py \
  --cxlmemuring /home/victoryang00/CXLMemUring \
  --m2ndp-root /mnt/disk0/M2NDP-public \
  --variants-build-root "${SCALING_ROOT}/builds" \
  --timeout 0
```

Expected: transient service starts. `--timeout 0` means no per-point wall-clock
limit and no periodic simulator checkpoint is enabled.

- [ ] **Step 9: Verify live identity and first-point progress**

Run:

```bash
SCALING_SHA=$(git rev-parse --short=12 HEAD)
SCALING_ROOT=/mnt/disk0/gem5-CXL-eval/pr-scaling-${SCALING_SHA}
systemctl status cira-amu-m2ndp-pr-scaling-formal.service --no-pager
journalctl -u cira-amu-m2ndp-pr-scaling-formal.service -n 100 --no-pager
python3 -m json.tool "${SCALING_ROOT}/run/state.json"
```

Expected: service active or successfully complete, the state carries exact
input/calibration/code/gem5/config/graph-set/g20 hashes, and only fully passed
points are marked `passed`.

- [ ] **Step 10: Publish only after the terminal 16/16 gate**

After `run/complete.json` exists and the service exits successfully, run:

```bash
SCALING_SHA=$(git rev-parse --short=12 HEAD)
SCALING_ROOT=/mnt/disk0/gem5-CXL-eval/pr-scaling-${SCALING_SHA}
python3 scripts/generate_pr_scaling_artifacts.py \
  --scaling "${SCALING_ROOT}/run/complete.json" \
  --output-root "${SCALING_ROOT}/publication"
sha256sum "${SCALING_ROOT}/publication"/pr-scaling-raw.* \
  "${SCALING_ROOT}/publication"/pr-scaling-table.tex \
  "${SCALING_ROOT}/publication"/fig/*
```

Expected: 16 raw rows, 12 hash-bound publication artifacts, and no combined
breadth figure unless the independent breadth root later passes its own gate.
