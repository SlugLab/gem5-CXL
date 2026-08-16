# PR Scaling g4 Smoke and AMU Line-Cache Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Repair the four-thread, all-CXL, 1 us PageRank experiment so every g4/g12/g14/g20 point is bit-exact, only the nine g12/g14/g20 accelerator points are performance-gated, CIRA emits scale-appropriate coherent prefetches, and AMU uses a real bounded 64-byte line cache/coalescing pipeline before any fresh formal result is accepted.

**Architecture:** Keep the fixed-20 synchronous float32 PageRank algorithm and its ordered scalar accumulation unchanged. Derive CIRA's effective row lead from graph scale at build time, and record that derivation in each scale-local variant manifest. Replace only formal `pr_spmv`'s scalar AMU request stream with a per-thread 16 KiB store containing 8 KiB of staging and 8 KiB of persistent line data; every logical value retains its original position, while one 64-byte AMU request supplies each distinct missing line. A separate g12 Vanilla/AMU/CIRA qualification root must pass correctness, mechanism, and the 1.4x–1.6x interval before a new 16-point formal root may start.

**Tech Stack:** Python 3 `unittest`, C++11/OpenMP GAPBS source generation, gem5 x86 m5ops, JSON/CSV evidence manifests, SCons, systemd-run, Git.

---

## File responsibility map

- `scripts/run_cira_amu_m2ndp_scaling.py`: 16-point correctness state, exact nine-point performance gate, qualification provenance, and formal mechanism validation.
- `scripts/generate_pr_scaling_artifacts.py`: publication-side independent recomputation of the same nine-point gate.
- `scripts/cira_lead_policy.py`: pure scale/partition lead derivation.
- `scripts/build_gapbs_cira_cxlmemuring.py`: generated CIRA future-row window implementation.
- `scripts/build_gapbs_matched_pr_spmv_variants.py`: scale-local AMU/CIRA source generation and manifest provenance.
- `scripts/pr_scaling_variant_build.py`: atomic scale-local build orchestration and validation.
- `util/amu/gapbs_amu_line_cache.h`: bounded production line store, batch coalescing, completion matching, counters, and SPM budget assertions.
- `scripts/build_gapbs_amu_cxlmemuring.py`: generated AMU compatibility header and non-formal GAPBS transformations; formal PR uses the new line-store API.
- `scripts/compare_gapbs_cxl_amu_cira.py`: parse measured-trial AMU line-cache counters into the raw summary.
- `scripts/run_gapbs_matched_pr_spmv_variants.py`: validate variant manifest scale and AMU/CIRA mechanism rows.
- `scripts/qualify_pr_scaling_g12.py`: isolated g12 Vanilla/AMU/CIRA pre-formal gate.
- `docs/amu-gapbs-benchmark.md`: exact qualification, formal launch, monitoring, and publication commands.

### Task 1: Make the performance contract exactly nine points

**Files:**
- Modify: `scripts/run_cira_amu_m2ndp_scaling.py`
- Modify: `scripts/generate_pr_scaling_artifacts.py`
- Modify: `tests/pyunit/cross_system/test_run_cira_amu_m2ndp_scaling.py`
- Modify: `tests/pyunit/cross_system/test_generate_pr_scaling_artifacts.py`

- [ ] **Step 1: Add runner tests that distinguish correctness scales from performance scales**

Add `PERFORMANCE_SCALES = (12, 14, 20)` assertions and replace the g4 offender fixture with these cases:

```python
def test_performance_gate_checks_exactly_nine_points(self):
    self.assertEqual(scaling.SCALES, (4, 12, 14, 20))
    self.assertEqual(scaling.PERFORMANCE_SCALES, (12, 14, 20))
    state = self.complete_state_with_overrides({
        "g4:amu": "0.01",
        "g4:cira": "99",
        "g4:m2ndp": "0.5",
    })
    self.assertEqual(
        scaling.evaluate_performance_gate(state),
        {"status": "passed", "checked_points": 9, "offenders": []},
    )

def test_performance_gate_reports_only_large_scale_offenders(self):
    state = self.complete_state_with_overrides({
        "g4:amu": "0.01",
        "g12:amu": "1.399999",
        "g20:m2ndp": "1.600001",
    })
    result = scaling.evaluate_performance_gate(state)
    self.assertEqual(result["status"], "hold")
    self.assertEqual(result["checked_points"], 9)
    self.assertEqual(
        {row["point"] for row in result["offenders"]},
        {"g12:amu", "g20:m2ndp"},
    )
```

Change the terminal-hold fixture to use `g12:amu=1.39`. Keep the 16/16 completeness test unchanged.

- [ ] **Step 2: Add publisher tests proving g4 is retained but not gated**

Add a helper that changes both absolute latency and the matching native count, then assert `load_data()` accepts an arbitrary g4 AMU ratio but rejects the same ratio at g12:

```python
def set_speedup(self, value, key, speedup):
    scale = int(key.split(":", 1)[0][1:])
    baseline = Decimal(value["points"][f"g{scale}:vanilla"]["latency_seconds"])
    seconds = baseline / Decimal(speedup)
    point = value["points"][key]
    point["latency_seconds"] = str(seconds)
    point["speedup"] = str(Decimal(speedup))
    point["mechanism"]["sim_ticks"] = str(int(seconds * Decimal(10**12)))
```

Expected assertions: g4 appears in the returned 16 rows with its recomputed speedup; g12 raises `performance gate did not pass`.

- [ ] **Step 3: Run the RED tests**

```bash
PYTHONPATH=. python3 -m unittest \
  tests.pyunit.cross_system.test_run_cira_amu_m2ndp_scaling \
  tests.pyunit.cross_system.test_generate_pr_scaling_artifacts -v
```

Expected: failures because `PERFORMANCE_SCALES`/`checked_points` do not exist and g4 is still gated.

- [ ] **Step 4: Implement the shared nine-point rule in both trust boundaries**

In both scripts add:

```python
SCALES = (4, 12, 14, 20)
PERFORMANCE_SCALES = (12, 14, 20)
```

Iterate `PERFORMANCE_SCALES` only when enforcing 1.4x–1.6x. The runner returns:

```python
return {
    "status": "hold" if offenders else "passed",
    "checked_points": len(PERFORMANCE_SCALES) * 3,
    "offenders": offenders,
}
```

The publisher must require the terminal gate to be exactly `passed`, `checked_points == 9`, and `offenders == []`, then independently recheck only non-Vanilla rows whose scale is in `PERFORMANCE_SCALES`. It still validates and emits all 16 rows.

- [ ] **Step 5: Run GREEN tests and commit**

```bash
PYTHONPATH=. python3 -m unittest \
  tests.pyunit.cross_system.test_run_cira_amu_m2ndp_scaling \
  tests.pyunit.cross_system.test_generate_pr_scaling_artifacts -v
git diff --check
git add scripts/run_cira_amu_m2ndp_scaling.py \
  scripts/generate_pr_scaling_artifacts.py \
  tests/pyunit/cross_system/test_run_cira_amu_m2ndp_scaling.py \
  tests/pyunit/cross_system/test_generate_pr_scaling_artifacts.py
git commit -m "fix: gate nine paper-scale PR points"
```

### Task 2: Derive and test a scale-aware CIRA lead

**Files:**
- Modify: `scripts/cira_lead_policy.py`
- Modify: `tests/pyunit/amu/test_cira_lead_policy.py`

- [ ] **Step 1: Write RED derivation and partition tests**

Add tests for this public record:

```python
expected = {
    4:  {"effective_rows": 1, "effective_blocks": None,
         "correctness_fallback": True, "minimum_thread_rows": 4},
    12: {"effective_rows": 512, "effective_blocks": 8,
         "correctness_fallback": False, "minimum_thread_rows": 1024},
    14: {"effective_rows": 2048, "effective_blocks": 32,
         "correctness_fallback": False, "minimum_thread_rows": 4096},
    20: {"effective_rows": 2048, "effective_blocks": 32,
         "correctness_fallback": False, "minimum_thread_rows": 262144},
}
for scale, fields in expected.items():
    actual = policy.effective_lead_for_scale(
        scale, num_threads=4, calibrated_lead_blocks=32
    )
    for name, value in fields.items():
        self.assertEqual(actual[name], value)
```

For every thread and every owned row, test that `future_window(..., effective_rows, batch_rows)` either returns `None` or returns a positive, same-owner interval strictly ahead of the current row and ending no later than the thread boundary. Explicitly require nonzero windows on every g4 thread.

- [ ] **Step 2: Run the RED test**

```bash
PYTHONPATH=. python3 -m unittest \
  tests.pyunit.amu.test_cira_lead_policy -v
```

Expected: `effective_lead_for_scale` and `future_window` are missing.

- [ ] **Step 3: Implement deterministic derivation**

Add exact integer-only functions:

```python
def effective_lead_for_scale(scale, *, num_threads, calibrated_lead_blocks):
    if scale < 0 or num_threads <= 0 or calibrated_lead_blocks <= 0:
        raise LeadPolicyError("scale-aware lead inputs must be positive")
    total_rows = 1 << scale
    spans = [
        end - begin
        for begin, end in (
            static_partition(total_rows, num_threads, tid)
            for tid in range(num_threads)
        )
    ]
    minimum = min(spans)
    calibrated_rows = calibrated_lead_blocks * ROW_BLOCK_SIZE
    if minimum < 2 * ROW_BLOCK_SIZE:
        effective_rows = 1
        fallback = True
        effective_blocks = None
        batch_rows = 1
    else:
        half_aligned = (minimum // 2 // ROW_BLOCK_SIZE) * ROW_BLOCK_SIZE
        effective_rows = min(calibrated_rows, half_aligned)
        fallback = False
        effective_blocks = effective_rows // ROW_BLOCK_SIZE
        batch_rows = ROW_BLOCK_SIZE
    return {
        "graph_scale": scale,
        "total_rows": total_rows,
        "num_threads": num_threads,
        "minimum_thread_rows": minimum,
        "calibrated_rows": calibrated_rows,
        "calibrated_blocks": calibrated_lead_blocks,
        "effective_rows": effective_rows,
        "effective_blocks": effective_blocks,
        "batch_rows": batch_rows,
        "correctness_fallback": fallback,
    }
```

Implement `future_window()` with the same static partition math. For the one-row fallback, issue one future row whenever `current + 1 < thread_end`. For aligned policies, issue only when `(current - thread_begin) % 64 == 0`, at `current + effective_rows`, with at most 64 rows. Keep `future_block()` as a compatibility wrapper for existing non-scaling callers.

- [ ] **Step 4: Run GREEN tests and commit**

```bash
PYTHONPATH=. python3 -m unittest \
  tests.pyunit.amu.test_cira_lead_policy -v
git diff --check
git add scripts/cira_lead_policy.py \
  tests/pyunit/amu/test_cira_lead_policy.py
git commit -m "feat: derive CIRA lead from graph scale"
```

### Task 3: Bind graph scale and effective CIRA policy into every variant

**Files:**
- Modify: `scripts/build_gapbs_cira_cxlmemuring.py`
- Modify: `scripts/build_gapbs_matched_pr_spmv_variants.py`
- Modify: `scripts/pr_scaling_variant_build.py`
- Modify: `scripts/run_cira_amu_m2ndp_scaling.py`
- Modify: `tests/pyunit/m2ndp/test_matched_pr_spmv_variants.py`
- Modify: `tests/pyunit/cross_system/test_pr_scaling_variant_build.py`
- Modify: `tests/pyunit/cross_system/test_run_cira_amu_m2ndp_scaling.py`

- [ ] **Step 1: Write RED builder/manifest tests**

Require calibrated builds to receive `--graph-scale`. Assert `build_command(..., graph_scale=12)` contains `--graph-scale 12`. Validate that a g12 manifest contains:

```json
{
  "graph_scale": 12,
  "cira_policy": {
    "base_1us_lead_blocks": 32,
    "scale_derived": {
      "effective_rows": 512,
      "effective_blocks": 8,
      "batch_rows": 64,
      "correctness_fallback": false
    }
  }
}
```

Add g4 source/command assertions for `GAPBS_CIRA_LEAD_ROWS=1` and `GAPBS_CIRA_BATCH_ROWS=1`, and g12 assertions for `512` and `64`. Add negative tests for a missing scale, a manifest scale mismatch, and an effective policy inconsistent with a recomputation.

- [ ] **Step 2: Run the RED suites**

```bash
PYTHONPATH=. python3 -m unittest \
  tests.pyunit.m2ndp.test_matched_pr_spmv_variants \
  tests.pyunit.cross_system.test_pr_scaling_variant_build \
  tests.pyunit.cross_system.test_run_cira_amu_m2ndp_scaling -v
```

Expected: failures because scale is neither propagated nor validated.

- [ ] **Step 3: Propagate scale through the atomic build path**

Add `graph_scale` to `build_command`, `ensure_variant_build`, and `validate_variant_build`. In `ensure_variants_for_scale`, pass the current scale to both build and validation. The matched builder accepts:

```python
parser.add_argument("--graph-scale", type=int)
```

and rejects calibrated modes unless it is one of `(4, 12, 14, 20)`. Resolve the latency-calibrated maximum first, then call `effective_lead_for_scale()` and store the complete returned dictionary as `cira_policy["scale_derived"]`.

- [ ] **Step 4: Generate row-distance CIRA code without breaking legacy callers**

Add defaults to `CIRA_HEADER`:

```cpp
#ifndef GAPBS_CIRA_LEAD_ROWS
#define GAPBS_CIRA_LEAD_ROWS 0
#endif
#ifndef GAPBS_CIRA_BATCH_ROWS
#define GAPBS_CIRA_BATCH_ROWS GAPBS_CIRA_ROW_BLOCK_SIZE
#endif
```

When `GAPBS_CIRA_LEAD_ROWS > 0`, compute `candidate = current64 + GAPBS_CIRA_LEAD_ROWS`; use a one-row batch for the fallback, otherwise retain the 64-row-aligned issue cadence and cap. The compile command for calibrated scaling emits only the manifest-derived row and batch macros; legacy profiles retain the existing block/distance path.

- [ ] **Step 5: Run GREEN tests and commit**

```bash
PYTHONPATH=. python3 -m unittest \
  tests.pyunit.amu.test_cira_lead_policy \
  tests.pyunit.m2ndp.test_matched_pr_spmv_variants \
  tests.pyunit.cross_system.test_pr_scaling_variant_build \
  tests.pyunit.cross_system.test_run_cira_amu_m2ndp_scaling -v
git diff --check
git add scripts/build_gapbs_cira_cxlmemuring.py \
  scripts/build_gapbs_matched_pr_spmv_variants.py \
  scripts/pr_scaling_variant_build.py \
  scripts/run_cira_amu_m2ndp_scaling.py \
  tests/pyunit/m2ndp/test_matched_pr_spmv_variants.py \
  tests/pyunit/cross_system/test_pr_scaling_variant_build.py \
  tests/pyunit/cross_system/test_run_cira_amu_m2ndp_scaling.py
git commit -m "feat: bind scale-aware CIRA variants"
```

### Task 4: Implement and native-test the bounded AMU line store

**Files:**
- Create: `util/amu/gapbs_amu_line_cache.h`
- Create: `tests/pyunit/amu/test_amu_line_cache.py`
- Modify: `scripts/build_gapbs_amu_cxlmemuring.py`
- Modify: `tests/pyunit/amu/pyunit_gapbs_amu_builder.py`

- [ ] **Step 1: Write a fake-backend native harness before the header exists**

The Python test writes a temporary C++11 program, includes `gapbs_amu_line_cache.h`, and instantiates the template with a fake backend whose `load()` copies 64 bytes and whose `getfin()` returns queued IDs in both FIFO and reverse order. Cover these exact cases:

1. Two 4-byte logical values in one missing line issue one backend request and preserve both values.
2. A second batch to the same line increments `cache_hits` and issues no request.
3. Two different tags mapping to the same direct-mapped cache slot remain readable from staging before drain.
4. Three duplicate logical addresses in one miss increment `coalesced_misses` by two.
5. `reset_iteration()` invalidates the hit and forces a fresh request.
6. Reverse completion order does not change logical extraction order.
7. An unmatched completion, over-capacity batch, early `value()`, or a value crossing a 64-byte boundary terminates through the injected failure backend.
8. The exported staging/cache byte constants are 8192 each and the four-thread data-byte constant is exactly 65536.

- [ ] **Step 2: Run the RED test**

```bash
PYTHONPATH=. python3 -m unittest \
  tests.pyunit.amu.test_amu_line_cache -v
```

Expected: compile failure because the production header does not exist.

- [ ] **Step 3: Implement a dependency-injected, fixed-capacity header**

Use these fixed public constants and shapes:

```cpp
constexpr size_t kLineBytes = 64;
constexpr size_t kFormalThreads = 4;
constexpr size_t kStagingLinesPerThread = 128;
constexpr size_t kCacheLinesPerThread = 128;
constexpr size_t kDataBytesPerThread = 16 * 1024;
constexpr size_t kTotalDataBytes = 64 * 1024;

struct LineCounters {
  uint64_t logical_values;
  uint64_t line_requests;
  uint64_t cache_hits;
  uint64_t coalesced_misses;
};

template <class Backend>
class LineStore {
 public:
  void begin_trial();
  void reset_iteration();
  LineCounters counters() const;
  unsigned char *staging_line(size_t line_slot);
  bool cache_lookup(uintptr_t line_address, unsigned char *destination);
  void cache_install(uintptr_t line_address,
                     const unsigned char *source);
};

template <class Backend>
class LineBatch {
 public:
  explicit LineBatch(LineStore<Backend> &store);
  template <class T> size_t add(const T *address);
  void issue_all();
  void wait_all();
  template <class T> T value(size_t logical_slot) const;
  void clear();
};
```

Implementation rules:

- align addresses with `uintptr_t line = address & ~uintptr_t(63)` and reject `offset + sizeof(T) > 64`;
- use bounded linear searches over at most 128 batch lines, never `std::vector`;
- assign one staging line per distinct batch line, including hits; copy a cache hit into staging immediately so a later colliding miss cannot overwrite its logical source;
- issue exactly one `Backend::load(staging, far_line, 64)` for each distinct miss;
- match each nonzero completion ID exactly once, copy completed misses to their direct-mapped cache slot, and set the full-address tag only after completion;
- make every `value<T>()` read from its stable staging line plus recorded offset;
- invalidate only cache valid bits at each PageRank iteration; reset counters at trial start;
- provide compile-time assertions for 8 KiB staging, 8 KiB cache, 16 KiB/thread, and 64 KiB/four threads.

`AMU_HEADER` includes this header and defines the gem5 backend using `amu_cfgwr`, `amu_aload`, `amu_getfin`, `clflush`, and `mfence`. Retain `load_value`, `AsyncWindow`, and `LoadWindow` only for non-formal BC/BFS/SSSP compatibility; formal matched PR must not call them.

- [ ] **Step 4: Make provenance include the production header**

The AMU build manifest already hashes `util/amu/*.h`; add an explicit `amu_line_cache_sha256` field to the matched variant row so validation can name the exact implementation rather than relying only on a tree hash.

- [ ] **Step 5: Run GREEN tests, sanitizer harness, and commit**

```bash
PYTHONPATH=. python3 -m unittest \
  tests.pyunit.amu.test_amu_line_cache \
  tests.pyunit.amu.pyunit_gapbs_amu_builder -v
CXXFLAGS='-fsanitize=address,undefined -fno-omit-frame-pointer' \
  PYTHONPATH=. python3 -m unittest \
  tests.pyunit.amu.test_amu_line_cache -v
git diff --check
git add util/amu/gapbs_amu_line_cache.h \
  scripts/build_gapbs_amu_cxlmemuring.py \
  tests/pyunit/amu/test_amu_line_cache.py \
  tests/pyunit/amu/pyunit_gapbs_amu_builder.py
git commit -m "feat: add bounded AMU line cache"
```

### Task 5: Replace formal PR scalar AMU waits with an ordered two-stage line pipeline

**Files:**
- Modify: `scripts/build_gapbs_matched_pr_spmv_variants.py`
- Modify: `util/amu/gapbs_amu_line_cache.h`
- Modify: `tests/pyunit/m2ndp/test_matched_pr_spmv_variants.py`
- Modify: `tests/pyunit/amu/test_amu_line_cache.py`

- [ ] **Step 1: Add RED source-structure and float-order tests**

Require generated formal AMU PR to contain `LineStore`, `begin_trial`, `reset_iteration`, `issue_all`, and `wait_all`, while containing none of:

```text
gapbs_amu::load_value(
gapbs_amu::load_values(
gapbs_amu::AsyncWindow
incoming_total +=
```

Require exactly one ordered expression:

```cpp
incoming_total = incoming_total + current_batch.value<ScoreT>(score_slots[i]);
```

Add a native float32 fixture whose source values include cancellation-sensitive bit patterns; compare the raw `uint32_t` result of the original scalar loop with the pipelined logical-order extraction under reversed completion order.

- [ ] **Step 2: Run RED tests**

```bash
PYTHONPATH=. python3 -m unittest \
  tests.pyunit.m2ndp.test_matched_pr_spmv_variants \
  tests.pyunit.amu.test_amu_line_cache -v
```

Expected: the matched source still uses scalar `AsyncWindow` requests.

- [ ] **Step 3: Give four OpenMP workers exactly four line stores**

In the production header, define `stores[kFormalThreads]`, index it only by `omp_get_thread_num()`, and fail if the active team is not exactly four. `begin_trial()` clears that thread's counters and valid bits. `reset_iteration()` clears valid bits only. `report_trial(trial)` sums the four post-barrier counter records and writes one line:

```text
AMU_LINE_CACHE trial=1 logical_values=N line_requests=N cache_hits=N coalesced_misses=N
```

No atomic increment or reporting syscall occurs inside the timed pull loop.

- [ ] **Step 4: Transform the fixed source without changing arithmetic order**

At trial start, replace the score-init `omp parallel for` with an `omp parallel` region that calls `begin_trial()` once per worker and then an `omp for`. At each of the 20 pull iterations, use an `omp parallel` region that calls `reset_iteration()` once per worker and then the same `omp for schedule(static)`.

For each row, process at most 64 logical neighbors per chunk:

```cpp
// Initial dependency: obtain NodeIDs for chunk 0.
gapbs_amu::LineBatch initial_batch(gapbs_amu::thread_store());
for (; v_it != neigh.end() && current_count < GAPBS_AMU_BATCH_SIZE; ++v_it)
  current_node_slots[current_count++] = initial_batch.add(&*v_it);
initial_batch.issue_all();
initial_batch.wait_all();
for (size_t i = 0; i < current_count; ++i)
  current_nodes[i] = initial_batch.value<NodeID>(current_node_slots[i]);

while (current_count != 0) {
  gapbs_amu::LineBatch current_batch(gapbs_amu::thread_store());
  for (size_t i = 0; i < current_count; ++i)
    score_slots[i] = current_batch.add(&outgoing_contrib[current_nodes[i]]);
  for (; v_it != neigh.end() && next_count < GAPBS_AMU_BATCH_SIZE; ++v_it)
    next_node_slots[next_count++] = current_batch.add(&*v_it);
  current_batch.issue_all();
  current_batch.wait_all();
  for (size_t i = 0; i < current_count; ++i)
    incoming_total = incoming_total +
        current_batch.value<ScoreT>(score_slots[i]);
  for (size_t i = 0; i < next_count; ++i)
    next_nodes[i] = current_batch.value<NodeID>(next_node_slots[i]);
  current_batch.clear();
  // Copy only NodeID metadata, preserving its original order.
  for (size_t i = 0; i < next_count; ++i)
    current_nodes[i] = next_nodes[i];
  current_count = next_count;
  next_count = 0;
}
```

This joint batch issues independent score lines for chunk N and CSR lines for chunk N+1 before one dependency wait. With 64 scores and 64 four-byte NodeIDs, the maximum distinct line count is at most 69, below the 128 staging-line bound. Call `report_trial(trial)` immediately after `m5_work_end(trial, 0)`, so reporting is excluded from gem5 ROI ticks.

- [ ] **Step 5: Run GREEN source/native tests and compile the real fixed source**

```bash
PYTHONPATH=. python3 -m unittest \
  tests.pyunit.m2ndp.test_matched_pr_spmv_variants \
  tests.pyunit.amu.test_amu_line_cache \
  tests.pyunit.amu.pyunit_gapbs_amu_builder -v
python3 -m py_compile scripts/build_gapbs_matched_pr_spmv_variants.py
git diff --check
```

Expected: all tests pass; generated source compiles with C++11, OpenMP, `-ffp-contract=off`, and `-fno-fast-math`.

- [ ] **Step 6: Commit**

```bash
git add scripts/build_gapbs_matched_pr_spmv_variants.py \
  util/amu/gapbs_amu_line_cache.h \
  tests/pyunit/m2ndp/test_matched_pr_spmv_variants.py \
  tests/pyunit/amu/test_amu_line_cache.py
git commit -m "feat: pipeline formal PR through AMU lines"
```

### Task 6: Carry line-cache evidence through summaries and fail closed

**Files:**
- Modify: `scripts/compare_gapbs_cxl_amu_cira.py`
- Modify: `scripts/run_gapbs_matched_pr_spmv_variants.py`
- Modify: `scripts/run_cira_amu_m2ndp_scaling.py`
- Modify: `tests/pyunit/amu/pyunit_gapbs_amu_builder.py`
- Modify: `tests/pyunit/m2ndp/test_run_gapbs_matched_pr_spmv_variants.py`
- Modify: `tests/pyunit/cross_system/test_run_cira_amu_m2ndp_scaling.py`

- [ ] **Step 1: Add RED parser and mechanism tests**

Parse exactly one measured-trial record with:

```python
AMU_LINE_CACHE_RE = re.compile(
    r"^AMU_LINE_CACHE trial=(?P<trial>[0-9]+) "
    r"logical_values=(?P<logical_values>[0-9]+) "
    r"line_requests=(?P<line_requests>[0-9]+) "
    r"cache_hits=(?P<cache_hits>[0-9]+) "
    r"coalesced_misses=(?P<coalesced_misses>[0-9]+)$"
)
```

Tests must reject a missing trial 1 record, duplicates, malformed/negative fields, `line_requests != asmc_loads`, `line_requests >= logical_values`, nonzero AMU errors, or issue/completion mismatch. For g12/g14/g20 reject zero `cache_hits` or zero `coalesced_misses`. g4 requires valid balanced nonzero line requests but treats hit/coalescing counts as diagnostic.

- [ ] **Step 2: Run RED tests**

```bash
PYTHONPATH=. python3 -m unittest \
  tests.pyunit.amu.pyunit_gapbs_amu_builder \
  tests.pyunit.m2ndp.test_run_gapbs_matched_pr_spmv_variants \
  tests.pyunit.cross_system.test_run_cira_amu_m2ndp_scaling -v
```

- [ ] **Step 3: Add summary fields and independent validators**

Add these columns to `SUMMARY_FIELDS` and populate them only for AMU:

```text
amu_logical_values
amu_line_requests
amu_line_cache_hits
amu_coalesced_misses
```

Both the matched runner and formal runner must independently apply the invariants above. Preserve the raw marker in `gem5.log`, the CSV row, and the final `mechanism` object.

- [ ] **Step 4: Run GREEN tests and commit**

```bash
PYTHONPATH=. python3 -m unittest \
  tests.pyunit.amu.pyunit_gapbs_amu_builder \
  tests.pyunit.m2ndp.test_run_gapbs_matched_pr_spmv_variants \
  tests.pyunit.cross_system.test_run_cira_amu_m2ndp_scaling -v
git diff --check
git add scripts/compare_gapbs_cxl_amu_cira.py \
  scripts/run_gapbs_matched_pr_spmv_variants.py \
  scripts/run_cira_amu_m2ndp_scaling.py \
  tests/pyunit/amu/pyunit_gapbs_amu_builder.py \
  tests/pyunit/m2ndp/test_run_gapbs_matched_pr_spmv_variants.py \
  tests/pyunit/cross_system/test_run_cira_amu_m2ndp_scaling.py
git commit -m "feat: verify AMU line-request evidence"
```

### Task 7: Add an isolated g12 pre-formal qualification gate

**Files:**
- Create: `scripts/qualify_pr_scaling_g12.py`
- Create: `tests/pyunit/cross_system/test_qualify_pr_scaling_g12.py`
- Modify: `scripts/run_cira_amu_m2ndp_scaling.py`
- Modify: `tests/pyunit/cross_system/test_run_cira_amu_m2ndp_scaling.py`

- [ ] **Step 1: Write RED state-machine tests**

Test a three-entry matrix `(g12:vanilla, g12:amu, g12:cira)`. Require Vanilla to pass before building/running variants. A pass writes `qualification.json` with both independently recomputed speedups and all identity hashes. A correct 1.39x point writes `performance-hold.json`, exits zero, and never writes `qualification.json`. Any bit/mechanism failure writes `failed.json` and exits nonzero. Resume must validate every input/output hash.

Add a formal-runner test that refuses to create `state.json` unless `--qualification` is a PASS record bound to the current code, inputs, calibration, gem5, m5 library, config, g12 graph, and g12 variant manifest.

- [ ] **Step 2: Run RED tests**

```bash
PYTHONPATH=. python3 -m unittest \
  tests.pyunit.cross_system.test_qualify_pr_scaling_g12 \
  tests.pyunit.cross_system.test_run_cira_amu_m2ndp_scaling -v
```

- [ ] **Step 3: Implement the qualifier by reusing formal primitives**

Import `MatrixEntry`, `command_for`, `ensure_variants_for_scale`, `_point_outputs`, `_point_measurement`, `record_pass`, and exact-decimal helpers from the formal runner. Do not duplicate the simulator command or bit validator. Use a separate root, separate scale-local builds, and separate checkpoints. Compute:

```python
speedups = {
    system: vanilla_seconds / Decimal(points[f"g12:{system}"]["latency_seconds"])
    for system in ("amu", "cira")
}
```

Require both inclusive bounds, store the raw point records, then atomically write either PASS or performance hold. The formal runner accepts `--qualification PATH`, hashes it into `new_state()`, and will not use any run directory, binary, checkpoint, or raw vector from the qualification root.

- [ ] **Step 4: Run GREEN tests and commit**

```bash
PYTHONPATH=. python3 -m unittest \
  tests.pyunit.cross_system.test_qualify_pr_scaling_g12 \
  tests.pyunit.cross_system.test_run_cira_amu_m2ndp_scaling -v
git diff --check
git add scripts/qualify_pr_scaling_g12.py \
  scripts/run_cira_amu_m2ndp_scaling.py \
  tests/pyunit/cross_system/test_qualify_pr_scaling_g12.py \
  tests/pyunit/cross_system/test_run_cira_amu_m2ndp_scaling.py
git commit -m "feat: gate formal PR on fresh g12 qualification"
```

### Task 8: Full proof, fresh qualification, formal launch, and push

**Files:**
- Modify: `docs/amu-gapbs-benchmark.md`
- Modify only after terminal PASS: paper files under `6472666535e6f359942ddac6/`

- [ ] **Step 1: Run focused and regression test suites**

```bash
PYTHONPATH=. python3 -m unittest discover \
  -s tests/pyunit/cross_system -p 'test_*.py' -v
PYTHONPATH=. python3 -m unittest discover \
  -s tests/pyunit/amu -p 'test_*.py' -v
PYTHONPATH=. python3 -m unittest discover \
  -s tests/pyunit/m2ndp -p 'test_*.py' -v
python3 -m compileall -q scripts tests/pyunit
git diff --check
```

Expected: all pass. Record exact counts and logs.

- [ ] **Step 2: Build gem5 and the scale-local smoke variants**

```bash
scons build/X86/gem5.opt -j2
```

Build fresh g4 and g12 matched variants with their exact frozen graph scales. Inspect each `manifest.json` and require effective CIRA rows `1` and `512`, an explicit 64 KiB AMU data budget, and hashes for the line-cache header and both binaries.

- [ ] **Step 3: Run a fresh g4 four-core bit-exact smoke**

Use a new identity-derived root. Run Vanilla, AMU, and CIRA at all-CXL `delay=1000000`, four timing cores, four workers, full trial-0 CXL warmup, and measured trial 1. Require raw-vector equality, `Verification: PASS`, balanced AMU/CIRA completion, nonzero CIRA work on all four cores, and zero errors. Record g4 latency/speedup without applying the performance interval.

- [ ] **Step 4: Run the isolated g12 qualification with no timeout**

```bash
EVAL_SHA=$(git rev-parse --short=12 HEAD)
QUAL_ROOT=/mnt/disk0/gem5-CXL-eval/pr-scaling-${EVAL_SHA}-g12-qualification
python3 scripts/qualify_pr_scaling_g12.py \
  --inputs /mnt/disk0/gem5-CXL-eval/pr-scaling-5ed1d7369b-bitexact/inputs.json \
  --calibration /mnt/disk0/gem5-CXL-g14-eval/amu-paper-full-2c07da6b73.calibration.json \
  --root "${QUAL_ROOT}" \
  --gem5 build/X86/gem5.opt \
  --m5-library util/m5/build/x86/out/libm5.a \
  --config configs/example/gem5_library/x86-gapbs-amu-se.py \
  --cxlmemuring /home/victoryang00/CXLMemUring \
  --m2ndp-root /mnt/disk0/M2NDP-public \
  --variants-build-root "${QUAL_ROOT}/builds" \
  --timeout 0
```

The reused input manifest is immutable graph provenance only; its SHA-256 is `6926727d0550e44112705a5238ef297ba5fc2413e85b29deb7f168b36a4c382e`, and no run/checkpoint/result beneath that old root is reused. Expected: `qualification.json` exists only if both g12 speedups are 1.4x–1.6x and every correctness/mechanism gate passes. If `performance-hold.json` appears, stop, preserve evidence, diagnose counters, and do not tune modeled timing to the interval.

- [ ] **Step 5: Start a brand-new formal root only after qualification PASS**

```bash
EVAL_SHA=$(git rev-parse --short=12 HEAD)
QUAL_ROOT=/mnt/disk0/gem5-CXL-eval/pr-scaling-${EVAL_SHA}-g12-qualification
FORMAL_ROOT=/mnt/disk0/gem5-CXL-eval/pr-scaling-${EVAL_SHA}-formal
systemd-run --unit=gem5-pr-scaling-${EVAL_SHA} --collect \
  --description='Formal four-thread all-CXL 1us PR scaling' \
  --property=WorkingDirectory=/home/victoryang00/gem5-CXL/.worktrees/m2ndp-g20-pr-spmv \
  /usr/bin/python3 scripts/run_cira_amu_m2ndp_scaling.py \
  --inputs /mnt/disk0/gem5-CXL-eval/pr-scaling-5ed1d7369b-bitexact/inputs.json \
  --qualification "${QUAL_ROOT}/qualification.json" \
  --calibration /mnt/disk0/gem5-CXL-g14-eval/amu-paper-full-2c07da6b73.calibration.json \
  --root "${FORMAL_ROOT}" \
  --gem5 build/X86/gem5.opt \
  --m5-library util/m5/build/x86/out/libm5.a \
  --config configs/example/gem5_library/x86-gapbs-amu-se.py \
  --cxlmemuring /home/victoryang00/CXLMemUring \
  --m2ndp-root /mnt/disk0/M2NDP-public \
  --variants-build-root "${FORMAL_ROOT}/builds" \
  --timeout 0
```

No old root, variant directory, or checkpoint is reused. Monitor `state.json`, service status, and journals. Completion requires 16/16 correctness points and `performance_gate.checked_points == 9` with no offenders.

- [ ] **Step 6: Publish raw data and figures only from terminal complete evidence**

```bash
python3 scripts/generate_pr_scaling_artifacts.py \
  --scaling "${FORMAL_ROOT}/complete.json" \
  --output-root "${FORMAL_ROOT}/publication"
sha256sum "${FORMAL_ROOT}/publication"/pr-scaling-raw.* \
  "${FORMAL_ROOT}/publication"/pr-scaling-table.tex \
  "${FORMAL_ROOT}/publication"/fig/*
```

Independently compare all four raw rank hashes at each scale. Rasterize PDFs for visual QA. Only after this passes may the paper table/figures be replaced; preserve the lossless JSON/CSV beside them.

- [ ] **Step 7: Update runbook, run final verification, commit, review, and push**

Document exact paths, hashes, service name, status transitions, and why g4 is correctness-only. Then run:

```bash
git diff --check
git status --short
git add docs/amu-gapbs-benchmark.md
git commit -m "docs: record qualified PR scaling workflow"
git log --oneline --decorate -10
git push origin m2ndp-g20-pr-spmv
git rev-parse HEAD
git rev-parse origin/m2ndp-g20-pr-spmv
```

Expected: local and remote commit IDs match. Before claiming completion, perform a focused code review against `docs/superpowers/specs/2026-08-16-pr-scaling-g4-smoke-and-amu-line-cache-design.md`, scan for `TODO|TBD|placeholder`, confirm all new function signatures have consistent call sites, and retain the exact test/build/evidence logs.
