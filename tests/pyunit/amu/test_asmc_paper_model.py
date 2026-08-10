# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[3]
ASMC_PY = (REPO / "src/mem/ASMC.py").read_text(encoding="utf-8")
HEADER = (REPO / "src/mem/asmc.hh").read_text(encoding="utf-8")
SOURCE = (REPO / "src/mem/asmc.cc").read_text(encoding="utf-8")


class AsmcPaperModelTest(unittest.TestCase):
    def test_paper_resource_parameters_exist(self):
        for token in (
            'pending_queue_entries = Param.Unsigned(32',
            'id_batch_entries = Param.Unsigned(32',
            'metadata_latency = Param.Cycles(10',
            'id_refill_latency = Param.Cycles(0',
            'completion_publish_latency = Param.Cycles(0',
        ):
            self.assertIn(token, ASMC_PY)

    def test_internal_pending_queue_does_not_replace_amart_limit(self):
        issue = SOURCE[
            SOURCE.index("ASMC::issue(ThreadContext") :
            SOURCE.index("ASMC::startSpmWriteback")
        ]
        self.assertIn("outstanding.size() >= maxOutstanding", issue)
        self.assertIn("metadataPending >= pendingQueueEntries", issue)
        self.assertIn("startMemoryAccess", HEADER + SOURCE)
        self.assertIn("metadataPending--", SOURCE)

    def test_id_batches_refill_only_at_batch_boundary(self):
        self.assertIn("idsRemaining", HEADER)
        self.assertIn("if (idsRemaining == 0)", SOURCE)
        self.assertIn("idsRemaining = idBatchEntries", SOURCE)
        self.assertIn("++stats.idBatchRefills", SOURCE)
        self.assertIn("--idsRemaining", SOURCE)

    def test_completion_and_polling_stats_are_recorded(self):
        for token in (
            "outstandingIntegral",
            "maxObservedOutstanding",
            "pendingQueueFull",
            "idBatchRefills",
            "metadataAccesses",
            "emptyGetfinPolls",
            "successfulGetfin",
            "consumerWaitTicks",
            "avgOutstanding",
        ):
            self.assertIn(token, HEADER + SOURCE)

    def test_occupancy_is_closed_at_dump_and_reset_boundaries(self):
        self.assertIn("void preDumpStats() override", HEADER)
        self.assertIn("owner.updateOccupancyIntegral()", SOURCE)
        self.assertIn("void resetStats() override", HEADER)
        reset = SOURCE[
            SOURCE.index("ASMC::resetStats") : SOURCE.index("ASMC::getPort")
        ]
        self.assertIn("lastOccupancyTick = curTick()", reset)

    def test_getfin_measures_real_poll_wait_without_extra_fake_delay(self):
        getfin = SOURCE[
            SOURCE.index("ASMC::getFinished") : SOURCE.index("ASMC::cfgWrite")
        ]
        self.assertIn("++stats.emptyGetfinPolls", getfin)
        self.assertIn("++stats.successfulGetfin", getfin)
        self.assertIn("pollWaitStart", getfin)
        self.assertNotIn("getfinLatency", getfin)

    def test_reset_clears_new_resource_state(self):
        reset = SOURCE[SOURCE.index("ASMC::reset") :]
        for token in (
            "metadataPending = 0",
            "completionPending = 0",
            "idsRemaining = 0",
            "pollWaitStart.clear()",
        ):
            self.assertIn(token, reset)


if __name__ == "__main__":
    unittest.main()
