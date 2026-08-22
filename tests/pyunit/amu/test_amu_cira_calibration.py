# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

import inspect
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
SPATTER_CSV = Path(
    os.environ.get(
        "CIRA_SPATTER_CSV",
        "/root/ia780i_type2_delay_buffer_new/"
        "benchmark_spatter_workloads_ci_long.csv",
    )
)
REPO = Path(__file__).resolve().parents[3]
PROXY = REPO / "util/amu/amu_paper_profile.cc"


class CalibrationSourceTest(unittest.TestCase):
    def test_proxy_emits_one_work_event_pair_per_iteration(self):
        source = PROXY.read_text(encoding="utf-8")
        main = source[source.index("\nmain(int argc") :]
        self.assertIn(
            "for (size_t iteration = 0; iteration < options.iterations;",
            main,
        )
        self.assertIn("m5_work_begin(iteration, 0)", main)
        self.assertIn("runKernelIteration(options, state, scheduler.get(), iteration)", main)
        self.assertIn("m5_work_end(iteration, 0)", main)

    def test_proxy_scheduler_control_state_is_hoisted_before_roi(self):
        source = PROXY.read_text(encoding="utf-8")
        main = source[source.index("\nmain(int argc") :]
        scheduler = main.index("std::make_unique<PersistentScheduler>")
        roi = main.index("m5_work_begin")
        self.assertLess(scheduler, roi)
        for name, following in (
            ("runGupsAmu", "initializeHashQueries"),
            ("runHashJoinAmu", "runStreamBaseline"),
            ("runStreamAmu", "runKernelIteration"),
        ):
            start = source.index(f"\n{name}(")
            body = source[start : source.index(f"\n{following}(", start)]
            self.assertNotIn("PersistentScheduler scheduler", body)

    def test_proxy_scheduler_uses_a_direct_packed_token_owner_map(self):
        source = PROXY.read_text(encoding="utf-8")
        scheduler = source[
            source.index("class PersistentScheduler"):
            source.index("\nvoid\nprimeSpm", source.index("class PersistentScheduler"))
        ]
        self.assertNotIn("IdOwner", scheduler)
        self.assertNotIn("idOwners", scheduler)
        self.assertIn("ownerWords", scheduler)
        self.assertIn("findOwnerToken", scheduler)
        self.assertIn("static_assert(sizeof(Slot) * kWindowSlots <= 8 * 1024", source)
        self.assertIn("kOwnerEntries = 1 << kCompletionTokenBits", source)
        self.assertIn("return ownerWords[token] & kOwnerLive", scheduler)
        self.assertIn("ownerWords[token] != 0", scheduler)
        self.assertIn("static_assert(sizeof(uint32_t) * kOwnerEntries <= 128 * 1024", source)

    def test_gups_uses_the_paper_handwritten_completion_hot_path(self):
        source = PROXY.read_text(encoding="utf-8")
        scheduler = source[
            source.index("class PersistentScheduler"):
            source.index("\nvoid\nprimeSpm", source.index("class PersistentScheduler"))
        ]
        start = source.index("\nrefillGupsSlot(")
        body = source[start : source.index("\nvoid\ninitializeHashQueries", start)]
        self.assertIn("waitCompletionOwners", scheduler)
        self.assertIn("issueGupsLoad", scheduler)
        self.assertIn("issueGupsStore", scheduler)
        self.assertIn("scheduler.waitCompletionOwners", body)
        self.assertIn("scheduler.issueGupsLoad", body)
        self.assertNotIn("scheduler.waitCompletionBatch", body)
        self.assertNotIn("scheduler.issueLoad", body)
        self.assertNotIn("scheduler.slot", body)
        self.assertNotIn("scheduler.payload", body)

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

    def test_spatter_rows_are_direct_policy_evidence_not_speedup_targets(self):
        facts = calibration.load_cira_spatter_source(SPATTER_CSV)
        self.assertEqual(
            facts["sha256"], calibration.CIRA_SPATTER_CSV_SHA256
        )
        for workload in ("amg_gather", "lulesh_scatter"):
            row = calibration.classify_breadth_cira_evidence(
                workload,
                trace_identity={
                    "input_sha256": "a" * 64,
                    "source_sha256": "b" * 64,
                    "roi_sha256": "c" * 64,
                },
                spatter=facts,
            )
            self.assertEqual(row["classification"], "direct_cira_policy")
            self.assertFalse(row["fit_source_speedup"])
            self.assertEqual(set(row["modes"]), {
                "baseline", "A", "B", "C", "ABC",
            })

    def test_unmatched_mcf_cg_mg_rows_are_component_costs_only(self):
        trace_identity = {
            "input_sha256": "1" * 64,
            "source_sha256": "2" * 64,
            "roi_sha256": "3" * 64,
        }
        hardware_identity = {
            "input_sha256": "1" * 64,
            "source_sha256": "9" * 64,
            "roi_sha256": "3" * 64,
        }
        for workload in ("mcf", "npb_cg", "npb_mg"):
            with self.subTest(workload=workload):
                row = calibration.classify_breadth_cira_evidence(
                    workload,
                    trace_identity=trace_identity,
                    hardware_identity=hardware_identity,
                )
                self.assertEqual(
                    row["classification"], "component_costs_only"
                )
                self.assertEqual(row["mismatched_identity"], [
                    "source_sha256"
                ])
                self.assertFalse(row["fit_source_speedup"])

        exact = calibration.classify_breadth_cira_evidence(
            "npb_cg",
            trace_identity=trace_identity,
            hardware_identity=trace_identity,
        )
        self.assertEqual(exact["classification"], "direct_cira_policy")

    def test_synthetic_mcf_can_never_be_a_345_mb_speedup_target(self):
        with self.assertRaisesRegex(
            calibration.CalibrationError, "synthetic MCF.*speedup target"
        ):
            calibration.classify_breadth_cira_evidence(
                "mcf",
                trace_identity={
                    "input_sha256": "1" * 64,
                    "source_sha256": "2" * 64,
                    "roi_sha256": "3" * 64,
                },
                hardware_identity={
                    "input_sha256": "1" * 64,
                    "source_sha256": "2" * 64,
                    "roi_sha256": "3" * 64,
                },
                synthetic=True,
            )


class CalibrationFitTest(unittest.TestCase):
    def test_mlp_capacity_analysis_proves_nonnegative_fit_infeasible(self):
        measurements = calibration.paper_measurements_for_test()
        point = next(
            row
            for row in measurements
            if row["workload"] == "gups" and row["latency_us"] == 5.0
        )
        point.update(
            {
                "simulated_normalized_time": 4.42,
                "normalizer_ticks": 1_000_000.0,
                "occupancy_ticks": 4_420_000.0,
                "outstanding_integral": 1_051_822_875.0,
                "average_mlp": 237.9689762443439,
                "peak_mlp": 256,
                "max_mlp": 256,
            }
        )

        result = calibration.analyze_amu_proxy_feasibility(measurements)

        self.assertEqual(
            result["status"], "INFEASIBLE_NONNEGATIVE_COSTS"
        )
        gups = result["points"]["gups@5"]
        self.assertGreater(gups["required_average_mlp"], 1_020)
        self.assertGreater(gups["mlp_capacity_floor"], 4.10)
        self.assertGreater(gups["mlp_capacity_floor"], gups["target"])
        self.assertIn("MLP_CAPACITY", gups["reasons"])
        self.assertIn("ZERO_COST_PROXY", gups["reasons"])

    def test_mlp_capacity_analysis_rejects_inconsistent_raw_stats(self):
        measurements = calibration.paper_measurements_for_test()
        measurements[0]["average_mlp"] += 1.0
        with self.assertRaisesRegex(
            calibration.CalibrationError, "average_mlp differs"
        ):
            calibration.analyze_amu_proxy_feasibility(measurements)

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
            "gapbs_roi_state.py",
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

    def _terminal_path(self, options, status):
        manifest = options.collection_manifest
        return manifest.with_name(
            f"{manifest.stem}.{status}{manifest.suffix}"
        )

    def _is_build(self, command, options):
        return Path(command[-1]) == (
            options.outdir / "bin/amu_paper_profile"
        ).resolve()

    def test_measurement_parser_rejects_nonzero_amu_failure_counters(self):
        clean_stats = {
            "simTicks": 1000,
            "board.asmc.metadataAccesses": 6,
            "board.asmc.idBatchRefills": 1,
            "board.asmc.completedLoads": 2,
            "board.asmc.completedStores": 1,
            "board.asmc.avgOutstanding": 2.5,
            "board.asmc.rejectedQueueFull": 0,
            "board.asmc.rejectedSpmFull": 0,
            "board.asmc.translationFaults": 0,
            "board.asmc.pendingQueueFull": 0,
            "board.asmc.farSpmFlagPackets": 0,
            "board.asmc.spmMissingFlagPackets": 0,
        }
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary)
            raw = run_dir / "checksum.raw"
            raw.write_bytes(b"\0" * 8)
            record = {
                "run_dir": str(run_dir),
                "raw": str(raw),
                "kind": "amu",
            }
            for suffix in (
                "rejectedQueueFull",
                "rejectedSpmFull",
                "translationFaults",
                "pendingQueueFull",
                "farSpmFlagPackets",
                "spmMissingFlagPackets",
            ):
                stats = dict(clean_stats)
                stats[f"board.asmc.{suffix}"] = 1
                with self.subTest(counter=suffix), mock.patch(
                    "scripts.compare_gapbs_cxl_amu_cira.parse_stats",
                    return_value=stats,
                ):
                    with self.assertRaisesRegex(
                        calibration.CalibrationError,
                        f"nonzero AMU failure counter .{suffix}",
                    ):
                        runner._parse_run(record)

    def test_measurement_parser_preserves_mlp_capacity_evidence(self):
        stats = {
            "simTicks": 1000,
            "board.asmc.metadataAccesses": 6,
            "board.asmc.idBatchRefills": 1,
            "board.asmc.completedLoads": 2,
            "board.asmc.completedStores": 1,
            "board.asmc.outstandingIntegral": 2500,
            "board.asmc.occupancyTicks": 1000,
            "board.asmc.avgOutstanding": 2.5,
            "board.asmc.maxObservedOutstanding": 3,
            "board.asmc.rejectedQueueFull": 0,
            "board.asmc.rejectedSpmFull": 0,
            "board.asmc.translationFaults": 0,
            "board.asmc.pendingQueueFull": 0,
            "board.asmc.farSpmFlagPackets": 0,
            "board.asmc.spmMissingFlagPackets": 0,
        }
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary)
            raw = run_dir / "checksum.raw"
            raw.write_bytes(b"\0" * 8)
            (run_dir / "config.ini").write_text(
                "[board.asmc]\n"
                "calibration_profile=paper-calibration-base\n"
                "calibration_manifest_sha256=\n"
                "spm_size=65536\n"
                "pending_queue_entries=32\n"
                "id_batch_entries=32\n"
                "metadata_latency=0\n"
                "id_refill_latency=0\n"
                "completion_publish_latency=0\n"
                "max_outstanding=256\n",
                encoding="utf-8",
            )
            record = {
                "run_dir": str(run_dir),
                "raw": str(raw),
                "kind": "amu",
            }
            with mock.patch(
                "scripts.compare_gapbs_cxl_amu_cira.parse_stats",
                return_value=stats,
            ):
                parsed = runner._parse_run(record)
        self.assertEqual(parsed["outstanding_integral"], 2500)
        self.assertEqual(parsed["occupancy_ticks"], 1000)
        self.assertEqual(parsed["peak_mlp"], 3)
        self.assertEqual(parsed["max_mlp"], 256)

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
        sources = ["--pdf", "paper.pdf", "--cira-csv", "hardware.csv"]
        with self.assertRaises(SystemExit):
            runner.parse_args([*common, *sources])
        parsed = runner.parse_args([
            *common,
            *sources,
            "--collection-manifest",
            "collection.json",
        ])
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
        for artifact in (
            "outdir",
            "measurements",
            "collection_manifest",
            "complete",
            "failed",
        ):
            with self.subTest(
                artifact=artifact
            ), tempfile.TemporaryDirectory() as temporary:
                options = self._collect_options(temporary)
                target = (
                    self._terminal_path(options, artifact)
                    if artifact in {"complete", "failed"}
                    else getattr(options, artifact)
                )
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

    def test_collect_rejects_dangling_output_symlinks_lexically(self):
        for artifact in ("outdir", "collection_manifest"):
            with self.subTest(
                artifact=artifact
            ), tempfile.TemporaryDirectory() as temporary:
                options = self._collect_options(temporary)
                link = getattr(options, artifact)
                foreign = Path(temporary) / f"foreign-{artifact}"
                link.symlink_to(
                    foreign, target_is_directory=artifact == "outdir"
                )
                original_link = os.readlink(link)

                with mock.patch.object(
                    runner, "_git_provenance",
                    return_value={
                        "commit": "5" * 40,
                        "branch": "freeze",
                        "clean": True,
                    },
                ), mock.patch.object(runner.subprocess, "run") as run:
                    with self.assertRaisesRegex(
                        calibration.CalibrationError, "already exists"
                    ):
                        runner.run_collect(options)

                run.assert_not_called()
                self.assertTrue(link.is_symlink())
                self.assertEqual(os.readlink(link), original_link)
                self.assertFalse(foreign.exists())

    def test_collect_rejects_bidirectional_output_ancestor_collisions(self):
        cases = (
            "manifest_owns_outdir",
            "measurements_owns_outdir",
            "file_owns_file",
        )
        for case in cases:
            with self.subTest(
                case=case
            ), tempfile.TemporaryDirectory() as temporary:
                options = self._collect_options(temporary)
                root = Path(temporary)
                if case == "manifest_owns_outdir":
                    options.collection_manifest = root / "collision"
                    options.outdir = options.collection_manifest / "evidence"
                elif case == "measurements_owns_outdir":
                    options.measurements = root / "collision"
                    options.outdir = options.measurements / "evidence"
                else:
                    options.measurements = root / "collision"
                    options.collection_manifest = (
                        options.measurements / "collection.json"
                    )

                with mock.patch.object(
                    runner, "_git_provenance",
                    return_value={
                        "commit": "6" * 40,
                        "branch": "freeze",
                        "clean": True,
                    },
                ), mock.patch.object(runner.subprocess, "run") as run:
                    with self.assertRaisesRegex(
                        calibration.CalibrationError, "output paths overlap"
                    ):
                        runner.run_collect(options)

                run.assert_not_called()

    def test_collect_atomically_rejects_concurrently_claimed_root(self):
        with tempfile.TemporaryDirectory() as temporary:
            options = self._collect_options(temporary)
            marker = options.outdir / "foreign"
            original_mkdir = Path.mkdir
            raced = False

            def racing_mkdir(path, *args, **kwargs):
                nonlocal raced
                if (
                    Path(path) == options.outdir
                    and kwargs.get("exist_ok") is False
                    and not raced
                ):
                    raced = True
                    original_mkdir(path)
                    marker.write_text("foreign\n", encoding="utf-8")
                return original_mkdir(path, *args, **kwargs)

            with mock.patch.object(
                runner, "_git_provenance",
                return_value={
                    "commit": "1" * 40,
                    "branch": "freeze",
                    "clean": True,
                },
            ), mock.patch.object(Path, "mkdir", new=racing_mkdir), \
                 mock.patch.object(runner.subprocess, "run") as run:
                with self.assertRaisesRegex(
                    calibration.CalibrationError, "evidence root already exists"
                ):
                    runner.run_collect(options)

            run.assert_not_called()
            self.assertEqual(marker.read_text(encoding="utf-8"), "foreign\n")

    def test_collect_build_failure_publishes_machine_readable_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            options = self._collect_options(temporary)

            def fail_build(command, **kwargs):
                raise subprocess.CalledProcessError(1, command)

            with mock.patch.object(
                runner, "_git_provenance",
                return_value={
                    "commit": "2" * 40,
                    "branch": "freeze",
                    "clean": True,
                },
            ), mock.patch.object(
                runner.subprocess, "run", side_effect=fail_build
            ):
                with self.assertRaises(subprocess.CalledProcessError):
                    runner.run_collect(options)

            failed = runner.load_json(self._terminal_path(options, "failed"))
            owner = options.outdir / "collection-owner.json"
            self.assertEqual(failed["status"], "failed")
            self.assertIn("CalledProcessError", failed["failure_reason"])
            self.assertEqual(
                failed["immutable_manifest_sha256"],
                calibration.sha256_file(owner),
            )

    def test_compiler_identity_verification_rejects_hash_drift(self):
        with tempfile.TemporaryDirectory() as temporary:
            compiler = Path(temporary) / "compiler"
            compiler.write_bytes(b"frozen compiler\n")
            compiler.chmod(0o755)
            identity = runner._compiler_identity(compiler)

            runner._verify_compiler_identity(identity)
            compiler.write_bytes(b"changed compiler\n")
            with self.assertRaisesRegex(
                calibration.CalibrationError, "compiler changed"
            ):
                runner._verify_compiler_identity(identity)

    def test_collect_rejects_copy_restore_snapshot_mismatch_before_build(self):
        with tempfile.TemporaryDirectory() as temporary:
            options = self._collect_options(temporary)
            original = options.config.read_bytes()
            real_copyfile = runner.shutil.copyfile

            def copy_changed_then_restored(source, destination):
                if Path(source).resolve() == options.config.resolve():
                    options.config.write_bytes(b"transient changed origin\n")
                    try:
                        return real_copyfile(source, destination)
                    finally:
                        options.config.write_bytes(original)
                return real_copyfile(source, destination)

            with mock.patch.object(
                runner, "_git_provenance",
                return_value={
                    "commit": "7" * 40,
                    "branch": "freeze",
                    "clean": True,
                },
            ), mock.patch.object(
                runner.shutil,
                "copyfile",
                side_effect=copy_changed_then_restored,
            ), mock.patch.object(runner.subprocess, "run") as run:
                with self.assertRaisesRegex(
                    calibration.CalibrationError, "snapshot hash mismatch"
                ):
                    runner.run_collect(options)

            run.assert_not_called()
            failed = runner.load_json(self._terminal_path(options, "failed"))
            self.assertEqual(failed["status"], "failed")

    def test_collect_does_not_overwrite_manifest_created_by_build(self):
        with tempfile.TemporaryDirectory() as temporary:
            options = self._collect_options(temporary)
            foreign_manifest = b"created concurrently by proxy build\n"
            simulations = []

            def fake_run(command, **kwargs):
                if self._is_build(command, options):
                    Path(command[-1]).write_bytes(b"frozen proxy binary\n")
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
            self.assertEqual(
                runner.load_json(
                    self._terminal_path(options, "failed")
                )["status"],
                "failed",
            )

    def test_collect_does_not_overwrite_measurements_created_during_run(self):
        with tempfile.TemporaryDirectory() as temporary:
            options = self._collect_options(temporary)
            foreign_measurements = b"created concurrently during collection\n"
            simulations = 0

            def fake_run(command, **kwargs):
                nonlocal simulations
                if self._is_build(command, options):
                    Path(command[-1]).write_bytes(b"frozen proxy binary\n")
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
            failed = runner.load_json(self._terminal_path(options, "failed"))
            self.assertEqual(failed["status"], "failed")
            self.assertIn(
                "measurements file already exists",
                failed["failure_reason"],
            )

    def test_collect_freezes_and_completes_immutable_manifest(self):
        with tempfile.TemporaryDirectory() as temporary:
            options = self._collect_options(temporary)
            executed = []

            def fake_run(command, **kwargs):
                executed.append((command, kwargs))
                if self._is_build(command, options):
                    Path(command[-1]).write_bytes(b"frozen proxy binary\n")
                return subprocess.CompletedProcess(command, 0)

            rows = calibration.paper_measurements_for_test()
            with mock.patch.object(
                runner, "_git_provenance",
                return_value={
                    "commit": "b" * 40,
                    "branch": "freeze",
                    "clean": True,
                },
            ), mock.patch.object(
                runner.subprocess, "run", side_effect=fake_run
            ), mock.patch.object(
                runner, "_materialize_register_checksum"
            ), mock.patch.object(
                runner, "_measurement_rows", return_value=rows
            ):
                self.assertEqual(runner.run_collect(options), 0)

            manifest = runner.load_json(options.collection_manifest)
            complete = runner.load_json(
                self._terminal_path(options, "complete")
            )
            self.assertEqual(manifest["status"], "in_progress")
            self.assertEqual(complete["status"], "complete")
            self.assertIsNone(complete["failure_reason"])
            self.assertEqual(
                complete["immutable_manifest_sha256"],
                calibration.sha256_file(options.collection_manifest),
            )
            self.assertEqual(complete["actual"]["completed_simulations"], 36)
            self.assertEqual(complete["actual"]["measurement_rows"], 18)
            self.assertEqual(
                complete["measurements"]["path"],
                str(options.measurements.resolve()),
            )
            self.assertEqual(
                complete["measurements"]["sha256"],
                calibration.sha256_file(options.measurements),
            )
            self.assertFalse(options.measurements.stat().st_mode & 0o222)
            self.assertIn("terminal_utc", complete)
            self.assertEqual(manifest["git"]["commit"], "b" * 40)
            self.assertEqual(
                manifest["outputs"]["complete_terminal"],
                str(self._terminal_path(options, "complete")),
            )
            self.assertEqual(
                manifest["outputs"]["failed_terminal"],
                str(self._terminal_path(options, "failed")),
            )
            self.assertEqual(
                set(manifest["inputs"]),
                {
                    "gem5",
                    "config",
                    "gapbs_roi_state",
                    "m5_library",
                    "amu_pdf",
                    "cira_csv",
                    "proxy",
                },
            )
            for record in manifest["inputs"].values():
                self.assertTrue(Path(record["origin_path"]).is_absolute())
                self.assertTrue(Path(record["frozen_path"]).is_absolute())
                self.assertEqual(
                    record["frozen_sha256"],
                    calibration.sha256_file(record["frozen_path"]),
                )
                self.assertFalse(
                    Path(record["frozen_path"]).stat().st_mode & 0o222
                )
            self.assertTrue(Path(manifest["compiler"]["path"]).is_absolute())
            self.assertIn("sha256", manifest["compiler"])
            self.assertTrue(Path(manifest["plan"]["build_cwd"]).is_absolute())
            self.assertTrue(all(kwargs.get("cwd") for _, kwargs in executed))
            for planned in manifest["plan"]["runs"]:
                self.assertTrue(Path(planned["cwd"]).is_absolute())
                self.assertTrue(Path(planned["argv"][0]).is_absolute())
                self.assertTrue(Path(planned["argv"][3]).is_absolute())

    def test_collect_rejects_measurements_mutated_before_complete_terminal(self):
        with tempfile.TemporaryDirectory() as temporary:
            options = self._collect_options(temporary)
            real_write_measurements = runner.write_measurements

            def fake_run(command, **kwargs):
                if self._is_build(command, options):
                    Path(command[-1]).write_bytes(b"frozen proxy binary\n")
                return subprocess.CompletedProcess(command, 0)

            def write_then_mutate(path, rows, **kwargs):
                expected_sha256 = real_write_measurements(
                    path, rows, **kwargs
                )
                Path(path).chmod(0o644)
                with Path(path).open("ab") as stream:
                    stream.write(b"mutated after publication\n")
                return expected_sha256

            with mock.patch.object(
                runner, "_git_provenance",
                return_value={
                    "commit": "8" * 40,
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
            ), mock.patch.object(
                runner,
                "write_measurements",
                side_effect=write_then_mutate,
            ):
                with self.assertRaisesRegex(
                    calibration.CalibrationError,
                    "published measurements changed",
                ):
                    runner.run_collect(options)

            self.assertFalse(
                self._terminal_path(options, "complete").exists()
            )
            failed = runner.load_json(self._terminal_path(options, "failed"))
            self.assertEqual(failed["status"], "failed")

    def test_collect_stops_before_next_run_if_frozen_input_changes(self):
        with tempfile.TemporaryDirectory() as temporary:
            options = self._collect_options(temporary)
            simulations = 0

            def fake_run(command, **kwargs):
                nonlocal simulations
                if self._is_build(command, options):
                    Path(command[-1]).write_bytes(b"frozen proxy binary\n")
                else:
                    simulations += 1
                    if simulations == 1:
                        frozen = options.outdir / "inputs/config.py"
                        frozen.chmod(0o644)
                        frozen.write_text("mutated frozen input\n", encoding="utf-8")
                return subprocess.CompletedProcess(command, 0)

            with mock.patch.object(
                runner, "_git_provenance",
                return_value={
                    "commit": "c" * 40,
                    "branch": "freeze",
                    "clean": True,
                },
            ), mock.patch.object(
                runner.subprocess, "run", side_effect=fake_run
            ), mock.patch.object(runner, "_materialize_register_checksum"):
                with self.assertRaisesRegex(
                    calibration.CalibrationError, "frozen input changed"
                ):
                    runner.run_collect(options)

            self.assertEqual(simulations, 1)
            failed = runner.load_json(self._terminal_path(options, "failed"))
            self.assertEqual(failed["status"], "failed")

    def test_collect_uses_frozen_absolute_argv_when_origin_changes_and_restores(self):
        with tempfile.TemporaryDirectory() as temporary:
            options = self._collect_options(temporary)
            original = options.config.read_bytes()
            simulation_commands = []

            def fake_run(command, **kwargs):
                if self._is_build(command, options):
                    Path(command[-1]).write_bytes(b"frozen proxy binary\n")
                else:
                    options.config.write_bytes(b"temporary origin change\n")
                    options.config.write_bytes(original)
                    simulation_commands.append(command)
                return subprocess.CompletedProcess(command, 0)

            with mock.patch.object(
                runner, "_git_provenance",
                return_value={
                    "commit": "3" * 40,
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
                self.assertEqual(runner.run_collect(options), 0)

            frozen_config = (options.outdir / "inputs/config.py").resolve()
            self.assertEqual(len(simulation_commands), 36)
            for command in simulation_commands:
                self.assertEqual(Path(command[3]), frozen_config)
                self.assertNotIn(str(options.config), command)

    def test_collect_preserves_tampered_manifest_and_publishes_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            options = self._collect_options(temporary)
            foreign_manifest = b'{"status":"foreign"}\n'
            simulation_started = False

            def fake_run(command, **kwargs):
                nonlocal simulation_started
                if self._is_build(command, options):
                    Path(command[-1]).write_bytes(b"frozen proxy binary\n")
                elif not simulation_started:
                    simulation_started = True
                    options.collection_manifest.write_bytes(foreign_manifest)
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
            ), mock.patch.object(runner, "_materialize_register_checksum"):
                with self.assertRaisesRegex(
                    calibration.CalibrationError,
                    "collection manifest changed",
                ):
                    runner.run_collect(options)

            self.assertEqual(
                options.collection_manifest.read_bytes(), foreign_manifest
            )
            failed = runner.load_json(self._terminal_path(options, "failed"))
            self.assertEqual(failed["status"], "failed")
            self.assertIn(
                "collection manifest changed", failed["failure_reason"]
            )

    def test_collect_does_not_overwrite_racing_complete_terminal(self):
        with tempfile.TemporaryDirectory() as temporary:
            options = self._collect_options(temporary)
            complete_path = self._terminal_path(options, "complete")
            foreign_complete = b'{"status":"foreign-complete"}\n'
            simulations = 0

            def fake_run(command, **kwargs):
                nonlocal simulations
                if self._is_build(command, options):
                    Path(command[-1]).write_bytes(b"frozen proxy binary\n")
                else:
                    simulations += 1
                    if simulations == 36:
                        complete_path.write_bytes(foreign_complete)
                return subprocess.CompletedProcess(command, 0)

            with mock.patch.object(
                runner, "_git_provenance",
                return_value={
                    "commit": "4" * 40,
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
                    "complete terminal already exists",
                ):
                    runner.run_collect(options)

            self.assertEqual(complete_path.read_bytes(), foreign_complete)
            self.assertEqual(
                runner.load_json(
                    self._terminal_path(options, "failed")
                )["status"],
                "failed",
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
                    "--collection-manifest",
                    str(root / "collection.json"),
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
                    self.assertNotIn("paper-calibration-base", command)
                else:
                    self.assertNotIn("--no-asmc", command)
                    profile = command.index("--asmc-profile")
                    self.assertEqual(
                        command[profile + 1], "paper-calibration-base"
                    )

    def test_two_iteration_collect_switches_atomic_to_o3_before_warmup(self):
        with tempfile.TemporaryDirectory() as temporary:
            options = self._collect_options(temporary)
            options.iterations = 2
            plan = runner.collect_plan(options)
        for run in plan["runs"]:
            command = run["command"]
            self.assertEqual(command[command.index("--cpu") + 1], "o3")
            self.assertEqual(
                command[command.index("--fast-forward-cpu") + 1], "atomic"
            )
            self.assertEqual(command[command.index("--iterations") + 1], "2")
            self.assertEqual(command[command.index("--measure-trial") + 1], "1")

    def test_collection_rejects_nonzero_embedded_control_costs(self):
        source = (
            REPO
            / "configs/example/gem5_library/x86-gapbs-amu-se.py"
        ).read_text(encoding="utf-8")
        self.assertIn('"paper-calibration-base"', source)
        self.assertIn('args.asmc_profile == "paper-calibration-base"', source)
        self.assertIn('"metadata_cycles": 0', source)
        self.assertIn('"id_refill_cycles": 0', source)
        self.assertIn('"completion_cycles": 0', source)
        parse_run = inspect.getsource(runner._parse_run)
        self.assertIn('"calibration_profile": "paper-calibration-base"', parse_run)
        self.assertIn('"metadata_latency": "0"', parse_run)

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
            self.assertEqual(manifest["schema"], 2)
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
            near_data = manifest["near_data_pr"]
            self.assertEqual(
                near_data["amu"]["fit_role"],
                "architecture_and_cross_workload_validation",
            )
            self.assertEqual(
                near_data["cira"]["fit_role"],
                "pr_spmv_policy_ranking",
            )
            self.assertFalse(near_data["formal_speedup_is_fit_target"])
            self.assertEqual(
                near_data["cira"]["selected_source_row"], "B"
            )
            self.assertEqual(
                len(near_data["cira"]["candidates"]["B"]["raw_times_ms"]),
                10,
            )
            for owner in (near_data["amu"], near_data["cira"]):
                self.assertIn("parameters", owner)
                self.assertIn("parameter_sources", owner)

    def test_infeasible_proxy_uses_architecture_defaults_not_fake_fit(self):
        rows = calibration.paper_measurements_for_test()
        point = next(
            row
            for row in rows
            if row["workload"] == "gups" and row["latency_us"] == 5.0
        )
        point.update(
            {
                "simulated_normalized_time": 4.42,
                "normalizer_ticks": 1_000_000.0,
                "occupancy_ticks": 4_420_000.0,
                "outstanding_integral": 1_051_822_875.0,
                "average_mlp": 237.9689762443439,
                "peak_mlp": 256,
                "max_mlp": 256,
            }
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            measurements = root / "measurements.csv"
            output = root / "manifest.json"
            runner.write_measurements(measurements, rows)
            self.assertEqual(
                runner.main(
                    runner.fit_arguments(measurements, PDF, CSV, output)
                ),
                0,
            )
            manifest = runner.load_json(output)

        amu = manifest["amu"]
        self.assertEqual(amu["validation"]["status"], "PASS")
        self.assertEqual(
            amu["validation"]["proxy_feasibility_status"],
            "INFEASIBLE_NONNEGATIVE_COSTS",
        )
        self.assertEqual(amu["fit"]["role"], "diagnostic_only")
        self.assertEqual(
            amu["formal_profile_selection"]["source"],
            "asmc_architecture_defaults",
        )
        self.assertFalse(
            amu["formal_profile_selection"]["fit_parameters_applied"]
        )
        self.assertEqual(
            {
                name: amu["formal_profile"][name]
                for name in (
                    "metadata_cycles",
                    "id_refill_cycles",
                    "completion_cycles",
                )
            },
            calibration.AMU_ARCHITECTURE_DEFAULTS,
        )

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
