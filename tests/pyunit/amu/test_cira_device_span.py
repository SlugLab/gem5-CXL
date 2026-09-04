# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from scripts import compare_gapbs_cxl_amu_cira as comparison


REPO = Path(__file__).resolve().parents[3]
WORKLOAD = REPO / "tests/gem5/cira/cira_multicore_prefetch.cc"
CONFIG = REPO / "tests/gem5/cira/run_cira_multicore.py"


class CiraDeviceSpanTest(unittest.TestCase):
    def run_multicore_workload(self, gem5, m5_library):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            binary = root / "cira_multicore_prefetch"
            compile_result = subprocess.run(
                [
                    "g++",
                    "-std=c++17",
                    "-O2",
                    "-Wall",
                    "-Wextra",
                    "-static",
                    "-no-pie",
                    "-I",
                    str(REPO / "include"),
                    "-I",
                    str(REPO / "util/cira"),
                    str(WORKLOAD),
                    str(m5_library),
                    "-o",
                    str(binary),
                ],
                cwd=REPO,
                capture_output=True,
                text=True,
                timeout=120,
            )
            self.assertEqual(
                compile_result.returncode,
                0,
                compile_result.stdout + compile_result.stderr,
            )
            outdir = root / "m5out"
            run_result = subprocess.run(
                [
                    str(gem5),
                    "--outdir",
                    str(outdir),
                    str(CONFIG),
                    "--binary",
                    str(binary),
                ],
                cwd=REPO,
                capture_output=True,
                text=True,
                timeout=600,
            )
            self.assertEqual(
                run_result.returncode,
                0,
                run_result.stdout + run_result.stderr,
            )
            return comparison.parse_stats(outdir / "stats.txt")

    def test_live_multicore_prefetch_reports_consistent_busy_spans(self):
        gem5_value = os.environ.get("CIRA_TEST_GEM5")
        m5_value = os.environ.get("CIRA_TEST_M5_LIBRARY")
        if not gem5_value or not m5_value:
            self.skipTest(
                "CIRA_TEST_GEM5 and CIRA_TEST_M5_LIBRARY are not set"
            )
        gem5 = Path(gem5_value).resolve()
        m5_library = Path(m5_value).resolve()
        self.assertTrue(gem5.is_file(), gem5)
        self.assertTrue(m5_library.is_file(), m5_library)

        stats = self.run_multicore_workload(gem5, m5_library)
        first = int(stats["board.cira.genericPrefetchFirstIssueTick"])
        last = int(stats["board.cira.genericPrefetchLastCompletionTick"])
        busy = int(stats["board.cira.genericPrefetchBusyTicks"])
        self.assertGreater(first, 0)
        self.assertGreaterEqual(last, first)
        self.assertEqual(busy, last - first)
        self.assertEqual(
            int(stats["board.cira.genericPrefetchSpanValid"]), 1
        )
        self.assertEqual(
            int(stats["board.cira.genericPrefetchResetOutstanding"]), 0
        )

        for core in range(4):
            issued = int(
                stats[f"board.cira.issuedPrefetchesPerCore::{core}"]
            )
            completed = int(
                stats[f"board.cira.completedPrefetchesPerCore::{core}"]
            )
            self.assertGreater(issued, 0)
            self.assertEqual(completed, issued)
            first = int(
                stats[
                    f"board.cira.genericPrefetchFirstIssueTickPerCore::{core}"
                ]
            )
            last = int(
                stats[
                    f"board.cira.genericPrefetchLastCompletionTickPerCore::{core}"
                ]
            )
            busy = int(
                stats[f"board.cira.genericPrefetchBusyTicksPerCore::{core}"]
            )
            self.assertGreater(first, 0)
            self.assertGreaterEqual(last, first)
            self.assertEqual(busy, last - first)
            self.assertEqual(
                int(
                    stats[
                        f"board.cira.genericPrefetchSpanValidPerCore::{core}"
                    ]
                ),
                1,
            )


if __name__ == "__main__":
    unittest.main()
