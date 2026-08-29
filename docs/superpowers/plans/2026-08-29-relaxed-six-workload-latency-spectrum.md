# Relaxed Six-Workload CXL Latency Spectrum Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run and publish the real 96-coordinate AMU/CIRA/M2NDP CXL latency spectrum while replacing the all-bit-exact gate with workload-native correctness.

**Architecture:** Add an identity-bound `native-verified` correctness policy to the existing breadth state machine, propagate it through the four-latency orchestrator and publisher, then rebuild the frozen prepared suite under the new code identity. Run the four immutable campaigns in a resumable background service and publish only after all 96 real timing coordinates pass correctness and uncertainty gates.

**Tech Stack:** Python 3 `unittest`, gem5 X86 timing simulation, M2NDP FuncSim/NDPSim, content-addressed JSON/CSV evidence, Matplotlib, systemd, LaTeX.

---

## File map

- Modify `scripts/run_cira_amu_m2ndp_breadth.py`: define and enforce the identity-bound correctness policy.
- Modify `scripts/run_matched_breadth_gem5.py`: expose native and numerical verification fields in replay evidence.
- Modify `scripts/m2ndp_workload_trace.py`: expose the same correctness fields while retaining launch, memory-match, and calibration checks.
- Modify `scripts/run_cira_amu_m2ndp_latency_spectrum.py`: bind `native-verified` into aggregate and child commands.
- Modify `scripts/generate_cira_amu_m2ndp_latency_spectrum.py`: accept the relaxed evidence contract and record per-row verification strength.
- Modify focused tests under `tests/pyunit/cross_system/`.
- Regenerate the prepared suite under `/mnt/disk0/gem5-CXL-eval/cira-amu-m2ndp-spectrum/shared/prepared-relaxed-20260829/`.
- Run the campaign under `/mnt/disk0/gem5-CXL-eval/cira-amu-m2ndp-spectrum-relaxed-20260829-r1/`.
- Modify `/home/victoryang00/gem5-CXL/6472666535e6f359942ddac6/WIP_jf_asplos.tex` only after publication data is complete.

### Task 1: Add the native-verified breadth correctness policy

**Files:**
- Modify: `scripts/run_cira_amu_m2ndp_breadth.py`
- Modify: `scripts/run_matched_breadth_gem5.py`
- Modify: `scripts/m2ndp_workload_trace.py`
- Test: `tests/pyunit/cross_system/test_run_cira_amu_m2ndp_breadth.py`
- Test: `tests/pyunit/cross_system/test_matched_breadth_gem5.py`
- Test: `tests/pyunit/cross_system/test_m2ndp_workload_trace.py`

- [ ] **Step 1: Write failing relaxed-policy tests**

Add a breadth fixture whose output boundary names, widths, and counts match the
reference, but whose float boundary hash and `mismatched_words` differ. Require
the following evidence fields:

```python
record = {
    "status": "pass",
    "verification": "pass",
    "numeric_verification": "pass",
    "bit_exact": False,
    "compared_words": 16,
    "mismatched_words": 2,
    "nonfinite_words": 0,
    "boundaries": relaxed_boundaries,
    "outputs": {"rank": sha("relaxed-rank")},
    **mechanism("cira"),
}
state = breadth.new_state(
    identity(), specs(), g20_graph_sha256=sha("g20"),
    correctness_policy="native-verified",
)
breadth.record_reference(state, "mcf", reference_boundaries)
breadth.record_functional(state, "mcf", "cira", record)
self.assertEqual(state["correctness_policy"], "native-verified")
```

Add rejection cases for `verification != "pass"`,
`numeric_verification != "pass"`, nonzero `nonfinite_words`, changed boundary
name/width/count, mechanism counter errors, and unbalanced work. Retain a test
proving `correctness_policy="bit-exact"` rejects this same record.

- [ ] **Step 2: Run the focused tests and verify RED**

```bash
python3 -m unittest \
  tests.pyunit.cross_system.test_run_cira_amu_m2ndp_breadth \
  tests.pyunit.cross_system.test_matched_breadth_gem5 \
  tests.pyunit.cross_system.test_m2ndp_workload_trace -v
```

Expected: the new state argument and relaxed record fail because the current
implementation still requires bit identity and equal boundary hashes.

- [ ] **Step 3: Implement policy-bound validation**

Add the exact policy set and state field:

```python
CORRECTNESS_POLICIES = ("bit-exact", "native-verified")

def _correctness_policy(value):
    if value not in CORRECTNESS_POLICIES:
        raise BreadthError(f"unsupported correctness policy: {value}")
    return value
```

Extend `new_state(..., correctness_policy="bit-exact")`. In
`record_functional`, always require equal boundary names, `word_bits`, and
counts. Require equal hashes, `bit_exact is True`, and zero mismatches only for
`bit-exact`. For `native-verified`, require:

```python
if (
    normalized.get("verification") != "pass"
    or normalized.get("numeric_verification") != "pass"
    or normalized.get("nonfinite_words") != 0
):
    raise BreadthError("workload-native numerical verification failed")
```

Keep `_validate_mechanism` unchanged. Apply the same policy in timing-window
validation. Extend both backend evidence producers to record
`verification`, `numeric_verification`, `bit_exact`, `mismatched_words`, and
`nonfinite_words`; do not weaken gem5 topology or M2NDP launch, memory-match,
or calibration validation.

- [ ] **Step 4: Run the focused tests and verify GREEN**

Run the Step 2 command. Expected: all tests pass, including strict-policy
regressions and relaxed-policy failure cases.

- [ ] **Step 5: Commit the correctness policy**

```bash
git add scripts/run_cira_amu_m2ndp_breadth.py \
  scripts/run_matched_breadth_gem5.py scripts/m2ndp_workload_trace.py \
  tests/pyunit/cross_system/test_run_cira_amu_m2ndp_breadth.py \
  tests/pyunit/cross_system/test_matched_breadth_gem5.py \
  tests/pyunit/cross_system/test_m2ndp_workload_trace.py
git commit -m "feat: allow native-verified breadth evidence"
```

### Task 2: Propagate the policy through aggregation and publication

**Files:**
- Modify: `scripts/run_cira_amu_m2ndp_latency_spectrum.py`
- Modify: `scripts/generate_cira_amu_m2ndp_latency_spectrum.py`
- Test: `tests/pyunit/cross_system/test_run_cira_amu_m2ndp_latency_spectrum.py`
- Test: `tests/pyunit/cross_system/test_generate_cira_amu_m2ndp_latency_spectrum.py`

- [ ] **Step 1: Write failing identity and publisher tests**

Assert that aggregate state, aggregate identity, every child command, each
`SpectrumRow`, canonical CSV/JSON, and the publication manifest record
`correctness_policy="native-verified"`. Add tamper cases proving a strict child
cannot be mixed into a relaxed aggregate and that `verification="failed"`
cannot be published even when timing is complete.

- [ ] **Step 2: Run the focused tests and verify RED**

```bash
python3 -m unittest \
  tests.pyunit.cross_system.test_run_cira_amu_m2ndp_latency_spectrum \
  tests.pyunit.cross_system.test_generate_cira_amu_m2ndp_latency_spectrum -v
```

Expected: the CLI, identity, and fixed `verification="bit-exact"` row field do
not satisfy the new assertions.

- [ ] **Step 3: Implement propagation and exact publication wording**

Add this CLI to the aggregate runner and forward it to every child:

```python
parser.add_argument(
    "--correctness-policy",
    choices=breadth.CORRECTNESS_POLICIES,
    default="bit-exact",
)
```

Bind the policy into aggregate identity material, state, resume validation,
child commands, and child complete-manifest validation. Replace the frozen
row default with an evidence-derived field:

```python
verification = (
    "bit-exact" if row_evidence.get("bit_exact") is True
    else "native-verified"
)
```

The publisher must still require exactly 96 coordinates, terminal timing,
matched Vanilla recomputation, valid confidence intervals, and content hashes.

- [ ] **Step 4: Run both modules and the complete cross-system unit suite**

```bash
python3 -m unittest \
  tests.pyunit.cross_system.test_run_cira_amu_m2ndp_latency_spectrum \
  tests.pyunit.cross_system.test_generate_cira_amu_m2ndp_latency_spectrum -v
python3 -m unittest discover -s tests/pyunit/cross_system -p 'test_*.py' -v
```

Expected: zero failures.

- [ ] **Step 5: Commit policy propagation**

```bash
git add scripts/run_cira_amu_m2ndp_latency_spectrum.py \
  scripts/generate_cira_amu_m2ndp_latency_spectrum.py \
  tests/pyunit/cross_system/test_run_cira_amu_m2ndp_latency_spectrum.py \
  tests/pyunit/cross_system/test_generate_cira_amu_m2ndp_latency_spectrum.py
git commit -m "feat: publish native-verified latency spectrum"
```

### Task 3: Rebuild and preflight the frozen six-workload suite

**Files:**
- Read: `/mnt/disk0/gem5-CXL-eval/cira-amu-m2ndp-spectrum/shared/inputs.json`
- Create: `/mnt/disk0/gem5-CXL-eval/cira-amu-m2ndp-spectrum/shared/prepared-relaxed-20260829/`

- [ ] **Step 1: Verify inputs, calibration, qualification, storage, and binaries**

```bash
python3 scripts/freeze_cross_system_inputs.py \
  --paper-input-record /mnt/disk0/gem5-CXL-eval/cira-amu-m2ndp-spectrum/paper-input-record.json \
  --graph-manifest /mnt/disk0/gem5-CXL-g14-eval/graphs/g4.manifest.json \
  --graph-manifest /mnt/disk0/gem5-CXL-g14-eval/graphs/g12.manifest.json \
  --graph-manifest /mnt/disk0/gem5-CXL-g14-eval/graphs/g14.manifest.json \
  --graph-manifest /mnt/disk0/gem5-CXL-g14-eval/graphs/g20.manifest.json \
  --output /mnt/disk0/gem5-CXL-eval/cira-amu-m2ndp-spectrum/shared/inputs.relaxed-20260829.json
sha256sum \
  /mnt/disk0/gem5-CXL-eval/pr-offload-calibration-cae49a9b50/amu-cira.json \
  /mnt/disk0/gem5-CXL-eval/pr-offload-formal-a1e45e2d79-r13/qualification.json
df -h /mnt/disk0
```

Expected: accepted six-workload input record, calibration hash matching the
qualification identity, present simulator binaries, and adequate storage. A
failed input record stops execution.

- [ ] **Step 2: Rebuild under a new immutable prepared root**

```bash
python3 scripts/build_matched_breadth_workloads.py \
  --formal \
  --inputs /mnt/disk0/gem5-CXL-eval/cira-amu-m2ndp-spectrum/shared/inputs.relaxed-20260829.json \
  --outdir /mnt/disk0/gem5-CXL-eval/cira-amu-m2ndp-spectrum/shared/prepared-relaxed-20260829
```

Expected: schema-1 `status=verified`, exactly six workloads, four functional
systems, four timing systems, four threads, and all-CXL placement.

- [ ] **Step 3: Run a no-timing preflight against all shared hashes**

Invoke the spectrum runner's validation functions from its unit-test fixture
and verify the new prepared manifest, qualification, and calibration records
without creating an aggregate state. Expected: no identity or schema error.

### Task 4: Run all 96 real timing coordinates in the background

**Files:**
- Create: `/mnt/disk0/gem5-CXL-eval/cira-amu-m2ndp-spectrum-relaxed-20260829-r1/`

- [ ] **Step 1: Launch one resumable systemd service**

```bash
systemd-run \
  --unit=cira-relaxed-spectrum-20260829-r1 \
  --collect \
  --property=WorkingDirectory=/home/victoryang00/gem5-CXL/.worktrees/m2ndp-g20-pr-spmv \
  /usr/bin/env \
  PATH=/mnt/disk0/gem5-CXL-eval/toolchains/m2ndp-conan1/bin:/opt/miniconda3/envs/infer_machine/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
  /usr/bin/python3 scripts/run_cira_amu_m2ndp_latency_spectrum.py \
  --inputs /mnt/disk0/gem5-CXL-eval/cira-amu-m2ndp-spectrum/shared/inputs.relaxed-20260829.json \
  --prepared /mnt/disk0/gem5-CXL-eval/cira-amu-m2ndp-spectrum/shared/prepared-relaxed-20260829/manifest.json \
  --qualification /mnt/disk0/gem5-CXL-eval/pr-offload-formal-a1e45e2d79-r13/qualification.json \
  --calibration /mnt/disk0/gem5-CXL-eval/pr-offload-calibration-cae49a9b50/amu-cira.json \
  --correctness-policy native-verified \
  --root /mnt/disk0/gem5-CXL-eval/cira-amu-m2ndp-spectrum-relaxed-20260829-r1
```

Expected: the service becomes active and writes aggregate `state.json` plus a
latency-local `spectrum-driver.log`.

- [ ] **Step 2: Validate progress without treating partial state as publication**

```bash
systemctl status cira-relaxed-spectrum-20260829-r1 --no-pager
jq '{status, correctness_policy, latencies}' \
  /mnt/disk0/gem5-CXL-eval/cira-amu-m2ndp-spectrum-relaxed-20260829-r1/state.json
```

If interrupted, relaunch the same command with `--resume`; resume must reject
any code, input, calibration, prepared, policy, or latency identity drift.

- [ ] **Step 3: Accept only a complete 96-coordinate aggregate**

```bash
jq -e '.status == "complete" and .coordinate_count == 96 and .correctness_policy == "native-verified"' \
  /mnt/disk0/gem5-CXL-eval/cira-amu-m2ndp-spectrum-relaxed-20260829-r1/complete.json
```

Expected: true and four complete child manifests. Any failed or inconclusive
coordinate remains a blocker rather than being estimated.

### Task 5: Publish, update `WIP_jf_asplos.tex`, verify, and push

**Files:**
- Generate: `/mnt/disk0/gem5-CXL-eval/cira-amu-m2ndp-spectrum-relaxed-20260829-r1/publication/`
- Modify: `/home/victoryang00/gem5-CXL/6472666535e6f359942ddac6/WIP_jf_asplos.tex`
- Add: paper `data/cira-amu-m2ndp-latency-spectrum*`
- Add: paper `fig/cira-amu-m2ndp-latency-spectrum.{pdf,svg,png}`

- [ ] **Step 1: Generate the canonical raw package and figures**

```bash
python3 scripts/generate_cira_amu_m2ndp_latency_spectrum.py \
  --complete /mnt/disk0/gem5-CXL-eval/cira-amu-m2ndp-spectrum-relaxed-20260829-r1/complete.json \
  --outdir /mnt/disk0/gem5-CXL-eval/cira-amu-m2ndp-spectrum-relaxed-20260829-r1/publication
```

Expected: 96-row CSV/JSON, hash manifest, 1-us grouped comparison, 2-by-3
latency spectrum, and six standalone figures in PDF/SVG/PNG.

- [ ] **Step 2: Independently recompute and visually inspect**

Regenerate into a fresh temporary directory, compare CSV/JSON values and
artifact hashes, render all PDFs to PNG, and inspect at original detail. The
2-by-3 figure must have shared speedup limits, all four latency labels, visible
1.0x references, readable CI whiskers, and non-color line/marker distinction.

- [ ] **Step 3: Update only the intended ASPLOS draft**

Copy hash-verified raw data and figure exports into the paper repository.
Replace the preliminary 1-us-only scaling block in `WIP_jf_asplos.tex` with
the 2-by-3 spectrum, exact values derived from the CSV, and this caption
contract:

```text
All points are real matched timing measurements and pass workload-native
correctness, mechanism-balance, four-core/all-CXL identity, and paired-CI
gates; bit-exact output was recorded where available but was not required for
every coordinate.
```

- [ ] **Step 4: Compile without overwriting the user's existing WIP PDF**

Build with a temporary job name and output directory. If the pre-existing
line-1115 `Missing $ inserted` block remains, validate the modified evaluation
section with the same temporary `\iffalse` wrapper used for the prior WIP
handoff and report that unrelated error separately. Inspect the table and
figure pages.

- [ ] **Step 5: Run final tests and push both repositories**

```bash
python3 -m unittest discover -s tests/pyunit/cross_system -p 'test_*.py' -v
git diff --check
git push origin m2ndp-g20-pr-spmv
git -C /home/victoryang00/gem5-CXL/6472666535e6f359942ddac6 push origin master
```

Expected: both remote heads equal local heads, unrelated
`src/mem/cache/base.cc`, `.superpowers/`, WIP PDFs, and prior GAPBS evidence
remain unstaged.
