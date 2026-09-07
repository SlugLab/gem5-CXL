#!/usr/bin/env python3

import copy
import unittest

from scripts import publish_g12_fast_spectrum as publisher


class PublishG12FastSpectrumTest(unittest.TestCase):
    def setUp(self):
        measured = {}
        for workload in publisher.WORKLOADS:
            for latency in publisher.LATENCIES:
                measured[f"{workload}:{latency}"] = {
                    "status": "pass",
                    "cycles": 100,
                    "core_period_ns": "0.5",
                    "selected_link_latency": 1397,
                    "calibration_residual_ns": "0.004",
                    "evidence_path": f"/{workload}/{latency}/evidence.json",
                    "evidence_sha256": ("a" if workload == "pr_spmv" else "b") * 64,
                }
        anchors = {}
        models = {}
        for workload in publisher.WORKLOADS:
            anchors[workload] = {}
            models[workload] = {}
            for system in publisher.ANCHOR_SYSTEMS:
                anchors[workload][system] = {
                    "graph_sha256": publisher.G12_GRAPH_SHA256,
                    "latency": "1us",
                    "time_ns": "1000",
                    "entry_count": 20,
                    "compute_ticks_per_core": "10;11;12;13",
                    "queue_stall_ticks_per_core": "1;2;3;4",
                    "evidence_sha256": "c" * 64,
                }
                models[workload][system] = {
                    "anchor_latency": "1us",
                    "factors": {
                        "200ns": "0.7", "500ns": "0.8", "1us": "1",
                        "2us": "1.3",
                    },
                    "model_sha256": "d" * 64,
                }
        self.spec = {
            "graph_sha256": publisher.G12_GRAPH_SHA256,
            "measured": measured,
            "anchors": anchors,
            "models": models,
        }

    def test_measured_and_derived_rows_are_distinct(self):
        rows = publisher.build_rows(self.spec)
        self.assertEqual(
            {row["measurement_kind"] for row in rows},
            {"measured", "calibrated-derived"},
        )
        self.assertEqual(len(rows), 24)
        m2ndp = next(row for row in rows if row["system"] == "m2ndp")
        self.assertEqual(m2ndp["selected_link_latency"], "1397")
        self.assertEqual(m2ndp["calibration_residual_ns"], "0.004")
        cira = next(row for row in rows if row["system"] == "cira")
        self.assertEqual(cira["compute_ticks_per_core"], "10;11;12;13")

    def test_missing_model_stays_pending(self):
        spec = copy.deepcopy(self.spec)
        del spec["models"]["pr_spmv"]["cira"]
        rows = publisher.build_rows(spec)
        row = next(
            row for row in rows
            if row["workload"] == "pr_spmv"
            and row["system"] == "cira"
            and row["latency"] == "200ns"
        )
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["time_ns"], "")

    def test_g20_anchor_is_rejected(self):
        spec = copy.deepcopy(self.spec)
        spec["anchors"]["gap_bc"]["host_inline"]["graph_sha256"] = (
            "ce900a7147a073835a7450e8f1afedf9f13db6833652bf2f9647819be26bedb3"
        )
        with self.assertRaisesRegex(publisher.PublishError, "anchor graph identity differs"):
            publisher.build_rows(spec)


if __name__ == "__main__":
    unittest.main()
