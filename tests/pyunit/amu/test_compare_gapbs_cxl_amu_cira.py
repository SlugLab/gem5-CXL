# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

import csv
import importlib.util
import inspect
import subprocess
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


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

    def fast_forward_stats(self):
        stats = self.stats()
        for name in tuple(stats):
            if "processor.cores.core." in name:
                del stats[name]
        stats[
            "board.cache_hierarchy.l1d-cache-0.demandMisses::"
            "processor.switch.core.data"
        ] = stats[
            "board.cache_hierarchy.l1d-cache-0.demandMisses::total"
        ]
        stats[
            "board.cache_hierarchy.l1d-cache-0.demandHits::"
            "processor.switch.core.data"
        ] = Decimal(1)
        stats[
            "board.cache_hierarchy.l1d-cache-0.demandHits::total"
        ] = Decimal(1)
        stats[
            "board.cache_hierarchy.l1d-cache-0.demandAccesses::"
            "processor.switch.core.data"
        ] = Decimal(12)
        stats[
            "board.cache_hierarchy.l1d-cache-0.demandAccesses::total"
        ] = Decimal(12)
        stats[
            "board.cache_hierarchy.l2-cache-0.demandHits::total"
        ] = Decimal(226)
        stats[
            "board.cache_hierarchy.l2-cache-0.demandMisses::total"
        ] = Decimal(228)
        stats[
            "board.cache_hierarchy.l2-cache-0.demandAccesses::"
            "processor.switch.core.data"
        ] = Decimal(225)
        stats[
            "board.cache_hierarchy.l2-cache-0.demandAccesses::"
            "processor.switch.core.inst"
        ] = Decimal(229)
        stats[
            "board.cache_hierarchy.l2-cache-0.demandAccesses::total"
        ] = Decimal(454)
        return stats

    def two_core_stats(self):
        stats = {
            (
                "board.cache_hierarchy.membus.pktCount_"
                "board.cache_hierarchy.l2-cache-0.mem_side_port::"
                "board.cxl_mem_link0.cpu_side_port"
            ): Decimal(30),
            (
                "board.cache_hierarchy.membus.pktCount_"
                "board.cache_hierarchy.l2-cache-1.mem_side_port::"
                "board.cxl_mem_link0.cpu_side_port"
            ): Decimal(40),
            (
                "board.cache_hierarchy.membus.pktSize_"
                "board.cache_hierarchy.l2-cache-0.mem_side_port::"
                "board.cxl_mem_link0.cpu_side_port"
            ): Decimal(64),
            (
                "board.cache_hierarchy.membus.pktSize_"
                "board.cache_hierarchy.l2-cache-1.mem_side_port::"
                "board.cxl_mem_link0.cpu_side_port"
            ): Decimal(128),
        }
        for core, hits, misses in ((0, 10, 2), (1, 20, 3)):
            root = f"board.cache_hierarchy.l1d-cache-{core}"
            requestor = f"processor.cores{core}.core.data"
            stats[f"{root}.demandHits::{requestor}"] = Decimal(hits)
            stats[f"{root}.demandHits::total"] = Decimal(hits)
            stats[f"{root}.demandMisses::{requestor}"] = Decimal(misses)
            stats[f"{root}.demandMisses::total"] = Decimal(misses)
            stats[f"{root}.demandAccesses::{requestor}"] = Decimal(
                hits + misses
            )
            stats[f"{root}.demandAccesses::total"] = Decimal(hits + misses)

        l2_cells = {
            0: {
                "data": {"accesses": 11, "hits": None, "misses": 11},
                "inst": {"accesses": 7, "hits": 6, "misses": 1},
            },
            1: {
                "data": {"accesses": 13, "hits": 2, "misses": 11},
                "inst": {"accesses": 5, "hits": None, "misses": 5},
                "prefetcher": {"accesses": 4, "hits": 1, "misses": 3},
            },
        }
        for core, requestors in l2_cells.items():
            root = f"board.cache_hierarchy.l2-cache-{core}"
            totals = {
                "demandAccesses": 0,
                "demandHits": 0,
                "demandMisses": 0,
            }
            for role, values in requestors.items():
                requestor = (
                    f"processor.cores{core}.core.{role}"
                    if role != "prefetcher"
                    else f"cache_hierarchy.l1d-cache-{core}.prefetcher"
                )
                for family, key in (
                    ("demandAccesses", "accesses"),
                    ("demandHits", "hits"),
                    ("demandMisses", "misses"),
                ):
                    value = values[key]
                    if value is not None:
                        stats[f"{root}.{family}::{requestor}"] = Decimal(value)
                        totals[family] += value
            for family, value in totals.items():
                stats[f"{root}.{family}::total"] = Decimal(value)
        return stats

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
            self.fast_forward_stats(), "baseline", fast_forward=True
        )
        self.assertEqual(metrics["l1d_demand_misses"], Decimal(11))
        self.assertEqual(metrics["l2d_demand_hits"], Decimal(112))
        self.assertEqual(metrics["l2d_demand_misses"], Decimal(113))
        self.assertEqual(metrics["l2i_demand_hits"], Decimal(114))
        self.assertEqual(metrics["l2i_demand_misses"], Decimal(115))

    def test_fast_forward_rejects_ambiguous_timing_switch_requestors(self):
        stats = self.fast_forward_stats()
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

    def test_fast_forward_rejects_legacy_requestor(self):
        stats = self.fast_forward_stats()
        del stats[
            "board.cache_hierarchy.l2-cache-0.demandMisses::"
            "processor.switch.core.data"
        ]
        stats[
            "board.cache_hierarchy.l2-cache-0.demandMisses::"
            "processor.cores.core.data"
        ] = Decimal(13)
        with self.assertRaisesRegex(
            self.runner.StatsError, "legacy cache requestor"
        ):
            self.runner.extract_diagnostic_metrics(
                stats, "baseline", fast_forward=True
            )

    def test_fast_forward_rejects_unknown_processor_requestor(self):
        stats = self.fast_forward_stats()
        del stats[
            "board.cache_hierarchy.l2-cache-0.demandMisses::"
            "processor.switch.core.data"
        ]
        stats[
            "board.cache_hierarchy.l2-cache-0.demandMisses::"
            "procesor.swotch.core.data"
        ] = Decimal(13)
        with self.assertRaisesRegex(
            self.runner.StatsError, "unknown cache requestor"
        ):
            self.runner.extract_diagnostic_metrics(
                stats, "baseline", fast_forward=True
            )

    def test_fast_forward_nozero_cell_is_zero_when_family_total_exists(self):
        stats = self.fast_forward_stats()
        del stats[
            "board.cache_hierarchy.l2-cache-0.demandHits::"
            "processor.switch.core.data"
        ]
        stats[
            "board.cache_hierarchy.l2-cache-0.demandHits::total"
        ] = Decimal(114)
        stats[
            "board.cache_hierarchy.l2-cache-0.demandAccesses::"
            "processor.switch.core.data"
        ] = Decimal(113)
        stats[
            "board.cache_hierarchy.l2-cache-0.demandAccesses::total"
        ] = Decimal(342)
        metrics = self.runner.extract_diagnostic_metrics(
            stats, "baseline", fast_forward=True
        )
        self.assertEqual(metrics["l2d_demand_hits"], 0)

    def test_fast_forward_rejects_missing_nonzero_cell_with_family_total(self):
        stats = self.fast_forward_stats()
        del stats[
            "board.cache_hierarchy.l2-cache-0.demandHits::"
            "processor.switch.core.data"
        ]
        with self.assertRaisesRegex(
            self.runner.StatsError, "family total"
        ):
            self.runner.extract_diagnostic_metrics(
                stats, "baseline", fast_forward=True
            )

    def test_fast_forward_rejects_deleted_nonzero_demand_family(self):
        stats = self.fast_forward_stats()
        for name in (
            "board.cache_hierarchy.l2-cache-0.demandMisses::"
            "processor.switch.core.data",
            "board.cache_hierarchy.l2-cache-0.demandMisses::"
            "processor.switch.core.inst",
            "board.cache_hierarchy.l2-cache-0.demandMisses::total",
        ):
            del stats[name]
        with self.assertRaisesRegex(
            self.runner.StatsError, "omitted nonzero demandMisses"
        ):
            self.runner.cache_diagnostic(
                stats, "l2d_demand_misses", fast_forward=True
            )

    def test_real_nozero_shape_accepts_wholly_omitted_zero_families(self):
        # Minimal first-ROI excerpt from the real smoke stats. The omitted
        # cells/families are proven zero only by demandAccesses=hits+misses.
        stats = {
            (
                "board.cache_hierarchy.l1d-cache-0.demandHits::"
                "processor.switch.core.data"
            ): Decimal(8834),
            (
                "board.cache_hierarchy.l1d-cache-0.demandHits::total"
            ): Decimal(8834),
            (
                "board.cache_hierarchy.l1d-cache-0.demandAccesses::"
                "processor.switch.core.data"
            ): Decimal(8834),
            (
                "board.cache_hierarchy.l1d-cache-0.demandAccesses::total"
            ): Decimal(8834),
            (
                "board.cache_hierarchy.l2-cache-0.demandHits::"
                "processor.switch.core.inst"
            ): Decimal(57),
            (
                "board.cache_hierarchy.l2-cache-0.demandHits::total"
            ): Decimal(57),
            (
                "board.cache_hierarchy.l2-cache-0.demandAccesses::"
                "processor.switch.core.inst"
            ): Decimal(57),
            (
                "board.cache_hierarchy.l2-cache-0.demandAccesses::total"
            ): Decimal(57),
        }
        self.assertEqual(
            {
                field: self.runner.cache_diagnostic(
                    stats, field, fast_forward=True
                )
                for field in self.runner.DIAGNOSTIC_STATS
            },
            {
                "l1d_demand_misses": 0,
                "l2d_demand_hits": 0,
                "l2d_demand_misses": 0,
                "l2i_demand_hits": Decimal(57),
                "l2i_demand_misses": 0,
            },
        )

    def test_wholly_missing_family_rejects_absent_requestor_identity(self):
        stats = {
            (
                "board.cache_hierarchy.l2-cache-0.replacements"
            ): Decimal(0)
        }
        with self.assertRaisesRegex(
            self.runner.StatsError, "missing exact switch requestor identity"
        ):
            self.runner.cache_diagnostic(
                stats, "l2d_demand_misses", fast_forward=True
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

    def test_two_core_cira_evidence_rejects_inactive_and_dropped_work(self):
        stats = {
            "board.cira.issuedPrefetchesPerCore::0": Decimal(100),
            "board.cira.issuedPrefetchesPerCore::1": Decimal(120),
            "board.cira.completedPrefetchesPerCore::0": Decimal(100),
            "board.cira.completedPrefetchesPerCore::1": Decimal(120),
            "board.cira.usefulPrefetchesPerCore::0": Decimal(10),
            "board.cira.usefulPrefetchesPerCore::1": Decimal(12),
            "board.cira.issuedCsrPrefetchesPerCore::0": Decimal(5),
            "board.cira.issuedCsrPrefetchesPerCore::1": Decimal(6),
            "board.cira.coalescedPrefetches": Decimal(500),
            "board.cira.rejectedQueueFull": Decimal(0),
            "board.cira.droppedCsrDescriptors": Decimal(0),
            "board.cira.csrQueueHighWatermark": Decimal(8),
        }
        evidence = self.runner.extract_cira_evidence(stats, "cira", 2)
        self.assertEqual(
            self.runner.cira_evidence_failure(evidence, 2), None
        )
        self.assertEqual(evidence["cira_issued_per_core"], "100;120")
        self.assertEqual(evidence["cira_completed_per_core"], "100;120")
        self.assertEqual(evidence["cira_useful_per_core"], "10;12")
        self.assertEqual(evidence["cira_csr_per_core"], "5;6")
        self.assertEqual(evidence["cira_coalesced"], Decimal(500))
        self.assertEqual(evidence["cira_csr_queue_high_watermark"], Decimal(8))

        for field in ("cira_csr_per_core",):
            with self.subTest(field=field):
                candidate = dict(evidence)
                candidate[field] = "100;0"
                self.assertEqual(
                    self.runner.cira_evidence_failure(candidate, 2),
                    "inactive-cira-core",
                )

        candidate = dict(evidence)
        candidate["cira_issued_per_core"] = "100;0"
        candidate["cira_completed_per_core"] = "100;0"
        self.assertIsNone(
            self.runner.cira_evidence_failure(candidate, 2)
        )

        for field in (
            "cira_rejected_queue_full",
            "cira_dropped_csr_descriptors",
        ):
            with self.subTest(field=field):
                candidate = dict(evidence)
                candidate[field] = Decimal(1)
                self.assertEqual(
                    self.runner.cira_evidence_failure(candidate, 2),
                    "cira-rejected-work",
                )

    def test_cira_forwards_bounded_scheduler_parameters(self):
        args = SimpleNamespace(
            cira_max_outstanding=256,
            cira_max_send_queue=1024,
            cira_max_csr_walk_queue=4096,
            cira_csr_lines_per_turn=64,
            cira_max_completed_lines=65536,
            cira_issue_latency="1ns",
            cira_completion_latency="0ns",
            env=[],
        )
        cmd = []
        self.runner.append_kind_args(cmd, args, "cira")
        for option, value in (
            ("--cira-max-csr-walk-queue", "4096"),
            ("--cira-csr-lines-per-turn", "64"),
            ("--cira-max-completed-lines", "65536"),
        ):
            index = cmd.index(option)
            self.assertEqual(cmd[index + 1], value)

        parameters = self.runner.checkpoint_model_parameters(args, "cira")
        self.assertEqual(parameters["cira_max_csr_walk_queue"], 4096)
        self.assertEqual(parameters["cira_csr_lines_per_turn"], 64)
        self.assertEqual(parameters["cira_max_completed_lines"], 65536)

    def test_counts_exact_cpu_switch_markers(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "gem5.log"
            log.write_text(
                "Switching from fast-forward CPU to timing CPU!\n"
                "not Switching from fast-forward CPU to timing CPU!\n",
                encoding="utf-8",
            )
            self.assertEqual(self.runner.parse_cpu_switches(log), 1)

    def test_parse_stats_rejects_incomplete_or_empty_first_section(self):
        cases = (
            ("missing Begin marker", "simTicks 100\n"),
            (
                "missing End marker",
                "---------- Begin Simulation Statistics ----------\n"
                "simTicks 100\n",
            ),
            (
                "missing Begin marker",
                "---------- End Simulation Statistics   ----------\n",
            ),
            (
                "empty first ROI stats section",
                "---------- Begin Simulation Statistics ----------\n"
                "---------- End Simulation Statistics   ----------\n",
            ),
        )
        for message, content in cases:
            with (
                self.subTest(message=message),
                tempfile.TemporaryDirectory() as tmp,
            ):
                path = Path(tmp) / "stats.txt"
                path.write_text(content, encoding="utf-8")
                with self.assertRaisesRegex(
                    self.runner.StatsError, message
                ):
                    self.runner.parse_stats(path)

    def test_parse_stats_rejects_missing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "missing-stats.txt"
            with self.assertRaisesRegex(
                self.runner.StatsError, "missing stats file"
            ):
                self.runner.parse_stats(path)

    def test_summary_schema_has_no_duplicate_fields(self):
        self.assertEqual(
            len(self.runner.SUMMARY_FIELDS),
            len(set(self.runner.SUMMARY_FIELDS)),
        )

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

    def test_two_core_metrics_aggregate_exact_core_owned_cells(self):
        metrics = self.runner.extract_diagnostic_metrics(
            self.two_core_stats(), "baseline", num_cores=2
        )
        self.assertEqual(metrics["cxl_packets"], Decimal(70))
        self.assertEqual(metrics["cxl_bytes"], Decimal(192))
        self.assertEqual(metrics["l1d_demand_misses"], Decimal(5))
        self.assertEqual(metrics["l2d_demand_hits"], Decimal(2))
        self.assertEqual(metrics["l2d_demand_misses"], Decimal(22))
        self.assertEqual(metrics["l2i_demand_hits"], Decimal(6))
        self.assertEqual(metrics["l2i_demand_misses"], Decimal(6))

    def test_run_one_forwards_the_configured_core_count(self):
        source = inspect.getsource(self.runner.run_one)
        self.assertIn("num_cores=args.cores", source)

    def test_two_core_rejects_missing_directional_core_cell(self):
        stats = self.two_core_stats()
        del stats[
            "board.cache_hierarchy.membus.pktCount_"
            "board.cache_hierarchy.l2-cache-1.mem_side_port::"
            "board.cxl_mem_link0.cpu_side_port"
        ]
        with self.assertRaisesRegex(
            self.runner.StatsError, "expected directional CXL cells"
        ):
            self.runner.extract_diagnostic_metrics(
                stats, "baseline", num_cores=2
            )

    def test_two_core_nozero_byte_cell_contributes_zero(self):
        stats = self.two_core_stats()
        del stats[
            "board.cache_hierarchy.membus.pktSize_"
            "board.cache_hierarchy.l2-cache-1.mem_side_port::"
            "board.cxl_mem_link0.cpu_side_port"
        ]
        metrics = self.runner.extract_diagnostic_metrics(
            stats, "baseline", num_cores=2
        )
        self.assertEqual(metrics["cxl_packets"], Decimal(70))
        self.assertEqual(metrics["cxl_bytes"], Decimal(64))

    def test_two_core_rejects_missing_requestor_identity(self):
        stats = self.two_core_stats()
        del stats[
            "board.cache_hierarchy.l2-cache-1.demandAccesses::"
            "processor.cores1.core.inst"
        ]
        with self.assertRaisesRegex(
            self.runner.StatsError, "missing exact core requestor identity"
        ):
            self.runner.extract_diagnostic_metrics(
                stats, "baseline", num_cores=2
            )

    def test_two_core_accepts_wholly_omitted_zero_requestor(self):
        stats = self.two_core_stats()
        for family, value in (
            ("demandAccesses", 5),
            ("demandHits", None),
            ("demandMisses", 5),
        ):
            if value is not None:
                del stats[
                    f"board.cache_hierarchy.l2-cache-1.{family}::"
                    "processor.cores1.core.inst"
                ]
            total = (
                "board.cache_hierarchy.l2-cache-1."
                f"{family}::total"
            )
            stats[total] -= Decimal(value or 0)
        metrics = self.runner.extract_diagnostic_metrics(
            stats, "baseline", num_cores=2
        )
        self.assertEqual(metrics["l2i_demand_hits"], Decimal(6))
        self.assertEqual(metrics["l2i_demand_misses"], Decimal(1))

    def test_two_core_amu_includes_exact_asmc_io_directional_cell(self):
        stats = self.two_core_stats()
        stats[
            "board.cache_hierarchy.membus.pktCount_"
            "board.asmc_io_cache.mem_side::"
            "board.cxl_mem_link0.cpu_side_port"
        ] = Decimal(9)
        stats[
            "board.cache_hierarchy.membus.pktSize_"
            "board.asmc_io_cache.mem_side::"
            "board.cxl_mem_link0.cpu_side_port"
        ] = Decimal(256)
        metrics = self.runner.extract_diagnostic_metrics(
            stats, "amu", num_cores=2
        )
        self.assertEqual(metrics["cxl_packets"], Decimal(79))
        self.assertEqual(metrics["cxl_bytes"], Decimal(448))

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

    def test_formal_g4_checkpoint_profile_accepts_four_threads(self):
        args = SimpleNamespace(
            profile="g4-4thread-sweep",
            smoke_test=False,
            graph_scale=4,
            cores=4,
            iterations=2,
            measure_trial=1,
            cxl_link_delay="500ns",
            env=["OMP_NUM_THREADS=4"],
        )

        profile = self.runner.validate_checkpoint_profile(args)

        self.assertEqual(profile.name, "g4-4thread-sweep")

    def test_checkpoint_restore_timeout_returns_failure_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binary_dir = root / "bin"
            binary_dir.mkdir()
            binary = binary_dir / "pr"
            binary.write_bytes(b"binary")
            graph = root / "g20.sg"
            graph.write_bytes(b"graph")
            checkpoint = root / "checkpoint"
            checkpoint.mkdir()
            manifest = checkpoint / "manifest.json"
            manifest.write_text("{}\n", encoding="utf-8")
            args = SimpleNamespace(
                outdir=root / "out",
                graph=graph,
                graph_scale=20,
                cores=2,
                iterations=2,
                measure_trial=1,
                cxl_link_delay="1us",
                timeout=1,
                verify=True,
                dry_run=False,
                env=[],
                allow_zero_cira=False,
            )
            identity = {
                "graph_sha256": "a" * 64,
                "binary_sha256": "b" * 64,
            }
            with (
                mock.patch.object(
                    self.runner,
                    "ensure_checkpoint",
                    return_value=(
                        checkpoint,
                        manifest,
                        identity,
                        "c" * 64,
                    ),
                ),
                mock.patch.object(
                    self.runner,
                    "checkpoint_common_command",
                    return_value=["gem5"],
                ),
                mock.patch.object(self.runner, "append_cache_args"),
                mock.patch.object(self.runner, "append_kind_args"),
                mock.patch.object(
                    self.runner.subprocess,
                    "run",
                    side_effect=subprocess.TimeoutExpired(["gem5"], 1),
                ),
            ):
                row = self.runner.run_one_checkpoint(
                    args,
                    "pr",
                    "cxl_vanilla",
                    binary_dir,
                    "baseline",
                )
        self.assertEqual(row["status"], "restore-timeout")
        self.assertEqual(row["verification"], "missing")
        self.assertEqual(row["cores"], 2)
        self.assertIn("timed out", row["error"])


if __name__ == "__main__":
    unittest.main()
