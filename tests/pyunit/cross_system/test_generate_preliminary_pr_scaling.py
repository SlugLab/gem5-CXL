# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

import csv
import hashlib
import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from scripts import generate_preliminary_pr_scaling as scaling


class PreliminaryPageRankScalingTest(unittest.TestCase):
    def _inputs(self, root):
        g4 = root / "g4.csv"
        with g4.open("w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=(
                "benchmark", "latency", "system", "latency_seconds",
                "verification", "bit_exact", "source_path", "source_sha256",
            ))
            writer.writeheader()
            for system, seconds in (
                ("vanilla", "4"), ("amu", "2"),
                ("cira", "1"), ("m2ndp", "0.5"),
            ):
                writer.writerow({
                    "benchmark": "pr_spmv", "latency": "1us",
                    "system": system, "latency_seconds": seconds,
                    "verification": "pass", "bit_exact": "pass",
                    "source_path": f"g4/{system}.csv",
                    "source_sha256": hashlib.sha256(system.encode()).hexdigest(),
                })
        points = {}
        for scale in (12, 14):
            for system, seconds in (
                ("vanilla", str(scale)), ("amu", str(scale / 2)),
                ("cira-few-shot", str(scale / 4)),
                ("m2ndp", str(scale / 8)),
            ):
                points[f"g{scale}:{system}"] = {
                    "status": "passed",
                    "evidence": {
                        "seconds": seconds,
                        "verification": "pass",
                        "raw_sha256": hashlib.sha256(
                            f"{scale}:{system}".encode()
                        ).hexdigest(),
                    },
                    "artifacts": {
                        f"/evidence/g{scale}/{system}.csv": hashlib.sha256(
                            f"artifact:{scale}:{system}".encode()
                        ).hexdigest(),
                    },
                }
        points["g20:vanilla"] = {"status": "running", "evidence": {}}
        state = root / "campaign-state.json"
        state.write_text(json.dumps({
            "status": "in_progress", "identity": {"source_sha256": "a" * 64},
            "points": points,
        }), encoding="utf-8")
        return g4, state

    def test_collect_recomputes_twelve_measured_rows_and_omits_pending_g20(self):
        with tempfile.TemporaryDirectory() as temporary:
            g4, state = self._inputs(Path(temporary))
            rows = scaling.collect_rows(g4, state)
        self.assertEqual(len(rows), 12)
        self.assertEqual({row["scale"] for row in rows}, {4, 12, 14})
        self.assertNotIn(20, {row["scale"] for row in rows})
        g12_cira = next(
            row for row in rows
            if row["scale"] == 12 and row["system"] == "cira"
        )
        self.assertEqual(Decimal(g12_cira["speedup"]), Decimal("4"))
        self.assertEqual(g12_cira["verification"], "pass")

    def test_publish_writes_reproducible_data_and_static_exports(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            g4, state = self._inputs(root)
            output = root / "publication"
            manifest = scaling.publish(g4, state, output)
            self.assertEqual(manifest["status"], "preliminary")
            self.assertEqual(manifest["measured_scales"], [4, 12, 14])
            self.assertEqual(manifest["pending_scales"], [20])
            self.assertTrue(all(
                item["path"] == name
                for name, item in manifest["outputs"].items()
            ))
            for name in (
                "pagerank-scaling-preliminary.csv",
                "pagerank-scaling-preliminary.json",
                "pagerank-scaling-preliminary.pdf",
                "pagerank-scaling-preliminary.svg",
                "pagerank-scaling-preliminary.png",
                "publication-manifest.json",
            ):
                self.assertGreater((output / name).stat().st_size, 0)
            self.assertNotIn(
                b"\r\n", (output / "pagerank-scaling-preliminary.csv").read_bytes()
            )
            svg_lines = (output / "pagerank-scaling-preliminary.svg").read_text(
                encoding="utf-8"
            ).splitlines()
            self.assertTrue(all(line == line.rstrip() for line in svg_lines))


if __name__ == "__main__":
    unittest.main()
