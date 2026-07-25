# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import m2ndp_artifacts as artifacts
from scripts import run_m2ndp_g20_pr_spmv as runner


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


if __name__ == "__main__":
    unittest.main()
