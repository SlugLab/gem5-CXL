# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

import csv
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


REPO = Path(__file__).resolve().parents[3]
VALIDATOR_PATH = REPO / "scripts" / "validate_gapbs_amu_latency_sweep.py"
CHECKPOINT_PATH = REPO / "scripts" / "gapbs_checkpoint.py"


def load_validator():
    spec = importlib.util.spec_from_file_location("latency_validator", VALIDATOR_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class GapbsAmuLatencySweepValidatorTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.validator = load_validator()
        cls.checkpoint = load_module(
            "latency_validator_checkpoint", CHECKPOINT_PATH
        )

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
                            "demandHits::processor.switch.core.data 90"
                        ),
                        (
                            "board.cache_hierarchy.l1d-cache-0."
                            "demandHits::total 90"
                        ),
                        (
                            "board.cache_hierarchy.l1d-cache-0."
                            "demandMisses::processor.switch.core.data 10"
                        ),
                        (
                            "board.cache_hierarchy.l1d-cache-0."
                            "demandMisses::total 10"
                        ),
                        (
                            "board.cache_hierarchy.l1d-cache-0."
                            "demandAccesses::processor.switch.core.data 100"
                        ),
                        (
                            "board.cache_hierarchy.l1d-cache-0."
                            "demandAccesses::total 100"
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
                        (
                            "board.cache_hierarchy.l2-cache-0.demandAccesses::"
                            "processor.switch.core.data 5011"
                        ),
                        (
                            "board.cache_hierarchy.l2-cache-0.demandAccesses::"
                            "processor.switch.core.inst 27"
                        ),
                        (
                            "board.cache_hierarchy.l2-cache-0."
                            "demandAccesses::total 5038"
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
        self.upgrade_to_checkpoint_evidence(root)

    def checkpoint_config(self, binary, graph, benchmark, kind, delay):
        cira_port = (
            " board.cira.mem_side_port" if kind == "cira" else ""
        )
        asmc_membus_port = (
            "cpu_side_ports=board.asmc_io_cache.mem_side\n"
            if kind == "amu"
            else ""
        )
        asmc = (
            "[board.asmc]\n"
            "asmc_latency=0\n"
            "completion_latency=0\n"
            "default_granularity=8\n"
            "issue_latency=1000\n"
            "max_outstanding=256\n"
            "max_send_queue=512\n"
            "spm_size=262144\n"
            "mem_side_port="
            "board.asmc_io_cache.cpu_side\n"
            "[board.asmc_io_cache]\n"
            "type=Cache\n"
            "cpu_side=board.asmc.mem_side_port\n"
            "mem_side="
            "board.cache_hierarchy.membus.cpu_side_ports[0]\n"
            if kind == "amu"
            else ""
        )
        cira = (
            "[board.cira]\n"
            "completion_latency=0\n"
            "issue_latency=1000\n"
            "max_outstanding=256\n"
            "max_send_queue=1024\n"
            "demand_probe_target=board.cache_hierarchy.l2-cache-0\n"
            "mem_side_port="
            "board.cache_hierarchy.l2buses0.cpu_side_ports[2]\n"
            if kind == "cira"
            else ""
        )
        return (
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
            + asmc_membus_port
            +
            "mem_side_ports=board.cxl_mem_link0.cpu_side_port "
            "board.processor.cores0.core.interrupts.pio "
            "board.processor.cores1.core.interrupts.pio\n"
            "[board.cache_hierarchy.l2buses0]\n"
            "cpu_side_ports=board.cache_hierarchy.l1i-cache-0.mem_side "
            f"board.cache_hierarchy.l1d-cache-0.mem_side{cira_port}\n"
            "mem_side_ports=board.cache_hierarchy.l2-cache-0.cpu_side\n"
            "[board.cache_hierarchy.l2buses1]\n"
            "cpu_side_ports=board.cache_hierarchy.l1i-cache-1.mem_side "
            "board.cache_hierarchy.l1d-cache-1.mem_side\n"
            "mem_side_ports=board.cache_hierarchy.l2-cache-1.cpu_side\n"
            "[board.processor.cores0.core]\n"
            "type=BaseTimingSimpleCPU\n"
            "[board.processor.cores1.core]\n"
            "type=BaseTimingSimpleCPU\n"
            "[board.processor.cores0.core.workload]\n"
            f"cmd={binary} -f {graph} -n 2 -v\n"
            + (
                "env=OMP_NUM_THREADS=2 CIRA_GEM5_M5OPS=1\n"
                if kind == "cira"
                else "env=OMP_NUM_THREADS=2\n"
            )
            + asmc
            + cira
        )

    def checkpoint_stats(self, kind, completed_prefetches):
        lines = [
            "---------- Begin Simulation Statistics ----------",
            "simTicks 100",
            "simInsts 10",
            (
                "board.cache_hierarchy.membus.pktCount_"
                "board.cache_hierarchy.l2-cache-0.mem_side_port::"
                "board.cxl_mem_link0.cpu_side_port 60"
            ),
            (
                "board.cache_hierarchy.membus.pktCount_"
                "board.cache_hierarchy.l2-cache-1.mem_side_port::"
                "board.cxl_mem_link0.cpu_side_port 77"
            ),
            (
                "board.cache_hierarchy.membus.pktSize_"
                "board.cache_hierarchy.l2-cache-0.mem_side_port::"
                "board.cxl_mem_link0.cpu_side_port 1000"
            ),
            (
                "board.cache_hierarchy.membus.pktSize_"
                "board.cache_hierarchy.l2-cache-1.mem_side_port::"
                "board.cxl_mem_link0.cpu_side_port 1880"
            ),
        ]
        if kind == "amu":
            lines += [
                (
                    "board.cache_hierarchy.membus.pktCount_"
                    "board.asmc_io_cache.mem_side::"
                    "board.cxl_mem_link0.cpu_side_port 7"
                ),
                (
                    "board.cache_hierarchy.membus.pktSize_"
                    "board.asmc_io_cache.mem_side::"
                    "board.cxl_mem_link0.cpu_side_port 64"
                ),
            ]
        for core, hits, misses in ((0, 46, 4), (1, 44, 6)):
            root = f"board.cache_hierarchy.l1d-cache-{core}"
            requestor = f"processor.cores{core}.core.data"
            lines += [
                f"{root}.demandHits::{requestor} {hits}",
                f"{root}.demandHits::total {hits}",
                f"{root}.demandMisses::{requestor} {misses}",
                f"{root}.demandMisses::total {misses}",
                f"{root}.demandAccesses::{requestor} {hits + misses}",
                f"{root}.demandAccesses::total {hits + misses}",
            ]
        l2_values = {
            0: {"data": (5, 2500), "inst": (6, 7)},
            1: {"data": (6, 2500), "inst": (7, 7)},
        }
        for core, roles in l2_values.items():
            root = f"board.cache_hierarchy.l2-cache-{core}"
            totals = {"hits": 0, "misses": 0, "accesses": 0}
            for role, (hits, misses) in roles.items():
                requestor = f"processor.cores{core}.core.{role}"
                accesses = hits + misses
                lines += [
                    f"{root}.demandHits::{requestor} {hits}",
                    f"{root}.demandMisses::{requestor} {misses}",
                    f"{root}.demandAccesses::{requestor} {accesses}",
                ]
                totals["hits"] += hits
                totals["misses"] += misses
                totals["accesses"] += accesses
            lines += [
                f"{root}.demandHits::total {totals['hits']}",
                f"{root}.demandMisses::total {totals['misses']}",
                f"{root}.demandAccesses::total {totals['accesses']}",
            ]
        if kind == "amu":
            lines += [
                "board.asmc.issuedLoads 7",
                "board.asmc.completedLoads 7",
            ]
        elif kind == "cira":
            lines += [
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
        lines += [
            "---------- End Simulation Statistics   ----------",
            "---------- Begin Simulation Statistics ----------",
            "simTicks 200",
            "simInsts 20",
            "---------- End Simulation Statistics   ----------",
        ]
        return "\n".join(lines) + "\n"

    def upgrade_to_checkpoint_evidence(self, root):
        artifacts = root / "checkpoint-fixture"
        artifacts.mkdir()
        graph = REPO / "m5out" / "gapbs_graphs" / "g20.sg"
        self.assertTrue(graph.is_file())
        gem5 = artifacts / "gem5.opt"
        gem5.write_bytes(b"gem5")
        config_source = artifacts / "x86-gapbs-amu-se.py"
        config_source.write_bytes(b"config")
        config_dependency = artifacts / "gapbs_roi_state.py"
        config_dependency.write_bytes(b"state")
        graph_sha = self.validator.EXPECTED_GRAPH_SHA256

        checkpoint_fields = [
            "graph_path",
            "graph_scale",
            "graph_sha256",
            "checkpoint_id",
            "checkpoint_manifest",
            "checkpoint_binary_sha256",
            "checkpoint_restores",
        ]
        for latency, delay in self.validator.EXPECTED_LATENCIES.items():
            summary = root / latency / "summary.csv"
            with summary.open(newline="", encoding="utf-8") as stream:
                reader = csv.DictReader(stream)
                fields = list(reader.fieldnames)
                rows = list(reader)
            for field in checkpoint_fields:
                fields.append(field)
            for row in rows:
                run_dir = Path(row["run_dir"])
                binary = (
                    artifacts
                    / "bin"
                    / row["label"]
                    / row["benchmark"]
                )
                binary.parent.mkdir(parents=True, exist_ok=True)
                binary.write_bytes(
                    f"{row['kind']}-{row['benchmark']}".encode()
                )
                arguments = ["-f", str(graph.resolve()), "-n", "2", "-v"]
                model_parameters = {
                    "kind": row["kind"],
                    "env": "",
                }
                if row["kind"] == "amu":
                    model_parameters.update(
                        {
                            "asmc_spm_size": "256KiB",
                            "asmc_granularity": "8",
                            "asmc_max_outstanding": "256",
                            "asmc_max_send_queue": "512",
                            "asmc_issue_latency": "1ns",
                            "asmc_completion_latency": "0ns",
                            "asmc_latency": "0ns",
                        }
                    )
                elif row["kind"] == "cira":
                    model_parameters.update(
                        {
                            "cira_max_outstanding": "256",
                            "cira_max_send_queue": "1024",
                            "cira_issue_latency": "1ns",
                            "cira_completion_latency": "0ns",
                        }
                    )
                identity = {
                    "schema": 2,
                    "kind": row["kind"],
                    "binary_path": str(binary.resolve()),
                    "binary_sha256": self.checkpoint.sha256_file(binary),
                    "graph_path": str(graph.resolve()),
                    "graph_sha256": graph_sha,
                    "graph_scale": 20,
                    "arguments": arguments,
                    "cores": 2,
                    "memory_size": "4GiB",
                    "gem5_path": str(gem5.resolve()),
                    "gem5_sha256": self.checkpoint.sha256_file(gem5),
                    "config_path": str(config_source.resolve()),
                    "config_sha256": self.checkpoint.sha256_file(
                        config_source
                    ),
                    "config_dependencies": {
                        str(config_dependency.resolve()):
                        self.checkpoint.sha256_file(config_dependency),
                    },
                    "model_parameters": model_parameters,
                }
                checkpoint_id = self.checkpoint.identity_key(identity)
                checkpoint_root = artifacts / "checkpoints" / checkpoint_id
                checkpoint_root.mkdir(parents=True, exist_ok=True)
                (checkpoint_root / "m5.cpt").write_bytes(b"checkpoint")
                manifest = self.checkpoint.write_manifest(
                    checkpoint_root, identity
                )
                (run_dir / "config.ini").write_text(
                    self.checkpoint_config(
                        binary.resolve(),
                        graph.resolve(),
                        row["benchmark"],
                        row["kind"],
                        delay,
                    ),
                    encoding="utf-8",
                )
                (run_dir / "gem5.log").write_text(
                    f"GAPBS_CHECKPOINT_RESTORED path={checkpoint_root}\n"
                    "Dump stats at the end of the measured ROI!\n"
                    "GAPBS_VERIFICATION_EXIT_CAUSE "
                    "cause=m5_exit instruction encountered\n"
                    "Verification: PASS\n",
                    encoding="utf-8",
                )
                completed = int(row["cira_completed"])
                (run_dir / "stats.txt").write_text(
                    self.checkpoint_stats(row["kind"], completed),
                    encoding="utf-8",
                )
                row.update(
                    {
                        "fast_forward_cpu": "",
                        "cpu_switches": "0",
                        "graph_path": str(graph.resolve()),
                        "graph_scale": "20",
                        "graph_sha256": graph_sha,
                        "checkpoint_id": checkpoint_id,
                        "checkpoint_manifest": str(manifest),
                        "checkpoint_binary_sha256": identity[
                            "binary_sha256"
                        ],
                        "checkpoint_restores": "1",
                    }
                )
                if row["kind"] == "amu":
                    row["cxl_packets"] = "144"
                    row["cxl_bytes"] = "2944"
            with summary.open("w", newline="", encoding="utf-8") as stream:
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
                "processor.cores0.core.data 2500",
                "processor.cores0.core.data 2000",
            )
            .replace(
                "processor.cores1.core.data 2500",
                "processor.cores1.core.data 2000",
            )
            .replace(
                "demandMisses::total 2507",
                "demandMisses::total 2007",
            )
            .replace(
                "demandAccesses::total 2518",
                "demandAccesses::total 2018",
            )
            .replace(
                "demandAccesses::total 2520",
                "demandAccesses::total 2020",
            )
            .replace(
                "processor.cores0.core.data 2505",
                "processor.cores0.core.data 2005",
            )
            .replace(
                "processor.cores1.core.data 2506",
                "processor.cores1.core.data 2006",
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
                "missing End marker for simulation statistics section",
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

    def test_rejects_duplicate_summary_header_before_value_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_sweep(root)
            summary = root / "200ns" / "summary.csv"
            with summary.open(newline="", encoding="utf-8") as stream:
                rows = list(csv.reader(stream))
            status_index = rows[0].index("status")
            rows[0].append("status")
            for row in rows[1:]:
                row[status_index] = "fail"
                row.append("ok")
            with summary.open("w", newline="", encoding="utf-8") as stream:
                csv.writer(stream).writerows(rows)
            with self.assertRaisesRegex(
                self.validator.ValidationError,
                "duplicate columns: status",
            ):
                self.validator.validate_sweep(root)

    def test_rejects_missing_exact_raw_cxl_packet_stat(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_sweep(root)
            stats = root / "200ns" / "bfs" / "cxl_vanilla" / "stats.txt"
            text = stats.read_text(encoding="utf-8").replace(
                "board.cache_hierarchy.membus.pktCount_"
                "board.cache_hierarchy.l2-cache-0.mem_side_port::"
                "board.cxl_mem_link0.cpu_side_port 60\n",
                "",
            )
            stats.write_text(text, encoding="utf-8")
            with self.assertRaisesRegex(
                self.validator.ValidationError,
                "expected directional CXL cells",
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
                "cxl_packets=4195 != exact first-ROI value 137",
            ):
                self.validator.validate_sweep(root)

    def test_rejects_multiple_directional_cxl_packet_stats(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_sweep(root)
            stats = root / "200ns" / "bfs" / "cxl_vanilla" / "stats.txt"
            text = stats.read_text(encoding="utf-8").replace(
                "simInsts 10\n",
                (
                    "simInsts 10\n"
                    "board.cache_hierarchy.membus.pktCount_other::"
                    "board.cxl_mem_link0.cpu_side_port 1\n"
                ),
                1,
            )
            stats.write_text(text, encoding="utf-8")
            with self.assertRaisesRegex(
                self.validator.ValidationError,
                "expected directional CXL cells",
            ):
                self.validator.validate_sweep(root)

    def test_rejects_packet_and_byte_directional_cell_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_sweep(root)
            stats = root / "200ns" / "bfs" / "cxl_vanilla" / "stats.txt"
            text = stats.read_text(encoding="utf-8").replace(
                "board.cache_hierarchy.membus.pktSize_"
                "board.cache_hierarchy.l2-cache-0.mem_side_port::",
                "board.cache_hierarchy.membus.pktSize_other.mem_side_port::",
                1,
            )
            stats.write_text(text, encoding="utf-8")
            with self.assertRaisesRegex(
                self.validator.ValidationError, "expected directional CXL cells"
            ):
                self.validator.validate_sweep(root)

    def test_rejects_missing_exact_raw_cache_stat(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_sweep(root)
            stats = root / "200ns" / "bfs" / "cxl_vanilla" / "stats.txt"
            text = stats.read_text(encoding="utf-8").replace(
                "board.cache_hierarchy.l2-cache-0.demandMisses::"
                "processor.cores0.core.data 2500\n",
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
                "processor.cores0.core.data 5\n",
                "",
            ).replace(
                "board.cache_hierarchy.l2-cache-0.demandHits::total 11\n",
                "board.cache_hierarchy.l2-cache-0.demandHits::total 6\n",
            ).replace(
                "board.cache_hierarchy.l2-cache-0.demandAccesses::"
                "processor.cores0.core.data 2505\n",
                "board.cache_hierarchy.l2-cache-0.demandAccesses::"
                "processor.cores0.core.data 2500\n",
            ).replace(
                "board.cache_hierarchy.l2-cache-0.demandAccesses::total 2518\n",
                "board.cache_hierarchy.l2-cache-0.demandAccesses::total 2513\n",
            ).replace(
                "board.cache_hierarchy.l2-cache-1.demandHits::"
                "processor.cores1.core.data 6\n",
                "",
            ).replace(
                "board.cache_hierarchy.l2-cache-1.demandHits::total 13\n",
                "board.cache_hierarchy.l2-cache-1.demandHits::total 7\n",
            ).replace(
                "board.cache_hierarchy.l2-cache-1.demandAccesses::"
                "processor.cores1.core.data 2506\n",
                "board.cache_hierarchy.l2-cache-1.demandAccesses::"
                "processor.cores1.core.data 2500\n",
            ).replace(
                "board.cache_hierarchy.l2-cache-1.demandAccesses::total 2520\n",
                "board.cache_hierarchy.l2-cache-1.demandAccesses::total 2514\n",
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
            for core, hits, misses in ((0, 46, 4), (1, 44, 6)):
                root_name = f"board.cache_hierarchy.l1d-cache-{core}"
                requestor = f"processor.cores{core}.core.data"
                text = text.replace(
                    f"{root_name}.demandMisses::{requestor} {misses}\n",
                    "",
                ).replace(
                    f"{root_name}.demandMisses::total {misses}\n",
                    "",
                ).replace(
                    f"{root_name}.demandAccesses::{requestor} 50\n",
                    f"{root_name}.demandAccesses::{requestor} {hits}\n",
                ).replace(
                    f"{root_name}.demandAccesses::total 50\n",
                    f"{root_name}.demandAccesses::total {hits}\n",
                )
            stats.write_text(text, encoding="utf-8")
            self.mutate_summary(
                root,
                "200ns",
                lambda row: row["benchmark"] == "pr"
                and row["kind"] == "baseline",
                l1d_demand_misses="0",
            )
            result = self.validator.validate_sweep(root)
            self.assertEqual(result.row_count, 48)

    def test_rejects_deleted_nonzero_demand_family_reported_as_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_sweep(root)
            stats = root / "500ns" / "bc" / "cxl_vanilla" / "stats.txt"
            text = stats.read_text(encoding="utf-8")
            text = text.replace(
                "board.cache_hierarchy.l2-cache-0.demandMisses::"
                "processor.cores0.core.data 2500\n",
                "",
            )
            stats.write_text(text, encoding="utf-8")
            self.mutate_summary(
                root,
                "500ns",
                lambda row: row["benchmark"] == "bc"
                and row["kind"] == "baseline",
                l2d_demand_misses="0",
                l2i_demand_misses="0",
            )
            with self.assertRaisesRegex(
                self.validator.ValidationError,
                "demandMisses family total",
            ):
                self.validator.validate_sweep(root)

    def test_full_matrix_rejects_legacy_cache_requestor_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_sweep(root)
            stats = root / "2us" / "sssp" / "cxl_vanilla" / "stats.txt"
            stats.write_text(
                stats.read_text(encoding="utf-8").replace(
                    "processor.cores0.core.data 46",
                    "processor.cores.core.data 46",
                    1,
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                self.validator.ValidationError,
                "omitted nonzero demandHits",
            ):
                self.validator.validate_sweep(root)

    def test_full_matrix_rejects_unknown_cache_requestor(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_sweep(root)
            stats = root / "2us" / "sssp" / "cxl_vanilla" / "stats.txt"
            stats.write_text(
                stats.read_text(encoding="utf-8").replace(
                    "processor.cores0.core.data 46",
                    "procesor.cores0.core.data 46",
                    1,
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                self.validator.ValidationError,
                "omitted nonzero demandHits",
            ):
                self.validator.validate_sweep(root)

    def test_rejects_adjacent_large_integer_counter_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_sweep(root)
            stats = root / "200ns" / "bfs" / "cxl_vanilla" / "stats.txt"
            text = stats.read_text(encoding="utf-8").replace(
                "board.cxl_mem_link0.cpu_side_port 60\n",
                "board.cxl_mem_link0.cpu_side_port 9007199254740993\n",
                1,
            )
            stats.write_text(text, encoding="utf-8")
            summary = root / "200ns" / "summary.csv"
            with summary.open(newline="", encoding="utf-8") as stream:
                reader = csv.DictReader(stream)
                fields = reader.fieldnames
                rows = list(reader)
            rows[0]["cxl_packets"] = "9007199254741069"
            with summary.open("w", newline="", encoding="utf-8") as stream:
                writer = csv.DictWriter(stream, fieldnames=fields)
                writer.writeheader()
                writer.writerows(rows)
            with self.assertRaisesRegex(
                self.validator.ValidationError,
                "9007199254741069 != exact first-ROI value "
                "9007199254741070",
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
                "missing required ROI statistic: board.cira.avgLatency",
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
                    "cira_total_latency=999 != exact first-ROI value 0",
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

    def test_rejects_noncanonical_graph_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_sweep(root)
            self.mutate_summary(
                root,
                "200ns",
                lambda row: row["benchmark"] == "bfs"
                and row["kind"] == "baseline",
                graph_sha256="0" * 64,
            )
            with self.assertRaisesRegex(
                self.validator.ValidationError, "graph_sha256"
            ):
                self.validator.validate_sweep(root)

    def test_rejects_manifest_identity_or_missing_payload(self):
        for case in ("identity", "manifest", "payload"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                self.make_sweep(root)
                summary = root / "200ns" / "summary.csv"
                with summary.open(newline="", encoding="utf-8") as stream:
                    row = next(csv.DictReader(stream))
                manifest = Path(row["checkpoint_manifest"])
                if case == "identity":
                    payload = json.loads(manifest.read_text(encoding="utf-8"))
                    payload["identity"]["cores"] = 1
                    manifest.write_text(
                        json.dumps(payload), encoding="utf-8"
                    )
                    expected = "manifest checkpoint id mismatch"
                elif case == "manifest":
                    manifest.unlink()
                    expected = "missing checkpoint manifest"
                else:
                    (manifest.parent / "m5.cpt").unlink()
                    expected = "missing checkpoint payload"
                with self.assertRaisesRegex(
                    self.validator.ValidationError, expected
                ):
                    self.validator.validate_sweep(root)

    def test_rejects_checkpoint_directory_name_or_dependency_hash(self):
        for case in ("directory", "dependency"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                self.make_sweep(root)
                summary = root / "200ns" / "summary.csv"
                with summary.open(newline="", encoding="utf-8") as stream:
                    reader = csv.DictReader(stream)
                    fields = reader.fieldnames
                    rows = list(reader)
                row = rows[0]
                manifest = Path(row["checkpoint_manifest"])
                if case == "directory":
                    wrong_root = manifest.parent.parent / "wrong-id"
                    wrong_root.mkdir()
                    for name in ("manifest.json", "m5.cpt"):
                        (wrong_root / name).write_bytes(
                            (manifest.parent / name).read_bytes()
                        )
                    row["checkpoint_manifest"] = str(
                        wrong_root / "manifest.json"
                    )
                    log = Path(row["run_dir"]) / "gem5.log"
                    log.write_text(
                        log.read_text(encoding="utf-8").replace(
                            str(manifest.parent), str(wrong_root), 1
                        ),
                        encoding="utf-8",
                    )
                    expected = "directory name"
                else:
                    payload = json.loads(
                        manifest.read_text(encoding="utf-8")
                    )
                    dependency = Path(
                        next(
                            iter(
                                payload["identity"][
                                    "config_dependencies"
                                ]
                            )
                        )
                    )
                    dependency.write_bytes(b"changed-state")
                    expected = "config dependency hash mismatch"
                if case == "directory":
                    with summary.open(
                        "w", newline="", encoding="utf-8"
                    ) as stream:
                        writer = csv.DictWriter(stream, fieldnames=fields)
                        writer.writeheader()
                        writer.writerows(rows)
                with self.assertRaisesRegex(
                    self.validator.ValidationError, expected
                ):
                    self.validator.validate_sweep(root)

    def test_rejects_zero_or_duplicate_restore_marker(self):
        for count in (0, 2):
            with self.subTest(count=count), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                self.make_sweep(root)
                log = root / "200ns" / "bfs" / "cxl_vanilla" / "gem5.log"
                lines = [
                    line
                    for line in log.read_text(encoding="utf-8").splitlines()
                    if not line.startswith("GAPBS_CHECKPOINT_RESTORED path=")
                ]
                marker = "GAPBS_CHECKPOINT_RESTORED path=/checkpoint"
                log.write_text(
                    "\n".join([marker] * count + lines) + "\n",
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(
                    self.validator.ValidationError, "restore marker count"
                ):
                    self.validator.validate_sweep(root)

    def test_rejects_restore_marker_for_different_checkpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_sweep(root)
            log = root / "200ns" / "bfs" / "cxl_vanilla" / "gem5.log"
            text = log.read_text(encoding="utf-8")
            marker = next(
                line
                for line in text.splitlines()
                if line.startswith("GAPBS_CHECKPOINT_RESTORED path=")
            )
            log.write_text(
                text.replace(
                    marker,
                    f"GAPBS_CHECKPOINT_RESTORED path={root / 'other'}",
                    1,
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                self.validator.ValidationError,
                "restore marker path",
            ):
                self.validator.validate_sweep(root)

    def test_rejects_missing_strict_m5_exit_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_sweep(root)
            log = root / "200ns" / "bfs" / "cxl_vanilla" / "gem5.log"
            log.write_text(
                log.read_text(encoding="utf-8").replace(
                    "GAPBS_VERIFICATION_EXIT_CAUSE "
                    "cause=m5_exit instruction encountered\n",
                    "",
                    1,
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                self.validator.ValidationError,
                "strict m5_exit marker",
            ):
                self.validator.validate_sweep(root)

    def test_rejects_missing_or_different_serialized_graph_argument(self):
        for case in ("missing", "different"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                self.make_sweep(root)
                summary = root / "200ns" / "summary.csv"
                with summary.open(newline="", encoding="utf-8") as stream:
                    row = next(csv.DictReader(stream))
                config = Path(row["run_dir"]) / "config.ini"
                graph = row["graph_path"]
                if case == "missing":
                    replacement = ""
                else:
                    replacement = f"-f {root / 'other.sg'}"
                config.write_text(
                    config.read_text(encoding="utf-8").replace(
                        f"-f {graph}", replacement, 1
                    ),
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(
                    self.validator.ValidationError, "exact argv"
                ):
                    self.validator.validate_sweep(root)

    def test_rejects_missing_verify_or_extra_workload_argument(self):
        for old, new in (
            (" -v", ""),
            (" -v", " -v --extra"),
        ):
            with self.subTest(new=new), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                self.make_sweep(root)
                config = (
                    root
                    / "200ns"
                    / "bfs"
                    / "cxl_vanilla"
                    / "config.ini"
                )
                config.write_text(
                    config.read_text(encoding="utf-8").replace(old, new, 1),
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(
                    self.validator.ValidationError,
                    "exact argv",
                ):
                    self.validator.validate_sweep(root)

    def test_rejects_extra_core_or_wrong_openmp_thread_count(self):
        mutations = (
            (
                "\n[board.processor.cores2.core]\n"
                "type=BaseTimingSimpleCPU\n",
                "exact two-core",
            ),
            (
                "env=OMP_NUM_THREADS=1\n",
                "OMP_NUM_THREADS=2",
            ),
        )
        for addition, expected in mutations:
            with (
                self.subTest(addition=addition),
                tempfile.TemporaryDirectory() as tmp,
            ):
                root = Path(tmp)
                self.make_sweep(root)
                config = (
                    root
                    / "200ns"
                    / "bfs"
                    / "cxl_vanilla"
                    / "config.ini"
                )
                text = config.read_text(encoding="utf-8")
                if addition.startswith("env="):
                    text = text.replace(
                        "env=OMP_NUM_THREADS=2\n", addition, 1
                    )
                else:
                    text += addition
                config.write_text(text, encoding="utf-8")
                with self.assertRaisesRegex(
                    self.validator.ValidationError, expected
                ):
                    self.validator.validate_sweep(root)

    def test_rejects_unproven_workload_environment(self):
        for label, extra in (
            ("cxl_vanilla", "OMP_THREAD_LIMIT=1"),
            ("cxl_vanilla", "OMP_DYNAMIC=TRUE"),
            ("cira_pgo", "CIRA_GAPBS_DEVICE_OFFLOAD=1"),
        ):
            with self.subTest(extra=extra), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                self.make_sweep(root)
                config = root / "200ns" / "bfs" / label / "config.ini"
                config.write_text(
                    config.read_text(encoding="utf-8").replace(
                        "env=OMP_NUM_THREADS=2",
                        f"env=OMP_NUM_THREADS=2 {extra}",
                        1,
                    ),
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(
                    self.validator.ValidationError,
                    "exact workload environment",
                ):
                    self.validator.validate_sweep(root)

    def test_rejects_wrong_kind_specific_accelerator_topology(self):
        cases = (
            ("amu", "[board.asmc]", "[board.missing_asmc]", "ASMC"),
            (
                "baseline",
                "",
                "[board.asmc]\n"
                "mem_side_port="
                "board.cache_hierarchy.membus.cpu_side_ports[0]\n",
                "must not contain ASMC",
            ),
            ("cira", "[board.cira]", "[board.missing_cira]", "CIRA"),
        )
        for label, old, new, expected in cases:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                self.make_sweep(root)
                config = root / "200ns" / "bfs" / (
                    {
                        "baseline": "cxl_vanilla",
                        "amu": "amu",
                        "cira": "cira_pgo",
                    }[label]
                ) / "config.ini"
                text = config.read_text(encoding="utf-8")
                config.write_text(
                    text.replace(old, new, 1) if old else text + new,
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(
                    self.validator.ValidationError, expected
                ):
                    self.validator.validate_sweep(root)

    def test_rejects_accelerator_parameter_mismatch(self):
        cases = (
            ("amu", "max_outstanding=256", "max_outstanding=255", "ASMC"),
            (
                "cira_pgo",
                "max_send_queue=1024",
                "max_send_queue=1000",
                "CIRA",
            ),
        )
        for label, old, new, expected in cases:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                self.make_sweep(root)
                config = root / "200ns" / "bfs" / label / "config.ini"
                config.write_text(
                    config.read_text(encoding="utf-8").replace(old, new, 1),
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(
                    self.validator.ValidationError, expected
                ):
                    self.validator.validate_sweep(root)

    def test_rejects_zero_or_extra_stats_sections(self):
        for case in ("zero", "extra"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                self.make_sweep(root)
                stats = (
                    root
                    / "200ns"
                    / "bfs"
                    / "cxl_vanilla"
                    / "stats.txt"
                )
                if case == "zero":
                    stats.write_text("no stats\n", encoding="utf-8")
                    expected = "missing simulation statistics section"
                else:
                    with stats.open("a", encoding="utf-8") as stream:
                        stream.write(
                            "---------- Begin Simulation Statistics "
                            "----------\n"
                            "simTicks 300\n"
                            "---------- End Simulation Statistics   "
                            "----------\n"
                        )
                    expected = "found 3 complete sections"
                with self.assertRaisesRegex(
                    self.validator.ValidationError, expected
                ):
                    self.validator.validate_sweep(root)

    def test_rejects_switch_marker_count_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_sweep(root)
            log = root / "200ns" / "bfs" / "cxl_vanilla" / "gem5.log"
            log.write_text(
                log.read_text(encoding="utf-8")
                + "Switching from fast-forward CPU to timing CPU!\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                self.validator.ValidationError, "cpu_switches"
            ):
                self.validator.validate_sweep(root)

    def test_rejects_config_workload_shape_mismatch(self):
        for option, old, replacement in (
            ("graph", "-f ", "-g 20 "),
            ("iterations", "-n 2", "-n 1"),
        ):
            with self.subTest(option=option), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                self.make_sweep(root)
                config = root / "200ns" / "bfs" / "cxl_vanilla" / "config.ini"
                config.write_text(
                    config.read_text(encoding="utf-8").replace(
                        old, replacement, 1
                    ),
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(
                    self.validator.ValidationError,
                    "exact argv",
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
                "expected Timing",
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
                    "board.cache_hierarchy.l2buses0.cpu_side_ports[2]"
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
                            "processor.cores0.core.data 2500",
                            "processor.cores0.core.data 2048",
                        )
                        .replace(
                            "processor.cores1.core.data 2500",
                            "processor.cores1.core.data 2048",
                        )
                        .replace(
                            "processor.cores0.core.data 2505",
                            "processor.cores0.core.data 2053",
                        )
                        .replace(
                            "processor.cores1.core.data 2506",
                            "processor.cores1.core.data 2054",
                        )
                        .replace(
                            "demandMisses::total 2507",
                            "demandMisses::total 2055",
                        )
                        .replace(
                            "demandAccesses::total 2518",
                            "demandAccesses::total 2066",
                        )
                        .replace(
                            "demandAccesses::total 2520",
                            "demandAccesses::total 2068",
                        ),
                        encoding="utf-8",
                    )
                elif case == "cira_not_lower":
                    cira["l2d_demand_misses"] = "5000"
                    path = gate / "pr" / "cira_pgo" / "stats.txt"
                    path.write_text(
                        path.read_text(encoding="utf-8")
                        .replace(
                            "processor.cores0.core.data 2000",
                            "processor.cores0.core.data 2500",
                        )
                        .replace(
                            "processor.cores1.core.data 2000",
                            "processor.cores1.core.data 2500",
                        )
                        .replace(
                            "processor.cores0.core.data 2005",
                            "processor.cores0.core.data 2505",
                        )
                        .replace(
                            "processor.cores1.core.data 2006",
                            "processor.cores1.core.data 2506",
                        )
                        .replace(
                            "demandMisses::total 2007",
                            "demandMisses::total 2507",
                        )
                        .replace(
                            "demandAccesses::total 2018",
                            "demandAccesses::total 2518",
                        )
                        .replace(
                            "demandAccesses::total 2020",
                            "demandAccesses::total 2520",
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

    def test_paired_outputs_reject_resolve_equivalent_paths_before_io(self):
        result = self.validator.ValidationResult(
            1,
            0,
            0,
            [{"latency": "1us", "status": "ok"}],
            "full",
        )
        for alias_kind in (
            "exact",
            "relative",
            "symlink-parent",
            "symlink-file",
        ):
            with (
                self.subTest(alias_kind=alias_kind),
                tempfile.TemporaryDirectory() as tmp,
            ):
                root = Path(tmp)
                real = root / "real"
                real.mkdir()
                combined = real / "evidence.out"
                combined.write_text("old evidence\n", encoding="utf-8")
                if alias_kind == "exact":
                    validation = combined
                elif alias_kind == "relative":
                    validation = Path(
                        os.path.relpath(combined, Path.cwd())
                    )
                elif alias_kind == "symlink-parent":
                    alias = root / "alias"
                    alias.symlink_to(real, target_is_directory=True)
                    validation = alias / combined.name
                else:
                    validation = root / "alias.out"
                    validation.symlink_to(combined)
                with (
                    mock.patch.object(
                        self.validator.tempfile,
                        "mkstemp",
                        wraps=self.validator.tempfile.mkstemp,
                    ) as make_temp,
                    mock.patch.object(
                        self.validator.os,
                        "replace",
                        wraps=self.validator.os.replace,
                    ) as replace,
                ):
                    with self.assertRaisesRegex(
                        self.validator.ValidationError,
                        "distinct paths",
                    ):
                        self.validator.write_outputs(
                            result,
                            combined_output=combined,
                            validation_output=validation,
                        )
                make_temp.assert_not_called()
                replace.assert_not_called()
                self.assertEqual(
                    combined.read_text(encoding="utf-8"),
                    "old evidence\n",
                )
                self.assertEqual(list(root.rglob(".*.tmp-*")), [])

    def test_cli_rejects_same_output_path_without_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sweep = root / "sweep"
            self.make_sweep(sweep)
            output = root / "evidence.out"
            output.write_text("old evidence\n", encoding="utf-8")
            result = subprocess.run(
                [
                    sys.executable,
                    str(VALIDATOR_PATH),
                    str(sweep),
                    "--combined-output",
                    str(output),
                    "--validation-output",
                    str(output),
                ],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("distinct paths", result.stderr)
            self.assertEqual(
                output.read_text(encoding="utf-8"),
                "old evidence\n",
            )
            self.assertEqual(list(root.rglob(".*.tmp-*")), [])

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
