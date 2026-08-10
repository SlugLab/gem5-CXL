# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

import dataclasses
import json
import os
import struct
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import m2ndp_artifacts as artifacts
from scripts import run_m2ndp_g20_pr_spmv as runner
from scripts import gapbs_pr_experiment_profiles as profiles


class OrchestratorTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.outdir = root / "run"
        self.outdir.mkdir()
        self.graph = root / "g20.sg"
        self.graph.write_bytes(b"graph")
        self.options = runner.Options(
            graph=self.graph,
            graph_scale=20,
            cxlmemuring=root / "CXLMemUring",
            m2ndp_root=root / "M2NDP-public",
            gem5=root / "gem5.opt",
            outdir=self.outdir,
            smoke_test=False,
            resume=False,
            timeout=0,
            stop_after=None,
        )
        self.paths = runner.make_paths(self.options)

    def state_with(self, stage, *, status, outputs=None):
        state = runner.new_state(self.options)
        state["stages"][stage]["status"] = status
        state["stages"][stage]["inputs"] = {}
        state["stages"][stage]["outputs"] = outputs or {}
        return state

    def test_publication_command_is_two_core_all_cxl_trial_one(self):
        command = runner.gem5_command(self.options, self.paths)
        for option, value in (
            ("--benchmarks", "pr_spmv"),
            ("--cores", "2"),
            ("--cpu", "timing"),
            ("--cxl-link-delay", "1us"),
            ("--iterations", "2"),
            ("--measure-trial", "1"),
        ):
            self.assertIn(option, command)
            self.assertEqual(command[command.index(option) + 1], value)
        self.assertIn("--checkpoint-root", command)
        self.assertIn("--roi-work-events", command)
        self.assertIn("--verify", command)

    def test_g4_commands_use_four_cores_and_selected_latency(self):
        options = dataclasses.replace(
            self.options,
            profile="g4-4thread-sweep",
            graph_scale=4,
            cxl_link_delay="200ns",
        )

        gem5 = runner.gem5_command(options, self.paths)
        calibrated = runner._calibration_command(options, self.paths)

        self.assertEqual(gem5[gem5.index("--cores") + 1], "4")
        self.assertEqual(
            gem5[gem5.index("--cxl-link-delay") + 1], "200ns"
        )
        self.assertEqual(
            calibrated[calibrated.index("--cxl-delay") + 1], "200ns"
        )

    def test_g4_state_records_immutable_profile_contract(self):
        options = dataclasses.replace(
            self.options,
            profile="g4-4thread-sweep",
            graph_scale=4,
            cxl_link_delay="2us",
        )

        contract = runner.new_state(options)["contract"]

        self.assertEqual(contract["profile"], "g4-4thread-sweep")
        self.assertEqual(contract["cores"], 4)
        self.assertEqual(contract["threads"], 4)
        self.assertEqual(contract["cxl_link_delay"], "2us")

    def test_manifest_backed_g14_profile_is_bound_to_every_command(self):
        root = self.outdir.parent
        graph = root / "g14.sg"
        generator = root / "converter"
        nodes = 1 << 14
        graph.write_bytes(
            struct.pack("<?qq", False, 1, nodes)
            + struct.pack(f"<{nodes + 1}q", *([0] * nodes + [1]))
            + struct.pack("<i", 0)
        )
        generator.write_bytes(b"generator")
        os.chmod(generator, 0o755)
        manifest = root / "g14.manifest.json"
        manifest.write_text(
            json.dumps(
                {
                    "schema": 1,
                    "scale": 14,
                    "graph": str(graph.resolve()),
                    "graph_sha256": artifacts.sha256_file(graph),
                    "generator": str(generator.resolve()),
                    "generator_sha256": artifacts.sha256_file(generator),
                    "generator_command": [
                        str(generator.resolve()), "-g", "14", "-b",
                        str(graph.resolve()),
                    ],
                    "num_nodes": nodes,
                    "directed_edges": 1,
                }
            ) + "\n",
            encoding="utf-8",
        )
        options = dataclasses.replace(
            self.options,
            graph=graph,
            graph_scale=14,
            profile="g14-4thread-sweep",
            profile_manifest=manifest,
            cxl_link_delay="500ns",
        )

        profile = runner._experiment_profile(options)
        contract = runner.new_state(options)["contract"]
        command = runner.gem5_command(options, self.paths)

        self.assertEqual(profile.num_nodes, nodes)
        self.assertEqual(contract["profile_manifest_sha256"],
                         artifacts.sha256_file(manifest))
        self.assertIn("--graph-manifest", command)
        self.assertEqual(
            command[command.index("--graph-manifest") + 1],
            str(manifest.resolve()),
        )

    def test_resume_migrates_legacy_g20_contract(self):
        legacy = runner.new_state(self.options)
        for field in ("profile", "graph_sha256", "threads"):
            legacy["contract"].pop(field)
        self.paths.status.write_text(
            json.dumps(legacy), encoding="utf-8"
        )
        options = dataclasses.replace(self.options, resume=True)

        state = runner._load_or_create_state(options, self.paths)

        self.assertEqual(
            state["contract"], runner.new_state(options)["contract"]
        )

    def test_failed_funcsim_blocks_ndpsim_and_summary(self):
        state = self.state_with("funcsim", status="failed")
        with self.assertRaisesRegex(
            artifacts.EvidenceError, "FuncSim"
        ):
            runner.next_stage(state)
        self.assertFalse((self.outdir / "summary.csv").exists())

    def test_resume_does_not_repeat_hashed_passed_stage(self):
        meta = self.outdir / "graph.meta.json"
        meta.write_text("complete\n")
        state = self.state_with(
            "graph_export",
            status="passed",
            outputs={
                "graph.meta.json": artifacts.sha256_file(meta),
            },
        )
        self.assertFalse(
            runner.should_run("graph_export", state, self.outdir)
        )

    def test_changed_output_invalidates_stage_and_downstream(self):
        meta = self.outdir / "graph.meta.json"
        meta.write_text("old\n")
        state = self.state_with(
            "graph_export",
            status="passed",
            outputs={
                "graph.meta.json": artifacts.sha256_file(meta),
            },
        )
        state["stages"]["gem5_baseline"]["status"] = "passed"
        meta.write_text("changed\n")

        runner.invalidate_mismatched_stages(state, self.outdir)

        self.assertEqual(
            state["stages"]["graph_export"]["status"], "pending"
        )
        self.assertEqual(
            state["stages"]["gem5_baseline"]["status"], "pending"
        )

    def test_tree_hash_changes_when_nested_file_changes(self):
        tree = self.outdir / "tree"
        tree.mkdir()
        nested = tree / "value"
        nested.write_text("one")
        before = runner.hash_path(tree)
        nested.write_text("two")
        self.assertNotEqual(before, runner.hash_path(tree))

    def test_funcsim_output_directory_exists_before_launch(self):
        def fake_run(*_args, **_kwargs):
            self.assertTrue(self.paths.funcsim_dump.parent.is_dir())
            return 2

        with mock.patch.object(runner, "_run_command", fake_run):
            with self.assertRaises(runner.StageCommandError):
                runner._execute_stage(
                    "funcsim",
                    self.options,
                    self.paths,
                    ["FuncSim"],
                    self.paths.logs / "funcsim.log",
                )

    def test_calibration_exports_persistent_ndpsim_runtime_library(self):
        captured = {}

        def fake_run(*_args, **kwargs):
            captured.update(kwargs)
            return 2

        runtime_library = self.paths.tools / "lib/libNDPSim_lib.so"
        runtime_library.parent.mkdir(parents=True)
        runtime_library.write_bytes(b"runtime library")
        with (
            mock.patch.dict(
                os.environ, {"LD_LIBRARY_PATH": "/existing"}, clear=False
            ),
            mock.patch.object(runner, "_run_command", fake_run),
            self.assertRaises(runner.StageCommandError),
        ):
            runner._execute_stage(
                "calibration",
                self.options,
                self.paths,
                ["calibrate"],
                self.paths.logs / "calibration.log",
            )

        environment = captured.get("env")
        self.assertIsNotNone(environment)
        self.assertEqual(
            environment["LD_LIBRARY_PATH"],
            f"{runtime_library.parent}:/existing",
        )


if __name__ == "__main__":
    unittest.main()
