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

from scripts import generate_gapbs_g14_4thread_latency_results as publisher
from scripts import m2ndp_artifacts as artifacts
from scripts import run_gapbs_g14_4thread_latency_sweep as sweep


GRAPH_SHA = "1" * 64
MANIFEST_SHA = "2" * 64
BASE_TICKS = {
    "200ns": 1_000_000,
    "500ns": 2_000_000,
    "1us": 3_000_000,
    "2us": 4_000_000,
}


def make_valid_rows():
    rows = []
    for latency, vanilla_ticks in BASE_TICKS.items():
        for system, divisor in (
            ("vanilla", 1), ("amu", 2), ("cira", 4), ("m2ndp", 5)
        ):
            ticks = vanilla_ticks // divisor
            is_m2ndp = system == "m2ndp"
            row = {field: "" for field in publisher.FIELDNAMES}
            row.update(
                profile="g14-4thread-sweep",
                benchmark="pr_spmv",
                latency=latency,
                system=system,
                graph_sha256=GRAPH_SHA,
                profile_manifest_sha256=MANIFEST_SHA,
                cores="4",
                threads="4",
                trials="2",
                measured_trial="1",
                iterations="20",
                all_memory_cxl="True",
                verification="pass",
                bit_exact="pass",
                raw_vector_bytes=str(16384 * 4),
                result_sha256="3" * 64,
                roi_seconds=str(Decimal(ticks) / Decimal(10**12)),
                roi_ticks=str(ticks),
                roi_microseconds=str(Decimal(ticks) / Decimal(10**6)),
                speedup=str(Decimal(vanilla_ticks) / Decimal(ticks)),
                sim_ticks="" if is_m2ndp else str(ticks),
                measured_cycles=str(ticks) if is_m2ndp else "",
                core_period_seconds="1e-12" if is_m2ndp else "",
                mem_ctrl_read_reqs="1000",
                mem_ctrl_read_bursts="1000",
                mem_ctrl_bytes_read="64000",
                mem_ctrl_cpu_data_reads="900",
                asmc_loads="64" if system == "amu" else "0",
                asmc_completed="64" if system == "amu" else "0",
                cira_prefetches="64" if system == "cira" else "0",
                cira_completed="64" if system == "cira" else "0",
                cira_indexed_prefetches="0",
                cira_csr_prefetches="16" if system == "cira" else "0",
                cira_issued_per_core=("16;16;16;16" if system == "cira" else ""),
                cira_completed_per_core=("16;16;16;16" if system == "cira" else ""),
                cira_csr_per_core=("4;4;4;4" if system == "cira" else ""),
                cira_rejected_queue_full="0",
                cira_dropped_csr_descriptors="0",
                cira_csr_queue_high_watermark=("16" if system == "cira" else "0"),
                funcsim_compared="16384" if is_m2ndp else "",
                funcsim_mismatched="0" if is_m2ndp else "",
                calibration_pass="pass" if is_m2ndp else "",
                calibration_cxl_delay=latency if is_m2ndp else "",
                calibration_residual_ns="0.125" if is_m2ndp else "",
                calibration_link_period_ns="0.5" if is_m2ndp else "",
                gem5_microprobe_ns=(str(publisher.LATENCY_NS[latency]) if is_m2ndp else ""),
                m2ndp_boundary_ns=(str(Decimal(publisher.LATENCY_NS[latency]) + Decimal("0.125")) if is_m2ndp else ""),
                source_path=f"formal/runs/{latency}/{system}/summary.csv",
                source_sha256="4" * 64,
                config_sha256="5" * 64,
                checkpoint_sha256="6" * 64,
                binary_sha256="7" * 64,
                trace_sha256="8" * 64 if is_m2ndp else "",
                m2ndp_patch_sha256="9" * 64 if is_m2ndp else "",
                m2ndp_config_sha256="a" * 64 if is_m2ndp else "",
                funcsim_binary_sha256="b" * 64 if is_m2ndp else "",
                ndpsim_binary_sha256="c" * 64 if is_m2ndp else "",
                calibration_sha256="d" * 64 if is_m2ndp else "",
                gem5_binary_sha256="e" * 64 if is_m2ndp else "",
                provenance_json='{"artifact":"' + "f" * 64 + '"}',
            )
            rows.append(row)
    return rows


def write_csv(path, row):
    path.parent.mkdir(parents=True, exist_ok=True)
    artifacts.atomic_write_csv(path, tuple(row), [row])


def write_completed_formal_sweep(root):
    manifest = root / "graphs/g14.manifest.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text("{}\n", encoding="utf-8")
    manifest_sha = artifacts.sha256_file(manifest)
    state = sweep.new_state({
        "profile": "g14-4thread-sweep",
        "hashes": {"graph_manifest": manifest_sha},
    })
    raw_payload = bytes(range(256)) * 256

    for latency, base_ticks in BASE_TICKS.items():
        latency_root = root / "formal/runs" / latency
        common = latency_root / "common"
        common.mkdir(parents=True)
        config = common / "config.ini"
        config.write_text(f"delay={publisher.profiles.LATENCY_TICKS[latency]}\n")
        checkpoint = common / "checkpoint.json"
        checkpoint.write_text("{}\n")
        binary = common / "pr_spmv"
        binary.write_bytes(b"binary")
        reference = latency_root / "m2ndp/reference/scores.raw"
        reference.parent.mkdir(parents=True)
        reference.write_bytes(raw_payload)
        real_cxl = {
            "mem_ctrl_read_reqs": "1000",
            "mem_ctrl_read_bursts": "1000",
            "mem_ctrl_bytes_read": "64000",
            "mem_ctrl_cpu_data_reads": "900",
        }

        vanilla_summary = latency_root / "m2ndp/gem5/run/summary.csv"
        write_csv(vanilla_summary, {
            "benchmark": "pr_spmv", "kind": "baseline", "status": "ok",
            "verification": "pass", "roi_cpu": "timing", "scale": "14",
            "cores": "4", "cxl_link_delay": latency,
            "all_memory_cxl": "True", "graph_sha256": GRAPH_SHA,
            "iterations": "2", "measured_trial": "1",
            "checkpoint_restores": "1", "sim_ticks": str(base_ticks),
            **real_cxl,
        })
        vanilla_outputs = {
            "summary": vanilla_summary, "raw": reference, "config": config,
            "checkpoint": checkpoint, "workload_binary": binary,
        }
        state["latencies"][latency]["vanilla"] = {
            "status": "passed", "input_hashes": {},
            "output_paths": {name: str(path) for name, path in vanilla_outputs.items()},
            "output_hashes": sweep.shared.hash_named_paths(vanilla_outputs),
        }

        for system, divisor in (("amu", 2), ("cira", 4)):
            raw = latency_root / f"{system}/scores.raw"
            raw.parent.mkdir(parents=True)
            raw.write_bytes(raw_payload)
            summary = latency_root / f"{system}/summary.csv"
            write_csv(summary, {
                "benchmark": "pr_spmv", "kind": system, "status": "ok",
                "verification": "pass", "roi_cpu": "timing", "scale": "14",
                "cores": "4", "cxl_link_delay": latency,
                "all_memory_cxl": "True", "graph_sha256": GRAPH_SHA,
                "iterations": "2", "measured_trial": "1",
                "checkpoint_restores": "1", "sim_ticks": str(base_ticks // divisor),
                "asmc_loads": "64" if system == "amu" else "0",
                "asmc_completed": "64" if system == "amu" else "0",
                "cira_prefetches": "64" if system == "cira" else "0",
                "cira_completed": "64" if system == "cira" else "0",
                "cira_indexed_prefetches": "0",
                "cira_csr_prefetches": "16" if system == "cira" else "0",
                "cira_issued_per_core": "16;16;16;16" if system == "cira" else "",
                "cira_completed_per_core": "16;16;16;16" if system == "cira" else "",
                "cira_csr_per_core": "4;4;4;4" if system == "cira" else "",
                "cira_rejected_queue_full": "0",
                "cira_dropped_csr_descriptors": "0",
                "cira_csr_queue_high_watermark": "16" if system == "cira" else "0",
                **real_cxl,
            })
            outputs = {"summary": summary, "raw": raw, "config": config,
                       "checkpoint": checkpoint}
            state["latencies"][latency][system] = {
                "status": "passed", "input_hashes": {"binary": "7" * 64},
                "output_paths": {name: str(path) for name, path in outputs.items()},
                "output_hashes": sweep.shared.hash_named_paths(outputs),
            }

        mroot = latency_root / "m2ndp"
        dump = mroot / "funcsim/scores.u32"
        dump.parent.mkdir(parents=True)
        dump.write_bytes(raw_payload)
        calibration = mroot / "calibration/calibration.json"
        calibration.parent.mkdir(parents=True)
        target = Decimal(publisher.LATENCY_NS[latency])
        calibration.write_text(json.dumps({
            "passed": True, "cxl_link_delay": latency, "residual_ns": "0.125",
            "link_period_ns": "0.5", "gem5_microprobe_ns": str(target),
            "m2ndp_boundary_ns": str(target + Decimal("0.125")),
        }))
        trace = mroot / "trace.bin"
        patch_file = mroot / "patch.diff"
        funcsim = mroot / "FuncSim"
        ndpsim = mroot / "NDPSim"
        gem5 = mroot / "gem5.opt"
        for path, payload in ((trace, b"trace"), (patch_file, b"patch"),
                              (funcsim, b"funcsim"), (ndpsim, b"ndpsim"),
                              (gem5, b"gem5")):
            path.write_bytes(payload)
        artifact_hashes = {
            "trace": artifacts.sha256_file(trace),
            "m2ndp_patch": artifacts.sha256_file(patch_file),
            "m2ndp_config": artifacts.sha256_file(config),
            "gem5_binary": artifacts.sha256_file(gem5),
            "funcsim_binary": artifacts.sha256_file(funcsim),
            "ndpsim_binary": artifacts.sha256_file(ndpsim),
            "reference_raw": artifacts.sha256_file(reference),
            "funcsim_dump": artifacts.sha256_file(dump),
            "calibration": artifacts.sha256_file(calibration),
            "profile_manifest": manifest_sha,
        }
        m2_summary = mroot / "summary.csv"
        cycles = base_ticks // 5
        write_csv(m2_summary, {
            "profile": "g14-4thread-sweep", "benchmark": "pr_spmv",
            "graph_sha256": GRAPH_SHA, "profile_manifest_sha256": manifest_sha,
            "cores": "4", "iterations": "20", "trials": "2",
            "measured_trial": "1", "all_memory_cxl": "True",
            "cxl_link_delay": latency, "verification": "pass",
            "funcsim_strict": "pass", "funcsim_compared": "16384",
            "funcsim_dump_sha256": artifacts.sha256_file(dump),
            "reference_raw_sha256": artifacts.sha256_file(reference),
            "ndpsim_measured_cycles": str(cycles),
            "ndpsim_core_period_seconds": "1e-12",
            "m2ndp_seconds": str(Decimal(cycles) / Decimal(10**12)),
            "trace_sha256": artifact_hashes["trace"],
            "m2ndp_patch_sha256": artifact_hashes["m2ndp_patch"],
            "m2ndp_config_sha256": artifact_hashes["m2ndp_config"],
            "gem5_binary_sha256": artifact_hashes["gem5_binary"],
            **real_cxl,
        })
        artifact_hashes["summary"] = artifacts.sha256_file(m2_summary)
        final_manifest = mroot / "manifest.json"
        final_manifest.write_text(json.dumps({
            "contract": {"profile": "g14-4thread-sweep",
                         "cxl_link_delay": latency,
                         "profile_manifest_sha256": manifest_sha},
            "build_binary_sha256": artifacts.sha256_file(binary),
            "artifact_sha256": artifact_hashes,
        }))
        m2_outputs = {
            "summary": m2_summary, "raw": reference, "funcsim_raw": dump,
            "calibration": calibration, "m2ndp_manifest": final_manifest,
            "config": config, "checkpoint": checkpoint,
            "workload_binary": binary,
        }
        state["latencies"][latency]["m2ndp"] = {
            "status": "passed", "input_hashes": {},
            "output_paths": {name: str(path) for name, path in m2_outputs.items()},
            "output_hashes": sweep.shared.hash_named_paths(m2_outputs),
        }
    artifacts.atomic_write_json(root / "formal/status.json", state)
    return manifest_sha


class MatrixTest(unittest.TestCase):
    def test_collect_rows_reparses_formal_sources_and_provenance(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest_sha = write_completed_formal_sweep(root)
            profile = SimpleNamespace(
                name="g14-4thread-sweep", graph_scale=14,
                graph_sha256=GRAPH_SHA, num_nodes=16384, cores=4, threads=4,
                trials=2, measured_trial=1, page_rank_iterations=20,
                latencies=publisher.LATENCIES,
            )
            with mock.patch.object(
                publisher.profiles, "load_frozen_profile", return_value=profile
            ):
                rows, evidence = publisher.collect_rows(root)
        self.assertEqual(len(rows), 16)
        self.assertEqual(rows[0]["profile_manifest_sha256"], manifest_sha)
        self.assertIn("manifest/trace", rows[3]["provenance_json"])
        self.assertEqual(evidence["profile_manifest_sha256"], manifest_sha)

    def test_requires_exact_16_rows_and_recomputes_same_latency_speedup(self):
        rows = make_valid_rows()
        validated = publisher.validate_matrix(
            rows, graph_sha256=GRAPH_SHA, profile_manifest_sha256=MANIFEST_SHA
        )
        self.assertEqual(len(validated), 16)
        for row in validated:
            vanilla = next(
                item for item in validated
                if item["latency"] == row["latency"]
                and item["system"] == "vanilla"
            )
            self.assertEqual(
                Decimal(row["speedup"]),
                Decimal(vanilla["roi_seconds"]) / Decimal(row["roi_seconds"]),
            )
        with self.assertRaisesRegex(publisher.PublicationError, "16 rows"):
            publisher.validate_matrix(rows[:-1], graph_sha256=GRAPH_SHA,
                                      profile_manifest_sha256=MANIFEST_SHA)

    def test_rejects_cross_latency_denominator_raw_mismatch_and_provenance(self):
        mutations = (
            ("speedup", "9", "speedup"),
            ("result_sha256", "f" * 64, "bit-exact"),
            ("raw_vector_bytes", "4", "vector length"),
            ("source_sha256", "", "provenance"),
            ("funcsim_mismatched", "1", "FuncSim"),
        )
        for field, value, message in mutations:
            with self.subTest(field=field):
                rows = make_valid_rows()
                target = next(row for row in rows if row["system"] == "m2ndp")
                target[field] = value
                with self.assertRaisesRegex(publisher.PublicationError, message):
                    publisher.validate_matrix(
                        rows, graph_sha256=GRAPH_SHA,
                        profile_manifest_sha256=MANIFEST_SHA,
                    )

    def test_atomic_failed_publication_preserves_current_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old = root / "publication-old"
            old.mkdir()
            (old / "marker").write_bytes(b"old")
            (root / "publication-current").symlink_to(old.name)
            before = (root / "publication-current").readlink()
            with self.assertRaises(publisher.PublicationError):
                publisher.publish(
                    make_valid_rows()[:-1], root,
                    graph_sha256=GRAPH_SHA,
                    profile_manifest_sha256=MANIFEST_SHA,
                )
            self.assertEqual((root / "publication-current").readlink(), before)
            self.assertEqual((old / "marker").read_bytes(), b"old")

    def test_publish_emits_six_files_in_content_addressed_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = publisher.publish(
                make_valid_rows(), Path(tmp), graph_sha256=GRAPH_SHA,
                profile_manifest_sha256=MANIFEST_SHA,
            )
            current = Path(tmp) / "publication-current"
            self.assertTrue(current.is_symlink())
            self.assertEqual(paths.root, current.resolve())
            self.assertEqual(set(path.name for path in paths.files), set(publisher.OUTPUT_NAMES))
            with paths.csv.open(newline="", encoding="utf-8") as stream:
                self.assertEqual(len(list(csv.DictReader(stream))), 16)

    def test_late_validation_failure_preserves_existing_publication(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old = root / "publication-old"
            old.mkdir()
            (old / "marker").write_bytes(b"old")
            (root / "publication-current").symlink_to(old.name)
            with mock.patch(
                "scripts.validate_gapbs_g14_4thread_latency_results.validate_directory",
                side_effect=RuntimeError("injected validation failure"),
            ), self.assertRaisesRegex(RuntimeError, "injected"):
                publisher.publish(
                    make_valid_rows(), root, graph_sha256=GRAPH_SHA,
                    profile_manifest_sha256=MANIFEST_SHA,
                )
            self.assertEqual((root / "publication-current").resolve(), old)
            self.assertEqual((old / "marker").read_bytes(), b"old")


if __name__ == "__main__":
    unittest.main()
