# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

import unittest
from pathlib import Path

from scripts import build_gapbs_matched_pr_spmv_variants as variants


REPO = Path(__file__).resolve().parents[3]
WORKLOAD = REPO / "util" / "pr_offload" / "gapbs_pr_spmv_offload.cc"


class CiraPrRuntimePolicyTest(unittest.TestCase):
    def test_candidate_table_is_frozen(self):
        self.assertEqual(
            variants.CANDIDATES,
            {
                "A": {"row_window": 64, "lead_blocks": 1},
                "B": {"row_window": 2048, "lead_blocks": 32},
                "C": {"row_window": 1024, "lead_blocks": 16},
            },
        )

    def test_common_driver_is_persistent_four_thread_double_buffered(self):
        self.assertTrue(WORKLOAD.is_file())
        source = WORKLOAD.read_text(encoding="utf-8")
        for token in (
            "#pragma omp parallel num_threads(4)",
            "omp_get_num_threads() != 4",
            "pr_static_partition",
            "PR_ROW_CONTRIB",
            "PR_ROW_PULL",
            "waitForExact",
            "scores.swap(nextScores)",
            "constexpr int kPageRankIterations = 20",
        ):
            self.assertIn(token, source)
        self.assertEqual(source.count("#pragma omp parallel num_threads(4)"), 1)

    def test_few_shot_samples_a_b_c_then_irreversibly_reconfigures(self):
        source = WORKLOAD.read_text(encoding="utf-8")
        self.assertIn("sampleCandidate(Candidate::A", source)
        self.assertIn("sampleCandidate(Candidate::B", source)
        self.assertIn("sampleCandidate(Candidate::C", source)
        self.assertIn("selectMinimumPositive", source)
        self.assertIn("CIRA_CFG_PR_RECONFIGURE", source)
        self.assertIn("waitForExact(reconfiguration", source)
        self.assertIn("discardedSampleOutputs", source)
        self.assertIn('"PR_CIRA_POLICY selected=%c', source)
        sample = source[
            source.index("sampleCandidate(Candidate candidate"):
            source.index("selectMinimumPositive")
        ]
        self.assertNotIn("flushRange(", sample)

        sampling_batch = source[
            source.index("ledger.transition(Phase::Sampling"):
            source.index("ledger.transition(Phase::Selection")
        ]
        self.assertEqual(sampling_batch.count("flushRange("), 1)
        self.assertLess(
            sampling_batch.index("flushRange("),
            sampling_batch.index("sampleCandidate(Candidate::A"),
        )

    def test_cira_writes_back_each_owned_partition_before_phase_barriers(self):
        source = WORKLOAD.read_text(encoding="utf-8")
        contribution_done = source.index("submitAndWait(contribution")
        contribution_flush = source.index(
            "flushRange(contributions.data() + begin", contribution_done
        )
        pull_done = source.index("submitAndWait(pull")
        pull_flush = source.index(
            "flushRange(nextScores.data() + begin", pull_done
        )
        self.assertLess(contribution_done, contribution_flush)
        self.assertLess(contribution_flush, pull_done)
        self.assertLess(pull_done, pull_flush)
        self.assertIn("#if defined(PR_OFFLOAD_CIRA)", source)

    def test_phase_ledger_is_inside_roi_but_marker_is_after_work_end(self):
        source = WORKLOAD.read_text(encoding="utf-8")
        begin = source.index("m5_work_begin(trial, 0)")
        ledger = source.index("ledger.start(m5_rpns()", begin)
        finish = source.index("ledger.finish(m5_rpns()", ledger)
        end = source.index("m5_work_end(trial, 0)", finish)
        marker = source.index('"PR_E2E_PHASES formation=%llu', end)
        self.assertLess(begin, ledger)
        self.assertLess(ledger, finish)
        self.assertLess(finish, end)
        self.assertLess(end, marker)

    def test_policy_compile_definitions_are_unambiguous(self):
        self.assertEqual(
            variants.policy_compile_definitions("static", "A"),
            ["-DPR_CIRA_POLICY_STATIC=1", "-DPR_CIRA_SOURCE_ROW=0"],
        )
        self.assertEqual(
            variants.policy_compile_definitions("pgo-selected", "B"),
            ["-DPR_CIRA_POLICY_PGO=1", "-DPR_CIRA_SOURCE_ROW=1"],
        )
        self.assertEqual(
            variants.policy_compile_definitions("few-shot-online", None),
            ["-DPR_CIRA_POLICY_FEWSHOT=1"],
        )


if __name__ == "__main__":
    unittest.main()
