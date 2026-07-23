# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

"""Pure event sequencing for GAPBS work-begin/work-end annotations."""


class RoiSequenceError(RuntimeError):
    """Raised when GAPBS emits an incomplete or malformed ROI sequence."""


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
