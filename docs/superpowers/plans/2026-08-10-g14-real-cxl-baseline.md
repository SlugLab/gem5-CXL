# G14 Real-CXL Baseline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Produce a validated 16-row g14 PageRank latency matrix in which Vanilla, AMU, coherent CIRA, and M2NDP use one frozen graph, all gem5 host memory is behind CXL, every result is raw bit-exact, and the Vanilla denominator demonstrably performs CPU-data reads at the memory controller.

**Architecture:** Extend the existing profile-driven g4/g20 workflow with a manifest-backed g12 qualification profile and frozen g14 formal profile. Add first-ROI memory-controller gates, remove AMU-only source flushing, replace CIRA functional CSR reads with bounded timing requests, freeze a latency-scaled 64-row rolling-window policy before the formal sweep, then reuse the existing matched gem5 and M2NDP pipelines under a resumable external run root. Publication remains fail-closed and atomic: the paper changes only after all 16 rows and an independent validator pass.

**Tech Stack:** Python 3.13, `unittest`, C++17, gem5 X86 TimingSimpleCPU, GAPBS/OpenMP, gem5 application checkpoints, CIRA/ASMC gem5 SimObjects, M2NDP FuncSim/NDPSim, Matplotlib, LaTeX, systemd transient services.

---

## Fixed paths and proof boundary

- Worktree: `/home/victoryang00/gem5-CXL/.worktrees/m2ndp-g20-pr-spmv`
- External run root: `/mnt/disk0/gem5-CXL-g14-eval`
- Stable worktree link: `m5out/g14-real-cxl-eval`
- Qualification manifest: `/mnt/disk0/gem5-CXL-g14-eval/graphs/g12.manifest.json`
- Formal graph manifest: `/mnt/disk0/gem5-CXL-g14-eval/graphs/g14.manifest.json`
- Canonical formal result root: `/mnt/disk0/gem5-CXL-g14-eval/formal`
- Paper repository: `/home/victoryang00/gem5-CXL/6472666535e6f359942ddac6`
- Formal matrix order: latency-major over `200ns`, `500ns`, `1us`, `2us`; within each latency use `vanilla`, `amu`, `cira`, `m2ndp`.
- Existing g4/g20 output and checkpoint trees are read-only evidence. Do not delete, rename, or reuse them.

## Task 1: Add manifest-backed g12 and g14 experiment profiles

**Files:**
- Modify: `scripts/gapbs_pr_experiment_profiles.py`
- Create: `scripts/prepare_gapbs_pr_graph.py`
- Modify: `tests/pyunit/m2ndp/test_gapbs_pr_experiment_profiles.py`
- Create: `tests/pyunit/m2ndp/test_prepare_gapbs_pr_graph.py`

- [ ] **Step 1: Write failing manifest and graph-freeze tests**

Add tests that create a temporary graph and generator binary, call `write_graph_manifest`, and assert the exact contract below:

```python
manifest = graph_prep.write_graph_manifest(
    graph=graph,
    scale=14,
    generator=generator,
    generator_command=[str(generator), "-g", "14", "-o", str(graph)],
    num_nodes=1 << 14,
    directed_edges=edges,
    output=manifest_path,
)
self.assertEqual(manifest["schema"], 1)
self.assertEqual(manifest["scale"], 14)
self.assertEqual(manifest["graph_sha256"], artifacts.sha256_file(graph))
self.assertEqual(
    manifest["generator_sha256"], artifacts.sha256_file(generator)
)
self.assertEqual(manifest["num_nodes"], 1 << 14)
self.assertEqual(manifest["directed_edges"], edges)
self.assertEqual(manifest["generator_command"], [
    str(generator), "-g", "14", "-o", str(graph)
])

profile = profiles.load_frozen_profile(
    "g14-4thread-sweep", manifest_path
)
self.assertEqual(profile.graph_scale, 14)
self.assertEqual(profile.graph_sha256, artifacts.sha256_file(graph))
self.assertEqual(profile.cores, 4)
self.assertEqual(profile.threads, 4)
self.assertEqual(profile.latencies, ("200ns", "500ns", "1us", "2us"))
```

Also test rejection of a changed graph, generator, command, scale, node count, nonpositive edge count, and a second attempt to overwrite an existing manifest with different contents.

- [ ] **Step 2: Run the focused tests and observe failure**

```bash
python3 -m unittest \
  tests.pyunit.m2ndp.test_gapbs_pr_experiment_profiles \
  tests.pyunit.m2ndp.test_prepare_gapbs_pr_graph -v
```

Expected: FAIL because `load_frozen_profile` and `prepare_gapbs_pr_graph.py` do not exist.

- [ ] **Step 3: Implement immutable manifest creation and loading**

Implement `FrozenGraphManifest` parsing with required keys and exact integer/string types. Use `os.open(..., os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o444)` for first publication; if the file already exists, accept it only when canonical JSON bytes are identical. Do not derive a new hash after the manifest is frozen.

Add this profile loader:

```python
FROZEN_PROFILE_CONTRACTS = {
    "g12-4thread-qualification": (12, ("1us",)),
    "g14-4thread-sweep": (14, ("200ns", "500ns", "1us", "2us")),
}

def load_frozen_profile(name: str, manifest_path: Path) -> ExperimentProfile:
    try:
        expected_scale, latencies = FROZEN_PROFILE_CONTRACTS[name]
    except KeyError as error:
        raise ProfileError(f"unknown frozen profile: {name}") from error
    manifest = load_graph_manifest(manifest_path)
    if manifest.scale != expected_scale:
        raise ProfileError(
            f"profile {name} requires scale {expected_scale}, got {manifest.scale}"
        )
    if manifest.num_nodes != 1 << expected_scale:
        raise ProfileError("graph node count does not match scale")
    if manifest.directed_edges <= 0:
        raise ProfileError("graph directed-edge count must be positive")
    validate_frozen_graph(manifest)
    return ExperimentProfile(
        name=name,
        graph_scale=expected_scale,
        graph_sha256=manifest.graph_sha256,
        num_nodes=manifest.num_nodes,
        cores=4,
        threads=4,
        latencies=latencies,
    )
```

The graph-preparation CLI must accept only `--scale 12` or `--scale 14`, execute the existing deterministic GAPBS generator once, parse nodes/edges from the serialized graph, hash both graph and generator, and freeze the manifest. It must refuse to regenerate an already-frozen graph.

- [ ] **Step 4: Run focused and compatibility tests**

```bash
python3 -m unittest \
  tests.pyunit.m2ndp.test_gapbs_pr_experiment_profiles \
  tests.pyunit.m2ndp.test_prepare_gapbs_pr_graph \
  tests.pyunit.m2ndp.test_m2ndp_artifacts -v
```

Expected: PASS, including unchanged g4 and g20 profile tests.

- [ ] **Step 5: Commit the graph contract**

```bash
git add scripts/gapbs_pr_experiment_profiles.py \
  scripts/prepare_gapbs_pr_graph.py \
  tests/pyunit/m2ndp/test_gapbs_pr_experiment_profiles.py \
  tests/pyunit/m2ndp/test_prepare_gapbs_pr_graph.py
git commit -m "feat: freeze g12 and g14 PageRank graph contracts"
```

## Task 2: Replace the packet-only gate with first-ROI real-CXL evidence

**Files:**
- Modify: `scripts/compare_gapbs_cxl_amu_cira.py`
- Modify: `scripts/m2ndp_results.py`
- Modify: `tests/pyunit/amu/test_compare_gapbs_cxl_amu_cira.py`
- Modify: `tests/pyunit/m2ndp/test_m2ndp_results.py`

- [ ] **Step 1: Add failing exact-stat tests**

Construct two ROI sections whose first section contains:

```text
board.memory.mem_ctrl.readReqs 31
board.memory.mem_ctrl.readBursts 29
board.memory.mem_ctrl.bytesReadSys 1856
board.memory.mem_ctrl.requestorReadAccesses::processor.cores0.core.data 7
board.memory.mem_ctrl.requestorReadAccesses::processor.cores1.core.data 8
board.memory.mem_ctrl.requestorReadAccesses::processor.cores2.core.data 6
board.memory.mem_ctrl.requestorReadAccesses::processor.cores3.core.data 8
```

Assert exact `Decimal` extraction into:

```python
{
    "mem_ctrl_read_reqs": Decimal(31),
    "mem_ctrl_read_bursts": Decimal(29),
    "mem_ctrl_bytes_read": Decimal(1856),
    "mem_ctrl_cpu_data_reads": Decimal(29),
}
```

Add separate tests that reject zero or missing values, reject instruction-only requestors, ignore positive values that appear only in the second ROI section, preserve integers above `2**53`, and continue reporting directional membus packet cells as diagnostics only.

- [ ] **Step 2: Run the focused tests and observe failure**

```bash
python3 -m unittest \
  tests.pyunit.amu.test_compare_gapbs_cxl_amu_cira \
  tests.pyunit.m2ndp.test_m2ndp_results -v
```

Expected: FAIL because the four real-CXL fields are not extracted or validated.

- [ ] **Step 3: Implement exact first-ROI extraction and validation**

Add constants and helpers:

```python
MEM_CTRL_EXACT_STATS = {
    "mem_ctrl_read_reqs": "board.memory.mem_ctrl.readReqs",
    "mem_ctrl_read_bursts": "board.memory.mem_ctrl.readBursts",
    "mem_ctrl_bytes_read": "board.memory.mem_ctrl.bytesReadSys",
}
CPU_DATA_READ_PREFIX = (
    "board.memory.mem_ctrl.requestorReadAccesses::processor.cores"
)
CPU_DATA_READ_SUFFIX = ".core.data"

def real_cxl_metrics(stats: dict[str, Decimal], cores: int) -> dict[str, Decimal]:
    metrics = {
        field: require_exact_stat(stats, stat)
        for field, stat in MEM_CTRL_EXACT_STATS.items()
    }
    metrics["mem_ctrl_cpu_data_reads"] = sum(
        (
            require_exact_stat(
                stats, f"{CPU_DATA_READ_PREFIX}{core}{CPU_DATA_READ_SUFFIX}"
            )
            for core in range(cores)
        ),
        Decimal(0),
    )
    return metrics

def require_real_cxl(metrics: dict[str, Decimal]) -> None:
    for field in (
        "mem_ctrl_read_reqs", "mem_ctrl_read_bursts",
        "mem_ctrl_bytes_read", "mem_ctrl_cpu_data_reads",
    ):
        if metrics[field] <= 0:
            raise ResultError(f"{field} must be positive in measured ROI")
```

Keep all values as integer strings or `Decimal`; never pass these counters through `float`. Call `require_real_cxl` for every formal Vanilla row and for the g12 Vanilla qualification row.

- [ ] **Step 4: Run focused tests**

```bash
python3 -m unittest \
  tests.pyunit.amu.test_compare_gapbs_cxl_amu_cira \
  tests.pyunit.m2ndp.test_m2ndp_results -v
```

Expected: PASS.

- [ ] **Step 5: Commit the evidence gate**

```bash
git add scripts/compare_gapbs_cxl_amu_cira.py scripts/m2ndp_results.py \
  tests/pyunit/amu/test_compare_gapbs_cxl_amu_cira.py \
  tests/pyunit/m2ndp/test_m2ndp_results.py
git commit -m "fix: require measured ROI CPU data reads through CXL"
```

## Task 3: Make Vanilla endpoint sensitivity a formal aggregate gate

**Files:**
- Modify: `scripts/generate_gapbs_g4_4thread_latency_results.py`
- Modify: `tests/pyunit/m2ndp/test_generate_gapbs_g4_4thread_latency_results.py`

- [ ] **Step 1: Add failing endpoint and counter-consistency tests**

Refactor the aggregator entry point to accept a selected profile and add tests for a g14 matrix. Assert that:

```python
publisher.validate_vanilla_endpoints(rows)
```

rejects `vanilla_2us_ticks <= vanilla_200ns_ticks`, rejects zero real-CXL counters, and rejects counter divergence above a fixed 5% relative range unless an evidence JSON file contains a nonempty `vanilla_counter_variation_explanation`. Do not require strict ordering for 500 ns or 1 us.

- [ ] **Step 2: Run and observe failure**

```bash
python3 -m unittest \
  tests.pyunit.m2ndp.test_generate_gapbs_g4_4thread_latency_results -v
```

Expected: FAIL because no endpoint gate exists.

- [ ] **Step 3: Implement the profile-neutral endpoint validator**

Use `Decimal` throughout:

```python
def validate_vanilla_endpoints(rows, explanation=""):
    vanilla = {row["latency"]: row for row in rows
               if row["system"] == "vanilla"}
    delta = Decimal(vanilla["2us"]["roi_ticks"]) - Decimal(
        vanilla["200ns"]["roi_ticks"]
    )
    if delta <= 0:
        raise PublicationError(
            "Vanilla 2us ROI must be slower than Vanilla 200ns ROI"
        )
    for field in REAL_CXL_FIELDS:
        values = [Decimal(vanilla[latency][field]) for latency in LATENCIES]
        if min(values) <= 0:
            raise PublicationError(f"{field} must be positive")
        if (max(values) - min(values)) / min(values) > Decimal("0.05") \
                and not explanation.strip():
            raise PublicationError(
                f"{field} varies by more than 5 percent without explanation"
            )
    return delta
```

Preserve g4 parsing but do not classify old g4 rows as valid real-CXL performance evidence.

- [ ] **Step 4: Run tests**

```bash
python3 -m unittest \
  tests.pyunit.m2ndp.test_generate_gapbs_g4_4thread_latency_results -v
```

Expected: PASS.

- [ ] **Step 5: Commit the aggregate gate**

```bash
git add scripts/generate_gapbs_g4_4thread_latency_results.py \
  tests/pyunit/m2ndp/test_generate_gapbs_g4_4thread_latency_results.py
git commit -m "test: gate PageRank results on Vanilla CXL sensitivity"
```

## Task 4: Remove AMU source-flush asymmetry and preserve batched bit-exact order

**Files:**
- Modify: `scripts/build_gapbs_amu_cxlmemuring.py`
- Modify: `scripts/build_gapbs_matched_pr_spmv_variants.py`
- Modify: `tests/pyunit/amu/pyunit_gapbs_amu_builder.py`
- Modify: `tests/pyunit/amu/test_asmc_coherent_spm_writeback.py`
- Modify: `tests/pyunit/m2ndp/test_matched_pr_spmv_variants.py`

- [ ] **Step 1: Replace old flush expectations with failing symmetry tests**

Assert the generated AMU header and matched source contain none of:

```python
self.assertNotIn("flush_source_lines", header)
self.assertNotIn("_mm_clflush", header)
self.assertNotIn("prime_graph_pages(g)", generated)
self.assertNotIn("prime_worker_stack_pages()", generated)
self.assertNotIn("load_value(", generated_pull_loop)
```

Assert the pull loop still calls `gapbs_amu::load_values` exactly twice per batch. Assert `load_values` constructs one bounded `LoadWindow`, calls `issue_all()` once, calls `wait_all()` once, and copies values in ascending slot order. Assert the final float32 accumulation visits `scores_batch[0]` through `scores_batch[amu_count - 1]` in ascending original neighbor order.

- [ ] **Step 2: Run the tests and observe failure**

```bash
python3 -m unittest \
  tests.pyunit.amu.pyunit_gapbs_amu_builder \
  tests.pyunit.amu.test_asmc_coherent_spm_writeback \
  tests.pyunit.m2ndp.test_matched_pr_spmv_variants -v
```

Expected: FAIL because source lines are flushed and AMU-only priming remains.

- [ ] **Step 3: Remove source flushes while retaining destination safety**

Delete `flush_source_lines` and both callers. Keep scratchpad destination invalidation, completion-ID ownership, `_mm_mfence`, bounded issue, and batch completion waits. Remove AMU-only page/stack priming from the formal matched source. Do not change Vanilla or CIRA warmup.

The PageRank replacement must retain this exact dependency structure and the existing `ScoreT` type:

```cpp
auto neigh = g.in_neigh(u);
for (auto v_it = neigh.begin(); v_it != neigh.end();) {
  const NodeID *node_addrs[GAPBS_AMU_BATCH_SIZE];
  NodeID nodes[GAPBS_AMU_BATCH_SIZE];
  const ScoreT *score_addrs[GAPBS_AMU_BATCH_SIZE];
  ScoreT scores_batch[GAPBS_AMU_BATCH_SIZE];
  size_t amu_count = 0;
  for (; v_it != neigh.end() && amu_count < GAPBS_AMU_BATCH_SIZE;
       ++v_it)
    node_addrs[amu_count++] = &*v_it;
  gapbs_amu::load_values(node_addrs, nodes, amu_count);
  for (size_t amu_i = 0; amu_i < amu_count; ++amu_i)
    score_addrs[amu_i] = &outgoing_contrib[nodes[amu_i]];
  gapbs_amu::load_values(score_addrs, scores_batch, amu_count);
  for (size_t amu_i = 0; amu_i < amu_count; ++amu_i)
    incoming_total = incoming_total + scores_batch[amu_i];
}
```

Retain the existing invalid-node fail-closed check between the two stages. Do not change the outer PageRank arithmetic expression or loop order.

- [ ] **Step 4: Prove coherent dirty-source behavior and builder compatibility**

```bash
python3 -m unittest \
  tests.pyunit.amu.pyunit_gapbs_amu_builder \
  tests.pyunit.amu.test_asmc_coherent_spm_writeback \
  tests.pyunit.m2ndp.test_matched_pr_spmv_variants -v
```

Then run the existing ASMC coherent integration test command printed by `test_asmc_coherent_spm_writeback.py`; require exit code 0, exact dirty-data readback, and issued equals completed. Disassembly may still contain destination-SPM `clflush`; the source test must prove no call site can apply it to an AMU load source.

- [ ] **Step 5: Commit the AMU fix**

```bash
git add scripts/build_gapbs_amu_cxlmemuring.py \
  scripts/build_gapbs_matched_pr_spmv_variants.py \
  tests/pyunit/amu/pyunit_gapbs_amu_builder.py \
  tests/pyunit/amu/test_asmc_coherent_spm_writeback.py \
  tests/pyunit/m2ndp/test_matched_pr_spmv_variants.py
git commit -m "fix: make AMU loads coherent without source flushes"
```

## Task 5: Implement the frozen CIRA 64-row rolling-window policy

**Files:**
- Modify: `scripts/build_gapbs_cira_cxlmemuring.py`
- Modify: `scripts/build_gapbs_matched_pr_spmv_variants.py`
- Create: `scripts/cira_lead_policy.py`
- Modify: `tests/pyunit/m2ndp/test_matched_pr_spmv_variants.py`
- Create: `tests/pyunit/amu/test_cira_lead_policy.py`

- [ ] **Step 1: Add failing ownership, boundary, and scaling tests**

Test these exact mappings:

```python
self.assertEqual(policy.lead_blocks_for_latency(2, 200), 1)
self.assertEqual(policy.lead_blocks_for_latency(2, 500), 1)
self.assertEqual(policy.lead_blocks_for_latency(2, 1000), 2)
self.assertEqual(policy.lead_blocks_for_latency(2, 2000), 4)
self.assertEqual(policy.ROW_BLOCK_SIZE, 64)
self.assertEqual(policy.CANDIDATE_1US_LEADS, (1, 2, 4, 8))
```

For uneven static partitions, assert each submission starts on a 64-row boundary relative to that thread's partition, submits at most 64 rows, is strictly ahead of the current block, never crosses `thread_end`, and is never issued by a different thread.

- [ ] **Step 2: Run and observe failure**

```bash
python3 -m unittest \
  tests.pyunit.amu.test_cira_lead_policy \
  tests.pyunit.m2ndp.test_matched_pr_spmv_variants -v
```

Expected: FAIL because row batch defaults to 256 and no frozen lead module exists.

- [ ] **Step 3: Implement lead selection and row-window calculation**

```python
ROW_BLOCK_SIZE = 64
CANDIDATE_1US_LEADS = (1, 2, 4, 8)

def lead_blocks_for_latency(selected_1us: int, latency_ns: int) -> int:
    if selected_1us not in CANDIDATE_1US_LEADS:
        raise LeadPolicyError("1us lead is outside the qualification set")
    if latency_ns <= 0:
        raise LeadPolicyError("latency must be positive")
    return max(1, (selected_1us * latency_ns + 999) // 1000)

def select_1us_lead(candidate_rows):
    for lead in CANDIDATE_1US_LEADS:
        row = candidate_rows[lead]
        if row["queue_rejections"] == 0 \
                and row["dropped_descriptors"] == 0 \
                and row["useful_prefetches"] > row["late_prefetches"]:
            return lead
    raise LeadPolicyError("no 1us CIRA lead passed qualification")
```

Change the generated C++ helper to interpret `GAPBS_CIRA_LEAD_BLOCKS` as whole 64-row blocks. At a current block boundary, compute `future_begin = current_block_begin + lead_blocks * 64`, then submit `[future_begin, min(future_begin + 64, thread_end))`. Remove the old free-form node-distance behavior from formal builds while leaving legacy smoke builds compatible.

- [ ] **Step 4: Run focused tests**

```bash
python3 -m unittest \
  tests.pyunit.amu.test_cira_lead_policy \
  tests.pyunit.m2ndp.test_matched_pr_spmv_variants -v
```

Expected: PASS.

- [ ] **Step 5: Commit the rolling-window policy**

```bash
git add scripts/cira_lead_policy.py scripts/build_gapbs_cira_cxlmemuring.py \
  scripts/build_gapbs_matched_pr_spmv_variants.py \
  tests/pyunit/amu/test_cira_lead_policy.py \
  tests/pyunit/m2ndp/test_matched_pr_spmv_variants.py
git commit -m "feat: add frozen CIRA rolling block lead policy"
```

## Task 6: Replace timed CIRA functional CSR reads with bounded timing requests

**Files:**
- Modify: `src/mem/CIRA.py`
- Modify: `src/mem/cira.hh`
- Modify: `src/mem/cira.cc`
- Modify: `configs/example/gem5_library/x86-gapbs-amu-se.py`
- Modify: `scripts/validate_gapbs_amu_latency_sweep.py`
- Modify: `tests/pyunit/amu/test_cira_usefulness_contract.py`
- Modify: `tests/pyunit/amu/test_validate_gapbs_amu_latency_sweep.py`
- Modify: `tests/gem5/cira/cira_multicore_prefetch.cc`
- Modify: `tests/gem5/cira/run_cira_multicore.py`

- [ ] **Step 1: Add failing static and runtime contract tests**

The static test must isolate `CIRA::processCsrWalk()` and assert it contains no call to `readIndex` or `readGuest`. It must assert the model declares `timingCsrTraversal`, a bounded `maxCsrIndexReads`, a distinct CSR-index sender-state role, and the following stats:

```text
csrIndexReadPackets
csrIndexReadBytes
completedCsrIndexReads
rejectedCsrIndexQueueFull
timingCsrTraversalEnabled
```

Extend the multicore runtime test to dirty the changing PageRank values in host caches, keep the immutable CSR indices in device memory, issue one descriptor per core, and require all four cores to complete with exact values, nonzero timing index packets, and zero ownership/drop counters. Add a config test that rejects a CSR walker connected to a host L2 or on the CPU side of the CXL link.

- [ ] **Step 2: Run and observe failure**

```bash
python3 -m unittest \
  tests.pyunit.amu.test_cira_usefulness_contract \
  tests.pyunit.amu.test_validate_gapbs_amu_latency_sweep -v
```

Expected: FAIL because `processCsrWalk()` calls functional `readIndex`.

- [ ] **Step 3: Add packet roles and bounded traversal state**

Add a distinct near-memory `csr_mem_side_port` and these concepts to `cira.hh`:

```cpp
enum class PacketRole { PrefetchLine, CsrIndexRead };

struct PacketSenderState : public Packet::SenderState {
  PacketSenderState(PacketRole role, uint64_t request_id,
                    PortID target_core, uint64_t walk_id = 0,
                    uint64_t entry = 0)
      : role(role), id(request_id), targetCore(target_core),
        walkId(walk_id), entry(entry) {}
  PacketRole role;
  uint64_t id;
  PortID targetCore;
  uint64_t walkId;
  uint64_t entry;
};

struct PendingCsrIndexRead {
  uint64_t walkId;
  PortID targetCore;
  ThreadContext *tc;
  Addr valuesAddr;
  uint64_t valueSize;
  uint64_t indexSize;
};
```

Give each walk a stable ID, retain it until all issued index reads have returned, and count queued plus in-flight reads against `max_csr_index_reads`. CSR index `ReadReq` packets use the dedicated `csr_mem_side_port`; they must never enter or fill a host L2. On response, decode exactly 1/2/4/8 little-endian bytes, enqueue the value-line prefetch through the issuing core's existing coherent CIRA-to-private-L2 port, update traversal stats, and only then retire the walk entry. Prefetch usefulness tracking must ignore index-read packets.

In `x86-gapbs-amu-se.py`, insert a device-side `NoncoherentXBar` between each CXL `SerialLink.mem_side_port` and its memory-controller port. Connect `board.cira.csr_mem_side_port` to that device-side crossbar's `cpu_side_ports`, while the controller remains behind the crossbar's `mem_side_ports`. Thus host demands and coherent soft-prefetches cross the configured CXL link, whereas CIRA's internal CSR traversal reaches device memory locally. The topology validator must prove this exact split and reject any direct membus-to-controller path.

Set `timing_csr_traversal=True` by default in `CIRA.py`. Functional `readIndex` may remain for indexed debug/checkpoint paths, but timed CSR descriptors must fail closed if timing traversal is disabled in a formal run.

- [ ] **Step 4: Build and run the multicore timing-CSR proof**

```bash
scons build/X86/gem5.opt -j4
python3 -m unittest tests.pyunit.amu.test_cira_usefulness_contract -v
build/X86/gem5.opt \
  --outdir=/mnt/disk0/gem5-CXL-g14-eval/tests/cira-timing-csr \
  tests/gem5/cira/run_cira_multicore.py
```

Expected: build succeeds; the runtime exits 0; all four per-core issued/completed counters are positive; `csrIndexReadPackets`, `csrIndexReadBytes`, and `completedCsrIndexReads` are positive; queue rejection, dropped descriptor, ownership mismatch, and incomplete request counts are zero.

- [ ] **Step 5: Commit the timing traversal**

```bash
git add src/mem/CIRA.py src/mem/cira.hh src/mem/cira.cc \
  configs/example/gem5_library/x86-gapbs-amu-se.py \
  scripts/validate_gapbs_amu_latency_sweep.py \
  tests/pyunit/amu/test_cira_usefulness_contract.py \
  tests/pyunit/amu/test_validate_gapbs_amu_latency_sweep.py \
  tests/gem5/cira/cira_multicore_prefetch.cc \
  tests/gem5/cira/run_cira_multicore.py
git commit -m "fix: model CIRA CSR traversal with timing reads"
```

## Task 7: Build a resumable g12 qualification and frozen-lead runner

**Files:**
- Create: `scripts/run_gapbs_g12_qualification.py`
- Create: `tests/pyunit/m2ndp/test_run_gapbs_g12_qualification.py`
- Modify: `scripts/run_gapbs_matched_pr_spmv_variants.py`
- Modify: `tests/pyunit/m2ndp/test_run_gapbs_matched_pr_spmv_variants.py`

- [ ] **Step 1: Add failing phase and fail-closed tests**

Assert this exact action order:

```python
(
    "vanilla-1us",
    "amu-1us",
    "cira-lead-1-1us",
    "cira-lead-2-1us",
    "cira-lead-4-1us",
    "cira-lead-8-1us",
    "freeze-cira-policy",
)
```

The runner must stop at the first passing CIRA candidate, never inspect speedup, reject a resumed stage whose command/input/binary/output hashes differ, and write `qualification.json` with either `g12_real_cxl=true` or `g12_cache_resident=true`. Cache-resident g12 advances to g14 pre-formal qualification; it does not weaken the traffic gate.

- [ ] **Step 2: Run and observe failure**

```bash
python3 -m unittest \
  tests.pyunit.m2ndp.test_run_gapbs_g12_qualification \
  tests.pyunit.m2ndp.test_run_gapbs_matched_pr_spmv_variants -v
```

Expected: FAIL because the qualification runner does not exist.

- [ ] **Step 3: Implement qualification orchestration**

Use the manifest-backed `g12-4thread-qualification` profile, four timing cores, `OMP_NUM_THREADS=4`, two trials, 20 iterations, and the pre-trial-0 checkpoint. Validate raw vector SHA equality after every mechanism. A CIRA candidate passes only when:

```python
def cira_candidate_passes(row):
    return (
        int(row["issued_csr_prefetches"]) > 0
        and int(row["completed_prefetches"]) > 0
        and all(int(row[f"issued_csr_prefetches_core{core}"]) > 0
                for core in range(4))
        and all(int(row[f"completed_prefetches_core{core}"]) > 0
                for core in range(4))
        and int(row["useful_prefetches"]) > int(row["late_prefetches"])
        and int(row["rejected_queue_full"]) == 0
        and int(row["rejected_csr_index_queue_full"]) == 0
        and int(row["dropped_csr_descriptors"]) == 0
        and row["timing_csr_traversal"] == "true"
    )
```

Freeze the smallest passing lead to `policy/cira-lead.json` using exclusive creation and include qualification result hashes. If no g12 candidate passes because the graph is cache-resident, generate/freeze g14 and run the same 1 us lead grid before any formal timing row; select only from CIRA activity counters.

- [ ] **Step 4: Run qualification unit tests**

```bash
python3 -m unittest \
  tests.pyunit.m2ndp.test_run_gapbs_g12_qualification \
  tests.pyunit.m2ndp.test_run_gapbs_matched_pr_spmv_variants -v
```

Expected: PASS.

- [ ] **Step 5: Commit the qualification runner**

```bash
git add scripts/run_gapbs_g12_qualification.py \
  scripts/run_gapbs_matched_pr_spmv_variants.py \
  tests/pyunit/m2ndp/test_run_gapbs_g12_qualification.py \
  tests/pyunit/m2ndp/test_run_gapbs_matched_pr_spmv_variants.py
git commit -m "feat: add g12 real-CXL qualification workflow"
```

## Task 8: Add the external-storage, low-priority g14 formal orchestrator

**Files:**
- Create: `scripts/run_gapbs_g14_4thread_latency_sweep.py`
- Create: `tests/pyunit/m2ndp/test_run_gapbs_g14_4thread_latency_sweep.py`
- Modify: `scripts/run_gapbs_g4_4thread_latency_sweep.py`
- Modify: `docs/amu-gapbs-benchmark.md`

- [ ] **Step 1: Add failing matrix, storage, resume, and command tests**

Assert 16 latency-major actions, exact frozen-profile arguments, four cores/threads, all-memory-CXL topology, pre-trial-0 checkpoint semantics, per-latency lead scaling, and sequential execution. Test that the runner rejects:

- less than 100 GiB free on `/mnt/disk0` before graph generation or a formal latency;
- a missing `m5out/g14-real-cxl-eval` symlink;
- a symlink whose resolved target is not `/mnt/disk0/gem5-CXL-g14-eval`;
- any output, command, graph, binary, config, checkpoint, or policy hash mismatch on resume; and
- any attempt to place formal outputs on `/`.

- [ ] **Step 2: Run and observe failure**

```bash
python3 -m unittest \
  tests.pyunit.m2ndp.test_run_gapbs_g14_4thread_latency_sweep -v
```

Expected: FAIL because the g14 orchestrator does not exist.

- [ ] **Step 3: Implement the formal runner by reusing tested g4 primitives**

Do not copy the full g4 runner. Extract shared immutable action/state/hash helpers into the g4 module or a small shared module, then make the g14 runner load `g14.manifest.json` and `cira-lead.json`. Build this matrix:

```python
LATENCIES = ("200ns", "500ns", "1us", "2us")
SYSTEMS = ("vanilla", "amu", "cira", "m2ndp")
MATRIX = tuple((latency, system)
               for latency in LATENCIES for system in SYSTEMS)
```

Before each latency, check free bytes with `shutil.disk_usage(EXTERNAL_ROOT).free >= 100 * 1024**3`. Write status atomically. Completed stages are reusable only when every recorded hash still matches. Never delete an output directory and never fall back to another filesystem.

Document creation of the stable link and service:

```bash
mkdir -p /mnt/disk0/gem5-CXL-g14-eval
ln -s /mnt/disk0/gem5-CXL-g14-eval \
  /home/victoryang00/gem5-CXL/.worktrees/m2ndp-g20-pr-spmv/m5out/g14-real-cxl-eval
systemd-run --user --unit=gem5-g14-real-cxl \
  --property=Nice=15 --property=IOSchedulingClass=idle \
  --collect /usr/bin/python3 \
  /home/victoryang00/gem5-CXL/.worktrees/m2ndp-g20-pr-spmv/scripts/run_gapbs_g14_4thread_latency_sweep.py \
  --root /mnt/disk0/gem5-CXL-g14-eval --resume
```

The script itself must verify the resolved link and root before launching.

- [ ] **Step 4: Run orchestrator and legacy runner tests**

```bash
python3 -m unittest \
  tests.pyunit.m2ndp.test_run_gapbs_g14_4thread_latency_sweep \
  tests.pyunit.m2ndp.test_run_gapbs_g4_4thread_latency_sweep -v
```

Expected: PASS, including unchanged g4 behavior.

- [ ] **Step 5: Commit the formal orchestrator**

```bash
git add scripts/run_gapbs_g14_4thread_latency_sweep.py \
  scripts/run_gapbs_g4_4thread_latency_sweep.py \
  tests/pyunit/m2ndp/test_run_gapbs_g14_4thread_latency_sweep.py \
  docs/amu-gapbs-benchmark.md
git commit -m "feat: orchestrate resumable g14 real-CXL sweep"
```

## Task 9: Bind M2NDP trace, FuncSim, and calibration to frozen g14 evidence

**Files:**
- Modify: `scripts/run_m2ndp_g20_pr_spmv.py`
- Modify: `scripts/m2ndp_pagerank_trace.py`
- Modify: `scripts/m2ndp_results.py`
- Modify: `tests/pyunit/m2ndp/test_run_m2ndp_g20_pr_spmv.py`
- Modify: `tests/pyunit/m2ndp/test_m2ndp_trace.py`
- Modify: `tests/pyunit/m2ndp/test_m2ndp_results.py`

- [ ] **Step 1: Add failing g14 trace and per-latency calibration tests**

Assert the trace manifest records the frozen g14 graph SHA, 16,384 nodes, directed-edge count, four-stage launch sequence, two trials, trial 1 ROI, and 20 synchronous double-buffered iterations. Assert FuncSim compares raw bytes against the matching-latency gem5 Vanilla vector and reports zero mismatched elements.

For each latency, assert:

```python
abs(Decimal(calibration["m2ndp_boundary_ns"]) -
    Decimal(calibration["gem5_microprobe_ns"])) <= Decimal("0.125")
```

Reject reuse of a calibration, trace, Vanilla vector, or graph from another latency/profile.

- [ ] **Step 2: Run and observe failure**

```bash
python3 -m unittest \
  tests.pyunit.m2ndp.test_run_m2ndp_g20_pr_spmv \
  tests.pyunit.m2ndp.test_m2ndp_trace \
  tests.pyunit.m2ndp.test_m2ndp_results -v
```

Expected: FAIL because the current runner does not consume a manifest-backed g14 profile at every proof boundary.

- [ ] **Step 3: Implement profile-bound M2NDP stages**

Pass `--profile g14-4thread-sweep --profile-manifest /mnt/disk0/gem5-CXL-g14-eval/graphs/g14.manifest.json` through trace generation, FuncSim, calibration, NDPSim, and result aggregation. Record SHA-256 for the trace, kernel binary, FuncSim output, NDPSim configuration, calibration JSON, and same-latency Vanilla raw vector.

Keep graph conversion, trace generation, FuncSim, calibration search, and validation outside ROI. NDPSim ROI must cover the complete trial-1 four-stage, 20-iteration launch sequence. Do not change M2NDP's native NDP-unit topology.

- [ ] **Step 4: Run M2NDP tests**

```bash
python3 -m unittest \
  tests.pyunit.m2ndp.test_run_m2ndp_g20_pr_spmv \
  tests.pyunit.m2ndp.test_m2ndp_trace \
  tests.pyunit.m2ndp.test_m2ndp_results \
  tests.pyunit.m2ndp.test_m2ndp_calibration -v
```

Expected: PASS, including unchanged g20 behavior.

- [ ] **Step 5: Commit the M2NDP binding**

```bash
git add scripts/run_m2ndp_g20_pr_spmv.py \
  scripts/m2ndp_pagerank_trace.py scripts/m2ndp_results.py \
  tests/pyunit/m2ndp/test_run_m2ndp_g20_pr_spmv.py \
  tests/pyunit/m2ndp/test_m2ndp_trace.py \
  tests/pyunit/m2ndp/test_m2ndp_results.py
git commit -m "feat: bind M2NDP evidence to frozen g14 runs"
```

## Task 10: Add the independent 16-row validator and atomic publication staging

**Files:**
- Create: `scripts/generate_gapbs_g14_4thread_latency_results.py`
- Create: `scripts/generate_gapbs_g14_4thread_latency_figure.py`
- Create: `scripts/validate_gapbs_g14_4thread_latency_results.py`
- Create: `tests/pyunit/m2ndp/test_generate_gapbs_g14_4thread_latency_results.py`
- Create: `tests/pyunit/m2ndp/test_generate_gapbs_g14_4thread_latency_figure.py`
- Create: `tests/pyunit/m2ndp/test_validate_gapbs_g14_4thread_latency_results.py`

- [ ] **Step 1: Add failing completeness, denominator, and atomicity tests**

Create synthetic passing evidence and assert exactly 16 unique `(latency, system)` rows. For each row independently recompute:

```python
expected = Decimal(vanilla_by_latency[row["latency"]]["roi_seconds"]) / \
           Decimal(row["roi_seconds"])
self.assertEqual(Decimal(row["speedup"]), expected)
```

Reject cross-latency or non-g14 denominators, vector length/hash mismatches, nonzero mismatch counts, failed mechanism gates, incomplete calibrations, missing provenance hashes, and publication when any row fails. Test that a failed generation leaves existing canonical files byte-identical.

- [ ] **Step 2: Run and observe failure**

```bash
python3 -m unittest \
  tests.pyunit.m2ndp.test_generate_gapbs_g14_4thread_latency_results \
  tests.pyunit.m2ndp.test_generate_gapbs_g14_4thread_latency_figure \
  tests.pyunit.m2ndp.test_validate_gapbs_g14_4thread_latency_results -v
```

Expected: FAIL because the g14 publication modules do not exist.

- [ ] **Step 3: Implement canonical outputs**

Generate under a uniquely named temporary sibling directory, fsync every file and the directory, validate it independently, rename it to an immutable content-hash directory, then atomically replace a `publication-current` symlink with `os.replace`. Readers resolve one complete version and never observe a mixed matrix. Emit:

```text
gapbs-g14-4thread-latency.csv
gapbs-g14-4thread-latency-evidence.json
gapbs-g14-4thread-latency-table-data.tex
gapbs-g14-4thread-latency-sweep.pdf
gapbs-g14-4thread-latency-sweep.svg
gapbs-g14-4thread-latency-validation.json
```

The CSV retains unrounded decimal ROI seconds/ticks, microseconds, speedup, all real-CXL counters, AMU/CIRA activity, raw vector length/hash, calibration residual, and every provenance hash. TeX rounds only for display. The independent validator reparses raw summaries rather than trusting generated speedups. Paper installation first stages and validates all destination files, then performs per-file `os.replace` with a rollback journal; any installation error restores every original file before returning failure.

- [ ] **Step 4: Run publication tests and inspect fixtures**

```bash
python3 -m unittest \
  tests.pyunit.m2ndp.test_generate_gapbs_g14_4thread_latency_results \
  tests.pyunit.m2ndp.test_generate_gapbs_g14_4thread_latency_figure \
  tests.pyunit.m2ndp.test_validate_gapbs_g14_4thread_latency_results -v
```

Expected: PASS, deterministic CSV/JSON/TeX content and deterministic plot labels/layout.

- [ ] **Step 5: Commit publication tooling**

```bash
git add scripts/generate_gapbs_g14_4thread_latency_results.py \
  scripts/generate_gapbs_g14_4thread_latency_figure.py \
  scripts/validate_gapbs_g14_4thread_latency_results.py \
  tests/pyunit/m2ndp/test_generate_gapbs_g14_4thread_latency_results.py \
  tests/pyunit/m2ndp/test_generate_gapbs_g14_4thread_latency_figure.py \
  tests/pyunit/m2ndp/test_validate_gapbs_g14_4thread_latency_results.py
git commit -m "feat: publish validated g14 latency matrix"
```

## Task 11: Run the qualification and staged g14 proof gates

**Files:**
- Create during execution: `/mnt/disk0/gem5-CXL-g14-eval/graphs/g12.sg`
- Create during execution: `/mnt/disk0/gem5-CXL-g14-eval/graphs/g12.manifest.json`
- Create during execution: `/mnt/disk0/gem5-CXL-g14-eval/graphs/g14.sg`
- Create during execution: `/mnt/disk0/gem5-CXL-g14-eval/graphs/g14.manifest.json`
- Create during execution: `/mnt/disk0/gem5-CXL-g14-eval/qualification/`
- Create during execution: `/mnt/disk0/gem5-CXL-g14-eval/preformal/`

- [ ] **Step 1: Run all fast tests before simulation**

```bash
python3 -m unittest discover -s tests/pyunit/amu -p 'test_*.py' -v
python3 -m unittest discover -s tests/pyunit/m2ndp -p 'test_*.py' -v
scons build/X86/gem5.opt -j4
```

Expected: all tests PASS and gem5 builds successfully.

- [ ] **Step 2: Verify storage and freeze deterministic graphs**

```bash
df -B1 /mnt/disk0
readlink -f m5out/g14-real-cxl-eval
python3 scripts/prepare_gapbs_pr_graph.py \
  --scale 12 --root /mnt/disk0/gem5-CXL-g14-eval/graphs
python3 scripts/prepare_gapbs_pr_graph.py \
  --scale 14 --root /mnt/disk0/gem5-CXL-g14-eval/graphs
```

Expected: at least 107,374,182,400 bytes free before generation; the link resolves exactly to the external root; both manifests validate their graph and generator hashes.

- [ ] **Step 3: Run g12 qualification**

```bash
python3 scripts/run_gapbs_g12_qualification.py \
  --root /mnt/disk0/gem5-CXL-g14-eval --resume
```

Expected: Vanilla/AMU/CIRA raw hashes match; AMU issued equals completed with zero loss; CIRA timing traversal is active on all four cores with zero drops. Either g12 Vanilla passes real-CXL counters and a lead freezes, or the manifest explicitly records cache residency and routes lead selection to g14/1 us pre-formal runs.

- [ ] **Step 4: Run one g14/1 us proof before the sweep**

```bash
python3 scripts/run_gapbs_g14_4thread_latency_sweep.py \
  --root /mnt/disk0/gem5-CXL-g14-eval \
  --only-latency 1us --stop-after cira --resume
```

Expected: g14 Vanilla has positive `readReqs`, `readBursts`, `bytesReadSys`, and CPU-data requestor reads; AMU and CIRA raw hashes match Vanilla; AMU has balanced completions; CIRA has timing traversal, all-core activity, and no drops.

- [ ] **Step 5: Run g14 FuncSim and NDPSim smoke**

```bash
python3 scripts/run_gapbs_g14_4thread_latency_sweep.py \
  --root /mnt/disk0/gem5-CXL-g14-eval \
  --only-latency 1us --resume
```

Expected: FuncSim mismatches are zero, launch sequence is complete, NDPSim cycles are positive, and 1 us calibration residual is at most 0.125 ns.

- [ ] **Step 6: Record the staged proof without committing generated bulk data**

```bash
python3 scripts/validate_gapbs_g14_4thread_latency_results.py \
  --allow-partial --root /mnt/disk0/gem5-CXL-g14-eval
git status --short
```

Expected: the 1 us partial validation passes; generated bulk data remains under `/mnt/disk0`; only intentional source/test/doc changes appear in Git.

## Task 12: Run the full sweep, publish into the paper, verify, commit, and push

**Files:**
- Generate: `/mnt/disk0/gem5-CXL-g14-eval/formal/publication/*`
- Replace: `/home/victoryang00/gem5-CXL/6472666535e6f359942ddac6/gapbs-vtune-cxl-table.tex`
- Modify: `/home/victoryang00/gem5-CXL/6472666535e6f359942ddac6/sections/evaluation.tex`
- Replace: `/home/victoryang00/gem5-CXL/6472666535e6f359942ddac6/fig/gapbs-g14-4thread-latency-sweep.pdf`
- Replace: `/home/victoryang00/gem5-CXL/6472666535e6f359942ddac6/fig/gapbs-g14-4thread-latency-sweep.svg`
- Add: canonical g14 CSV/JSON/table-data files under the paper repository's existing data location

- [ ] **Step 1: Launch the sequential resumable formal service**

```bash
systemd-run --user --unit=gem5-g14-real-cxl \
  --property=Nice=15 --property=IOSchedulingClass=idle \
  --collect /usr/bin/python3 \
  /home/victoryang00/gem5-CXL/.worktrees/m2ndp-g20-pr-spmv/scripts/run_gapbs_g14_4thread_latency_sweep.py \
  --root /mnt/disk0/gem5-CXL-g14-eval --resume
```

Monitor with:

```bash
systemctl --user status gem5-g14-real-cxl --no-pager
journalctl --user -u gem5-g14-real-cxl -n 80 --no-pager
python3 scripts/run_gapbs_g14_4thread_latency_sweep.py \
  --root /mnt/disk0/gem5-CXL-g14-eval --status
```

Expected: all 16 stages eventually report `passed`; restarting the same command resumes only hash-matched completed stages.

- [ ] **Step 2: Generate and independently validate publication artifacts**

```bash
python3 scripts/generate_gapbs_g14_4thread_latency_results.py \
  --root /mnt/disk0/gem5-CXL-g14-eval
python3 scripts/generate_gapbs_g14_4thread_latency_figure.py \
  --csv /mnt/disk0/gem5-CXL-g14-eval/formal/publication/gapbs-g14-4thread-latency.csv \
  --outdir /mnt/disk0/gem5-CXL-g14-eval/formal/publication
python3 scripts/validate_gapbs_g14_4thread_latency_results.py \
  --root /mnt/disk0/gem5-CXL-g14-eval
```

Expected: exactly 16 rows; all raw vector hashes and lengths match; all same-latency speedups recompute exactly; every Vanilla row has real CPU-data memory reads; 2 us Vanilla is slower than 200 ns Vanilla; all M2NDP residuals are at most 0.125 ns.

- [ ] **Step 3: Visually inspect the final PDF and SVG**

Render the PDF to PNG and inspect both formats:

```bash
pdftoppm -png -singlefile -r 160 \
  /mnt/disk0/gem5-CXL-g14-eval/formal/publication/gapbs-g14-4thread-latency-sweep.pdf \
  /mnt/disk0/gem5-CXL-g14-eval/formal/publication/gapbs-g14-4thread-latency-sweep-preview
```

Expected: four latency groups are legible; absolute latency and speedup encodings agree with the CSV; no clipped labels, overlapping legend, or hidden slowdown exists.

- [ ] **Step 4: Atomically replace the paper's old performance artifacts**

Use the publication script's explicit `--paper-root /home/victoryang00/gem5-CXL/6472666535e6f359942ddac6` mode only after the independent validation report says `passed`. Update the evaluation text to identify g12 as qualification only, g14 as publication, four host cores/threads, all-CXL memory, two trials/20 iterations, raw bit-exact validation, and per-latency Vanilla denominators. Remove the g4 performance claim but retain any correctly labeled g4 correctness discussion.

- [ ] **Step 5: Build the paper twice**

```bash
cd /home/victoryang00/gem5-CXL/6472666535e6f359942ddac6
pdflatex -interaction=nonstopmode -halt-on-error main.tex
pdflatex -interaction=nonstopmode -halt-on-error main.tex
```

Expected: both builds exit 0. If the repository's existing duplicate BibTeX-key warning remains, record it separately; do not describe a warning as a successful bibliography fix.

- [ ] **Step 6: Run final source, result, and paper verification**

```bash
cd /home/victoryang00/gem5-CXL/.worktrees/m2ndp-g20-pr-spmv
python3 -m unittest discover -s tests/pyunit/amu -p 'test_*.py' -v
python3 -m unittest discover -s tests/pyunit/m2ndp -p 'test_*.py' -v
python3 scripts/validate_gapbs_g14_4thread_latency_results.py \
  --root /mnt/disk0/gem5-CXL-g14-eval
git diff --check
git status --short --branch
cd /home/victoryang00/gem5-CXL/6472666535e6f359942ddac6
git diff --check
git status --short --branch
```

Expected: tests PASS, validation PASS, no whitespace errors, and only intended changes exist in both repositories.

- [ ] **Step 7: Commit and push the gem5 branch**

```bash
cd /home/victoryang00/gem5-CXL/.worktrees/m2ndp-g20-pr-spmv
git add scripts src/mem tests docs/amu-gapbs-benchmark.md
git commit -m "eval: publish bit-exact g14 real-CXL comparison"
git push origin m2ndp-g20-pr-spmv
```

If earlier task commits already contain all gem5 changes, skip the empty final commit and push the existing verified commits.

- [ ] **Step 8: Commit and push the paper branch**

```bash
cd /home/victoryang00/gem5-CXL/6472666535e6f359942ddac6
git add gapbs-vtune-cxl-table.tex sections/evaluation.tex fig \
  gapbs-g14-4thread-latency.csv \
  gapbs-g14-4thread-latency-evidence.json \
  gapbs-g14-4thread-latency-table-data.tex \
  gapbs-g14-4thread-latency-validation.json
git commit -m "eval: replace cache-hot g4 result with g14 CXL sweep"
git push
```

Before pushing, inspect `git diff --cached --stat` and `git diff --cached` in each repository. Do not commit raw checkpoints, simulator logs, traces, or other bulk files from `/mnt/disk0`.

## Completion checklist

- [ ] Frozen g12 and g14 manifests validate graph, generator, command, node, and edge identity.
- [ ] Vanilla first-ROI memory-controller data-read gates pass for all four g14 latencies.
- [ ] Vanilla 2 us ROI is slower than Vanilla 200 ns ROI.
- [ ] AMU formal source and binary contain no source flush or mechanism-only priming.
- [ ] AMU issued/completed requests balance and coherent dirty-data proof passes.
- [ ] CIRA uses timing CSR reads, four cores are active, and no bounded queue drops work.
- [ ] CIRA lead is frozen before formal speedups are inspected and scales from 64-row blocks.
- [ ] Vanilla, AMU, CIRA, FuncSim, and M2NDP raw output vectors are elementwise and bytewise identical.
- [ ] Every M2NDP latency calibration residual is at most 0.125 ns.
- [ ] All 16 same-latency speedups independently recompute from g14 Vanilla.
- [ ] Publication generation is atomic and paper PDF builds twice.
- [ ] Final figure is visually inspected.
- [ ] Both repositories are committed and pushed; old g4/g20 evidence remains untouched.
