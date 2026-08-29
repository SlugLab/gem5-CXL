# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from scripts import stratified_timing as timing


class StratifiedTimingTest(unittest.TestCase):
    def test_gap_bc_uses_four_vertex_windows_for_degree_skew_budget(self):
        plan = timing.make_plan("d" * 64, "bc_bfs", 645_268)
        self.assertFalse(plan.full_phase)
        self.assertEqual(plan.length, 4)
        self.assertEqual(len(plan.windows), 64)
        self.assertTrue(all(
            row.measure_start - row.warmup_start == 4
            and row.measure_stop - row.measure_start == 4
            for row in plan.windows
        ))

    def test_windows_are_nested_evenly_distributed_and_have_equal_warmup(self):
        plan = timing.make_plan("a" * 64, "pricing", 20_000_000)
        self.assertEqual(plan.length, 65_536)
        self.assertEqual(len(plan.coordinates(8)), 8)
        self.assertLess(set(plan.coordinates(8)), set(plan.coordinates(16)))
        self.assertLess(set(plan.coordinates(16)), set(plan.coordinates(32)))
        self.assertLess(set(plan.coordinates(32)), set(plan.coordinates(64)))
        self.assertEqual(
            tuple(window.stratum for window in plan.coordinates(8)),
            (0, 32, 16, 48, 8, 40, 24, 56),
        )
        self.assertTrue(
            all(
                window.measure_start - window.warmup_start == plan.length
                and window.measure_stop - window.measure_start == plan.length
                for window in plan.coordinates(64)
            )
        )

    def test_same_identity_is_deterministic_and_trace_drift_changes_offsets(self):
        first = timing.make_plan("a" * 64, "pricing", 20_000_000)
        second = timing.make_plan("a" * 64, "pricing", 20_000_000)
        changed = timing.make_plan("b" * 64, "pricing", 20_000_000)
        self.assertEqual(first, second)
        self.assertNotEqual(first.windows, changed.windows)

    def test_short_phase_uses_full_timing(self):
        plan = timing.make_plan("b" * 64, "short", 100_000)
        self.assertTrue(plan.full_phase)
        self.assertEqual(plan.length, 100_000)
        self.assertEqual(plan.coordinates(8)[0].measure_start, 0)
        self.assertEqual(plan.coordinates(8)[0].measure_stop, 100_000)

    def test_plan_round_trip_freezes_coordinates_and_seed(self):
        plan = timing.make_plan("c" * 64, "gather", 20_000_000)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "plan.json"
            timing.write_plan(path, plan)
            loaded = timing.read_plan(path)
            self.assertEqual(loaded, plan)
            value = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(value["trace_sha256"], "c" * 64)
            self.assertEqual(len(value["windows"]), 64)

    def test_reconstruct_adds_fixed_and_full_dynamic_phase_work(self):
        phases = (
            timing.PhaseEstimate(
                full_work_items=100,
                seconds_per_item=(Decimal("0.01"), Decimal("0.03")),
            ),
            timing.PhaseEstimate(
                full_work_items=10,
                seconds_per_item=(Decimal("0.2"),),
            ),
        )
        self.assertEqual(timing.reconstruct(Decimal("1"), phases), Decimal("5"))

    def test_exact_pairs_pass_five_percent_gate(self):
        result = timing.bootstrap_speedup([10] * 8, [5] * 8, seed=7)
        self.assertEqual(result.speedup, Decimal("2"))
        self.assertEqual(result.ci_low, Decimal("2"))
        self.assertEqual(result.ci_high, Decimal("2"))
        self.assertTrue(result.publishable)
        self.assertEqual(timing.final_status(result, level=8), "complete")

    def test_unpaired_windows_are_rejected(self):
        with self.assertRaisesRegex(timing.TimingError, "paired"):
            timing.bootstrap_speedup([10, 11], [5], seed=7)

    def test_wide_ci_expands_then_becomes_inconclusive(self):
        vanilla = [1, 100, 2, 80, 3, 60, 4, 40]
        system = [50, 1, 40, 2, 30, 3, 20, 4]
        result = timing.bootstrap_speedup(vanilla, system, seed=11)
        self.assertFalse(result.publishable)
        self.assertEqual(timing.final_status(result, level=8), "expand")
        self.assertEqual(timing.final_status(result, level=64), "inconclusive")


if __name__ == "__main__":
    unittest.main()
