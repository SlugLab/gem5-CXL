# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

"""Pure event sequencing for GAPBS work-begin/work-end annotations."""


class RoiSequenceError(RuntimeError):
    """Raised when GAPBS emits an incomplete or malformed ROI sequence."""


def _workload_integer_option(arguments, name):
    positions = [
        index for index, argument in enumerate(arguments) if argument == name
    ]
    if not positions:
        return None
    if len(positions) != 1:
        raise ValueError(f"workload arguments contain duplicate {name}")
    try:
        return int(arguments[positions[0] + 1])
    except (ValueError, IndexError):
        raise ValueError(f"{name} must be followed by an integer") from None


def resolve_workload_shape(
    arguments,
    configured_scale,
    configured_iterations,
    fast_forward,
):
    workload_scale = _workload_integer_option(arguments, "-g")
    workload_iterations = _workload_integer_option(arguments, "-n")

    if fast_forward:
        if (
            configured_scale != 20
            or configured_iterations != 2
            or workload_scale != 20
            or workload_iterations != 2
        ):
            raise ValueError(
                "fast-forward requires matching -g 20 -n 2"
            )
        return configured_scale, configured_iterations

    scale = (
        configured_scale
        if configured_scale is not None
        else workload_scale
    )
    iterations = (
        configured_iterations
        if configured_iterations is not None
        else workload_iterations
    )
    return scale, iterations if iterations is not None else 1


def classify_final_exit(exit_cause):
    if exit_cause == "m5_fail instruction encountered":
        return "fail", 2
    if exit_cause == "exiting with last active thread context":
        return "pass", 0
    return "missing", 3


class GapbsRoiState:
    def __init__(self, iterations, measure_trial, switch_at_trial_zero):
        if iterations <= 0:
            raise ValueError("iterations must be positive")
        if measure_trial < 0:
            raise ValueError("measure_trial must be non-negative")
        if measure_trial >= iterations:
            raise ValueError("measure_trial must be less than iterations")

        self.iterations = iterations
        self.measure_trial = measure_trial
        self.switch_at_trial_zero = switch_at_trial_zero
        self.next_trial = 0
        self.active_trial = None
        self.switch_count = 0
        self.reset_count = 0
        self.dump_count = 0

    def work_begin(self):
        if self.active_trial is not None:
            raise RoiSequenceError(
                f"begin before trial {self.active_trial} end"
            )
        if self.next_trial >= self.iterations:
            raise RoiSequenceError(
                f"unexpected trial {self.next_trial} begin"
            )

        trial = self.next_trial
        self.active_trial = trial
        actions = []
        if self.switch_at_trial_zero and trial == 0:
            if self.switch_count:
                raise RoiSequenceError("second CPU switch requested")
            self.switch_count += 1
            actions.append("switch")
        if trial == self.measure_trial:
            self.reset_count += 1
            actions.extend(("reset", "record_start_tick"))
        return tuple(actions)

    def work_end(self):
        if self.active_trial is None:
            raise RoiSequenceError("end without begin")

        trial = self.active_trial
        actions = []
        if trial == self.measure_trial:
            self.dump_count += 1
            actions.append("dump")
        self.active_trial = None
        self.next_trial += 1
        return tuple(actions)

    def finish(self):
        if self.active_trial is not None:
            raise RoiSequenceError(
                f"missing trial {self.active_trial} end"
            )
        if self.next_trial < self.iterations:
            raise RoiSequenceError(
                f"missing trial {self.next_trial} begin"
            )
        if self.reset_count != 1:
            raise RoiSequenceError(
                f"measured trial reset count is {self.reset_count}, expected 1"
            )
        if self.dump_count != 1:
            raise RoiSequenceError(
                f"measured trial dump count is {self.dump_count}, expected 1"
            )
        if self.switch_at_trial_zero and self.switch_count != 1:
            raise RoiSequenceError(
                f"CPU switch count is {self.switch_count}, expected 1"
            )
        return ("verify",)


class GapbsCheckpointState:
    """Pure event sequencing for checkpoint creation and restoration."""

    def __init__(self, mode, iterations, measure_trial):
        if mode not in ("save", "restore"):
            raise ValueError(f"invalid checkpoint mode: {mode}")
        if (iterations, measure_trial) != (2, 1):
            raise ValueError(
                "checkpoint mode requires iterations=2 and measure_trial=1"
            )

        self.mode = mode
        self.next_trial = 0 if mode == "save" else 2
        self.active_trial = None if mode == "save" else 1
        self.resumed = False
        self.saved = False
        self.ended = False

    def resume_actions(self):
        if self.mode != "restore":
            raise RoiSequenceError("cannot resume a checkpoint save")
        if self.resumed:
            raise RoiSequenceError("duplicate checkpoint resume")
        if self.ended:
            raise RoiSequenceError("cannot resume after measured trial end")
        self.resumed = True
        return ("reset", "record_start_tick")

    def work_begin(self):
        if self.mode == "restore":
            raise RoiSequenceError("unexpected begin after checkpoint restore")
        if self.saved:
            raise RoiSequenceError("unexpected begin after checkpoint save")
        if self.active_trial is not None:
            raise RoiSequenceError(
                f"begin before trial {self.active_trial} end"
            )
        if self.next_trial == 0:
            self.active_trial = 0
            return ()
        if self.next_trial == 1:
            self.active_trial = 1
            return ("checkpoint",)
        raise RoiSequenceError(
            f"unexpected trial {self.next_trial} begin"
        )

    def work_end(self):
        if self.mode == "restore":
            if not self.resumed:
                raise RoiSequenceError("work end before checkpoint resume")
            if self.ended or self.active_trial is None:
                raise RoiSequenceError("duplicate measured trial end")
            self.active_trial = None
            self.ended = True
            return ("dump",)

        if self.active_trial is None:
            raise RoiSequenceError("end without begin")
        if self.active_trial == 1:
            raise RoiSequenceError(
                "measured trial ran instead of being checkpointed"
            )
        self.active_trial = None
        self.next_trial = 1
        return ()

    def checkpoint_saved(self):
        if self.mode != "save":
            raise RoiSequenceError("cannot save after checkpoint restore")
        if self.saved:
            raise RoiSequenceError("duplicate checkpoint save")
        if self.active_trial != 1 or self.next_trial != 1:
            raise RoiSequenceError(
                "checkpoint save is not at measured trial begin"
            )
        self.saved = True
        self.active_trial = None
        self.next_trial = 2
        return ("stop",)

    def finish(self):
        if self.mode == "save":
            if not self.saved:
                raise RoiSequenceError("checkpoint was not saved")
            return ()
        if not self.resumed:
            raise RoiSequenceError("checkpoint was not resumed")
        if not self.ended:
            raise RoiSequenceError("missing measured trial 1 end")
        return ("verify",)
