# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

import csv
import json
import tempfile
import unittest
from pathlib import Path

from scripts import gapbs_pr_experiment_profiles as profiles
from scripts import generate_gapbs_g4_4thread_latency_results as publisher
from scripts import m2ndp_artifacts as artifacts
from scripts import run_gapbs_g4_4thread_latency_sweep as sweep_runner


LATENCY_BASE_TICKS = {
    "200ns": 1_000_000,
    "500ns": 2_000_000,
    "1us": 3_000_000,
    "2us": 4_000_000,
}


def make_valid_rows():
    rows = []
    for latency, base_ticks in LATENCY_BASE_TICKS.items():
        for system, divisor, speedup in (
            ("vanilla", 1, "1"),
            ("amu", 2, "2"),
            ("cira", 4, "4"),
            ("m2ndp", 5, "5"),
        ):
            is_m2ndp = system == "m2ndp"
            ticks = base_ticks // divisor
            row = {
                "profile": "g4-4thread-sweep",
                "benchmark": "pr_spmv",
                "latency": latency,
                "system": system,
                "graph_sha256": profiles.G4_SHA256,
                "cores": "4",
                "threads": "4",
                "trials": "2",
                "measured_trial": "1",
                "iterations": "20",
                "all_memory_cxl": "True",
                "verification": "pass",
                "bit_exact": "pass",
                "result_sha256": "a" * 64,
                "latency_seconds": str(ticks / 1_000_000_000_000),
                "speedup_vs_vanilla_cxl": speedup,
                "sim_ticks": "" if is_m2ndp else str(ticks),
                "measured_cycles": str(ticks) if is_m2ndp else "",
                "core_period_seconds": "1e-12" if is_m2ndp else "",
                "asmc_loads": "32" if system == "amu" else "0",
                "asmc_completed": "32" if system == "amu" else "0",
                "cira_prefetches": "64" if system == "cira" else "0",
                "cira_completed": "64" if system == "cira" else "0",
                "cira_indexed_prefetches": "0",
                "cira_csr_prefetches": "16" if system == "cira" else "0",
                "cira_issued_per_core": (
                    "16;16;16;16" if system == "cira" else ""
                ),
                "cira_completed_per_core": (
                    "16;16;16;16" if system == "cira" else ""
                ),
                "cira_csr_per_core": (
                    "4;4;4;4" if system == "cira" else ""
                ),
                "cira_rejected_queue_full": "0",
                "cira_dropped_csr_descriptors": "0",
                "cira_csr_queue_high_watermark": (
                    "16" if system == "cira" else "0"
                ),
                "funcsim_compared": "16" if is_m2ndp else "",
                "funcsim_mismatched": "0" if is_m2ndp else "",
                "calibration_pass": "pass" if is_m2ndp else "",
                "calibration_cxl_delay": latency if is_m2ndp else "",
                "calibration_residual_ns": "0.0625" if is_m2ndp else "",
                "calibration_link_period_ns": "0.125" if is_m2ndp else "",
                "source_path": f"runs/{latency}/{system}/summary.csv",
                "source_sha256": "b" * 64,
            }
            rows.append(row)
    return rows


def make_g14_vanilla_rows():
    ticks = {
        "200ns": "1000000",
        "500ns": "1200000",
        "1us": "1100000",
        "2us": "1800000",
    }
    rows = []
    for index, latency in enumerate(publisher.LATENCIES):
        rows.append(
            {
                "profile": "g14-4thread-sweep",
                "latency": latency,
                "system": "vanilla",
                "sim_ticks": ticks[latency],
                "mem_ctrl_read_reqs": str(1000 + index * 10),
                "mem_ctrl_read_bursts": str(990 + index * 10),
                "mem_ctrl_bytes_read": str(64000 + index * 640),
                "mem_ctrl_cpu_data_reads": str(900 + index * 9),
            }
        )
    return rows


def write_csv(path, row):
    artifacts.atomic_write_csv(path, tuple(row), [row])


def write_completed_sweep(root):
    state = sweep_runner.new_state()
    raw = bytes(range(64))
    for latency, base_ticks in LATENCY_BASE_TICKS.items():
        latency_root = root / "runs" / latency
        m2ndp = latency_root / "m2ndp"
        reference = m2ndp / "reference/scores.raw"
        dump = m2ndp / "funcsim/scores.u32"
        reference.parent.mkdir(parents=True)
        dump.parent.mkdir(parents=True)
        reference.write_bytes(raw)
        dump.write_bytes(raw)
        result_sha256 = artifacts.sha256_file(reference)

        vanilla_dir = m2ndp / "gem5/run/pr_spmv/cxl_vanilla"
        vanilla_dir.mkdir(parents=True)
        (vanilla_dir / "config.ini").write_text(
            f"delay={profiles.LATENCY_TICKS[latency]}\n",
            encoding="utf-8",
        )
        vanilla_summary = m2ndp / "gem5/run/summary.csv"
        write_csv(
            vanilla_summary,
            {
                "benchmark": "pr_spmv",
                "kind": "baseline",
                "status": "ok",
                "verification": "pass",
                "roi_cpu": "timing",
                "scale": "4",
                "cores": "4",
                "cxl_link_delay": latency,
                "all_memory_cxl": "True",
                "graph_sha256": profiles.G4_SHA256,
                "iterations": "2",
                "measured_trial": "1",
                "checkpoint_restores": "1",
                "sim_ticks": str(base_ticks),
                "run_dir": str(vanilla_dir),
            },
        )

        m2_summary = m2ndp / "summary.csv"
        measured_cycles = base_ticks // 5
        write_csv(
            m2_summary,
            {
                "profile": "g4-4thread-sweep",
                "benchmark": "pr_spmv",
                "graph_sha256": profiles.G4_SHA256,
                "iterations": "20",
                "trials": "2",
                "measured_trial": "1",
                "cores": "4",
                "all_memory_cxl": "True",
                "cxl_link_delay": latency,
                "verification": "pass",
                "funcsim_strict": "pass",
                "funcsim_compared": "16",
                "gem5_sim_ticks": str(base_ticks),
                "ndpsim_measured_cycles": str(measured_cycles),
                "ndpsim_core_period_seconds": "1e-12",
                "m2ndp_seconds": str(measured_cycles / 10**12),
                "speedup": "5",
            },
        )
        calibration = m2ndp / "calibration/calibration.json"
        calibration.parent.mkdir(parents=True)
        calibration.write_text(
            json.dumps(
                {
                    "passed": True,
                    "cxl_delay": latency,
                    "residual_ns": "0.0625",
                    "link_period_ns": "0.125",
                }
            ),
            encoding="utf-8",
        )

        for system, divisor in (("amu", 2), ("cira", 4)):
            run_dir = latency_root / system / f"pr_spmv/{system}_matched"
            run_dir.mkdir(parents=True)
            (run_dir / "config.ini").write_text(
                f"delay={profiles.LATENCY_TICKS[latency]}\n",
                encoding="utf-8",
            )
            summary = latency_root / system / "summary.csv"
            row = {
                "benchmark": "pr_spmv",
                "kind": system,
                "status": "ok",
                "verification": "pass",
                "scale": "4",
                "iterations": "2",
                "measured_trial": "1",
                "roi_cpu": "timing",
                "cores": "4",
                "cxl_link_delay": latency,
                "all_memory_cxl": "True",
                "graph_sha256": profiles.G4_SHA256,
                "checkpoint_restores": "1",
                "sim_ticks": str(base_ticks // divisor),
                "asmc_loads": "32" if system == "amu" else "0",
                "asmc_completed": "32" if system == "amu" else "0",
                "cira_prefetches": "64" if system == "cira" else "0",
                "cira_completed": "64" if system == "cira" else "0",
                "cira_indexed_prefetches": "0",
                "cira_csr_prefetches": "16" if system == "cira" else "0",
                "cira_issued_per_core": (
                    "16;16;16;16" if system == "cira" else ""
                ),
                "cira_completed_per_core": (
                    "16;16;16;16" if system == "cira" else ""
                ),
                "cira_csr_per_core": (
                    "4;4;4;4" if system == "cira" else ""
                ),
                "cira_rejected_queue_full": "0",
                "cira_dropped_csr_descriptors": "0",
                "cira_csr_queue_high_watermark": (
                    "16" if system == "cira" else "0"
                ),
                "run_dir": str(run_dir),
            }
            write_csv(summary, row)
            (latency_root / system / "evidence.json").write_text(
                json.dumps(
                    {
                        "profile": "g4-4thread-sweep",
                        "cxl_link_delay": latency,
                        "graph_sha256": profiles.G4_SHA256,
                        "runs": {
                            system: {
                                "reference_raw_sha256": result_sha256
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )

        sweep_runner.record_pass(
            state, latency, "vanilla", vanilla_summary
        )
        sweep_runner.record_pass(
            state, latency, "amu", latency_root / "amu/summary.csv"
        )
        sweep_runner.record_pass(
            state, latency, "cira", latency_root / "cira/summary.csv"
        )
        sweep_runner.record_pass(state, latency, "m2ndp", m2_summary)
    artifacts.atomic_write_json(root / "status.json", state)


class MatrixValidationTest(unittest.TestCase):
    def test_publication_requires_exact_16_row_matrix(self):
        rows = make_valid_rows()[:-1]
        with self.assertRaisesRegex(publisher.PublicationError, "16 rows"):
            publisher.validate_matrix(rows)

    def test_g14_vanilla_endpoints_require_real_cxl_and_positive_sensitivity(self):
        rows = make_g14_vanilla_rows()

        delta = publisher.validate_vanilla_endpoints(rows)

        self.assertEqual(delta, 800000)

    def test_g14_vanilla_endpoint_gate_rejects_flat_or_reversed_result(self):
        for ticks in ("1000000", "999999"):
            with self.subTest(ticks=ticks):
                rows = make_g14_vanilla_rows()
                rows[-1]["sim_ticks"] = ticks
                with self.assertRaisesRegex(
                    publisher.PublicationError,
                    "2us ROI must be slower than Vanilla 200ns ROI",
                ):
                    publisher.validate_vanilla_endpoints(rows)

    def test_g14_vanilla_endpoint_gate_rejects_each_zero_counter(self):
        for field in publisher.REAL_CXL_FIELDS:
            with self.subTest(field=field):
                rows = make_g14_vanilla_rows()
                rows[0][field] = "0"
                with self.assertRaisesRegex(
                    publisher.PublicationError, field
                ):
                    publisher.validate_vanilla_endpoints(rows)

    def test_g14_vanilla_counter_variation_requires_explanation(self):
        rows = make_g14_vanilla_rows()
        rows[-1]["mem_ctrl_read_reqs"] = "2000"
        with self.assertRaisesRegex(
            publisher.PublicationError, "varies by more than 5 percent"
        ):
            publisher.validate_vanilla_endpoints(rows)

        delta = publisher.validate_vanilla_endpoints(
            rows,
            explanation="2us run records additional deterministic retries",
        )

        self.assertEqual(delta, 800000)

    def test_g14_vanilla_intermediate_points_need_not_be_monotonic(self):
        rows = make_g14_vanilla_rows()
        self.assertGreater(
            int(rows[1]["sim_ticks"]), int(rows[2]["sim_ticks"])
        )

        self.assertEqual(publisher.validate_vanilla_endpoints(rows), 800000)


class PublicationTest(unittest.TestCase):
    def test_collect_rows_builds_valid_matrix_from_completed_sweep(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_completed_sweep(root)

            rows, evidence = publisher.collect_rows(root)

        self.assertEqual(len(rows), 16)
        self.assertEqual(len(evidence["source_sha256"]), 16)
        self.assertEqual(
            next(row for row in rows if row["system"] == "m2ndp")[
                "funcsim_compared"
            ],
            "16",
        )

    def test_cli_collects_and_publishes_completed_sweep(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_completed_sweep(root)

            code = publisher.main(["--sweep-root", str(root)])

            self.assertEqual(code, 0)
            self.assertTrue((root / "published" / publisher.CSV_NAME).is_file())

    def test_publish_writes_reloadable_csv_evidence_and_tex(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = publisher.publish(make_valid_rows(), Path(tmp))
            with paths.csv.open(newline="", encoding="utf-8") as stream:
                written = list(csv.DictReader(stream))
            tex = paths.tex.read_text(encoding="utf-8")

        self.assertEqual(len(written), 16)
        self.assertEqual(paths.csv.name, publisher.CSV_NAME)
        self.assertEqual(paths.evidence.name, publisher.EVIDENCE_NAME)
        self.assertEqual(paths.tex.name, publisher.TEX_NAME)
        self.assertIn("AMU", tex)
        self.assertIn(r"$\times$", tex)
        self.assertNotIn("\t", tex)
        self.assertIn(r"ROI ($\mu$s)", tex)
        self.assertIn(r"M$^2$NDP", tex)
        self.assertIn(
            r"200ns & Vanilla CXL & 1.000 & 1.000\,$\times$", tex
        )

    def test_rejected_matrix_preserves_existing_publication(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            published = root / "published"
            published.mkdir()
            marker = published / "keep"
            marker.write_text("old\n", encoding="utf-8")
            rows = make_valid_rows()[:-1]

            with self.assertRaises(publisher.PublicationError):
                publisher.publish(rows, root)

            self.assertEqual(marker.read_text(encoding="utf-8"), "old\n")

    def test_speedup_uses_same_latency_vanilla(self):
        rows = make_valid_rows()
        cira = next(
            row
            for row in rows
            if row["latency"] == "500ns" and row["system"] == "cira"
        )
        cira["speedup_vs_vanilla_cxl"] = "9.0"
        with self.assertRaisesRegex(
            publisher.PublicationError, "speedup mismatch"
        ):
            publisher.validate_matrix(rows)

    def test_raw_hash_mismatch_blocks_publication(self):
        rows = make_valid_rows()
        rows[3]["result_sha256"] = "f" * 64
        with self.assertRaisesRegex(
            publisher.PublicationError, "bit-exact"
        ):
            publisher.validate_matrix(rows)

    def test_cira_requires_four_balanced_active_ports(self):
        rows = make_valid_rows()
        cira = next(row for row in rows if row["system"] == "cira")
        cira["cira_completed_per_core"] = "16;16;16;0"
        with self.assertRaisesRegex(
            publisher.PublicationError, "four.*CIRA ports"
        ):
            publisher.validate_matrix(rows)

    def test_m2ndp_calibration_must_be_within_one_link_clock(self):
        rows = make_valid_rows()
        m2ndp = next(row for row in rows if row["system"] == "m2ndp")
        m2ndp["calibration_residual_ns"] = "0.25"
        with self.assertRaisesRegex(
            publisher.PublicationError, "calibration residual"
        ):
            publisher.validate_matrix(rows)


if __name__ == "__main__":
    unittest.main()
