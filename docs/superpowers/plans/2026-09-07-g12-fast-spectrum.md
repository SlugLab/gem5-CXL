# G12 Fast Spectrum Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Measure PageRank and GAP BC on the frozen G12 graph across four M2NDP CXL latencies, while replacing the hour-scale 48-run gem5 path with explicitly labeled anchor-derived host/CIRA rows.

**Architecture:** A G12-only runner validates the accepted registry, prepares workload-specific packages, executes FuncSim before NDPSim, and writes one evidence record per graph workload and latency. A separate publisher combines measured M2NDP rows with host/CIRA anchors; it never upgrades a derived or missing value to measured evidence.

**Tech Stack:** Python 3.13, `unittest`, SHA-256, canonical JSON, M2NDP FuncSim/NDPSim, `Decimal`, CSV, systemd.

**Spec:** `docs/superpowers/specs/2026-09-07-g12-fast-spectrum-design.md`

## Global Constraints

- The graph is scale 12 with SHA-256 `759003842b672ad90eabbd5b045980e9ddf43a95bffb01b318db7fc4b8b551f1`.
- PageRank and BC use 4,096 vertices and 96,772 directed edges from the accepted G12 registry.
- G20 paths, hashes, and evidence are invalid for the new graph rows.
- FuncSim must pass before NDPSim; every M2NDP result must pass memory, launch-count, and calibration gates.
- Measured and calibrated-derived values remain visibly distinct in every CSV and manifest.
- M2NDP cells execute sequentially and interrupted directories are archived, not deleted.

---

### Task 1: Add the G12 graph-spectrum runner

**Files:**
- Create: `tests/pyunit/cross_system/test_run_g12_m2ndp_graph_spectrum.py`
- Create: `scripts/run_g12_m2ndp_graph_spectrum.py`

**Interfaces:**
- Consumes: accepted `inputs.json`, `registry.json`, four calibration records, the existing PageRank native trace generator, and GAP BC lazy trace expander.
- Produces: `load_contract(inputs: Path, registry: Path) -> dict`, `read_cell(root: Path, workload: str, latency: str) -> dict`, and per-cell `ndpsim-evidence.json` records.

- [ ] **Step 1: Write failing identity and evidence tests**

```python
def test_contract_accepts_only_the_frozen_g12_graph(self):
    contract = runner.load_contract(self.inputs, self.registry)
    self.assertEqual(contract["graph_scale"], 12)
    self.assertEqual(contract["graph_sha256"], runner.G12_GRAPH_SHA256)

def test_g20_graph_is_rejected(self):
    self.mutate_inputs("graph.sha256", "ce900a7147a073835a7450e8f1afedf9f13db6833652bf2f9647819be26bedb3")
    with self.assertRaisesRegex(runner.SpectrumError, "G12 graph identity differs"):
        runner.load_contract(self.inputs, self.registry)

def test_ndpsim_requires_functional_and_memory_gates(self):
    row = self.valid_evidence()
    row["memory_match"] = "fail"
    with self.assertRaisesRegex(runner.SpectrumError, "memory match"):
        runner.validate_evidence(row, "pr_spmv", "200ns")
```

- [ ] **Step 2: Run the focused test and verify RED**

Run:

```bash
python3 -m unittest tests.pyunit.cross_system.test_run_g12_m2ndp_graph_spectrum -v
```

Expected: import failure for `scripts.run_g12_m2ndp_graph_spectrum`.

- [ ] **Step 3: Implement strict G12 preparation and execution**

Define these constants and validate them against both manifests:

```python
G12_GRAPH_SHA256 = "759003842b672ad90eabbd5b045980e9ddf43a95bffb01b318db7fc4b8b551f1"
G12_NODES = 4096
G12_DIRECTED_EDGES = 96772
WORKLOADS = ("pr_spmv", "gap_bc")
LATENCIES = ("200ns", "500ns", "1us", "2us")
```

For PageRank, hash-revalidate and resume the existing G12 source root at
`/mnt/disk0/gem5-CXL-eval/pr-scaling-be84a6c362-g12-qualification-v2/scales/g12/m2ndp` through its pending reference, trace, FuncSim, calibration, NDPSim, and publish stages. Package only the measured trial. For BC, lower
`/mnt/disk0/gem5-CXL-eval/g12-timing-24cell-inputs-20260906/sources/gap_bc` with `gap_bc_lazy_trace` and `m2ndp_workload_trace`.

Write evidence only after checking:

```python
if functional["status"] != "pass" or functional["bit_exact"] is not True:
    raise SpectrumError("FuncSim gate did not pass")
if evidence["memory_match"] != "pass":
    raise SpectrumError("NDPSim memory match did not pass")
if evidence["completed_launches"] != evidence["expected_launches"]:
    raise SpectrumError("NDPSim launch count differs")
```

- [ ] **Step 4: Run the focused test and verify GREEN**

```bash
python3 -m unittest tests.pyunit.cross_system.test_run_g12_m2ndp_graph_spectrum -v
```

Expected: all tests pass.

- [ ] **Step 5: Commit the runner**

```bash
git add scripts/run_g12_m2ndp_graph_spectrum.py tests/pyunit/cross_system/test_run_g12_m2ndp_graph_spectrum.py
git commit -m "feat: run strict g12 m2ndp graph spectrum"
```

### Task 2: Add the resumable sequential service

**Files:**
- Create: `scripts/run_g12_m2ndp_graph_spectrum_service.sh`
- Create: `util/systemd/m2ndp-g12-graph-spectrum.service`

**Interfaces:**
- Consumes: `run_g12_m2ndp_graph_spectrum.py --workload NAME --latency LABEL --resume`.
- Produces: eight measured cell records under `/mnt/disk0/gem5-CXL-eval/cira-amu-m2ndp-spectrum/shared/g12-graph-m2ndp-r1`.

- [ ] **Step 1: Add a failing service-contract test**

Extend the Task 1 test with:

```python
def test_service_is_sequential_and_has_no_start_timeout(self):
    unit = runner.parse_service_unit(runner.SERVICE_UNIT)
    self.assertEqual(unit["Service"]["TimeoutStartSec"], "infinity")
    self.assertNotIn("&", runner.SERVICE_SCRIPT.read_text())
```

- [ ] **Step 2: Run the test and verify RED**

Expected: failure because the service artifacts do not exist.

- [ ] **Step 3: Implement the service artifacts**

The shell script iterates `pr_spmv gap_bc` and `200ns 500ns 1us 2us` without background operators. Before retrying an incomplete cell, move it to:

```text
failed-attempts/<workload>-<latency>-<UTC timestamp>
```

The unit uses `Type=oneshot`, `Restart=on-failure`, `RestartSec=30s`, and
`TimeoutStartSec=infinity`. Set `OPENBLAS_NUM_THREADS=1` and
`OMP_NUM_THREADS=1`.

- [ ] **Step 4: Verify syntax and tests**

```bash
bash -n scripts/run_g12_m2ndp_graph_spectrum_service.sh
systemd-analyze verify util/systemd/m2ndp-g12-graph-spectrum.service
python3 -m unittest tests.pyunit.cross_system.test_run_g12_m2ndp_graph_spectrum -v
```

- [ ] **Step 5: Commit and start the service**

```bash
git add scripts/run_g12_m2ndp_graph_spectrum_service.sh util/systemd/m2ndp-g12-graph-spectrum.service tests/pyunit/cross_system/test_run_g12_m2ndp_graph_spectrum.py
git commit -m "feat: persist g12 m2ndp graph sweep"
systemctl enable --now --no-block m2ndp-g12-graph-spectrum.service
```

### Task 3: Publish measured and calibrated-derived raw data separately

**Files:**
- Create: `tests/pyunit/cross_system/test_publish_g12_fast_spectrum.py`
- Create: `scripts/publish_g12_fast_spectrum.py`

**Interfaces:**
- Consumes: the eight measured M2NDP cell records, accepted G12 host/CIRA anchors, and a hash-bound calibration-model JSON.
- Produces: `g12-fast-spectrum-raw.csv`, `g12-fast-spectrum-manifest.json`, and `g12-fast-spectrum-progress.csv`.

- [ ] **Step 1: Write failing provenance-kind tests**

```python
def test_measured_and_derived_rows_are_distinct(self):
    rows = publisher.build_rows(self.inputs)
    self.assertEqual({row["measurement_kind"] for row in rows}, {"measured", "calibrated-derived"})

def test_missing_model_stays_pending(self):
    rows = publisher.build_rows(self.inputs_without_model)
    row = next(row for row in rows if row["system"] == "cira" and row["latency"] == "200ns")
    self.assertEqual(row["status"], "pending")
    self.assertEqual(row["time_ns"], "")

def test_g20_anchor_is_rejected(self):
    with self.assertRaisesRegex(publisher.PublishError, "anchor graph identity differs"):
        publisher.build_rows(self.inputs_with_g20_anchor)
```

- [ ] **Step 2: Run the publisher test and verify RED**

```bash
python3 -m unittest tests.pyunit.cross_system.test_publish_g12_fast_spectrum -v
```

- [ ] **Step 3: Implement deterministic publication**

Use `Decimal` for every conversion. Require explicit `measurement_kind` and
preserve empty numeric fields for pending rows. Hash every anchor, model, cell,
and output in the manifest. Reject duplicate `(workload, latency, system)`
coordinates and cross-workload evidence SHA aliases.

- [ ] **Step 4: Run all focused tests and data-quality checks**

```bash
python3 -m unittest \
  tests.pyunit.cross_system.test_run_g12_m2ndp_graph_spectrum \
  tests.pyunit.cross_system.test_publish_g12_fast_spectrum -v
python3 scripts/publish_g12_fast_spectrum.py --check-only
```

Expected: tests pass; the progress CSV may contain pending cells, while the
final CSV is emitted only when all required source rows validate.

- [ ] **Step 5: Commit the publisher**

```bash
git add scripts/publish_g12_fast_spectrum.py tests/pyunit/cross_system/test_publish_g12_fast_spectrum.py
git commit -m "data: separate measured and derived g12 evidence"
```
