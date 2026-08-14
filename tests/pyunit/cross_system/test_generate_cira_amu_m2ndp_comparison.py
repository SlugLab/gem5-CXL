# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

import hashlib
import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from scripts import generate_cira_amu_m2ndp_comparison as comparison


SYSTEMS = ("vanilla", "amu", "cira", "m2ndp")
SCALES = (4, 12, 14, 20)
WORKLOADS = (
    "pr_spmv", "mcf", "amg_gather", "lulesh_scatter", "npb_cg", "npb_mg",
)


def sha(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class ComparisonPublisherTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.scaling = self.root / "scaling.json"
        self.breadth = self.root / "breadth.json"
        self._write_inputs()

    def _write_inputs(self):
        points = {}
        for scale in SCALES:
            vanilla = Decimal(scale)
            for index, system in enumerate(SYSTEMS):
                seconds = vanilla if system == "vanilla" else vanilla / Decimal(index + 1)
                points[f"g{scale}:{system}"] = {
                    "scale": scale,
                    "system": system,
                    "status": "passed",
                    "latency": "1us",
                    "full_e2e": True,
                    "latency_seconds": str(seconds),
                    "output_elements": 1 << scale,
                    "mechanism": {"verification": "pass"},
                    "outputs": {"summary": sha(f"{scale}:{system}")},
                }
        self.scaling.write_text(json.dumps({
            "schema": 1,
            "status": "complete",
            "profile": "pr-scaling-4thread-1us",
            "inputs_sha256": sha("inputs"),
            "calibration_sha256": sha("calibration"),
            "points": points,
        }, sort_keys=True) + "\n", encoding="utf-8")

        results = {}
        workloads = {}
        for index, workload in enumerate(WORKLOADS):
            vanilla = Decimal(index + 10)
            absolute = {
                "vanilla": str(vanilla),
                "amu": str(vanilla / Decimal(2)),
                "cira": str(vanilla / Decimal(3)),
                "m2ndp": str(vanilla / Decimal(4)),
            }
            systems = {
                system: {
                    "speedup": str(vanilla / Decimal(absolute[system])),
                    "ci_low": str(vanilla / Decimal(absolute[system]) - Decimal("0.1")),
                    "ci_high": str(vanilla / Decimal(absolute[system]) + Decimal("0.1")),
                    "publishable": True,
                    "relative_half_width": "0.04",
                    "resamples": 10000,
                }
                for system in SYSTEMS[1:]
            }
            results[workload] = {
                "status": "complete", "level": 16,
                "absolute_seconds": absolute, "systems": systems,
                "publishable": True, "relative_half_width": "0.04",
            }
            workloads[workload] = {
                "functional": {
                    system: {"status": "pass", "compared_words": index + 1}
                    for system in (
                        "vanilla", "amu", "cira", "m2ndp-funcsim"
                    )
                },
                "phases": {"phase": {"work_items": 100}},
            }
        self.breadth.write_text(json.dumps({
            "schema": 1,
            "status": "complete",
            "identity": {
                "input_manifest_sha256": sha("inputs"),
                "calibration_manifest_sha256": sha("calibration"),
            },
            "results": results,
            "workload_order": list(WORKLOADS),
            "workloads": workloads,
        }, sort_keys=True) + "\n", encoding="utf-8")

    def test_loads_exact_matrix_and_recomputes_every_ratio(self):
        data = comparison.load_data(self.scaling, self.breadth)
        self.assertEqual(data.scaling_scales, SCALES)
        self.assertEqual(data.breadth_workloads, WORKLOADS)
        rows = {(row.scope, row.item, row.system): row for row in data.rows}
        self.assertEqual(rows[("scaling", "g20", "amu")].speedup, Decimal("2"))
        self.assertEqual(rows[("breadth", "mcf", "cira")].speedup, Decimal("3"))
        self.assertEqual(rows[("breadth", "npb_mg", "m2ndp")].ci_high,
                         Decimal("4.1"))

    def test_rejects_stored_speedup_drift_and_incomplete_numeric_bar(self):
        value = json.loads(self.breadth.read_text())
        value["results"]["mcf"]["systems"]["amu"]["speedup"] = "99"
        self.breadth.write_text(json.dumps(value) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(comparison.ComparisonError, "speedup"):
            comparison.load_data(self.scaling, self.breadth)

        self._write_inputs()
        value = json.loads(self.breadth.read_text())
        value["results"]["mcf"]["status"] = "inconclusive"
        value["results"]["mcf"]["systems"]["amu"]["publishable"] = False
        self.breadth.write_text(json.dumps(value) + "\n", encoding="utf-8")
        data = comparison.load_data(self.scaling, self.breadth)
        row = next(row for row in data.rows
                   if row.scope == "breadth" and row.item == "mcf"
                   and row.system == "amu")
        self.assertIsNone(row.speedup)

    def test_publish_writes_one_page_figure_table_and_hash_bound_evidence(self):
        data = comparison.load_data(self.scaling, self.breadth)
        output = self.root / "publication"
        result = comparison.publish(data, output)
        expected = {
            "cira-amu-m2ndp-comparison.csv",
            "cira-amu-m2ndp-evidence.json",
            "gapbs-vtune-cxl-table.tex",
            "fig/cira-amu-m2ndp-scaling-breadth.pdf",
            "fig/cira-amu-m2ndp-scaling-breadth.svg",
        }
        self.assertEqual(set(result), expected)
        for relative in expected:
            self.assertTrue((output / relative).is_file())
        evidence = json.loads(
            (output / "cira-amu-m2ndp-evidence.json").read_text()
        )
        self.assertEqual(evidence["status"], "pass")
        self.assertEqual(evidence["row_count"], 34)
        self.assertEqual(evidence["inputs"]["scaling"]["sha256"],
                         sha(self.scaling.read_text()))

    def test_publication_bytes_are_deterministic(self):
        data = comparison.load_data(self.scaling, self.breadth)
        first = comparison.publish(data, self.root / "first")
        second = comparison.publish(data, self.root / "second")
        self.assertEqual(
            {name: row["sha256"] for name, row in first.items()},
            {name: row["sha256"] for name, row in second.items()},
        )

    def test_atomic_publish_restores_every_old_file_on_promotion_failure(self):
        data = comparison.load_data(self.scaling, self.breadth)
        output = self.root / "publication"
        output.mkdir()
        old = output / "cira-amu-m2ndp-comparison.csv"
        old.write_text("old\n", encoding="utf-8")
        with self.assertRaisesRegex(comparison.ComparisonError, "injected"):
            comparison.publish(data, output, fail_after_promotions=2)
        self.assertEqual(old.read_text(encoding="utf-8"), "old\n")
        self.assertFalse((output / "gapbs-vtune-cxl-table.tex").exists())


if __name__ == "__main__":
    unittest.main()
