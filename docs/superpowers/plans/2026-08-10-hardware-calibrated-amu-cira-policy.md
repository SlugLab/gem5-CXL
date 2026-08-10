# Hardware-Calibrated AMU and CIRA Policy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build hash-bound AMU-paper and CIRA-PGO calibration models, apply them to the four-thread all-CXL PageRank experiment, and publish only bit-exact calibrated latency results.

**Architecture:** A pure-Python calibration layer owns source parsing, source classification, parameter manifests, fitting, and policy decisions. gem5 ASMC gains only the finite resources, timing costs, and statistics required by the AMU paper model; CIRA keeps its coherent timing datapath and receives a causal analytical policy gate. The g12 qualification freezes the calibrated parameters, while g14 consumes the immutable manifest without refitting.

**Tech Stack:** Python 3 standard library, gem5 Python SimObjects, C++17 gem5 memory objects, generated C++ GAPBS headers, `unittest`, SCons, CSV/JSON/SHA-256 provenance, Matplotlib, LaTeX.

---

## File map

- Create `scripts/amu_cira_calibration.py`: immutable AMU PDF and CIRA CSV parsing, observation classification, fitting, and manifest generation.
- Create `scripts/cira_hoist_model.py`: legal/profitable hoist checks and causal static/PGO/few-shot selectors.
- Create `scripts/run_amu_paper_calibration.py`: execute or consume AMU paper-profile proxy runs, fit unreported control costs, and emit residuals.
- Create `util/amu/amu_paper_profile.cc`: deterministic GUPS, HJ, and STREAM proxy kernels with paper-described sizes/granularities where specified.
- Create `tests/pyunit/amu/test_amu_cira_calibration.py`: source/hash/schema/fit tests.
- Create `tests/pyunit/amu/test_cira_hoist_model.py`: analytical and causal-selection tests.
- Create `tests/pyunit/amu/test_asmc_paper_model.py`: source-level finite-resource and timing-stat tests.
- Create `tests/pyunit/m2ndp/test_calibrated_g12_g14_contract.py`: orchestration and publication provenance tests.
- Modify `src/mem/ASMC.py`: expose paper-model resource and timing parameters.
- Modify `src/mem/asmc.hh`: hold finite pending/ID-batch state and new statistics.
- Modify `src/mem/asmc.cc`: charge metadata/ID/completion paths and record MLP/polling/window statistics.
- Modify `configs/example/gem5_library/x86-gapbs-amu-se.py`: wire calibrated AMU arguments and refuse inconsistent profiles.
- Modify `scripts/build_gapbs_amu_cxlmemuring.py`: preserve a rolling async window and expose batch/window parameters.
- Modify `scripts/build_gapbs_matched_pr_spmv_variants.py`: bind AMU profile and CIRA mode to generated source and build manifest.
- Modify `scripts/cira_lead_policy.py`: route lead choice through the legal/profitable policy model.
- Modify `scripts/run_gapbs_g12_qualification.py`: calibrate, run all CIRA modes, freeze the manifest, and enforce bit-exact gates.
- Modify `scripts/run_gapbs_g14_4thread_latency_sweep.py`: consume the frozen manifest and reject stale checkpoints/results.
- Modify `scripts/generate_gapbs_g14_4thread_latency_results.py`: publish calibrated labels and manifest hashes.
- Modify `scripts/validate_gapbs_g14_4thread_latency_results.py`: validate calibration, per-mode activity, and bit equality.
- Modify `scripts/generate_gapbs_g14_4thread_latency_figure.py`: plot AMU paper-calibrated and all three CIRA modes.
- Modify `docs/amu-gapbs-benchmark.md`: document exact calibration and rerun commands.
- Modify the paper's existing `gapbs-vtune-cxl-table.tex` after locating its tracked path with `rg --files`.

### Task 1: Parse and bind both calibration sources

**Files:**
- Create: `scripts/amu_cira_calibration.py`
- Create: `tests/pyunit/amu/test_amu_cira_calibration.py`

- [ ] **Step 1: Write failing source and classification tests**

```python
class CalibrationSourceTest(unittest.TestCase):
    def test_amu_source_hash_and_direct_parameters(self):
        facts = calibration.load_amu_source(PDF)
        self.assertEqual(facts["sha256"], calibration.AMU_PDF_SHA256)
        self.assertEqual(facts["direct"]["spm_bytes"], 64 * 1024)
        self.assertEqual(facts["direct"]["pending_entries"], 32)
        self.assertEqual(facts["direct"]["id_batch_entries"], 32)
        self.assertEqual(facts["direct"]["latency_us"], [0.1, 0.2, 0.5, 1, 2, 5])
        self.assertEqual(facts["validation"]["gups_5us_min_mlp"], 130)

    def test_cira_excludes_failed_pr_and_preserves_fallbacks(self):
        facts = calibration.load_cira_source(CSV)
        self.assertNotIn("pr", facts["verified_workloads"])
        self.assertEqual(facts["primary"]["workload"], "pr_spmv")
        self.assertEqual(facts["primary"]["pgo_over_static"], 1.004128673)
        self.assertEqual(facts["rows"]["bfs"]["B"]["selected_from"], "")
        self.assertIn("fell back", facts["rows"]["bfs"]["B"]["fallback"])
```

- [ ] **Step 2: Run tests and verify the module is missing**

Run: `python3 -m unittest tests.pyunit.amu.test_amu_cira_calibration -v`

Expected: `ImportError: cannot import name 'amu_cira_calibration'`.

- [ ] **Step 3: Implement immutable source parsing**

```python
AMU_PDF_SHA256 = "cba178ece7593b3ede868417a031ded3efddd85d5f7c50672b0a93735187790f"
CIRA_CSV_SHA256 = "4e0297da423cee0a742bc2e10656d022bb27776807f2d2ce4cca43e65c634184"

def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()

def require_hash(path, expected, label):
    actual = sha256_file(path)
    if actual != expected:
        raise CalibrationError(f"{label} SHA-256 {actual} != {expected}")
    return actual

def load_amu_source(path):
    digest = require_hash(path, AMU_PDF_SHA256, "AMU PDF")
    return {
        "path": str(Path(path).resolve()), "sha256": digest,
        "direct": {
            "clock_ghz": 3, "issue_width": 6, "rob_entries": 512,
            "physical_registers": 512, "lsq_entries": 192,
            "l1_bytes": 32 * 1024, "l1_mshrs": 48, "l1_cycles": 4,
            "l2_bytes": 256 * 1024, "l2_mshrs": 48, "l2_cycles": 10,
            "spm_bytes": 64 * 1024, "pending_entries": 32,
            "id_bits": 16, "id_vector_bits": 512,
            "id_batch_entries": 32,
            "latency_us": [0.1, 0.2, 0.5, 1, 2, 5],
        },
        "validation": {
            "mean_speedup_1us": 2.42, "gups_speedup_5us": 26.86,
            "gups_5us_min_mlp": 130,
            "table4": AMU_TABLE4,
        },
    }
```

Parse CSV rows with `csv.DictReader`, require ten trials, convert numeric fields, retain `Fallback`, `Verification`, and confidence intervals, and compute geometric means with `math.exp(sum(math.log(x))/len(x))`. Assert the computed `pr_spmv` and seven-workload ratios with `math.isclose(..., rel_tol=1e-9)` before returning them.

- [ ] **Step 4: Run source tests**

Run: `AMU_PDF=/home/victoryang00/gem5-CXL/3663479.pdf CIRA_CSV=/root/ia780i_type2_delay_buffer_new/benchmark_gapbs_workloads_ci_long.csv python3 -m unittest tests.pyunit.amu.test_amu_cira_calibration -v`

Expected: all source/hash/classification tests pass.

- [ ] **Step 5: Commit source ingestion**

```bash
git add scripts/amu_cira_calibration.py tests/pyunit/amu/test_amu_cira_calibration.py
git commit -m "feat: bind AMU and CIRA calibration sources"
```

### Task 2: Fit AMU control costs without copying paper speedups

**Files:**
- Modify: `scripts/amu_cira_calibration.py`
- Create: `scripts/run_amu_paper_calibration.py`
- Create: `util/amu/amu_paper_profile.cc`
- Modify: `tests/pyunit/amu/test_amu_cira_calibration.py`

- [ ] **Step 1: Write failing fit/holdout tests**

```python
def test_fit_uses_numeric_training_rows_and_reports_holdout(self):
    measurements = calibration.synthetic_measurements_for_test()
    result = calibration.fit_amu_control_costs(
        measurements, holdout={"workload": "stream", "latency_us": 2.0})
    self.assertEqual(result["objective"], "normalized_time_weighted_sse")
    self.assertNotIn("stream@2", result["training_points"])
    self.assertIn("stream@2", result["holdout_residuals"])
    self.assertGreaterEqual(result["parameters"]["metadata_cycles"], 0)
    self.assertGreaterEqual(result["parameters"]["completion_cycles"], 0)

def test_speedup_is_never_a_direct_parameter(self):
    result = calibration.fit_amu_control_costs(
        calibration.synthetic_measurements_for_test(),
        holdout={"workload": "stream", "latency_us": 2.0})
    self.assertNotIn("speedup", result["parameters"])
```

- [ ] **Step 2: Run the fit tests and see the missing function**

Run: `python3 -m unittest tests.pyunit.amu.test_amu_cira_calibration.CalibrationFitTest -v`

Expected: `AttributeError` for `fit_amu_control_costs`.

- [ ] **Step 3: Implement deterministic bounded grid fitting**

Search only declared microarchitectural costs:

```python
AMU_SEARCH_SPACE = {
    "metadata_cycles": (0, 2, 4, 6, 8, 10),
    "id_refill_cycles": (0, 2, 4, 6, 8, 10),
    "completion_cycles": (0, 2, 4, 6, 8, 10),
}

def fit_amu_control_costs(measurements, holdout):
    training, held = split_measurements(measurements, holdout)
    candidates = []
    for values in itertools.product(*AMU_SEARCH_SPACE.values()):
        params = dict(zip(AMU_SEARCH_SPACE, values))
        predictions = [predict_normalized_time(row, params) for row in training]
        error = math.fsum(
            row["weight"] * (pred - row["target"]) ** 2
            for row, pred in zip(training, predictions)
        )
        candidates.append((error, tuple(values), params))
    error, _, selected = min(candidates)
    return build_fit_record(training, held, selected, error)
```

`predict_normalized_time` must use measured baseline work/request counts and
simulated AMU request/queue statistics supplied in each input row. It adds
metadata, amortized ID refill, and completion costs. Empty `getfin` calls are
already executed instructions in gem5 and are measured rather than assigned
a second fitted latency. The predictor must not accept a speedup multiplier.

- [ ] **Step 4: Add deterministic paper-profile proxy kernels**

Implement `amu_paper_profile.cc` with `--workload gups|hj|stream`,
`--iterations`, `--raw-output`, and `--amu`. GUPS uses an HPCC-compatible
64-bit XOR update table, HJ uses 16,000 buckets with 48-byte nodes as stated
in Table 3, and STREAM uses 512-byte AMU operations. Each mode allocates the
measured arrays in the configured far-memory range, surrounds only the kernel
with ROI work events, prints request/update counts, and writes a deterministic
u64 checksum. The baseline and AMU paths execute the same operations in the
same order; only their memory-access mechanism differs.

- [ ] **Step 5: Implement the calibration runner**

`run_amu_paper_calibration.py collect` builds both proxy variants and runs
them at 0.1/0.2/0.5/1/2/5 us using a single-core x86 proxy configured with
the Table 2 width, window, cache, and frequency parameters, 64 KiB SPM, and
fresh per-latency checkpoints. The manifest marks the ISA mismatch from the
paper's RISC-V setup, so Table 4 is a trend/holdout target rather than a claim
of exact reproduction. It writes
`amu-paper-measurements.csv` with baseline/AMU ticks, operation counts, MLP,
queue statistics, checksum, and all input hashes. `fit` accepts
`--measurements`, `--pdf`, `--cira-csv`, `--holdout-workload stream`,
`--holdout-latency 2us`, and `--output`. It verifies source hashes, requires
matching baseline/AMU checksums, fits costs, records Table 4 targets, writes
JSON atomically, and exits nonzero when the held-out latency trend has the
wrong sign, calibrated error exceeds the predeclared proxy bound of 25%, or
GUPS 5 us modeled MLP is not greater than 130.

- [ ] **Step 6: Run tests and a fixture fit**

Run: `python3 -m unittest tests.pyunit.amu.test_amu_cira_calibration -v`

Expected: all tests pass and repeated fits produce byte-identical JSON after excluding the output path.

- [ ] **Step 7: Commit the fitter**

```bash
git add scripts/amu_cira_calibration.py scripts/run_amu_paper_calibration.py util/amu/amu_paper_profile.cc tests/pyunit/amu/test_amu_cira_calibration.py
git commit -m "feat: fit bounded AMU paper control costs"
```

### Task 3: Add paper-described finite AMU resources and statistics

**Files:**
- Modify: `src/mem/ASMC.py`
- Modify: `src/mem/asmc.hh`
- Modify: `src/mem/asmc.cc`
- Create: `tests/pyunit/amu/test_asmc_paper_model.py`

- [ ] **Step 1: Write failing source-contract tests**

```python
def test_paper_resource_parameters_exist(self):
    for token in (
        'pending_queue_entries = Param.Unsigned(32',
        'id_batch_entries = Param.Unsigned(32',
        'metadata_latency = Param.Cycles(10',
        'id_refill_latency = Param.Cycles(0',
        'completion_publish_latency = Param.Cycles(0',
    ):
        self.assertIn(token, ASMC_PY)

def test_completion_and_polling_stats_are_recorded(self):
    for token in (
        "outstandingIntegral", "maxObservedOutstanding",
        "pendingQueueFull", "idBatchRefills", "metadataAccesses",
        "emptyGetfinPolls", "successfulGetfin", "consumerWaitTicks",
    ):
        self.assertIn(token, HEADER + SOURCE)
```

- [ ] **Step 2: Run tests and verify the parameters are absent**

Run: `python3 -m unittest tests.pyunit.amu.test_asmc_paper_model -v`

Expected: failures naming `pending_queue_entries` and new stats.

- [ ] **Step 3: Add SimObject parameters and C++ state**

Add these exact parameters to `ASMC.py`:

```python
pending_queue_entries = Param.Unsigned(32, "Per-state-machine pending entries")
id_batch_entries = Param.Unsigned(32, "16-bit IDs held by one 512-bit batch")
metadata_latency = Param.Cycles(10, "SPM metadata access latency")
id_refill_latency = Param.Cycles(0, "List-vector ID refill latency")
completion_publish_latency = Param.Cycles(0, "Finished-ID publication latency")
```

In `asmc.hh`, add per-stage `metadataPending` and `completionPending` queues,
`pendingQueueEntries`, `idBatchEntries`, the three cycle costs,
`idsRemaining`, `lastOccupancyTick`, `pollWaitStart`, and
`updateOccupancyIntegral()`. Add
scalar stats for every token tested above. The SPM-backed `outstanding` map
remains bounded by `queue_length`/`maxOutstanding`, not by 32.

- [ ] **Step 4: Charge resources at causal events**

In `issue()`, enqueue metadata work into its 32-entry internal service queue;
backpressure new admission only while that queue is full. Once metadata service
finishes, the request moves into AMART/outstanding state and frees the pending
entry. Refill IDs only when `idsRemaining == 0`; charge metadata plus optional
refill delay before the first packet; decrement the batch once per accepted
operation. In `recvTimingResp()`, enqueue completion work in a separate
32-entry service queue, charge metadata before SPM writeback, and charge
completion publication before adding the finished ID. In `getFinished()`,
count empty/successful calls; when an empty poll occurs with live requests,
record its first tick per thread and close that wait interval when a finished
ID is returned. The pseudo-instruction itself continues to consume normal CPU
execution cycles, so no second artificial poll delay is added. Update the
occupancy integral at every insertion/removal and record the maximum. Reset
every new queue and state member in `reset()`.

- [ ] **Step 5: Run source tests and build gem5**

Run: `python3 -m unittest tests.pyunit.amu.test_asmc_paper_model tests.pyunit.amu.test_asmc_coherent_spm_writeback -v`

Run: `scons build/X86/gem5.opt -j2`

Expected: both suites pass and SCons finishes with exit code 0.

- [ ] **Step 6: Commit the finite-resource model**

```bash
git add src/mem/ASMC.py src/mem/asmc.hh src/mem/asmc.cc tests/pyunit/amu/test_asmc_paper_model.py
git commit -m "feat: model paper-calibrated AMU control resources"
```

### Task 4: Wire AMU profiles and remove per-request synchronous drains

**Files:**
- Modify: `configs/example/gem5_library/x86-gapbs-amu-se.py`
- Modify: `scripts/build_gapbs_amu_cxlmemuring.py`
- Modify: `scripts/build_gapbs_matched_pr_spmv_variants.py`
- Modify: `tests/pyunit/amu/test_compare_gapbs_cxl_amu_cira.py`
- Modify: `tests/pyunit/amu/test_asmc_paper_model.py`

- [ ] **Step 1: Write failing profile and rolling-window tests**

Require `--asmc-calibration-manifest`, 64 KiB SPM for `paper-calibrated`, manifest-selected cycle costs, and a generated AMU `Window` whose `submit()` never calls `drain()` unless capacity is full. Require `load_value()` to be absent from the transformed `pr_spmv` pull loop.

- [ ] **Step 2: Run the targeted tests**

Run: `python3 -m unittest tests.pyunit.amu.test_asmc_paper_model tests.pyunit.amu.test_compare_gapbs_cxl_amu_cira -v`

Expected: failures for the missing manifest option and rolling submit/consume API.

- [ ] **Step 3: Implement strict config wiring**

Load the calibration JSON before constructing ASMC. Verify its source hashes and require:

```python
if args.asmc_profile == "paper-calibrated":
    if args.asmc_spm_size != "64KiB":
        raise ValueError("paper-calibrated AMU requires 64KiB SPM")
    amu = calibration["amu"]["formal_profile"]
    args.asmc_pending_queue_entries = 32
    args.asmc_id_batch_entries = 32
    args.asmc_metadata_latency = amu["metadata_cycles"]
```

Pass every value into `ASMC(...)`; print the manifest SHA-256 and selected profile in `config.ini`-visible SimObject parameters.

- [ ] **Step 4: Implement producer/consumer rolling batches**

Replace the generated batch helper with `AsyncWindow<T>` that keeps `head`, `tail`, `submitted`, and ID-to-slot mapping. `submit(addr)` fills free slots until `GAPBS_AMU_WINDOW_SIZE`; `consume_next()` polls completions until the oldest program-order slot is ready, copies its value, and releases it. The two-phase PageRank dependency remains: submit node IDs, consume nodes in order to form score addresses, submit scores as slots free, then consume scores in original edge order. No result addition may occur out of order.

- [ ] **Step 5: Verify generated source contracts**

Run:

```bash
python3 -m unittest tests.pyunit.amu.test_compare_gapbs_cxl_amu_cira tests.pyunit.amu.test_asmc_paper_model -v
```

Expected: generated-source assertions pass, including absence of a
per-request drain and preservation of program-order accumulation. The runtime
bit-exact proof is performed once in Task 9 after all policy plumbing is bound.

- [ ] **Step 6: Commit AMU integration**

```bash
git add configs/example/gem5_library/x86-gapbs-amu-se.py scripts/build_gapbs_amu_cxlmemuring.py scripts/build_gapbs_matched_pr_spmv_variants.py tests/pyunit/amu/test_compare_gapbs_cxl_amu_cira.py tests/pyunit/amu/test_asmc_paper_model.py
git commit -m "feat: apply calibrated AMU rolling windows"
```

### Task 5: Implement the CIRA analytical hoist gate

**Files:**
- Create: `scripts/cira_hoist_model.py`
- Create: `tests/pyunit/amu/test_cira_hoist_model.py`

- [ ] **Step 1: Write failing legality and profitability tests**

```python
def test_unsafe_alias_fails_before_profit(self):
    result = model.evaluate(candidate(alias_safe=False), resources())
    self.assertFalse(result.legal)
    self.assertEqual(result.reason, "unsafe-alias")

def test_slack_and_net_benefit_are_both_required(self):
    self.assertEqual(model.evaluate(candidate(slack_ns=900), resources()).reason,
                     "insufficient-slack")
    self.assertEqual(model.evaluate(candidate(saved_ns=1200, extra_ns=1300),
                                    resources()).reason,
                     "non-positive-benefit")

def test_capacity_failure_leaves_demand_synchronous(self):
    result = model.evaluate(candidate(), resources(mshrs_free=0))
    self.assertFalse(result.emit_prefetch)
    self.assertEqual(result.reason, "capacity")
```

- [ ] **Step 2: Run tests and verify the module is missing**

Run: `python3 -m unittest tests.pyunit.amu.test_cira_hoist_model -v`

Expected: import failure.

- [ ] **Step 3: Implement typed immutable decisions**

Use frozen dataclasses `HoistCandidate`, `ResourceState`, and `HoistDecision`. Evaluate in this exact order: dominance/guard availability, alias safety, invalidation/lifetime safety, capacity, slack, then positive net benefit. Return all cost terms and the first failure reason. Reject negative durations, capacities, or probabilities with `PolicyError`.

- [ ] **Step 4: Add static, PGO, and causal few-shot selectors**

`select_static()` returns only its declared fixed candidate. `select_pgo()` may inspect only the source manifest's completed A/B/C rows. `FewShotSelector.observe()` accepts samples sequentially, accumulates charged profiling ticks, and `freeze()` selects the lowest observed mean only after every candidate has the declared sample count. Calling `select()` before freeze or observing after freeze raises `PolicyError`.

- [ ] **Step 5: Run tests and commit**

Run: `python3 -m unittest tests.pyunit.amu.test_cira_hoist_model -v`

Expected: all tests pass.

```bash
git add scripts/cira_hoist_model.py tests/pyunit/amu/test_cira_hoist_model.py
git commit -m "feat: add causal CIRA hoist policy model"
```

### Task 6: Build and freeze the three CIRA modes

**Files:**
- Modify: `scripts/cira_lead_policy.py`
- Modify: `scripts/build_gapbs_matched_pr_spmv_variants.py`
- Modify: `scripts/run_gapbs_g12_qualification.py`
- Modify: `tests/pyunit/amu/test_cira_lead_policy.py`
- Modify: `tests/pyunit/m2ndp/test_run_gapbs_g12_qualification.py`

- [ ] **Step 1: Write failing three-mode qualification tests**

Require actions for `cira-static-1us`, `cira-pgo-selected-1us`, and `cira-few-shot-online-1us`; require PGO source selection `B` for `pr_spmv`; require few-shot profiling ticks to be positive; and require every emitted candidate to contain a successful `HoistDecision`.

- [ ] **Step 2: Run tests and observe the old lead-only contract**

Run: `python3 -m unittest tests.pyunit.amu.test_cira_lead_policy tests.pyunit.m2ndp.test_run_gapbs_g12_qualification -v`

Expected: missing mode/action assertions fail.

- [ ] **Step 3: Extend build manifests**

Add `--cira-mode {static,pgo-selected,few-shot-online}` and `--calibration-manifest`. Map source candidates exactly as `A -> static-default`, `B -> row-window-2048`, and `C -> row-window-1024`; keep those names in the manifest. Convert row windows to 64-row descriptor blocks only after checking divisibility. Record mode, source row, lead, row window, hoist decision, and calibration SHA-256 in each binary manifest.

- [ ] **Step 4: Make g12 qualification causal and fail-closed**

Run Vanilla and calibrated AMU once, then static and PGO-selected. Run few-shot candidate samples only in warmup, add their simulated ticks plus reconfiguration ticks to the few-shot end-to-end total, freeze, then rerun the chosen steady policy. Validate raw hashes after every mode. Reject PGO/static ratio outside `[0.97, 1.04]`, a range declared before execution around the source ratio and broad source confidence intervals; report the exact residual rather than forcing 1.004128673.

- [ ] **Step 5: Run the complete policy/qualification unit tests**

Run:

```bash
python3 -m unittest tests.pyunit.amu.test_cira_lead_policy tests.pyunit.amu.test_cira_hoist_model tests.pyunit.m2ndp.test_run_gapbs_g12_qualification -v
```

Expected: all tests pass; fixture evidence has four active CIRA ports, no
drops/rejections, and positive charged few-shot selection time. The live
four-thread proof is performed in Task 9.

- [ ] **Step 6: Commit CIRA modes**

```bash
git add scripts/cira_lead_policy.py scripts/build_gapbs_matched_pr_spmv_variants.py scripts/run_gapbs_g12_qualification.py tests/pyunit/amu/test_cira_lead_policy.py tests/pyunit/m2ndp/test_run_gapbs_g12_qualification.py
git commit -m "feat: qualify static PGO and few-shot CIRA modes"
```

### Task 7: Bind calibration provenance through g12 and g14

**Files:**
- Modify: `scripts/run_gapbs_g12_qualification.py`
- Modify: `scripts/run_gapbs_g14_4thread_latency_sweep.py`
- Create: `tests/pyunit/m2ndp/test_calibrated_g12_g14_contract.py`

- [ ] **Step 1: Write failing stale-artifact tests**

Test that changing either source hash, fit parameters, simulator hash, binary mode, or checkpoint manifest rejects resume. Test that g14 cannot start without a passed g12 calibration and cannot refit parameters.

- [ ] **Step 2: Run the contract tests**

Run: `python3 -m unittest tests.pyunit.m2ndp.test_calibrated_g12_g14_contract -v`

Expected: failures because calibration hashes are not yet part of action contracts.

- [ ] **Step 3: Add calibration to every action contract**

Include `calibration_manifest`, `amu_profile`, `cira_mode`, `policy_manifest`, and relevant binary/config hashes in `_action_contract()`. Store them in status records and output evidence. A resumed action must recompute and exactly compare every input hash before accepting existing outputs.

- [ ] **Step 4: Freeze g12 and consume in g14**

The g12 output contains selected AMU parameters, all CIRA mode definitions, fit and holdout residuals, source hashes, raw-vector hash, and simulator/config hashes. g14 accepts this record read-only, scales only the already approved latency-dependent lead formula, and creates fresh checkpoints when the calibration hash differs.

- [ ] **Step 5: Run orchestration tests and commit**

Run: `python3 -m unittest tests.pyunit.m2ndp.test_run_gapbs_g12_qualification tests.pyunit.m2ndp.test_run_gapbs_g14_4thread_latency_sweep tests.pyunit.m2ndp.test_calibrated_g12_g14_contract -v`

Expected: all tests pass.

```bash
git add scripts/run_gapbs_g12_qualification.py scripts/run_gapbs_g14_4thread_latency_sweep.py tests/pyunit/m2ndp/test_calibrated_g12_g14_contract.py
git commit -m "feat: freeze calibrated g12 provenance into g14"
```

### Task 8: Validate and publish calibrated modes

**Files:**
- Modify: `scripts/generate_gapbs_g14_4thread_latency_results.py`
- Modify: `scripts/validate_gapbs_g14_4thread_latency_results.py`
- Modify: `scripts/generate_gapbs_g14_4thread_latency_figure.py`
- Modify: `tests/pyunit/m2ndp/test_generate_gapbs_g14_4thread_latency_results.py`
- Modify: `tests/pyunit/m2ndp/test_validate_gapbs_g14_4thread_latency_results.py`
- Modify: `tests/pyunit/m2ndp/test_generate_gapbs_g14_4thread_latency_figure.py`

- [ ] **Step 1: Write failing canonical-schema tests**

Require one row per latency for `vanilla`, `amu-paper-calibrated`, `cira-static`, `cira-pgo-selected`, `cira-few-shot-online`, and `m2ndp`. Require calibration SHA, raw-vector SHA, end-to-end ticks, profiling ticks, and residual fields. Reject legacy generic `AMU`/`CIRA` labels in new calibrated artifacts.

- [ ] **Step 2: Run publication tests and observe schema failures**

Run the three g14 publication test modules.

Expected: missing calibrated series and fields.

- [ ] **Step 3: Extend canonical generation and validation**

Compute speedup only as matched Vanilla end-to-end ticks divided by each system's end-to-end ticks. For few-shot, end-to-end ticks include warmup sampling and reconfiguration. Require identical graph, iterations, cores, all-CXL flag, and raw-vector SHA across host modes; balanced AMU events; four active CIRA ports; zero rejections; and M2NDP FuncSim/calibration PASS.

- [ ] **Step 4: Extend the figure and table labels**

Use separate styles for all five accelerated series and preserve the 200/500/1000/2000 ns x-axis. The table reports latency in milliseconds plus speedup, and a calibration note states AMU PDF DOI/hash and CIRA CSV hash. Do not plot failed, missing, stale, or unverified rows.

- [ ] **Step 5: Run publication tests and commit**

Run:

```bash
python3 -m unittest tests.pyunit.m2ndp.test_generate_gapbs_g14_4thread_latency_results tests.pyunit.m2ndp.test_validate_gapbs_g14_4thread_latency_results tests.pyunit.m2ndp.test_generate_gapbs_g14_4thread_latency_figure -v
```

Expected: all tests pass and fixture generation produces deterministic
SVG/CSV/TeX hashes on two consecutive invocations.

```bash
git add scripts/generate_gapbs_g14_4thread_latency_results.py scripts/validate_gapbs_g14_4thread_latency_results.py scripts/generate_gapbs_g14_4thread_latency_figure.py tests/pyunit/m2ndp/test_generate_gapbs_g14_4thread_latency_results.py tests/pyunit/m2ndp/test_validate_gapbs_g14_4thread_latency_results.py tests/pyunit/m2ndp/test_generate_gapbs_g14_4thread_latency_figure.py
git commit -m "feat: publish calibrated AMU and CIRA modes"
```

### Task 9: Run the calibrated proof ladder

**Files:**
- Modify only generated evidence under `/mnt/disk0/gem5-CXL-g14-eval/calibrated/`; do not commit runtime data.

- [ ] **Step 1: Run the full unit suite**

Run:

```bash
python3 -m unittest discover -s tests/pyunit/amu -p 'test_*.py' -v
python3 -m unittest discover -s tests/pyunit/m2ndp -p 'test_*.py' -v
```

Expected: all tests pass.

- [ ] **Step 2: Rebuild gem5 and m5 library**

Run:

```bash
scons build/X86/gem5.opt -j2
scons build/X86/out/m5 -j2
```

Expected: exit code 0 for both.

- [ ] **Step 3: Generate the immutable calibration manifest**

Run:

```bash
python3 scripts/run_amu_paper_calibration.py collect \
  --gem5 build/X86/gem5.opt \
  --config configs/example/gem5_library/x86-gapbs-amu-se.py \
  --outdir /mnt/disk0/gem5-CXL-g14-eval/calibrated/amu-paper \
  --measurements /mnt/disk0/gem5-CXL-g14-eval/calibrated/amu-paper-measurements.csv
python3 scripts/run_amu_paper_calibration.py fit \
  --measurements /mnt/disk0/gem5-CXL-g14-eval/calibrated/amu-paper-measurements.csv \
  --pdf /home/victoryang00/gem5-CXL/3663479.pdf \
  --cira-csv /root/ia780i_type2_delay_buffer_new/benchmark_gapbs_workloads_ci_long.csv \
  --holdout-workload stream --holdout-latency 2us \
  --output /mnt/disk0/gem5-CXL-g14-eval/calibrated/calibration.json
```

Expected: source hashes match, fit PASS, holdout PASS, and GUPS MLP bound PASS.

- [ ] **Step 4: Run the fresh g12 qualification**

Run:

```bash
python3 scripts/run_gapbs_g12_qualification.py \
  --root /mnt/disk0/gem5-CXL-g14-eval/calibrated \
  --gem5 build/X86/gem5.opt \
  --config configs/example/gem5_library/x86-gapbs-amu-se.py \
  --cxlmemuring /home/victoryang00/CXLMemUring \
  --m5-library util/m5/build/x86/out/libm5.a \
  --calibration-manifest /mnt/disk0/gem5-CXL-g14-eval/calibrated/calibration.json
```

Expected: Vanilla, calibrated AMU, and all three CIRA modes pass bit-exact;
no result is accepted from the pre-calibration qualification.

- [ ] **Step 5: Run the g14 latency sweep in the background**

Launch:

```bash
setsid -f python3 scripts/run_gapbs_g14_4thread_latency_sweep.py \
  --root /mnt/disk0/gem5-CXL-g14-eval/calibrated \
  --gem5 build/X86/gem5.opt \
  --config configs/example/gem5_library/x86-gapbs-amu-se.py \
  --cxlmemuring /home/victoryang00/CXLMemUring \
  --m2ndp-root m5out/m2ndp/source \
  --m5-library util/m5/build/x86/out/libm5.a \
  --calibration-manifest /mnt/disk0/gem5-CXL-g14-eval/calibrated/calibration.json \
  > /mnt/disk0/gem5-CXL-g14-eval/calibrated/g14-sweep.log 2>&1
```

Record the detached PID, command, log, status JSON, and source hashes. Do not
enable live CRIU checkpoint rotation.

- [ ] **Step 6: Validate final evidence**

Run:

```bash
python3 scripts/generate_gapbs_g14_4thread_latency_results.py \
  --sweep-root /mnt/disk0/gem5-CXL-g14-eval/calibrated \
  --output-root /mnt/disk0/gem5-CXL-g14-eval/calibrated/publication
python3 scripts/validate_gapbs_g14_4thread_latency_results.py \
  --publication /mnt/disk0/gem5-CXL-g14-eval/calibrated/publication \
  --sweep-root /mnt/disk0/gem5-CXL-g14-eval/calibrated \
  --output /mnt/disk0/gem5-CXL-g14-eval/calibrated/publication/independent-validation.json
```

Expected: exact row count, all raw-vector hashes equal, AMU/CIRA activity
gates pass, M2NDP FuncSim/NDPSim gates pass, and every result hash matches its
status record.

### Task 10: Install paper artifacts and push

**Files:**
- Modify: `docs/amu-gapbs-benchmark.md`
- Modify: `/home/victoryang00/gem5-CXL/6472666535e6f359942ddac6/gapbs-vtune-cxl-table.tex`
- Modify: `/home/victoryang00/gem5-CXL/6472666535e6f359942ddac6/gapbs-g4-4thread-latency-table-data.tex`
- Modify: `/home/victoryang00/gem5-CXL/6472666535e6f359942ddac6/WIP_jesun_eurosys.tex`
- Modify: `/home/victoryang00/gem5-CXL/6472666535e6f359942ddac6/fig/gapbs-g14-4thread-latency-sweep.pdf`

- [ ] **Step 1: Document exact reproduction commands**

Add source hashes, calibration command, g12/g14 commands, bit-exact rules, labels, and caveats distinguishing AMU paper reproduction, CIRA PGO selection, and causal few-shot.

- [ ] **Step 2: Install validated TeX and figure**

Copy only validator-approved generated outputs into their tracked paper paths. Update caption text to state four threads, g14, all memory on CXL, 20 iterations, and the four latency points.

- [ ] **Step 3: Build the paper**

Run `make` in
`/home/victoryang00/gem5-CXL/6472666535e6f359942ddac6` (its Makefile invokes
`latexmk -pdf -halt-on-error main.tex`). Expected: PDF build succeeds without
missing references, an overfull table caused by the new columns, or missing
figure files.

- [ ] **Step 4: Run final verification**

Run:

```bash
git diff --check
python3 -m unittest discover -s tests/pyunit/amu -p 'test_*.py' -v
python3 -m unittest discover -s tests/pyunit/m2ndp -p 'test_*.py' -v
python3 scripts/validate_gapbs_g14_4thread_latency_results.py \
  --publication /mnt/disk0/gem5-CXL-g14-eval/calibrated/publication \
  --sweep-root /mnt/disk0/gem5-CXL-g14-eval/calibrated \
  --output /mnt/disk0/gem5-CXL-g14-eval/calibrated/publication/independent-validation.json
git status --short
```

Then compare the validator-approved generated artifacts byte-for-byte with
the staged paper files:

```bash
cmp /mnt/disk0/gem5-CXL-g14-eval/calibrated/publication/publication-current/gapbs-g14-4thread-latency-table-data.tex \
  /home/victoryang00/gem5-CXL/6472666535e6f359942ddac6/gapbs-g4-4thread-latency-table-data.tex
cmp /mnt/disk0/gem5-CXL-g14-eval/calibrated/publication/publication-current/gapbs-g14-4thread-latency-sweep.pdf \
  /home/victoryang00/gem5-CXL/6472666535e6f359942ddac6/fig/gapbs-g14-4thread-latency-sweep.pdf
```

- [ ] **Step 5: Commit and push the implementation branch**

```bash
git add docs scripts src configs tests util/amu/amu_paper_profile.cc
git commit -m "feat: publish hardware-calibrated AMU CIRA comparison"
git push origin m2ndp-g20-pr-spmv
```

Then, in `/home/victoryang00/gem5-CXL/6472666535e6f359942ddac6`, stage only
`gapbs-vtune-cxl-table.tex`, `gapbs-g4-4thread-latency-table-data.tex`,
`WIP_jesun_eurosys.tex`, and the generated g14 figure; commit on its confirmed
current branch and push that branch only after `git diff --cached` contains
no generated LaTeX build products.
