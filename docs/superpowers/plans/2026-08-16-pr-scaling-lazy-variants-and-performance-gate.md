# PR Scaling Lazy Variants and Performance Gate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build exact scale-local AMU/CIRA variants after Vanilla, safely migrate the existing 1/16 evidence state, and publish only when all twelve accelerated points are bit-exact and between 1.4x and 1.6x.

**Architecture:** Extend the matched-variant builder so a temporary physical build can record final published paths, then put orchestration and validation in a focused `pr_scaling_variant_build.py` module. The scaling runner records each scale build as evidence, permits one exact pre-fix state migration, and separates correctness completion from the terminal performance gate.

**Tech Stack:** Python 3, `unittest`, `pathlib`, SHA-256 manifests, exact `Decimal`, atomic rename, systemd, gem5/GAPBS, CXLMemUring, and M2NDP.

---

### Task 1: Make matched variant builds atomically publishable

**Files:**
- Modify: `scripts/build_gapbs_matched_pr_spmv_variants.py:145-430`
- Modify: `tests/pyunit/m2ndp/test_matched_pr_spmv_variants.py`

- [ ] **Step 1: Write the failing recorded-root test**

Add a test with different staging and final roots. It must prove output paths
are rebased while historical compiler commands remain unchanged:

```python
def test_rebase_manifest_paths_records_final_root(self):
    staging = self.root / ".g4.staging"
    final = self.root / "g4"
    manifest = {"variants": [{
        "binary": str((staging / "amu/bin/pr_spmv").resolve()),
        "reference_raw": str((staging / "reference/amu.u32").resolve()),
        "generated_source": str(
            (staging / "amu/generated/pr_spmv.cc").resolve()
        ),
        "command": ["g++", str(staging / "amu/generated/pr_spmv.cc")],
    }]}

    rebased = variants.rebase_output_paths(manifest, staging, final)

    row = rebased["variants"][0]
    self.assertEqual(row["binary"], str((final / "amu/bin/pr_spmv").resolve()))
    self.assertEqual(
        row["reference_raw"],
        str((final / "reference/amu.u32").resolve()),
    )
    self.assertIn(str(staging), " ".join(row["command"]))
```

- [ ] **Step 2: Run the test and verify RED**

```bash
PYTHONPATH=. python3 -m unittest discover -s tests/pyunit/m2ndp \
  -p 'test_matched_pr_spmv_variants.py' -v
```

Expected: FAIL because `rebase_output_paths` does not exist.

- [ ] **Step 3: Implement path rebasing and the CLI**

```python
def rebase_output_paths(manifest, physical_root, recorded_root):
    physical_root = Path(physical_root).resolve()
    recorded_root = Path(recorded_root).resolve()
    result = json.loads(json.dumps(manifest))
    for row in result.get("variants", []):
        for field in ("binary", "reference_raw", "generated_source"):
            path = Path(row[field]).resolve()
            try:
                relative = path.relative_to(physical_root)
            except ValueError as error:
                raise VariantEvidenceError(
                    f"{field} is outside physical output root: {path}"
                ) from error
            row[field] = str(recorded_root / relative)
    return result
```

Add `parser.add_argument("--recorded-outdir", type=Path)`. Before writing the
manifest:

```python
if args.recorded_outdir is not None:
    manifest = rebase_output_paths(manifest, args.outdir, args.recorded_outdir)
```

- [ ] **Step 4: Run the focused suite and verify GREEN**

Run Step 2 again. Expected: all tests PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/build_gapbs_matched_pr_spmv_variants.py \
  tests/pyunit/m2ndp/test_matched_pr_spmv_variants.py
git commit -m "feat: record final paths for staged variant builds"
```

### Task 2: Add the scale-local lazy variant build stage

**Files:**
- Create: `scripts/pr_scaling_variant_build.py`
- Create: `tests/pyunit/cross_system/test_pr_scaling_variant_build.py`
- Modify: `scripts/run_cira_amu_m2ndp_scaling.py:65-175,371-430,625-690`
- Modify: `tests/pyunit/cross_system/test_run_cira_amu_m2ndp_scaling.py`

- [ ] **Step 1: Write failing module tests**

Create real tiny baseline/calibration/variant manifests and binary files. Test
the exact contract and each drift dimension:

```python
def test_validate_build_binds_baseline_calibration_policy_and_binaries(self):
    record = stage.validate_variant_build(
        self.output,
        baseline_build=self.baseline,
        calibration=self.calibration,
    )
    self.assertEqual(record["cira_mode"], "pgo-selected")
    self.assertEqual(record["cira_policy_latency_ns"], 1000)
    self.assertEqual(set(record["binary_sha256"]), {"amu", "cira"})

def test_validate_build_rejects_baseline_drift(self):
    value = json.loads((self.output / "manifest.json").read_text())
    value["baseline_manifest_sha256"] = "0" * 64
    (self.output / "manifest.json").write_text(json.dumps(value) + "\n")
    with self.assertRaisesRegex(stage.VariantBuildError, "baseline"):
        stage.validate_variant_build(
            self.output,
            baseline_build=self.baseline,
            calibration=self.calibration,
        )
```

Also test calibration hash, CIRA mode, policy latency, missing AMU/CIRA row,
missing binary, and binary hash drift. Assert `build_command()` contains
`--cira-mode pgo-selected`, `--cira-policy-latency-ns 1000`,
`--cira-row-batch 64`, the frozen `--m5-library`, and `--recorded-outdir`.

- [ ] **Step 2: Run the new test and verify RED**

```bash
PYTHONPATH=. python3 -m unittest discover -s tests/pyunit/cross_system \
  -p 'test_pr_scaling_variant_build.py' -v
```

Expected: FAIL because `scripts.pr_scaling_variant_build` does not exist.

- [ ] **Step 3: Implement the focused module**

Create `VariantBuildError`, `sha256_file`, `build_command`,
`validate_variant_build`, and `ensure_variant_build`. The command is:

```python
def build_command(
    *, baseline_build, staging, final, cxlmemuring, m5_library, calibration,
):
    return [
        sys.executable,
        str(REPO / "scripts/build_gapbs_matched_pr_spmv_variants.py"),
        "--baseline-build", str(Path(baseline_build).resolve()),
        "--outdir", str(Path(staging).resolve()),
        "--recorded-outdir", str(Path(final).resolve()),
        "--cxlmemuring", str(Path(cxlmemuring).resolve()),
        "--m5-library", str(Path(m5_library).resolve()),
        "--cira-mode", "pgo-selected",
        "--calibration-manifest", str(Path(calibration).resolve()),
        "--cira-row-batch", "64",
        "--cira-policy-latency-ns", "1000",
    ]
```

Require this semantic contract:

```python
expected = {
    "benchmark": "pr_spmv",
    "page_rank_iterations": 20,
    "fixed_iterations": True,
    "fp_contract": False,
    "fast_math": False,
    "baseline_manifest_sha256": sha256_file(
        Path(baseline_build) / "manifest.json"
    ),
    "cira_mode": "pgo-selected",
    "cira_policy_latency_ns": 1000,
}
```

Require `cira_policy.calibration_manifest_sha256` to equal the live calibration
hash, exactly one AMU and one CIRA row, and matching live binary hashes.
`ensure_variant_build` must validate an existing final build or build into a
unique temporary sibling, validate staged content by mapping recorded final
paths back to staging, atomically rename, and validate final content. Reject a
nonempty invalid final directory; never overwrite it.

- [ ] **Step 4: Run the module tests and verify GREEN**

Run Step 2 again. Expected: all tests PASS.

- [ ] **Step 5: Write failing runner integration tests**

```python
def test_amu_lazily_builds_scale_variants_after_vanilla(self):
    state_value = scaling.new_state(self.options)
    scaling.record_pass(
        state_value, scaling.MatrixEntry(4, "vanilla"),
        {"summary": sha("vanilla")}, latency_seconds="4",
        output_elements=16, mechanism={"verification": "pass"},
    )
    with mock.patch.object(
        scaling.variant_build, "ensure_variant_build"
    ) as ensure:
        ensure.return_value = {"manifest_sha256": sha("variant")}
        scaling.ensure_variants_for_scale(4, state_value, self.options)
    ensure.assert_called_once()
    self.assertEqual(state_value["variant_builds"]["g4"]["status"], "passed")
```

Also prove no build occurs for Vanilla/M2NDP and AMU/CIRA cannot launch while
their scale build is pending or failed.

- [ ] **Step 6: Run runner tests and verify RED**

```bash
PYTHONPATH=. python3 -m unittest discover -s tests/pyunit/cross_system \
  -p 'test_run_cira_amu_m2ndp_scaling.py' -v
```

Expected: FAIL because the new state/function do not exist.

- [ ] **Step 7: Integrate build evidence**

Import the new module. Add `variant_builds` records with status, command,
inputs, outputs, log, and error for g4/g12/g14/g20. Implement
`ensure_variants_for_scale()` to hash immutable inputs, persist running state,
invoke the module, and mark passed only after validation. Log to
`run/scales/g<scale>/variant-build.log`.

Before executing AMU or CIRA:

```python
if entry.system in {"amu", "cira"}:
    ensure_variants_for_scale(entry.scale, state, options)
```

On resume, revalidate passed build bytes and semantic identity. Retry failed
builds only when inputs match and no published final directory exists.

Extend `_code_sha256()` to include both
`scripts/pr_scaling_variant_build.py` and
`scripts/build_gapbs_matched_pr_spmv_variants.py`. Add a test proving a byte
change in either file changes the runner code identity.

- [ ] **Step 8: Run both focused suites**

Run Steps 2 and 6. Expected: all tests PASS.

- [ ] **Step 9: Commit**

```bash
git add scripts/pr_scaling_variant_build.py \
  scripts/run_cira_amu_m2ndp_scaling.py \
  tests/pyunit/cross_system/test_pr_scaling_variant_build.py \
  tests/pyunit/cross_system/test_run_cira_amu_m2ndp_scaling.py
git commit -m "fix: lazily build scale-local PR variants"
```

### Task 3: Add the one-time fail-closed resume migration

**Files:**
- Modify: `scripts/run_cira_amu_m2ndp_scaling.py:625-685`
- Modify: `tests/pyunit/cross_system/test_run_cira_amu_m2ndp_scaling.py`

- [ ] **Step 1: Write migration acceptance and rejection tests**

Use exact old code hash
`438735b038266173d5337d86db3fdbcf26321794336f8af52180081ef08f94d3`:

```python
def test_exact_prefixed_one_point_state_migrates_with_lineage(self):
    legacy = self.real_legacy_state_with_passed_g4_vanilla()
    migrated = scaling.migrate_pre_lazy_variant_state(
        legacy, scaling.new_state(self.options), self.options
    )
    self.assertEqual(migrated["points"]["g4:vanilla"]["status"], "passed")
    self.assertEqual(migrated["variant_builds"]["g4"]["status"], "pending")
    self.assertEqual(
        migrated["resume_lineage"]["previous_code_sha256"],
        scaling.PRE_LAZY_VARIANT_CODE_SHA256,
    )
```

Reject a second passed point, different old code hash, changed Vanilla outputs
or measurement, existing published variant directory, and any changed non-code
identity.

- [ ] **Step 2: Run runner tests and verify RED**

Run Task 2 Step 6. Expected: FAIL because migration is absent.

- [ ] **Step 3: Implement exact migration**

Require exactly this passed set:

```python
passed = {key for key, row in state["points"].items()
          if row.get("status") == "passed"}
if passed != {"g4:vanilla"}:
    raise ScalingError("pre-lazy-variant migration shape differs")
```

Compare all identities except code hash/new fields. Recompute `_point_outputs`
and `_point_measurement` for g4 Vanilla and require exact stored equality. Copy
new top-level defaults from the expected state (including `variant_builds`),
replace only code SHA, and record:

```python
"resume_lineage": {
    "previous_code_sha256": PRE_LAZY_VARIANT_CODE_SHA256,
    "current_code_sha256": expected["code_sha256"],
    "retained_points": ["g4:vanilla"],
}
```

Remove stale `failed.json` only after migration and passed-point revalidation
succeed.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run Task 2 Step 6. Expected: all tests PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/run_cira_amu_m2ndp_scaling.py \
  tests/pyunit/cross_system/test_run_cira_amu_m2ndp_scaling.py
git commit -m "fix: migrate the exact one-point PR scaling state"
```

### Task 4: Enforce the twelve-point 1.4x-to-1.6x gate

**Files:**
- Modify: `scripts/run_cira_amu_m2ndp_scaling.py:371-450,625-715`
- Modify: `tests/pyunit/cross_system/test_run_cira_amu_m2ndp_scaling.py`
- Modify: `tests/pyunit/cross_system/test_generate_pr_scaling_artifacts.py`

- [ ] **Step 1: Write failing exact-boundary tests**

```python
def test_performance_gate_accepts_exact_boundaries(self):
    state = self.complete_state_with_speedups(("1.4", "1.6"))
    self.assertEqual(
        scaling.evaluate_performance_gate(state),
        {"status": "passed", "offenders": []},
    )

def test_performance_gate_reports_all_out_of_range_points(self):
    state = self.complete_state_with_overrides({
        "g4:amu": "1.399999", "g20:m2ndp": "1.600001",
    })
    result = scaling.evaluate_performance_gate(state)
    self.assertEqual(result["status"], "hold")
    self.assertEqual(
        {(row["point"], row["speedup"]) for row in result["offenders"]},
        {("g4:amu", "1.399999"), ("g20:m2ndp", "1.600001")},
    )
```

Add an end-state test: a hold writes `performance-hold.json`, preserves all
points, blocks `complete.json`, removes stale `failed.json`, and returns zero as
an expected terminal result.

- [ ] **Step 2: Run runner tests and verify RED**

Run Task 2 Step 6. Expected: FAIL because gate evaluation is absent.

- [ ] **Step 3: Implement exact Decimal evaluation**

```python
MIN_ACCELERATOR_SPEEDUP = Decimal("1.4")
MAX_ACCELERATOR_SPEEDUP = Decimal("1.6")
```

Require correctness-complete state, iterate exactly the twelve non-Vanilla
entries, and return sorted offender records with point, scale, system, speedup,
minimum, and maximum. After 16/16 correctness:

```python
gate = evaluate_performance_gate(state)
state["performance_gate"] = gate
if gate["status"] == "hold":
    state["status"] = "performance_hold"
    contract.atomic_write_json(state_path, state)
    contract.atomic_write_json(performance_hold_path, state)
    complete_path.unlink(missing_ok=True)
    failed_path.unlink(missing_ok=True)
    print(f"SCALING_PERFORMANCE_HOLD offenders={len(gate['offenders'])}")
    return 0
```

On pass, remove the hold artifact, set complete, and write `complete.json`.

- [ ] **Step 4: Test publisher rejection of a hold artifact**

Pass a hold JSON to the publisher and require rejection mentioning status or
performance gate. Keep `complete.json` as the only publishable input.

- [ ] **Step 5: Run focused runner and publisher suites**

```bash
PYTHONPATH=. python3 -m unittest discover -s tests/pyunit/cross_system \
  -p 'test_run_cira_amu_m2ndp_scaling.py' -v
PYTHONPATH=. python3 -m unittest discover -s tests/pyunit/cross_system \
  -p 'test_generate_pr_scaling_artifacts.py' -v
```

Expected: all tests PASS.

- [ ] **Step 6: Commit**

```bash
git add scripts/run_cira_amu_m2ndp_scaling.py \
  tests/pyunit/cross_system/test_run_cira_amu_m2ndp_scaling.py \
  tests/pyunit/cross_system/test_generate_pr_scaling_artifacts.py
git commit -m "feat: gate PR scaling publication near 1.5x"
```

### Task 5: Document, verify, push, and resume

**Files:**
- Modify: `docs/amu-gapbs-benchmark.md`
- Evidence update: `/mnt/disk0/gem5-CXL-eval/pr-scaling-120b389653d8/run/`

- [ ] **Step 1: Update operator documentation**

Document automatic PGO-selected scale builds, `performance-hold.json`, the
inclusive 1.4x-to-1.6x rule, exact one-point migration, and the distinction
between correctness failure and performance hold. Preserve the pinned toolchain
PATH and `--resume` in the launch command.

- [ ] **Step 2: Run static checks**

```bash
python3 -m py_compile \
  scripts/build_gapbs_matched_pr_spmv_variants.py \
  scripts/pr_scaling_variant_build.py \
  scripts/run_cira_amu_m2ndp_scaling.py \
  scripts/generate_pr_scaling_artifacts.py
git diff --check
```

Expected: exit zero and no output.

- [ ] **Step 3: Run all direct Python suites**

```bash
PYTHONPATH=. python3 -m unittest discover -s tests/pyunit/amu -p 'test_*.py' -q
PYTHONPATH=. python3 -m unittest discover -s tests/pyunit/m2ndp -p 'test_*.py' -q
PYTHONPATH=. python3 -m unittest discover -s tests/pyunit/cross_system -p 'test_*.py' -q
```

Expected: all tests PASS; only documented skips are allowed. Do not discover
from `tests/pyunit`, whose `test_run.py` requires gem5 TestLib initialization.

- [ ] **Step 4: Commit docs, push, and verify identity**

```bash
git add docs/amu-gapbs-benchmark.md
git commit -m "docs: describe PR scaling build and speedup gates"
git push origin m2ndp-g20-pr-spmv
test "$(git rev-parse HEAD)" = \
  "$(git rev-parse origin/m2ndp-g20-pr-spmv)"
git status --short
```

Expected: matching commits and no status output.

- [ ] **Step 5: Resume the current evidence root**

Require the service inactive and exactly `g4:vanilla` passed. Launch:

```bash
systemd-run --unit=cira-amu-m2ndp-pr-scaling-formal --collect \
  --description='Formal four-thread all-CXL 1us PR scaling' \
  --property=WorkingDirectory=/home/victoryang00/gem5-CXL/.worktrees/m2ndp-g20-pr-spmv \
  --setenv=PATH=/home/victoryang00/gem5-CXL/.worktrees/m2ndp-g20-pr-spmv/m5out/m2ndp_toolchain/venv311/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
  /usr/bin/python3 scripts/run_cira_amu_m2ndp_scaling.py \
  --inputs /mnt/disk0/gem5-CXL-eval/pr-scaling-120b389653d8/inputs.json \
  --calibration /mnt/disk0/gem5-CXL-g14-eval/amu-paper-full-2c07da6b73.calibration.json \
  --root /mnt/disk0/gem5-CXL-eval/pr-scaling-120b389653d8/run \
  --gem5 /mnt/disk0/gem5-CXL-g14-eval/amu-paper-full-2c07da6b73/inputs/gem5 \
  --m5-library /home/victoryang00/gem5-CXL/.worktrees/m2ndp-g20-pr-spmv/util/m5/build/x86/out/libm5.a \
  --config /home/victoryang00/gem5-CXL/.worktrees/m2ndp-g20-pr-spmv/configs/example/gem5_library/x86-gapbs-amu-se.py \
  --cxlmemuring /home/victoryang00/CXLMemUring \
  --m2ndp-root /mnt/disk0/M2NDP-public \
  --variants-build-root /mnt/disk0/gem5-CXL-eval/pr-scaling-120b389653d8/builds \
  --timeout 0 --resume
```

- [ ] **Step 6: Verify migration and live progress**

```bash
systemctl status cira-amu-m2ndp-pr-scaling-formal.service --no-pager -l
journalctl -u cira-amu-m2ndp-pr-scaling-formal.service -n 120 --no-pager
python3 -m json.tool \
  /mnt/disk0/gem5-CXL-eval/pr-scaling-120b389653d8/run/state.json
```

Expected: `resume_lineage` has exact old/new code hashes, g4 Vanilla remains
passed, g4 variant build is running or passed, and stale `failed.json` is gone.

- [ ] **Step 7: Publish only after both gates pass**

If and only if `complete.json` has 16 passed points and performance gate
`passed`:

```bash
python3 scripts/generate_pr_scaling_artifacts.py \
  --scaling /mnt/disk0/gem5-CXL-eval/pr-scaling-120b389653d8/run/complete.json \
  --output-root /mnt/disk0/gem5-CXL-eval/pr-scaling-120b389653d8/publication
sha256sum \
  /mnt/disk0/gem5-CXL-eval/pr-scaling-120b389653d8/publication/pr-scaling-raw.* \
  /mnt/disk0/gem5-CXL-eval/pr-scaling-120b389653d8/publication/pr-scaling-table.tex \
  /mnt/disk0/gem5-CXL-eval/pr-scaling-120b389653d8/publication/fig/*
```

If `performance-hold.json` exists, report every real out-of-range point and do
not invoke the publisher.
