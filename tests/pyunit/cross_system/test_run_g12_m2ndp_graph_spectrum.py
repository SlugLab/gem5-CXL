#!/usr/bin/env python3

import json
import tempfile
import unittest
from pathlib import Path

from scripts import run_g12_m2ndp_graph_spectrum as runner


class G12M2NDPGraphSpectrumTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.inputs = self.root / "inputs.json"
        self.registry = self.root / "registry.json"
        graph = {
            "scale": 12,
            "num_nodes": 4096,
            "directed_edges": 96772,
            "sha256": runner.G12_GRAPH_SHA256,
        }
        self.inputs.write_text(json.dumps({
            "schema": 1,
            "status": "accepted",
            "graph": graph,
            "workloads": {
                "pr_spmv": {**graph, "input_sha256": "a" * 64},
                "gap_bc": {**graph, "input_sha256": "b" * 64},
            },
        }))
        cells = {}
        for workload in runner.WORKLOADS:
            for latency in runner.LATENCIES:
                cells[f"{workload}:{latency}"] = {
                    "workload": workload,
                    "latency": latency,
                    "graph_sha256": runner.G12_GRAPH_SHA256,
                    "input_sha256": "a" * 64 if workload == "pr_spmv" else "b" * 64,
                }
        self.registry.write_text(json.dumps({
            "schema": 1,
            "status": "verified",
            "graph": graph,
            "cells": cells,
        }))

    def tearDown(self):
        self.temporary.cleanup()

    def test_contract_accepts_only_the_frozen_g12_graph(self):
        contract = runner.load_contract(self.inputs, self.registry)
        self.assertEqual(contract["graph_scale"], 12)
        self.assertEqual(contract["graph_sha256"], runner.G12_GRAPH_SHA256)
        self.assertEqual(len(contract["cells"]), 8)

    def test_g20_graph_is_rejected(self):
        value = json.loads(self.inputs.read_text())
        value["graph"]["sha256"] = (
            "ce900a7147a073835a7450e8f1afedf9f13db6833652bf2f9647819be26bedb3"
        )
        self.inputs.write_text(json.dumps(value))
        with self.assertRaisesRegex(runner.SpectrumError, "G12 graph identity differs"):
            runner.load_contract(self.inputs, self.registry)

    def test_ndpsim_requires_functional_and_memory_gates(self):
        row = {
            "status": "pass",
            "verification": "pass",
            "bit_exact": True,
            "memory_match": "fail",
            "cxl_link_delay": "200ns",
            "completed_launches": 1,
            "expected_launches": 1,
            "cycles": 10,
            "calibration": {"passed": True, "cxl_delay": "200ns"},
            "serial_launch": False,
        }
        with self.assertRaisesRegex(runner.SpectrumError, "memory match"):
            runner.validate_evidence(row, "pr_spmv", "200ns")

    def test_evidence_requires_matching_launch_and_latency(self):
        row = {
            "status": "pass",
            "verification": "pass",
            "bit_exact": True,
            "memory_match": "pass",
            "cxl_link_delay": "1us",
            "completed_launches": 2,
            "expected_launches": 3,
            "cycles": 10,
            "calibration": {"passed": True, "cxl_delay": "1us"},
            "serial_launch": False,
        }
        with self.assertRaisesRegex(runner.SpectrumError, "launch count"):
            runner.validate_evidence(row, "gap_bc", "1us")

    def test_service_is_sequential_and_has_no_start_timeout(self):
        unit = runner.parse_service_unit(runner.SERVICE_UNIT)
        self.assertEqual(unit["Service"]["TimeoutStartSec"], "infinity")
        script = runner.SERVICE_SCRIPT.read_text(encoding="utf-8")
        self.assertNotIn("&", script)
        self.assertIn("pr_spmv gap_bc", script)
        self.assertIn("200ns 500ns 1us 2us", script)
        self.assertIn("publish_g12_fast_spectrum.py", script)
        self.assertIn("generate_g12_m2ndp_latency_spectrum.py", script)

    def test_cell_serialization_does_not_disable_partition_concurrency(self):
        self.assertEqual(runner.PR_NDPSIM_SERIAL_LAUNCH, "false")

    def test_gap_bc_cell_is_one_registry_selected_bfs_window(self):
        row = {
            "status": "pass",
            "verification": "pass",
            "bit_exact": True,
            "memory_match": "pass",
            "cxl_link_delay": "200ns",
            "completed_launches": 13,
            "expected_launches": 13,
            "cycles": 10,
            "calibration": {"passed": True, "cxl_delay": "200ns"},
        }
        with self.assertRaisesRegex(runner.SpectrumError, "selected BFS window"):
            runner.validate_evidence(row, "gap_bc", "200ns")


if __name__ == "__main__":
    unittest.main()
