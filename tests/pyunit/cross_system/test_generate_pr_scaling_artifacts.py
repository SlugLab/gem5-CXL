# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

import copy
import hashlib
import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from scripts import generate_pr_scaling_artifacts as artifacts
from scripts import pr_offload_contract as gate_contract


SCALES = (4, 12, 14, 20)
SYSTEMS = ("vanilla", "amu", "cira", "m2ndp")


def sha(label):
    return hashlib.sha256(label.encode()).hexdigest()


class PrScalingArtifactTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.complete = self.root / "complete.json"
        points = {}
        for scale in SCALES:
            vanilla = Decimal(scale * 12)
            for index, system in enumerate(SYSTEMS):
                divisor = Decimal("1" if system == "vanilla" else "1.5")
                latency = vanilla / divisor
                mechanism = {"verification": "pass"}
                if system == "m2ndp":
                    mechanism.update(
                        ndpsim_measured_cycles=str(scale * 800),
                        ndpsim_core_period_seconds="0.01",
                    )
                else:
                    mechanism["sim_ticks"] = str(
                        int(latency * Decimal(10**12))
                    )
                points[f"g{scale}:{system}"] = {
                    "scale": scale,
                    "system": system,
                    "status": "passed",
                    "latency": "1us",
                    "full_e2e": True,
                    "latency_seconds": str(latency),
                    "speedup": str(divisor),
                    "output_elements": 1 << scale,
                    "mechanism": mechanism,
                    "outputs": {
                        "rank": sha(f"rank-{scale}-{system}"),
                        "summary": sha(f"summary-{scale}-{system}"),
                    },
                }
        self.complete.write_text(
            json.dumps(
                {
                    "schema": 1,
                    "status": "complete",
                    "profile": "pr-scaling-4thread-1us",
                    "graph_set_sha256": sha("graph-set"),
                    "g20_graph_sha256": sha("g20"),
                    "inputs_sha256": sha("inputs"),
                    "calibration_sha256": sha("calibration"),
                    "code_sha256": sha("code"),
                    "gem5_sha256": sha("gem5"),
                    "config_sha256": sha("config"),
                    "performance_gate": {
                        "status": "passed",
                        "checked_points": 9,
                        "policies": {
                            system: gate_contract.performance_policy(system)
                            for system in ("amu", "cira", "m2ndp")
                        },
                        "offenders": [],
                    },
                    "points": points,
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )

    def set_speedup(self, value, key, speedup):
        scale = int(key.split(":", 1)[0][1:])
        baseline = Decimal(
            value["points"][f"g{scale}:vanilla"]["latency_seconds"]
        )
        seconds = baseline / Decimal(speedup)
        point = value["points"][key]
        point["latency_seconds"] = str(seconds)
        point["speedup"] = str(Decimal(speedup))
        if key.endswith(":m2ndp"):
            point["mechanism"]["ndpsim_measured_cycles"] = "1"
            point["mechanism"]["ndpsim_core_period_seconds"] = str(seconds)
        else:
            point["mechanism"]["sim_ticks"] = str(
                int(seconds * Decimal(10**12))
            )

    def test_load_recomputes_speedup_and_native_counts(self):
        data = artifacts.load_data(self.complete)
        rows = {(row.scale, row.system): row for row in data.rows}
        self.assertEqual(rows[(20, "amu")].speedup, Decimal("1.5"))
        self.assertEqual(rows[(20, "amu")].native_time_kind, "gem5_ticks")
        self.assertEqual(rows[(20, "m2ndp")].native_time_kind, "ndpsim_cycles")
        self.assertEqual(rows[(20, "m2ndp")].native_time_count, 16000)

    def test_load_retains_but_does_not_gate_g4(self):
        value = json.loads(self.complete.read_text())
        self.set_speedup(value, "g4:amu", "0.01")
        self.complete.write_text(json.dumps(value))
        rows = {
            (row.scale, row.system): row
            for row in artifacts.load_data(self.complete).rows
        }
        self.assertEqual(rows[(4, "amu")].speedup, Decimal("0.01"))

        self.set_speedup(value, "g12:amu", "0.01")
        self.complete.write_text(json.dumps(value))
        with self.assertRaisesRegex(
            artifacts.ArtifactError, "performance gate did not pass"
        ):
            artifacts.load_data(self.complete)

    def test_load_accepts_m2ndp_above_old_upper_bound(self):
        value = json.loads(self.complete.read_text())
        for scale in (12, 14, 20):
            self.set_speedup(
                value,
                f"g{scale}:m2ndp",
                "2.634272138228941520602758013",
            )
        self.complete.write_text(json.dumps(value))
        rows = {
            (row.scale, row.system): row
            for row in artifacts.load_data(self.complete).rows
        }
        self.assertEqual(
            rows[(20, "m2ndp")].speedup,
            Decimal("2.634272138228941520602758013"),
        )

    def test_load_rejects_bounded_upper_and_m2ndp_minimum_failures(self):
        original = json.loads(self.complete.read_text())
        for key, speedup in (
            ("g12:amu", "1.600001"),
            ("g20:m2ndp", "1.399999"),
        ):
            value = copy.deepcopy(original)
            self.set_speedup(value, key, speedup)
            self.complete.write_text(json.dumps(value))
            with self.subTest(key=key), self.assertRaisesRegex(
                artifacts.ArtifactError, "performance gate did not pass"
            ):
                artifacts.load_data(self.complete)

    def test_rejects_performance_hold_or_unpassed_gate(self):
        value = json.loads(self.complete.read_text())
        value["status"] = "performance_hold"
        value["performance_gate"] = {
            "status": "hold",
            "checked_points": 9,
            "offenders": [{"point": "g12:amu", "speedup": "1.39"}],
        }
        self.complete.write_text(json.dumps(value))
        with self.assertRaisesRegex(artifacts.ArtifactError, "complete|gate"):
            artifacts.load_data(self.complete)

        value["status"] = "complete"
        self.complete.write_text(json.dumps(value))
        with self.assertRaisesRegex(artifacts.ArtifactError, "gate"):
            artifacts.load_data(self.complete)

    def test_rejects_incomplete_or_unverified_matrix(self):
        original = json.loads(self.complete.read_text())
        missing = copy.deepcopy(original)
        missing["points"].pop("g20:m2ndp")
        self.complete.write_text(json.dumps(missing))
        with self.assertRaisesRegex(artifacts.ArtifactError, "16 points"):
            artifacts.load_data(self.complete)

        unverified = copy.deepcopy(original)
        unverified["points"]["g14:cira"]["mechanism"][
            "verification"
        ] = "fail"
        self.complete.write_text(json.dumps(unverified))
        with self.assertRaisesRegex(artifacts.ArtifactError, "verification"):
            artifacts.load_data(self.complete)

    def test_rejects_ratio_time_and_native_counter_drift(self):
        original = json.loads(self.complete.read_text())
        cases = (
            ("speedup", "99", "stored speedup"),
            ("latency_seconds", "0", "positive"),
        )
        for field, value, message in cases:
            changed = copy.deepcopy(original)
            changed["points"]["g12:amu"][field] = value
            self.complete.write_text(json.dumps(changed))
            with self.subTest(field=field):
                with self.assertRaisesRegex(artifacts.ArtifactError, message):
                    artifacts.load_data(self.complete)
        changed = copy.deepcopy(original)
        changed["points"]["g12:amu"]["mechanism"]["sim_ticks"] = "1.5"
        self.complete.write_text(json.dumps(changed))
        with self.assertRaisesRegex(
            artifacts.ArtifactError, "positive integer"
        ):
            artifacts.load_data(self.complete)

    def test_rejects_native_count_to_latency_mismatch(self):
        value = json.loads(self.complete.read_text())
        value["points"]["g4:m2ndp"]["mechanism"][
            "ndpsim_measured_cycles"
        ] = "1"
        self.complete.write_text(json.dumps(value))
        with self.assertRaisesRegex(artifacts.ArtifactError, "native timing"):
            artifacts.load_data(self.complete)

    def test_publish_emits_raw_data_table_and_four_plot_families(self):
        result = artifacts.publish(
            artifacts.load_data(self.complete), self.root / "publication"
        )
        expected = {
            "pr-scaling-raw.json",
            "pr-scaling-raw.csv",
            "pr-scaling-evidence.json",
            "pr-scaling-table.tex",
            "fig/pr-scaling-speedup.pdf",
            "fig/pr-scaling-speedup.svg",
            "fig/pr-scaling-latency.pdf",
            "fig/pr-scaling-latency.svg",
            "fig/pr-scaling-grouped.pdf",
            "fig/pr-scaling-grouped.svg",
            "fig/pr-scaling-heatmap.pdf",
            "fig/pr-scaling-heatmap.svg",
        }
        self.assertEqual(set(result), expected)
        raw = json.loads(
            (self.root / "publication/pr-scaling-raw.json").read_text()
        )
        self.assertEqual(raw["row_count"], 16)
        self.assertEqual(len(raw["rows"]), 16)

    def test_publication_bytes_are_deterministic(self):
        data = artifacts.load_data(self.complete)
        first = artifacts.publish(data, self.root / "first")
        second = artifacts.publish(data, self.root / "second")
        self.assertEqual(
            {name: row["sha256"] for name, row in first.items()},
            {name: row["sha256"] for name, row in second.items()},
        )

    def test_publish_rolls_back_every_file_after_injected_failure(self):
        output = self.root / "publication"
        output.mkdir()
        old = output / "pr-scaling-raw.csv"
        old.write_text("old\n")
        with self.assertRaisesRegex(artifacts.ArtifactError, "injected"):
            artifacts.publish(
                artifacts.load_data(self.complete),
                output,
                fail_after_promotions=3,
            )
        self.assertEqual(old.read_text(), "old\n")
        self.assertFalse((output / "pr-scaling-table.tex").exists())


if __name__ == "__main__":
    unittest.main()
