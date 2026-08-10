# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

import unittest

from scripts import cira_lead_policy as policy


class CiraLeadPolicyTest(unittest.TestCase):
    def calibration(self):
        rows = {
            "A": {
                "verification": "PASS", "return_code": 0,
                "mean_time_ms": 181.223,
            },
            "B": {
                "verification": "PASS", "return_code": 0,
                "mean_time_ms": 180.505,
            },
            "C": {
                "verification": "PASS", "return_code": 0,
                "mean_time_ms": 187.470,
            },
        }
        return {
            "sources": {"cira_csv": {"rows": {"pr_spmv": rows}}},
            "cira": {"primary": {"selected_source_mode": "B"}},
        }

    def test_source_candidates_map_exact_hardware_rows(self):
        self.assertEqual(
            policy.SOURCE_CANDIDATES,
            {
                "A": {
                    "name": "static-default",
                    "row_window_rows": 64,
                    "lead_blocks": 1,
                },
                "B": {
                    "name": "row-window-2048",
                    "row_window_rows": 2048,
                    "lead_blocks": 32,
                },
                "C": {
                    "name": "row-window-1024",
                    "row_window_rows": 1024,
                    "lead_blocks": 16,
                },
            },
        )

    def test_three_modes_are_source_selected_and_hoist_gated(self):
        calibration = self.calibration()
        static = policy.resolve_mode(calibration, "static")
        pgo = policy.resolve_mode(calibration, "pgo-selected")
        few = policy.resolve_mode(
            calibration, "few-shot-online", source_row="C"
        )
        self.assertEqual(static["source_row"], "A")
        self.assertEqual(pgo["source_row"], "B")
        self.assertEqual(few["source_row"], "C")
        for selected in (static, pgo, few):
            self.assertTrue(selected["hoist_decision"]["emit_prefetch"])
            self.assertEqual(selected["row_window_rows"] % 64, 0)
            self.assertEqual(
                selected["lead_blocks"], selected["row_window_rows"] // 64
            )

    def test_pgo_rejects_abc_or_unverified_or_drifted_selection(self):
        calibration = self.calibration()
        calibration["cira"]["primary"]["selected_source_mode"] = "C"
        with self.assertRaisesRegex(policy.LeadPolicyError, "selected source"):
            policy.resolve_mode(calibration, "pgo-selected")
        calibration = self.calibration()
        calibration["sources"]["cira_csv"]["rows"]["pr_spmv"]["B"][
            "verification"
        ] = "FAIL"
        with self.assertRaisesRegex(policy.LeadPolicyError, "verified"):
            policy.resolve_mode(calibration, "pgo-selected")

    def test_frozen_latency_scaling(self):
        self.assertEqual(policy.lead_blocks_for_latency(2, 200), 1)
        self.assertEqual(policy.lead_blocks_for_latency(2, 500), 1)
        self.assertEqual(policy.lead_blocks_for_latency(2, 1000), 2)
        self.assertEqual(policy.lead_blocks_for_latency(2, 2000), 4)
        self.assertEqual(policy.ROW_BLOCK_SIZE, 64)
        self.assertEqual(policy.CANDIDATE_1US_LEADS, (1, 2, 4, 8))

    def test_invalid_leads_and_latencies_fail_closed(self):
        for selected in (0, 3, 7):
            with self.subTest(selected=selected):
                with self.assertRaises(policy.LeadPolicyError):
                    policy.lead_blocks_for_latency(selected, 1000)
        with self.assertRaises(policy.LeadPolicyError):
            policy.lead_blocks_for_latency(2, 0)

    def test_selection_uses_first_qualifying_lead_not_speedup(self):
        rows = {
            1: {
                "queue_rejections": 0,
                "dropped_descriptors": 0,
                "useful_prefetches": 3,
                "late_prefetches": 4,
                "speedup": 99,
            },
            2: {
                "queue_rejections": 0,
                "dropped_descriptors": 0,
                "useful_prefetches": 5,
                "late_prefetches": 4,
                "speedup": 0.1,
            },
            4: {
                "queue_rejections": 0,
                "dropped_descriptors": 0,
                "useful_prefetches": 9,
                "late_prefetches": 1,
                "speedup": 100,
            },
            8: {
                "queue_rejections": 0,
                "dropped_descriptors": 0,
                "useful_prefetches": 9,
                "late_prefetches": 1,
                "speedup": 100,
            },
        }
        self.assertEqual(policy.select_1us_lead(rows), 2)

    def test_uneven_static_partitions_keep_each_window_owned_and_bounded(self):
        total_rows = 1003
        num_threads = 4
        self.assertEqual(
            [
                policy.static_partition(total_rows, num_threads, tid)
                for tid in range(num_threads)
            ],
            [(0, 251), (251, 502), (502, 753), (753, 1003)],
        )
        for tid in range(num_threads):
            thread_begin, thread_end = policy.static_partition(
                total_rows, num_threads, tid
            )
            for current in range(thread_begin, thread_end):
                window = policy.future_block(
                    total_rows, num_threads, tid, current, lead_blocks=2
                )
                if window is None:
                    continue
                first, count = window
                self.assertEqual((first - thread_begin) % 64, 0)
                self.assertGreater(first, current)
                self.assertGreater(count, 0)
                self.assertLessEqual(count, 64)
                self.assertLessEqual(first + count, thread_end)
                self.assertEqual(
                    policy.owner_for_row(total_rows, num_threads, first), tid
                )


if __name__ == "__main__":
    unittest.main()
