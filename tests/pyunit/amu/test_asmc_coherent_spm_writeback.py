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
        self.assertIn(
            "reservedSendSlots -= state.reservedWritePackets", SOURCE
        )

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
