# GAPBS g20 AMU/CIRA/M2NDP End-to-End Table Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a fail-closed generator that replaces the paper's GAPBS table with a formal g20 AMU/CIRA/M2NDP end-to-end comparison plus a separately labeled scale-4 latency-sensitivity panel.

**Architecture:** One importable Python generator owns evidence loading, validation, calculation, rendering, and atomic output replacement. It reuses the existing M2NDP and matched-variant validators, recomputes every displayed value from explicit artifact roots, and writes the LaTeX table last so invalid evidence never changes the paper table.

**Tech Stack:** Python 3 standard library (`argparse`, `csv`, `decimal`, `hashlib`, `json`, `math`, `os`, `pathlib`, `tempfile`, `unittest`), existing gem5/M2NDP evidence parsers, LaTeX/booktabs.

---

## Scope and File Map

Create:

- `scripts/generate_gapbs_g20_e2e_table.py`: evidence model, formal g20
  validator, sensitivity validator, calculations, renderer, atomic publisher,
  and CLI.
- `tests/pyunit/m2ndp/test_generate_gapbs_g20_e2e_table.py`: synthetic
  evidence fixtures and all success/failure tests.

Modify:

- `docs/amu-gapbs-benchmark.md`: add the exact publication command,
  generated artifacts, and fail-closed proof boundary.

Generate but do not add to the gem5 experiment commit:

- `/home/victoryang00/gem5-CXL/6472666535e6f359942ddac6/gapbs-vtune-cxl-table.tex`;
- `/home/victoryang00/gem5-CXL/6472666535e6f359942ddac6/gapbs-g20-e2e-results.csv`; and
- `/home/victoryang00/gem5-CXL/6472666535e6f359942ddac6/gapbs-g20-e2e-table-evidence.json`.

Do not modify the active background-run directories except for files written
by their existing orchestrators.

### Task 1: Validate and Calculate the Formal g20 Rows

**Files:**

- Create: `scripts/generate_gapbs_g20_e2e_table.py`
- Create: `tests/pyunit/m2ndp/test_generate_gapbs_g20_e2e_table.py`

- [ ] **Step 1: Write the failing formal-evidence tests**

Create a `unittest.TestCase` with a `write_formal_bundle(root)` fixture that
writes:

- `m2ndp/status.json` with every
  `run_m2ndp_g20_pr_spmv.STAGES` entry set to `passed`;
- `m2ndp/summary.csv` with the exact fields emitted by
  `m2ndp_results.build_summary`;
- `m2ndp/manifest.json` with hashes for the summary, reference raw file,
  FuncSim dump, calibration, trace, and logs;
- `m2ndp/gem5/run/summary.csv` with one valid `pr_spmv/cxl_vanilla` row;
- 4 MiB identical raw files for the baseline, AMU, CIRA, and FuncSim;
- `variants/build/manifest.json` with the fixed-20 floating-point contract;
  and
- separate `variants/amu/run` and `variants/cira/run` summary/evidence
  bundles whose embedded rows pass
  `run_gapbs_matched_pr_spmv_variants.validate_row`.

The initial tests must be:

```python
def test_formal_rows_recompute_absolute_time_and_speedup(self):
    with tempfile.TemporaryDirectory() as tmp:
        roots = write_formal_bundle(Path(tmp))
        rows, evidence = table.load_formal_rows(
            roots.m2ndp, roots.variants
        )
    self.assertEqual(
        [row.system for row in rows],
        ["Vanilla CXL", "AMU", "CIRA", "M2NDP"],
    )
    self.assertEqual(rows[0].latency_seconds, Decimal("2"))
    self.assertEqual(rows[1].speedup, Decimal("0.5"))
    self.assertEqual(rows[2].speedup, Decimal("1.25"))
    self.assertEqual(rows[3].latency_seconds, Decimal("0.5"))
    self.assertEqual(rows[3].speedup, Decimal("4"))
    self.assertEqual(evidence["graph_sha256"], table.G20_SHA256)


def test_formal_rows_reject_one_bit_difference(self):
    with tempfile.TemporaryDirectory() as tmp:
        roots = write_formal_bundle(Path(tmp))
        raw = roots.variants / "cira.raw"
        data = bytearray(raw.read_bytes())
        data[-1] ^= 1
        raw.write_bytes(data)
        refresh_variant_evidence_hash(roots, "cira")
        with self.assertRaisesRegex(
            table.TableEvidenceError, "raw float32"
        ):
            table.load_formal_rows(roots.m2ndp, roots.variants)


def test_formal_rows_reject_running_or_failed_stage(self):
    with tempfile.TemporaryDirectory() as tmp:
        roots = write_formal_bundle(Path(tmp))
        status = read_json(roots.m2ndp / "status.json")
        status["stages"]["ndpsim"]["status"] = "running"
        write_json(roots.m2ndp / "status.json", status)
        with self.assertRaisesRegex(
            table.TableEvidenceError, "ndpsim.*passed"
        ):
            table.load_formal_rows(roots.m2ndp, roots.variants)
```

Add subtests that mutate one field at a time and require rejection for:

- wrong graph SHA-256;
- `iterations != 20`, `trials != 2`, or `measured_trial != 1`;
- non-Timing CPU, non-two-core execution, or non-CXL placement;
- link delay other than `1us` or `config.ini` other than
  `delay=1000000`;
- missing `verification=pass`;
- AMU issued/completed mismatch;
- zero CIRA descriptors or completions;
- `funcsim_strict != pass`, wrong compared count, or mismatched raw hash;
- M2NDP manifest hash drift;
- failed calibration or residual greater than one link clock; and
- stored M2NDP seconds/speedup differing from recomputation.

- [ ] **Step 2: Run the new test module and verify the import failure**

Run:

```bash
python3 -m unittest \
  tests.pyunit.m2ndp.test_generate_gapbs_g20_e2e_table -v
```

Expected: FAIL because
`scripts/generate_gapbs_g20_e2e_table.py` does not exist.

- [ ] **Step 3: Add the formal evidence model and strict scalar parsers**

Start the generator with these public values and types:

```python
G20_SHA256 = (
    "ce900a7147a073835a7450e8f1afedf9f13db6833652bf2f"
    "9647819be26bedb3"
)
G20_WORDS = 1 << 20
TICKS_PER_SECOND = Decimal(10**12)


class TableEvidenceError(RuntimeError):
    pass


@dataclasses.dataclass(frozen=True)
class MainRow:
    system: str
    latency_seconds: Decimal
    speedup: Decimal
    correctness: str


def require_decimal(mapping, field, context):
    try:
        value = Decimal(str(mapping[field]))
    except (KeyError, decimal.InvalidOperation) as error:
        raise TableEvidenceError(
            f"{context}: invalid {field}"
        ) from error
    if not value.is_finite() or value <= 0:
        raise TableEvidenceError(
            f"{context}: {field} must be finite and positive"
        )
    return value


def require_one_csv(path, context):
    with Path(path).open(newline="", encoding="utf-8") as stream:
        rows = list(csv.DictReader(stream))
    if len(rows) != 1:
        raise TableEvidenceError(
            f"{context}: expected exactly one row, got {len(rows)}"
        )
    return rows[0]
```

Use `m2ndp_artifacts.sha256_file` for files and
`run_m2ndp_g20_pr_spmv.hash_path` for directory artifacts. Do not create a
second hash implementation.

- [ ] **Step 4: Implement `load_formal_rows`**

Implement this interface:

```python
def load_formal_rows(m2ndp_root, variants_root):
    m2ndp_root = Path(m2ndp_root).resolve()
    variants_root = Path(variants_root).resolve()
    status = load_json(m2ndp_root / "status.json", "M2NDP status")
    require_all_m2ndp_stages_passed(status)
    manifest = load_json(
        m2ndp_root / "manifest.json", "M2NDP manifest"
    )
    verify_m2ndp_manifest(m2ndp_root, manifest)

    m2ndp = require_one_csv(
        m2ndp_root / "summary.csv", "M2NDP summary"
    )
    baseline = results.parse_gem5_summary(
        m2ndp_root / "gem5/run/summary.csv"
    )
    require_config_delay(
        m2ndp_root
        / "gem5/run/pr_spmv/cxl_vanilla/config.ini",
        expected="1000000",
    )
    amu = load_variant(variants_root, "amu")
    cira = load_variant(variants_root, "cira")
    require_shared_raw_bits(
        m2ndp_root, manifest, amu, cira, expected_words=G20_WORDS
    )

    baseline_seconds = (
        Decimal(baseline.sim_ticks) / TICKS_PER_SECOND
    )
    amu_seconds = (
        Decimal(amu["row"]["sim_ticks"]) / TICKS_PER_SECOND
    )
    cira_seconds = (
        Decimal(cira["row"]["sim_ticks"]) / TICKS_PER_SECOND
    )
    m2ndp_seconds = (
        require_decimal(
            m2ndp, "ndpsim_measured_cycles", "M2NDP"
        )
        * require_decimal(
            m2ndp, "ndpsim_core_period_seconds", "M2NDP"
        )
    )
    rows = [
        MainRow("Vanilla CXL", baseline_seconds, Decimal(1), "PASS"),
        MainRow(
            "AMU",
            amu_seconds,
            baseline_seconds / amu_seconds,
            "Bit-exact PASS",
        ),
        MainRow(
            "CIRA",
            cira_seconds,
            baseline_seconds / cira_seconds,
            "Bit-exact PASS",
        ),
        MainRow(
            "M2NDP",
            m2ndp_seconds,
            baseline_seconds / m2ndp_seconds,
            "FuncSim bit-exact PASS",
        ),
    ]
    require_stored_m2ndp_values(m2ndp, rows[0], rows[3])
    return rows, build_formal_evidence(
        m2ndp_root, variants_root, manifest, rows
    )
```

`load_variant` must load `variants/<kind>/run/evidence.json`, require exactly
one embedded run with the requested kind, call
`matched_runner.validate_row(row, kind, smoke_test=False)`, call
`matched_runner.validate_config_delay(Path(row["run_dir"]) / "config.ini")`,
verify the binary/reference hashes recorded in evidence, and verify that the
shared build manifest still has the hash stored in each evidence file.
Translate `matched_runner.VariantRunError` and
`m2ndp_artifacts.EvidenceError` into `TableEvidenceError` at the public
loader boundary so the CLI has one fail-closed error type.

`verify_m2ndp_manifest` must bind the final manifest to the pinned graph,
two-core fixed-20 contract, pinned upstream commit, and the known artifact
paths. It must recompute every entry in `artifact_sha256` that the table uses.
The exact result-root mapping is:

```python
{
    "reference_raw": m2ndp_root / "reference/scores.raw",
    "funcsim_dump": m2ndp_root / "funcsim/scores.u32",
    "calibration": m2ndp_root / "calibration/calibration.json",
    "gem5_log": (
        m2ndp_root / "gem5/run/pr_spmv/cxl_vanilla/gem5.log"
    ),
    "funcsim_log": m2ndp_root / "logs/funcsim.log",
    "ndpsim_log": m2ndp_root / "logs/ndpsim.log",
    "summary": m2ndp_root / "summary.csv",
}
```

Read `calibration/calibration.json` and require `passed`, positive finite
periods, exact `abs(measured_ns - target_ns) == residual_ns`, and
`residual_ns <= link_period_ns`.

- [ ] **Step 5: Run formal-evidence tests and the existing gate suites**

Run:

```bash
python3 -m unittest \
  tests.pyunit.m2ndp.test_generate_gapbs_g20_e2e_table \
  tests.pyunit.m2ndp.test_m2ndp_results \
  tests.pyunit.m2ndp.test_run_gapbs_matched_pr_spmv_variants -v
```

Expected: all tests PASS.

- [ ] **Step 6: Commit the formal validator**

```bash
git add \
  scripts/generate_gapbs_g20_e2e_table.py \
  tests/pyunit/m2ndp/test_generate_gapbs_g20_e2e_table.py
git commit -m "gapbs: validate formal g20 table evidence"
```

### Task 2: Validate and Recompute the Scale-4 Sensitivity Panel

**Files:**

- Modify: `scripts/generate_gapbs_g20_e2e_table.py`
- Modify: `tests/pyunit/m2ndp/test_generate_gapbs_g20_e2e_table.py`

- [ ] **Step 1: Write failing sensitivity tests**

Add a fixture with 48 rows: four latencies, four workloads, and exactly one
baseline/AMU/CIRA row per pair. Each row gets a real
`run_dir/config.ini` beneath the supplied run root.

```python
def test_sensitivity_recomputes_rows_and_per_latency_geomean(self):
    with tempfile.TemporaryDirectory() as tmp:
        csv_path, run_root = write_sensitivity_bundle(Path(tmp))
        values = table.load_sensitivity(csv_path, run_root)
    self.assertEqual(set(values), set(table.LATENCIES))
    self.assertEqual(
        values["1us"]["pr"]["AMU"], Decimal("0.5")
    )
    self.assertEqual(
        values["1us"]["pr"]["CIRA"], Decimal("1.25")
    )
    self.assertGreater(values["1us"]["Geo."]["CIRA"], 0)


def test_sensitivity_rejects_duplicate_row(self):
    with tempfile.TemporaryDirectory() as tmp:
        csv_path, run_root = write_sensitivity_bundle(Path(tmp))
        append_first_csv_row(csv_path)
        with self.assertRaisesRegex(
            table.TableEvidenceError, "duplicate"
        ):
            table.load_sensitivity(csv_path, run_root)


def test_sensitivity_rejects_wrong_config_delay(self):
    with tempfile.TemporaryDirectory() as tmp:
        csv_path, run_root = write_sensitivity_bundle(Path(tmp))
        config = next(run_root.rglob("config.ini"))
        config.write_text("delay=1000000\n", encoding="utf-8")
        with self.assertRaisesRegex(
            table.TableEvidenceError, "delay"
        ):
            table.load_sensitivity(csv_path, run_root)
```

Add subtests for missing combinations, `status != ok`,
`verification != pass`, nonpositive ticks, a `run_dir` escaping the explicit
run root, and a stored speedup outside the approved tolerance.

- [ ] **Step 2: Run the focused tests and verify failure**

Run:

```bash
python3 -m unittest \
  tests.pyunit.m2ndp.test_generate_gapbs_g20_e2e_table \
  -k sensitivity -v
```

Expected: FAIL because `load_sensitivity` is absent.

- [ ] **Step 3: Implement the sensitivity contract**

Add these constants and entry point:

```python
LATENCIES = ("200ns", "500ns", "1us", "2us")
WORKLOADS = ("bfs", "bc", "pr", "sssp")
DELAY_TICKS = {
    "200ns": "200000",
    "500ns": "500000",
    "1us": "1000000",
    "2us": "2000000",
}
SPEEDUP_TOLERANCE = Decimal("1e-12")


def close_speedup(stored, recomputed):
    difference = abs(stored - recomputed)
    limit = max(
        SPEEDUP_TOLERANCE,
        abs(recomputed) * SPEEDUP_TOLERANCE,
    )
    return difference <= limit


def load_sensitivity(csv_path, run_root):
    rows = read_csv(csv_path, "latency sensitivity")
    run_root = Path(run_root).resolve()
    by_key = {}
    for row in rows:
        key = sensitivity_key(row)
        if key in by_key:
            raise TableEvidenceError(f"duplicate sensitivity row: {key}")
        by_key[key] = validate_sensitivity_row(row, run_root)
    require_exact_sensitivity_keys(by_key)
    return calculate_sensitivity(by_key)
```

`sensitivity_key` maps labels to `Baseline`, `AMU`, or `CIRA` and rejects
unknown configurations. `validate_sensitivity_row` resolves
`run_root / row["run_dir"]`, proves it remains inside `run_root`, requires the
expected `delay=` value from that run's `config.ini`, and validates positive
finite integral `sim_ticks`.

For each workload/latency, recompute:

```python
amu_speedup = baseline_ticks / amu_ticks
cira_speedup = baseline_ticks / cira_ticks
```

Check the stored `speedup_vs_cxl` with `close_speedup`. Compute each
within-latency four-workload geometric mean as:

```python
Decimal(
    str(math.exp(
        sum(math.log(float(value)) for value in values)
        / len(values)
    ))
)
```

Never include panel-(a) values in this function.

- [ ] **Step 4: Run sensitivity and full generator tests**

Run:

```bash
python3 -m unittest \
  tests.pyunit.m2ndp.test_generate_gapbs_g20_e2e_table -v
```

Expected: all tests PASS.

- [ ] **Step 5: Commit the sensitivity validator**

```bash
git add \
  scripts/generate_gapbs_g20_e2e_table.py \
  tests/pyunit/m2ndp/test_generate_gapbs_g20_e2e_table.py
git commit -m "gapbs: validate latency sensitivity evidence"
```

### Task 3: Render and Atomically Publish the Three Artifacts

**Files:**

- Modify: `scripts/generate_gapbs_g20_e2e_table.py`
- Modify: `tests/pyunit/m2ndp/test_generate_gapbs_g20_e2e_table.py`

- [ ] **Step 1: Write failing renderer and preservation tests**

```python
def test_render_latex_has_separate_panels_and_no_cross_geomean(self):
    latex = table.render_latex(
        valid_main_rows(),
        valid_sensitivity_values(),
        evidence_sha256="a" * 64,
    )
    self.assertIn(r"\textbf{(a) Formal g20", latex)
    self.assertIn(r"\textbf{(b) Scale-4 latency sensitivity", latex)
    self.assertIn("M2NDP", latex)
    self.assertIn("FuncSim bit-exact PASS", latex)
    self.assertEqual(latex.count("Geo."), 1)
    self.assertIn("scale 4", latex)
    self.assertIn("two Timing cores", latex)
    self.assertIn(r"\label{tab:gapbs_vtune_cxl}", latex)


def test_rejected_input_preserves_existing_outputs(self):
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        outputs = table.output_paths(root)
        for path in outputs:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"sentinel")
        with self.assertRaises(table.TableEvidenceError):
            table.publish(
                broken_formal_root(root),
                broken_variants_root(root),
                broken_latency_csv(root),
                root / "old-runs",
                root,
            )
        self.assertTrue(
            all(path.read_bytes() == b"sentinel" for path in outputs)
        )
```

Also test that:

- all panel-(a) latency cells use seconds with six fractional digits;
- speedups use two fractional digits;
- CSV/JSON retain unrounded decimal strings;
- the LaTeX comment contains the evidence JSON SHA-256;
- LaTeX special characters in provenance never enter visible cells; and
- a simulated validation/render failure creates no `.tmp` leftovers.

- [ ] **Step 2: Run the renderer tests and verify failure**

Run:

```bash
python3 -m unittest \
  tests.pyunit.m2ndp.test_generate_gapbs_g20_e2e_table \
  -k 'render|preserve|publish' -v
```

Expected: FAIL because rendering and publication functions are absent.

- [ ] **Step 3: Implement CSV and JSON serialization**

Define:

```python
MAIN_FIELDS = (
    "system",
    "latency_seconds",
    "speedup_vs_vanilla_cxl",
    "correctness",
)


def main_csv_bytes(rows):
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=MAIN_FIELDS)
    writer.writeheader()
    for row in rows:
        writer.writerow(
            {
                "system": row.system,
                "latency_seconds": str(row.latency_seconds),
                "speedup_vs_vanilla_cxl": str(row.speedup),
                "correctness": row.correctness,
            }
        )
    return stream.getvalue().encode("utf-8")


def evidence_json_bytes(formal, sensitivity, input_hashes):
    payload = {
        "schema": 1,
        "contract": {
            "graph_sha256": G20_SHA256,
            "iterations": 20,
            "trials": 2,
            "measured_trial": 1,
            "cores": 2,
            "cpu": "timing",
            "all_memory_cxl": True,
            "cxl_link_delay": "1us",
        },
        "formal": formal,
        "sensitivity": sensitivity,
        "input_sha256": input_hashes,
        "repository_commit": git_head(REPO),
    }
    return (
        json.dumps(payload, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")
```

Convert all `Decimal` instances to strings before calling `json.dumps`.
Do not emit `NaN` or infinity.

- [ ] **Step 4: Implement the two-panel LaTeX renderer**

`render_latex` must emit one `table` with one caption/label and two internal
tabulars. Preserve the existing
`\label{tab:gapbs_vtune_cxl}` so current paper references remain valid. Use
`\resizebox{\columnwidth}{!}{...}` for the nine-column sensitivity panel.
The generated caption must state:

```text
Panel (a) reports matched application end-to-end latency for fixed-20
PageRank on g20 with two Timing cores, all memory on 1 us CXL. Panel (b)
reports separate scale 4, single-core latency sensitivity and is not g20
evidence. Every displayed run passes its verifier; M2NDP additionally passes
strict FuncSim bit-exact validation.
```

Format cells with:

```python
def latency_cell(value):
    return f"{value:.6f} s"


def speedup_cell(value):
    return f"{value:.2f}$\\times$"
```

Use `M$^2$NDP` in the visible LaTeX system cell and plain `M2NDP` in CSV/JSON.
Only the four sensitivity workloads contribute to the single `Geo.` row.

- [ ] **Step 5: Implement staged output replacement**

Define fixed output names and stage every byte string before any replacement:

```python
def output_paths(output_dir):
    output_dir = Path(output_dir)
    return (
        output_dir / "gapbs-g20-e2e-results.csv",
        output_dir / "gapbs-g20-e2e-table-evidence.json",
        output_dir / "gapbs-vtune-cxl-table.tex",
    )


def publish_bytes(output_dir, csv_bytes, evidence_bytes, latex_bytes):
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    targets = output_paths(output_dir)
    staged = []
    try:
        for target, content in zip(
            targets, (csv_bytes, evidence_bytes, latex_bytes)
        ):
            temporary = target.with_name(f".{target.name}.tmp")
            temporary.unlink(missing_ok=True)
            temporary.write_bytes(content)
            staged.append((temporary, target))
        for temporary, target in staged:
            os.replace(temporary, target)
    except BaseException:
        for temporary, _target in staged:
            temporary.unlink(missing_ok=True)
        raise
```

Call this only after formal validation, sensitivity validation, CSV/JSON
serialization, evidence hashing, and LaTeX rendering have all succeeded.
Keep the tuple order CSV, JSON, LaTeX so the requested table is replaced
last.

- [ ] **Step 6: Run renderer tests**

Run:

```bash
python3 -m unittest \
  tests.pyunit.m2ndp.test_generate_gapbs_g20_e2e_table -v
```

Expected: all tests PASS.

- [ ] **Step 7: Commit rendering and publication**

```bash
git add \
  scripts/generate_gapbs_g20_e2e_table.py \
  tests/pyunit/m2ndp/test_generate_gapbs_g20_e2e_table.py
git commit -m "gapbs: render gated end-to-end table"
```

### Task 4: Add the CLI and Document the Reproducible Command

**Files:**

- Modify: `scripts/generate_gapbs_g20_e2e_table.py`
- Modify: `tests/pyunit/m2ndp/test_generate_gapbs_g20_e2e_table.py`
- Modify: `docs/amu-gapbs-benchmark.md`

- [ ] **Step 1: Write the failing CLI tests**

Patch `load_formal_rows` and `load_sensitivity` with valid synthetic values
to isolate argument/output behavior:

```python
def test_cli_requires_all_explicit_roots(self):
    with self.assertRaises(SystemExit) as error:
        table.parse_args([])
    self.assertEqual(error.exception.code, 2)


def test_main_reports_failure_and_preserves_table(self):
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        target = root / "gapbs-vtune-cxl-table.tex"
        target.write_text("old\n", encoding="utf-8")
        with mock.patch.object(
            table,
            "load_formal_rows",
            side_effect=table.TableEvidenceError("bad graph"),
        ):
            code = table.main(valid_cli_args(root))
        self.assertEqual(code, 1)
        self.assertEqual(target.read_text(encoding="utf-8"), "old\n")
```

Add a successful CLI test that asserts all three paths are printed and all
three files exist.

- [ ] **Step 2: Run CLI tests and verify failure**

Run:

```bash
python3 -m unittest \
  tests.pyunit.m2ndp.test_generate_gapbs_g20_e2e_table \
  -k cli -v
```

Expected: FAIL because the parser and `main` are absent.

- [ ] **Step 3: Implement the exact CLI**

```python
def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--m2ndp-results-root", type=Path, required=True
    )
    parser.add_argument(
        "--variants-results-root", type=Path, required=True
    )
    parser.add_argument("--latency-csv", type=Path, required=True)
    parser.add_argument(
        "--latency-run-root", type=Path, required=True
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv=None):
    options = parse_args(argv)
    try:
        paths = publish(
            options.m2ndp_results_root,
            options.variants_results_root,
            options.latency_csv,
            options.latency_run_root,
            options.output_dir,
        )
    except (TableEvidenceError, OSError, ValueError) as error:
        print(f"GAPBS_G20_TABLE_FAILED error={error}")
        return 1
    for path in paths:
        print(path)
    return 0
```

End the file with `raise SystemExit(main())`.

- [ ] **Step 4: Document the formal publication command**

Append a `## G20 AMU/CIRA/M2NDP table publication` section to
`docs/amu-gapbs-benchmark.md`. Include:

```bash
python3 scripts/generate_gapbs_g20_e2e_table.py \
  --m2ndp-results-root m5out/m2ndp_g20_pr_spmv_e2e \
  --variants-results-root m5out/matched_pr_spmv_g20_e2e \
  --latency-csv \
    /home/victoryang00/gem5-CXL/6472666535e6f359942ddac6/gapbs-amu-latency-results.csv \
  --latency-run-root \
    /home/victoryang00/gem5-CXL/.worktrees/gapbs-latency-table \
  --output-dir \
    /home/victoryang00/gem5-CXL/6472666535e6f359942ddac6
```

State that the command is expected to fail while any formal service is
running, that no displayed speedup is authorized by process state alone, and
that the generated evidence JSON is the machine-readable table provenance.

- [ ] **Step 5: Run CLI tests, syntax checks, and diff checks**

Run:

```bash
python3 -m unittest \
  tests.pyunit.m2ndp.test_generate_gapbs_g20_e2e_table -v
python3 -m py_compile scripts/generate_gapbs_g20_e2e_table.py
git diff --check
```

Expected: all tests PASS, compilation exits zero, and `git diff --check`
prints nothing.

- [ ] **Step 6: Commit the CLI and documentation**

```bash
git add \
  scripts/generate_gapbs_g20_e2e_table.py \
  tests/pyunit/m2ndp/test_generate_gapbs_g20_e2e_table.py \
  docs/amu-gapbs-benchmark.md
git commit -m "docs: add reproducible g20 table publication"
```

### Task 5: Run the Formal Gates and Replace the Paper Table

**Files:**

- Generate:
  `/home/victoryang00/gem5-CXL/6472666535e6f359942ddac6/gapbs-vtune-cxl-table.tex`
- Generate:
  `/home/victoryang00/gem5-CXL/6472666535e6f359942ddac6/gapbs-g20-e2e-results.csv`
- Generate:
  `/home/victoryang00/gem5-CXL/6472666535e6f359942ddac6/gapbs-g20-e2e-table-evidence.json`

- [ ] **Step 1: Prove all three formal background services finished**

Run:

```bash
systemctl show \
  m2ndp-g20-pr-spmv-resume2-20260726.service \
  gapbs-matched-pr-spmv-amu-g20-resume-20260726.service \
  gapbs-matched-pr-spmv-cira-g20-resume-20260726.service \
  -p Id -p ActiveState -p SubState -p Result -p ExecMainStatus
```

Expected for each service: no running gem5 child, `Result=success`, and
`ExecMainStatus=0`. If any service is still active, stop this task without
touching the paper table.

- [ ] **Step 2: Prove final artifacts exist before publication**

Run:

```bash
test -s m5out/m2ndp_g20_pr_spmv_e2e/summary.csv
test -s m5out/m2ndp_g20_pr_spmv_e2e/status.json
test -s m5out/m2ndp_g20_pr_spmv_e2e/manifest.json
test -s m5out/matched_pr_spmv_g20_e2e/amu/run/summary.csv
test -s m5out/matched_pr_spmv_g20_e2e/amu/run/evidence.json
test -s m5out/matched_pr_spmv_g20_e2e/cira/run/summary.csv
test -s m5out/matched_pr_spmv_g20_e2e/cira/run/evidence.json
```

Expected: every command exits zero.

- [ ] **Step 3: Run all relevant regression suites**

Run:

```bash
python3 -m unittest discover -s tests/pyunit/m2ndp -v
python3 -m unittest discover -s tests/pyunit \
  -p 'test_*gapbs*py' -v
python3 -m py_compile \
  scripts/generate_gapbs_g20_e2e_table.py \
  scripts/run_m2ndp_g20_pr_spmv.py \
  scripts/run_gapbs_matched_pr_spmv_variants.py
```

Expected: every test PASS and compilation exits zero.

- [ ] **Step 4: Generate the formal table and provenance**

Run the documented command:

```bash
python3 scripts/generate_gapbs_g20_e2e_table.py \
  --m2ndp-results-root m5out/m2ndp_g20_pr_spmv_e2e \
  --variants-results-root m5out/matched_pr_spmv_g20_e2e \
  --latency-csv \
    /home/victoryang00/gem5-CXL/6472666535e6f359942ddac6/gapbs-amu-latency-results.csv \
  --latency-run-root \
    /home/victoryang00/gem5-CXL/.worktrees/gapbs-latency-table \
  --output-dir \
    /home/victoryang00/gem5-CXL/6472666535e6f359942ddac6
```

Expected: exit zero and exactly the three output paths printed. Any
`GAPBS_G20_TABLE_FAILED` line blocks publication.

- [ ] **Step 5: Independently inspect the generated evidence and table**

Run:

```bash
sed -n '1,260p' \
  /home/victoryang00/gem5-CXL/6472666535e6f359942ddac6/gapbs-vtune-cxl-table.tex
python3 -m json.tool \
  /home/victoryang00/gem5-CXL/6472666535e6f359942ddac6/gapbs-g20-e2e-table-evidence.json
column -s, -t \
  /home/victoryang00/gem5-CXL/6472666535e6f359942ddac6/gapbs-g20-e2e-results.csv
```

Expected: four formal rows, one formal baseline of `1.00x`, one sensitivity
geometric-mean row, distinct scale labels, and no missing values.

- [ ] **Step 6: Compile the paper**

Run:

```bash
make -C /home/victoryang00/gem5-CXL/6472666535e6f359942ddac6
```

Expected: exit zero and a nonempty PDF. If the local TeX toolchain is absent,
run:

```bash
python3 -c 'from pathlib import Path; p=Path("/home/victoryang00/gem5-CXL/6472666535e6f359942ddac6/gapbs-vtune-cxl-table.tex"); s=p.read_text(); assert s.count("\\\\begin{table}") == s.count("\\\\end{table}") == 1; assert s.count("{") == s.count("}")'
```

Report PDF compilation as unavailable rather than passed.

- [ ] **Step 7: Run tracked-tree checks, push, and report proof boundaries**

Run:

```bash
git status --short
git diff --check
git log --oneline -5
git push origin m2ndp-g20-pr-spmv
```

Expected: only intentional tracked changes, no whitespace errors, and the
remote branch advances to the final implementation commit. Report the four
absolute latencies and speedups only from the generated CSV, together with
the bit-exact and calibration gates. State separately that the paper
directory is an untracked external artifact tree and was not added wholesale
to the gem5 branch.
