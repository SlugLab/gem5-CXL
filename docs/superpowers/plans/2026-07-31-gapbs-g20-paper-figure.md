# Evidence-Gated GAPBS g20 Paper Figure Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Generate a bit-exact-gated two-panel PDF/SVG figure, publish it transactionally with the exact GAPBS table, and insert the final artifact into the paper.

**Architecture:** Keep `generate_gapbs_g20_e2e_table.py` as the only evidence authority. A focused plotting module receives already-validated in-memory rows and sensitivity values, returns deterministic vector bytes, and never reads simulator logs. The publisher stages and promotes CSV, JSON, TeX, PDF, and SVG as one recoverable generation; paper source changes occur only after a real formal publication succeeds.

**Tech Stack:** Python 3.13, `unittest`, Matplotlib 3.10 Agg backend, Decimal, SVG XML, PDF metadata, LaTeX/latexmk.

---

## File map

- Create `scripts/generate_gapbs_g20_e2e_figure.py`: validate chart-ready data, select scale, and render deterministic PDF/SVG bytes.
- Create `tests/pyunit/m2ndp/test_generate_gapbs_g20_e2e_figure.py`: unit-test chart contract, metadata, dimensions, and deterministic rendering.
- Modify `scripts/generate_gapbs_g20_e2e_table.py`: call the plot renderer only after evidence validation and transactionally publish five outputs.
- Modify `tests/pyunit/m2ndp/test_generate_gapbs_g20_e2e_table.py`: test five-output publication and rollback after a mid-promotion failure.
- Modify `docs/amu-gapbs-benchmark.md`: document the new evidence-gated figure outputs and QA commands.
- Modify paper `sections/evaluation.tex`: replace the stale manually entered AMU table with the final figure after real publication succeeds.
- Generate paper `fig/gapbs-g20-e2e.pdf`, `fig/gapbs-g20-e2e.svg`, `gapbs-vtune-cxl-table.tex`, `gapbs-g20-e2e-results.csv`, and `gapbs-g20-e2e-table-evidence.json` from final evidence.

### Task 1: Chart data contract and scale selection

**Files:**
- Create: `tests/pyunit/m2ndp/test_generate_gapbs_g20_e2e_figure.py`
- Create: `scripts/generate_gapbs_g20_e2e_figure.py`

- [ ] **Step 1: Write failing data-contract tests**

Add tests that pass four `SimpleNamespace` rows in semantic order and a four-latency sensitivity map:

```python
def test_chart_data_preserves_system_order_and_separates_grains(self):
    data = figure.prepare_figure_data(
        valid_rows(), valid_sensitivity(), evidence_sha256="a" * 64
    )
    self.assertEqual(
        data.systems, ("Vanilla CXL", "AMU", "CIRA", "M2NDP")
    )
    self.assertEqual(data.latency_ns, (200, 500, 1000, 2000))
    self.assertEqual(tuple(data.sensitivity), ("AMU", "CIRA"))
    self.assertNotIn("M2NDP", data.sensitivity)

def test_panel_a_uses_log_only_at_ten_to_one(self):
    self.assertEqual(figure.choose_latency_scale((1.0, 9.99)), "linear")
    self.assertEqual(figure.choose_latency_scale((1.0, 10.0)), "log")
```

Also test rejection of reordered/missing systems, nonpositive values, missing
`Geo.` cells, unsupported latency keys, and a malformed evidence digest.

- [ ] **Step 2: Run the tests and verify RED**

Run:

```bash
env PYTHONPATH=. MPLCONFIGDIR=/tmp/gapbs-mplconfig \
  python3 tests/pyunit/m2ndp/test_generate_gapbs_g20_e2e_figure.py -v
```

Expected: import failure because `generate_gapbs_g20_e2e_figure.py` does not
exist.

- [ ] **Step 3: Implement the minimal validated data model**

Create immutable `FigureData` with systems, latencies, speedups, sensitivity,
evidence digest, and selected scale. Define:

```python
SYSTEMS = ("Vanilla CXL", "AMU", "CIRA", "M2NDP")
LATENCY_KEYS = ("200ns", "500ns", "1us", "2us")
LATENCY_NS = (200, 500, 1000, 2000)

def choose_latency_scale(values):
    values = tuple(float(value) for value in values)
    if not values or any(not math.isfinite(value) or value <= 0 for value in values):
        raise FigureDataError("formal latencies must be finite and positive")
    return "log" if max(values) / min(values) >= 10.0 else "linear"

def prepare_figure_data(rows, sensitivity, *, evidence_sha256):
    if not re.fullmatch(r"[0-9a-f]{64}", evidence_sha256):
        raise FigureDataError("evidence SHA-256 is invalid")
    if tuple(row.system for row in rows) != SYSTEMS:
        raise FigureDataError("formal systems are absent or out of order")
    # Convert positive Decimal values to floats, then read only each
    # LATENCY_KEYS[latency]["Geo."][series] for AMU and CIRA.
```

- [ ] **Step 4: Run the focused tests and verify GREEN**

Run the command from Step 2. Expected: all data-contract tests pass.

- [ ] **Step 5: Commit the data contract**

```bash
git add scripts/generate_gapbs_g20_e2e_figure.py \
  tests/pyunit/m2ndp/test_generate_gapbs_g20_e2e_figure.py
git commit -m "test: define g20 paper figure contract"
```

### Task 2: Deterministic vector rendering

**Files:**
- Modify: `tests/pyunit/m2ndp/test_generate_gapbs_g20_e2e_figure.py`
- Modify: `scripts/generate_gapbs_g20_e2e_figure.py`

- [ ] **Step 1: Write failing PDF/SVG tests**

Add tests that call `render_figure()` and assert:

```python
pdf_bytes, svg_bytes = figure.render_figure(
    valid_rows(), valid_sensitivity(), evidence_sha256="b" * 64
)
self.assertTrue(pdf_bytes.startswith(b"%PDF-"))
self.assertIn(b"Evidence SHA-256: " + b"b" * 64, pdf_bytes)
root = ElementTree.fromstring(svg_bytes)
self.assertEqual(root.attrib["width"], "504pt")
self.assertEqual(root.attrib["height"], "230.4pt")
self.assertIn("scale-4, single-core", svg_bytes.decode("utf-8"))
self.assertIn("panel_a_scale=log", svg_bytes.decode("utf-8"))
```

Render twice and assert byte equality for both formats. Add a linear-scale
fixture and assert `panel_a_scale=linear` in metadata.

- [ ] **Step 2: Run and verify RED**

Run the focused figure test. Expected: failure because `render_figure` is
absent.

- [ ] **Step 3: Implement the renderer**

Use Agg before importing pyplot, set `svg.hashsalt`, fixed fonts, and fixed size.
Create two axes with `plt.subplots(1, 2, figsize=(7.0, 3.2))`. Panel (a) uses
horizontal bars in `SYSTEMS` order, inverts the Y axis, applies the selected X
scale, and labels every bar with six-decimal seconds and two-decimal baseline
speedup. Panel (b) plots only AMU and CIRA `Geo.` points with distinct
line/marker styles, a dashed 1.0x line, and direct endpoint labels.

Save with deterministic metadata:

```python
description = (
    f"Evidence SHA-256: {data.evidence_sha256}; "
    f"panel_a_scale={data.panel_a_scale}; "
    "panel_a=g20 PageRank, 2 cores, all CXL, 1 us, 20 iterations; "
    "panel_b=scale-4, single-core GAPBS sensitivity, not g20 evidence"
)
canvas.savefig(
    pdf_stream,
    format="pdf",
    metadata={"Title": TITLE, "Subject": description,
              "Keywords": description, "CreationDate": None},
)
canvas.savefig(
    svg_stream,
    format="svg",
    metadata={"Title": TITLE, "Description": description, "Date": None},
)
```

Close the figure in `finally` and return immutable bytes.

- [ ] **Step 4: Run focused tests and inspect a synthetic rendering**

Run the focused test. Write synthetic bytes to `/tmp/gapbs-g20-e2e.pdf` and
`/tmp/gapbs-g20-e2e.svg`, then run:

```bash
pdfinfo /tmp/gapbs-g20-e2e.pdf
pdftocairo -singlefile -png -r 180 \
  /tmp/gapbs-g20-e2e.pdf /tmp/gapbs-g20-e2e
```

Expected: 504 by 230.4 pt PDF page, one page, evidence digest in metadata, and
no clipped labels in the rendered PNG.

- [ ] **Step 5: Commit the renderer**

```bash
git add scripts/generate_gapbs_g20_e2e_figure.py \
  tests/pyunit/m2ndp/test_generate_gapbs_g20_e2e_figure.py
git commit -m "feat: render evidence-linked g20 vector figure"
```

### Task 3: Five-artifact transactional publication

**Files:**
- Modify: `tests/pyunit/m2ndp/test_generate_gapbs_g20_e2e_table.py`
- Modify: `scripts/generate_gapbs_g20_e2e_table.py`

- [ ] **Step 1: Write failing integration and rollback tests**

Change the successful publication expectation from three paths to five, with:

```python
self.assertEqual(
    tuple(path.relative_to(output).as_posix() for path in paths),
    (
        "gapbs-g20-e2e-results.csv",
        "gapbs-g20-e2e-table-evidence.json",
        "gapbs-vtune-cxl-table.tex",
        "fig/gapbs-g20-e2e.pdf",
        "fig/gapbs-g20-e2e.svg",
    ),
)
```

Add a stateful `os.replace` mock that fails exactly once after at least one
temporary has been promoted. Assert that all five pre-existing sentinel files
are restored byte-for-byte and that no `.tmp` or `.bak` files remain.

- [ ] **Step 2: Run the table test and verify RED**

Run:

```bash
env PYTHONPATH=. MPLCONFIGDIR=/tmp/gapbs-mplconfig \
  python3 tests/pyunit/m2ndp/test_generate_gapbs_g20_e2e_table.py -v
```

Expected: failures because only three output paths exist and no vector renderer
is called.

- [ ] **Step 3: Integrate rendering after evidence validation**

Import the figure module in both package and script import paths. After
computing `evidence_sha256`, call:

```python
pdf_bytes, svg_bytes = figure.render_figure(
    rows, sensitivity, evidence_sha256=evidence_sha256
)
```

Extend `output_paths()` with `fig/gapbs-g20-e2e.pdf` and
`fig/gapbs-g20-e2e.svg`.

- [ ] **Step 4: Implement recoverable promotion**

For each target, create its parent directory and write a sibling `.tmp`. Move
each existing target to a sibling `.bak`, then promote every `.tmp`. On any
exception, remove newly promoted targets and move all `.bak` files back. On
success, remove backups. Reject empty payloads and verify PDF/SVG signatures
before moving an existing target.

- [ ] **Step 5: Run both test files and verify GREEN**

```bash
env PYTHONPATH=. MPLCONFIGDIR=/tmp/gapbs-mplconfig \
  python3 tests/pyunit/m2ndp/test_generate_gapbs_g20_e2e_figure.py -v
env PYTHONPATH=. MPLCONFIGDIR=/tmp/gapbs-mplconfig \
  python3 tests/pyunit/m2ndp/test_generate_gapbs_g20_e2e_table.py -v
```

Expected: both suites pass, including the injected mid-promotion rollback.

- [ ] **Step 6: Commit publication integration**

```bash
git add scripts/generate_gapbs_g20_e2e_table.py \
  tests/pyunit/m2ndp/test_generate_gapbs_g20_e2e_table.py
git commit -m "feat: publish g20 table and figure transactionally"
```

### Task 4: Documentation and static verification

**Files:**
- Modify: `docs/amu-gapbs-benchmark.md`

- [ ] **Step 1: Update the publication section**

Document the five output paths, the evidence digest embedded in both vector
formats, `MPLCONFIGDIR`, and the rule that failed or incomplete bit-exact
evidence preserves the prior complete generation. State explicitly that the
latency plot is scale-4/single-core and not g20 evidence. Replace the obsolete
live-CRIU recovery instructions with the current state: live-checkpoint units
are disabled, while the two application-level resume services and the
evidence-gated publisher timer remain enabled.

- [ ] **Step 2: Run static and regression checks**

```bash
python3 -m py_compile scripts/generate_gapbs_g20_e2e_figure.py \
  scripts/generate_gapbs_g20_e2e_table.py
env PYTHONPATH=. MPLCONFIGDIR=/tmp/gapbs-mplconfig \
  python3 tests/pyunit/m2ndp/test_generate_gapbs_g20_e2e_figure.py -v
env PYTHONPATH=. MPLCONFIGDIR=/tmp/gapbs-mplconfig \
  python3 tests/pyunit/m2ndp/test_generate_gapbs_g20_e2e_table.py -v
git diff --check
```

Expected: zero compilation errors, all tests pass, and no whitespace errors.

- [ ] **Step 3: Commit and push implementation**

```bash
git add docs/amu-gapbs-benchmark.md scripts tests
git commit -m "docs: document g20 figure publication"
git push origin m2ndp-g20-pr-spmv
```

### Task 5: Publish real evidence and integrate the paper

**Files:**
- Generate: paper `gapbs-g20-e2e-results.csv`
- Generate: paper `gapbs-g20-e2e-table-evidence.json`
- Generate: paper `gapbs-vtune-cxl-table.tex`
- Generate: paper `fig/gapbs-g20-e2e.pdf`
- Generate: paper `fig/gapbs-g20-e2e.svg`
- Modify: paper `sections/evaluation.tex`

- [ ] **Step 1: Confirm the formal evidence gate is ready**

Run the publisher against the real M2NDP, matched AMU/CIRA, and 48-row
sensitivity roots:

```bash
env MPLCONFIGDIR=/tmp/gapbs-mplconfig \
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

Expected: exit zero and five printed paper paths.
If it reports `GAPBS_G20_TABLE_FAILED`, preserve the current paper and continue
the formal simulations; do not use synthetic or partial values.

- [ ] **Step 2: Verify artifact linkage**

Compute SHA-256 of `gapbs-g20-e2e-table-evidence.json` and confirm the same
64-character digest appears in the TeX comment, PDF metadata, and SVG metadata.
Confirm all formal correctness cells are PASS and M2NDP says strict FuncSim
bit-exact PASS.

- [ ] **Step 3: Replace the stale paper table**

Replace `tab:gem5_amu_comparison` with:

```latex
\begin{figure*}[t]
  \centering
  \includegraphics[width=\textwidth]{fig/gapbs-g20-e2e.pdf}
  \caption{End-to-end PageRank latency and CXL-link sensitivity. Panel (a)
  compares matched g20 PageRank runs using two Timing cores, 20 synchronous
  double-buffered iterations, all memory on 1~$\mu$s CXL, and bit-exact
  validation. Panel (b) is the separate scale-4, single-core GAPBS sensitivity
  suite and is not g20 evidence.}
  \label{fig:gem5_amu_m2ndp_e2e}
\end{figure*}
```

Update the nearby reference to `\Cref{fig:gem5_amu_m2ndp_e2e}` and rewrite only
numeric claims directly supported by the generated CSV.

- [ ] **Step 4: Build and visually inspect the paper**

Run `make` in the paper checkout, use `pdfinfo main.pdf`, rasterize the page
containing the figure with `pdftocairo`, and inspect it for clipping, readable
labels, correct grains, and non-overlapping direct labels.

- [ ] **Step 5: Commit and push the paper**

```bash
git add sections/evaluation.tex gapbs-g20-e2e-results.csv \
  gapbs-g20-e2e-table-evidence.json gapbs-vtune-cxl-table.tex \
  fig/gapbs-g20-e2e.pdf fig/gapbs-g20-e2e.svg
git commit -m "evaluation: add bit-exact g20 AMU CIRA M2NDP comparison"
git push origin master
```

Commit only after the real publisher, LaTeX build, and visual inspection all
pass.
