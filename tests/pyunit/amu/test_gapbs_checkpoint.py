# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

import importlib.util
import hashlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[3]
ROI_STATE_PATH = (
    REPO / "configs" / "example" / "gem5_library" / "gapbs_roi_state.py"
)
CHECKPOINT_PATH = REPO / "scripts" / "gapbs_checkpoint.py"
RUNNER_PATH = REPO / "scripts" / "compare_gapbs_cxl_amu_cira.py"


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


roi = load_module("gapbs_checkpoint_roi_state", ROI_STATE_PATH)


class GapbsCheckpointStateTest(unittest.TestCase):
    def test_save_stops_at_trial_zero_begin(self):
        state = roi.GapbsCheckpointState(
            mode="save", iterations=2, measure_trial=1
        )
        self.assertEqual(state.work_begin(), ("checkpoint",))
        self.assertEqual(state.checkpoint_saved(), ("stop",))
        self.assertEqual(state.finish(), ())

    def test_restore_warms_trial_zero_then_measures_trial_one(self):
        state = roi.GapbsCheckpointState(
            mode="restore", iterations=2, measure_trial=1
        )
        self.assertEqual(state.resume_actions(), ())
        self.assertEqual(state.work_end(), ())
        self.assertEqual(
            state.work_begin(), ("reset", "record_start_tick")
        )
        self.assertEqual(state.work_end(), ("dump",))
        self.assertEqual(state.finish(), ("verify",))

    def test_restore_rejects_trial_one_begin_before_trial_zero_end(self):
        state = roi.GapbsCheckpointState(
            mode="restore", iterations=2, measure_trial=1
        )
        state.resume_actions()
        with self.assertRaisesRegex(roi.RoiSequenceError, "begin before"):
            state.work_begin()
        with self.assertRaisesRegex(
            roi.RoiSequenceError, "missing trial 0 end"
        ):
            state.finish()


class GapbsCheckpointConfigContractTest(unittest.TestCase):
    def test_config_declares_checkpoint_contract(self):
        config = (
            REPO
            / "configs"
            / "example"
            / "gem5_library"
            / "x86-gapbs-amu-se.py"
        ).read_text(encoding="utf-8")
        self.assertIn("CheckpointResource", config)
        self.assertIn('"--checkpoint-save"', config)
        self.assertIn('"--checkpoint-restore"', config)
        self.assertIn('"--require-m5-verification-exit"', config)
        self.assertIn("GAPBS_CHECKPOINT_SAVED", config)
        self.assertIn("GAPBS_CHECKPOINT_RESTORED", config)
        self.assertIn("simulator.save_checkpoint", config)
        self.assertIn("args.require_m5_verification_exit", config)
        self.assertIn("validate_checkpoint_options(", config)

    def assert_checkpoint_options_rejected(self, message, **overrides):
        options = {
            "checkpoint_save": None,
            "checkpoint_restore": None,
            "cxl_memory": False,
            "cpu": "timing",
            "roi_work_events": True,
            "continue_after_roi": True,
            "fast_forward_cpu": None,
            "iterations": 2,
            "measure_trial": 1,
        }
        options.update(overrides)
        with self.assertRaisesRegex(ValueError, message):
            roi.validate_checkpoint_options(**options)

    def test_checkpoint_option_exclusion_rules(self):
        self.assert_checkpoint_options_rejected(
            "mutually exclusive",
            checkpoint_save=Path("save"),
            checkpoint_restore=Path("restore"),
        )
        self.assert_checkpoint_options_rejected(
            "save requires local memory",
            checkpoint_save=Path("save"),
            cxl_memory=True,
        )
        for overrides, message in (
            (
                {
                    "checkpoint_restore": Path("restore"),
                    "cxl_memory": False,
                },
                "restore requires --cxl-memory",
            ),
            (
                {
                    "checkpoint_restore": Path("restore"),
                    "cpu": "atomic",
                    "cxl_memory": True,
                },
                "restore requires --cpu timing",
            ),
            (
                {
                    "checkpoint_restore": Path("restore"),
                    "roi_work_events": False,
                    "cxl_memory": True,
                },
                "checkpoint mode requires --roi-work-events",
            ),
            (
                {
                    "checkpoint_restore": Path("restore"),
                    "continue_after_roi": False,
                    "cxl_memory": True,
                },
                "restore requires --continue-after-roi",
            ),
            (
                {
                    "checkpoint_save": Path("save"),
                    "fast_forward_cpu": "atomic",
                },
                "checkpoint mode rejects --fast-forward-cpu",
            ),
            (
                {
                    "checkpoint_save": Path("save"),
                    "iterations": 1,
                    "measure_trial": 0,
                },
                "checkpoint mode requires iterations=2 and measure_trial=1",
            ),
        ):
            with self.subTest(overrides=overrides):
                self.assert_checkpoint_options_rejected(message, **overrides)

    def test_valid_save_and_restore_options(self):
        roi.validate_checkpoint_options(
            checkpoint_save=Path("save"),
            checkpoint_restore=None,
            cxl_memory=False,
            cpu="atomic",
            roi_work_events=True,
            continue_after_roi=False,
            fast_forward_cpu=None,
            iterations=2,
            measure_trial=1,
        )
        roi.validate_checkpoint_options(
            checkpoint_save=None,
            checkpoint_restore=Path("restore"),
            cxl_memory=True,
            cpu="timing",
            roi_work_events=True,
            continue_after_roi=True,
            fast_forward_cpu=None,
            iterations=2,
            measure_trial=1,
        )


class GapbsCheckpointRunnerContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.runner = load_module("gapbs_checkpoint_runner", RUNNER_PATH)

    def test_checkpoint_summary_schema_records_provenance(self):
        expected = {
            "graph_path",
            "graph_scale",
            "graph_sha256",
            "checkpoint_id",
            "checkpoint_manifest",
            "checkpoint_binary_sha256",
            "checkpoint_restores",
        }
        self.assertTrue(expected <= set(self.runner.SUMMARY_FIELDS))

    def test_dry_run_builds_local_save_and_cxl_restore_commands(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binary_dir = root / "baseline"
            binary_dir.mkdir()
            (binary_dir / "pr").write_bytes(b"binary")
            graph = root / "g20.sg"
            graph.write_bytes(b"graph")
            gem5 = root / "gem5.opt"
            gem5.write_bytes(b"gem5")
            checkpoint_root = root / "checkpoints"
            outdir = root / "out"
            command = [
                sys.executable,
                str(RUNNER_PATH),
                "--gem5",
                str(gem5),
                "--baseline-bin-dir",
                str(binary_dir),
                "--benchmarks",
                "pr",
                "--graph",
                str(graph),
                "--graph-scale",
                "20",
                "--iterations",
                "2",
                "--measure-trial",
                "1",
                "--cpu",
                "timing",
                "--cores",
                "2",
                "--checkpoint-root",
                str(checkpoint_root),
                "--cxl-link-delay",
                "1us",
                "--roi-work-events",
                "--verify",
                "--dry-run",
                "--outdir",
                str(outdir),
            ]
            proc = subprocess.run(
                command,
                text=True,
                capture_output=True,
            )
        output = proc.stdout + proc.stderr
        self.assertEqual(proc.returncode, 0, output)
        self.assertIn("--checkpoint-save", output)
        self.assertIn("--cpu atomic", output)
        self.assertIn("--cxl-link-delay 0ns", output)
        self.assertIn("--checkpoint-restore", output)
        self.assertIn("--cpu timing", output)
        self.assertIn("--cxl-memory", output)
        self.assertIn("--cxl-link-delay 1us", output)
        absolute_graph = str(graph.resolve())
        self.assertIn(
            f"--arguments -f {absolute_graph} -n 2 -v",
            output,
        )
        self.assertNotIn("--fast-forward-cpu", output)
        self.assertNotIn("--arguments -g 20", output)


class GapbsCheckpointManifestTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.checkpoint = load_module(
            "gapbs_checkpoint_manifest", CHECKPOINT_PATH
        )

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        temporary = Path(self.temporary.name)
        self.binary = temporary / "binary"
        self.graph = temporary / "g20.sg"
        self.gem5 = temporary / "gem5.opt"
        self.config = temporary / "config.py"
        self.root = temporary / "checkpoint"
        self.root.mkdir()
        (self.root / "m5.cpt").write_text("checkpoint")
        for path, payload in (
            (self.binary, b"binary"),
            (self.graph, b"graph"),
            (self.gem5, b"gem5"),
            (self.config, b"config"),
        ):
            path.write_bytes(payload)
        self.identity = self.checkpoint.build_identity(
            binary=self.binary,
            graph=self.graph,
            graph_scale=20,
            arguments=["-f", str(self.graph), "-n", "2", "-v"],
            cores=2,
            memory_size="4GiB",
            gem5=self.gem5,
            config=self.config,
            kind="baseline",
            model_parameters={"cxl_link_delay": "0ns"},
        )

    def tearDown(self):
        self.temporary.cleanup()

    @staticmethod
    def sha256(path):
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()

    def test_identity_covers_every_portability_input(self):
        identity = self.identity
        self.assertEqual(identity["schema"], 1)
        self.assertEqual(identity["graph_scale"], 20)
        self.assertEqual(identity["graph_sha256"], self.sha256(self.graph))
        self.assertEqual(
            identity["binary_sha256"], self.sha256(self.binary)
        )
        self.assertEqual(identity["arguments"][0], "-f")
        self.assertEqual(len(self.checkpoint.identity_key(identity)), 64)

    def test_reuse_rejects_changed_graph_or_incomplete_checkpoint(self):
        self.checkpoint.write_manifest(self.root, self.identity)
        self.assertTrue(
            self.checkpoint.validate_reuse(self.root, self.identity)
        )
        changed = dict(self.identity, graph_sha256="0" * 64)
        with self.assertRaisesRegex(
            self.checkpoint.CheckpointError, "identity mismatch"
        ):
            self.checkpoint.validate_reuse(self.root, changed)
        (self.root / "m5.cpt").unlink()
        with self.assertRaisesRegex(
            self.checkpoint.CheckpointError,
            "missing checkpoint payload",
        ):
            self.checkpoint.validate_reuse(self.root, self.identity)


if __name__ == "__main__":
    unittest.main()
