# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from scripts import calibrate_m2ndp_cxl as calibration


class CalibrationTest(unittest.TestCase):
    def test_selects_closest_link_latency_within_one_clock(self):
        def simulate(link_latency):
            return Decimal("100.0") + Decimal(link_latency) * Decimal("0.125")

        result = calibration.search_link_latency(
            target_ns=Decimal("1100.0"),
            link_period_ns=Decimal("0.125"),
            simulate=simulate,
            low=0,
            high=10000,
        )
        self.assertEqual(result.link_latency, 8000)
        self.assertLessEqual(
            abs(result.measured_ns - result.target_ns),
            result.link_period_ns,
        )
        self.assertGreater(len(result.samples), 0)

    def test_rejects_default_35ns_as_1us(self):
        with self.assertRaisesRegex(
            calibration.CalibrationError, "outside one link clock"
        ):
            calibration.require_residual(
                target_ns=Decimal("1000"),
                measured_ns=Decimal("35"),
                link_period_ns=Decimal("0.125"),
            )

    def test_parses_probe_markers_and_core_period(self):
        text = "\n".join(
            (
                "noise",
                "M2NDP_CXL_PROBE request_bytes=64 requests=1",
                "Memory request latency: 261",
            )
        )
        measured = calibration.parse_m2ndp_probe(
            text,
            returncode=0,
            expected_request_bytes=64,
            core_period_ns=Decimal("0.5"),
        )
        self.assertEqual(measured, Decimal("130.5"))

    def test_rejects_duplicate_probe_marker(self):
        text = "\n".join(
            (
                "M2NDP_CXL_PROBE request_bytes=64 requests=1",
                "M2NDP_CXL_PROBE request_bytes=64 requests=1",
                "Memory request latency: 261",
            )
        )
        with self.assertRaisesRegex(
            calibration.CalibrationError, "probe marker count"
        ):
            calibration.parse_m2ndp_probe(
                text,
                returncode=0,
                expected_request_bytes=64,
                core_period_ns=Decimal("0.5"),
            )

    def test_structurally_derives_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source"
            target = Path(tmp) / "target"
            source.mkdir()
            (source / "m2ndp.config").write_text(
                "ramulator_config=./LPDDR5-config.cfg\n"
                "cxl_link_config=./cxl_link.icnt\n"
                "local_cross_bar_config=./memory_buffer_crossbar.icnt\n"
                "max_kernel_launch=64\n"
                "freq=2000,2000,2000,8000,2000,800\n"
            )
            (source / "cxl_link.icnt").write_text(
                "link_latency = 274; // 35ns\n"
            )
            for name in (
                "LPDDR5-config.cfg",
                "memory_buffer_crossbar.icnt",
            ):
                (source / name).write_text("fixture\n")

            derived = calibration.derive_config(
                source, target, link_latency=8000
            )

            self.assertEqual(derived.core_period_ns, Decimal("0.5"))
            self.assertEqual(derived.link_period_ns, Decimal("0.125"))
            self.assertEqual(derived.official_link_latency, 274)
            self.assertIn(
                "max_kernel_launch=128",
                derived.config_path.read_text(),
            )
            self.assertIn(
                "link_latency = 8000; // 35ns",
                derived.link_path.read_text(),
            )
            first_hash = calibration.sha256_config_tree(target)
            calibration.set_link_latency(derived.link_path, 8002)
            self.assertNotEqual(
                first_hash,
                calibration.sha256_config_tree(target),
            )

    def test_parses_single_gem5_probe_request(self):
        stats = "\n".join(
            (
                "---------- Begin Simulation Statistics ----------",
                "simTicks 2100000",
                "board.cache_hierarchy.membus.transDist::ReadResp 1",
                "board.cache_hierarchy.membus.transDist::ReadSharedReq 1",
                "board.cache_hierarchy.membus.pktCount_"
                "board.cache_hierarchy.l2-cache-0.mem_side_port::"
                "board.cxl_mem_link0.cpu_side_port 2",
                "board.cache_hierarchy.membus.pktSize_"
                "board.cache_hierarchy.l2-cache-0.mem_side_port::"
                "board.cxl_mem_link0.cpu_side_port 64",
                "---------- End Simulation Statistics   ----------",
            )
        )
        evidence = calibration.parse_gem5_probe_stats(stats)
        self.assertEqual(evidence.sim_ticks, 2100000)
        self.assertEqual(evidence.request_count, 1)
        self.assertEqual(evidence.response_count, 1)
        self.assertEqual(evidence.round_trip_packets, 2)
        self.assertEqual(evidence.request_bytes, 64)
        self.assertEqual(evidence.target_ns, Decimal("2100"))

    def test_rejects_gem5_probe_without_one_read_response(self):
        stats = "\n".join(
            (
                "---------- Begin Simulation Statistics ----------",
                "simTicks 2100000",
                "board.cache_hierarchy.membus.transDist::ReadSharedReq 1",
                "board.cache_hierarchy.membus.pktCount_"
                "board.cache_hierarchy.l2-cache-0.mem_side_port::"
                "board.cxl_mem_link0.cpu_side_port 2",
                "board.cache_hierarchy.membus.pktSize_"
                "board.cache_hierarchy.l2-cache-0.mem_side_port::"
                "board.cxl_mem_link0.cpu_side_port 64",
                "---------- End Simulation Statistics   ----------",
            )
        )
        with self.assertRaisesRegex(
            calibration.CalibrationError, "read response count"
        ):
            calibration.parse_gem5_probe_stats(stats)


if __name__ == "__main__":
    unittest.main()
