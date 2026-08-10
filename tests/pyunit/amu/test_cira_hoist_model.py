# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

import unittest

from scripts import cira_hoist_model as model


def candidate(**overrides):
    values = {
        "name": "lead-1k",
        "operands_dominate": True,
        "guards_available": True,
        "alias_safe": True,
        "invalidation_safe": True,
        "lifetime_safe": True,
        "available_slack_ns": 1400,
        "issue_ns": 10,
        "index_walk_ns": 80,
        "queue_wait_ns": 20,
        "cxl_memory_ns": 1000,
        "cache_install_ns": 40,
        "expected_saved_stall_ns": 1300,
        "usefulness_probability": 0.9,
        "descriptor_formation_ns": 20,
        "runtime_guards_ns": 10,
        "selection_cost_ns": 0,
        "extra_traffic_ns": 30,
        "cache_pollution_ns": 10,
        "late_request_ns": 20,
        "lead_rows": 1024,
    }
    values.update(overrides)
    return model.HoistCandidate(**values)


def resources(**overrides):
    values = {
        "descriptor_queue_free": 1,
        "csr_walk_queue_free": 1,
        "outstanding_reads_free": 1,
        "destination_ports_free": 1,
        "mshrs_free": 1,
        "max_lead_rows": 2048,
    }
    values.update(overrides)
    return model.ResourceState(**values)


class CiraHoistDecisionTest(unittest.TestCase):
    def test_unsafe_alias_fails_before_profit(self):
        result = model.evaluate(
            candidate(alias_safe=False, expected_saved_stall_ns=100000),
            resources(),
        )
        self.assertFalse(result.legal)
        self.assertFalse(result.emit_prefetch)
        self.assertEqual(result.reason, "unsafe-alias")

    def test_dominance_guard_invalidation_and_lifetime_are_ordered(self):
        cases = (
            ({"operands_dominate": False, "alias_safe": False}, "non-dominating-operands"),
            ({"guards_available": False, "alias_safe": False}, "guard-unavailable"),
            ({"invalidation_safe": False}, "unsafe-invalidation"),
            ({"lifetime_safe": False}, "expired-lifetime"),
        )
        for changes, reason in cases:
            with self.subTest(reason=reason):
                self.assertEqual(
                    model.evaluate(candidate(**changes), resources()).reason,
                    reason,
                )

    def test_slack_and_net_benefit_are_both_required(self):
        self.assertEqual(
            model.evaluate(
                candidate(available_slack_ns=900), resources()
            ).reason,
            "insufficient-slack",
        )
        self.assertEqual(
            model.evaluate(
                candidate(expected_saved_stall_ns=50), resources()
            ).reason,
            "non-positive-benefit",
        )

    def test_capacity_failure_leaves_demand_synchronous(self):
        for field in (
            "descriptor_queue_free",
            "csr_walk_queue_free",
            "outstanding_reads_free",
            "destination_ports_free",
            "mshrs_free",
        ):
            with self.subTest(field=field):
                result = model.evaluate(candidate(), resources(**{field: 0}))
                self.assertFalse(result.emit_prefetch)
                self.assertEqual(result.reason, "capacity")
        self.assertEqual(
            model.evaluate(candidate(lead_rows=4096), resources()).reason,
            "capacity",
        )

    def test_profitable_decision_reports_every_cost_term(self):
        result = model.evaluate(candidate(), resources())
        self.assertTrue(result.legal)
        self.assertTrue(result.profitable)
        self.assertTrue(result.emit_prefetch)
        self.assertEqual(result.reason, "profitable")
        self.assertEqual(result.required_slack_ns, 1150)
        self.assertEqual(result.effective_saved_stall_ns, 1170)
        self.assertEqual(result.total_overhead_ns, 90)
        self.assertEqual(result.net_benefit_ns, 1080)

    def test_invalid_durations_capacities_and_probabilities_fail_closed(self):
        for invalid_candidate, invalid_resources in (
            (candidate(issue_ns=-1), resources()),
            (candidate(usefulness_probability=1.1), resources()),
            (candidate(), resources(mshrs_free=-1)),
        ):
            with self.subTest(candidate=invalid_candidate, resources=invalid_resources):
                with self.assertRaises(model.PolicyError):
                    model.evaluate(invalid_candidate, invalid_resources)


class CiraSelectorTest(unittest.TestCase):
    def test_static_returns_only_declared_candidate(self):
        declared = candidate(name="static")
        self.assertIs(model.select_static(declared), declared)

    def test_pgo_uses_only_completed_verified_a_b_c_rows(self):
        candidates = {
            name: candidate(name=name) for name in ("A", "B", "C")
        }
        rows = {
            "A": {"verification": "PASS", "return_code": 0, "mean_time_ms": 10.0},
            "B": {"verification": "PASS", "return_code": 0, "mean_time_ms": 9.0},
            "C": {"verification": "PASS", "return_code": 0, "mean_time_ms": 8.0},
        }
        self.assertEqual(model.select_pgo(candidates, rows).name, "C")
        rows["C"]["verification"] = "FAIL"
        with self.assertRaisesRegex(model.PolicyError, "verified"):
            model.select_pgo(candidates, rows)

    def test_few_shot_is_causal_charged_and_frozen(self):
        selector = model.FewShotSelector(
            ("A", "B", "C"),
            samples_per_candidate=2,
            profiling_cost_ns_per_sample=5,
            reconfiguration_cost_ns=7,
        )
        with self.assertRaisesRegex(model.PolicyError, "before freeze"):
            selector.select()
        for name, value in (
            ("A", 10), ("B", 8), ("C", 9),
            ("A", 12), ("B", 6), ("C", 11),
        ):
            selector.observe(name, value)
        self.assertEqual(selector.freeze(), "B")
        self.assertEqual(selector.select(), "B")
        self.assertEqual(selector.charged_profiling_ns, 30)
        self.assertEqual(selector.charged_reconfiguration_ns, 7)
        self.assertEqual(selector.total_charged_ns, 37)
        with self.assertRaisesRegex(model.PolicyError, "after freeze"):
            selector.observe("B", 1)

    def test_few_shot_requires_every_declared_sample(self):
        selector = model.FewShotSelector(("A", "B"), samples_per_candidate=1)
        selector.observe("A", 1)
        with self.assertRaisesRegex(model.PolicyError, "missing samples"):
            selector.freeze()


if __name__ == "__main__":
    unittest.main()
