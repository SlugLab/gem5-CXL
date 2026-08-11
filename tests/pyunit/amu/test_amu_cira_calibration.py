# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

import math
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from scripts import amu_cira_calibration as calibration
from scripts import run_amu_paper_calibration as runner


PDF = Path(
    os.environ.get(
        "AMU_PDF", "/home/victoryang00/gem5-CXL/3663479.pdf"
    )
)
CSV = Path(
    os.environ.get(
        "CIRA_CSV",
        "/root/ia780i_type2_delay_buffer_new/"
        "benchmark_gapbs_workloads_ci_long.csv",
    )
)
REPO = Path(__file__).resolve().parents[3]
PROXY = REPO / "util/amu/amu_paper_profile.cc"


class CalibrationSourceTest(unittest.TestCase):
    def test_amu_source_hash_and_direct_parameters(self):
        facts = calibration.load_amu_source(PDF)
        self.assertEqual(facts["sha256"], calibration.AMU_PDF_SHA256)
        self.assertEqual(facts["direct"]["spm_bytes"], 64 * 1024)
        self.assertEqual(facts["direct"]["pending_entries"], 32)
        self.assertEqual(facts["direct"]["id_batch_entries"], 32)
        self.assertEqual(
            facts["direct"]["latency_us"], [0.1, 0.2, 0.5, 1, 2, 5]
        )
        self.assertEqual(facts["validation"]["gups_5us_min_mlp"], 130)
        self.assertEqual(
            facts["classification"]["mean_speedup_1us"], "validation"
        )

    def test_amu_table4_is_numeric_validation_not_a_parameter(self):
        facts = calibration.load_amu_source(PDF)
        self.assertEqual(
            facts["validation"]["table4"]["gups"]["1"],
            {"baseline": 4.40, "amu": 0.98},
        )
        self.assertNotIn("mean_speedup_1us", facts["direct"])
        self.assertNotIn("table4", facts["direct"])

    def test_cira_excludes_failed_pr_and_preserves_fallbacks(self):
        facts = calibration.load_cira_source(CSV)
        self.assertNotIn("pr", facts["verified_workloads"])
        self.assertEqual(facts["primary"]["workload"], "pr_spmv")
        self.assertTrue(
            math.isclose(
                facts["primary"]["pgo_over_static"],
                1.004128673,
                rel_tol=1e-9,
            )
        )
        self.assertEqual(facts["rows"]["bfs"]["B"]["selected_from"], "")
        self.assertIn(
            "fell back", facts["rows"]["bfs"]["B"]["fallback"]
        )

    def test_cira_geomeans_use_only_seven_verified_workloads(self):
        facts = calibration.load_cira_source(CSV)
        self.assertEqual(
            facts["verified_workloads"],
            ["bc", "bfs", "cc", "cc_sv", "pr_spmv", "sssp", "tc"],
        )
        self.assertTrue(
            math.isclose(
                facts["geomean"]["static"], 0.884214397, rel_tol=1e-9
            )
        )
        self.assertTrue(
            math.isclose(
                facts["geomean"]["pgo_selected"],
                0.892296283,
                rel_tol=1e-9,
            )
        )

    def test_wrong_source_hash_fails_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            wrong = Path(temporary) / "source"
            wrong.write_bytes(b"not the approved source")
            with self.assertRaisesRegex(
                calibration.CalibrationError, "SHA-256"
            ):
                calibration.load_amu_source(wrong)
            with self.assertRaisesRegex(
                calibration.CalibrationError, "SHA-256"
            ):
                calibration.load_cira_source(wrong)


class CalibrationFitTest(unittest.TestCase):
    def test_fit_uses_numeric_training_rows_and_reports_holdout(self):
        measurements = calibration.synthetic_measurements_for_test()
        result = calibration.fit_amu_control_costs(
            measurements,
            holdout={"workload": "stream", "latency_us": 2.0},
        )
        self.assertEqual(
            result["objective"], "normalized_time_weighted_sse"
        )
        self.assertNotIn("stream@2", result["training_points"])
        self.assertIn("stream@2", result["holdout_residuals"])
        self.assertEqual(
            result["parameters"],
            {
                "metadata_cycles": 4,
                "id_refill_cycles": 6,
                "completion_cycles": 2,
            },
        )

    def test_speedup_is_never_a_direct_parameter(self):
        result = calibration.fit_amu_control_costs(
            calibration.synthetic_measurements_for_test(),
            holdout={"workload": "stream", "latency_us": 2.0},
        )
        self.assertNotIn("speedup", result["parameters"])
        self.assertNotIn("speedup", calibration.AMU_SEARCH_SPACE)

    def test_fit_rejects_duplicate_holdout_or_invalid_counts(self):
        rows = calibration.synthetic_measurements_for_test()
        rows.append(dict(rows[-1]))
        with self.assertRaisesRegex(
            calibration.CalibrationError, "duplicate measurement"
        ):
            calibration.fit_amu_control_costs(
                rows, holdout={"workload": "stream", "latency_us": 2.0}
            )
        invalid = calibration.synthetic_measurements_for_test()
        invalid[0]["metadata_accesses"] = -1
        with self.assertRaisesRegex(
            calibration.CalibrationError, "metadata_accesses"
        ):
            calibration.fit_amu_control_costs(
                invalid,
                holdout={"workload": "stream", "latency_us": 2.0},
            )


class CalibrationRunnerTest(unittest.TestCase):
    def _collect_options(self, root):
        root = Path(root)
        inputs = root / "inputs"
        inputs.mkdir()
        paths = {}
        for name in (
            "gem5",
            "config.py",
            "libm5.a",
            "paper.pdf",
            "hardware.csv",
        ):
            path = inputs / name
            path.write_bytes(f"frozen {name}\n".encode("utf-8"))
            paths[name] = path
        return SimpleNamespace(
            command="collect",
            gem5=paths["gem5"],
            config=paths["config.py"],
            m5_library=paths["libm5.a"],
            pdf=paths["paper.pdf"],
            cira_csv=paths["hardware.csv"],
            cxx="g++",
            outdir=root / "evidence",
            measurements=root / "measurements.csv",
            collection_manifest=root / "collection.json",
            iterations=1,
            dry_run=False,
        )

    def test_collect_cli_requires_pdf_and_hardware_csv(self):
        common = [
            "collect",
            "--gem5",
            "gem5.opt",
            "--config",
            "config.py",
            "--m5-library",
            "libm5.a",
            "--outdir",
            "evidence",
            "--measurements",
            "measurements.csv",
        ]
        with self.assertRaises(SystemExit):
            runner.parse_args(common)
        parsed = runner.parse_args(
            [*common, "--pdf", "paper.pdf", "--cira-csv", "hardware.csv"]
        )
        self.assertEqual(parsed.pdf, Path("paper.pdf"))
        self.assertEqual(parsed.cira_csv, Path("hardware.csv"))

    def test_git_provenance_captures_clean_commit_and_rejects_dirty_tree(self):
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary) / "repo"
            repo.mkdir()
            subprocess.run(
                ["git", "init", "-b", "freeze-test"], cwd=repo,
                check=True, stdout=subprocess.DEVNULL
            )
            subprocess.run(
                ["git", "config", "user.name", "Calibration Test"],
                cwd=repo, check=True
            )
            subprocess.run(
                ["git", "config", "user.email", "test@example.invalid"],
                cwd=repo, check=True
            )
            tracked = repo / "tracked"
            tracked.write_text("frozen\n", encoding="utf-8")
            subprocess.run(["git", "add", "tracked"], cwd=repo, check=True)
            subprocess.run(
                ["git", "commit", "-m", "freeze"], cwd=repo,
                check=True, stdout=subprocess.DEVNULL
            )
            expected_commit = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=repo,
                check=True, text=True, stdout=subprocess.PIPE
            ).stdout.strip()

            provenance = runner._git_provenance(repo)
            self.assertEqual(
                provenance,
                {
                    "commit": expected_commit,
                    "branch": "freeze-test",
                    "clean": True,
                },
            )
            (repo / "untracked").write_text("dirty\n", encoding="utf-8")
            with self.assertRaisesRegex(
                calibration.CalibrationError, "dirty worktree"
            ):
                runner._git_provenance(repo)

    def test_collect_rejects_reused_artifacts_before_build(self):
        for artifact in ("outdir", "measurements", "collection_manifest"):
            with self.subTest(
                artifact=artifact
            ), tempfile.TemporaryDirectory() as temporary:
                options = self._collect_options(temporary)
                target = getattr(options, artifact)
                if artifact == "outdir":
                    target.mkdir()
                else:
                    target.write_text("existing\n", encoding="utf-8")
                with mock.patch.object(
                    runner, "_git_provenance",
                    return_value={
                        "commit": "a" * 40,
                        "branch": "test",
                        "clean": True,
                    },
                ), mock.patch.object(runner.subprocess, "run") as run:
                    with self.assertRaisesRegex(
                        calibration.CalibrationError, "already exists"
                    ):
                        runner.run_collect(options)
                    run.assert_not_called()

    def test_collect_does_not_overwrite_manifest_created_by_build(self):
        with tempfile.TemporaryDirectory() as temporary:
            options = self._collect_options(temporary)
            plan = runner.collect_plan(options)
            foreign_manifest = b"created concurrently by proxy build\n"
            simulations = []

            def fake_run(command, **kwargs):
                if command == plan["build"]:
                    binary = Path(command[-1])
                    binary.parent.mkdir(parents=True, exist_ok=True)
                    binary.write_bytes(b"frozen proxy binary\n")
                    options.collection_manifest.write_bytes(foreign_manifest)
                else:
                    simulations.append(command)
                return subprocess.CompletedProcess(command, 0)

            with mock.patch.object(
                runner, "_git_provenance",
                return_value={
                    "commit": "e" * 40,
                    "branch": "freeze",
                    "clean": True,
                },
            ), mock.patch.object(
                runner.subprocess, "run", side_effect=fake_run
            ), mock.patch.object(
                runner, "_materialize_register_checksum"
            ), mock.patch.object(
                runner,
                "_measurement_rows",
                return_value=calibration.paper_measurements_for_test(),
            ):
                with self.assertRaisesRegex(
                    calibration.CalibrationError,
                    "collection manifest already exists",
                ):
                    runner.run_collect(options)

            self.assertEqual(
                options.collection_manifest.read_bytes(), foreign_manifest
            )
            self.assertEqual(simulations, [])

    def test_collect_does_not_overwrite_measurements_created_during_run(self):
        with tempfile.TemporaryDirectory() as temporary:
            options = self._collect_options(temporary)
            plan = runner.collect_plan(options)
            foreign_measurements = b"created concurrently during collection\n"
            simulations = 0

            def fake_run(command, **kwargs):
                nonlocal simulations
                if command == plan["build"]:
                    binary = Path(command[-1])
                    binary.parent.mkdir(parents=True, exist_ok=True)
                    binary.write_bytes(b"frozen proxy binary\n")
                else:
                    simulations += 1
                    if simulations == 36:
                        options.measurements.write_bytes(foreign_measurements)
                return subprocess.CompletedProcess(command, 0)

            with mock.patch.object(
                runner, "_git_provenance",
                return_value={
                    "commit": "f" * 40,
                    "branch": "freeze",
                    "clean": True,
                },
            ), mock.patch.object(
                runner.subprocess, "run", side_effect=fake_run
            ), mock.patch.object(
                runner, "_materialize_register_checksum"
            ), mock.patch.object(
                runner,
                "_measurement_rows",
                return_value=calibration.paper_measurements_for_test(),
            ):
                with self.assertRaisesRegex(
                    calibration.CalibrationError,
                    "measurements file already exists",
                ):
                    runner.run_collect(options)

            self.assertEqual(
                options.measurements.read_bytes(), foreign_measurements
            )
            manifest = runner.load_json(options.collection_manifest)
            self.assertEqual(manifest["status"], "failed")
            self.assertIn(
                "measurements file already exists",
                manifest["failure_reason"],
            )

    def test_collect_freezes_and_completes_manifest_after_input_rehash(self):
        with tempfile.TemporaryDirectory() as temporary:
            options = self._collect_options(temporary)
            seen_statuses = []
            real_atomic_write = runner.atomic_write_json

            def record_manifest(path, value, **kwargs):
                if Path(path) == options.collection_manifest:
                    seen_statuses.append(value["status"])
                real_atomic_write(path, value, **kwargs)

            def fake_run(command, **kwargs):
                if command == runner.collect_plan(options)["build"]:
                    binary = Path(command[-1])
                    binary.parent.mkdir(parents=True, exist_ok=True)
                    binary.write_bytes(b"frozen proxy binary\n")
                return subprocess.CompletedProcess(command, 0)

            rows = calibration.paper_measurements_for_test()
            with mock.patch.object(
                runner, "_git_provenance",
                return_value={
                    "commit": "b" * 40,
                    "branch": "freeze",
                    "clean": True,
                },
            ), mock.patch.object( \
                runner, "atomic_write_json", side_effect=record_manifest
            ), \
                 mock.patch.object(runner.subprocess, "run", side_effect=fake_run), \
                 mock.patch.object(runner, "_materialize_register_checksum"), \
                 mock.patch.object(runner, "_measurement_rows", return_value=rows):
                self.assertEqual(runner.run_collect(options), 0)

            self.assertEqual(seen_statuses, ["in_progress", "complete"])
            manifest = runner.load_json(options.collection_manifest)
            self.assertEqual(
                manifest["schema"], "amu-paper-calibration-collection"
            )
            self.assertEqual(manifest["version"], 1)
            self.assertEqual(manifest["status"], "complete")
            self.assertEqual(manifest["git"]["commit"], "b" * 40)
            self.assertTrue(manifest["git"]["clean"])
            self.assertEqual(
                set(manifest["inputs"]),
                {
                    "gem5",
                    "config",
                    "m5_library",
                    "amu_pdf",
                    "cira_csv",
                    "proxy",
                },
            )
            self.assertEqual(len(manifest["plan"]["runs"]), 36)
            self.assertEqual(manifest["plan"]["expected_simulations"], 36)
            self.assertEqual(manifest["plan"]["expected_measurement_rows"], 18)
            self.assertEqual(manifest["actual"]["completed_simulations"], 36)
            self.assertEqual(manifest["actual"]["measurement_rows"], 18)
            self.assertIn("started_utc", manifest["timestamps"])
            self.assertIn("completed_utc", manifest["timestamps"])
            self.assertIn("platform", manifest["host"])
            for planned, actual in zip(
                manifest["plan"]["runs"], runner.collect_plan(options)["runs"]
            ):
                self.assertEqual(planned["argv"], actual["command"])

    def test_collect_marks_manifest_failed_when_frozen_input_changes(self):
        with tempfile.TemporaryDirectory() as temporary:
            options = self._collect_options(temporary)
            plan = runner.collect_plan(options)
            simulation_started = False

            def fake_run(command, **kwargs):
                nonlocal simulation_started
                if command == plan["build"]:
                    binary = Path(command[-1])
                    binary.parent.mkdir(parents=True, exist_ok=True)
                    binary.write_bytes(b"frozen proxy binary\n")
                elif not simulation_started:
                    simulation_started = True
                    options.config.write_text(
                        "changed during collection\n", encoding="utf-8"
                    )
                return subprocess.CompletedProcess(command, 0)

            with mock.patch.object(
                runner, "_git_provenance",
                return_value={
                    "commit": "c" * 40,
                    "branch": "freeze",
                    "clean": True,
                },
            ), mock.patch.object(runner.subprocess, "run", side_effect=fake_run), \
                 mock.patch.object(runner, "_materialize_register_checksum"), \
                 mock.patch.object(
                     runner, "_measurement_rows",
                     return_value=calibration.paper_measurements_for_test(),
                 ):
                with self.assertRaisesRegex(
                    calibration.CalibrationError, "input changed"
                ):
                    runner.run_collect(options)

            self.assertFalse(options.measurements.exists())
            manifest = runner.load_json(options.collection_manifest)
            self.assertEqual(manifest["status"], "failed")
            self.assertIn("input changed", manifest["failure_reason"])
            self.assertNotIn("completed_utc", manifest["timestamps"])

    def test_collect_fails_if_in_progress_manifest_is_modified(self):
        with tempfile.TemporaryDirectory() as temporary:
            options = self._collect_options(temporary)
            plan = runner.collect_plan(options)
            simulation_started = False

            def fake_run(command, **kwargs):
                nonlocal simulation_started
                if command == plan["build"]:
                    binary = Path(command[-1])
                    binary.parent.mkdir(parents=True, exist_ok=True)
                    binary.write_bytes(b"frozen proxy binary\n")
                elif not simulation_started:
                    simulation_started = True
                    manifest = runner.load_json(options.collection_manifest)
                    self.assertEqual(manifest["status"], "in_progress")
                    options.collection_manifest.write_text(
                        '{"status":"tampered"}\n', encoding="utf-8"
                    )
                return subprocess.CompletedProcess(command, 0)

            with mock.patch.object(
                runner, "_git_provenance",
                return_value={
                    "commit": "d" * 40,
                    "branch": "freeze",
                    "clean": True,
                },
            ), mock.patch.object(
                runner.subprocess, "run", side_effect=fake_run
            ), mock.patch.object(
                runner, "_materialize_register_checksum"
            ), mock.patch.object(
                runner,
                "_measurement_rows",
                return_value=calibration.paper_measurements_for_test(),
            ):
                with self.assertRaisesRegex(
                    calibration.CalibrationError,
                    "collection manifest changed",
                ):
                    runner.run_collect(options)

            manifest = runner.load_json(options.collection_manifest)
            self.assertEqual(manifest["status"], "failed")
            self.assertIn(
                "collection manifest changed", manifest["failure_reason"]
            )

    def test_proxy_source_has_paper_workloads_and_bit_checks(self):
        source = PROXY.read_text(encoding="utf-8")
        for token in (
            'workload == "gups"',
            'workload == "hj"',
            'workload != "stream"',
            "kHashBuckets = 16000",
            "static_assert(sizeof(HashNode) == 48",
            "kStreamGranularity = 512",
            "m5_work_begin",
            "m5_work_end",
            "amu_aload",
            "amu_astore",
            "prepareAndPrime",
            "runKernel",
            "checksum",
            "PROXY_CHECKSUM",
            "kChecksumMagic = 0x414d5531",
            "m5_sum",
        ):
            self.assertIn(token, source)

    def test_collect_plan_is_full_table2_proxy_matrix(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            options = runner.parse_args(
                [
                    "collect",
                    "--gem5",
                    str(REPO / "build/X86/gem5.opt"),
                    "--config",
                    str(
                        REPO
                        / "configs/example/gem5_library/"
                        "x86-gapbs-amu-se.py"
                    ),
                    "--m5-library",
                    str(REPO / "util/m5/build/x86/out/libm5.a"),
                    "--pdf",
                    str(PDF),
                    "--cira-csv",
                    str(CSV),
                    "--outdir",
                    str(root / "runs"),
                    "--measurements",
                    str(root / "measurements.csv"),
                    "--dry-run",
                ]
            )
            plan = runner.collect_plan(options)
            self.assertEqual(len(plan["runs"]), 3 * 6 * 2)
            self.assertIn("-ffp-contract=off", plan["build"])
            for run in plan["runs"]:
                command = run["command"]
                self.assertIn("--cores", command)
                self.assertIn("--cxl-memory", command)
                self.assertIn("--roi-work-events", command)
                self.assertIn("--debug-flags=PseudoInst", command)
                self.assertIn("--disable-hw-prefetchers", command)
                self.assertIn("64KiB", command)
                self.assertIn(run["latency"], command)
                if run["kind"] == "baseline":
                    self.assertIn("--no-asmc", command)
                else:
                    self.assertNotIn("--no-asmc", command)

    def test_register_checksum_transport_is_tagged_and_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run_dir = root / "run"
            run_dir.mkdir()
            raw = run_dir / "checksum.u64"
            record = {
                "run_dir": str(run_dir),
                "raw": str(raw),
                "workload": "stream",
                "kind": "amu",
            }
            marker = (
                "7: global: pseudo_inst::m5sum(0xb0232525, "
                "0x6b7b0b31, 0x414d5531, 0x3, 0x1, 0)\n"
            )
            (run_dir / "gem5.log").write_text(marker, encoding="utf-8")
            self.assertEqual(
                runner._materialize_register_checksum(record),
                "6b7b0b31b0232525",
            )
            self.assertEqual(
                raw.read_bytes(), bytes.fromhex("252523b0310b7b6b")
            )

            for bad in (marker + marker, marker.replace("0x3", "0x2", 1)):
                (run_dir / "gem5.log").write_text(bad, encoding="utf-8")
                with self.assertRaises(calibration.CalibrationError):
                    runner._materialize_register_checksum(record)

    def test_fit_cli_writes_hash_bound_deterministic_manifest(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            measurements = root / "measurements.csv"
            output_a = root / "a.json"
            output_b = root / "b.json"
            runner.write_measurements(
                measurements, calibration.paper_measurements_for_test()
            )
            common = [
                "fit",
                "--measurements",
                str(measurements),
                "--pdf",
                str(PDF),
                "--cira-csv",
                str(CSV),
                "--holdout-workload",
                "stream",
                "--holdout-latency",
                "2us",
            ]
            self.assertEqual(runner.main([*common, "--output", str(output_a)]), 0)
            self.assertEqual(runner.main([*common, "--output", str(output_b)]), 0)
            self.assertEqual(output_a.read_bytes(), output_b.read_bytes())
            manifest = runner.load_json(output_a)
            self.assertEqual(manifest["schema"], 1)
            self.assertEqual(
                manifest["sources"]["amu_pdf"]["sha256"],
                calibration.AMU_PDF_SHA256,
            )
            self.assertEqual(
                manifest["sources"]["cira_csv"]["sha256"],
                calibration.CIRA_CSV_SHA256,
            )
            self.assertEqual(
                manifest["amu"]["fit"]["parameters"],
                {
                    "metadata_cycles": 4,
                    "id_refill_cycles": 6,
                    "completion_cycles": 2,
                },
            )
            self.assertEqual(manifest["amu"]["validation"]["status"], "PASS")
            self.assertEqual(manifest["amu"]["proxy_isa"], "x86")
            self.assertEqual(manifest["amu"]["paper_isa"], "RISC-V")

    def test_fit_cli_rejects_checksum_or_mlp_failure(self):
        rows = calibration.paper_measurements_for_test()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            measurements = root / "measurements.csv"
            output = root / "manifest.json"
            bad_checksum = [dict(row) for row in rows]
            bad_checksum[0]["amu_checksum"] = "different"
            runner.write_measurements(measurements, bad_checksum)
            with self.assertRaisesRegex(
                calibration.CalibrationError, "checksum"
            ):
                runner.main(
                    runner.fit_arguments(measurements, PDF, CSV, output)
                )
            low_mlp = [dict(row) for row in rows]
            for row in low_mlp:
                if row["workload"] == "gups" and row["latency_us"] == 5.0:
                    row["average_mlp"] = 130.0
            runner.write_measurements(measurements, low_mlp)
            with self.assertRaisesRegex(
                calibration.CalibrationError, "GUPS 5us MLP"
            ):
                runner.main(
                    runner.fit_arguments(measurements, PDF, CSV, output)
                )


if __name__ == "__main__":
    unittest.main()
