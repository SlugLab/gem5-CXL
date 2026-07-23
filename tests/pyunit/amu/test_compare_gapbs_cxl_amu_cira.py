# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

import csv
import importlib.util
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path


REPO = Path(__file__).resolve().parents[3]
RUNNER_PATH = REPO / "scripts" / "compare_gapbs_cxl_amu_cira.py"


def load_runner():
    spec = importlib.util.spec_from_file_location("gapbs_runner", RUNNER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class GapbsAmuCiraMetricTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.runner = load_runner()

    def stats(self):
        return {
            (
                "board.cache_hierarchy.membus.pktCount_l2.mem_side_port::"
                "board.cxl_mem_link0.cpu_side_port"
            ): Decimal(137),
            (
                "board.cache_hierarchy.membus.pktSize_l2.mem_side_port::"
                "board.cxl_mem_link0.cpu_side_port"
            ): Decimal(2880),
            (
                "board.cache_hierarchy.membus.pktCount::"
                "board.cxl_mem_link0.cpu_side_port"
            ): Decimal(777),
            (
                "board.cache_hierarchy.membus.pktSize::"
                "board.cxl_mem_link0.cpu_side_port"
            ): Decimal(888),
            "board.cache_hierarchy.membus.pktCount::total": 9999.0,
            "board.cache_hierarchy.membus.pktSize::total": 99999.0,
            # These used to be accidentally added to one mixed-unit value.
            "board.cxl_mem_link0.cpu_side_port.pktCount": Decimal(406),
            "board.cxl_mem_link0.cpu_side_port.pktSize": Decimal(8576),
            "board.cache_hierarchy.l1d-cache-0.demandMisses::total": 11.0,
            (
                "board.cache_hierarchy.l2-cache-0.demandHits::"
                "processor.cores.core.data"
            ): 12.0,
            (
                "board.cache_hierarchy.l2-cache-0.demandMisses::"
                "processor.cores.core.data"
            ): 13.0,
            (
                "board.cache_hierarchy.l2-cache-0.demandHits::"
                "processor.cores.core.inst"
            ): 14.0,
            (
                "board.cache_hierarchy.l2-cache-0.demandMisses::"
                "processor.cores.core.inst"
            ): 15.0,
            (
                "board.cache_hierarchy.l2-cache-0.demandHits::"
                "processor.switch.core.data"
            ): 112.0,
            (
                "board.cache_hierarchy.l2-cache-0.demandMisses::"
                "processor.switch.core.data"
            ): 113.0,
            (
                "board.cache_hierarchy.l2-cache-0.demandHits::"
                "processor.switch.core.inst"
            ): 114.0,
            (
                "board.cache_hierarchy.l2-cache-0.demandMisses::"
                "processor.switch.core.inst"
            ): 115.0,
            "board.cache_hierarchy.l2-cache-0.demandHits::total": 26.0,
            "board.cache_hierarchy.l2-cache-0.demandMisses::total": 28.0,
            "board.cira.totalLatency": 1600.0,
            "board.cira.avgLatency": 100.0,
        }

    def test_extracts_exact_first_roi_metrics_without_mixed_unit_sums(self):
        metrics = self.runner.extract_diagnostic_metrics(self.stats(), "cira")
        self.assertEqual(
            metrics,
            {
                "cxl_packets": Decimal(137),
                "cxl_bytes": Decimal(2880),
                "l1d_demand_misses": 11.0,
                "l2d_demand_hits": 12.0,
                "l2d_demand_misses": 13.0,
                "l2i_demand_hits": 14.0,
                "l2i_demand_misses": 15.0,
                "cira_total_latency": 1600.0,
                "cira_avg_latency": 100.0,
            },
        )

    def test_fast_forward_selects_only_timing_switch_requestor(self):
        metrics = self.runner.extract_diagnostic_metrics(
            self.stats(), "baseline", fast_forward=True
        )
        self.assertEqual(metrics["l2d_demand_hits"], Decimal(112))
        self.assertEqual(metrics["l2d_demand_misses"], Decimal(113))
        self.assertEqual(metrics["l2i_demand_hits"], Decimal(114))
        self.assertEqual(metrics["l2i_demand_misses"], Decimal(115))

    def test_fast_forward_rejects_ambiguous_timing_switch_requestors(self):
        stats = self.stats()
        stats[
            "board.cache_hierarchy.l2-cache-0.demandHits::"
            "processor.switch.core.data.extra"
        ] = Decimal(1)
        with self.assertRaisesRegex(
            self.runner.StatsError, "ambiguous timing switch requestor"
        ):
            self.runner.extract_diagnostic_metrics(
                stats, "baseline", fast_forward=True
            )

    def test_fast_forward_nozero_is_zero_only_when_family_total_exists(self):
        stats = self.stats()
        del stats[
            "board.cache_hierarchy.l2-cache-0.demandHits::"
            "processor.switch.core.data"
        ]
        metrics = self.runner.extract_diagnostic_metrics(
            stats, "baseline", fast_forward=True
        )
        self.assertEqual(metrics["l2d_demand_hits"], 0)
        del stats[
            "board.cache_hierarchy.l2-cache-0.demandHits::total"
        ]
        with self.assertRaisesRegex(
            self.runner.StatsError, "missing required ROI statistic"
        ):
            self.runner.extract_diagnostic_metrics(
                stats, "baseline", fast_forward=True
            )

    def test_extracts_owner_evidence_and_blanks_nonowners(self):
        stats = self.stats()
        stats.update(
            {
                "board.asmc.issuedLoads": Decimal(7),
                "board.asmc.completedLoads": Decimal(7),
                "board.cira.issuedPrefetches": Decimal(8),
                "board.cira.completedPrefetches": Decimal(8),
                "board.cira.issuedIndexedPrefetches": Decimal(3),
                "board.cira.issuedCsrPrefetches": Decimal(5),
                "board.cira.usefulPrefetches": Decimal(4),
                "board.cira.latePrefetches": Decimal(0),
                "board.cira.readPackets": Decimal(16),
                "board.cira.readBytes": Decimal(1024),
            }
        )
        amu = self.runner.extract_owned_metrics(stats, "amu")
        self.assertEqual(amu["asmc_loads"], Decimal(7))
        self.assertEqual(amu["asmc_completed"], Decimal(7))
        self.assertEqual(amu["cira_prefetches"], 0)
        cira = self.runner.extract_owned_metrics(stats, "cira")
        self.assertEqual(cira["asmc_loads"], 0)
        self.assertEqual(cira["cira_prefetches"], Decimal(8))
        self.assertEqual(cira["cira_completed"], Decimal(8))
        self.assertEqual(cira["cira_indexed_prefetches"], Decimal(3))
        self.assertEqual(cira["cira_csr_prefetches"], Decimal(5))
        self.assertEqual(cira["cira_useful"], Decimal(4))
        self.assertEqual(cira["cira_late"], Decimal(0))
        self.assertEqual(cira["cira_read_packets"], Decimal(16))
        self.assertEqual(cira["cira_read_bytes"], Decimal(1024))
        baseline = self.runner.extract_owned_metrics(stats, "baseline")
        self.assertTrue(all(value == 0 for value in baseline.values()))

    def test_cira_requires_useful_late_and_read_evidence(self):
        stats = self.stats()
        stats.update(
            {
                "board.cira.issuedPrefetches": Decimal(8),
                "board.cira.completedPrefetches": Decimal(8),
                "board.cira.issuedIndexedPrefetches": Decimal(3),
                "board.cira.issuedCsrPrefetches": Decimal(5),
                "board.cira.usefulPrefetches": Decimal(4),
                "board.cira.latePrefetches": Decimal(0),
                "board.cira.readPackets": Decimal(16),
                "board.cira.readBytes": Decimal(1024),
            }
        )
        for name in (
            "board.cira.usefulPrefetches",
            "board.cira.latePrefetches",
            "board.cira.readPackets",
            "board.cira.readBytes",
        ):
            with self.subTest(name=name):
                candidate = dict(stats)
                del candidate[name]
                with self.assertRaisesRegex(
                    self.runner.StatsError, f"missing required ROI statistic: {name}"
                ):
                    self.runner.extract_owned_metrics(candidate, "cira")

    def test_counts_exact_cpu_switch_markers(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "gem5.log"
            log.write_text(
                "Switching from fast-forward CPU to timing CPU!\n"
                "not Switching from fast-forward CPU to timing CPU!\n",
                encoding="utf-8",
            )
            self.assertEqual(self.runner.parse_cpu_switches(log), 1)

    def test_rejects_missing_exact_cxl_packet_or_byte_stat(self):
        for key in (
            (
                "board.cache_hierarchy.membus.pktCount_l2.mem_side_port::"
                "board.cxl_mem_link0.cpu_side_port"
            ),
            (
                "board.cache_hierarchy.membus.pktSize_l2.mem_side_port::"
                "board.cxl_mem_link0.cpu_side_port"
            ),
        ):
            with self.subTest(key=key):
                stats = self.stats()
                del stats[key]
                with self.assertRaisesRegex(
                    self.runner.StatsError, "expected exactly one ROI statistic"
                ):
                    self.runner.extract_diagnostic_metrics(stats, "baseline")

    def test_rejects_multiple_directional_packet_candidates(self):
        stats = self.stats()
        stats[
            "board.cache_hierarchy.membus.pktCount_other.mem_side_port::"
            "board.cxl_mem_link0.cpu_side_port"
        ] = 1.0
        with self.assertRaisesRegex(
            self.runner.StatsError, "expected exactly one ROI statistic.*found 2"
        ):
            self.runner.extract_diagnostic_metrics(stats, "baseline")

    def test_rejects_packet_and_byte_directional_cell_mismatch(self):
        stats = self.stats()
        old_key = (
            "board.cache_hierarchy.membus.pktSize_l2.mem_side_port::"
            "board.cxl_mem_link0.cpu_side_port"
        )
        value = stats.pop(old_key)
        stats[
            "board.cache_hierarchy.membus.pktSize_other.mem_side_port::"
            "board.cxl_mem_link0.cpu_side_port"
        ] = value
        with self.assertRaisesRegex(
            self.runner.StatsError, "directional identity mismatch"
        ):
            self.runner.extract_diagnostic_metrics(stats, "baseline")

    def test_large_integer_counter_survives_stats_to_summary_exactly(self):
        stats = self.stats()
        packet_key = (
            "board.cache_hierarchy.membus.pktCount_l2.mem_side_port::"
            "board.cxl_mem_link0.cpu_side_port"
        )
        stats[packet_key] = 9007199254740993
        stats["simTicks"] = 100
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stats_path = root / "stats.txt"
            stats_path.write_text(
                "---------- Begin Simulation Statistics ----------\n"
                + "\n".join(f"{key} {value}" for key, value in stats.items())
                + "\n---------- End Simulation Statistics   ----------\n",
                encoding="utf-8",
            )
            parsed = self.runner.parse_stats(stats_path)
            row = {
                "benchmark": "bc",
                "label": "cxl_vanilla",
                "kind": "baseline",
                "status": "ok",
                "verification": "pass",
                "sim_ticks": parsed["simTicks"],
                **self.runner.extract_diagnostic_metrics(parsed, "baseline"),
            }
            summary = root / "summary.csv"
            self.runner.write_summary(summary, [row])
            with summary.open(newline="", encoding="utf-8") as stream:
                written = next(csv.DictReader(stream))
        self.assertEqual(written["cxl_packets"], "9007199254740993")

    def test_rejects_missing_exact_cache_stat(self):
        for field, key in self.runner.DIAGNOSTIC_STATS.items():
            with self.subTest(field=field):
                stats = self.stats()
                del stats[key]
                family = self.runner.DIAGNOSTIC_FAMILY_TOTALS[field]
                if family in stats:
                    del stats[family]
                with self.assertRaisesRegex(
                    self.runner.StatsError,
                    "missing required ROI statistic",
                ):
                    self.runner.extract_diagnostic_metrics(stats, "baseline")

    def test_nozero_cache_requestor_stat_is_zero_when_family_total_exists(self):
        stats = self.stats()
        key = self.runner.DIAGNOSTIC_STATS["l2d_demand_hits"]
        del stats[key]
        metrics = self.runner.extract_diagnostic_metrics(stats, "baseline")
        self.assertEqual(metrics["l2d_demand_hits"], 0)

    def test_baseline_may_omit_cira_latency_stats(self):
        stats = self.stats()
        del stats["board.cira.totalLatency"]
        del stats["board.cira.avgLatency"]
        metrics = self.runner.extract_diagnostic_metrics(stats, "baseline")
        self.assertEqual(metrics["cira_total_latency"], 0)
        self.assertEqual(metrics["cira_avg_latency"], 0)

    def test_cira_rejects_missing_latency_stats(self):
        for key in ("board.cira.totalLatency", "board.cira.avgLatency"):
            with self.subTest(key=key):
                stats = self.stats()
                del stats[key]
                with self.assertRaisesRegex(
                    self.runner.StatsError,
                    f"missing required ROI statistic: {key}",
                ):
                    self.runner.extract_diagnostic_metrics(stats, "cira")

    def test_summary_reports_every_diagnostic_column(self):
        row = {
            "benchmark": "bc",
            "label": "cxl_vanilla",
            "kind": "baseline",
            "status": "ok",
            "verification": "pass",
            "sim_ticks": 100,
            "sim_insts": 10,
            **self.runner.extract_diagnostic_metrics(self.stats(), "baseline"),
        }
        with tempfile.TemporaryDirectory() as tmp:
            summary = Path(tmp) / "summary.csv"
            self.runner.write_summary(summary, [row])
            with summary.open(newline="", encoding="utf-8") as stream:
                written = next(csv.DictReader(stream))
        for field, value in self.runner.extract_diagnostic_metrics(
            self.stats(), "baseline"
        ).items():
            self.assertEqual(float(written[field]), value)

    def test_summary_schema_contains_canonical_g20_evidence(self):
        expected = {
            "scale",
            "iterations",
            "measured_trial",
            "fast_forward_cpu",
            "roi_cpu",
            "cpu_switches",
            "cxl_link_delay",
            "all_memory_cxl",
            "asmc_loads",
            "asmc_completed",
            "cira_prefetches",
            "cira_completed",
            "cira_indexed_prefetches",
            "cira_csr_prefetches",
            "cira_useful",
            "cira_late",
            "cira_read_packets",
            "cira_read_bytes",
            "cxl_packets",
            "cxl_bytes",
            "l1d_demand_misses",
            "l2d_demand_hits",
            "l2d_demand_misses",
            "l2i_demand_hits",
            "l2i_demand_misses",
            "cira_total_latency",
            "cira_avg_latency",
        }
        self.assertTrue(expected <= set(self.runner.SUMMARY_FIELDS))


if __name__ == "__main__":
    unittest.main()
