# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[3]
HEADER = (REPO / "src/mem/asmc.hh").read_text(encoding="utf-8")
SOURCE = (REPO / "src/mem/asmc.cc").read_text(encoding="utf-8")
CONFIG = (
    REPO / "configs/example/gem5_library/x86-gapbs-amu-se.py"
).read_text(encoding="utf-8")


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
        self.assertIn(
            "reservedSendSlots -= state.reservedWritePackets", SOURCE
        )

    def test_load_destination_uses_coherent_timing_writes(self):
        self.assertIn("startSpmWriteback", HEADER)
        self.assertIn("MemCmd::WriteReq", SOURCE)
        self.assertIn("state.spmChunks", SOURCE)
        self.assertIn("RequestPhase::SpmWriteback", SOURCE)

    def test_load_source_uses_coherent_timing_read_not_functional_read(self):
        issue = SOURCE[
            SOURCE.index("ASMC::issue(ThreadContext"):
            SOURCE.index("ASMC::startSpmWriteback")
        ]
        self.assertIn("MemCmd::ReadReq", issue)
        self.assertIn(
            "enqueuePackets(*raw_state, chunks, command, "
            "RequestPhase::MemoryAccess)",
            issue,
        )
        self.assertNotIn("readGuest(tc, mem_addr", issue)

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

    def test_backpressured_port_waits_for_real_retry_callback(self):
        schedule = SOURCE[
            SOURCE.index("ASMC::scheduleSend"):
            SOURCE.index("ASMC::trySend")
        ]
        self.assertIn("if (retryPkt)\n        return;", schedule)
        retry = SOURCE[
            SOURCE.index("ASMC::recvReqRetry"):
            SOURCE.index("ASMC::completeRequest")
        ]
        self.assertIn("schedule(sendEvent, curTick())", retry)

    def test_asmc_uses_a_coherent_io_cache_before_the_membus(self):
        self.assertIn("board.asmc_io_cache = Cache(", CONFIG)
        self.assertIn(
            "board.asmc.mem_side_port = board.asmc_io_cache.cpu_side",
            CONFIG,
        )
        self.assertIn(
            "board.asmc_io_cache.mem_side = "
            "cache_hierarchy.get_cpu_side_port()",
            CONFIG,
        )

    def test_io_cache_ranges_are_bound_after_workload_initializes_board(self):
        workload = CONFIG.index("board.set_se_binary_workload(")
        range_binding = CONFIG.index(
            "board.asmc_io_cache.addr_ranges = board.mem_ranges"
        )
        self.assertGreater(range_binding, workload)

        constructor = CONFIG[
            CONFIG.index("board.asmc_io_cache = Cache("):
            CONFIG.index("board.asmc.mem_side_port = ")
        ]
        self.assertNotIn("addr_ranges=board.mem_ranges", constructor)


if __name__ == "__main__":
    unittest.main()
