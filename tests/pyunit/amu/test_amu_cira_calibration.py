# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

import math
import os
import tempfile
import unittest
from pathlib import Path

from scripts import amu_cira_calibration as calibration


PDF = Path(
    os.environ.get(
        "AMU_PDF", "/home/victoryang00/gem5-CXL/3663479.pdf"
    )
)
CSV = Path(
    os.environ.get(
        "CIRA_CSV",
        "/root/ia780i_type2_delay_buffer_new/"
        "benchmark_gapbs_workloads_ci_long.csv",
    )
)


class CalibrationSourceTest(unittest.TestCase):
    def test_amu_source_hash_and_direct_parameters(self):
        facts = calibration.load_amu_source(PDF)
        self.assertEqual(facts["sha256"], calibration.AMU_PDF_SHA256)
        self.assertEqual(facts["direct"]["spm_bytes"], 64 * 1024)
        self.assertEqual(facts["direct"]["pending_entries"], 32)
        self.assertEqual(facts["direct"]["id_batch_entries"], 32)
        self.assertEqual(
            facts["direct"]["latency_us"], [0.1, 0.2, 0.5, 1, 2, 5]
        )
        self.assertEqual(facts["validation"]["gups_5us_min_mlp"], 130)
        self.assertEqual(
            facts["classification"]["mean_speedup_1us"], "validation"
        )

    def test_amu_table4_is_numeric_validation_not_a_parameter(self):
        facts = calibration.load_amu_source(PDF)
        self.assertEqual(
            facts["validation"]["table4"]["gups"]["1"],
            {"baseline": 4.40, "amu": 0.98},
        )
        self.assertNotIn("mean_speedup_1us", facts["direct"])
        self.assertNotIn("table4", facts["direct"])

    def test_cira_excludes_failed_pr_and_preserves_fallbacks(self):
        facts = calibration.load_cira_source(CSV)
        self.assertNotIn("pr", facts["verified_workloads"])
        self.assertEqual(facts["primary"]["workload"], "pr_spmv")
        self.assertTrue(
            math.isclose(
                facts["primary"]["pgo_over_static"],
                1.004128673,
                rel_tol=1e-9,
            )
        )
        self.assertEqual(facts["rows"]["bfs"]["B"]["selected_from"], "")
        self.assertIn(
            "fell back", facts["rows"]["bfs"]["B"]["fallback"]
        )

    def test_cira_geomeans_use_only_seven_verified_workloads(self):
        facts = calibration.load_cira_source(CSV)
        self.assertEqual(
            facts["verified_workloads"],
            ["bc", "bfs", "cc", "cc_sv", "pr_spmv", "sssp", "tc"],
        )
        self.assertTrue(
            math.isclose(
                facts["geomean"]["static"], 0.884214397, rel_tol=1e-9
            )
        )
        self.assertTrue(
            math.isclose(
                facts["geomean"]["pgo_selected"],
                0.892296283,
                rel_tol=1e-9,
            )
        )

    def test_wrong_source_hash_fails_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            wrong = Path(temporary) / "source"
            wrong.write_bytes(b"not the approved source")
            with self.assertRaisesRegex(
                calibration.CalibrationError, "SHA-256"
            ):
                calibration.load_amu_source(wrong)
            with self.assertRaisesRegex(
                calibration.CalibrationError, "SHA-256"
            ):
                calibration.load_cira_source(wrong)


if __name__ == "__main__":
    unittest.main()
