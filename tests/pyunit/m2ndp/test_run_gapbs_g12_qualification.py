# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

import json
import tempfile
import unittest
from pathlib import Path

from scripts import run_gapbs_g12_qualification as qualification


class G12QualificationTest(unittest.TestCase):
    def passing_candidate(self, lead):
        row = {
            "lead_blocks": lead,
            "issued_csr_prefetches": 8,
            "completed_prefetches": 64,
            "useful_prefetches": 20,
            "late_prefetches": 4,
            "rejected_queue_full": 0,
            "rejected_csr_index_queue_full": 0,
            "dropped_csr_descriptors": 0,
            "timing_csr_traversal": "true",
            "speedup_vs_cxl": "0.01",
        }
        for core in range(4):
            row[f"issued_csr_prefetches_core{core}"] = 2
            row[f"completed_prefetches_core{core}"] = 16
        return row

    def test_exact_action_order_is_latency_and_lead_frozen(self):
        self.assertEqual(
            qualification.ACTIONS,
            (
                "vanilla-1us",
                "amu-1us",
                "cira-lead-1-1us",
                "cira-lead-2-1us",
                "cira-lead-4-1us",
                "cira-lead-8-1us",
                "freeze-cira-policy",
            ),
        )

    def test_candidate_gate_uses_activity_not_speedup_and_stops_first(self):
        rows = {
            1: {**self.passing_candidate(1), "useful_prefetches": 4,
                "late_prefetches": 4},
            2: self.passing_candidate(2),
            4: self.passing_candidate(4),
            8: self.passing_candidate(8),
        }
        self.assertFalse(qualification.cira_candidate_passes(rows[1]))
        self.assertTrue(qualification.cira_candidate_passes(rows[2]))
        self.assertEqual(qualification.select_first_passing(rows), 2)

    def test_candidate_gate_rejects_each_loss_or_inactive_core(self):
        for field, value in (
            ("issued_csr_prefetches", 0),
            ("completed_prefetches", 0),
            ("useful_prefetches", 4),
            ("rejected_queue_full", 1),
            ("rejected_csr_index_queue_full", 1),
            ("dropped_csr_descriptors", 1),
            ("timing_csr_traversal", "false"),
            ("issued_csr_prefetches_core3", 0),
            ("completed_prefetches_core2", 0),
        ):
            with self.subTest(field=field):
                row = self.passing_candidate(2)
                row[field] = value
                self.assertFalse(qualification.cira_candidate_passes(row))

    def test_g12_traffic_classification_is_fail_closed(self):
        row = {
            field: 1 for field in qualification.REAL_CXL_FIELDS
        }
        self.assertEqual(
            qualification.classify_g12_traffic(row),
            {"g12_real_cxl": True, "g12_cache_resident": False},
        )
        row["mem_ctrl_cpu_data_reads"] = 0
        self.assertEqual(
            qualification.classify_g12_traffic(row),
            {"g12_real_cxl": False, "g12_cache_resident": True},
        )

    def test_resume_rejects_command_input_binary_or_output_hash_drift(self):
        record = qualification.passed_record(
            command=("run", "--lead", "2"),
            input_hashes={"graph": "a" * 64, "binary": "b" * 64},
            output_hashes={"summary": "c" * 64, "raw": "d" * 64},
            result={"verification": "pass"},
        )
        qualification.validate_resumed_record(
            record,
            command=("run", "--lead", "2"),
            input_hashes={"graph": "a" * 64, "binary": "b" * 64},
            output_hashes={"summary": "c" * 64, "raw": "d" * 64},
        )
        mutations = (
            {"command": ("run", "--lead", "4")},
            {"input_hashes": {"graph": "0" * 64, "binary": "b" * 64}},
            {"input_hashes": {"graph": "a" * 64, "binary": "0" * 64}},
            {"output_hashes": {"summary": "c" * 64, "raw": "0" * 64}},
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation), self.assertRaisesRegex(
                qualification.QualificationError, "resume.*hash|command"
            ):
                qualification.validate_resumed_record(
                    record,
                    command=mutation.get("command", ("run", "--lead", "2")),
                    input_hashes=mutation.get(
                        "input_hashes",
                        {"graph": "a" * 64, "binary": "b" * 64},
                    ),
                    output_hashes=mutation.get(
                        "output_hashes",
                        {"summary": "c" * 64, "raw": "d" * 64},
                    ),
                )

    def test_policy_freeze_is_exclusive_and_contains_result_hashes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "policy/cira-lead.json"
            qualification.freeze_policy(
                path,
                selected_1us_lead=2,
                source_profile="g12-4thread-qualification",
                result_hashes={"qualification": "a" * 64, "cira": "b" * 64},
            )
            value = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(value["selected_1us_lead_blocks"], 2)
            self.assertEqual(value["row_block_size"], 64)
            self.assertEqual(value["result_hashes"]["cira"], "b" * 64)
            with self.assertRaisesRegex(
                qualification.QualificationError, "already exists"
            ):
                qualification.freeze_policy(
                    path,
                    selected_1us_lead=4,
                    source_profile="g12-4thread-qualification",
                    result_hashes={"qualification": "c" * 64},
                )


if __name__ == "__main__":
    unittest.main()
