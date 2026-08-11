# Coherent Per-Core L2-SPM Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `executing-plans` to implement this plan task by task. Keep the RED/GREEN evidence for every task and do not launch the full sweep until the 5 us GUPS gate passes.

**Goal:** Repair the calibrated AMU path so far-memory traffic remains on CXL while each timing core accesses a coherent 64 KiB, two-way private-L2 SPM, then keep 256 GUPS operations in flight without changing program results or the published MLP definition.

**Architecture:** ASMC retains its scalar far-memory port and gains one SPM timing port per core. An `SPM_ACCESS` request flag selects two reserved L2 ways only for ASMC SPM allocations; normal CPU traffic uses the other six ways but can still hit matching SPM tags. Loads execute far-read then local-SPM-write, stores execute local-SPM-read then far-write, with independent queues and retry state. The proxy primes a persistent SPM arena before ROI and uses fixed coroutine slots plus O(1) completion dispatch during ROI.

**Tech Stack:** gem5 C++/Python SimObjects, gem5 cache partitioning policies, X86 SE timing translation, C++17 proxy code, Python `unittest`, SCons, CSV/JSON/SHA-256 evidence, Matplotlib, LaTeX.

**Non-negotiable gates:** All checksums are byte-identical; GUPS 5 us uses the unchanged full-ROI `outstandingIntegral / occupancyTicks` metric and must exceed 130 without exceeding 256; all 36 calibration runs use one binary hash; g12/g14 and paper publication stay blocked until independent validation passes.

---

## Task 1: Reserve two private-L2 ways for flagged SPM allocations

**Files:**
- Modify: `src/mem/request.hh`
- Modify: `src/mem/cache/tags/partitioning_policies/PartitioningPolicies.py`
- Modify: `src/mem/cache/tags/partitioning_policies/SConscript`
- Create: `src/mem/cache/tags/partitioning_policies/spm_partition_manager.hh`
- Create: `src/mem/cache/tags/partitioning_policies/spm_partition_manager.cc`
- Create: `tests/pyunit/amu/test_asmc_l2_spm_partition.py`

- [ ] **Step 1: Write the failing source-contract tests**

Require a unique `Request::SPM_ACCESS` flag, an `isSpmAccess()` accessor, a
`SpmPartitionManager` SimObject, and this selection contract:

```python
class SpmPartitionContractTest(unittest.TestCase):
    def test_request_has_dedicated_spm_flag(self):
        self.assertRegex(REQUEST, r"SPM_ACCESS\s*=\s*0x[0-9A-Fa-f]+")
        self.assertIn("bool isSpmAccess() const", REQUEST)

    def test_partition_manager_selects_only_flagged_requests(self):
        self.assertIn("pkt->req->isSpmAccess()", SOURCE)
        self.assertIn("return spmPartitionId", SOURCE)
        self.assertIn("return 0", SOURCE)

    def test_sconscript_builds_policy(self):
        self.assertIn("SpmPartitionManager", SCONSCRIPT)
        self.assertIn("spm_partition_manager.cc", SCONSCRIPT)
```

Run:

```bash
python3 -m unittest discover -s tests/pyunit/amu \
  -p 'test_asmc_l2_spm_partition.py' -v
```

Expected RED: the flag and policy do not exist.

- [ ] **Step 2: Add a collision-free request flag**

Add an unused bit to `Request::FlagsType` and its accessor in
`src/mem/request.hh`. Add a compile-time assertion in the test that parses all
hex-valued flags and rejects duplicate bits. Only ASMC-created SPM packets may
set it.

```cpp
SPM_ACCESS = 0x0002000000000000,

bool isSpmAccess() const { return _flags.isSet(SPM_ACCESS); }
```

- [ ] **Step 3: Implement the flag-aware partition manager**

Expose `SpmPartitionManager` as a subclass of `PartitionManager`. Override
`readPacketPartitionID()` and map flagged requests to parameter
`spm_partition_id` (default 1); return partition 0 otherwise. Panic if the
configured ID is not represented by the attached policies.

```cpp
uint64_t
SpmPartitionManager::readPacketPartitionID(const PacketPtr pkt) const
{
    return pkt->req->isSpmAccess() ? spmPartitionId : 0;
}
```

Register the SimObject and source in `SConscript`.

- [ ] **Step 4: Run RED-to-GREEN tests and build the generated params**

```bash
python3 -m unittest discover -s tests/pyunit/amu \
  -p 'test_asmc_l2_spm_partition.py' -v
scons build/X86/gem5.opt -j"$(nproc)"
```

Expected: focused tests pass and the X86 target links without a SimObject or
parameter error.

- [ ] **Step 5: Commit**

```bash
git add src/mem/request.hh \
  src/mem/cache/tags/partitioning_policies \
  tests/pyunit/amu/test_asmc_l2_spm_partition.py
git commit -m "mem-cache: add AMU SPM way partition selector"
```

## Task 2: Give ASMC independent far-memory and per-core SPM timing paths

**Files:**
- Modify: `src/mem/ASMC.py`
- Modify: `src/mem/asmc.hh`
- Modify: `src/mem/asmc.cc`
- Replace: `tests/pyunit/amu/test_asmc_coherent_spm_writeback.py`
- Modify: `tests/pyunit/amu/test_asmc_paper_model.py`

- [ ] **Step 1: Replace the old single-port assumptions with failing tests**

The tests must require:

```python
self.assertIn("spm_side_ports = VectorRequestPort", ASMC_PY)
self.assertIn("SpmRead", HEADER)
self.assertIn("targetCore", HEADER)
self.assertIn("spmSendQueues", HEADER)
self.assertIn("spmRetryPkts", HEADER)
self.assertIn("Request::SPM_ACCESS", SOURCE)
self.assertIn("farReadPackets", HEADER)
self.assertIn("spmReadPackets", HEADER)
```

Slice the `astore` issue/response implementation and explicitly reject
`readSpm(` and `readGuest(`. Require far packets to call the scalar-port send
helper and SPM packets to select `spmSidePorts[state.targetCore]`.

Run:

```bash
python3 -m unittest discover -s tests/pyunit/amu \
  -p 'test_asmc_coherent_spm_writeback.py' -v
```

Expected RED: ASMC still has one request port and `astore` reads a functional
fallback.

- [ ] **Step 2: Extend ASMC state without changing the external ISA**

In `ASMC.py`, add:

```python
spm_side_ports = VectorRequestPort("Per-core coherent private-L2 SPM ports")
spm_send_queue_size = Param.Unsigned(512, "Packets queued per SPM port")
```

In C++, follow CIRA's vector-port constructor/getPort pattern. Add phases and
route metadata:

```cpp
enum class RequestPhase { SpmRead, MemoryAccess, SpmWriteback };

struct RequestState {
    unsigned targetCore = 0;
    RequestPhase phase = RequestPhase::MemoryAccess;
    std::vector<TranslationChunk> memoryChunks;
    std::vector<TranslationChunk> spmChunks;
    std::vector<uint8_t> data;
    uint32_t pendingPackets = 0;
};
```

Packet sender state records request ID, phase, core, byte offset, and size.
Resolve `targetCore` from `tc->contextId()` during issue and fail closed when it
is outside `spmSidePorts.size()`.

- [ ] **Step 3: Separate queue, retry, reservation, and scheduling state**

Keep the existing scalar far queue/retry state. Add one queue, retry packet,
retry flag, and send event per SPM port. Admission reserves all packets needed
by the next phase on that route before returning a nonzero ID. A response can
release only the reservation owned by that request and route. Reset must empty
every queue and reservation.

Do not share one `retryPkt` or `reservedSendSlots` across the two routes. Add
invariants for queue bounds, duplicate response phases, invalid core indices,
and completion with pending packets.

- [ ] **Step 4: Implement phase-correct packet routing**

State transitions are exactly:

```text
aload:  MemoryAccess(ReadReq, far port)
          -> SpmWriteback(WriteReq|SPM_ACCESS, issuing core's SPM port)
          -> finished ID

astore: SpmRead(ReadReq|SPM_ACCESS, issuing core's SPM port)
          -> MemoryAccess(WriteReq, far port)
          -> finished ID
```

For `SpmRead`, copy response bytes into `state.data`; the later far `WriteReq`
uses only those bytes. For a far load, copy response bytes into the same buffer;
the SPM write uses only that buffer. Completion becomes visible after the
second phase. Remove the store's internal-map and functional guest-memory
fallbacks. Keep the internal functional SPM helpers only if another explicitly
tested non-timing API still requires them.

Set `Request::SPM_ACCESS` only when constructing packets from `spmChunks`.
Assert the flag is absent on every far packet.

- [ ] **Step 5: Add route-specific statistics**

Expose scalar totals `farReadPackets`, `farWritePackets`, `farRetries`, and
vector totals `spmReadPackets`, `spmWritePackets`, `spmRetries` per core.
Preserve `issuedLoads`, `issuedStores`, `completedLoads`, `completedStores`,
and the unchanged outstanding-integral formula. Do not count SPM packets as
far-memory demand packets.

- [ ] **Step 6: Run focused suites and rebuild**

```bash
python3 -m unittest discover -s tests/pyunit/amu \
  -p 'test_asmc_coherent_spm_writeback.py' -v
python3 -m unittest discover -s tests/pyunit/amu \
  -p 'test_asmc_paper_model.py' -v
scons build/X86/gem5.opt -j"$(nproc)"
```

Expected: tests pass; X86 binary links; no old single-port fallback remains.

- [ ] **Step 7: Commit**

```bash
git add src/mem/ASMC.py src/mem/asmc.hh src/mem/asmc.cc \
  tests/pyunit/amu/test_asmc_coherent_spm_writeback.py \
  tests/pyunit/amu/test_asmc_paper_model.py
git commit -m "mem-asmc: route coherent SPM phases per core"
```

## Task 3: Wire one SPM port and a 6/2 way partition to every private L2

**Files:**
- Modify: `configs/example/gem5_library/x86-gapbs-amu-se.py`
- Create: `tests/pyunit/amu/test_amu_l2_spm_config.py`

- [ ] **Step 1: Write failing configuration tests**

Parse the config and require:

```python
self.assertIn("WayPolicyAllocation(0, [0, 1, 2, 3, 4, 5])", CONFIG)
self.assertIn("WayPolicyAllocation(1, [6, 7])", CONFIG)
self.assertIn("SpmPartitionManager", CONFIG)
self.assertIn("board.asmc.spm_side_ports", CONFIG)
self.assertIn("cache_hierarchy.l2buses", CONFIG)
```

Also import the config module with lightweight mocks and assert that the two
way sets are disjoint, their union is `range(8)`, every timing core is connected
once, and calibrated AMU rejects core/L2/SPM-port cardinality mismatches.

Run:

```bash
python3 -m unittest discover -s tests/pyunit/amu \
  -p 'test_amu_l2_spm_config.py' -v
```

Expected RED: no SPM partition or port connections exist.

- [ ] **Step 2: Configure the private L2s**

Import `SpmPartitionManager`, `WayPolicyAllocation`, and
`WayPartitioningPolicy`. When the calibrated AMU paper profile is active,
attach this partition manager to each 8-way private L2:

```python
SpmPartitionManager(partitioning_policies=[
    WayPartitioningPolicy(allocations=[
        WayPolicyAllocation(0, [0, 1, 2, 3, 4, 5]),
        WayPolicyAllocation(1, [6, 7]),
    ])
], spm_partition_id=1)
```

Reject an L2 associativity other than eight rather than silently changing
capacity. Leave non-AMU, Vanilla, and CIRA cache configuration unchanged.

- [ ] **Step 3: Connect ASMC ports after board components exist**

In `CXLSimpleBoard._connect_things()`, after the superclass connection, require
one L2 bus per timing core and connect each ASMC SPM vector port to the matching
`l2buses[index].cpu_side_ports`. Retain:

```text
ASMC mem_side_port -> ASMC I/O cache -> normal memory-side routing -> CXL
```

No SPM port may connect to the I/O cache or CXL bridge.

- [ ] **Step 4: Validate generated topology**

Run the focused test, rebuild, and run the smallest AMU SE smoke configuration.
Then require in `config.ini`:

```bash
rg -n 'spm_side_ports|partition_manager|spm_partition_id|ways=' \
  /mnt/disk0/gem5-CXL-g14-eval/coherent-l2-spm-topology-smoke/config.ini
```

Expected: four SPM connections for a four-core run; every L2 has the 6/2
partition; the scalar ASMC port remains on the far path.

- [ ] **Step 5: Commit**

```bash
git add configs/example/gem5_library/x86-gapbs-amu-se.py \
  tests/pyunit/amu/test_amu_l2_spm_config.py
git commit -m "configs: reserve per-core L2 ways for AMU SPM"
```

## Task 4: Keep the paper proxy window continuously occupied

**Files:**
- Modify: `util/amu/amu_paper_profile.cc`
- Create: `tests/pyunit/amu/test_amu_paper_profile_scheduler.py`
- Modify: `tests/pyunit/amu/test_amu_cira_calibration.py`

- [ ] **Step 1: Write failing scheduler and ROI-boundary tests**

Require a 64-byte-aligned persistent 64 KiB arena, exactly 256 fixed slots,
an ID-indexed owner/phase table, immediate slot refill, a pre-ROI `primeSpm()`
call, and checksum evaluation after `m5_work_end()`.

```python
self.assertIn("constexpr size_t kSpmBytes = 64 * 1024", SOURCE)
self.assertIn("constexpr size_t kWindowSlots = 256", SOURCE)
self.assertIn("primeSpm", BEFORE_WORK_BEGIN)
self.assertNotIn("std::find", SOURCE)
self.assertLess(SOURCE.index("m5_work_end"), SOURCE.index("checksum"))
```

Add a host-only deterministic scheduler test with completions returned in
reverse and shuffled order. It must produce the same final byte image and
checksum as the scalar reference and reject an ID with wrong owner or phase.

Run:

```bash
python3 -m unittest discover -s tests/pyunit/amu \
  -p 'test_amu_paper_profile_scheduler.py' -v
```

Expected RED: current code drains batches, performs linear `std::find`, and
checksums inside ROI.

- [ ] **Step 2: Allocate and prime one persistent SPM arena**

Allocate one aligned arena once per process. Reuse its slots across GUPS, HJ,
and STREAM. Before `m5_work_begin`, issue completed AMU loads covering every
cache line that the selected kernel will use; drain all completion IDs and
assert no request remains outstanding. The priming path must use normal ASMC
load operations so its local write packets carry `SPM_ACCESS` and allocate in
the reserved ways.

- [ ] **Step 3: Implement fixed coroutine slots and O(1) ID dispatch**

Each slot holds operation index, SPM offset, source/destination address, value,
request ID, and phase:

```cpp
enum class SlotPhase { Free, LoadPending, ReadyToStore, StorePending };
struct Slot { size_t op; uint64_t id; SlotPhase phase; /* payload */ };
struct IdOwner { uint16_t slot; SlotPhase expected; bool live; };
```

On completion, validate ID range, live ownership, slot ID, and expected phase
in O(1), advance only that slot, and refill a newly free slot immediately while
unissued operations remain. Never drain all loads before beginning stores.
Cap live request IDs at 256 and panic on duplicate or stale completions.

- [ ] **Step 4: Preserve deterministic operation order**

GUPS computes each update from its own loaded word, while commits that can
observe the same location follow scalar operation order. HJ and STREAM retain
their published granularity and baseline accumulation order. If independent
operations complete out of order, stage their results until the next scalar
commit index is ready; never reassociate floating-point additions.

- [ ] **Step 5: Move verification outside ROI**

Split each workload into `prepare`, `runKernel`, and `checksum` phases:

```cpp
prepareAndPrime(options);
m5_work_begin(0, 0);
runKernel(options);
m5_work_end(0, 0);
const auto digest = checksum(options);
writeRawOutput(digest);
```

The raw output schema and bytes stay unchanged so baseline/AMU comparison
remains exact.

- [ ] **Step 6: Run tests and rebuild the proxy**

```bash
python3 -m unittest discover -s tests/pyunit/amu \
  -p 'test_amu_paper_profile_scheduler.py' -v
python3 -m unittest discover -s tests/pyunit/amu \
  -p 'test_amu_cira_calibration.py' -v
scons build/X86/gem5.opt -j"$(nproc)"
make -C util/m5 build/x86/out/libm5.a
```

Compile both proxy variants with the existing calibration runner and compare
their host-visible reference output before launching gem5.

- [ ] **Step 7: Commit**

```bash
git add util/amu/amu_paper_profile.cc \
  tests/pyunit/amu/test_amu_paper_profile_scheduler.py \
  tests/pyunit/amu/test_amu_cira_calibration.py
git commit -m "benchmarks: keep AMU paper windows continuously occupied"
```

## Task 5: Enforce the 5 us GUPS proof gate before the full collection

**Files:**
- Modify if needed: `scripts/run_amu_paper_calibration.py`
- Create: `tests/pyunit/amu/test_amu_gups_gate.py`
- Runtime evidence: `/mnt/disk0/gem5-CXL-g14-eval/coherent-l2-spm-gate/`

- [ ] **Step 1: Add a fail-closed gate test**

The gate consumes baseline and AMU `stats.txt`, stdout, raw output, config, and
binary hashes. It must reject missing stats, `delay != 5000000`, checksum
mismatch, average MLP `<= 130`, peak MLP `> 256`, mixed binaries, wrong core
count, any far packet carrying `SPM_ACCESS`, or any SPM packet lacking it.

- [ ] **Step 2: Run a fresh baseline and AMU GUPS 5 us pair**

Use a new output root and the runner's existing `collect --workload gups
--latency 5us` selection. Do not copy or symlink old `stats.txt` files. Record
the exact invocation in `command.txt` and SHA-256 all binaries/configs/results.

- [ ] **Step 3: Independently recompute the gate**

From raw stats, calculate:

```text
avgOutstanding = outstandingIntegral / occupancyTicks
```

Verify baseline and AMU raw-output SHA-256 equality, issued/completed equality,
peak <= 256, average > 130, and the route-specific packet counts expected from
the operation count. Confirm generated `config.ini` records `delay=5000000`.

Expected: `Verification: PASS`, byte-identical output, and the unchanged metric
above 130. If any condition fails, stop here and return to the responsible
task; do not tune the threshold or launch the 36-run suite.

- [ ] **Step 4: Commit only code/test corrections**

Do not commit bulky simulator output. Commit runner/test changes and a compact
hash/proof manifest only after independent validation.

## Task 6: Recollect, fit, qualify, and publish AMU/CIRA/M2NDP end-to-end data

**Files:**
- Runtime evidence: `/mnt/disk0/gem5-CXL-g14-eval/calibrated-coherent-l2-spm/`
- Modify as required: `scripts/run_amu_paper_calibration.py`
- Modify as required: `scripts/run_gapbs_g12_qualification.py`
- Modify as required: `scripts/run_gapbs_g14_4thread_latency_sweep.py`
- Modify as required: `scripts/generate_gapbs_g14_4thread_latency_results.py`
- Modify as required: `scripts/validate_gapbs_g14_4thread_latency_results.py`
- Modify as required: `scripts/generate_gapbs_g14_4thread_latency_figure.py`
- Modify: `docs/amu-gapbs-benchmark.md`
- Modify: paper `gapbs-vtune-cxl-table.tex` and its generated figure/data assets

- [ ] **Step 1: Freeze one build and start a clean 36-run collection**

Create a new evidence root; never append to the failed pre-fix root. Record
git commit, dirty-state rejection, gem5/proxy SHA-256, AMU PDF SHA-256, hardware
CSV SHA-256, command lines, host information, and UTC timestamps. Run GUPS, HJ,
and STREAM at 0.1/0.2/0.5/1/2/5 us for baseline and AMU: 36 simulations total.

- [ ] **Step 2: Validate every row before fitting**

Require 36/36 successful processes, 18/18 baseline-AMU byte-equality pairs,
matching operation/request counts, correct latency in every `config.ini`, one
binary hash, and nonempty route-specific ASMC statistics. The validator must
produce a machine-readable rejection reason for any missing or stale row.

- [ ] **Step 3: Fit and freeze the calibrated manifest**

Run the existing fit path against only the new measurement CSV. Preserve the
holdout, residual, GUPS 5 us MLP, and no-direct-speedup checks. Freeze the
result as immutable JSON with input hashes; do not overwrite it during g12 or
g14.

- [ ] **Step 4: Run fresh g12 and g14 experiments**

Run four-thread, all-CXL data placement with the approved 200 ns/500 ns/1 us/
2 us sweep and the calibrated AMU/CIRA policies. Verify PageRank baseline,
AMU, CIRA modes, and M2NDP outputs bit-exact under the established synchronous
double-buffer, 20-iteration contract. Reject any restored checkpoint whose
binary/config/calibration hash differs from the frozen manifest.

- [ ] **Step 5: Generate and independently validate the paper artifacts**

Produce the end-to-end latency CSV, table, and figure comparing AMU, CIRA, and
M2NDP against the same gem5 two-/four-core all-CXL baseline defined by the
paper experiment. Recompute speedups from raw ticks, validate table/CSV/figure
hashes, then replace the tracked `gapbs-vtune-cxl-table.tex` and copy generated
assets into the paper repository. Build LaTeX and require a clean successful
PDF build.

- [ ] **Step 6: Run final verification**

```bash
python3 -m unittest discover -s tests/pyunit/amu -v
python3 -m unittest discover -s tests/pyunit/m2ndp -v
git diff --check
git status --short
```

Also run the publication validator and compare installed paper artifacts
byte-for-byte with the validated publication directory.

- [ ] **Step 7: Review, commit, and push both repositories**

Inspect the complete diff and proof manifest. Commit implementation/docs in the
gem5 branch and only generated validated artifacts in the paper branch. Push
`m2ndp-g20-pr-spmv` and the corresponding paper branch only after all proof
gates pass. Report exact commit IDs, evidence paths, row counts, checksum
status, MLP, and resulting AMU/CIRA/M2NDP latency comparison.

---

## Stop conditions

Stop and diagnose rather than publishing if any of the following occurs:

- GUPS 5 us average outstanding is not strictly greater than 130.
- Any baseline/accelerator output differs at the byte level.
- An SPM port carries graph/table demand traffic or a far port carries flagged
  SPM traffic.
- Core, private-L2, and connected SPM-port counts differ.
- Any collection row uses a different binary or calibration hash.
- A fit/holdout/g12/g14/publication validator reports failure.

No cached pre-fix result can satisfy any post-fix gate.
