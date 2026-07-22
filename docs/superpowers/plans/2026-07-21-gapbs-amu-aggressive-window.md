# GAPBS AMU Aggressive Window Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace blocking scalar AMU loads in GAPBS hot paths with dependency-aware asynchronous load windows while preserving bit-exact BFS, BC, PR, and SSSP results at a 1 us CXL delay.

**Architecture:** The generated `amu_gapbs.h` will own one fixed-capacity heterogeneous load window with typed add/value operations and explicit issue and wait phases. Kernel rewrites will gather addresses by dependency stage, overlap independent mixed-type requests in one completion domain, and retain the baseline order for floating-point accumulation, CAS, frontier insertion, and other state changes.

**Tech Stack:** Python 3 generator and `unittest`, generated C++11/OpenMP GAPBS sources, gem5 AMU m5ops, X86 syscall emulation, CSV/stat validation.

---

## File Map

- Create `tests/pyunit/amu/pyunit_gapbs_amu_builder.py`: focused generator and transformed-source regression tests.
- Create `tests/pyunit/amu/__init__.py`: makes the focused test directory importable by the existing pyunit runner.
- Modify `scripts/build_gapbs_amu_cxlmemuring.py`: add heterogeneous `LoadWindow` and rewrite BC/PR/SSSP hot paths around staged windows.
- Modify `configs/example/gem5_library/x86-gapbs-amu-se.py`: dump ROI stats at work end and optionally continue through GAPBS verification.
- Modify `scripts/compare_gapbs_cxl_amu_cira.py`: retain verifier evidence in the result summary and reject runs whose workload verification fails.
- Modify `docs/amu-gapbs-benchmark.md`: replace the nonexistent legacy AMU config command with the current local SE build/run/verification workflow.

### Task 1: Heterogeneous load-window helper contract

**Files:**
- Create: `tests/pyunit/amu/__init__.py`
- Create: `tests/pyunit/amu/pyunit_gapbs_amu_builder.py`
- Modify: `scripts/build_gapbs_amu_cxlmemuring.py:24-100`

- [ ] **Step 1: Write the failing helper tests**

Load the generator with `importlib.util.spec_from_file_location`, then assert
against `AMU_HEADER` with tests equivalent to:

```python
def test_load_window_issues_before_waiting(self):
    header = self.builder.AMU_HEADER
    self.assertIn("class LoadWindow", header)
    issue = header.index("void issue_all()")
    wait = header.index("void wait_all()")
    self.assertLess(issue, wait)
    issue_body = header[issue:wait]
    self.assertIn("amu_aload", issue_body)
    self.assertNotIn("amu_getfin", issue_body)

def test_load_values_uses_the_window(self):
    header = self.builder.AMU_HEADER
    wrapper = header[header.index("static inline void load_values") :]
    self.assertIn("LoadWindow window", wrapper)
    self.assertIn("window.issue_all()", wrapper)
    self.assertIn("window.wait_all()", wrapper)

def test_window_handles_empty_and_bounded_batches(self):
    header = self.builder.AMU_HEADER
    self.assertIn("if (count_ == 0)", header)
    self.assertIn("assert(count_ < GAPBS_AMU_BATCH_SIZE)", header)
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```bash
python3 -m unittest -v tests.pyunit.amu.pyunit_gapbs_amu_builder
```

Expected: failures stating that `class LoadWindow`, `issue_all`, and
`window.wait_all()` are absent from `AMU_HEADER`.

- [ ] **Step 3: Implement the minimal heterogeneous window**

In `AMU_HEADER`, add `<assert.h>` and a `LoadWindow` with templated `add<T>()`
and `value<T>()` methods. Record each slot's byte size, configure that size
immediately before its `amu_aload`, and consume every completion through the
single window ID table. Store SPM and completed values in 64-byte slot arrays.

```cpp
template <typename T>
class LoadWindow {
 public:
  LoadWindow() : count_(0), issued_(false), complete_(false) {
    memset(spm_, 0, sizeof(spm_));
    memset(ids_, 0, sizeof(ids_));
  }

  size_t add(const T *addr) {
    assert(!issued_);
    assert(count_ < GAPBS_AMU_BATCH_SIZE);
    addrs_[count_] = addr;
    return count_++;
  }

  void issue_all() {
    assert(!issued_);
    issued_ = true;
    if (count_ == 0) {
      complete_ = true;
      return;
    }
    configure(sizeof(T));
    for (size_t i = 0; i < count_; ++i)
      ids_[i] = amu_aload(spm_[i], addrs_[i]);
  }

  void wait_all() {
    assert(issued_);
    size_t done = 0;
    while (done < count_) {
      uint64_t id = amu_getfin();
      if (id == 0)
        continue;
      bool matched = false;
      for (size_t i = 0; i < count_; ++i) {
        if (ids_[i] == id) {
          memcpy(&values_[i], spm_[i], sizeof(T));
          ids_[i] = 0;
          ++done;
          matched = true;
          break;
        }
      }
      assert(matched);
    }
    complete_ = true;
  }

  const T &value(size_t slot) const {
    assert(complete_ && slot < count_);
    return values_[slot];
  }

  size_t size() const { return count_; }

 private:
  const T *addrs_[GAPBS_AMU_BATCH_SIZE];
  alignas(64) unsigned char
      spm_[GAPBS_AMU_BATCH_SIZE][sizeof(T) <= 64 ? 64 : sizeof(T)];
  uint64_t ids_[GAPBS_AMU_BATCH_SIZE];
  T values_[GAPBS_AMU_BATCH_SIZE];
  size_t count_;
  bool issued_;
  bool complete_;
};
```

Rewrite `load_values()` as a compatibility wrapper that adds `count` addresses,
issues once, waits once, then copies `window.value(i)` in slot order. Leave
`load_value()` unchanged for dependent retry paths.

- [ ] **Step 4: Run the helper tests and verify GREEN**

Run the focused unittest command from Step 2. Expected: the three helper tests
pass with no errors.

- [ ] **Step 5: Compile a generated header smoke program**

Generate a temporary header through `patch_sources`, then compile a small C++11
translation unit that instantiates `LoadWindow` and calls `add<uint64_t>` using the repository's
AMU include paths. Run:

```bash
g++ -std=c++11 -fsyntax-only \
  -I util/amu -I include -I /tmp/gapbs-amu-header-smoke \
  /tmp/gapbs-amu-header-smoke/smoke.cc
```

Expected: exit 0 and no C++ syntax diagnostics.

- [ ] **Step 6: Commit the helper and tests**

```bash
git add tests/pyunit/amu scripts/build_gapbs_amu_cxlmemuring.py
git commit -m "mem: add typed GAPBS AMU load windows"
```

### Task 2: Batch BC reverse-pass values

**Files:**
- Modify: `tests/pyunit/amu/pyunit_gapbs_amu_builder.py`
- Modify: `scripts/build_gapbs_amu_cxlmemuring.py:238-323`

- [ ] **Step 1: Write a failing transformed-BC test**

Create a temporary `src/bc.cc` fixture containing the exact original snippets
consumed by `patch_bc()`. After patching, assert:

```python
self.assertIn("LoadWindow value_window", transformed)
self.assertLess(
    transformed.index("value_window.issue_all()"),
    transformed.index("value_window.wait_all()"),
)
self.assertNotIn("load_value(&path_counts[v])", transformed)
self.assertNotIn("load_value(&deltas[v])", transformed)
self.assertIn("value_window.value<CountT>", transformed)
self.assertIn("value_window.value<ScoreT>", transformed)
```

- [ ] **Step 2: Run only the BC test and verify RED**

```bash
python3 -m unittest -v \
  tests.pyunit.amu.pyunit_gapbs_amu_builder.GapbsAmuBuilderTest.test_bc_batches_reverse_values
```

Expected: failure because the transformed source still contains scalar
`load_value()` calls.

- [ ] **Step 3: Implement aligned BC windows**

In `patch_bc()`, after the neighbor-ID batch completes, retain original
neighbor slots and construct one heterogeneous `value_window` plus compact
path-count and delta slot mappings only for `succ` entries. Add both addresses
for each selected successor, issue and wait once, then accumulate in ascending
successor order. Keep `path_counts[u]` outside the window and preserve the
original `delta_u += ...` expression order.

- [ ] **Step 4: Run the BC test and full focused suite**

Run the single test, then the Task 1 full focused unittest command. Expected:
all tests pass.

- [ ] **Step 5: Commit the BC rewrite**

```bash
git add tests/pyunit/amu/pyunit_gapbs_amu_builder.py \
  scripts/build_gapbs_amu_cxlmemuring.py
git commit -m "benchmarks: overlap BC AMU value loads"
```

### Task 3: Batch SSSP initial distances

**Files:**
- Modify: `tests/pyunit/amu/pyunit_gapbs_amu_builder.py`
- Modify: `scripts/build_gapbs_amu_cxlmemuring.py:357-401`

- [ ] **Step 1: Write a failing transformed-SSSP test**

Patch a minimal exact-match `sssp.cc` fixture and assert:

```python
self.assertIn("LoadWindow distance_window", transformed)
self.assertIn("distance_window.add(&dist[edges[amu_i].v])", transformed)
self.assertIn("distance_window.issue_all()", transformed)
self.assertIn("distance_window.wait_all()", transformed)
self.assertIn("distance_window.value(amu_i)", transformed)
self.assertEqual(transformed.count("load_value(&dist[wn.v])"), 1)
```

The single remaining scalar occurrence must appear after
`compare_and_swap(...)` and inside its retry loop.

- [ ] **Step 2: Run the SSSP test and verify RED**

```bash
python3 -m unittest -v \
  tests.pyunit.amu.pyunit_gapbs_amu_builder.GapbsAmuBuilderTest.test_sssp_batches_initial_distances
```

Expected: failure because each initial destination distance is scalar-loaded.

- [ ] **Step 3: Implement the SSSP distance stage**

After `load_values(edge_addrs, edges, amu_count)`, create one
`LoadWindow`, add `&dist[edges[i].v]` in edge order, issue and wait,
then use `value(i)` as the initial `old_dist`. Preserve the existing scalar
reload following failed CAS, edge order, bin resizing, and bin insertion.

- [ ] **Step 4: Run the SSSP test and full focused suite**

Expected: all focused generator tests pass.

- [ ] **Step 5: Commit the SSSP rewrite**

```bash
git add tests/pyunit/amu/pyunit_gapbs_amu_builder.py \
  scripts/build_gapbs_amu_cxlmemuring.py
git commit -m "benchmarks: batch SSSP AMU distance loads"
```

### Task 4: Lock down PR and BFS ordering

**Files:**
- Modify: `tests/pyunit/amu/pyunit_gapbs_amu_builder.py`
- Modify: `scripts/build_gapbs_amu_cxlmemuring.py:155-235`
- Modify: `scripts/build_gapbs_amu_cxlmemuring.py:325-355`

- [ ] **Step 1: Write ordering regression tests**

For transformed PR, assert node loads complete before score addresses are
formed, score loads complete before accumulation, and accumulation remains a
scalar loop in ascending `amu_i`. For transformed BFS, assert bottom-up checks
`amu_values[amu_i]` in ascending order and sets `found` only after the matching
parent assignment; top-down CAS remains inside the ordered `amu_i` loop.

- [ ] **Step 2: Run the PR/BFS tests**

Expected: tests pass if the existing source already meets the approved design.
If a test fails, treat that as a discovered ordering defect and continue with
Step 3; do not weaken the assertion.

- [ ] **Step 3: Make only ordering fixes demonstrated by RED**

Adjust the generated loops so that `wait_all()` precedes all uses, `amu_i`
controls commit order, PR adds all score addresses before issuing, and BFS does
not commit based on completion order. Do not add new speculative operations.

- [ ] **Step 4: Run the entire focused suite**

```bash
python3 -m unittest -v tests.pyunit.amu.pyunit_gapbs_amu_builder
```

Expected: all helper and kernel transformation tests pass.

- [ ] **Step 5: Commit ordering coverage**

```bash
git add tests/pyunit/amu/pyunit_gapbs_amu_builder.py \
  scripts/build_gapbs_amu_cxlmemuring.py
git commit -m "tests: lock down GAPBS AMU commit ordering"
```

### Task 5: Run the verifier after the timed ROI

**Files:**
- Modify: `tests/pyunit/amu/pyunit_gapbs_amu_builder.py`
- Modify: `configs/example/gem5_library/x86-gapbs-amu-se.py:210-294`
- Modify: `scripts/compare_gapbs_cxl_amu_cira.py:37-226`

- [ ] **Step 1: Write failing log-parser tests**

Add tests for a pure `parse_verification(log_path)` helper. A log containing
the exact GAPBS line `Verification: PASS` must return `pass`; a log containing
`Verification: FAIL` must return `fail`; a log with neither line must return
`missing`. Add a source-level config test requiring the work-end handler to
yield `False` when verification continuation is enabled and `True` otherwise.

- [ ] **Step 2: Verify parser tests RED**

Run the focused unittest suite. Expected: import failure or assertion failure
because `parse_verification` does not exist.

- [ ] **Step 3: Add explicit post-ROI verification mode**

Add `--continue-after-roi` to `x86-gapbs-amu-se.py`. Change the work-end handler
to dump ROI stats and `yield not args.continue_after_roi`. When continuation is
enabled, gem5 runs the verifier and reaches normal program exit, while the
already-dumped `stats.txt` remains the kernel ROI measurement.

Add `--verify` to `compare_gapbs_cxl_amu_cira.py`. When enabled, append `-v` to
the GAPBS workload arguments and pass `--continue-after-roi` to the config.

- [ ] **Step 4: Implement parsing and summary gating**

Add `verification` to each row and `summary.csv`. When gem5 exits zero but the
workload log explicitly reports verifier failure, set status to
`verification-failed`. When `--verify` is requested and the line is missing,
set status to `verification-missing`. Add `verification` to
`write_summary()` fields.

- [ ] **Step 5: Verify parser and existing script syntax**

```bash
python3 -m unittest -v tests.pyunit.amu.pyunit_gapbs_amu_builder
python3 -m py_compile scripts/build_gapbs_amu_cxlmemuring.py \
  scripts/compare_gapbs_cxl_amu_cira.py \
  configs/example/gem5_library/x86-gapbs-amu-se.py
```

Expected: all tests pass and all three Python files compile without output.

- [ ] **Step 6: Commit verifier execution and reporting**

```bash
git add tests/pyunit/amu/pyunit_gapbs_amu_builder.py \
  scripts/compare_gapbs_cxl_amu_cira.py \
  configs/example/gem5_library/x86-gapbs-amu-se.py
git commit -m "scripts: verify GAPBS after ROI timing"
```

### Task 6: Rebuild matched binaries and prove native correctness

**Files:**
- Generated only: `m5out/gapbs_baseline_bins_window_20260721/`
- Generated only: `m5out/gapbs_amu_bins_window_20260721/`

- [ ] **Step 1: Rebuild the baseline binaries**

```bash
python3 scripts/build_gapbs_baseline_cxlmemuring.py \
  --benchmarks bfs,bc,pr,sssp --roi-work-markers \
  --outdir m5out/gapbs_baseline_bins_window_20260721
```

Expected: four binaries and a manifest naming the same CXLMemUring commit used
for the AMU build.

- [ ] **Step 2: Rebuild the aggressive-window AMU binaries**

```bash
python3 scripts/build_gapbs_amu_cxlmemuring.py \
  --benchmarks bfs,bc,pr,sssp --roi-work-markers --amu-batch-size 64 \
  --outdir m5out/gapbs_amu_bins_window_20260721
```

Expected: four binaries, `amu_rewritten_benchmarks` containing all four names,
and generated sources matching the structural tests.

- [ ] **Step 3: Compare build manifests**

Use a short read-only Python assertion to require identical
`cxlmemuring_commit` values and the exact benchmark set in both manifests.
Expected: exit 0.

- [ ] **Step 4: Run native verifier checks where functional m5ops are supported**

Run each matched binary with deterministic `-g 4 -n 1` parameters and capture
stdout separately. Require process exit 0 and the same GAPBS verification
success marker for baseline and AMU. If native AMU m5ops are unsupported on the
host, record that exact blocker and use the gem5 runs in Task 7 as the mandatory
correctness gate; do not report native proof.

### Task 7: Run the 1 us gem5 correctness and performance gate

**Files:**
- Generated only: `m5out/gapbs_cxl_amu_cira/window_g4_1us_20260721/`

- [ ] **Step 1: Run all matched workloads**

```bash
python3 scripts/compare_gapbs_cxl_amu_cira.py \
  --baseline-bin-dir m5out/gapbs_baseline_bins_window_20260721/bin \
  --amu-bin-dir m5out/gapbs_amu_bins_window_20260721/bin \
  --benchmarks bfs,bc,pr,sssp --scale 4 --iterations 1 \
  --cpu timing --cores 1 --cxl-link-delay 1us --roi-work-events \
  --verify \
  --outdir m5out/gapbs_cxl_amu_cira/window_g4_1us_20260721
```

Expected: eight successful runs and a summary containing per-workload verifier
status, ticks, speedup, AMU loads, and run directories.

- [ ] **Step 2: Enforce the correctness invariants**

Run a Python assertion over the summary and stats requiring:

```python
assert len(rows) == 8
assert all(row["status"] == "ok" for row in rows)
assert all(row["verification"] == "pass" for row in rows)
assert all("delay=1000000" in (Path(row["run_dir"]) / "config.ini").read_text()
assert issued_loads == completed_loads  # for every AMU row
```

Expected: exit 0. Any verifier failure stops performance interpretation.

- [ ] **Step 3: Compare against the pre-change evidence**

Compare the new summary with
`m5out/gapbs_cxl_amu_cira/diagnose_all_g4_1us_20260720/summary.csv`. Report each
workload's old and new speedup, AMU load count, sim instructions, and CXL
traffic. Do not average away regressions.

### Task 8: Update the runnable documentation and final verification

**Files:**
- Modify: `docs/amu-gapbs-benchmark.md`

- [ ] **Step 1: Write the corrected workflow**

Replace the nonexistent `x86-gapbs-amu-benchmarks.py` example with the exact
baseline build, AMU build, and 1 us comparison commands from Tasks 6 and 7.
Document that scripts without executable bits must be launched with `python3`,
that `--roi-work-events` is mandatory for kernel-only timing, and that valid
AMU evidence requires verifier pass plus `issuedLoads == completedLoads`.

- [ ] **Step 2: Validate documentation commands and repository diff**

Run each documented build/run command with `--help` or `--dry-run` where
available, then run:

```bash
python3 -m unittest -v tests.pyunit.amu.pyunit_gapbs_amu_builder
python3 -m py_compile scripts/build_gapbs_amu_cxlmemuring.py \
  scripts/compare_gapbs_cxl_amu_cira.py \
  configs/example/gem5_library/x86-gapbs-amu-se.py
git diff --check
git status --short
```

Expected: tests pass, Python syntax checks pass, no whitespace errors, and only
the planned source/test/doc files are modified.

- [ ] **Step 3: Commit documentation**

```bash
git add docs/amu-gapbs-benchmark.md
git commit -m "docs: describe verified 1us GAPBS AMU runs"
```

- [ ] **Step 4: Report the proof boundary**

Provide links to the new summary, manifests, and representative stats/config
files. State separately: generator tests, build status, verifier status,
issued/completed equality, and measured per-workload speedups. If any gate is
blocked, state the exact failing command and do not claim the implementation is
complete.
