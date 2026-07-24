# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

import importlib.util
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[3]
ROI_STATE_PATH = (
    REPO / "configs" / "example" / "gem5_library" / "gapbs_roi_state.py"
)


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


if __name__ == "__main__":
    unittest.main()
