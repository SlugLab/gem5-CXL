# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

import dataclasses
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import run_gapbs_g4_4thread_latency_sweep as runner


class SweepRunnerTest(unittest.TestCase):
    def make_options(self, root):
        return runner.Options(
            graph=root / "g4.sg",
            cxlmemuring=root / "CXLMemUring",
            m2ndp_root=root / "M2NDP-public",
            gem5=root / "gem5.opt",
            variants_build=root / "variants",
            outdir=root / "sweep",
            timeout=0,
            resume=False,
        )

    def test_matrix_has_four_latencies_and_four_systems(self):
        matrix = runner.build_matrix()
        self.assertEqual(len(matrix), 16)
        self.assertEqual(
            {item.latency for item in matrix},
            {"200ns", "500ns", "1us", "2us"},
        )
        self.assertEqual(
            {item.system for item in matrix},
            {"vanilla", "amu", "cira", "m2ndp"},
        )

    def test_failure_blocks_later_latency_and_publication(self):
        state = runner.new_state()
        state["latencies"]["500ns"]["cira"] = "failed"
        with self.assertRaisesRegex(runner.SweepError, "500ns/cira"):
            runner.next_action(state)

    def test_resume_requires_matching_output_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root / "summary.csv"
            output.write_text("original\n", encoding="utf-8")
            state = runner.new_state()
            runner.record_pass(state, "200ns", "amu", output)
            output.write_text("changed\n", encoding="utf-8")

            changed = runner.invalidate_changed_outputs(state, root)

        self.assertTrue(changed)
        self.assertEqual(
            state["latencies"]["200ns"]["amu"]["status"],
            "pending",
        )

    def test_vanilla_then_m2ndp_share_resumable_latency_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            options = self.make_options(Path(tmp))
            paths = runner.make_paths(options)
            vanilla = runner.command_for_action(
                runner.MatrixEntry("200ns", "vanilla"), options, paths
            )
            m2_status = paths.runs / "200ns/m2ndp/status.json"
            m2_status.parent.mkdir(parents=True)
            m2_status.write_text("{}\n", encoding="utf-8")
            m2ndp = runner.command_for_action(
                runner.MatrixEntry("200ns", "m2ndp"), options, paths
            )

        self.assertIn("--stop-after", vanilla)
        self.assertEqual(
            vanilla[vanilla.index("--stop-after") + 1], "gem5_baseline"
        )
        self.assertNotIn("--resume", vanilla)
        self.assertIn("--resume", m2ndp)
        self.assertNotIn("--stop-after", m2ndp)

    def test_matched_command_is_profile_latency_and_kind_specific(self):
        with tempfile.TemporaryDirectory() as tmp:
            options = self.make_options(Path(tmp))
            paths = runner.make_paths(options)

            command = runner.command_for_action(
                runner.MatrixEntry("500ns", "cira"), options, paths
            )

        for option, value in (
            ("--profile", "g4-4thread-sweep"),
            ("--graph-scale", "4"),
            ("--cxl-link-delay", "500ns"),
            ("--kind", "cira"),
        ):
            self.assertEqual(command[command.index(option) + 1], value)
        self.assertEqual(
            Path(command[command.index("--outdir") + 1]),
            paths.runs / "500ns/cira",
        )

    def test_state_contract_binds_all_external_inputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            options = self.make_options(Path(tmp))

            state = runner.new_state(options)

        self.assertEqual(
            state["contract"]["graph"], str(options.graph.resolve())
        )
        self.assertEqual(
            state["contract"]["variants_build"],
            str(options.variants_build.resolve()),
        )
        self.assertEqual(
            state["contract"]["m2ndp_root"],
            str(options.m2ndp_root.resolve()),
        )

    def test_failed_command_is_recorded_and_blocks_progress(self):
        with tempfile.TemporaryDirectory() as tmp:
            options = self.make_options(Path(tmp))
            paths = runner.make_paths(options)
            paths.logs.mkdir(parents=True)
            state = runner.new_state(options)
            entry = runner.MatrixEntry("200ns", "vanilla")
            with mock.patch.object(
                runner,
                "command_for_action",
                return_value=["/bin/sh", "-c", "exit 7"],
            ):
                with self.assertRaisesRegex(runner.SweepError, "exited 7"):
                    runner.run_action(entry, state, options, paths)

        record = state["latencies"]["200ns"]["vanilla"]
        self.assertEqual(record["status"], "failed")
        self.assertIn("exited 7", record["error"])
        with self.assertRaisesRegex(runner.SweepError, "200ns/vanilla"):
            runner.next_action(state)

    def test_successful_command_records_hashed_canonical_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            options = self.make_options(Path(tmp))
            paths = runner.make_paths(options)
            paths.logs.mkdir(parents=True)
            output = paths.root / "summary.csv"
            output.write_text("passed\n", encoding="utf-8")
            state = runner.new_state(options)
            entry = runner.MatrixEntry("200ns", "vanilla")
            with (
                mock.patch.object(
                    runner,
                    "command_for_action",
                    return_value=["/bin/sh", "-c", "exit 0"],
                ),
                mock.patch.object(
                    runner, "output_for_action", return_value=output
                ),
            ):
                runner.run_action(entry, state, options, paths)

        record = state["latencies"]["200ns"]["vanilla"]
        self.assertEqual(record["status"], "passed")
        self.assertEqual(record["output"], str(output.resolve()))
        self.assertEqual(len(record["output_sha256"]), 64)

    def test_resume_rejects_external_contract_drift(self):
        with tempfile.TemporaryDirectory() as tmp:
            options = self.make_options(Path(tmp))
            paths = runner.make_paths(options)
            paths.root.mkdir(parents=True)
            runner.artifacts.atomic_write_json(
                paths.status, runner.new_state(options)
            )
            changed = dataclasses.replace(
                options,
                variants_build=Path(tmp) / "different-variants",
                resume=True,
            )

            with self.assertRaisesRegex(
                runner.SweepError, "resume contract"
            ):
                runner.load_or_create_state(changed, paths)

    def test_formal_launch_rejects_wrong_graph_before_execution(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            options = self.make_options(root)
            options.graph.write_bytes(b"not the fixed g4 graph")
            options.gem5.write_bytes(b"gem5")
            options.cxlmemuring.mkdir()
            options.m2ndp_root.mkdir()
            options.variants_build.mkdir()

            with self.assertRaisesRegex(runner.SweepError, "graph SHA-256"):
                runner.validate_options(options, runner.make_paths(options))

    def test_cli_parses_explicit_roots_without_smoke_mode(self):
        options = runner.parse_args(
            [
                "--graph",
                "g4.sg",
                "--cxlmemuring",
                "CXLMemUring",
                "--m2ndp-root",
                "M2NDP-public",
                "--gem5",
                "gem5.opt",
                "--variants-build",
                "variants",
                "--outdir",
                "sweep",
            ]
        )

        self.assertFalse(options.resume)
        self.assertEqual(options.timeout, 0)
        self.assertEqual(options.graph.name, "g4.sg")


if __name__ == "__main__":
    unittest.main()
