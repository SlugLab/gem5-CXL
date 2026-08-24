# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from scripts import pr_offload_contract as gate_contract
from scripts import qualify_pr_scaling_g12 as qualification


class G12QualificationTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    @staticmethod
    def points(amu="1.4", cira="1.6"):
        baseline = Decimal("16")
        return {
            "g12:vanilla": {
                "status": "passed", "latency_seconds": str(baseline),
                "speedup": "1", "outputs": {"rank": "a" * 64},
                "mechanism": {"verification": "pass"},
            },
            "g12:amu": {
                "status": "passed",
                "latency_seconds": str(baseline / Decimal(amu)),
                "speedup": amu, "outputs": {"rank": "a" * 64},
                "mechanism": {"verification": "pass"},
            },
            "g12:cira": {
                "status": "passed",
                "latency_seconds": str(baseline / Decimal(cira)),
                "speedup": cira, "outputs": {"rank": "a" * 64},
                "mechanism": {"verification": "pass"},
            },
        }

    def test_gate_accepts_inclusive_bounds_and_recomputes_speedups(self):
        result = qualification.evaluate_gate(self.points())
        self.assertEqual(result, {
            "status": "passed",
            "checked_points": 2,
            "speedups": {"amu": "1.4", "cira": "1.6"},
            "policies": {
                system: gate_contract.performance_policy(system)
                for system in ("amu", "cira")
            },
            "offenders": [],
        })

    def test_gate_holds_without_relabeling_correct_points(self):
        points = self.points(amu="1.39", cira="1.5")
        result = qualification.evaluate_gate(points)
        self.assertEqual(result["status"], "hold")
        self.assertEqual(result["offenders"], [{
            "point": "g12:amu", "speedup": "1.39",
            "minimum": "1.4", "maximum": "1.6",
            "correctness": "bit-exact",
        }])
        self.assertTrue(all(row["status"] == "passed" for row in points.values()))

    def test_gate_rejects_incomplete_or_unverified_points(self):
        for key, field, value in (
            ("g12:amu", "status", "failed"),
            ("g12:cira", "mechanism", {"verification": "fail"}),
        ):
            points = self.points()
            points[key][field] = value
            with self.subTest(key=key, field=field):
                with self.assertRaises(qualification.QualificationError):
                    qualification.evaluate_gate(points)

    def test_identity_binds_all_inputs_and_g12_variant_manifest(self):
        paths = {}
        for name in (
            "inputs", "calibration", "gem5", "m5_library", "config",
            "variant_manifest",
        ):
            path = self.root / name
            path.write_text(name + "\n", encoding="utf-8")
            paths[name] = path
        options = SimpleNamespace(
            inputs=paths["inputs"], calibration=paths["calibration"],
            gem5=paths["gem5"], m5_library=paths["m5_library"],
            config=paths["config"],
        )
        inputs = {
            "graphs": [{"scale": 12, "sha256": "b" * 64}],
        }

        identity = qualification.build_identity(
            options, inputs, paths["variant_manifest"]
        )

        self.assertEqual(identity["g12_graph_sha256"], "b" * 64)
        self.assertEqual(identity["variant_manifest"], str(
            paths["variant_manifest"].resolve()
        ))
        for field in (
            "code_sha256", "inputs_sha256", "calibration_sha256",
            "gem5_sha256", "m5_library_sha256", "config_sha256",
            "variant_manifest_sha256",
        ):
            self.assertEqual(len(identity[field]), 64)


if __name__ == "__main__":
    unittest.main()
