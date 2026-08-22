# PR Asymmetric Near-Data Offload Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement bit-exact, fully charged AMU and coherent CIRA PageRank row-block execution, retain the native M2NDP four-stage path, and publish the accepted g12/g14/g20 nine-point comparison plus CIRA policy breakdown.

**Architecture:** A shared C ABI describes contribution and pull row blocks, while ASMC and CIRA own independent timing state machines and memory paths. A single four-thread double-buffered workload drives Vanilla, AMU, and CIRA; M2NDP lowers the same four logical partitions to its native kernels. One immutable campaign runner gates g12 and its deterministic replay before g14/g20, and one fail-closed publisher is the only path from raw evidence to the paper.

**Tech Stack:** C++17 gem5 SimObjects and timing ports, C++11/OpenMP GAPBS workloads, X86 m5ops, Python 3 standard library and `unittest`, M2NDP FuncSim/NDPSim, SHA-256 JSON/CSV evidence, Matplotlib Agg, LaTeX/latexmk.

---

## Scope and working-tree rules

Execute in the existing isolated worktree:

```bash
cd /home/victoryang00/gem5-CXL/.worktrees/m2ndp-g20-pr-spmv
git status --short --branch
```

The pre-existing modification to `src/mem/cache/base.cc` belongs to the user.
Do not edit, format, stage, discard, or commit it. Every `git add` command below
names exact files and intentionally excludes that path.

This plan produces one integrated feature because the descriptor ABI,
bit-exact workload, device models, and qualification evidence are mutually
dependent. The paper repository at
`/home/victoryang00/gem5-CXL/6472666535e6f359942ddac6` remains a separate Git
repository and is touched only after a formal `complete.json` passes.

## File responsibility map

- `util/pr_offload/pr_row_offload.h`: stable guest/model descriptor ABI,
  phases, flags, partition helper, and compile-time layout assertions.
- `src/mem/pr_row_math.hh`: explicit float32 divide/add/multiply helpers shared
  only to enforce the common numerical contract.
- `util/m2ndp/gapbs_pr_spmv_fixed.cc`: matched Vanilla double-buffered
  reference and raw-vector writer.
- `util/pr_offload/gapbs_pr_spmv_offload.cc`: four-thread AMU/CIRA workload,
  completion waits, CIRA policy sampling, and post-ROI phase markers.
- `src/mem/asmc.{hh,cc}` and `src/mem/ASMC.py`: AMU-native descriptor queue,
  timing reads, ordered reduction, writes, and statistics.
- `src/mem/cira.{hh,cc}` and `src/mem/CIRA.py`: coherent CIRA descriptor queue,
  CSR traversal, L2-facing rank/output traffic, reconfiguration, and stats.
- `include/gem5/{m5ops.h,asm/generic/m5ops.h}`,
  `src/sim/pseudo_inst.{hh,cc}`, `util/{amu/amu.h,cira/cira.h}`: two new
  descriptor m5ops and their guest wrappers.
- `scripts/build_gapbs_{m2ndp_pr_spmv,matched_pr_spmv_variants}.py`: compile
  the matched double-buffered sources and bind their hashes/contracts.
- `scripts/amu_cira_calibration.py` and
  `scripts/run_amu_paper_calibration.py`: freeze the approved hardware sources
  and near-data model provenance without fitting to formal speedup.
- `scripts/gapbs_pr_experiment_profiles.py`,
  `scripts/m2ndp_pagerank_trace.py`, and
  `scripts/run_m2ndp_g20_pr_spmv.py`: retire the two-thread publication path
  and represent the four logical row partitions in FuncSim/NDPSim.
- `scripts/pr_offload_contract.py`: exact schemas, phase accounting, native
  timing conversion, bit gate, and mechanism validation.
- `scripts/run_pr_asymmetric_offload.py`: fresh-root orchestration, g12 gate,
  deterministic replay, g14/g20 execution, CIRA ablations, resume identity,
  and terminal artifacts.
- `scripts/generate_pr_offload_artifacts.py`: raw CSV/JSON, LaTeX, speedup,
  absolute latency, CIRA policy, additive phase, and mechanism figures.
- `tests/pyunit/{amu,cross_system,m2ndp}` and `tests/gem5/pr_offload`: TDD
  contracts and timing-model smoke proof.

### Task 1: Define the descriptor ABI and matched double-buffered reference

**Files:**
- Create: `util/pr_offload/pr_row_offload.h`
- Create: `scripts/pr_offload_contract.py`
- Modify: `util/m2ndp/gapbs_pr_spmv_fixed.cc`
- Modify: `scripts/build_gapbs_m2ndp_pr_spmv.py`
- Create: `tests/pyunit/cross_system/test_pr_row_offload_contract.py`
- Modify: `tests/pyunit/m2ndp/test_m2ndp_build.py`

- [ ] **Step 1: Write the failing ABI and reference tests**

Add tests that compile the header as both C and C++, exercise uneven static
partitions, and inspect the reference source/manifest:

```python
class PrRowOffloadAbiTest(unittest.TestCase):
    def test_descriptor_layout_and_four_way_partition(self):
        self.assertEqual(contract.PR_ROW_DESC_BYTES, 104)
        self.assertEqual(
            [contract.static_partition(11, 4, i) for i in range(4)],
            [(0, 3), (3, 6), (6, 9), (9, 11)],
        )

    def test_reference_is_explicitly_double_buffered(self):
        source = (REPO / "util/m2ndp/gapbs_pr_spmv_fixed.cc").read_text()
        self.assertIn("pvector<ScoreT> next_scores", source)
        self.assertIn("scores.swap(next_scores)", source)
        self.assertIn("incoming_total = incoming_total + outgoing_contrib[v]", source)
        self.assertNotIn("incoming_total +=", source)
        main = source[source.index("int main") :]
        self.assertLess(main.index("InitializePageRank("),
                        main.index("m5_work_begin(trial, 0)"))
```

Add a manifest assertion to `test_m2ndp_build.py` requiring
`double_buffered=True`, `threads=4`, `page_rank_iterations=20`, and both
`-ffp-contract=off` and `-fno-fast-math`.

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```bash
python3 -m unittest \
  tests.pyunit.cross_system.test_pr_row_offload_contract \
  tests.pyunit.m2ndp.test_m2ndp_build -v
```

Expected: FAIL because `util/pr_offload/pr_row_offload.h`,
`PR_ROW_DESC_BYTES`, and the explicit next-score buffer do not exist.

- [ ] **Step 3: Add the exact ABI and reference algorithm**

Define the descriptor in `util/pr_offload/pr_row_offload.h`:

```c
#ifndef PR_ROW_OFFLOAD_H
#define PR_ROW_OFFLOAD_H

#include <stdint.h>

enum pr_row_phase {
    PR_ROW_CONTRIB = 1,
    PR_ROW_PULL = 2,
};

enum pr_row_flags {
    PR_ROW_FLAG_SAMPLE = 1u << 0,
};

struct pr_row_offload_desc {
    uint64_t in_offsets_addr;
    uint64_t in_neighbors_addr;
    uint64_t out_degree_addr;
    uint64_t scores_in_addr;
    uint64_t contributions_addr;
    uint64_t scores_out_addr;
    uint64_t row_begin;
    uint64_t row_count;
    uint64_t node_count;
    uint64_t iteration;
    uint32_t phase;
    uint32_t row_window;
    uint32_t lead_blocks;
    uint32_t flags;
    uint32_t damping_bits;
    uint32_t base_score_bits;
};

static inline void
pr_static_partition(uint64_t rows, uint32_t workers, uint32_t worker,
                    uint64_t *begin, uint64_t *end)
{
    const uint64_t quotient = rows / workers;
    const uint64_t remainder = rows % workers;
    *begin = worker * quotient + (worker < remainder ? worker : remainder);
    *end = *begin + quotient + (worker < remainder ? 1 : 0);
}

#ifdef __cplusplus
static_assert(sizeof(pr_row_offload_desc) == 104,
              "pr_row_offload_desc ABI changed");
#else
_Static_assert(sizeof(struct pr_row_offload_desc) == 104,
               "pr_row_offload_desc ABI changed");
#endif

#endif
```

Create the matching Python constants/helper used by builders and tests:

```python
PR_ROW_DESC_BYTES = 104
FORMAL_THREADS = 4
FORMAL_ITERATIONS = 20

def static_partition(rows, workers, worker):
    if rows < 0 or workers <= 0 or worker < 0 or worker >= workers:
        raise ValueError("invalid PR static partition")
    quotient, remainder = divmod(rows, workers)
    begin = worker * quotient + min(worker, remainder)
    end = begin + quotient + (1 if worker < remainder else 0)
    return begin, end
```

Split initialization from measured iteration execution. Allocate `scores`,
`next_scores`, and `outgoing_contrib`, then call `InitializePageRank` before
`m5_work_begin`; it writes the initial score to `scores` and zeros the other
two arrays. `PageRankPullFixed20` contains only the 20 measured iterations:
calculate contributions from `scores`, write pull results to `next_scores`,
then call `scores.swap(next_scores)` after the pull barrier. Keep the two float
operations separate:

```cpp
const ScoreT product = kDamp * incoming_total;
next_scores[u] = base_score + product;
```

Write only `scores` after iteration 19. Record the fixed ABI size,
`double_buffered: true`, and `threads: 4` in the build manifest.

- [ ] **Step 4: Run the tests and compile the reference**

Run:

```bash
python3 -m unittest \
  tests.pyunit.cross_system.test_pr_row_offload_contract \
  tests.pyunit.m2ndp.test_m2ndp_build -v
python3 -m compileall -q scripts/build_gapbs_m2ndp_pr_spmv.py
```

Expected: PASS; the header reports 104 bytes and the manifest is explicitly
four-thread, fixed-20, double-buffered.

- [ ] **Step 5: Commit the ABI and reference**

```bash
git add util/pr_offload/pr_row_offload.h scripts/pr_offload_contract.py \
  util/m2ndp/gapbs_pr_spmv_fixed.cc \
  scripts/build_gapbs_m2ndp_pr_spmv.py \
  tests/pyunit/cross_system/test_pr_row_offload_contract.py \
  tests/pyunit/m2ndp/test_m2ndp_build.py
git commit -m "feat: define bit-exact PR row descriptor"
```

### Task 2: Add the AMU PageRank descriptor operation and executor

**Files:**
- Create: `src/mem/pr_row_math.hh`
- Modify: `include/gem5/asm/generic/m5ops.h`
- Modify: `include/gem5/m5ops.h`
- Modify: `src/sim/pseudo_inst.hh`
- Modify: `src/sim/pseudo_inst.cc`
- Modify: `util/amu/amu.h`
- Modify: `src/mem/ASMC.py`
- Modify: `src/mem/asmc.hh`
- Modify: `src/mem/asmc.cc`
- Modify: `tests/pyunit/amu/test_asmc_paper_model.py`

- [ ] **Step 1: Write failing AMU dispatch, queue, and stats tests**

Require opcode `0x65`, a descriptor-pointer wrapper, an ASMC entry point, and
balanced descriptor statistics:

```python
def test_amu_pr_descriptor_has_real_model_dispatch(self):
    ops = (REPO / "include/gem5/asm/generic/m5ops.h").read_text()
    pseudo = (REPO / "src/sim/pseudo_inst.cc").read_text()
    model = (REPO / "src/mem/asmc.hh").read_text()
    self.assertIn("M5OP_AMU_PR_ROWS       0x65", ops)
    self.assertIn("asmc->issuePrRows(tc, desc.addr)", pseudo)
    self.assertIn("uint64_t issuePrRows(ThreadContext *tc, Addr desc_addr);", model)

def test_amu_pr_stats_fail_closed_on_unfinished_work(self):
    source = (REPO / "src/mem/asmc.cc").read_text()
    for name in ("issuedPrDescriptors", "completedPrDescriptors",
                 "prRows", "prReadPackets", "prWritePackets",
                 "prComputeTicks", "prQueueStallTicks"):
        self.assertIn(name, source)
```

- [ ] **Step 2: Run the focused test and verify RED**

Run:

```bash
python3 -m unittest \
  tests.pyunit.amu.test_asmc_paper_model.AsmcPaperModelTest -v
```

Expected: FAIL on the missing opcode, `issuePrRows`, and PR statistics.

- [ ] **Step 3: Add the ABI dispatch and exact float helpers**

Add `M5OP_AMU_PR_ROWS` at `0x65` to `M5OP_FOREACH`, declare
`m5_amu_pr_rows(const void *desc)`, dispatch it through `PseudoInst::amuPrRows`,
and expose this wrapper:

```c
static inline uint64_t
amu_pr_rows(const struct pr_row_offload_desc *desc)
{
    return m5_amu_pr_rows(desc);
}
```

In `src/mem/pr_row_math.hh`, use explicit float32 stores to prevent excess
precision or fusion:

```cpp
static inline float prF32Div(float left, float right)
{
    volatile float value = left / right;
    return value;
}

static inline float prF32Add(float left, float right)
{
    volatile float value = left + right;
    return value;
}

static inline float prF32Mul(float left, float right)
{
    volatile float value = left * right;
    return value;
}
```

- [ ] **Step 4: Implement the AMU-native timing state machine**

Add a bounded `PrDescriptorState` queue to ASMC. `issuePrRows` must
functionally copy and validate only the 104-byte descriptor, translate every
payload address, reserve queue and packet credits before acceptance, and return
zero without partial admission on failure. The state machine is:

```text
CONTRIB: read score[row] + out_degree[row]
         -> prF32Div(score, float(out_degree))
         -> timing write contributions[row]
PULL:    timing read offsets[row:row+2]
         -> timing read neighbors in increasing CSR index
         -> timing read contribution[neighbor] into indexed slots
         -> reduce slots with prF32Add in increasing CSR index
         -> prF32Mul(damping, sum), then prF32Add(base, product)
         -> timing write scores_out[row]
COMPLETE: publish exactly one completion token after every write response
```

Responses may arrive out of order, but reduction must wait for the complete row
and consume its indexed slots in CSR order. Schedule modeled compute cycles,
never call the host GAPBS loop, and keep descriptor, memory, compute, and drain
counters separate. Add `pr_descriptor_entries`, `pr_read_entries`,
`pr_fp_add_cycles`, `pr_fp_mul_cycles`, and `pr_fp_div_cycles` SimObject
parameters; their values are bound by the calibration manifest in Task 6.

- [ ] **Step 5: Build gem5 and run the AMU tests**

Run:

```bash
scons build/X86/gem5.opt -j4
python3 -m unittest discover -s tests/pyunit/amu \
  -p 'test_asmc_paper_model.py' -v
```

Expected: the build succeeds and the focused AMU tests pass. Any accepted
descriptor with a missing completion or a partial credit reservation is a
failure.

- [ ] **Step 6: Commit the AMU executor**

```bash
git add include/gem5/asm/generic/m5ops.h include/gem5/m5ops.h \
  src/sim/pseudo_inst.hh src/sim/pseudo_inst.cc \
  util/amu/amu.h src/mem/pr_row_math.hh \
  src/mem/ASMC.py src/mem/asmc.hh src/mem/asmc.cc \
  tests/pyunit/amu/test_asmc_paper_model.py
git commit -m "feat: execute ordered PR row blocks in AMU"
```

### Task 3: Add the coherent CIRA PageRank operation and executor

**Files:**
- Modify: `include/gem5/asm/generic/m5ops.h`
- Modify: `include/gem5/m5ops.h`
- Modify: `src/sim/pseudo_inst.hh`
- Modify: `src/sim/pseudo_inst.cc`
- Modify: `util/cira/cira.h`
- Modify: `src/mem/CIRA.py`
- Modify: `src/mem/cira.hh`
- Modify: `src/mem/cira.cc`
- Modify: `tests/pyunit/amu/test_cira_usefulness_contract.py`
- Modify: `tests/pyunit/amu/test_cira_hoist_model.py`

- [ ] **Step 1: Write failing CIRA dispatch, coherence, and drain tests**

Add assertions for opcode `0x66`, `issuePrRows`, four per-core queues, direct
CSR traffic, coherent rank/output traffic, and reconfiguration stats:

```python
def test_cira_pr_descriptor_uses_device_csr_and_coherent_value_ports(self):
    source = (REPO / "src/mem/cira.cc").read_text()
    self.assertIn("issuePrRows(ThreadContext *tc, Addr desc_addr)", source)
    self.assertIn("PrPacketRole::CsrRead", source)
    self.assertIn("PrPacketRole::CoherentRead", source)
    self.assertIn("PrPacketRole::CoherentWrite", source)
    self.assertIn("completedPrDescriptorsPerCore", source)

def test_cira_jit_reconfiguration_is_a_real_completion(self):
    header = (REPO / "util/cira/cira.h").read_text()
    self.assertIn("CIRA_CFG_PR_RECONFIGURE", header)
    self.assertIn("CIRA_CFG_PR_ROW_WINDOW", header)
    self.assertIn("CIRA_CFG_PR_LEAD_BLOCKS", header)
```

- [ ] **Step 2: Run the CIRA tests and verify RED**

Run:

```bash
python3 -m unittest \
  tests.pyunit.amu.test_cira_usefulness_contract \
  tests.pyunit.amu.test_cira_hoist_model -v
```

Expected: FAIL because CIRA still performs prefetch-only work.

- [ ] **Step 3: Add the CIRA descriptor and configuration ABI**

Add `M5OP_CIRA_PR_ROWS` at `0x66`, its pseudo-instruction dispatch, and:

```c
enum cira_cfg_reg {
    CIRA_CFG_ENABLE = 0,
    CIRA_CFG_MAX_OUTSTANDING = 1,
    CIRA_CFG_RESET = 2,
    CIRA_CFG_OUTSTANDING = 2,
    CIRA_CFG_FINISHED = 3,
    CIRA_CFG_PR_ROW_WINDOW = 4,
    CIRA_CFG_PR_LEAD_BLOCKS = 5,
    CIRA_CFG_PR_RECONFIGURE = 6,
};

static inline uint64_t
cira_pr_rows(const struct pr_row_offload_desc *desc)
{
    return m5_cira_pr_rows(desc);
}
```

Writing `CIRA_CFG_PR_RECONFIGURE` must enqueue a real completion whose scheduled
latency is `pr_reconfiguration_latency`; it must not advance `curTick()` or
return an already-completed token.

- [ ] **Step 4: Implement coherent CIRA row execution**

Use `csr_mem_side_port` for offsets and neighbor-index reads. Use the issuing
core's existing `mem_side_ports[targetCore]` for contribution/rank reads and
next-rank writes so cache ownership and invalidation are modeled. Preserve the
same indexed-slot reduction rule as AMU but maintain an independent CIRA queue,
packet roles, retry state, and scheduler.

Reject rather than partially enqueue when descriptor, CSR-index, coherent
packet, or output-write capacity is unavailable. Completion occurs only after
the coherent write response. Add aggregate and four-entry per-core stats for
issued/completed descriptors, rows, CSR reads, coherent reads/writes, compute,
queue stalls, reconfiguration, useful hoists, ineffective hoists, outstanding
work, and high-water marks.

Keep admission and completion shaped as follows so partial descriptors cannot
escape:

```cpp
if (!validatePrDescriptor(desc) || !reservePrCredits(desc, targetCore)) {
    ++stats.rejectedPrDescriptors;
    return 0;
}
const uint64_t id = nextId++;
prDescriptors[targetCore].emplace_back(id, tc, desc);
++stats.issuedPrDescriptors;
++stats.issuedPrDescriptorsPerCore[targetCore];
schedulePr(targetCore, clockEdge(Cycles(1)));
return id;
```

- [ ] **Step 5: Build and run the focused CIRA tests**

Run:

```bash
scons build/X86/gem5.opt -j4
python3 -m unittest \
  tests.pyunit.amu.test_cira_usefulness_contract \
  tests.pyunit.amu.test_cira_hoist_model -v
```

Expected: PASS; `issuedPrDescriptors == completedPrDescriptors`, all active
cores have work, and reset/drain leaves no descriptor or packet state.

- [ ] **Step 6: Commit the CIRA executor**

```bash
git add include/gem5/asm/generic/m5ops.h include/gem5/m5ops.h \
  src/sim/pseudo_inst.hh src/sim/pseudo_inst.cc util/cira/cira.h \
  src/mem/CIRA.py src/mem/cira.hh src/mem/cira.cc \
  tests/pyunit/amu/test_cira_usefulness_contract.py \
  tests/pyunit/amu/test_cira_hoist_model.py
git commit -m "feat: execute coherent PR row blocks in CIRA"
```

### Task 4: Build the four-thread offload workload and runtime CIRA policies

**Files:**
- Create: `util/pr_offload/gapbs_pr_spmv_offload.cc`
- Modify: `scripts/build_gapbs_matched_pr_spmv_variants.py`
- Modify: `scripts/build_gapbs_amu_cxlmemuring.py`
- Modify: `scripts/build_gapbs_cira_cxlmemuring.py`
- Modify: `tests/pyunit/m2ndp/test_matched_pr_spmv_variants.py`
- Create: `tests/pyunit/amu/test_cira_pr_runtime_policy.py`

- [ ] **Step 1: Write failing workload/build tests**

Replace the old prefetch/load-loop expectations with assertions that each
worker submits one contribution descriptor and one pull descriptor per
iteration, waits once per phase, and swaps the buffers. Require these mode
definitions:

```python
CANDIDATES = {
    "A": {"row_window": 64, "lead_blocks": 1},
    "B": {"row_window": 2048, "lead_blocks": 32},
    "C": {"row_window": 1024, "lead_blocks": 16},
}
```

Test that Static fixes A, PGO consumes the frozen selected row, and Few-shot
emits A/B/C sample descriptors before one irreversible selection. Assert that
the generated source contains `m5_rpns()` timestamps but prints phase markers
only after `m5_work_end`.

- [ ] **Step 2: Run the source tests and verify RED**

Run:

```bash
python3 -m unittest \
  tests.pyunit.m2ndp.test_matched_pr_spmv_variants \
  tests.pyunit.amu.test_cira_pr_runtime_policy -v
```

Expected: FAIL because variants still transform the scalar pull loop.

- [ ] **Step 3: Implement the common four-thread driver**

Create a workload with two explicit device phases per iteration:

```cpp
#pragma omp parallel num_threads(4)
{
    const uint32_t worker = omp_get_thread_num();
    uint64_t begin = 0, end = 0;
    pr_static_partition(g.num_nodes(), 4, worker, &begin, &end);
    for (uint64_t iteration = 0; iteration < 20; ++iteration) {
        submit_and_wait(make_contrib_desc(begin, end, iteration));
#pragma omp barrier
        submit_and_wait(make_pull_desc(begin, end, iteration));
#pragma omp barrier
#pragma omp single
        scores.swap(next_scores);
#pragma omp barrier
    }
}
```

The actual implementation must use one persistent parallel region, validate
`omp_get_num_threads() == 4`, preserve contiguous ownership, and wait by exact
completion token rather than by completion count. AMU invokes `amu_pr_rows`;
CIRA invokes `cira_pr_rows`.

- [ ] **Step 4: Implement charged CIRA Few-shot/JIT**

For Few-shot, after contributions for iteration 0 are ready, execute A, B, and
C on the same bounded pilot rows into separate scratch outputs. Record the
start/end `m5_rpns()` timestamps, keep every sample charged, discard all sample
outputs, select the minimum positive measured duration with A/B/C as the stable
tie order, issue and wait for `CIRA_CFG_PR_RECONFIGURE`, then execute the full
pull phase. Static and PGO skip sampling and JIT.

Initialize both rank buffers and the contribution buffer before
`m5_work_begin`; graph loading and allocation also remain before that marker.
Use a `PhaseLedger` owned by the OpenMP single region. A transition reads
`m5_rpns()`, charges `now - last` to the previous stage, and starts the next
stage. All workers meet at a barrier before a transition, so every ROI tick is
owned by exactly one top-level stage even though executor-internal memory and
compute counters overlap. Initialize the ledger immediately after
`m5_work_begin`, finalize it immediately before `m5_work_end`, store its values
in memory, and emit exactly one line afterward with:

```cpp
std::fprintf(stderr,
    "PR_E2E_PHASES formation=%llu sampling=%llu selection=%llu "
    "jit=%llu execution=%llu drain=%llu total=%llu\n",
    formation_ticks, sampling_ticks, selection_ticks, jit_ticks,
    execution_ticks, drain_ticks, total_ticks);
```

Require all six components to be nonnegative and sum exactly to `total`;
Few-shot requires positive sampling, selection, and JIT. Candidate-only runs
use the same full workload with the named candidate and no sampling.

- [ ] **Step 5: Replace brittle loop transformation with template compilation**

Make `build_gapbs_matched_pr_spmv_variants.py` copy and compile
`gapbs_pr_spmv_offload.cc` with exactly one of `PR_OFFLOAD_AMU=1` or
`PR_OFFLOAD_CIRA=1`. Preserve `-ffp-contract=off -fno-fast-math`, bind the
descriptor-header hash, mode, source row, candidate table, graph scale,
calibration hash, and double-buffer contract into `manifest.json`. Do not keep
the old `_AMU_PULL_LOOP` or `_CIRA_PULL_LOOP` production path.

The compile-mode branch is exact:

```python
mode_flag = {
    "amu": "-DPR_OFFLOAD_AMU=1",
    "cira": "-DPR_OFFLOAD_CIRA=1",
}[kind]
command = [cxx, *COMMON_FLAGS, mode_flag,
           "-I", str(REPO / "util/pr_offload"),
           str(REPO / "util/pr_offload/gapbs_pr_spmv_offload.cc"),
           str(m5_library), "-o", str(output)]
```

- [ ] **Step 6: Run tests and commit**

Run:

```bash
python3 -m unittest \
  tests.pyunit.m2ndp.test_matched_pr_spmv_variants \
  tests.pyunit.amu.test_cira_pr_runtime_policy -v
python3 -m compileall -q scripts/build_gapbs_matched_pr_spmv_variants.py
```

Expected: PASS with all three binaries using the same 20-iteration numerical
contract.

```bash
git add util/pr_offload/gapbs_pr_spmv_offload.cc \
  scripts/build_gapbs_matched_pr_spmv_variants.py \
  scripts/build_gapbs_amu_cxlmemuring.py \
  scripts/build_gapbs_cira_cxlmemuring.py \
  tests/pyunit/m2ndp/test_matched_pr_spmv_variants.py \
  tests/pyunit/amu/test_cira_pr_runtime_policy.py
git commit -m "feat: drive charged PR row offload policies"
```

### Task 5: Bind hardware calibration and all-CXL model parameters

**Files:**
- Modify: `scripts/amu_cira_calibration.py`
- Modify: `scripts/run_amu_paper_calibration.py`
- Modify: `configs/example/gem5_library/x86-gapbs-amu-se.py`
- Modify: `scripts/compare_gapbs_cxl_amu_cira.py`
- Modify: `scripts/run_gapbs_matched_pr_spmv_variants.py`
- Modify: `tests/pyunit/amu/test_amu_cira_calibration.py`
- Modify: `tests/pyunit/amu/test_compare_gapbs_cxl_amu_cira.py`
- Modify: `tests/pyunit/m2ndp/test_run_gapbs_matched_pr_spmv_variants.py`

- [ ] **Step 1: Write failing schema and configuration tests**

Require calibration schema 2 to contain the approved source hashes and a
`near_data_pr` section. Require config forwarding for every new ASMC/CIRA
resource and compute parameter. Add a negative test showing that changing
either source hash or a manifest parameter blocks launch.

The tests must distinguish source roles:

```python
self.assertEqual(manifest["near_data_pr"]["amu"]["fit_role"],
                 "architecture_and_cross_workload_validation")
self.assertEqual(manifest["near_data_pr"]["cira"]["fit_role"],
                 "pr_spmv_policy_ranking")
self.assertFalse(manifest["near_data_pr"]["formal_speedup_is_fit_target"])
```

- [ ] **Step 2: Run focused calibration/config tests and verify RED**

Run:

```bash
AMU_PDF=/home/victoryang00/gem5-CXL/3663479.pdf \
CIRA_CSV=/root/ia780i_type2_delay_buffer_new/benchmark_gapbs_workloads_ci_long.csv \
python3 -m unittest \
  tests.pyunit.amu.test_amu_cira_calibration \
  tests.pyunit.amu.test_compare_gapbs_cxl_amu_cira \
  tests.pyunit.m2ndp.test_run_gapbs_matched_pr_spmv_variants -v
```

Expected: FAIL because schema 1 has no near-data executor parameters.

- [ ] **Step 3: Extend and freeze the calibration manifest**

Keep the existing immutable hashes:

```python
AMU_PDF_SHA256 = "cba178ece7593b3ede868417a031ded3efddd85d5f7c50672b0a93735187790f"
CIRA_CSV_SHA256 = "4e0297da423cee0a742bc2e10656d022bb27776807f2d2ce4cca43e65c634184"
```

Derive AMU queue/SPM/control limits only from the paper fields already parsed,
and preserve the paper's lack of PageRank data as a limitation. Preserve the
ten raw CIRA `pr_spmv` A/B/C measurements, confidence intervals, and selected
row; use them for candidate ranking and policy validation, not as a direct
replacement for gem5 time. Record every compute-cycle assumption explicitly
with units and source classification. Set
`formal_speedup_is_fit_target=false` and reject any manifest that stores a
g12/g14/g20 target speedup as a fitted parameter.

- [ ] **Step 4: Wire the frozen parameters and collect phase/counter evidence**

Add CLI parameters for the new ASMC/CIRA queue, compute, and reconfiguration
fields. Populate SimObjects only from schema-2 manifest values for the formal
profile. Extend summary CSV parsing with `PR_E2E_PHASES` and the new gem5 stats.
Require all-CXL `delay=1000000`, four cores/workers, descriptor balance, zero
rejection/pending state, and a full final drain.

Preserve analogous AMU formation/issue, execution, synchronization, and drain
fields in raw evidence. Task 7 maps M2NDP contribution, pull, synchronization,
and final completion cycles into its own native stage record; neither AMU nor
M2NDP stages are silently inferred from the CIRA model.

Fail the runner before launch unless the frozen shape matches:

```python
if manifest.get("schema") != 2:
    raise ValueError("formal PR offload requires calibration schema 2")
near_data = manifest["near_data_pr"]
if near_data.get("formal_speedup_is_fit_target") is not False:
    raise ValueError("formal speedup cannot be a calibration target")
if manifest["sources"]["amu_pdf"]["sha256"] != AMU_PDF_SHA256:
    raise ValueError("AMU source hash differs")
if manifest["sources"]["cira_csv"]["sha256"] != CIRA_CSV_SHA256:
    raise ValueError("CIRA source hash differs")
```

- [ ] **Step 5: Run tests and commit**

Run the Step 2 command again. Expected: PASS.

```bash
git add scripts/amu_cira_calibration.py \
  scripts/run_amu_paper_calibration.py \
  configs/example/gem5_library/x86-gapbs-amu-se.py \
  scripts/compare_gapbs_cxl_amu_cira.py \
  scripts/run_gapbs_matched_pr_spmv_variants.py \
  tests/pyunit/amu/test_amu_cira_calibration.py \
  tests/pyunit/amu/test_compare_gapbs_cxl_amu_cira.py \
  tests/pyunit/m2ndp/test_run_gapbs_matched_pr_spmv_variants.py
git commit -m "feat: bind hardware-calibrated PR offload model"
```

### Task 6: Prove the two device executors with a gem5 timing smoke test

**Files:**
- Create: `tests/gem5/pr_offload/pr_row_offload_smoke.cc`
- Create: `tests/gem5/pr_offload/run_pr_row_offload.py`
- Create: `tests/pyunit/cross_system/test_pr_row_offload_smoke.py`

- [ ] **Step 1: Write the failing smoke-test contract**

Create a six-row CSR fixture containing empty, one-edge, repeated-neighbor, and
cross-cache-line rows. The host reference and both device modes must run three
contribution/pull iterations on four timing cores, emit the six uint32 float
words after every iteration, and match at every boundary. Test one injected
changed bit, one queue-capacity rejection, and one unfinished-write condition;
all three must fail closed.

- [ ] **Step 2: Run the smoke test and verify RED**

Run:

```bash
python3 -m unittest \
  tests.pyunit.cross_system.test_pr_row_offload_smoke -v
```

Expected: FAIL because the test binary/config do not exist.

- [ ] **Step 3: Implement the deterministic timing smoke**

Compile the test with `-ffp-contract=off -fno-fast-math`. Run Vanilla, AMU,
and CIRA with `--cpu timing --cores 4 --cxl-memory --cxl-link-delay 1us`.
Require the Python harness to match this regular expression for iterations
zero, one, and two in every mode:

```text
^PR_ROW_ITER_BITS mode=(vanilla|amu|cira) iteration=([012]) words=([0-9a-f]{8})(,[0-9a-f]{8}){5}$
^PR_ROW_VERIFY mode=(vanilla|amu|cira) status=PASS$
```

The Python harness compares every word, checks `delay=1000000`, validates
issued/completed and all pending queues, and checks that all four per-core CIRA
descriptor counters are nonzero.

- [ ] **Step 4: Run and commit the smoke proof**

Run:

```bash
python3 -m unittest \
  tests.pyunit.cross_system.test_pr_row_offload_smoke -v
```

Expected: PASS for Vanilla/AMU/CIRA and all injected failure cases.

```bash
git add tests/gem5/pr_offload/pr_row_offload_smoke.cc \
  tests/gem5/pr_offload/run_pr_row_offload.py \
  tests/pyunit/cross_system/test_pr_row_offload_smoke.py
git commit -m "test: prove bit-exact PR row executors"
```

### Task 7: Convert M2NDP to the four-partition formal profile

**Files:**
- Modify: `scripts/gapbs_pr_experiment_profiles.py`
- Modify: `scripts/m2ndp_pagerank_trace.py`
- Modify: `scripts/run_m2ndp_g20_pr_spmv.py`
- Modify: `scripts/m2ndp_results.py`
- Modify: `tests/pyunit/m2ndp/test_gapbs_pr_experiment_profiles.py`
- Modify: `tests/pyunit/m2ndp/test_m2ndp_trace.py`
- Modify: `tests/pyunit/m2ndp/test_run_m2ndp_g20_pr_spmv.py`
- Modify: `tests/pyunit/m2ndp/test_m2ndp_results.py`

- [ ] **Step 1: Write failing four-partition profile and trace tests**

Define `pr-offload-4thread-1us` with scales `(12, 14, 20)`, four logical
partitions, two trials, measured trial 1, and 20 iterations. Require every
K0/K2/K3 launch to cover exactly one of four disjoint contiguous partitions;
K1 metadata remains a single launch. K0 initialization and K1 metadata occur
before the measured ROI; the measured marker is the first partition of
iteration-0 K2, not K0. Assert that no formal contract accepts
`g20-2thread-1us`.

- [ ] **Step 2: Run the focused M2NDP tests and verify RED**

Run:

```bash
python3 -m unittest \
  tests.pyunit.m2ndp.test_gapbs_pr_experiment_profiles \
  tests.pyunit.m2ndp.test_m2ndp_trace \
  tests.pyunit.m2ndp.test_run_m2ndp_g20_pr_spmv \
  tests.pyunit.m2ndp.test_m2ndp_results -v
```

Expected: FAIL on the new profile and logical-partition metadata.

- [ ] **Step 3: Make launches partition-aware without changing FP order**

Add distinct `SCORES_A_ADDR` and `SCORES_B_ADDR` arrays. K0 initializes A.
For iteration zero K2 reads A and K3 writes B; each following iteration swaps
the two launch addresses, so iteration 19 writes the final result to A. Add
`row_begin` and `row_count` arguments to K0, K2, and K3. Compute
`row = row_begin + local_index`; retain K3's increasing `x5` CSR loop and
scalar `fadd` sequence. Generate four launches per phase with bounds from
`pr_static_partition`, validate the final output at A, and bind these fields
into `trace.meta.json`:

```json
{
  "logical_partitions": 4,
  "partition_bounds": [[0, 1024], [1024, 2048], [2048, 3072], [3072, 4096]],
  "double_buffered": true,
  "profile": "pr-offload-4thread-1us"
}
```

Use the scale-specific bounds rather than hard-coding the example. FuncSim
must compare every output word before NDPSim runs.

Name the measured trial's first contribution launch
`K2_CONTRIB_TRIAL1_PART0` and bind it as `measure_marker`. NDPSim timing begins
there and ends after the fourth K3 partition of iteration 19, so initialization
and metadata are excluded exactly as they are around gem5 `m5_work_begin`.

- [ ] **Step 4: Reject legacy timing and run tests**

Change the formal runner/results defaults and binding checks to the new
profile. Legacy artifacts may remain readable only through explicitly named
diagnostic functions; they cannot satisfy the formal validator.

```python
FORMAL_PROFILE = "pr-offload-4thread-1us"
if result.get("profile") != FORMAL_PROFILE:
    raise artifacts.EvidenceError("M2NDP result is not the formal PR profile")
if result.get("logical_partitions") != 4:
    raise artifacts.EvidenceError("M2NDP trace is not four-way partitioned")
```

Run the Step 2 command again. Expected: PASS.

- [ ] **Step 5: Commit the M2NDP four-way path**

```bash
git add scripts/gapbs_pr_experiment_profiles.py \
  scripts/m2ndp_pagerank_trace.py scripts/run_m2ndp_g20_pr_spmv.py \
  scripts/m2ndp_results.py \
  tests/pyunit/m2ndp/test_gapbs_pr_experiment_profiles.py \
  tests/pyunit/m2ndp/test_m2ndp_trace.py \
  tests/pyunit/m2ndp/test_run_m2ndp_g20_pr_spmv.py \
  tests/pyunit/m2ndp/test_m2ndp_results.py
git commit -m "feat: lower four-way PR partitions to M2NDP"
```

### Task 8: Define immutable offload evidence and the campaign matrix

**Files:**
- Modify: `scripts/pr_offload_contract.py`
- Create: `scripts/run_pr_asymmetric_offload.py`
- Create: `tests/pyunit/cross_system/test_pr_offload_contract.py`
- Create: `tests/pyunit/cross_system/test_run_pr_asymmetric_offload.py`

- [ ] **Step 1: Write failing matrix, identity, and validation tests**

Require 12 matched primary points (Vanilla/AMU/CIRA Few-shot/M2NDP at three
scales) and 15 extra CIRA ablations (Static, PGO, A, B, C at three scales).
The formal accelerated gate contains exactly nine points. Oracle is derived
from A/B/C and is never scheduled.

Test exact identity fields for source, gem5, libm5, graph set, all workload
binaries, M2NDP commit/patches, configuration, calibration, and policy. Test
that a changed byte prevents resume. Test row-by-row raw-vector equality,
phase-sum equality, native timing recomputation, all-CXL topology, four workers,
20 iterations, balanced completions, zero pending work, and M2NDP FuncSim
precedence.

The accepted source input manifest contains the historical g4 graph. Test that
the new runner deterministically selects only g12/g14/g20, writes a canonical
`selected-inputs.json` into the campaign root, and binds both the source
manifest hash and selected manifest hash. A missing, reordered, or changed
formal graph fails closed; the unused g4 row can never become a timed point.

- [ ] **Step 2: Run the new tests and verify RED**

Run:

```bash
python3 -m unittest \
  tests.pyunit.cross_system.test_pr_offload_contract \
  tests.pyunit.cross_system.test_run_pr_asymmetric_offload -v
```

Expected: FAIL because the evidence modules do not exist.

- [ ] **Step 3: Implement exact schemas and point validators**

Use `Decimal` for all latency and speedup calculations. Define:

```python
SCALES = (12, 14, 20)
PRIMARY_SYSTEMS = ("vanilla", "amu", "cira-few-shot", "m2ndp")
CIRA_ABLATIONS = ("cira-static", "cira-pgo", "cira-A", "cira-B", "cira-C")
MIN_SPEEDUP = Decimal("1.4")
MAX_SPEEDUP = Decimal("1.6")
```

`validate_point` must accept no stored speedup until it recomputes
`vanilla_seconds / system_seconds`. CIRA phase fields are exact nonnegative
integer ticks and sum to `sim_ticks`. Mechanism counters are not summed into
wall time. `complete.json` is valid only with 12 primary and 15 ablation
points; its performance gate has nine accepted accelerated entries.

Normalize the graph input once:

```python
selected = [row for row in source_inputs["graphs"]
            if row.get("scale") in SCALES]
if [row.get("scale") for row in selected] != list(SCALES):
    raise OffloadError("formal graphs must be ordered g12,g14,g20")
atomic_write_json(root / "selected-inputs.json", {
    "schema": 1,
    "profile": "pr-offload-4thread-1us",
    "source_inputs_sha256": sha256_file(options.inputs),
    "graphs": selected,
})
```

- [ ] **Step 4: Implement fresh-root and resume orchestration**

The runner creates one campaign root with `qualification/primary`,
`qualification/replay`, `formal/g14`, `formal/g20`, and `ablation/g12|g14|g20`.
It atomically records the command and identity before each subprocess. Resume
revalidates every passed artifact byte-for-byte. Never import the old
`diagnostic-performance-hold.json`, a checkpoint from another root, or a
pre-offload variant.

```python
def point_root(root, entry):
    if entry.stage == "qualification":
        return root / "qualification" / entry.replica / entry.system
    if entry.stage == "ablation":
        return root / "ablation" / f"g{entry.scale}" / entry.system
    return root / "formal" / f"g{entry.scale}" / entry.system

def require_resume_identity(saved, live):
    if saved != live:
        raise OffloadError("campaign resume identity differs")
```

Formal subprocesses have no wall-clock timeout. The runner may accept
`--stop-after qualification` for the explicit gate, but it must not expose a
formal timeout that silently turns a long g20 run into partial evidence.

Command construction must call the existing baseline/variant/M2NDP runners
with `--profile pr-offload-4thread-1us`, exact frozen graph manifest,
`--cxl-link-delay 1us`, four cores/workers, the schema-2 calibration manifest,
and the requested CIRA mode/source row.

- [ ] **Step 5: Run tests and commit**

Run the Step 2 command again. Expected: PASS, including injected identity,
bit, phase, queue, topology, and FuncSim failures.

```bash
git add scripts/pr_offload_contract.py \
  scripts/run_pr_asymmetric_offload.py \
  tests/pyunit/cross_system/test_pr_offload_contract.py \
  tests/pyunit/cross_system/test_run_pr_asymmetric_offload.py
git commit -m "feat: orchestrate immutable PR offload campaign"
```

### Task 9: Enforce g12 qualification, deterministic replay, and performance hold

**Files:**
- Modify: `scripts/run_pr_asymmetric_offload.py`
- Modify: `scripts/pr_offload_contract.py`
- Modify: `tests/pyunit/cross_system/test_run_pr_asymmetric_offload.py`

- [ ] **Step 1: Write failing qualification/replay tests**

Add fixtures for inclusive 1.4x/1.6x, one 1.399x offender, bit mismatch,
Few-shot zero JIT, and replay tick/policy drift. Require the runner to stop
before any g14/g20 or ablation command on every failure.

```python
self.assertEqual(gate["checked_points"], 3)
self.assertEqual(gate["status"], "passed")
self.assertFalse(any(cmd.scale > 12 for cmd in commands_before_gate))
```

- [ ] **Step 2: Run the qualification tests and verify RED**

Run:

```bash
python3 -m unittest \
  tests.pyunit.cross_system.test_run_pr_asymmetric_offload -v
```

Expected: FAIL on replay and stop-before-larger-scale behavior.

- [ ] **Step 3: Implement the g12 state transition**

Run g12 Vanilla, AMU, CIRA Few-shot/JIT, and M2NDP. Validate bit/mechanism
gates first, then recompute three speedups. On an offender, write
`diagnostic-performance-hold.json` with `official_qualification=false`, retain
raw evidence, remove `qualification.json` and `complete.json`, and exit without
launching larger scales.

When primary g12 passes, run all four points again in `qualification/replay`.
Require identical rank hashes, exact native timing counts, and the same CIRA
selected candidate. Only then write `qualification.json` and adopt the primary
g12 records into the formal matrix within the same campaign identity.

```python
for system in PRIMARY_SYSTEMS:
    first = primary[f"g12:{system}"]
    again = replay[f"g12:{system}"]
    if first["outputs"]["rank"] != again["outputs"]["rank"]:
        raise OffloadError(f"g12 {system} replay rank differs")
    if native_count(first) != native_count(again):
        raise OffloadError(f"g12 {system} replay timing differs")
if primary["g12:cira-few-shot"]["selected_candidate"] != \
        replay["g12:cira-few-shot"]["selected_candidate"]:
    raise OffloadError("g12 CIRA replay policy differs")
```

- [ ] **Step 4: Implement the final nine-point gate**

After g14/g20 and ablations pass correctness, recompute AMU, CIRA Few-shot,
and M2NDP speedups at all three scales. Any offender produces
`performance-hold.json`, blocks `complete.json`, and leaves paper output
unchanged. Static/PGO/A/B/C have no interval gate. Derive Oracle and regret as:

```python
oracle_ticks = min(candidate[name]["sim_ticks"] for name in ("A", "B", "C"))
regret = Decimal(few_shot["sim_ticks"]) / Decimal(oracle_ticks) - Decimal(1)
```

- [ ] **Step 5: Run tests and commit**

Run the Step 2 command again. Expected: PASS for all stop, replay, and terminal
artifact cases.

```bash
git add scripts/run_pr_asymmetric_offload.py \
  scripts/pr_offload_contract.py \
  tests/pyunit/cross_system/test_run_pr_asymmetric_offload.py
git commit -m "feat: gate PR offload qualification and replay"
```

### Task 10: Generate raw data, figures, and LaTeX fail closed

**Files:**
- Create: `scripts/generate_pr_offload_artifacts.py`
- Create: `tests/pyunit/cross_system/test_generate_pr_offload_artifacts.py`
- Modify: `docs/amu-gapbs-benchmark.md`

- [ ] **Step 1: Write failing publisher tests**

Create a valid 27-point fixture and mutations for missing point, changed rank
hash, speedup drift, phase-sum drift, nonzero pending work, missing FuncSim
gate, non-passing performance gate, and source hash drift. Require atomic
rollback on an injected promotion failure.

Require these exact outputs:

```python
EXPECTED = {
    "pr-offload-raw.json", "pr-offload-raw.csv",
    "pr-offload-evidence.json", "pr-offload-table.tex",
    "fig/pr-offload-speedup.pdf", "fig/pr-offload-speedup.svg",
    "fig/pr-offload-latency.pdf", "fig/pr-offload-latency.svg",
    "fig/cira-policy-scaling.pdf", "fig/cira-policy-scaling.svg",
    "fig/cira-phase-breakdown.pdf", "fig/cira-phase-breakdown.svg",
    "fig/cira-mechanism-breakdown.pdf", "fig/cira-mechanism-breakdown.svg",
}
```

- [ ] **Step 2: Run the publisher test and verify RED**

Run:

```bash
python3 -m unittest \
  tests.pyunit.cross_system.test_generate_pr_offload_artifacts -v
```

Expected: FAIL because the publisher does not exist.

- [ ] **Step 3: Implement evidence loading and deterministic rendering**

Render speedup and absolute E2E latency for AMU/CIRA Few-shot/M2NDP at
g12/g14/g20. Render Static/PGO/Few-shot and A/B/C policy scaling separately.
The phase figure is stacked only from the six additive stages. The mechanism
figure uses normalized executor counters and labels them non-additive. Include
Oracle regret in raw data and annotations, not as a formal system bar.

Use a staging directory and `os.replace` only after every file renders and
hashes successfully. Never read diagnostic-hold files as publishable input.

```python
with tempfile.TemporaryDirectory(dir=outdir.parent) as temporary:
    staging = Path(temporary)
    render_all(data, staging)
    manifest = hash_outputs(staging)
    atomic_write_json(staging / "pr-offload-evidence.json", manifest)
    promote_tree(staging, outdir)
```

- [ ] **Step 4: Document the exact workflow and run tests**

Add the calibration hashes, descriptor contract, ROI boundaries, g12 gate,
replay rule, raw-data locations, runner command, publisher command, and failure
semantics to `docs/amu-gapbs-benchmark.md`.

Run:

```bash
python3 -m unittest \
  tests.pyunit.cross_system.test_generate_pr_offload_artifacts -v
python3 -m compileall -q scripts/generate_pr_offload_artifacts.py
```

Expected: PASS and byte-identical outputs across two renders.

- [ ] **Step 5: Commit the publisher**

```bash
git add scripts/generate_pr_offload_artifacts.py \
  tests/pyunit/cross_system/test_generate_pr_offload_artifacts.py \
  docs/amu-gapbs-benchmark.md
git commit -m "feat: publish PR offload scaling evidence"
```

### Task 11: Run complete verification and create a fresh campaign

**Files:**
- Verify only; do not modify tracked source while tests run.
- Create runtime artifacts under: `/mnt/disk0/gem5-CXL-eval/pr-offload-formal`

- [ ] **Step 1: Run all static and Python regression gates**

Run:

```bash
git diff --check
python3 -m compileall -q scripts tests/pyunit
python3 -m unittest discover -s tests/pyunit/amu -p 'test_*.py' -v
python3 -m unittest discover -s tests/pyunit/m2ndp -p 'test_*.py' -v
python3 -m unittest discover -s tests/pyunit/cross_system -p 'test_*.py' -v
```

Expected: every test passes; only documented skips remain.

- [ ] **Step 2: Rebuild gem5 and m5ops from the same commit**

Run:

```bash
scons build/X86/gem5.opt -j4
scons -C util/m5 build/x86/out/m5 -j4
git status --short --branch
```

Expected: both builds succeed. `src/mem/cache/base.cc` remains the only
unrelated dirty path; generated build outputs are ignored.

- [ ] **Step 3: Create the schema-2 calibration and inspect it**

Use a new identity-rooted calibration directory. Run the complete collection
and proof sequence instead of inventing or copying a manifest:

```bash
mkdir -p /mnt/disk0/gem5-CXL-eval/pr-offload-calibration
python3 scripts/run_amu_paper_calibration.py collect \
  --gem5 build/X86/gem5.opt \
  --config configs/example/gem5_library/x86-gapbs-amu-se.py \
  --m5-library util/m5/build/x86/out/libm5.a \
  --pdf /home/victoryang00/gem5-CXL/3663479.pdf \
  --cira-csv /root/ia780i_type2_delay_buffer_new/benchmark_gapbs_workloads_ci_long.csv \
  --outdir /mnt/disk0/gem5-CXL-eval/pr-offload-calibration/collection \
  --measurements /mnt/disk0/gem5-CXL-eval/pr-offload-calibration/measurements.json \
  --collection-manifest /mnt/disk0/gem5-CXL-eval/pr-offload-calibration/collection-manifest.json \
  --iterations 2
python3 scripts/run_amu_paper_calibration.py gate \
  --gem5 build/X86/gem5.opt \
  --config configs/example/gem5_library/x86-gapbs-amu-se.py \
  --m5-library util/m5/build/x86/out/libm5.a \
  --outdir /mnt/disk0/gem5-CXL-eval/pr-offload-calibration/gate \
  --proof /mnt/disk0/gem5-CXL-eval/pr-offload-calibration/gate-proof.json \
  --iterations 2
```

After both commands pass, generate the final manifest:

```bash
python3 scripts/run_amu_paper_calibration.py fit \
  --measurements /mnt/disk0/gem5-CXL-eval/pr-offload-calibration/measurements.json \
  --pdf /home/victoryang00/gem5-CXL/3663479.pdf \
  --cira-csv /root/ia780i_type2_delay_buffer_new/benchmark_gapbs_workloads_ci_long.csv \
  --holdout-workload stream --holdout-latency 2us \
  --output /mnt/disk0/gem5-CXL-eval/pr-offload-calibration/amu-cira.json
```

Expected: schema 2, both approved hashes, validation PASS, complete residuals,
and `formal_speedup_is_fit_target=false`.

- [ ] **Step 4: Launch only the fresh g12 qualification first**

Use the accepted frozen inputs and no timeout:

```bash
python3 scripts/run_pr_asymmetric_offload.py \
  --inputs /mnt/disk0/gem5-CXL-eval/pr-scaling-120b389653d8/inputs.json \
  --calibration /mnt/disk0/gem5-CXL-eval/pr-offload-calibration/amu-cira.json \
  --root /mnt/disk0/gem5-CXL-eval/pr-offload-formal \
  --gem5 build/X86/gem5.opt \
  --m5-library util/m5/build/x86/out/libm5.a \
  --config configs/example/gem5_library/x86-gapbs-amu-se.py \
  --cxlmemuring /home/victoryang00/CXLMemUring \
  --m2ndp-root /mnt/disk0/M2NDP-public \
  --stop-after qualification
```

Expected: either a valid `qualification.json`, or a diagnostic hold that names
the real offender. Do not start g14/g20 on a hold.

- [ ] **Step 5: Resume the complete campaign only after qualification PASS**

Run:

```bash
python3 scripts/run_pr_asymmetric_offload.py \
  --inputs /mnt/disk0/gem5-CXL-eval/pr-scaling-120b389653d8/inputs.json \
  --calibration /mnt/disk0/gem5-CXL-eval/pr-offload-calibration/amu-cira.json \
  --root /mnt/disk0/gem5-CXL-eval/pr-offload-formal \
  --gem5 build/X86/gem5.opt \
  --m5-library util/m5/build/x86/out/libm5.a \
  --config configs/example/gem5_library/x86-gapbs-amu-se.py \
  --cxlmemuring /home/victoryang00/CXLMemUring \
  --m2ndp-root /mnt/disk0/M2NDP-public \
  --resume
```

Expected: `complete.json` only if all 27 points pass correctness/mechanism and
the nine formal accelerated points pass 1.4x--1.6x. Otherwise preserve the
hold and return to mechanism diagnosis in a new design/plan cycle.

- [ ] **Step 6: Commit and push the verified simulator branch**

Before claiming success, rerun the focused tests affected by any last fix,
inspect `git diff --check`, and use exact staging. Then:

```bash
git push origin m2ndp-g20-pr-spmv
git status --short --branch
```

Expected: local and origin branch heads match; the unrelated `base.cc` edit is
still uncommitted.

### Task 12: Publish accepted results into the paper repository

**Files:**
- Modify: `/home/victoryang00/gem5-CXL/6472666535e6f359942ddac6/gapbs-vtune-cxl-table.tex`
- Modify: `/home/victoryang00/gem5-CXL/6472666535e6f359942ddac6/sections/evaluation.tex`
- Create: `/home/victoryang00/gem5-CXL/6472666535e6f359942ddac6/generated/pr-offload/*`

- [ ] **Step 1: Generate publication artifacts from `complete.json`**

Run only after Task 11 produced a valid completion manifest:

```bash
python3 scripts/generate_pr_offload_artifacts.py \
  --complete /mnt/disk0/gem5-CXL-eval/pr-offload-formal/complete.json \
  --outdir /mnt/disk0/gem5-CXL-eval/pr-offload-formal/publication
```

Expected: all raw, TeX, PDF, and SVG outputs listed in Task 10 with hashes in
`pr-offload-evidence.json`.

- [ ] **Step 2: Copy exact generated assets and update prose**

Copy the accepted artifact directory to the paper's `generated/pr-offload`
directory. Replace the g4 table with an input of the generated g12/g14/g20
table. Update evaluation prose to state four threads, all-CXL 1 us, 20
iterations, matched Vanilla denominator, fully charged CIRA Few-shot/JIT,
bit-exact gating, and the distinction between additive phase and non-additive
mechanism breakdown. Remove the old claim that g4 is the primary comparison.

Do not type latency or speedup literals by hand; LaTeX must consume generated
data/macros.

```latex
\begin{table}[t]
  \centering
  \caption{Bit-exact end-to-end PageRank latency and speedup for four-thread,
  all-CXL 1~us execution over 20 synchronous iterations.}
  \label{tab:gapbs_vtune_cxl}
  \input{generated/pr-offload/pr-offload-table}
\end{table}
```

- [ ] **Step 3: Build and verify the paper**

Run:

```bash
cd /home/victoryang00/gem5-CXL/6472666535e6f359942ddac6
latexmk -pdf -interaction=nonstopmode -halt-on-error main.tex
git diff --check
git status --short --branch
```

Expected: `main.pdf` builds; table/figure references resolve; existing
untracked PDFs and raw evidence files remain untouched.

- [ ] **Step 4: Commit and push only the paper changes**

```bash
git add gapbs-vtune-cxl-table.tex sections/evaluation.tex generated/pr-offload
git commit -m "eval: add bit-exact PR offload scaling results"
git push origin master
```

Expected: the Overleaf branch contains only accepted generated results and
their explanatory prose. If formal evidence is held, skip this entire task.

## Completion evidence

The work is complete only when all of the following are simultaneously true:

- the simulator branch and paper branch are pushed to their intended remotes;
- all Python suites, gem5 build, m5ops build, and timing smoke tests pass;
- g12 primary and replay are identical in bits, policy, and native ticks;
- every formal vector is bit-exact and every device queue drains cleanly;
- `complete.json` contains 12 primary points, 15 CIRA ablations, and a passing
  nine-point 1.4x--1.6x gate;
- raw CSV/JSON recompute every displayed value;
- all four figure families and the LaTeX table derive from the accepted root;
  and
- the paper builds without using any diagnostic or stale evidence.
