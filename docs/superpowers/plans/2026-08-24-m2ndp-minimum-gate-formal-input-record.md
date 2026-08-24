# M2NDP Minimum Gate and Formal Input Record Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Accept the measured bit-exact M2NDP `2.6342721382289415x` result under a correctness-plus-minimum-performance gate, keep AMU/CIRA bounded, and publish fail-closed formal workload input provenance.

**Architecture:** `scripts/pr_offload_contract.py` becomes the only owner of per-system performance policy, and every qualification, scaling, completion, and publishing path consumes that policy. A separate audit utility produces non-accepted input discovery and template records, while the existing freezer remains the sole writer of accepted input evidence after live path, hash, size, and Git validation.

**Tech Stack:** Python 3, `unittest`, `Decimal`, JSON evidence contracts, SHA-256, Git plumbing, gem5, M2NDP FuncSim/NDPSim.

---

## File map

- Modify `scripts/pr_offload_contract.py`: own and evaluate per-system policy.
- Modify `scripts/run_pr_asymmetric_offload.py`: qualify g12 primary/replay.
- Modify `scripts/run_cira_amu_m2ndp_scaling.py`: gate nine formal points.
- Modify `scripts/qualify_pr_scaling_g12.py`: emit bounded AMU/CIRA policy.
- Modify `scripts/generate_pr_scaling_artifacts.py`: recompute publication gate.
- Create `scripts/audit_cross_system_input_record.py`: generate non-accepted
  template and live discovery records.
- Modify the corresponding `tests/pyunit/cross_system/` suites.
- Create runtime evidence only under `/mnt/disk0/gem5-CXL-eval/`.

### Task 1: Centralize per-system performance policy

**Files:**
- Modify: `scripts/pr_offload_contract.py`
- Test: `tests/pyunit/cross_system/test_pr_offload_contract.py`

- [ ] **Step 1: Write failing boundary tests**

Add:

```python
def test_performance_policy_is_bounded_except_for_m2ndp(self):
    self.assertEqual(contract.performance_policy("amu"), {
        "minimum": "1.4", "maximum": "1.6",
        "correctness": "bit-exact",
    })
    self.assertEqual(contract.performance_policy("cira"), {
        "minimum": "1.4", "maximum": "1.6",
        "correctness": "bit-exact",
    })
    self.assertEqual(contract.performance_policy("cira-few-shot"), {
        "minimum": "1.4", "maximum": "1.6",
        "correctness": "bit-exact",
    })
    self.assertEqual(contract.performance_policy("m2ndp"), {
        "minimum": "1.4", "maximum": None,
        "correctness": "bit-exact-funcsim-before-ndpsim",
    })
    with self.assertRaisesRegex(contract.OffloadError, "performance policy"):
        contract.performance_policy("vanilla")

def test_m2ndp_acceptance_has_no_upper_bound(self):
    self.assertTrue(contract.performance_accepted("amu", Decimal("1.4")))
    self.assertTrue(contract.performance_accepted("amu", Decimal("1.6")))
    self.assertFalse(contract.performance_accepted("amu", Decimal("1.600001")))
    self.assertFalse(contract.performance_accepted("m2ndp", Decimal("1.399999")))
    self.assertTrue(contract.performance_accepted("m2ndp", Decimal("1.4")))
    self.assertTrue(contract.performance_accepted(
        "m2ndp", Decimal("2.634272138228941520602758013")
    ))
```

- [ ] **Step 2: Run RED test**

Run:

```bash
python3 -m unittest tests.pyunit.cross_system.test_pr_offload_contract -v
```

Expected: failure because both policy functions are absent.

- [ ] **Step 3: Implement the shared policy**

Add after `_decimal` in `scripts/pr_offload_contract.py`:

```python
def performance_policy(system):
    if system == "m2ndp":
        return {
            "minimum": str(MIN_SPEEDUP),
            "maximum": None,
            "correctness": "bit-exact-funcsim-before-ndpsim",
        }
    if system == "amu" or system == "cira" or system.startswith("cira-"):
        return {
            "minimum": str(MIN_SPEEDUP),
            "maximum": str(MAX_SPEEDUP),
            "correctness": "bit-exact",
        }
    raise OffloadError(f"no performance policy for system {system}")


def performance_accepted(system, speedup):
    value = _decimal(speedup, f"{system} speedup")
    policy = performance_policy(system)
    minimum = Decimal(policy["minimum"])
    maximum = (
        None if policy["maximum"] is None
        else Decimal(policy["maximum"])
    )
    return value >= minimum and (maximum is None or value <= maximum)
```

In `validate_complete`, construct each gate row with:

```python
policy = performance_policy(row["system"])
gate.append({
    "scale": row["scale"], "system": row["system"],
    "speedup": speedup, **policy,
    "accepted": performance_accepted(row["system"], speedup),
})
```

- [ ] **Step 4: Run GREEN test and commit**

Run:

```bash
python3 -m unittest tests.pyunit.cross_system.test_pr_offload_contract -v
git diff --check
git add scripts/pr_offload_contract.py tests/pyunit/cross_system/test_pr_offload_contract.py
git commit -m "feat: define system-specific offload gates"
```

Expected: all tests pass; `src/mem/cache/base.cc` is excluded.

### Task 2: Apply the policy to replayed g12 qualification

**Files:**
- Modify: `scripts/run_pr_asymmetric_offload.py`
- Test: `tests/pyunit/cross_system/test_run_pr_asymmetric_offload.py`
- Test: `tests/pyunit/cross_system/test_generate_pr_offload_artifacts.py`

- [ ] **Step 1: Write failing qualification tests**

Add tests proving that `2.634272138228941520602758013` passes for M2NDP,
`1.399999` fails for M2NDP, and `1.600001` still fails for AMU and CIRA. The
passing result must contain:

```python
self.assertEqual(gate["policies"]["m2ndp"], {
    "minimum": "1.4", "maximum": None,
    "correctness": "bit-exact-funcsim-before-ndpsim",
})
```

Update the fixture helper to accept an exact M2NDP speedup and derive its
period or cycle count with `Decimal`, never a Python float.

- [ ] **Step 2: Run RED test**

Run:

```bash
python3 -m unittest tests.pyunit.cross_system.test_run_pr_asymmetric_offload -v
```

Expected: the measured M2NDP point is an offender under the old maximum.

- [ ] **Step 3: Use the shared policy and improve hold evidence**

Replace the uniform comparison with:

```python
policies = {
    system: contract.performance_policy(system) for system in speedups
}
offenders = [
    system for system, speedup in speedups.items()
    if not contract.performance_accepted(system, speedup)
]
return {
    "status": "failed" if offenders else "passed",
    "checked_points": 3,
    "speedups": {name: str(value) for name, value in speedups.items()},
    "policies": policies,
    "offenders": offenders,
}
```

Change the hold reason to:

```python
error="g12 accelerated speedup outside system-specific performance policy"
```

Update artifact fixtures to include `minimum`, `maximum`, and `correctness` in
each recomputed gate row. Add a mutation removing M2NDP `maximum` and expect
`PublishError("stored performance gate differs")`.

- [ ] **Step 4: Run GREEN tests and commit**

Run:

```bash
python3 -m unittest \
  tests.pyunit.cross_system.test_run_pr_asymmetric_offload \
  tests.pyunit.cross_system.test_generate_pr_offload_artifacts -v
git diff --check
git add scripts/run_pr_asymmetric_offload.py \
  tests/pyunit/cross_system/test_run_pr_asymmetric_offload.py \
  tests/pyunit/cross_system/test_generate_pr_offload_artifacts.py
git commit -m "fix: accept minimum-qualified M2NDP timing"
```

Expected: correctness failures still raise before performance is evaluated.

### Task 3: Apply the contract to nine-point scaling and publication

**Files:**
- Modify: `scripts/run_cira_amu_m2ndp_scaling.py`
- Modify: `scripts/qualify_pr_scaling_g12.py`
- Modify: `scripts/generate_pr_scaling_artifacts.py`
- Test: `tests/pyunit/cross_system/test_run_cira_amu_m2ndp_scaling.py`
- Test: `tests/pyunit/cross_system/test_qualify_pr_scaling_g12.py`
- Test: `tests/pyunit/cross_system/test_generate_pr_scaling_artifacts.py`

- [ ] **Step 1: Write failing nine-point tests**

Add:

```python
def test_gate_accepts_m2ndp_above_old_upper_bound(self):
    state = self.complete_state_with_overrides({
        "g12:m2ndp": "2.634272138228941520602758013",
        "g14:m2ndp": "2.1",
        "g20:m2ndp": "1.600001",
    })
    result = scaling.evaluate_performance_gate(state)
    self.assertEqual(result["status"], "passed")
    self.assertEqual(result["offenders"], [])
    self.assertIsNone(result["policies"]["m2ndp"]["maximum"])

def test_gate_rejects_bounded_and_below_minimum_points(self):
    state = self.complete_state_with_overrides({
        "g12:amu": "1.600001",
        "g14:cira": "1.399999",
        "g20:m2ndp": "1.399999",
    })
    result = scaling.evaluate_performance_gate(state)
    self.assertEqual(
        {row["point"] for row in result["offenders"]},
        {"g12:amu", "g14:cira", "g20:m2ndp"},
    )
    m2ndp = next(row for row in result["offenders"]
                 if row["system"] == "m2ndp")
    self.assertIsNone(m2ndp["maximum"])
```

Update exact gate expectations with:

```python
"policies": {
    system: gate_contract.performance_policy(system)
    for system in ("amu", "cira", "m2ndp")
},
```

In the publisher test, make one formal M2NDP point exceed `1.6`, then add
separate mutations for AMU `1.600001` and M2NDP `1.399999` and expect
`ArtifactError` for both invalid cases.

- [ ] **Step 2: Run RED tests**

Run:

```bash
python3 -m unittest \
  tests.pyunit.cross_system.test_run_cira_amu_m2ndp_scaling \
  tests.pyunit.cross_system.test_qualify_pr_scaling_g12 \
  tests.pyunit.cross_system.test_generate_pr_scaling_artifacts -v
```

Expected: failures name the old uniform maximum or missing policy fields.

- [ ] **Step 3: Import and use the shared contract**

Add this package import, with the equivalent direct-import fallback, to the
scaling runner and publisher:

```python
from scripts import pr_offload_contract as gate_contract
```

Replace each uniform comparison with:

```python
policy = gate_contract.performance_policy(system)
if not gate_contract.performance_accepted(system, speedup):
    offenders.append({
        "point": key,
        "scale": scale,
        "system": system,
        "speedup": str(speedup),
        **policy,
    })
```

Return this alongside `status`, `checked_points`, and `offenders`:

```python
"policies": {
    system: gate_contract.performance_policy(system)
    for system in SYSTEMS if system != "vanilla"
},
```

Use the helper in stored g12 qualification validation and
`generate_pr_scaling_artifacts.load_data`. The separate qualifier has no
M2NDP point, so it emits only the shared AMU and CIRA bounded policies.

- [ ] **Step 4: Run GREEN tests and commit**

Run:

```bash
python3 -m unittest \
  tests.pyunit.cross_system.test_run_cira_amu_m2ndp_scaling \
  tests.pyunit.cross_system.test_qualify_pr_scaling_g12 \
  tests.pyunit.cross_system.test_generate_pr_scaling_artifacts -v
git diff --check
git add scripts/run_cira_amu_m2ndp_scaling.py \
  scripts/qualify_pr_scaling_g12.py \
  scripts/generate_pr_scaling_artifacts.py \
  tests/pyunit/cross_system/test_run_cira_amu_m2ndp_scaling.py \
  tests/pyunit/cross_system/test_qualify_pr_scaling_g12.py \
  tests/pyunit/cross_system/test_generate_pr_scaling_artifacts.py
git commit -m "fix: share M2NDP minimum gate across scaling"
```

Expected: all focused suites pass and stored gates name their exact policy.

### Task 4: Add fail-closed formal input discovery

**Files:**
- Create: `scripts/audit_cross_system_input_record.py`
- Create: `tests/pyunit/cross_system/test_audit_cross_system_input_record.py`
- Reuse unchanged: `scripts/freeze_cross_system_inputs.py`

- [ ] **Step 1: Write failing template and discovery tests**

Create tests with these assertions:

```python
def test_template_has_exact_six_workload_shape(self):
    value = audit.template_record()
    self.assertEqual(set(value), set(freeze.WORKLOADS))
    self.assertEqual(value["pr_spmv"]["scale"], 20)
    self.assertEqual(value["mcf"]["synthetic"], False)
    self.assertEqual(value["amg_gather"]["allocated_bytes"], 1 << 30)
    self.assertEqual(value["npb_cg"]["allocated_bytes"], 12_000_000_000)

def test_incomplete_candidate_is_never_accepted(self):
    candidate = {"pr_spmv": {
        "input": str(self.graph.resolve()),
        "input_sha256": sha256(self.graph),
        "allocated_bytes": 240_000_000,
        "scale": 20,
    }}
    result = audit.audit_record(candidate)
    self.assertEqual(result["status"], "incomplete")
    self.assertIn("mcf", result["missing_workloads"])
    self.assertEqual(
        result["workloads"]["pr_spmv"]["observed"]["input_sha256"],
        sha256(self.graph),
    )

def test_complete_live_candidate_is_ready_but_not_accepted(self):
    candidate = valid_record(self.root)
    result = audit.audit_record(candidate)
    self.assertEqual(result["status"], "ready_for_freeze")
    self.assertNotEqual(result["status"], "accepted")
```

Reuse the fixture builder from `test_freeze_cross_system_inputs.py`.

- [ ] **Step 2: Run RED test**

Run:

```bash
python3 -m unittest tests.pyunit.cross_system.test_audit_cross_system_input_record -v
```

Expected: import failure because the audit module is absent.

- [ ] **Step 3: Implement template generation**

`template_record()` returns exactly the fields required by
`freeze_cross_system_inputs.REQUIRED`. Use these minimum allocations:

```python
{
    "pr_spmv": 240_000_000,
    "mcf": 345_000_000,
    "amg_gather": 1 << 30,
    "lulesh_scatter": 1 << 30,
    "npb_cg": 12_000_000_000,
    "npb_mg": 12_000_000_000,
}
```

Path and digest values use explicit strings such as
`REQUIRED_ABSOLUTE_DATA_PATH` and `REQUIRED_SHA256`. Set `pr_spmv.scale` to 20
and `mcf.synthetic` to `False`. The template itself is intentionally invalid
for formal freezing.

- [ ] **Step 4: Implement live candidate auditing**

`audit_record(value)` must:

```python
result = {
    "schema": 1,
    "status": "incomplete",
    "missing_workloads": [],
    "workloads": {},
    "reasons": [],
}
```

For every present file path, resolve it, require an absolute existing file,
and record its observed SHA-256 and file size. For each NPB source root, record
`git rev-parse HEAD` and `git status --porcelain`. Record every missing field
from `freeze.REQUIRED`. Only when the candidate has the exact six-workload
shape call:

```python
freeze.validate_bound_inputs(value)
```

Set `status` to `ready_for_freeze` only when that call succeeds. Catch
`freeze.InputError`, append its exact message to `reasons`, and leave status as
`incomplete`. This utility never returns `accepted`.

The CLI accepts `--candidate-record`, `--discovery-output`, and
`--template-output`, treats a missing candidate as `{}` plus a reason, and
writes both outputs with `cross_system_contract.atomic_write_json`.

- [ ] **Step 5: Run GREEN tests and commit**

Run:

```bash
python3 -m unittest \
  tests.pyunit.cross_system.test_audit_cross_system_input_record \
  tests.pyunit.cross_system.test_freeze_cross_system_inputs -v
git diff --check
git add scripts/audit_cross_system_input_record.py \
  tests/pyunit/cross_system/test_audit_cross_system_input_record.py
git commit -m "feat: audit formal cross-system inputs"
```

Expected: all tests pass and the accepted freezer remains fail-closed.

### Task 5: Produce live workload path records

**Files:**
- Create runtime artifact:
  `/mnt/disk0/gem5-CXL-eval/cira-amu-m2ndp-spectrum/paper-input-candidates.json`
- Create runtime artifact:
  `/mnt/disk0/gem5-CXL-eval/cira-amu-m2ndp-spectrum/paper-input-discovery.json`
- Create runtime artifact:
  `/mnt/disk0/gem5-CXL-eval/cira-amu-m2ndp-spectrum/paper-input-record.template.json`
- Conditionally create runtime artifact:
  `/mnt/disk0/gem5-CXL-eval/cira-amu-m2ndp-spectrum/paper-input-record.json`

- [ ] **Step 1: Recompute g20 identity**

Run:

```bash
stat -c '%n %s' /home/victoryang00/gem5-CXL/.worktrees/gapbs-latency-table/m5out/gapbs_graphs/g20.sg
sha256sum /home/victoryang00/gem5-CXL/.worktrees/gapbs-latency-table/m5out/gapbs_graphs/g20.sg
python3 -m json.tool /mnt/disk0/gem5-CXL-eval/pr-scaling-120b389653d8/graphs/g20.manifest.json
```

Expected: size `133986161`, SHA-256
`ce900a7147a073835a7450e8f1afedf9f13db6833652bf2f9647819be26bedb3`,
scale 20, 1,048,576 vertices, and 31,399,382 directed edges. Drift stops this
task.

- [ ] **Step 2: Inspect candidate trees without promoting them**

Use `stat`, `sha256sum`, `git rev-parse HEAD`, and `git status --porcelain` as
appropriate on:

```text
/home/victoryang00/CXLMemSim/workloads/mcf
/home/victoryang00/CXLMemSim/workloads/lulesh
/home/victoryang00/CXLMemUring/bench/mcf
/home/victoryang00/CXLMemUring/bench/npb/NPB3.4
/home/5iri/cxl_baseline_benchmarks/workloads/npb
```

Record a path only for its proven role. A binary is not MCF input, and a
LULESH source tree is not a formal scatter data/index pair.

- [ ] **Step 3: Write the partial candidate record**

Use `apply_patch` to create `paper-input-candidates.json` with the exact six
workload keys. Populate the verified g20 values. Populate other fields only
when file role, class, and allocated size are proven. Omit unresolved fields;
do not insert guessed paths or hashes.

- [ ] **Step 4: Generate discovery and template records**

Run:

```bash
python3 scripts/audit_cross_system_input_record.py \
  --candidate-record /mnt/disk0/gem5-CXL-eval/cira-amu-m2ndp-spectrum/paper-input-candidates.json \
  --discovery-output /mnt/disk0/gem5-CXL-eval/cira-amu-m2ndp-spectrum/paper-input-discovery.json \
  --template-output /mnt/disk0/gem5-CXL-eval/cira-amu-m2ndp-spectrum/paper-input-record.template.json
python3 -m json.tool /mnt/disk0/gem5-CXL-eval/cira-amu-m2ndp-spectrum/paper-input-discovery.json
```

Expected: `ready_for_freeze` only if every binding validates; otherwise
`incomplete` with exact missing fields. Neither output says `accepted`.

- [ ] **Step 5: Cross the accepted boundary only with six valid workloads**

If and only if discovery says `ready_for_freeze`, use `apply_patch` to place
the complete candidate content at `paper-input-record.json`, then run:

```bash
python3 scripts/freeze_cross_system_inputs.py \
  --paper-input-record /mnt/disk0/gem5-CXL-eval/cira-amu-m2ndp-spectrum/paper-input-record.json \
  --graph-manifest /mnt/disk0/gem5-CXL-eval/pr-scaling-120b389653d8/graphs/g4.manifest.json \
  --graph-manifest /mnt/disk0/gem5-CXL-eval/pr-scaling-120b389653d8/graphs/g12.manifest.json \
  --graph-manifest /mnt/disk0/gem5-CXL-eval/pr-scaling-120b389653d8/graphs/g14.manifest.json \
  --graph-manifest /mnt/disk0/gem5-CXL-eval/pr-scaling-120b389653d8/graphs/g20.manifest.json \
  --output /mnt/disk0/gem5-CXL-eval/cira-amu-m2ndp-spectrum/shared/inputs.json
```

Expected when complete: exit 0 and `shared/inputs.json` says `accepted`.
Expected with missing bindings: do not create `paper-input-record.json`;
retain `shared/failed-input.json` and report discovery reasons.

### Task 6: Verify and run fresh g12 qualification

**Files:**
- Verify repository code and runtime evidence.
- Create: `/mnt/disk0/gem5-CXL-eval/pr-offload-formal-${formal_commit}-r8/`

- [ ] **Step 1: Run complete Python gates**

Run:

```bash
python3 -m compileall -q scripts tests/pyunit
python3 -m unittest discover -s tests/pyunit/m2ndp -p 'test_*.py' -v
python3 -m unittest discover -s tests/pyunit/cross_system -p 'test_*.py' -v
git diff --check
git status --short --branch
```

Expected: zero failures, only documented skips, and the unrelated
`src/mem/cache/base.cc` remains unstaged.

- [ ] **Step 2: Launch a never-reused qualification root**

Resolve a task-specific source identity and run:

```bash
formal_commit=$(git rev-parse --short=10 HEAD)
python3 scripts/run_pr_asymmetric_offload.py \
  --inputs /mnt/disk0/gem5-CXL-eval/pr-scaling-120b389653d8/inputs.json \
  --root "/mnt/disk0/gem5-CXL-eval/pr-offload-formal-${formal_commit}-r8" \
  --gem5 build/X86/gem5.opt \
  --m5-library util/m5/build/x86/out/libm5.a \
  --config configs/example/gem5_library/x86-gapbs-amu-se.py \
  --calibration /mnt/disk0/gem5-CXL-eval/pr-offload-calibration-cae49a9b50/amu-cira.json \
  --policy /mnt/disk0/gem5-CXL-eval/pr-offload-builds-4e9b85871d/policy.json \
  --cxlmemuring /home/victoryang00/CXLMemUring \
  --m2ndp-root /mnt/disk0/M2NDP-public \
  --variants-build-root /mnt/disk0/gem5-CXL-eval/pr-offload-builds-4e9b85871d/variants \
  --stop-after qualification
```

Expected: four primary and four replay points pass; AMU/CIRA remain bounded;
M2NDP reports about `2.6342721382x`, has `maximum: null`, and is not an
offender.

- [ ] **Step 3: Independently verify proof fields**

Load `qualification.json`, call `contract.validate_point` for every primary
and replay row, call `runner.validate_replay`, recompute speedups with
`Decimal`, and assert:

```python
assert qualification["performance_gate"]["status"] == "passed"
assert qualification["performance_gate"]["offenders"] == []
assert qualification["performance_gate"]["policies"]["m2ndp"]["maximum"] is None
assert qualification["primary"]["g12:m2ndp"]["funcsim"]["mismatched"] == 0
assert (
    qualification["primary"]["g12:m2ndp"]["funcsim"]["completed_at_seq"]
    < qualification["primary"]["g12:m2ndp"]["ndpsim_started_at_seq"]
)
```

Expected: assertions pass, primary/replay raw hashes match, and native timings
are identical per system.

- [ ] **Step 4: Push the verified branch**

Run:

```bash
git diff --check
git status --short
git push origin m2ndp-g20-pr-spmv
```

Expected: remote reaches local HEAD; runtime evidence and
`src/mem/cache/base.cc` remain unstaged.

## Completion evidence

- M2NDP `2.6342721382289415x` passes a recorded minimum-only policy.
- M2NDP remains bit-exact with FuncSim completion before NDPSim.
- AMU and CIRA retain inclusive `1.4x--1.6x` policies.
- All gate consumers and publishers recompute the same shared policy.
- The g20 path, size, metadata, and SHA-256 are live-verified.
- Discovery names every unresolved formal workload binding.
- Candidate/template records cannot be mistaken for accepted evidence.
- Fresh primary/replay qualification passes in a source-bound r8 root.
- The branch is pushed without the unrelated cache change.
