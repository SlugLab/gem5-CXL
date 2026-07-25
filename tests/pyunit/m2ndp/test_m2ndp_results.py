# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

import csv
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from scripts import m2ndp_artifacts as artifacts
from scripts import m2ndp_results as results


class M2NDPResultTest(unittest.TestCase):
    def setUp(self):
        self.funcsim_pass = results.FuncSimEvidence(
            passed=True,
            compared=3,
            mismatched=0,
            dump_sha256="a" * 64,
        )
        self.funcsim_fail = results.FuncSimEvidence(
            passed=False,
            compared=3,
            mismatched=1,
            dump_sha256="b" * 64,
        )
        self.ndpsim_pass = results.NDPSimEvidence(
            start_cycle=110,
            end_cycle=350,
            measured_cycles=240,
            core_period_seconds=Decimal("5e-10"),
        )
        self.calibration_pass = results.CalibrationEvidence(
            passed=True,
            request_bytes=64,
            target_ns=Decimal("1100"),
            measured_ns=Decimal("1100.0625"),
            residual_ns=Decimal("0.0625"),
            link_period_ns=Decimal("0.125"),
            config_sha256="c" * 64,
        )
        self.gem5_pass = results.Gem5Evidence(
            row={
                "benchmark": "pr_spmv",
                "kind": "baseline",
                "status": "ok",
                "verification": "pass",
                "roi_cpu": "timing",
                "cores": "2",
                "cxl_link_delay": "1us",
                "all_memory_cxl": "True",
                "graph_sha256": artifacts.EXPECTED_G20_SHA256,
                "iterations": "2",
                "measured_trial": "1",
                "checkpoint_restores": "1",
                "sim_ticks": "240000",
            },
            sim_ticks=240000,
        )
        self.provenance = results.ProvenanceEvidence(
            graph_sha256=artifacts.EXPECTED_G20_SHA256,
            gem5_binary_sha256="d" * 64,
            trace_sha256="e" * 64,
            m2ndp_patch_sha256="f" * 64,
            m2ndp_config_sha256="c" * 64,
            reference_raw_sha256="a" * 64,
            funcsim_dump_sha256="a" * 64,
        )

    def strict_log(self):
        return "\n".join(
            [
                "M2NDP_STRICT_MODE=1",
                "M2NDP_STRICT_COMPARED=3",
                "M2NDP_STRICT_MISMATCHED=0",
                "M2NDP_STRICT_MATCH=PASS",
            ]
        )

    def test_parse_strict_funcsim_pass(self):
        evidence = results.parse_funcsim(
            self.strict_log(), returncode=0, expected_count=3
        )
        self.assertTrue(evidence.passed)
        self.assertEqual(evidence.compared, 3)

    def test_parse_funcsim_rejects_missing_duplicate_and_nonzero(self):
        for log, returncode, message in (
            (
                self.strict_log().replace("M2NDP_STRICT_MODE=1\n", ""),
                0,
                "MODE",
            ),
            (
                self.strict_log() + "\nM2NDP_STRICT_MATCH=PASS",
                0,
                "MATCH",
            ),
            (self.strict_log(), 2, "exit"),
        ):
            with self.subTest(message=message):
                with self.assertRaisesRegex(
                    artifacts.EvidenceError, message
                ):
                    results.parse_funcsim(
                        log, returncode=returncode, expected_count=3
                    )

    def test_parse_funcsim_rejects_wrong_compared_count(self):
        with self.assertRaisesRegex(
            artifacts.EvidenceError, "compared"
        ):
            results.parse_funcsim(
                self.strict_log(), returncode=0, expected_count=4
            )

    def test_reference_dump_rejects_one_bit_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            expected = root / "expected.u32"
            actual = root / "actual.u32"
            expected.write_bytes(b"\x01\x00\x00\x00")
            actual.write_bytes(b"\x00\x00\x00\x00")
            with self.assertRaisesRegex(
                artifacts.EvidenceError, "bit-exact"
            ):
                results.validate_reference_dump(
                    expected, actual, expected_count=1
                )

    def test_parse_ndpsim_uses_trial_one_only(self):
        log = "\n".join(
            [
                "CORE period: 5e-10 DRAM period: 5e-10",
                "Launching NDP kernel: /t/K0_INIT at cycle 10",
                (
                    "Launching NDP kernel: /t/K0_INIT_TRIAL1 "
                    "at cycle 110"
                ),
                "EXPR FINISHED 350",
            ]
        )
        evidence = results.parse_ndpsim(log, returncode=0)
        self.assertEqual(evidence.start_cycle, 110)
        self.assertEqual(evidence.end_cycle, 350)
        self.assertEqual(evidence.measured_cycles, 240)
        self.assertEqual(
            evidence.core_period_seconds, Decimal("5e-10")
        )

    def test_parse_ndpsim_rejects_duplicate_and_reordered_markers(self):
        duplicate = "\n".join(
            [
                "CORE period: 5e-10",
                "K0_INIT_TRIAL1 at cycle 10",
                "K0_INIT_TRIAL1 at cycle 20",
                "EXPR FINISHED 30",
            ]
        )
        reordered = "\n".join(
            [
                "CORE period: 5e-10",
                "EXPR FINISHED 30",
                "K0_INIT_TRIAL1 at cycle 10",
            ]
        )
        for log in (duplicate, reordered):
            with self.subTest(log=log):
                with self.assertRaises(artifacts.EvidenceError):
                    results.parse_ndpsim(log, returncode=0)

    def test_parse_gem5_summary_requires_exact_baseline_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "summary.csv"
            with path.open("w", newline="") as stream:
                writer = csv.DictWriter(
                    stream, fieldnames=list(self.gem5_pass.row)
                )
                writer.writeheader()
                writer.writerow(self.gem5_pass.row)
            evidence = results.parse_gem5_summary(path)
        self.assertEqual(evidence.sim_ticks, 240000)

    def test_parse_gem5_summary_rejects_old_pr_row(self):
        row = dict(self.gem5_pass.row, benchmark="pr")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "summary.csv"
            with path.open("w", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=list(row))
                writer.writeheader()
                writer.writerow(row)
            with self.assertRaisesRegex(
                artifacts.EvidenceError, "benchmark"
            ):
                results.parse_gem5_summary(path)

    def test_smoke_mode_accepts_non_g20_hash_without_weakening_default(self):
        smoke_hash = "1" * 64
        row = dict(self.gem5_pass.row, graph_sha256=smoke_hash)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "summary.csv"
            with path.open("w", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=list(row))
                writer.writeheader()
                writer.writerow(row)
            with self.assertRaisesRegex(
                artifacts.EvidenceError, "graph_sha256"
            ):
                results.parse_gem5_summary(path)
            evidence = results.parse_gem5_summary(
                path, smoke_test=True
            )
        self.assertEqual(evidence.row["graph_sha256"], smoke_hash)

        provenance = results.ProvenanceEvidence(
            **{
                **self.provenance.__dict__,
                "graph_sha256": smoke_hash,
            }
        )
        smoke_gem5 = results.Gem5Evidence(row=row, sim_ticks=240000)
        summary = results.build_summary(
            gem5=smoke_gem5,
            funcsim=self.funcsim_pass,
            ndpsim=self.ndpsim_pass,
            calibration=self.calibration_pass,
            provenance=provenance,
            smoke_test=True,
        )
        self.assertEqual(summary["graph_sha256"], smoke_hash)

    def test_speedup_is_suppressed_when_funcsim_fails(self):
        with self.assertRaisesRegex(
            artifacts.EvidenceError, "FuncSim"
        ):
            results.build_summary(
                gem5=self.gem5_pass,
                funcsim=self.funcsim_fail,
                ndpsim=self.ndpsim_pass,
                calibration=self.calibration_pass,
            )

    def test_summary_computes_seconds_without_hardcoded_ndp_clock(self):
        row = results.build_summary(
            gem5=self.gem5_pass,
            funcsim=self.funcsim_pass,
            ndpsim=self.ndpsim_pass,
            calibration=self.calibration_pass,
            provenance=self.provenance,
        )
        self.assertEqual(row["gem5_seconds"], "2.4E-7")
        self.assertEqual(row["m2ndp_seconds"], "1.200E-7")
        self.assertEqual(row["speedup"], "2")

    def test_summary_rejects_config_outside_calibration(self):
        bad = results.ProvenanceEvidence(
            **{
                **self.provenance.__dict__,
                "m2ndp_config_sha256": "9" * 64,
            }
        )
        with self.assertRaisesRegex(
            artifacts.EvidenceError, "calibration"
        ):
            results.build_summary(
                gem5=self.gem5_pass,
                funcsim=self.funcsim_pass,
                ndpsim=self.ndpsim_pass,
                calibration=self.calibration_pass,
                provenance=bad,
            )


if __name__ == "__main__":
    unittest.main()
