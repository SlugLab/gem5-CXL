# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

import csv
import importlib.util
import math
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[3]
GENERATOR_PATH = REPO / "scripts" / "generate_gapbs_amu_latency_table.py"
LATENCIES = ("200ns", "500ns", "1us", "2us")
BENCHMARKS = ("bfs", "bc", "pr", "sssp")
FIELDS = (
    "benchmark,label,kind,status,verification,sim_ticks,sim_insts,"
    "speedup_vs_cxl,asmc_loads,cira_prefetches,cira_indexed_prefetches,"
    "cira_csr_prefetches,cira_completed,cxl_packets,run_dir"
).split(",")


def load_generator():
    spec = importlib.util.spec_from_file_location(
        "latency_table", GENERATOR_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class GapbsAmuLatencyTableGeneratorTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.generator = load_generator()

    def make_summaries(self, root):
        paths = {}
        for latency_index, latency in enumerate(LATENCIES):
            summary = root / f"input_{latency}.csv"
            paths[latency] = summary
            rows = []
            for benchmark_index, benchmark in enumerate(BENCHMARKS):
                baseline_ticks = (
                    1000.0 + latency_index * 100 + benchmark_index * 10
                )
                for label, kind, speedup in (
                    ("cxl_vanilla", "baseline", 1.0),
                    ("amu", "amu", 2.0 + latency_index + benchmark_index),
                    (
                        "cira_pgo",
                        "cira",
                        1.5 + latency_index + benchmark_index,
                    ),
                ):
                    ticks = baseline_ticks / speedup
                    rows.append(
                        {
                            "benchmark": benchmark,
                            "label": label,
                            "kind": kind,
                            "status": "ok",
                            "verification": "pass",
                            "sim_ticks": repr(ticks),
                            "sim_insts": "123",
                            "speedup_vs_cxl": repr(speedup),
                            "asmc_loads": "7" if kind == "amu" else "0",
                            "cira_prefetches": (
                                "8" if kind == "cira" else "0"
                            ),
                            "cira_indexed_prefetches": (
                                "1" if kind == "cira" else "0"
                            ),
                            "cira_csr_prefetches": "0",
                            "cira_completed": "8" if kind == "cira" else "0",
                            "cxl_packets": "99",
                            "run_dir": f"runs/{latency}/{benchmark}/{label}",
                        }
                    )
            with summary.open("w", newline="", encoding="utf-8") as stream:
                writer = csv.DictWriter(stream, fieldnames=FIELDS)
                writer.writeheader()
                writer.writerows(reversed(rows))
        return paths

    def generate(self, root, paths=None):
        paths = paths or self.make_summaries(root)
        latex = root / "table.tex"
        provenance = root / "provenance.csv"
        self.generator.generate_outputs(paths, latex, provenance)
        return latex, provenance

    def mutate(self, path, predicate, **updates):
        with path.open(newline="", encoding="utf-8") as stream:
            rows = list(csv.DictReader(stream))
        for row in rows:
            if predicate(row):
                row.update(updates)
                break
        with path.open("w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=FIELDS)
            writer.writeheader()
            writer.writerows(rows)

    def test_canonical_latency_and_row_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            latex, provenance = self.generate(root)
            text = latex.read_text(encoding="utf-8")
            self.assertLess(text.index("200 ns"), text.index("500 ns"))
            self.assertLess(text.index("500 ns"), text.index("1 $\\mu$s"))
            self.assertLess(text.index("1 $\\mu$s"), text.index("2 $\\mu$s"))
            self.assertLess(text.index("BFS"), text.index("BC"))
            self.assertLess(text.index("BC"), text.index("PR"))
            self.assertLess(text.index("PR"), text.index("SSSP"))
            with provenance.open(newline="", encoding="utf-8") as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual(
                [
                    (row["latency"], row["benchmark"], row["label"])
                    for row in rows[:4]
                ],
                [
                    ("200ns", "bfs", "cxl_vanilla"),
                    ("200ns", "bfs", "amu"),
                    ("200ns", "bfs", "cira_pgo"),
                    ("200ns", "bc", "cxl_vanilla"),
                ],
            )

    def test_geomeans_are_separate_for_amu_and_cira(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            latex, _ = self.generate(root)
            text = latex.read_text(encoding="utf-8")
            amu = math.prod((2.0, 3.0, 4.0, 5.0)) ** 0.25
            cira = math.prod((1.5, 2.5, 3.5, 4.5)) ** 0.25
            self.assertIn(
                f"Geo. & {amu:.2f}$\\times$ & {cira:.2f}$\\times$", text
            )

    def test_latex_escape(self):
        self.assertEqual(
            self.generator.latex_escape("a_b&c%#{}"),
            r"a\_b\&c\%\#\{\}",
        )

    def test_failed_or_missing_verification_is_rejected(self):
        for value in ("fail", ""):
            with self.subTest(value=value):
                with tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    paths = self.make_summaries(root)
                    self.mutate(
                        paths["200ns"],
                        lambda row: row["benchmark"] == "bfs"
                        and row["label"] == "amu",
                        verification=value,
                    )
                    with self.assertRaisesRegex(
                        self.generator.ValidationError, "verification"
                    ):
                        self.generate(root, paths)

    def test_wrong_label_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self.make_summaries(root)
            self.mutate(
                paths["200ns"],
                lambda row: row["benchmark"] == "bfs"
                and row["label"] == "cira_pgo",
                label="cira",
            )
            with self.assertRaisesRegex(
                self.generator.ValidationError, "exact row identities"
            ):
                self.generate(root, paths)

    def test_speedup_mismatch_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self.make_summaries(root)
            self.mutate(
                paths["500ns"],
                lambda row: row["benchmark"] == "pr"
                and row["label"] == "amu",
                speedup_vs_cxl="9.0",
            )
            with self.assertRaisesRegex(
                self.generator.ValidationError, "speedup mismatch"
            ):
                self.generate(root, paths)

    def test_output_is_deterministic(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self.make_summaries(root)
            first_latex, first_csv = self.generate(root, paths)
            expected = (first_latex.read_bytes(), first_csv.read_bytes())
            second_latex, second_csv = self.generate(
                root, dict(reversed(paths.items()))
            )
            self.assertEqual(
                expected, (second_latex.read_bytes(), second_csv.read_bytes())
            )

    def test_cli_requires_exactly_four_canonical_inputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self.make_summaries(root)
            command = [sys.executable, str(GENERATOR_PATH)]
            for latency in LATENCIES[:-1]:
                command += ["--input", f"{latency}={paths[latency]}"]
            command += [
                "--latex-output",
                str(root / "x.tex"),
                "--provenance-output",
                str(root / "x.csv"),
            ]
            result = subprocess.run(
                command, text=True, capture_output=True, check=False
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("exactly four", result.stderr)


if __name__ == "__main__":
    unittest.main()
