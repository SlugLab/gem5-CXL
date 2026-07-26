# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

import csv
import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from scripts import calibrate_m2ndp_cxl as calibration
from scripts import m2ndp_artifacts as artifacts
from scripts import run_m2ndp_g20_pr_spmv as orchestrator
from scripts import generate_gapbs_g20_e2e_table as table


REPO = Path(__file__).resolve().parents[3]
PATCH = REPO / "util/m2ndp/patches/0001-funcsim-strict-sequence.patch"


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def write_csv(path, row):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=tuple(row))
        writer.writeheader()
        writer.writerow(row)


def raw_words():
    return b"".join(
        value.to_bytes(4, "little")
        for value in (0x3F000000, 0x3E800000, 0x3E000000, 0x3D800000)
    )


def variant_row(kind, run_dir):
    return {
        "benchmark": "pr_spmv",
        "kind": kind,
        "status": "ok",
        "verification": "pass",
        "scale": 20,
        "iterations": 2,
        "measured_trial": 1,
        "roi_cpu": "timing",
        "cores": 2,
        "cxl_link_delay": "1us",
        "all_memory_cxl": True,
        "graph_sha256": artifacts.EXPECTED_G20_SHA256,
        "checkpoint_restores": 1,
        "sim_ticks": 4_000_000_000_000
        if kind == "amu"
        else 1_600_000_000_000,
        "asmc_loads": 32 if kind == "amu" else 0,
        "asmc_completed": 32 if kind == "amu" else 0,
        "cira_prefetches": 64 if kind == "cira" else 0,
        "cira_completed": 64 if kind == "cira" else 0,
        "cira_indexed_prefetches": 0,
        "cira_csr_prefetches": 8 if kind == "cira" else 0,
        "run_dir": str(run_dir.resolve()),
    }


def write_formal_bundle(root):
    m2ndp = root / "m2ndp"
    variants = root / "variants"
    raw = raw_words()

    reference_raw = m2ndp / "reference/scores.raw"
    funcsim_dump = m2ndp / "funcsim/scores.u32"
    for path in (reference_raw, funcsim_dump):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)

    gem5_run = m2ndp / "gem5/run/pr_spmv/cxl_vanilla"
    gem5_run.mkdir(parents=True)
    (gem5_run / "config.ini").write_text(
        "delay=1000000\n", encoding="utf-8"
    )
    (gem5_run / "gem5.log").write_text(
        "Verification: PASS\n", encoding="utf-8"
    )
    baseline_row = {
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
        "sim_ticks": "2000000000000",
    }
    write_csv(m2ndp / "gem5/run/summary.csv", baseline_row)

    funcsim_log = m2ndp / "logs/funcsim.log"
    funcsim_log.parent.mkdir(parents=True, exist_ok=True)
    funcsim_log.write_text(
        "\n".join(
            [
                "M2NDP_STRICT_MODE=1",
                "M2NDP_STRICT_COMPARED=4",
                "M2NDP_STRICT_MISMATCHED=0",
                "M2NDP_STRICT_MATCH=PASS",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    ndpsim_log = m2ndp / "logs/ndpsim.log"
    ndpsim_log.write_text(
        "\n".join(
            [
                "CORE period: 0.001 DRAM period: 0.001",
                "Launching NDP kernel: K0_INIT_TRIAL1 at cycle 100",
                "EXPR FINISHED 600",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    config = m2ndp / "calibration/config"
    config.mkdir(parents=True)
    (config / "m2ndp.config").write_text(
        "freq = 1000, 1000, 1000, 1000\n", encoding="utf-8"
    )
    config_sha256 = calibration.sha256_config_tree(config)
    calibration_path = m2ndp / "calibration/calibration.json"
    write_json(
        calibration_path,
        {
            "passed": True,
            "request_bytes": 64,
            "target_ns": "1000",
            "measured_ns": "1000",
            "residual_ns": "0",
            "link_period_ns": "0.125",
            "config_sha256": config_sha256,
        },
    )

    trace = m2ndp / "trace"
    trace.mkdir()
    (trace / "kernelslist.g").write_text("K0\nK1\nK2\nK3\n")

    summary_row = {
        "benchmark": "pr_spmv",
        "graph_sha256": artifacts.EXPECTED_G20_SHA256,
        "gem5_binary_sha256": "d" * 64,
        "m2ndp_patch_sha256": artifacts.sha256_file(PATCH),
        "m2ndp_config_sha256": config_sha256,
        "trace_sha256": orchestrator.hash_path(trace),
        "iterations": "20",
        "trials": "2",
        "measured_trial": "1",
        "cores": "2",
        "all_memory_cxl": "True",
        "cxl_link_delay": "1us",
        "verification": "pass",
        "funcsim_strict": "pass",
        "funcsim_compared": "4",
        "gem5_sim_ticks": "2000000000000",
        "ndpsim_start_cycle": "100",
        "ndpsim_end_cycle": "600",
        "ndpsim_measured_cycles": "500",
        "ndpsim_core_period_seconds": "0.001",
        "gem5_seconds": "2",
        "m2ndp_seconds": "0.5",
        "speedup": "4",
    }
    summary_path = m2ndp / "summary.csv"
    write_csv(summary_path, summary_row)

    status = {
        "schema": 1,
        "contract": {
            "benchmark": "pr_spmv",
            "graph": "g20.sg",
            "graph_scale": 20,
            "page_rank_iterations": 20,
            "trials": 2,
            "measured_trial": 1,
            "cpu": "timing",
            "cores": 2,
            "all_memory_cxl": True,
            "cxl_link_delay": "1us",
            "smoke_test": False,
        },
        "stages": {
            stage: {"status": "passed"}
            for stage in orchestrator.STAGES
        },
    }
    write_json(m2ndp / "status.json", status)

    manifest = {
        "schema": 1,
        "contract": status["contract"],
        "gem5_repository_commit": "a" * 40,
        "m2ndp_upstream_commit": artifacts.EXPECTED_M2NDP_COMMIT,
        "build_binary_sha256": "b" * 64,
        "artifact_sha256": {
            "m2ndp_patch": artifacts.sha256_file(PATCH),
            "trace": orchestrator.hash_path(trace),
            "m2ndp_config": orchestrator.hash_path(config),
            "reference_raw": artifacts.sha256_file(reference_raw),
            "funcsim_dump": artifacts.sha256_file(funcsim_dump),
            "calibration": artifacts.sha256_file(calibration_path),
            "gem5_log": artifacts.sha256_file(gem5_run / "gem5.log"),
            "funcsim_log": artifacts.sha256_file(funcsim_log),
            "ndpsim_log": artifacts.sha256_file(ndpsim_log),
            "summary": artifacts.sha256_file(summary_path),
        },
    }
    write_json(m2ndp / "manifest.json", manifest)

    baseline_build = variants / "baseline"
    write_json(baseline_build / "manifest.json", {"schema": 1})
    fixed_source_sha256 = "f" * 64
    variant_manifest = {
        "schema": 1,
        "benchmark": "pr_spmv",
        "page_rank_iterations": 20,
        "fixed_iterations": True,
        "fp_contract": False,
        "fast_math": False,
        "baseline_build": str(baseline_build.resolve()),
        "baseline_manifest_sha256": artifacts.sha256_file(
            baseline_build / "manifest.json"
        ),
        "fixed_source_sha256": fixed_source_sha256,
        "variants": [],
    }

    variant_state = {}
    for kind in ("amu", "cira"):
        binary = variants / f"{kind}.bin"
        binary.parent.mkdir(parents=True, exist_ok=True)
        binary.write_bytes(f"{kind}-binary".encode())
        variant_raw = variants / f"{kind}.raw"
        variant_raw.write_bytes(raw)
        run_dir = variants / kind / "run/pr_spmv" / f"{kind}_matched"
        run_dir.mkdir(parents=True)
        (run_dir / "config.ini").write_text(
            "delay=1000000\n", encoding="utf-8"
        )
        row = variant_row(kind, run_dir)
        variant_manifest["variants"].append(
            {
                "kind": kind,
                "binary": str(binary.resolve()),
                "binary_sha256": artifacts.sha256_file(binary),
                "reference_raw": str(variant_raw.resolve()),
                "fixed_source_sha256": fixed_source_sha256,
            }
        )
        variant_state[kind] = {
            "binary": binary,
            "reference": variant_raw,
            "row": row,
        }

    variant_manifest_path = variants / "build/manifest.json"
    write_json(variant_manifest_path, variant_manifest)
    variant_manifest_sha256 = artifacts.sha256_file(variant_manifest_path)
    for kind, state in variant_state.items():
        run_root = variants / kind / "run"
        write_csv(run_root / "summary.csv", state["row"])
        write_json(
            run_root / "evidence.json",
            {
                "schema": 1,
                "graph_sha256": artifacts.EXPECTED_G20_SHA256,
                "variant_manifest": str(
                    variant_manifest_path.resolve()
                ),
                "variant_manifest_sha256": variant_manifest_sha256,
                "fixed_source_sha256": fixed_source_sha256,
                "runs": {
                    kind: {
                        "row": state["row"],
                        "config_delay_ticks": 1_000_000,
                        "binary_sha256": artifacts.sha256_file(
                            state["binary"]
                        ),
                        "reference_raw": str(
                            state["reference"].resolve()
                        ),
                        "reference_raw_sha256": artifacts.sha256_file(
                            state["reference"]
                        ),
                    }
                },
            },
        )

    return SimpleNamespace(m2ndp=m2ndp, variants=variants)


def refresh_variant_evidence_hash(roots, kind):
    path = roots.variants / kind / "run/evidence.json"
    value = read_json(path)
    raw = roots.variants / f"{kind}.raw"
    value["runs"][kind]["reference_raw_sha256"] = artifacts.sha256_file(raw)
    write_json(path, value)


def write_sensitivity_bundle(root):
    csv_path = root / "latency.csv"
    run_root = root / "old-runs"
    fieldnames = (
        "latency",
        "benchmark",
        "label",
        "kind",
        "status",
        "verification",
        "sim_ticks",
        "speedup_vs_cxl",
        "run_dir",
    )
    delay_ticks = {
        "200ns": "200000",
        "500ns": "500000",
        "1us": "1000000",
        "2us": "2000000",
    }
    configurations = (
        ("cxl_vanilla", "baseline", 1000, Decimal("1")),
        ("amu", "amu", 2000, Decimal("0.5")),
        ("cira_pgo", "cira", 800, Decimal("1.25")),
    )
    rows = []
    for latency in ("200ns", "500ns", "1us", "2us"):
        for benchmark in ("bfs", "bc", "pr", "sssp"):
            for label, kind, ticks, speedup in configurations:
                relative = (
                    Path("m5out")
                    / latency
                    / benchmark
                    / label
                )
                config = run_root / relative / "config.ini"
                config.parent.mkdir(parents=True, exist_ok=True)
                config.write_text(
                    f"delay={delay_ticks[latency]}\n",
                    encoding="utf-8",
                )
                rows.append(
                    {
                        "latency": latency,
                        "benchmark": benchmark,
                        "label": label,
                        "kind": kind,
                        "status": "ok",
                        "verification": "pass",
                        "sim_ticks": str(ticks),
                        "speedup_vs_cxl": str(speedup),
                        "run_dir": relative.as_posix(),
                    }
                )
    with csv_path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    return csv_path, run_root


def rewrite_csv(path, mutate):
    with path.open(newline="", encoding="utf-8") as stream:
        rows = list(csv.DictReader(stream))
        fieldnames = tuple(rows[0])
    mutate(rows)
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


class FormalTableEvidenceTest(unittest.TestCase):
    def load_rows(self, roots):
        with mock.patch.object(table, "G20_WORDS", 4):
            return table.load_formal_rows(roots.m2ndp, roots.variants)

    def test_formal_rows_recompute_absolute_time_and_speedup(self):
        with tempfile.TemporaryDirectory() as tmp:
            roots = write_formal_bundle(Path(tmp))
            rows, evidence = self.load_rows(roots)

        self.assertEqual(
            [row.system for row in rows],
            ["Vanilla CXL", "AMU", "CIRA", "M2NDP"],
        )
        self.assertEqual(rows[0].latency_seconds, Decimal("2"))
        self.assertEqual(rows[1].speedup, Decimal("0.5"))
        self.assertEqual(rows[2].speedup, Decimal("1.25"))
        self.assertEqual(rows[3].latency_seconds, Decimal("0.500"))
        self.assertEqual(rows[3].speedup, Decimal("4"))
        self.assertEqual(
            evidence["graph_sha256"], artifacts.EXPECTED_G20_SHA256
        )

    def test_formal_rows_reject_one_bit_difference(self):
        with tempfile.TemporaryDirectory() as tmp:
            roots = write_formal_bundle(Path(tmp))
            raw = roots.variants / "cira.raw"
            data = bytearray(raw.read_bytes())
            data[-1] ^= 1
            raw.write_bytes(data)
            refresh_variant_evidence_hash(roots, "cira")

            with self.assertRaisesRegex(
                table.TableEvidenceError, "raw float32"
            ):
                self.load_rows(roots)

    def test_formal_rows_reject_running_stage(self):
        with tempfile.TemporaryDirectory() as tmp:
            roots = write_formal_bundle(Path(tmp))
            status_path = roots.m2ndp / "status.json"
            status = read_json(status_path)
            status["stages"]["ndpsim"]["status"] = "running"
            write_json(status_path, status)

            with self.assertRaisesRegex(
                table.TableEvidenceError, "ndpsim.*passed"
            ):
                self.load_rows(roots)

    def test_formal_rows_reject_wrong_baseline_config_delay(self):
        with tempfile.TemporaryDirectory() as tmp:
            roots = write_formal_bundle(Path(tmp))
            config = (
                roots.m2ndp
                / "gem5/run/pr_spmv/cxl_vanilla/config.ini"
            )
            config.write_text("delay=999999\n", encoding="utf-8")

            with self.assertRaisesRegex(
                table.TableEvidenceError, "delay"
            ):
                self.load_rows(roots)

    def test_formal_rows_reject_unbalanced_amu(self):
        with tempfile.TemporaryDirectory() as tmp:
            roots = write_formal_bundle(Path(tmp))
            evidence_path = roots.variants / "amu/run/evidence.json"
            evidence = read_json(evidence_path)
            evidence["runs"]["amu"]["row"]["asmc_completed"] = 31
            write_json(evidence_path, evidence)

            with self.assertRaisesRegex(
                table.TableEvidenceError, "issued/completed"
            ):
                self.load_rows(roots)

    def test_formal_rows_reject_split_variant_manifests(self):
        with tempfile.TemporaryDirectory() as tmp:
            roots = write_formal_bundle(Path(tmp))
            original = roots.variants / "build/manifest.json"
            split = roots.variants / "build/cira-manifest.json"
            value = read_json(original)
            value["schema"] = 2
            write_json(split, value)
            evidence_path = roots.variants / "cira/run/evidence.json"
            evidence = read_json(evidence_path)
            evidence["variant_manifest"] = str(split.resolve())
            evidence["variant_manifest_sha256"] = artifacts.sha256_file(
                split
            )
            write_json(evidence_path, evidence)

            with self.assertRaisesRegex(
                table.TableEvidenceError, "variant manifest.*same"
            ):
                self.load_rows(roots)

    def test_formal_rows_reject_failed_calibration(self):
        with tempfile.TemporaryDirectory() as tmp:
            roots = write_formal_bundle(Path(tmp))
            calibration_path = (
                roots.m2ndp / "calibration/calibration.json"
            )
            value = read_json(calibration_path)
            value["passed"] = False
            write_json(calibration_path, value)
            manifest_path = roots.m2ndp / "manifest.json"
            manifest = read_json(manifest_path)
            manifest["artifact_sha256"]["calibration"] = (
                artifacts.sha256_file(calibration_path)
            )
            write_json(manifest_path, manifest)

            with self.assertRaisesRegex(
                table.TableEvidenceError, "calibration.*passed"
            ):
                self.load_rows(roots)

    def test_formal_rows_reject_stored_speedup_drift(self):
        with tempfile.TemporaryDirectory() as tmp:
            roots = write_formal_bundle(Path(tmp))
            summary_path = roots.m2ndp / "summary.csv"
            with summary_path.open(
                newline="", encoding="utf-8"
            ) as stream:
                row = next(csv.DictReader(stream))
            row["speedup"] = "40"
            write_csv(summary_path, row)
            manifest_path = roots.m2ndp / "manifest.json"
            manifest = read_json(manifest_path)
            manifest["artifact_sha256"]["summary"] = (
                artifacts.sha256_file(summary_path)
            )
            write_json(manifest_path, manifest)

            with self.assertRaisesRegex(
                table.TableEvidenceError, "stored.*speedup"
            ):
                self.load_rows(roots)


class SensitivityEvidenceTest(unittest.TestCase):
    def test_sensitivity_recomputes_rows_and_per_latency_geomean(self):
        with tempfile.TemporaryDirectory() as tmp:
            csv_path, run_root = write_sensitivity_bundle(Path(tmp))
            values = table.load_sensitivity(csv_path, run_root)

        self.assertEqual(set(values), set(table.LATENCIES))
        self.assertEqual(
            values["1us"]["pr"]["AMU"], Decimal("0.5")
        )
        self.assertEqual(
            values["1us"]["pr"]["CIRA"], Decimal("1.25")
        )
        self.assertEqual(
            values["1us"]["Geo."]["AMU"], Decimal("0.5")
        )
        self.assertEqual(
            values["1us"]["Geo."]["CIRA"], Decimal("1.25")
        )

    def test_sensitivity_rejects_duplicate_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            csv_path, run_root = write_sensitivity_bundle(Path(tmp))

            def duplicate(rows):
                rows.append(dict(rows[0]))

            rewrite_csv(csv_path, duplicate)
            with self.assertRaisesRegex(
                table.TableEvidenceError, "duplicate"
            ):
                table.load_sensitivity(csv_path, run_root)

    def test_sensitivity_rejects_wrong_config_delay(self):
        with tempfile.TemporaryDirectory() as tmp:
            csv_path, run_root = write_sensitivity_bundle(Path(tmp))
            config = run_root / "m5out/200ns/bfs/amu/config.ini"
            config.write_text("delay=1000000\n", encoding="utf-8")

            with self.assertRaisesRegex(
                table.TableEvidenceError, "delay"
            ):
                table.load_sensitivity(csv_path, run_root)

    def test_sensitivity_rejects_stored_speedup_drift(self):
        with tempfile.TemporaryDirectory() as tmp:
            csv_path, run_root = write_sensitivity_bundle(Path(tmp))

            def drift(rows):
                row = next(
                    item
                    for item in rows
                    if item["latency"] == "1us"
                    and item["benchmark"] == "pr"
                    and item["kind"] == "amu"
                )
                row["speedup_vs_cxl"] = "0.6"

            rewrite_csv(csv_path, drift)
            with self.assertRaisesRegex(
                table.TableEvidenceError, "stored speedup"
            ):
                table.load_sensitivity(csv_path, run_root)

    def test_sensitivity_rejects_run_dir_escape(self):
        with tempfile.TemporaryDirectory() as tmp:
            csv_path, run_root = write_sensitivity_bundle(Path(tmp))

            def escape(rows):
                rows[0]["run_dir"] = "../outside"

            rewrite_csv(csv_path, escape)
            with self.assertRaisesRegex(
                table.TableEvidenceError, "escapes"
            ):
                table.load_sensitivity(csv_path, run_root)


if __name__ == "__main__":
    unittest.main()
