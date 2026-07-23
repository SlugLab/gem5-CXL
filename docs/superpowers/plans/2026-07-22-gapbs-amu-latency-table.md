# GAPBS AMU/CIRA CXL-Latency Table Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the invalid scale-4 paper table with verified scale-20 baseline-versus-AMU and baseline-versus-CIRA results at 200 ns, 500 ns, 1 us, and 2 us CXL latency.

**Architecture:** Build matched verifier-enabled binaries, generate the scale-20 graph on an Atomic CPU while all physical memory remains behind the CXL SerialLink, switch once to a Timing CPU at the first work-begin event, warm trial 0, and measure only trial 1. Make CIRA prefetch PGO-selected future CSR rows, collect demand-miss/useful/late and exact CXL packet/byte evidence, pass a strict PR@1us discriminator, then run and publish the complete 48-configuration matrix.

**Tech Stack:** Python 3, gem5 X86 `SimpleSwitchableProcessor`, classic private L1/L2 caches, C++ CIRA model and m5ops, GAPBS/CXLMemUring, CSV, LaTeX/USENIX template.

## Global Constraints

- Use GAPBS `-g 20 -n 2 -v`, one process, one core, and matched graph/binary provenance.
- Atomic execution is pre-ROI only. Trial 0 and measured trial 1 both run on the Timing CPU; only trial 1 contributes stats and ticks.
- Every physical memory range, including graph construction and kernel data, remains behind `board.cxl_mem_link0`; no host-memory range, direct CIRA-to-controller path, or device-compute shortcut is allowed.
- CIRA future-row distances come from the selected PGO profiles: BFS 16, BC 24, PR 8, and SSSP 20.
- Every run must be bit-exact PASS. Missing or failed verification suppresses speedup and blocks publication.
- The existing scale-4 table and provenance CSV must not be published and are overwritten only after the scale-20 sweep validates.
- CIRA issued/completed equality is integrity evidence only. Benefit claims additionally require demand-miss and CIRA-specific useful/late evidence.

---

### Task 1: Implement the two-trial Atomic-to-Timing state machine

**Files:**
- Modify: `tests/pyunit/amu/pyunit_gapbs_amu_builder.py`
- Modify: `configs/example/gem5_library/x86-gapbs-amu-se.py`
- Modify: `scripts/compare_gapbs_cxl_amu_cira.py`

**Interfaces:**
- Produces: config/runner options `--fast-forward-cpu atomic`, `--cpu timing`, and `--measure-trial 1`; exactly one trial-1 ROI stats section.
- Consumes: two `m5_work_begin`/`m5_work_end` pairs from GAPBS `-n 2`.

- [ ] **Step 1: Write failing state-machine tests**

Assert that the config uses `SimpleSwitchableProcessor` with Atomic start and
Timing switch cores. Feed a pure handler helper the sequence
`BEGIN, END, BEGIN, END`; require exactly one `processor.switch()`, no reset or
dump for trial 0, one reset at trial 1 begin, one dump at trial 1 end, and
continuation to verification. Assert the runner forwards all three options.

Run: `python3 tests/pyunit/amu/pyunit_gapbs_amu_builder.py -v`

Expected: FAIL because the switchable two-trial behavior is absent.

- [ ] **Step 2: Implement the state machine**

Use `SimpleSwitchableProcessor(starting_core_type=CPUTypes.ATOMIC,
switch_core_type=CPUTypes.TIMING, ...)` when fast-forwarding is selected. At
the first work-begin, switch exactly once and run trial 0 without resetting
ROI stats. At trial 1 begin, reset stats and save `start_tick`; at trial 1 end,
dump stats. Reject a missing trial, a third trial, or a second switch. Continue
after trial 1 so normal exit proves PASS and `m5_fail` proves FAIL.

Pass `--fast-forward-cpu`, `--measure-trial`, `--scale 20`, and
`--iterations 2` through the runner. Preserve the non-switching path for other
uses.

- [ ] **Step 3: Test and inspect the dry-run command**

Run:

```bash
python3 tests/pyunit/amu/pyunit_gapbs_amu_builder.py -v
python3 scripts/compare_gapbs_cxl_amu_cira.py \
  --gem5 build/X86/gem5.opt \
  --baseline-bin-dir m5out/gapbs_baseline_bins_latency_g20/bin \
  --benchmarks pr --scale 20 --iterations 2 \
  --fast-forward-cpu atomic --cpu timing --measure-trial 1 \
  --cxl-link-delay 1us --roi-work-events --verify --dry-run \
  --outdir /tmp/gapbs-g20-command-check
```

Expected: tests pass; the command contains `-g 20 -n 2 -v`, Atomic
fast-forward, Timing ROI, measured trial 1, CXL memory, and no device-offload
environment variable.

- [ ] **Step 4: Commit**

```bash
git add tests/pyunit/amu/pyunit_gapbs_amu_builder.py \
  configs/example/gem5_library/x86-gapbs-amu-se.py \
  scripts/compare_gapbs_cxl_amu_cira.py
git commit -m "configs: measure second GAPBS trial after atomic setup"
```

### Task 2: Issue PGO-selected future-row CIRA CSR prefetches

**Files:**
- Modify: `scripts/build_gapbs_cira_cxlmemuring.py`
- Modify: `tests/pyunit/amu/pyunit_gapbs_amu_builder.py`

**Interfaces:**
- Consumes: `resolve_profile()`'s positive per-workload distance and each GAPBS loop's current row.
- Produces: generated CSR helpers that clamp `current_row + GAPBS_CIRA_PREFETCH_DISTANCE` and issue one future-row descriptor without a per-row drain.

- [ ] **Step 1: Write failing transformation tests**

For BFS, BC, PR, and SSSP, require generated code to compute and bounds-check
the future row, issue a CSR descriptor for that row, omit whole-graph prefetch
at kernel entry, and omit `GAPBS_CIRA_DRAIN()` from the row loop. Add profile
fixtures resolving to BFS 16, BC 24, PR 8, and SSSP 20.

Run: `python3 tests/pyunit/amu/pyunit_gapbs_amu_builder.py -v`

Expected: FAIL because the CSR path uses current/coarse regions.

- [ ] **Step 2: Implement future-row helpers and transformations**

Generate this bounds rule and build a one-row descriptor from that row's CSR
record span:

```cpp
const uint64_t pf_row =
    static_cast<uint64_t>(current_row) + GAPBS_CIRA_PREFETCH_DISTANCE;
if (pf_row < static_cast<uint64_t>(g.num_nodes()))
  prefetch_csr_row(g, pf_row, values, index_offset, index_size);
```

BFS selects in/out CSR by traversal phase, BC and SSSP use out CSR, and PR uses
in CSR. Preserve each workload's indexed value arrays. Do not synchronously
drain after each descriptor.

- [ ] **Step 3: Build and verify PGO provenance**

```bash
python3 scripts/build_gapbs_cira_cxlmemuring.py \
  --cxlmemuring /home/victoryang00/CXLMemUring \
  --benchmarks bfs,bc,pr,sssp --roi-work-markers \
  --outdir m5out/gapbs_cira_bins_latency_g20
```

Expected: `manifest.json` reports `profile_mode=pgo`, distances
`bfs=16,bc=24,pr=8,sssp=20`, hashes all four profiles, and records no manual
distance or device-offload path.

- [ ] **Step 4: Commit**

```bash
git add scripts/build_gapbs_cira_cxlmemuring.py \
  tests/pyunit/amu/pyunit_gapbs_amu_builder.py
git commit -m "cira: prefetch PGO-selected future GAPBS rows"
```

### Task 3: Add CIRA-specific useful and late evidence

**Files:**
- Modify: `src/mem/CIRA.py`
- Modify: `src/mem/cira.hh`
- Modify: `src/mem/cira.cc`
- Modify: `configs/example/gem5_library/x86-gapbs-amu-se.py`
- Create: `tests/pyunit/amu/test_cira_usefulness_contract.py`

**Interfaces:**
- Consumes: private-L2 `Hit` and `Miss` `CacheAccessProbeArg` events and CIRA cacheline issue/response state.
- Produces: numeric `board.cira.usefulPrefetches` and `board.cira.latePrefetches` in every CIRA ROI.

- [ ] **Step 1: Write failing attribution tests**

Test these exact transitions: completed-CIRA-line then CPU demand is useful;
outstanding-CIRA-line then CPU demand is late; one demand after duplicate
issues is classified at most once; instruction, writeback, prefetcher, and
CIRA-originated traffic is ignored. Assert the exact stat names and the private
L2 probe parameter.

Run: `python3 tests/pyunit/amu/test_cira_usefulness_contract.py -v`

Expected: FAIL because CIRA currently exposes completion only.

- [ ] **Step 2: Implement cacheline attribution**

Track aligned outstanding/completed CIRA lines with duplicate reference
counts. Register listeners on the first private L2's `Hit` and `Miss` probe
points. On a Timing-CPU demand, consume either completed as useful or
outstanding as late, never both. Clear line state on reset and avoid dangling
state on responses. Wire the private L2 as CIRA's demand-probe target while
retaining CIRA's memory port through that L2.

- [ ] **Step 3: Build and test**

```bash
scons build/X86/gem5.opt -j2
python3 tests/pyunit/amu/test_cira_usefulness_contract.py -v
python3 tests/pyunit/amu/pyunit_gapbs_amu_builder.py -v
```

Expected: gem5 builds and all focused tests pass.

- [ ] **Step 4: Commit**

```bash
git add src/mem/CIRA.py src/mem/cira.hh src/mem/cira.cc \
  configs/example/gem5_library/x86-gapbs-amu-se.py \
  tests/pyunit/amu/test_cira_usefulness_contract.py
git commit -m "cira: attribute useful and late GAPBS prefetches"
```

### Task 4: Collect exact CXL and demand-miss metrics

**Files:**
- Modify: `scripts/compare_gapbs_cxl_amu_cira.py`
- Modify: `scripts/validate_gapbs_amu_latency_sweep.py`
- Modify: `scripts/generate_gapbs_amu_latency_table.py`
- Modify: `tests/pyunit/amu/test_validate_gapbs_amu_latency_sweep.py`
- Modify: `tests/pyunit/amu/test_generate_gapbs_amu_latency_table.py`

**Interfaces:**
- Produces: `l1d_demand_misses`, `l2d_demand_misses`, `cira_useful`, `cira_late`, `cira_read_packets`, `cira_read_bytes`, `cxl_packets`, and `cxl_bytes`.
- Produces: validator options `--pr-gate`, `--combined-output PATH`, and `--validation-output PATH` for the two-row discriminator and deterministic matrix evidence.
- Consumes: only trial-1 ROI stats and exact stat-key families.

- [ ] **Step 1: Write failing parser/validator regressions**

Create stats with packet count 137, byte count 2880, and unrelated formulas
whose keys all contain the CXL port. Require `cxl_packets == 137` and
`cxl_bytes == 2880`, never 3017. Add failures for scale other than 20,
iterations other than 2, measured trial other than 1, missing useful/late,
missing typed CXL traffic, or a `config.ini` memory path bypassing the link.

Run:

```bash
python3 tests/pyunit/amu/test_validate_gapbs_amu_latency_sweep.py -v
python3 tests/pyunit/amu/test_generate_gapbs_amu_latency_table.py -v
```

Expected: FAIL with the current substring sum.

- [ ] **Step 2: Parse typed evidence**

Select only `pktCount_*::board.cxl_mem_link0.cpu_side_port` for packets and
matching `pktSize_*::board.cxl_mem_link0.cpu_side_port` for bytes. Sum only
same-unit directional keys. Parse Timing-CPU L1D/L2D demand misses, CIRA
useful/late/read values, and AMU/CIRA integrity counters. Preserve all fields
in the provenance CSV without widening the compact LaTeX table.

- [ ] **Step 3: Strengthen validation**

Require 48 exact rows, finite ticks, bit-exact PASS, scale 20, two trials,
measured trial 1, one Atomic-to-Timing switch, exact latency, all-memory CXL
routing, and numeric evidence. Require balanced positive AMU loads; require
balanced positive CIRA leaf requests and positive future-row CSR descriptors.
Do not require every full-matrix speedup to exceed 1.

- [ ] **Step 4: Test and commit**

```bash
python3 tests/pyunit/amu/test_validate_gapbs_amu_latency_sweep.py -v
python3 tests/pyunit/amu/test_generate_gapbs_amu_latency_table.py -v
python3 tests/pyunit/amu/pyunit_gapbs_amu_builder.py -v
git add scripts/compare_gapbs_cxl_amu_cira.py \
  scripts/validate_gapbs_amu_latency_sweep.py \
  scripts/generate_gapbs_amu_latency_table.py tests/pyunit/amu
git commit -m "scripts: validate GAPBS CXL benefit evidence"
```

Expected: all tests pass and no result artifact is committed.

### Task 5: Pass the PR@1us discriminator

**Files:**
- Generated: `m5out/gapbs_baseline_bins_latency_g20/`
- Generated: `m5out/gapbs_amu_bins_latency_g20/`
- Generated: `m5out/gapbs_cira_bins_latency_g20/`
- Generated: `m5out/gapbs_cxl_amu_cira/latency_g20_pr_gate_20260723/`

- [ ] **Step 1: Build matched binaries**

Run:

```bash
python3 scripts/build_gapbs_baseline_cxlmemuring.py \
  --cxlmemuring /home/victoryang00/CXLMemUring \
  --benchmarks bfs,bc,pr,sssp --roi-work-markers \
  --outdir m5out/gapbs_baseline_bins_latency_g20
python3 scripts/build_gapbs_amu_cxlmemuring.py \
  --cxlmemuring /home/victoryang00/CXLMemUring \
  --benchmarks bfs,bc,pr,sssp --roi-work-markers --amu-batch-size 64 \
  --outdir m5out/gapbs_amu_bins_latency_g20
python3 scripts/build_gapbs_cira_cxlmemuring.py \
  --cxlmemuring /home/victoryang00/CXLMemUring \
  --benchmarks bfs,bc,pr,sssp --roi-work-markers \
  --outdir m5out/gapbs_cira_bins_latency_g20
```

Expected: four binaries and one manifest per configuration; matched commits,
dirty states, m5 inputs, and binary stems; CIRA profile hashes/distances present.

- [ ] **Step 2: Run baseline and CIRA PR at 1 us**

```bash
python3 scripts/compare_gapbs_cxl_amu_cira.py \
  --gem5 build/X86/gem5.opt \
  --baseline-bin-dir m5out/gapbs_baseline_bins_latency_g20/bin \
  --cira-bin-dir cira_pgo=m5out/gapbs_cira_bins_latency_g20/bin \
  --benchmarks pr --scale 20 --iterations 2 \
  --fast-forward-cpu atomic --cpu timing --measure-trial 1 --cores 1 \
  --cxl-link-delay 1us --roi-work-events --verify --timeout 7200 \
  --outdir m5out/gapbs_cxl_amu_cira/latency_g20_pr_gate_20260723
```

Expected: both rows are bit-exact PASS.

- [ ] **Step 3: Enforce the gate**

Run:

```bash
python3 scripts/validate_gapbs_amu_latency_sweep.py \
  --pr-gate \
  m5out/gapbs_cxl_amu_cira/latency_g20_pr_gate_20260723
```

Require baseline L2 demand-data misses greater than 4096, CIRA L2
demand-data misses strictly lower than baseline, CIRA useful greater than zero,
balanced positive CIRA leaf requests, positive future-row CSR descriptors, and
baseline ticks divided by CIRA ticks greater than 1.0.

Expected: `PASS: PR@1us scale-20 CIRA discriminator`. If any condition fails,
stop, preserve diagnostics, and do not run the matrix or replace the paper table.

### Task 6: Run the complete scale-20 matrix

**Files:**
- Generated: `m5out/gapbs_cxl_amu_cira/latency_table_g20_20260723/200ns/`
- Generated: `m5out/gapbs_cxl_amu_cira/latency_table_g20_20260723/500ns/`
- Generated: `m5out/gapbs_cxl_amu_cira/latency_table_g20_20260723/1us/`
- Generated: `m5out/gapbs_cxl_amu_cira/latency_table_g20_20260723/2us/`

- [ ] **Step 1: Run each latency**

For each `LATENCY` in `200ns 500ns 1us 2us`, run:

```bash
python3 scripts/compare_gapbs_cxl_amu_cira.py \
  --gem5 build/X86/gem5.opt \
  --baseline-bin-dir m5out/gapbs_baseline_bins_latency_g20/bin \
  --amu-bin-dir m5out/gapbs_amu_bins_latency_g20/bin \
  --cira-bin-dir cira_pgo=m5out/gapbs_cira_bins_latency_g20/bin \
  --benchmarks bfs,bc,pr,sssp --scale 20 --iterations 2 \
  --fast-forward-cpu atomic --cpu timing --measure-trial 1 --cores 1 \
  --cxl-link-delay LATENCY --roi-work-events --verify --timeout 7200 \
  --outdir m5out/gapbs_cxl_amu_cira/latency_table_g20_20260723/LATENCY
```

Expected: twelve bit-exact PASS rows per latency.

- [ ] **Step 2: Validate all 48 rows**

```bash
python3 scripts/validate_gapbs_amu_latency_sweep.py \
  m5out/gapbs_cxl_amu_cira/latency_table_g20_20260723 \
  --combined-output \
    m5out/gapbs_cxl_amu_cira/latency_table_g20_20260723/combined.csv \
  --validation-output \
    m5out/gapbs_cxl_amu_cira/latency_table_g20_20260723/validation.json
```

Expected: `PASS: 48/48 scale-20 rows`; config, CXL routing, evidence fields,
and integrity counters validate. Slowdowns remain valid numeric results.

- [ ] **Step 3: Freeze provenance**

Emit deterministic combined CSV and validation JSON beside the sweep, hashing
the gem5 binary, config, three manifests, four summaries, and every trial-1
stats file. Expected: a second validation run is byte-identical.

### Task 7: Regenerate and inspect the paper table

**Files:**
- Modify: `6472666535e6f359942ddac6/gapbs-amu-latency-results.csv`
- Modify: `6472666535e6f359942ddac6/gapbs-vtune-cxl-table.tex`
- Generated: `6472666535e6f359942ddac6/main.pdf`

- [ ] **Step 1: Generate from validated scale-20 evidence**

Run:

```bash
python3 scripts/generate_gapbs_amu_latency_table.py \
  --input \
    200ns=m5out/gapbs_cxl_amu_cira/latency_table_g20_20260723/200ns/summary.csv \
  --input \
    500ns=m5out/gapbs_cxl_amu_cira/latency_table_g20_20260723/500ns/summary.csv \
  --input \
    1us=m5out/gapbs_cxl_amu_cira/latency_table_g20_20260723/1us/summary.csv \
  --input \
    2us=m5out/gapbs_cxl_amu_cira/latency_table_g20_20260723/2us/summary.csv \
  --latex-output 6472666535e6f359942ddac6/gapbs-vtune-cxl-table.tex \
  --provenance-output \
    6472666535e6f359942ddac6/gapbs-amu-latency-results.csv
```

Expected: BFS/BC/PR/SSSP/Geo. row order and 200ns/500ns/1us/2us group order.
Values at or below 1.00x remain visible and are not called improvements.

- [ ] **Step 2: Reject stale provenance**

Assert every data row has `scale=20`, `iterations=2`, `measured_trial=1`,
`fast_forward_cpu=atomic`, `roi_cpu=timing`, and numeric miss/useful/late and
CXL packet/byte fields. Expected: no old scale-4 sweep path remains.

- [ ] **Step 3: Compile and inspect**

Run `make` in `6472666535e6f359942ddac6`, check the log for missing inputs,
overfull boxes, and unresolved references, then render and inspect the table
page. Expected: readable output; caption states scale 20, Atomic generation,
Timing trial-1 ROI, warmup trial, and bit-exact status.

- [ ] **Step 4: Final verification and commit**

```bash
python3 tests/pyunit/amu/pyunit_gapbs_amu_builder.py -v
python3 tests/pyunit/amu/test_cira_usefulness_contract.py -v
python3 tests/pyunit/amu/test_validate_gapbs_amu_latency_sweep.py -v
python3 tests/pyunit/amu/test_generate_gapbs_amu_latency_table.py -v
git diff --check
```

Run `git diff --check` in the Overleaf repository too. Expected: all tests and
both whitespace checks pass, all 48 rows remain validated, and no scale-4
performance claim survives.
