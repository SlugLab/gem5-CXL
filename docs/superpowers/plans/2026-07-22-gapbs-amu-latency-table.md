# GAPBS AMU CXL-Latency Table Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the Overleaf GAPBS table with validated baseline-versus-AMU and baseline-versus-CIRA speedups at 200 ns, 500 ns, 1 us, and 2 us CXL latency.

**Architecture:** Rebuild matched baseline, AMU, and CIRA PGO/static binaries once, run the existing comparison driver once per latency, validate every run directory, combine the four summaries, and generate a compact LaTeX table from the combined CSV. Do not add CIRA-no-PGO. Keep experimental artifacts in gem5 `m5out`; only the generated table and a provenance CSV enter the Overleaf repository.

**Tech Stack:** Python 3, gem5 X86 timing model, GAPBS/CXLMemUring, CSV, LaTeX/USENIX template.

---

### Task 1: Build matched verifier-enabled binaries

**Files:**
- Generated: `m5out/gapbs_baseline_bins_latency_20260722/`
- Generated: `m5out/gapbs_amu_bins_latency_20260722/`
- Generated: `m5out/gapbs_cira_bins_latency_20260722/`

- [ ] **Step 1: Run the focused builder tests**

Run: `python3 tests/pyunit/amu/pyunit_gapbs_amu_builder.py -v`

Expected: 16 tests pass.

- [ ] **Step 2: Build baseline binaries**

Run:

```bash
python3 scripts/build_gapbs_baseline_cxlmemuring.py \
  --cxlmemuring /home/victoryang00/CXLMemUring \
  --benchmarks bfs,bc,pr,sssp --roi-work-markers \
  --outdir m5out/gapbs_baseline_bins_latency_20260722
```

Expected: four binaries and `manifest.json`.

- [ ] **Step 3: Build AMU binaries**

Run:

```bash
python3 scripts/build_gapbs_amu_cxlmemuring.py \
  --cxlmemuring /home/victoryang00/CXLMemUring \
  --benchmarks bfs,bc,pr,sssp --roi-work-markers --amu-batch-size 64 \
  --outdir m5out/gapbs_amu_bins_latency_20260722
```

Expected: four binaries and `manifest.json`.

- [ ] **Step 4: Build CIRA PGO/static binaries**

Run:

```bash
python3 scripts/build_gapbs_cira_cxlmemuring.py \
  --cxlmemuring /home/victoryang00/CXLMemUring \
  --benchmarks bfs,bc,pr,sssp --roi-work-markers \
  --outdir m5out/gapbs_cira_bins_latency_20260722
```

Expected: four binaries and `manifest.json`; the manifest records GAPBS
commit/dirty state; SHA-256 values for the complete transformed copied `.cc`/
`.h` tree, builder script, `libm5.a`, gem5 m5ops headers, CIRA headers, every
profile actually used, and emitted binaries. Missing or unusable profiles fail
the PGO build. An explicit prefetch-distance override is non-PGO, records the
override, claims no profiles, and is outside this paper experiment.

- [ ] **Step 5: Compare source manifests**

Run a Python assertion requiring equal `cxlmemuring_commit`, `gapbs_commit`,
and recorded `gapbs_dirty` fields across all three manifests, recomputed
copied compiler-input tree, builder, m5 library/header, instrumentation-header,
profile, and binary hashes, and the exact binary stem set
`{bfs,bc,pr,sssp}`.

Expected: exit 0.

### Task 2: Run the four latency sweeps

**Files:**
- Create: `scripts/validate_gapbs_amu_latency_sweep.py`
- Create: `tests/pyunit/amu/test_validate_gapbs_amu_latency_sweep.py`
- Generated: `m5out/gapbs_cxl_amu_cira/latency_table_20260722/{200ns,500ns,1us,2us}/`

- [ ] **Step 1: Run each latency**

For each latency in `200ns 500ns 1us 2us`, run:

```bash
python3 scripts/compare_gapbs_cxl_amu_cira.py \
  --gem5 build/X86/gem5.opt \
  --baseline-bin-dir m5out/gapbs_baseline_bins_latency_20260722/bin \
  --amu-bin-dir m5out/gapbs_amu_bins_latency_20260722/bin \
  --cira-bin-dir cira=m5out/gapbs_cira_bins_latency_20260722/bin \
  --benchmarks bfs,bc,pr,sssp --scale 4 --iterations 1 \
  --cpu timing --cores 1 --cxl-link-delay LATENCY \
  --roi-work-events --verify --timeout 600 \
  --outdir m5out/gapbs_cxl_amu_cira/latency_table_20260722/LATENCY
```

Expected: each invocation exits 0 and writes twelve rows.

- [ ] **Step 2: Write the failing sweep-validator tests**

Create a synthetic 48-row sweep fixture. Test that balanced AMU loads,
balanced CIRA leaf requests, and nonzero CIRA descriptors pass. Add a
regression fixture where eight leaf requests plus one descriptor numerically
equals nine completed leaf requests; require rejection because leaf and
descriptor counters are different domains.

Run: `python3 tests/pyunit/amu/test_validate_gapbs_amu_latency_sweep.py -v`

Expected: fail because the validator does not exist.

- [ ] **Step 3: Implement the independent sweep validator**

Accept the sweep root as one positional argument. Require 48 rows total,
twelve per latency, `status=ok`, `verification=pass`, numeric finite speedups,
and the expected `board.cxl_mem_link0.delay` in every `config.ini`. Read only
the first ROI stats section. Require
`board.asmc.issuedLoads == board.asmc.completedLoads > 0` for AMU rows. For
CIRA rows require leaf cacheline balance
`board.cira.issuedPrefetches == board.cira.completedPrefetches > 0` and
separately require descriptor use via
`board.cira.issuedIndexedPrefetches + board.cira.issuedCsrPrefetches > 0`.
There are no completed descriptor counters; never add descriptor counts to
leaf counts.

- [ ] **Step 4: Verify the validator and existing artifacts**

Run:

```bash
python3 tests/pyunit/amu/test_validate_gapbs_amu_latency_sweep.py -v
python3 scripts/validate_gapbs_amu_latency_sweep.py \
  m5out/gapbs_cxl_amu_cira/latency_table_20260722
```

Expected: a single `PASS: 48/48` message.

### Task 3: Generate the paper table

**Files:**
- Create: `scripts/generate_gapbs_amu_latency_table.py`
- Create: `tests/pyunit/amu/test_generate_gapbs_amu_latency_table.py`
- Create: `6472666535e6f359942ddac6/gapbs-amu-latency-results.csv`
- Modify: `6472666535e6f359942ddac6/gapbs-vtune-cxl-table.tex`

- [ ] **Step 1: Write generator tests**

Cover latency ordering, suppression of failed verification, geometric mean,
LaTeX escaping, and the exact BFS/BC/PR/SSSP row order.

Run: `python3 tests/pyunit/amu/test_generate_gapbs_amu_latency_table.py -v`

Expected: fail because the generator does not exist.

- [ ] **Step 2: Implement the generator**

The generator accepts four `LATENCY=summary.csv` inputs, validates baseline,
AMU, and CIRA row groups,
writes a provenance CSV containing raw ticks and verification status, and emits
a `table*` with four latency groups containing AMU speedup, CIRA speedup, and
PASS marker columns.

- [ ] **Step 3: Run generator tests and create artifacts**

Run the test above, then invoke the generator with the four validated summaries.

Expected: tests pass; CSV and LaTeX table are regenerated deterministically.

### Task 4: Compile and inspect the Overleaf paper

**Files:**
- Generated: `6472666535e6f359942ddac6/main.pdf`

- [ ] **Step 1: Compile the paper**

Run: `make`

Expected: `main.pdf` exists and LaTeX reports no missing table input.

- [ ] **Step 2: Check layout diagnostics**

Search the build log for overfull boxes and unresolved references, then render
the table page to an image and inspect it for clipped columns and unreadable
labels.

Expected: the replacement table fits the intended width and remains legible.

- [ ] **Step 3: Run final verification**

Run both pyunit files, `git diff --check` in the gem5 repository, and
`git diff --check` in the Overleaf repository.

Expected: all commands exit 0.
