# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

import csv
import importlib.util
import tempfile
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[3]
VALIDATOR_PATH = REPO / "scripts" / "validate_gapbs_amu_latency_sweep.py"


def load_validator():
    spec = importlib.util.spec_from_file_location("latency_validator", VALIDATOR_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class GapbsAmuLatencySweepValidatorTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.validator = load_validator()

    def make_sweep(self, root, *, completed_prefetches=8, cira_label="cira_pgo"):
        fields = (
            "benchmark,label,kind,status,verification,sim_ticks,sim_insts,"
            "speedup_vs_cxl,asmc_loads,cira_prefetches,"
            "cira_indexed_prefetches,cira_csr_prefetches,cira_completed,"
            "cxl_packets,run_dir"
        ).split(",")
        for latency, delay in self.validator.EXPECTED_LATENCIES.items():
            rows = []
            for benchmark in self.validator.EXPECTED_BENCHMARKS:
                for label, kind in (
                    ("cxl_vanilla", "baseline"),
                    ("amu", "amu"),
                    (cira_label, "cira"),
                ):
                    run_dir = root / latency / benchmark / label
                    run_dir.mkdir(parents=True)
                    (run_dir / "config.ini").write_text(
                        "[board.cxl_mem_link0]\n"
                        f"delay={delay}\n"
                        "[next.section]\n",
                        encoding="utf-8",
                    )
                    stats = [
                        "---------- Begin Simulation Statistics ----------",
                        "simTicks 100",
                    ]
                    if kind == "amu":
                        stats += [
                            "board.asmc.issuedLoads 7",
                            "board.asmc.completedLoads 7",
                        ]
                    elif kind == "cira":
                        stats += [
                            "board.cira.issuedPrefetches 8",
                            "board.cira.issuedIndexedPrefetches 1",
                            "board.cira.issuedCsrPrefetches 0",
                            f"board.cira.completedPrefetches {completed_prefetches}",
                        ]
                    stats += [
                        "---------- End Simulation Statistics   ----------",
                        "---------- Begin Simulation Statistics ----------",
                        "board.cira.issuedPrefetches 999",
                        "---------- End Simulation Statistics   ----------",
                    ]
                    (run_dir / "stats.txt").write_text(
                        "\n".join(stats) + "\n", encoding="utf-8"
                    )
                    rows.append(
                        {
                            "benchmark": benchmark,
                            "label": label,
                            "kind": kind,
                            "status": "ok",
                            "verification": "pass",
                            "sim_ticks": "100",
                            "sim_insts": "10",
                            "speedup_vs_cxl": "1.0",
                            "asmc_loads": "7" if kind == "amu" else "0",
                            "cira_prefetches": "8" if kind == "cira" else "0",
                            "cira_indexed_prefetches": "1" if kind == "cira" else "0",
                            "cira_csr_prefetches": "0",
                            "cira_completed": "8" if kind == "cira" else "0",
                            "cxl_packets": "0",
                            "run_dir": str(run_dir),
                        }
                    )
            with (root / latency / "summary.csv").open("w", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=fields)
                writer.writeheader()
                writer.writerows(rows)

    def test_accepts_balanced_leaf_requests_and_nonzero_descriptors(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_sweep(root)
            result = self.validator.validate_sweep(root)
        self.assertEqual(result.row_count, 48)
        self.assertEqual(result.cira_rows, 16)

    def test_rejects_unbalanced_leaf_requests_even_when_mixed_sum_matches(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_sweep(root, completed_prefetches=9)
            with self.assertRaisesRegex(
                self.validator.ValidationError,
                "issuedPrefetches=8 != completedPrefetches=9",
            ):
                self.validator.validate_sweep(root)

    def test_rejects_non_pgo_cira_label(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_sweep(root, cira_label="cira_not_pgo")
            with self.assertRaisesRegex(
                self.validator.ValidationError,
                "exact cxl_vanilla/baseline, amu/amu, and cira_pgo/cira rows",
            ):
                self.validator.validate_sweep(root)

    def test_rejects_truncated_first_roi_stats_section(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_sweep(root)
            stats = root / "200ns" / "bfs" / "cxl_vanilla" / "stats.txt"
            stats.write_text(
                "---------- Begin Simulation Statistics ----------\n"
                "simTicks 100\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                self.validator.ValidationError,
                "missing End marker for first ROI stats section",
            ):
                self.validator.validate_sweep(root)


if __name__ == "__main__":
    unittest.main()
