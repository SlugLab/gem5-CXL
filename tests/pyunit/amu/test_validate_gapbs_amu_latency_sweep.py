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
            "cxl_packets,cxl_bytes,l1d_demand_misses,l2d_demand_hits,"
            "l2d_demand_misses,l2i_demand_hits,l2i_demand_misses,"
            "cira_total_latency,cira_avg_latency,run_dir"
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
                        (
                            "board.cache_hierarchy.membus.pktCount_l2.mem_side_port::"
                            "board.cxl_mem_link0.cpu_side_port 99"
                        ),
                        (
                            "board.cache_hierarchy.membus.pktSize_l2.mem_side_port::"
                            "board.cxl_mem_link0.cpu_side_port 4096"
                        ),
                        "board.cache_hierarchy.membus.pktCount::total 9999",
                        "board.cache_hierarchy.membus.pktSize::total 99999",
                        (
                            "board.cache_hierarchy.l1d-cache-0."
                            "demandMisses::total 10"
                        ),
                        (
                            "board.cache_hierarchy.l2-cache-0.demandHits::"
                            "processor.cores.core.data 11"
                        ),
                        (
                            "board.cache_hierarchy.l2-cache-0.demandMisses::"
                            "processor.cores.core.data 12"
                        ),
                        (
                            "board.cache_hierarchy.l2-cache-0.demandHits::"
                            "processor.cores.core.inst 13"
                        ),
                        (
                            "board.cache_hierarchy.l2-cache-0.demandMisses::"
                            "processor.cores.core.inst 14"
                        ),
                        (
                            "board.cache_hierarchy.l2-cache-0."
                            "demandHits::total 24"
                        ),
                        (
                            "board.cache_hierarchy.l2-cache-0."
                            "demandMisses::total 26"
                        ),
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
                            "board.cira.totalLatency 800",
                            "board.cira.avgLatency 100",
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
                            "cxl_packets": "99",
                            "cxl_bytes": "4096",
                            "l1d_demand_misses": "10",
                            "l2d_demand_hits": "11",
                            "l2d_demand_misses": "12",
                            "l2i_demand_hits": "13",
                            "l2i_demand_misses": "14",
                            "cira_total_latency": "800" if kind == "cira" else "0",
                            "cira_avg_latency": "100" if kind == "cira" else "0",
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

    def test_rejects_summary_missing_new_diagnostic_column(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_sweep(root)
            summary = root / "200ns" / "summary.csv"
            with summary.open(newline="", encoding="utf-8") as stream:
                reader = csv.DictReader(stream)
                rows = list(reader)
                fields = [field for field in reader.fieldnames if field != "cxl_bytes"]
            with summary.open("w", newline="", encoding="utf-8") as stream:
                writer = csv.DictWriter(stream, fieldnames=fields)
                writer.writeheader()
                writer.writerows(
                    {field: row[field] for field in fields} for row in rows
                )
            with self.assertRaisesRegex(
                self.validator.ValidationError, "missing columns: cxl_bytes"
            ):
                self.validator.validate_sweep(root)

    def test_rejects_missing_exact_raw_cxl_packet_stat(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_sweep(root)
            stats = root / "200ns" / "bfs" / "cxl_vanilla" / "stats.txt"
            text = stats.read_text(encoding="utf-8").replace(
                "board.cache_hierarchy.membus.pktCount_l2.mem_side_port::"
                "board.cxl_mem_link0.cpu_side_port 99\n",
                "",
            )
            stats.write_text(text, encoding="utf-8")
            with self.assertRaisesRegex(
                self.validator.ValidationError,
                "expected exactly one first-ROI statistic matching .*"
                "pktCount_.*found 0",
            ):
                self.validator.validate_sweep(root)

    def test_rejects_summary_cxl_packets_that_do_not_match_exact_stat(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_sweep(root)
            summary = root / "200ns" / "summary.csv"
            with summary.open(newline="", encoding="utf-8") as stream:
                reader = csv.DictReader(stream)
                fields = reader.fieldnames
                rows = list(reader)
            rows[0]["cxl_packets"] = "4195"
            with summary.open("w", newline="", encoding="utf-8") as stream:
                writer = csv.DictWriter(stream, fieldnames=fields)
                writer.writeheader()
                writer.writerows(rows)
            with self.assertRaisesRegex(
                self.validator.ValidationError,
                "cxl_packets=4195 != exact first-ROI packet count 99",
            ):
                self.validator.validate_sweep(root)

    def test_rejects_multiple_directional_cxl_packet_stats(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_sweep(root)
            stats = root / "200ns" / "bfs" / "cxl_vanilla" / "stats.txt"
            text = stats.read_text(encoding="utf-8").replace(
                "board.cache_hierarchy.membus.pktCount::total 9999\n",
                (
                    "board.cache_hierarchy.membus.pktCount_other::"
                    "board.cxl_mem_link0.cpu_side_port 1\n"
                    "board.cache_hierarchy.membus.pktCount::total 9999\n"
                ),
            )
            stats.write_text(text, encoding="utf-8")
            with self.assertRaisesRegex(
                self.validator.ValidationError,
                "expected exactly one first-ROI statistic matching .*"
                "pktCount_.*found 2",
            ):
                self.validator.validate_sweep(root)

    def test_rejects_packet_and_byte_directional_cell_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_sweep(root)
            stats = root / "200ns" / "bfs" / "cxl_vanilla" / "stats.txt"
            text = stats.read_text(encoding="utf-8").replace(
                "board.cache_hierarchy.membus.pktSize_l2.mem_side_port::",
                "board.cache_hierarchy.membus.pktSize_other.mem_side_port::",
                1,
            )
            stats.write_text(text, encoding="utf-8")
            with self.assertRaisesRegex(
                self.validator.ValidationError, "directional identity mismatch"
            ):
                self.validator.validate_sweep(root)

    def test_rejects_missing_exact_raw_cache_stat(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_sweep(root)
            stats = root / "200ns" / "bfs" / "cxl_vanilla" / "stats.txt"
            text = stats.read_text(encoding="utf-8").replace(
                "board.cache_hierarchy.l2-cache-0.demandMisses::"
                "processor.cores.core.data 12\n",
                "",
            ).replace(
                "board.cache_hierarchy.l2-cache-0.demandMisses::total 26\n",
                "",
            )
            stats.write_text(text, encoding="utf-8")
            with self.assertRaisesRegex(
                self.validator.ValidationError,
                "missing board.cache_hierarchy.l2-cache-0."
                "demandMisses::total",
            ):
                self.validator.validate_sweep(root)

    def test_accepts_nozero_requestor_stat_as_zero_with_family_total(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_sweep(root)
            stats = root / "200ns" / "pr" / "cxl_vanilla" / "stats.txt"
            text = stats.read_text(encoding="utf-8").replace(
                "board.cache_hierarchy.l2-cache-0.demandHits::"
                "processor.cores.core.data 11\n",
                "",
            )
            stats.write_text(text, encoding="utf-8")
            summary = root / "200ns" / "summary.csv"
            with summary.open(newline="", encoding="utf-8") as stream:
                reader = csv.DictReader(stream)
                fields = reader.fieldnames
                rows = list(reader)
            row = next(
                row
                for row in rows
                if row["benchmark"] == "pr" and row["kind"] == "baseline"
            )
            row["l2d_demand_hits"] = "0"
            with summary.open("w", newline="", encoding="utf-8") as stream:
                writer = csv.DictWriter(stream, fieldnames=fields)
                writer.writeheader()
                writer.writerows(rows)
            result = self.validator.validate_sweep(root)
            self.assertEqual(result.row_count, 48)

    def test_rejects_adjacent_large_integer_counter_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_sweep(root)
            stats = root / "200ns" / "bfs" / "cxl_vanilla" / "stats.txt"
            text = stats.read_text(encoding="utf-8").replace(
                "board.cxl_mem_link0.cpu_side_port 99\n",
                "board.cxl_mem_link0.cpu_side_port 9007199254740993\n",
                1,
            )
            stats.write_text(text, encoding="utf-8")
            summary = root / "200ns" / "summary.csv"
            with summary.open(newline="", encoding="utf-8") as stream:
                reader = csv.DictReader(stream)
                fields = reader.fieldnames
                rows = list(reader)
            rows[0]["cxl_packets"] = "9007199254740992"
            with summary.open("w", newline="", encoding="utf-8") as stream:
                writer = csv.DictWriter(stream, fieldnames=fields)
                writer.writeheader()
                writer.writerows(rows)
            with self.assertRaisesRegex(
                self.validator.ValidationError,
                "9007199254740992 != exact first-ROI packet count "
                "9007199254740993",
            ):
                self.validator.validate_sweep(root)

    def test_rejects_summary_cache_field_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_sweep(root)
            summary = root / "200ns" / "summary.csv"
            with summary.open(newline="", encoding="utf-8") as stream:
                reader = csv.DictReader(stream)
                fields = reader.fieldnames
                rows = list(reader)
            rows[0]["l2d_demand_misses"] = "999"
            with summary.open("w", newline="", encoding="utf-8") as stream:
                writer = csv.DictWriter(stream, fieldnames=fields)
                writer.writeheader()
                writer.writerows(rows)
            with self.assertRaisesRegex(
                self.validator.ValidationError,
                "l2d_demand_misses=999 != exact first-ROI value 12",
            ):
                self.validator.validate_sweep(root)

    def test_rejects_cira_missing_raw_latency_stat(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_sweep(root)
            stats = root / "200ns" / "bfs" / "cira_pgo" / "stats.txt"
            text = stats.read_text(encoding="utf-8").replace(
                "board.cira.avgLatency 100\n", ""
            )
            stats.write_text(text, encoding="utf-8")
            with self.assertRaisesRegex(
                self.validator.ValidationError,
                "missing board.cira.avgLatency",
            ):
                self.validator.validate_sweep(root)

    def test_rejects_cira_summary_latency_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_sweep(root)
            summary = root / "200ns" / "summary.csv"
            with summary.open(newline="", encoding="utf-8") as stream:
                reader = csv.DictReader(stream)
                fields = reader.fieldnames
                rows = list(reader)
            cira = next(row for row in rows if row["kind"] == "cira")
            cira["cira_avg_latency"] = "101"
            with summary.open("w", newline="", encoding="utf-8") as stream:
                writer = csv.DictWriter(stream, fieldnames=fields)
                writer.writeheader()
                writer.writerows(rows)
            with self.assertRaisesRegex(
                self.validator.ValidationError,
                "cira_avg_latency=101 != exact first-ROI value 100",
            ):
                self.validator.validate_sweep(root)

    def test_accepts_zero_or_blank_non_cira_latency_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_sweep(root)
            summary = root / "200ns" / "summary.csv"
            with summary.open(newline="", encoding="utf-8") as stream:
                reader = csv.DictReader(stream)
                fields = reader.fieldnames
                rows = list(reader)
            baseline = next(row for row in rows if row["kind"] == "baseline")
            baseline["cira_total_latency"] = ""
            baseline["cira_avg_latency"] = "0.0"
            with summary.open("w", newline="", encoding="utf-8") as stream:
                writer = csv.DictWriter(stream, fieldnames=fields)
                writer.writeheader()
                writer.writerows(rows)
            result = self.validator.validate_sweep(root)
            self.assertEqual(result.row_count, 48)

    def test_rejects_nonzero_cira_latency_on_non_cira_row(self):
        for kind in ("baseline", "amu"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                self.make_sweep(root)
                summary = root / "200ns" / "summary.csv"
                with summary.open(newline="", encoding="utf-8") as stream:
                    reader = csv.DictReader(stream)
                    fields = reader.fieldnames
                    rows = list(reader)
                row = next(row for row in rows if row["kind"] == kind)
                row["cira_total_latency"] = "999"
                with summary.open("w", newline="", encoding="utf-8") as stream:
                    writer = csv.DictWriter(stream, fieldnames=fields)
                    writer.writeheader()
                    writer.writerows(rows)
                with self.assertRaisesRegex(
                    self.validator.ValidationError,
                    "non-CIRA row must leave cira_total_latency blank or zero",
                ):
                    self.validator.validate_sweep(root)


if __name__ == "__main__":
    unittest.main()
