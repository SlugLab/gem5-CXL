# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[3]
ASMC_PY = (REPO / "src/mem/ASMC.py").read_text(encoding="utf-8")
HEADER = (REPO / "src/mem/asmc.hh").read_text(encoding="utf-8")
SOURCE = (REPO / "src/mem/asmc.cc").read_text(encoding="utf-8")
CONFIG = (
    REPO / "configs/example/gem5_library/x86-gapbs-amu-se.py"
).read_text(encoding="utf-8")


class AsmcCoherentSpmWritebackTest(unittest.TestCase):
    def test_asmc_exposes_one_coherent_spm_request_port_per_core(self):
        self.assertIn("spm_side_ports = VectorRequestPort", ASMC_PY)
        self.assertIn("spm_send_queue_size = Param.Unsigned(", ASMC_PY)
        self.assertIn('512, "Packets queued per SPM port"', ASMC_PY)
        self.assertIn("std::vector<std::unique_ptr<MemoryPort>> spmSidePorts", HEADER)
        self.assertIn('if (if_name == "spm_side_ports")', SOURCE)

    def test_request_state_records_all_routes_and_exact_payload_offsets(self):
        self.assertIn("SpmRead", HEADER)
        self.assertIn("MemoryAccess", HEADER)
        self.assertIn("SpmWriteback", HEADER)
        self.assertIn("unsigned targetCore = 0", HEADER)
        self.assertIn("std::vector<TranslationChunk> memoryChunks", HEADER)
        self.assertIn("std::vector<TranslationChunk> spmChunks", HEADER)
        self.assertIn("Addr byteOffset", HEADER)
        self.assertIn("unsigned size", HEADER)

    def test_far_and_spm_paths_have_independent_backpressure_state(self):
        for token in (
            "std::deque<PacketPtr> farSendQueue",
            "PacketPtr farRetryPkt",
            "uint64_t reservedFarSendSlots",
            "std::vector<std::deque<PacketPtr>> spmSendQueues",
            "std::vector<PacketPtr> spmRetryPkts",
            "std::vector<bool> spmRetryReady",
            "std::vector<uint64_t> reservedSpmSendSlots",
            "std::vector<std::unique_ptr<EventFunctionWrapper>> spmSendEvents",
        ):
            self.assertIn(token, HEADER)

    def test_issue_resolves_and_validates_the_issuing_core(self):
        issue = SOURCE[
            SOURCE.index("ASMC::issue(ThreadContext"):
            SOURCE.index("ASMC::startInitialAccess")
        ]
        self.assertIn("tc->contextId()", issue)
        self.assertIn("spmSidePorts.size()", issue)
        self.assertIn("state->targetCore", issue)
        self.assertIn("reservedFarSendSlots", issue)
        self.assertIn("reservedSpmSendSlots[target_core]", issue)

    def test_aload_routes_far_read_then_flagged_local_spm_write(self):
        start = SOURCE[
            SOURCE.index("ASMC::startInitialAccess"):
            SOURCE.index("ASMC::startCompletionService")
        ]
        self.assertIn("MemCmd::ReadReq", start)
        self.assertIn("enqueueFarPackets(state", start)
        writeback = SOURCE[
            SOURCE.index("ASMC::startSpmWriteback"):
            SOURCE.index("ASMC::recvTimingResp")
        ]
        self.assertIn("state.spmChunks", writeback)
        self.assertIn("MemCmd::WriteReq", writeback)
        self.assertIn("RequestPhase::SpmWriteback", writeback)

    def test_astore_routes_flagged_local_spm_read_then_far_write(self):
        start = SOURCE[
            SOURCE.index("ASMC::startInitialAccess"):
            SOURCE.index("ASMC::startCompletionService")
        ]
        self.assertIn("RequestPhase::SpmRead", start)
        self.assertIn("enqueueSpmPackets(state", start)
        self.assertIn("MemCmd::ReadReq", start)
        write = SOURCE[
            SOURCE.index("ASMC::startMemoryWrite"):
            SOURCE.index("ASMC::startSpmWriteback")
        ]
        self.assertIn("state.memoryChunks", write)
        self.assertIn("MemCmd::WriteReq", write)
        self.assertIn("RequestPhase::MemoryAccess", write)

    def test_only_spm_packets_receive_the_partition_flag(self):
        spm = SOURCE[
            SOURCE.index("ASMC::enqueueSpmPackets"):
            SOURCE.index("ASMC::issue(ThreadContext")
        ]
        self.assertIn("Request::SPM_ACCESS", spm)
        far = SOURCE[
            SOURCE.index("ASMC::enqueueFarPackets"):
            SOURCE.index("ASMC::enqueueSpmPackets")
        ]
        self.assertIn("panic_if(req->isSpmAccess()", far)
        self.assertNotIn("Request::SPM_ACCESS", far)

    def test_astore_timing_path_has_no_functional_payload_fallback(self):
        issue = SOURCE[
            SOURCE.index("ASMC::issue(ThreadContext"):
            SOURCE.index("ASMC::startInitialAccess")
        ]
        self.assertNotIn("readSpm(", issue)
        self.assertNotIn("readGuest(", issue)
        self.assertNotIn("spmData", issue)

    def test_responses_copy_reads_to_the_request_buffer_and_check_route(self):
        response = SOURCE[
            SOURCE.index("ASMC::recvTimingResp"):
            SOURCE.index("ASMC::recvReqRetry")
        ]
        self.assertIn("sender_state->targetCore", response)
        self.assertIn("sender_state->byteOffset", response)
        self.assertIn("pkt->getConstPtr<uint8_t>()", response)
        self.assertIn("std::memcpy", response)
        self.assertIn("state.pendingPackets == 0", response)

    def test_route_specific_statistics_do_not_merge_spm_with_far_demand(self):
        for token in (
            "farReadPackets",
            "farWritePackets",
            "farRetries",
            "spmReadPackets",
            "spmWritePackets",
            "spmRetries",
        ):
            self.assertIn(token, HEADER)
            self.assertIn(token, SOURCE)

    def test_reset_clears_both_routes_and_all_reservations(self):
        reset = SOURCE[SOURCE.index("ASMC::reset"):]
        for token in (
            "farSendQueue",
            "farRetryPkt",
            "reservedFarSendSlots = 0",
            "spmSendQueues",
            "spmRetryPkts",
            "spmRetryReady",
            "reservedSpmSendSlots",
        ):
            self.assertIn(token, reset)

    def test_asmc_far_path_still_uses_the_coherent_io_cache(self):
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


if __name__ == "__main__":
    unittest.main()
