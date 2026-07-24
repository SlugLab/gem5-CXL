# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

import importlib.util
import hashlib
import tempfile
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[3]
ROI_STATE_PATH = (
    REPO / "configs" / "example" / "gem5_library" / "gapbs_roi_state.py"
)
CHECKPOINT_PATH = REPO / "scripts" / "gapbs_checkpoint.py"


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


roi = load_module("gapbs_checkpoint_roi_state", ROI_STATE_PATH)


class GapbsCheckpointStateTest(unittest.TestCase):
    def test_save_stops_at_measured_begin(self):
        state = roi.GapbsCheckpointState(
            mode="save", iterations=2, measure_trial=1
        )
        self.assertEqual(state.work_begin(), ())
        self.assertEqual(state.work_end(), ())
        self.assertEqual(state.work_begin(), ("checkpoint",))
        self.assertEqual(state.checkpoint_saved(), ("stop",))
        self.assertEqual(state.finish(), ())

    def test_restore_resets_before_first_resumed_instruction(self):
        state = roi.GapbsCheckpointState(
            mode="restore", iterations=2, measure_trial=1
        )
        self.assertEqual(
            state.resume_actions(), ("reset", "record_start_tick")
        )
        self.assertEqual(state.work_end(), ("dump",))
        self.assertEqual(state.finish(), ("verify",))

    def test_restore_rejects_work_begin_and_missing_end(self):
        state = roi.GapbsCheckpointState(
            mode="restore", iterations=2, measure_trial=1
        )
        state.resume_actions()
        with self.assertRaisesRegex(roi.RoiSequenceError, "unexpected begin"):
            state.work_begin()
        with self.assertRaisesRegex(
            roi.RoiSequenceError, "missing measured trial 1 end"
        ):
            state.finish()


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
