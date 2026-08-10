# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

import json
import tempfile
import unittest
from collections import namedtuple
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from scripts import run_gapbs_g14_4thread_latency_sweep as sweep


class G14SweepTest(unittest.TestCase):
    def options(self, root):
        return SimpleNamespace(
            root=root,
            graph=root / "graphs/g14.sg",
            graph_manifest=root / "graphs/g14.manifest.json",
            policy=root / "policy/cira-lead.json",
            gem5=root / "gem5.opt",
            config=root / "config.py",
            cxlmemuring=root / "CXLMemUring",
            m2ndp_root=root / "M2NDP",
            timeout=0,
            resume=False,
            only_latency=None,
            stop_after=None,
        )

    def test_matrix_is_exactly_latency_major_sixteen_actions(self):
        matrix = sweep.build_matrix()
        self.assertEqual(len(matrix), 16)
        self.assertEqual(
            tuple((entry.latency, entry.system) for entry in matrix),
            tuple(
                (latency, system)
                for latency in ("200ns", "500ns", "1us", "2us")
                for system in ("vanilla", "amu", "cira", "m2ndp")
            ),
        )

    def test_lead_scaling_uses_frozen_1us_policy(self):
        self.assertEqual(sweep.lead_for_latency(2, "200ns"), 1)
        self.assertEqual(sweep.lead_for_latency(2, "500ns"), 1)
        self.assertEqual(sweep.lead_for_latency(2, "1us"), 2)
        self.assertEqual(sweep.lead_for_latency(2, "2us"), 4)

    def test_commands_bind_g14_manifest_and_formal_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            options = self.options(root)
            paths = sweep.make_paths(options)
            amu = sweep.command_for_action(
                sweep.MatrixEntry("500ns", "amu"), options, paths
            )
            vanilla = sweep.command_for_action(
                sweep.MatrixEntry("1us", "vanilla"), options, paths
            )
        for command in (amu, vanilla):
            self.assertIn("g14-4thread-sweep", command)
            self.assertIn(str(options.graph_manifest.resolve()), command)
            self.assertIn(str(options.graph.resolve()), command)
        self.assertIn("--kind", amu)
        self.assertEqual(amu[amu.index("--kind") + 1], "amu")
        self.assertIn("--stop-after", vanilla)
        self.assertEqual(
            vanilla[vanilla.index("--stop-after") + 1], "gem5_baseline"
        )

    def test_retry_resume_flag_does_not_change_recorded_vanilla_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            options = self.options(root)
            paths = sweep.make_paths(options)
            entry = sweep.MatrixEntry("1us", "vanilla")
            before = sweep.command_for_action(entry, options, paths)
            status = paths.runs / "1us/m2ndp/status.json"
            status.parent.mkdir(parents=True)
            status.write_text("{}\n", encoding="utf-8")
            after = sweep.command_for_action(entry, options, paths)
            execution = sweep.execution_command(entry, after, paths)
        self.assertEqual(before, after)
        self.assertNotIn("--resume", after)
        self.assertIn("--resume", execution)

    def test_external_root_requires_space_and_exact_stable_symlink(self):
        Usage = namedtuple("Usage", "total used free")
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp)
            external = parent / "external"
            external.mkdir()
            link = parent / "stable"
            link.symlink_to(external, target_is_directory=True)
            with mock.patch.object(
                sweep.shutil, "disk_usage",
                return_value=Usage(200 << 30, 1, 101 << 30),
            ):
                sweep.require_external_root(
                    external, stable_link=link, expected_root=external
                )
            with mock.patch.object(
                sweep.shutil, "disk_usage",
                return_value=Usage(200 << 30, 101 << 30, 99 << 30),
            ), self.assertRaisesRegex(sweep.SweepError, "100 GiB"):
                sweep.require_external_root(
                    external, stable_link=link, expected_root=external
                )
            wrong = parent / "wrong"
            wrong.mkdir()
            link.unlink()
            link.symlink_to(wrong, target_is_directory=True)
            with mock.patch.object(
                sweep.shutil, "disk_usage",
                return_value=Usage(200 << 30, 1, 101 << 30),
            ), self.assertRaisesRegex(sweep.SweepError, "stable link"):
                sweep.require_external_root(
                    external, stable_link=link, expected_root=external
                )

    def test_root_filesystem_is_rejected(self):
        with self.assertRaisesRegex(sweep.SweepError, "filesystem root"):
            sweep.require_external_root(
                Path("/"), stable_link=Path("/tmp/not-used"),
                expected_root=Path("/"),
            )

    def test_resume_rejects_command_input_and_output_drift(self):
        record = sweep.passed_record(
            command=("run", "1us", "cira"),
            input_hashes={"graph": "a" * 64, "binary": "b" * 64,
                          "config": "c" * 64, "policy": "d" * 64},
            output_hashes={"summary": "e" * 64,
                           "checkpoint": "f" * 64},
        )
        sweep.validate_passed_record(
            record,
            command=("run", "1us", "cira"),
            input_hashes={"graph": "a" * 64, "binary": "b" * 64,
                          "config": "c" * 64, "policy": "d" * 64},
            output_hashes={"summary": "e" * 64,
                           "checkpoint": "f" * 64},
        )
        for field, replacement in (
            ("command", ("run", "2us", "cira")),
            ("input_hashes", {"graph": "0" * 64}),
            ("output_hashes", {"summary": "0" * 64}),
        ):
            kwargs = {
                "command": ("run", "1us", "cira"),
                "input_hashes": {"graph": "a" * 64, "binary": "b" * 64,
                                 "config": "c" * 64, "policy": "d" * 64},
                "output_hashes": {"summary": "e" * 64,
                                  "checkpoint": "f" * 64},
            }
            kwargs[field] = replacement
            with self.subTest(field=field), self.assertRaisesRegex(
                sweep.SweepError, "resume"
            ):
                sweep.validate_passed_record(record, **kwargs)

    def test_next_action_is_strictly_sequential(self):
        state = sweep.new_state()
        first = sweep.next_action(state)
        self.assertEqual(first, sweep.MatrixEntry("200ns", "vanilla"))
        state["latencies"]["200ns"]["vanilla"] = {
            **sweep.passed_record(
                command=("one",), input_hashes={}, output_hashes={}
            ),
            "output_paths": {},
        }
        self.assertEqual(
            sweep.next_action(state), sweep.MatrixEntry("200ns", "amu")
        )

    def test_policy_loader_rejects_nonqualification_shape(self):
        with tempfile.TemporaryDirectory() as tmp:
            policy = Path(tmp) / "cira-lead.json"
            policy.write_text(json.dumps({"selected_1us_lead_blocks": 3}))
            with self.assertRaisesRegex(sweep.SweepError, "policy"):
                sweep.load_policy(policy)


if __name__ == "__main__":
    unittest.main()
