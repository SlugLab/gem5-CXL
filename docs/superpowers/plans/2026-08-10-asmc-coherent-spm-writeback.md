# ASMC Coherent SPM Writeback Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make an AMU load write its SPM destination through gem5's coherent timing path and expose completion only after that write is acknowledged.

**Architecture:** Extend each ASMC request with an initial memory-access phase and, for loads, a second SPM-writeback phase. Translate both virtual ranges before admission, reserve queue capacity for both packet sets, send the destination `WriteReq` packets through the existing coherent ASMC port, and enqueue the per-thread completion ID only after the final write response.

**Tech Stack:** gem5 C++ `ClockedObject` and timing ports, X86 SE virtual-to-physical translation, Python `unittest` source-contract tests, SCons incremental gem5 build, GAPBS fixed-float32 PageRank proof runner.

---

### Task 1: Lock the coherent completion contract with failing tests

**Files:**
- Create: `tests/pyunit/amu/test_asmc_coherent_spm_writeback.py`
- Inspect: `src/mem/asmc.hh`
- Inspect: `src/mem/asmc.cc`

- [ ] **Step 1: Write the source-contract test**

Create a test that reads the ASMC implementation and requires the exact
state needed by the approved design:

```python
# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[3]
HEADER = (REPO / "src/mem/asmc.hh").read_text(encoding="utf-8")
SOURCE = (REPO / "src/mem/asmc.cc").read_text(encoding="utf-8")


class AsmcCoherentSpmWritebackTest(unittest.TestCase):
    def test_load_request_has_two_explicit_timing_phases(self):
        self.assertIn("enum class RequestPhase", HEADER)
        self.assertIn("MemoryAccess", HEADER)
        self.assertIn("SpmWriteback", HEADER)
        self.assertIn("std::vector<TranslationChunk> spmChunks", HEADER)
        self.assertIn("RequestPhase phase = RequestPhase::MemoryAccess", HEADER)

    def test_admission_reserves_destination_packet_capacity(self):
        self.assertIn("reservedWritePackets", HEADER)
        self.assertIn("reservedSendSlots", HEADER)
        self.assertIn("countPackets", HEADER)
        self.assertIn("reservedSendSlots += spm_packets", SOURCE)
        self.assertIn("reservedSendSlots -= state.reservedWritePackets", SOURCE)

    def test_load_destination_uses_coherent_timing_writes(self):
        self.assertIn("startSpmWriteback", HEADER)
        self.assertIn("MemCmd::WriteReq", SOURCE)
        self.assertIn("state.spmChunks", SOURCE)
        self.assertIn("RequestPhase::SpmWriteback", SOURCE)

    def test_finished_queue_is_after_writeback_not_functional_write(self):
        complete = SOURCE[
            SOURCE.index("ASMC::completeRequest"):
            SOURCE.index("ASMC::getFinished")
        ]
        self.assertNotIn("writeGuest", complete)
        self.assertIn("finished[state.tc].push_back(id)", complete)
        response = SOURCE[
            SOURCE.index("ASMC::recvTimingResp"):
            SOURCE.index("ASMC::recvReqRetry")
        ]
        self.assertIn("startSpmWriteback(state)", response)

    def test_reset_clears_reserved_capacity(self):
        reset = SOURCE[SOURCE.index("ASMC::reset"):]
        self.assertIn("reservedSendSlots = 0", reset)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the test and verify RED**

Run:

```bash
python3 -m unittest discover -s tests/pyunit/amu \
  -p 'test_asmc_coherent_spm_writeback.py' -v
```

Expected: failures for missing `RequestPhase`, `spmChunks`, reservation
fields, and `startSpmWriteback`; the existing functional `writeGuest()` is
still visible in `completeRequest()`.

### Task 2: Add request phases and fail-closed packet reservations

**Files:**
- Modify: `src/mem/asmc.hh:38-150`
- Modify: `src/mem/asmc.cc:180-340`
- Test: `tests/pyunit/amu/test_asmc_coherent_spm_writeback.py`

- [ ] **Step 1: Define request and packet phase state**

Add the phase enum before `RequestState`, then extend request and sender
state:

```cpp
enum class RequestPhase
{
    MemoryAccess,
    SpmWriteback,
};

struct RequestState
{
    uint64_t id = 0;
    ReqType type = ReqType::Load;
    RequestPhase phase = RequestPhase::MemoryAccess;
    ThreadContext *tc = nullptr;
    Addr spmAddr = 0;
    Addr memAddr = 0;
    uint64_t size = 0;
    Tick issueTick = 0;
    std::vector<uint8_t> data;
    std::vector<TranslationChunk> spmChunks;
    uint32_t pendingPackets = 0;
    uint32_t reservedWritePackets = 0;
};

struct PacketSenderState : public Packet::SenderState
{
    PacketSenderState(uint64_t request_id, RequestPhase request_phase,
                      bool is_read)
        : id(request_id), phase(request_phase), read(is_read)
    {}

    uint64_t id;
    RequestPhase phase;
    bool read;
};
```

- [ ] **Step 2: Declare focused packet helpers and reservation state**

Add these private members:

```cpp
uint32_t countPackets(const std::vector<TranslationChunk> &chunks) const;
void enqueuePackets(RequestState &state,
                    const std::vector<TranslationChunk> &chunks,
                    MemCmd command, RequestPhase phase);
void startSpmWriteback(RequestState &state);

uint64_t reservedSendSlots = 0;
```

Remove `writeGuest()` from the header because coherent timing writes replace
the only load-completion caller.

- [ ] **Step 3: Implement packet counting and generic enqueue**

Factor the existing packet-splitting loop into helpers. `countPackets()` must
use both translated-page chunks and cache-line boundaries:

```cpp
uint32_t
ASMC::countPackets(const std::vector<TranslationChunk> &chunks) const
{
    uint32_t count = 0;
    for (const auto &chunk : chunks) {
        Addr offset = 0;
        while (offset < chunk.size) {
            const uint64_t line_remaining =
                cacheLineSize - ((chunk.paddr + offset) % cacheLineSize);
            offset += std::min<uint64_t>(chunk.size - offset,
                                        line_remaining);
            ++count;
        }
    }
    return count;
}
```

`enqueuePackets()` creates packets against `state.data`, records both the
request ID and phase in `PacketSenderState`, increments
`state.pendingPackets`, and calls the existing `enqueuePacket()`.

- [ ] **Step 4: Translate and reserve both load phases before admission**

In `ASMC::issue()`, keep the current source translation and add destination
translation for loads:

```cpp
std::vector<TranslationChunk> spm_chunks;
if (type == ReqType::Load &&
    !translate(tc, spm_addr, granularity, BaseMMU::Write, spm_chunks)) {
    ++stats.translationFaults;
    return 0;
}

const uint32_t memory_packets = countPackets(chunks);
const uint32_t spm_packets = countPackets(spm_chunks);
const uint64_t occupied = sendQueue.size() + (retryPkt ? 1 : 0) +
                          reservedSendSlots;
if (occupied + memory_packets + spm_packets > maxSendQueue) {
    ++stats.rejectedQueueFull;
    return 0;
}
```

Move `spm_chunks` into `state->spmChunks`, set
`state->reservedWritePackets`, increment `reservedSendSlots`, and enqueue
only the initial memory phase. A load uses `ReadReq`; a store retains its
existing `WriteReq` behavior.

- [ ] **Step 5: Run the focused test**

Run the Task 1 command.

Expected: phase and reservation assertions pass; writeback/final-completion
assertions remain RED until Task 3.

### Task 3: Send coherent destination writes before completion

**Files:**
- Modify: `src/mem/asmc.cc:330-490`
- Test: `tests/pyunit/amu/test_asmc_coherent_spm_writeback.py`

- [ ] **Step 1: Implement the phase transition**

Add `startSpmWriteback()`:

```cpp
void
ASMC::startSpmWriteback(RequestState &state)
{
    panic_if(state.type != ReqType::Load,
             "ASMC SPM writeback requested for a store");
    panic_if(state.phase != RequestPhase::MemoryAccess ||
             state.pendingPackets != 0,
             "ASMC invalid source-to-SPM phase transition");
    panic_if(state.reservedWritePackets !=
                 countPackets(state.spmChunks) ||
             reservedSendSlots < state.reservedWritePackets,
             "ASMC invalid SPM packet reservation");

    reservedSendSlots -= state.reservedWritePackets;
    state.reservedWritePackets = 0;
    state.phase = RequestPhase::SpmWriteback;
    enqueuePackets(state, state.spmChunks, MemCmd::WriteReq,
                   RequestPhase::SpmWriteback);
    scheduleSend(curTick());
}
```

- [ ] **Step 2: Make response processing phase-aware**

After locating the request in `recvTimingResp()`, require the packet phase to
equal the request phase. When `pendingPackets` reaches zero:

```cpp
if (state.pendingPackets == 0) {
    if (state.type == ReqType::Load &&
        state.phase == RequestPhase::MemoryAccess) {
        startSpmWriteback(state);
    } else {
        auto *event = new EventFunctionWrapper(
            [this, id] { completeRequest(id); },
            csprintf("%s.complete_%llu", name(),
                     static_cast<unsigned long long>(id)),
            true);
        schedule(event, curTick() + completionLatency + configuredLatency);
    }
}
```

The configured AMU completion latency remains after the full transfer rather
than between source read and destination write.

- [ ] **Step 3: Remove functional destination visibility**

Delete `ASMC::writeGuest()`. In `completeRequest()`, a load only updates the
internal SPM image because the coherent timing write already updated guest
memory:

```cpp
if (state.type == ReqType::Load)
    writeSpm(state.spmAddr, state.data.data(), state.size);
```

Before enqueuing `finished[state.tc]`, assert that a load is in
`SpmWriteback`, has no pending packets, and has no reservation. Store
completion remains in `MemoryAccess`.

- [ ] **Step 4: Clear reservation state on reset**

Append:

```cpp
reservedSendSlots = 0;
```

to `ASMC::reset()` after outstanding requests and queues are deleted.

- [ ] **Step 5: Run the source-contract test and verify GREEN**

Run the Task 1 command.

Expected: all tests PASS.

- [ ] **Step 6: Commit the coherent implementation**

```bash
git add src/mem/asmc.hh src/mem/asmc.cc \
  tests/pyunit/amu/test_asmc_coherent_spm_writeback.py
git commit -m "fix: make ASMC load completion coherent"
```

### Task 4: Build gem5 and run regression gates

**Files:**
- Generate: `build/X86/gem5.opt`
- Verify: `tests/pyunit/amu/`
- Verify: `tests/pyunit/m2ndp/`

- [ ] **Step 1: Incrementally rebuild gem5**

Run:

```bash
scons build/X86/gem5.opt -j16
```

Expected: exit status 0 and a new `build/X86/gem5.opt` containing the ASMC
object rebuild.

- [ ] **Step 2: Run AMU and M2NDP Python regressions**

Run:

```bash
python3 -m unittest discover -s tests/pyunit/amu -p 'test_*.py'
python3 -m unittest discover -s tests/pyunit/m2ndp -p 'test_*.py'
```

Expected: both suites PASS with zero failures and zero errors.

- [ ] **Step 3: Run syntax and diff checks**

Run:

```bash
python3 -m py_compile \
  scripts/build_gapbs_amu_cxlmemuring.py \
  scripts/build_gapbs_matched_pr_spmv_variants.py \
  scripts/run_gapbs_matched_pr_spmv_variants.py \
  scripts/run_gapbs_g4_4thread_latency_sweep.py
git diff --check
```

Expected: exit status 0 with no output.

### Task 5: Resume AMU at 200 ns and enforce bit-exact proof gates

**Files:**
- Reuse: `m5out/g4_4thread_latency_sweep_20260809-proof/`
- Generate: fresh hash-keyed AMU checkpoint and run directory
- Inspect: `m5out/g4_4thread_latency_sweep_20260809-proof/status.json`

- [ ] **Step 1: Confirm no prior proof unit is active**

Run:

```bash
sudo systemctl is-active gapbs-g4-200ns-proof-20260809-r4.service
```

Expected: `inactive` or `failed`. Do not signal any g20 process.

- [ ] **Step 2: Launch the proof resume without a runtime limit**

Use a new unit name and the pinned M2NDP build toolchain:

```bash
sudo systemd-run --no-block \
  --unit=gapbs-g4-200ns-proof-20260810-coherent \
  --collect \
  --property=WorkingDirectory=/home/victoryang00/gem5-CXL/.worktrees/m2ndp-g20-pr-spmv \
  --property=Nice=10 \
  --property=CPUWeight=10 \
  --property=StandardOutput=append:/home/victoryang00/gem5-CXL/.worktrees/m2ndp-g20-pr-spmv/m5out/g4_4thread_latency_sweep_20260809-proof/service-coherent.log \
  --property=StandardError=append:/home/victoryang00/gem5-CXL/.worktrees/m2ndp-g20-pr-spmv/m5out/g4_4thread_latency_sweep_20260809-proof/service-coherent.log \
  /usr/bin/env \
  CONAN=/home/victoryang00/gem5-CXL/.worktrees/m2ndp-g20-pr-spmv/m5out/m2ndp_toolchain/venv311/bin/conan \
  CMAKE=/home/victoryang00/gem5-CXL/.worktrees/m2ndp-g20-pr-spmv/m5out/m2ndp_toolchain/venv311/bin/cmake \
  CC=/usr/bin/x86_64-linux-gnu-gcc-13 \
  CXX=/usr/bin/x86_64-linux-gnu-g++-13 \
  /usr/bin/python3 scripts/run_gapbs_g4_4thread_latency_sweep.py \
  --graph /home/victoryang00/gem5-CXL/.worktrees/gapbs-latency-table/m5out/gapbs_graphs/g4.sg \
  --variants-build m5out/g4_4thread_latency_sweep_20260809/build/variants \
  --cxlmemuring /home/victoryang00/CXLMemUring \
  --m2ndp-root m5out/m2ndp/source \
  --gem5 build/X86/gem5.opt \
  --outdir m5out/g4_4thread_latency_sweep_20260809-proof \
  --stop-after-latency 200ns \
  --timeout 0 --resume
```

Expected: the existing Vanilla output hash is accepted, AMU receives a fresh
checkpoint identity due to the changed gem5/binary evidence, and the unit
continues to CIRA only after AMU passes.

- [ ] **Step 3: Validate AMU evidence before accepting the row**

Inspect `runs/200ns/amu/summary.csv`, `evidence.json`, `stats.txt`, and raw
vector output. Required conditions:

```text
status=ok
verification=pass
cores=4
cxl_link_delay=200ns
all_memory_cxl=True
checkpoint_restores=1
asmc_loads > 0
asmc_loads == asmc_completed
board.asmc.rejectedQueueFull == 0
board.asmc.rejectedSpmFull == 0
board.asmc.translationFaults == 0
AMU_INVALID_NODE absent
AMU raw SHA-256 == Vanilla raw SHA-256
```

If any condition fails, preserve the unit logs and stop at that first gate.

- [ ] **Step 4: Let the isolated proof finish CIRA and M2NDP**

Expected final service markers:

```text
G4_SWEEP_ACTION_PASS latency=200ns system=amu
G4_SWEEP_ACTION_PASS latency=200ns system=cira
G4_SWEEP_ACTION_PASS latency=200ns system=m2ndp
G4_SWEEP_STOP_BOUNDARY latency=200ns
```

Do not start the formal four-latency service until all four rows, all raw
hashes, FuncSim, and M2NDP calibration pass.

### Task 6: Record the repaired proof boundary

**Files:**
- Modify: `docs/amu-gapbs-benchmark.md`
- Verify: `m5out/g4_4thread_latency_sweep_20260809-proof/status.json`

- [ ] **Step 1: Document the coherent completion semantics and proof**

Add a short dated note stating that ASMC load completion now follows source
timing read, coherent SPM timing write, and final write acknowledgment. Record
the commit, gem5 binary SHA-256, proof unit name, 200 ns AMU ROI ticks,
issued/completed counts, and exact raw vector hash from the validated files.
Do not copy values from terminal memory; read them from the proof artifacts.

- [ ] **Step 2: Run documentation checks**

```bash
git diff --check
rg -n "coherent SPM|200 ns|bit-exact" docs/amu-gapbs-benchmark.md
```

Expected: no whitespace errors and the note includes all three proof terms.

- [ ] **Step 3: Commit the proof note**

```bash
git add docs/amu-gapbs-benchmark.md
git commit -m "docs: record coherent four-core AMU proof"
```
