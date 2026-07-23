# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

import csv
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


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
            "speedup_vs_cxl,scale,iterations,measured_trial,fast_forward_cpu,"
            "roi_cpu,cpu_switches,cxl_link_delay,all_memory_cxl,"
            "asmc_loads,asmc_completed,cira_prefetches,"
            "cira_indexed_prefetches,cira_csr_prefetches,cira_completed,"
            "cira_useful,cira_late,cira_read_packets,cira_read_bytes,"
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
                        "[board]\n"
                        "mem_ranges=0:4294967296\n"
                        "[board.cxl_mem_link0]\n"
                        "type=SerialLink\n"
                        f"delay={delay}\n"
                        "ranges=0:4294967296\n"
                        "cpu_side_port="
                        "board.cache_hierarchy.membus.mem_side_ports[0]\n"
                        "mem_side_port=board.memory.mem_ctrl.port\n"
                        "[board.memory.mem_ctrl]\n"
                        "port=board.cxl_mem_link0.mem_side_port\n"
                        "[board.memory.mem_ctrl.dram]\n"
                        "range=0:4294967296\n"
                        "[board.cache_hierarchy.membus]\n"
                        "mem_side_ports=board.cxl_mem_link0.cpu_side_port "
                        "board.processor.switch.core.interrupts.pio\n"
                        "[board.cache_hierarchy.l2buses]\n"
                        + (
                            "cpu_side_ports="
                            "board.cache_hierarchy.l1i-cache-0.mem_side "
                            "board.cache_hierarchy.l1d-cache-0.mem_side "
                            "board.cira.mem_side_port\n"
                            if kind == "cira"
                            else
                            "cpu_side_ports="
                            "board.cache_hierarchy.l1i-cache-0.mem_side "
                            "board.cache_hierarchy.l1d-cache-0.mem_side\n"
                        )
                        + "mem_side_ports="
                        "board.cache_hierarchy.l2-cache-0.cpu_side\n"
                        "[board.processor.start.core]\n"
                        "type=BaseAtomicSimpleCPU\n"
                        "[board.processor.switch.core]\n"
                        "type=BaseTimingSimpleCPU\n"
                        "[board.processor.switch.core.workload]\n"
                        f"cmd=/bin/{benchmark} -g 20 -n 2 -v\n"
                        + (
                            "[board.cira]\n"
                            "demand_probe_target="
                            "board.cache_hierarchy.l2-cache-0\n"
                            "mem_side_port="
                            "board.cache_hierarchy.l2buses.cpu_side_ports[2]\n"
                            if kind == "cira"
                            else ""
                        ),
                        encoding="utf-8",
                    )
                    (run_dir / "gem5.log").write_text(
                        "Switching from fast-forward CPU to timing CPU!\n"
                        "Verification: PASS\n",
                        encoding="utf-8",
                    )
                    stats = [
                        "---------- Begin Simulation Statistics ----------",
                        "simTicks 100",
                        "simInsts 10",
                        (
                            "board.cache_hierarchy.membus.pktCount_l2.mem_side_port::"
                            "board.cxl_mem_link0.cpu_side_port 137"
                        ),
                        (
                            "board.cache_hierarchy.membus.pktSize_l2.mem_side_port::"
                            "board.cxl_mem_link0.cpu_side_port 2880"
                        ),
                        (
                            "board.cache_hierarchy.membus.pktCount::"
                            "board.cxl_mem_link0.cpu_side_port 777"
                        ),
                        (
                            "board.cache_hierarchy.membus.pktSize::"
                            "board.cxl_mem_link0.cpu_side_port 888"
                        ),
                        "board.cache_hierarchy.membus.pktCount::total 9999",
                        "board.cache_hierarchy.membus.pktSize::total 99999",
                        (
                            "board.cache_hierarchy.l1d-cache-0."
                            "demandMisses::processor.switch.core.data 10"
                        ),
                        (
                            "board.cache_hierarchy.l1d-cache-0."
                            "demandMisses::total 10"
                        ),
                        (
                            "board.cache_hierarchy.l2-cache-0.demandHits::"
                            "processor.switch.core.data 11"
                        ),
                        (
                            "board.cache_hierarchy.l2-cache-0.demandMisses::"
                            "processor.switch.core.data 5000"
                        ),
                        (
                            "board.cache_hierarchy.l2-cache-0.demandHits::"
                            "processor.switch.core.inst 13"
                        ),
                        (
                            "board.cache_hierarchy.l2-cache-0.demandMisses::"
                            "processor.switch.core.inst 14"
                        ),
                        (
                            "board.cache_hierarchy.l2-cache-0."
                            "demandHits::total 24"
                        ),
                        (
                            "board.cache_hierarchy.l2-cache-0."
                            "demandMisses::total 5014"
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
                            "board.cira.issuedIndexedPrefetches 3",
                            "board.cira.issuedCsrPrefetches 5",
                            f"board.cira.completedPrefetches {completed_prefetches}",
                            "board.cira.usefulPrefetches 4",
                            "board.cira.latePrefetches 0",
                            "board.cira.readPackets 16",
                            "board.cira.readBytes 1024",
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
                            "scale": "20",
                            "iterations": "2",
                            "measured_trial": "1",
                            "fast_forward_cpu": "atomic",
                            "roi_cpu": "timing",
                            "cpu_switches": "1",
                            "cxl_link_delay": latency,
                            "all_memory_cxl": "true",
                            "asmc_loads": "7" if kind == "amu" else "0",
                            "asmc_completed": "7" if kind == "amu" else "0",
                            "cira_prefetches": "8" if kind == "cira" else "0",
                            "cira_indexed_prefetches": "3" if kind == "cira" else "0",
                            "cira_csr_prefetches": "5" if kind == "cira" else "0",
                            "cira_completed": (
                                str(completed_prefetches)
                                if kind == "cira"
                                else "0"
                            ),
                            "cira_useful": "4" if kind == "cira" else "0",
                            "cira_late": "0",
                            "cira_read_packets": "16" if kind == "cira" else "0",
                            "cira_read_bytes": "1024" if kind == "cira" else "0",
                            "cxl_packets": "137",
                            "cxl_bytes": "2880",
                            "l1d_demand_misses": "10",
                            "l2d_demand_hits": "11",
                            "l2d_demand_misses": "5000",
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

    def mutate_summary(self, root, latency, predicate, **updates):
        summary = root / latency / "summary.csv"
        with summary.open(newline="", encoding="utf-8") as stream:
            reader = csv.DictReader(stream)
            fields = reader.fieldnames
            rows = list(reader)
        for row in rows:
            if predicate(row):
                row.update(updates)
                break
        with summary.open("w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)

    def make_pr_gate(self, root):
        full = root / "full"
        self.make_sweep(full)
        summary = full / "1us" / "summary.csv"
        with summary.open(newline="", encoding="utf-8") as stream:
            reader = csv.DictReader(stream)
            fields = reader.fieldnames
            rows = [
                row
                for row in reader
                if row["benchmark"] == "pr"
                and row["label"] in ("cxl_vanilla", "cira_pgo")
            ]
        gate = root / "gate"
        gate.mkdir()
        for row in rows:
            source = Path(row["run_dir"])
            target = gate / "pr" / row["label"]
            target.mkdir(parents=True)
            for name in ("config.ini", "stats.txt", "gem5.log"):
                (target / name).write_bytes((source / name).read_bytes())
            row["run_dir"] = str(target)
        rows[0]["sim_ticks"] = "200"
        rows[0]["speedup_vs_cxl"] = "1"
        rows[1]["sim_ticks"] = "100"
        rows[1]["speedup_vs_cxl"] = "2"
        baseline_stats = gate / "pr" / "cxl_vanilla" / "stats.txt"
        baseline_stats.write_text(
            baseline_stats.read_text(encoding="utf-8").replace(
                "simTicks 100", "simTicks 200", 1
            ),
            encoding="utf-8",
        )
        cira_stats = gate / "pr" / "cira_pgo" / "stats.txt"
        cira_stats.write_text(
            cira_stats.read_text(encoding="utf-8")
            .replace(
                "processor.switch.core.data 5000",
                "processor.switch.core.data 4000",
            )
            .replace(
                "demandMisses::total 5014",
                "demandMisses::total 4014",
            ),
            encoding="utf-8",
        )
        rows[1]["l2d_demand_misses"] = "4000"
        with (gate / "summary.csv").open("w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)
        return gate

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
                "board.cxl_mem_link0.cpu_side_port 137\n",
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
                "cxl_packets=4195 != exact first-ROI packet count 137",
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
                "processor.switch.core.data 5000\n",
                "",
            )
            stats.write_text(text, encoding="utf-8")
            with self.assertRaisesRegex(
                self.validator.ValidationError,
                "demandMisses family total",
            ):
                self.validator.validate_sweep(root)

    def test_accepts_nozero_requestor_stat_as_zero_with_family_total(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_sweep(root)
            stats = root / "200ns" / "pr" / "cxl_vanilla" / "stats.txt"
            text = stats.read_text(encoding="utf-8").replace(
                "board.cache_hierarchy.l2-cache-0.demandHits::"
                "processor.switch.core.data 11\n",
                "",
            ).replace(
                "board.cache_hierarchy.l2-cache-0.demandHits::total 24\n",
                "board.cache_hierarchy.l2-cache-0.demandHits::total 13\n",
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

    def test_accepts_real_nozero_shape_with_wholly_omitted_zero_families(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_sweep(root)
            stats = root / "200ns" / "pr" / "cxl_vanilla" / "stats.txt"
            text = stats.read_text(encoding="utf-8")
            for line in (
                "board.cache_hierarchy.l1d-cache-0."
                "demandMisses::processor.switch.core.data 10\n",
                "board.cache_hierarchy.l1d-cache-0.demandMisses::total 10\n",
                "board.cache_hierarchy.l2-cache-0.demandMisses::"
                "processor.switch.core.data 5000\n",
                "board.cache_hierarchy.l2-cache-0.demandMisses::"
                "processor.switch.core.inst 14\n",
                "board.cache_hierarchy.l2-cache-0.demandMisses::total 5014\n",
            ):
                text = text.replace(line, "")
            text = text.replace(
                "board.cache_hierarchy.l2-cache-0.demandHits::"
                "processor.switch.core.data 11\n",
                "",
            )
            text = text.replace(
                "board.cache_hierarchy.l2-cache-0.demandHits::"
                "processor.switch.core.inst 13\n",
                (
                    "board.cache_hierarchy.l1d-cache-0.demandHits::"
                    "processor.switch.core.data 99\n"
                    "board.cache_hierarchy.l1d-cache-0.demandHits::total 99\n"
                    "board.cache_hierarchy.l2-cache-0.demandHits::"
                    "processor.switch.core.inst 13\n"
                ),
            )
            text = text.replace(
                "board.cache_hierarchy.l2-cache-0.demandHits::total 24\n",
                (
                    "board.cache_hierarchy.l2-cache-0.demandHits::total 13\n"
                    "board.cache_hierarchy.l2-cache-0.tags.occupancies::"
                    "processor.switch.core.data 7\n"
                ),
            )
            stats.write_text(text, encoding="utf-8")
            self.mutate_summary(
                root,
                "200ns",
                lambda row: row["benchmark"] == "pr"
                and row["kind"] == "baseline",
                l1d_demand_misses="0",
                l2d_demand_hits="0",
                l2d_demand_misses="0",
                l2i_demand_misses="0",
            )
            result = self.validator.validate_sweep(root)
            self.assertEqual(result.row_count, 48)

    def test_full_matrix_rejects_legacy_cache_requestor_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_sweep(root)
            stats = root / "2us" / "sssp" / "cxl_vanilla" / "stats.txt"
            stats.write_text(
                stats.read_text(encoding="utf-8").replace(
                    "processor.switch.core.data 5000",
                    "processor.cores.core.data 5000",
                    1,
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                self.validator.ValidationError, "legacy cache requestor"
            ):
                self.validator.validate_sweep(root)

    def test_full_matrix_rejects_unknown_cache_requestor(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_sweep(root)
            stats = root / "2us" / "sssp" / "cxl_vanilla" / "stats.txt"
            stats.write_text(
                stats.read_text(encoding="utf-8").replace(
                    "processor.switch.core.data 5000",
                    "procesor.swotch.core.data 5000",
                    1,
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                self.validator.ValidationError, "unknown cache requestor"
            ):
                self.validator.validate_sweep(root)

    def test_rejects_adjacent_large_integer_counter_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_sweep(root)
            stats = root / "200ns" / "bfs" / "cxl_vanilla" / "stats.txt"
            text = stats.read_text(encoding="utf-8").replace(
                "board.cxl_mem_link0.cpu_side_port 137\n",
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
                "l2d_demand_misses=999 != exact first-ROI value 5000",
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
                    "non-owner cira_total_latency must be blank or zero",
                ):
                    self.validator.validate_sweep(root)

    def test_rejects_noncanonical_g20_run_metadata(self):
        for field, value in (
            ("scale", "19"),
            ("iterations", "1"),
            ("measured_trial", "0"),
            ("fast_forward_cpu", "timing"),
            ("roi_cpu", "atomic"),
            ("cpu_switches", "2"),
            ("all_memory_cxl", "false"),
        ):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                self.make_sweep(root)
                self.mutate_summary(
                    root,
                    "200ns",
                    lambda row: row["benchmark"] == "bfs"
                    and row["kind"] == "baseline",
                    **{field: value},
                )
                with self.assertRaisesRegex(
                    self.validator.ValidationError, field
                ):
                    self.validator.validate_sweep(root)

    def test_rejects_switch_marker_count_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_sweep(root)
            log = root / "200ns" / "bfs" / "cxl_vanilla" / "gem5.log"
            log.write_text("Verification: PASS\n", encoding="utf-8")
            with self.assertRaisesRegex(
                self.validator.ValidationError, "cpu_switches"
            ):
                self.validator.validate_sweep(root)

    def test_rejects_config_workload_shape_mismatch(self):
        for option, replacement in (
            ("scale", "-g 19 -n 2"),
            ("iterations", "-g 20 -n 1"),
        ):
            with self.subTest(option=option), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                self.make_sweep(root)
                config = root / "200ns" / "bfs" / "cxl_vanilla" / "config.ini"
                config.write_text(
                    config.read_text(encoding="utf-8").replace(
                        "-g 20 -n 2", replacement, 1
                    ),
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(
                    self.validator.ValidationError,
                    f"config workload {option}",
                ):
                    self.validator.validate_sweep(root)

    def test_rejects_all_memory_cxl_topology_violations(self):
        mutations = (
            (
                "range mismatch",
                "[board]\nmem_ranges=0:4294967296",
                "[board]\nmem_ranges=0:2147483648",
            ),
            (
                "memory controller port",
                "port=board.cxl_mem_link0.mem_side_port",
                "port=board.cache_hierarchy.membus.mem_side_ports",
            ),
            (
                "direct memory controller",
                "mem_side_ports=board.cxl_mem_link0.cpu_side_port ",
                "mem_side_ports=board.memory.mem_ctrl.port "
                "board.cxl_mem_link0.cpu_side_port ",
            ),
            (
                "starting CPU",
                "type=BaseAtomicSimpleCPU",
                "type=BaseTimingSimpleCPU",
            ),
            (
                "switch CPU",
                "type=BaseTimingSimpleCPU",
                "type=BaseAtomicSimpleCPU",
            ),
            (
                "SerialLink",
                "type=SerialLink",
                "type=Bridge",
            ),
            (
                "cpu_side_port binding",
                "cpu_side_port=board.cache_hierarchy.membus.mem_side_ports[0]",
                "cpu_side_port=board.cache_hierarchy.membus.mem_side_ports[1]",
            ),
            (
                "mem_side_port binding",
                "mem_side_port=board.memory.mem_ctrl.port",
                "mem_side_port=board.cache_hierarchy.membus.cpu_side_ports[0]",
            ),
        )
        for message, old, new in mutations:
            with self.subTest(message=message), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                self.make_sweep(root)
                config = root / "200ns" / "bfs" / "cxl_vanilla" / "config.ini"
                config.write_text(
                    config.read_text(encoding="utf-8").replace(old, new, 1),
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(
                    self.validator.ValidationError, message
                ):
                    self.validator.validate_sweep(root)

    def test_rejects_cira_probe_or_port_bypass(self):
        for field, replacement in (
            (
                "demand_probe_target",
                "demand_probe_target=board.cache_hierarchy.l1d-cache-0",
            ),
            (
                "mem_side_port",
                "mem_side_port=board.memory.mem_ctrl.port",
            ),
            (
                "mem_side_port",
                "mem_side_port="
                "board.cache_hierarchy.l2buses.mem_side_ports[0]",
            ),
        ):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                self.make_sweep(root)
                config = root / "200ns" / "bfs" / "cira_pgo" / "config.ini"
                text = config.read_text(encoding="utf-8")
                line = (
                    "demand_probe_target=board.cache_hierarchy.l2-cache-0"
                    if field == "demand_probe_target"
                    else
                    "mem_side_port="
                    "board.cache_hierarchy.l2buses.cpu_side_ports[2]"
                )
                config.write_text(
                    text.replace(line, replacement, 1), encoding="utf-8"
                )
                with self.assertRaisesRegex(
                    self.validator.ValidationError, field
                ):
                    self.validator.validate_sweep(root)

    def test_rejects_cira_l2bus_index_without_reciprocal_binding(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_sweep(root)
            config = root / "200ns" / "bfs" / "cira_pgo" / "config.ini"
            config.write_text(
                config.read_text(encoding="utf-8").replace(
                    "board.cira.mem_side_port\n",
                    "board.other.mem_side_port\n",
                    1,
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                self.validator.ValidationError, "CIRA endpoint binding"
            ):
                self.validator.validate_sweep(root)

    def test_rejects_summary_amu_and_cira_balance_mismatches(self):
        for kind, field, value in (
            ("amu", "asmc_completed", "6"),
            ("cira", "cira_completed", "7"),
        ):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                self.make_sweep(root)
                self.mutate_summary(
                    root,
                    "200ns",
                    lambda row: row["benchmark"] == "bfs"
                    and row["kind"] == kind,
                    **{field: value},
                )
                with self.assertRaisesRegex(
                    self.validator.ValidationError, field
                ):
                    self.validator.validate_sweep(root)

    def test_rejects_nonpositive_csr_descriptor_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_sweep(root)
            stats = root / "200ns" / "bfs" / "cira_pgo" / "stats.txt"
            stats.write_text(
                stats.read_text(encoding="utf-8").replace(
                    "board.cira.issuedCsrPrefetches 5",
                    "board.cira.issuedCsrPrefetches 0",
                ),
                encoding="utf-8",
            )
            self.mutate_summary(
                root,
                "200ns",
                lambda row: row["benchmark"] == "bfs"
                and row["kind"] == "cira",
                cira_csr_prefetches="0",
            )
            with self.assertRaisesRegex(
                self.validator.ValidationError, "cira_csr_prefetches"
            ):
                self.validator.validate_sweep(root)

    def test_full_matrix_accepts_regression_speedup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_sweep(root)
            stats = root / "200ns" / "bfs" / "cira_pgo" / "stats.txt"
            stats.write_text(
                stats.read_text(encoding="utf-8")
                .replace("simTicks 100", "simTicks 125", 1)
                .replace(
                    "board.cira.usefulPrefetches 4",
                    "board.cira.usefulPrefetches 0",
                ),
                encoding="utf-8",
            )
            self.mutate_summary(
                root,
                "200ns",
                lambda row: row["benchmark"] == "bfs"
                and row["kind"] == "cira",
                sim_ticks="125",
                speedup_vs_cxl="0.8",
                cira_useful="0",
            )
            result = self.validator.validate_sweep(root)
            self.assertEqual(result.row_count, 48)

    def test_pr_gate_passes_exact_two_row_discriminator(self):
        with tempfile.TemporaryDirectory() as tmp:
            gate = self.make_pr_gate(Path(tmp))
            result = self.validator.validate_pr_gate(gate)
            self.assertEqual(result.row_count, 2)

    def test_pr_gate_rejects_each_discriminator_failure(self):
        cases = ("baseline_misses", "cira_not_lower", "useful_zero", "speedup")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp:
                gate = self.make_pr_gate(Path(tmp))
                summary = gate / "summary.csv"
                with summary.open(newline="", encoding="utf-8") as stream:
                    reader = csv.DictReader(stream)
                    fields = reader.fieldnames
                    rows = list(reader)
                baseline = next(row for row in rows if row["kind"] == "baseline")
                cira = next(row for row in rows if row["kind"] == "cira")
                if case == "baseline_misses":
                    baseline["l2d_demand_misses"] = "4096"
                    path = gate / "pr" / "cxl_vanilla" / "stats.txt"
                    path.write_text(
                        path.read_text(encoding="utf-8")
                        .replace(
                            "processor.switch.core.data 5000",
                            "processor.switch.core.data 4096",
                        )
                        .replace(
                            "demandMisses::total 5014",
                            "demandMisses::total 4110",
                        ),
                        encoding="utf-8",
                    )
                elif case == "cira_not_lower":
                    cira["l2d_demand_misses"] = "5000"
                    path = gate / "pr" / "cira_pgo" / "stats.txt"
                    path.write_text(
                        path.read_text(encoding="utf-8")
                        .replace(
                            "processor.switch.core.data 4000",
                            "processor.switch.core.data 5000",
                        )
                        .replace(
                            "demandMisses::total 4014",
                            "demandMisses::total 5014",
                        ),
                        encoding="utf-8",
                    )
                elif case == "useful_zero":
                    cira["cira_useful"] = "0"
                    path = gate / "pr" / "cira_pgo" / "stats.txt"
                    path.write_text(
                        path.read_text(encoding="utf-8").replace(
                            "board.cira.usefulPrefetches 4",
                            "board.cira.usefulPrefetches 0",
                        ),
                        encoding="utf-8",
                    )
                else:
                    cira["sim_ticks"] = "200"
                    cira["speedup_vs_cxl"] = "1"
                    path = gate / "pr" / "cira_pgo" / "stats.txt"
                    path.write_text(
                        path.read_text(encoding="utf-8").replace(
                            "simTicks 100", "simTicks 200", 1
                        ),
                        encoding="utf-8",
                    )
                with summary.open("w", newline="", encoding="utf-8") as stream:
                    writer = csv.DictWriter(stream, fieldnames=fields)
                    writer.writeheader()
                    writer.writerows(rows)
                with self.assertRaises(self.validator.ValidationError):
                    self.validator.validate_pr_gate(gate)

    def test_cli_outputs_are_deterministic_and_atomic(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "sweep"
            self.make_sweep(root)
            combined = Path(tmp) / "combined.csv"
            evidence = Path(tmp) / "evidence.json"
            command = [
                sys.executable,
                str(VALIDATOR_PATH),
                str(root),
                "--combined-output",
                str(combined),
                "--validation-output",
                str(evidence),
            ]
            first = subprocess.run(
                command, text=True, capture_output=True, check=False
            )
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertIn("PASS: 48/48", first.stdout)
            expected = (combined.read_bytes(), evidence.read_bytes())
            payload = json.loads(evidence.read_text(encoding="utf-8"))
            self.assertEqual(payload["row_count"], 48)
            second = subprocess.run(
                command, text=True, capture_output=True, check=False
            )
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertEqual(expected, (combined.read_bytes(), evidence.read_bytes()))

            self.mutate_summary(
                root,
                "200ns",
                lambda row: row["benchmark"] == "bfs",
                scale="19",
            )
            failed_combined = Path(tmp) / "failed.csv"
            failed_evidence = Path(tmp) / "failed.json"
            failure = subprocess.run(
                [
                    *command[:-4],
                    "--combined-output",
                    str(failed_combined),
                    "--validation-output",
                    str(failed_evidence),
                ],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertNotEqual(failure.returncode, 0)
            self.assertFalse(failed_combined.exists())
            self.assertFalse(failed_evidence.exists())

    def test_paired_output_second_replace_failure_is_rolled_back(self):
        result = self.validator.ValidationResult(
            1,
            0,
            0,
            [{"latency": "1us", "status": "ok"}],
            "full",
        )
        for existing in (False, True):
            with (
                self.subTest(existing=existing),
                tempfile.TemporaryDirectory() as tmp,
            ):
                root = Path(tmp)
                combined = root / "combined.csv"
                evidence = root / "evidence.json"
                if existing:
                    combined.write_text("old combined\n", encoding="utf-8")
                    evidence.write_text("old evidence\n", encoding="utf-8")
                original = self.validator.os.replace
                replace_calls = 0

                def fail_second_new_output(source, destination):
                    nonlocal replace_calls
                    destination = Path(destination)
                    if destination in (combined, evidence):
                        replace_calls += 1
                        if replace_calls == 2:
                            raise OSError("forced second output failure")
                    return original(source, destination)

                with mock.patch.object(
                    self.validator.os,
                    "replace",
                    side_effect=fail_second_new_output,
                ):
                    with self.assertRaisesRegex(
                        OSError, "forced second output failure"
                    ):
                        self.validator.write_outputs(
                            result,
                            combined_output=combined,
                            validation_output=evidence,
                        )
                if existing:
                    self.assertEqual(
                        combined.read_text(encoding="utf-8"),
                        "old combined\n",
                    )
                    self.assertEqual(
                        evidence.read_text(encoding="utf-8"),
                        "old evidence\n",
                    )
                else:
                    self.assertFalse(combined.exists())
                    self.assertFalse(evidence.exists())
                self.assertEqual(
                    list(root.glob(".*.tmp-*")),
                    [],
                    "transaction left staging or backup files",
                )

    def test_pr_gate_cli_prints_exact_pass_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            gate = self.make_pr_gate(Path(tmp))
            result = subprocess.run(
                [
                    sys.executable,
                    str(VALIDATOR_PATH),
                    str(gate),
                    "--pr-gate",
                ],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                result.stdout.strip(),
                "PASS: PR@1us scale-20 CIRA discriminator",
            )


if __name__ == "__main__":
    unittest.main()
