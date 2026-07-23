# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

import csv
import importlib.util
import math
import os
import subprocess
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest import mock


REPO = Path(__file__).resolve().parents[3]
GENERATOR_PATH = REPO / "scripts" / "generate_gapbs_amu_latency_table.py"
LATENCIES = ("200ns", "500ns", "1us", "2us")
BENCHMARKS = ("bfs", "bc", "pr", "sssp")
FIELDS = (
    "benchmark,label,kind,status,verification,sim_ticks,sim_insts,"
    "speedup_vs_cxl,scale,iterations,measured_trial,fast_forward_cpu,roi_cpu,"
    "cpu_switches,cxl_link_delay,all_memory_cxl,asmc_loads,asmc_completed,"
    "cira_prefetches,cira_indexed_prefetches,cira_csr_prefetches,"
    "cira_completed,cira_useful,cira_late,cira_read_packets,cira_read_bytes,"
    "cxl_packets,cxl_bytes,"
    "l1d_demand_misses,l2d_demand_hits,l2d_demand_misses,l2i_demand_hits,"
    "l2i_demand_misses,cira_total_latency,cira_avg_latency,run_dir"
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
                baseline_ticks = Decimal(3603600) * Decimal(
                    (latency_index + 1) * (benchmark_index + 1)
                )
                for label, kind, speedup in (
                    ("cxl_vanilla", "baseline", Decimal(1)),
                    (
                        "amu",
                        "amu",
                        Decimal(2 + latency_index + benchmark_index),
                    ),
                    (
                        "cira_pgo",
                        "cira",
                        Decimal("1.5")
                        + latency_index
                        + benchmark_index,
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
                            "sim_ticks": str(ticks),
                            "sim_insts": "123",
                            "speedup_vs_cxl": str(speedup),
                            "scale": "20",
                            "iterations": "2",
                            "measured_trial": "1",
                            "fast_forward_cpu": "atomic",
                            "roi_cpu": "timing",
                            "cpu_switches": "1",
                            "cxl_link_delay": latency,
                            "all_memory_cxl": "true",
                            "asmc_loads": "7" if kind == "amu" else "0",
                            "asmc_completed": "7" if kind == "amu" else "0",
                            "cira_prefetches": (
                                "8" if kind == "cira" else "0"
                            ),
                            "cira_indexed_prefetches": (
                                "3" if kind == "cira" else "0"
                            ),
                            "cira_csr_prefetches": (
                                "5" if kind == "cira" else "0"
                            ),
                            "cira_completed": "8" if kind == "cira" else "0",
                            "cira_useful": "4" if kind == "cira" else "0",
                            "cira_late": "0",
                            "cira_read_packets": (
                                "16" if kind == "cira" else "0"
                            ),
                            "cira_read_bytes": (
                                "1024" if kind == "cira" else "0"
                            ),
                            "cxl_packets": "99",
                            "cxl_bytes": "4096",
                            "l1d_demand_misses": "10",
                            "l2d_demand_hits": "11",
                            "l2d_demand_misses": "12",
                            "l2i_demand_hits": "13",
                            "l2i_demand_misses": "14",
                            "cira_total_latency": (
                                "800" if kind == "cira" else "0"
                            ),
                            "cira_avg_latency": (
                                "100" if kind == "cira" else "0"
                            ),
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

    def test_provenance_preserves_diagnostic_columns(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self.make_summaries(root)
            _, provenance = self.generate(root, paths)
            with provenance.open(newline="", encoding="utf-8") as stream:
                rows = list(csv.DictReader(stream))
            bc_cira = next(
                row
                for row in rows
                if row["latency"] == "200ns"
                and row["benchmark"] == "bc"
                and row["label"] == "cira_pgo"
            )
            self.assertEqual(bc_cira["cxl_packets"], "99")
            self.assertEqual(bc_cira["cxl_bytes"], "4096")
            self.assertEqual(bc_cira["l1d_demand_misses"], "10")
            self.assertEqual(bc_cira["l2d_demand_hits"], "11")
            self.assertEqual(bc_cira["l2d_demand_misses"], "12")
            self.assertEqual(bc_cira["l2i_demand_hits"], "13")
            self.assertEqual(bc_cira["l2i_demand_misses"], "14")
            self.assertEqual(bc_cira["cira_total_latency"], "800")
            self.assertEqual(bc_cira["cira_avg_latency"], "100")
            self.assertEqual(bc_cira["scale"], "20")
            self.assertEqual(bc_cira["iterations"], "2")
            self.assertEqual(bc_cira["measured_trial"], "1")
            self.assertEqual(bc_cira["fast_forward_cpu"], "atomic")
            self.assertEqual(bc_cira["roi_cpu"], "timing")
            self.assertEqual(bc_cira["cpu_switches"], "1")
            self.assertEqual(bc_cira["all_memory_cxl"], "true")
            self.assertEqual(bc_cira["cira_useful"], "4")
            self.assertEqual(bc_cira["cira_late"], "0")
            self.assertEqual(bc_cira["cira_read_packets"], "16")
            self.assertEqual(bc_cira["cira_read_bytes"], "1024")
            with paths["200ns"].open(newline="", encoding="utf-8") as stream:
                source = next(
                    row
                    for row in csv.DictReader(stream)
                    if row["benchmark"] == "bc"
                    and row["label"] == "cira_pgo"
                )
            for field in FIELDS:
                self.assertEqual(
                    bc_cira[field], source[field], f"provenance lost {field}"
                )

    def test_missing_new_schema_field_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self.make_summaries(root)
            path = paths["200ns"]
            with path.open(newline="", encoding="utf-8") as stream:
                reader = csv.DictReader(stream)
                rows = list(reader)
                fields = [
                    field
                    for field in reader.fieldnames
                    if field != "cira_useful"
                ]
            with path.open("w", newline="", encoding="utf-8") as stream:
                writer = csv.DictWriter(stream, fieldnames=fields)
                writer.writeheader()
                writer.writerows(
                    {field: row[field] for field in fields} for row in rows
                )
            with self.assertRaisesRegex(
                self.generator.ValidationError,
                "missing columns: cira_useful",
            ):
                self.generate(root, paths)

    def test_duplicate_summary_header_is_rejected_before_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self.make_summaries(root)
            path = paths["200ns"]
            with path.open(newline="", encoding="utf-8") as stream:
                rows = list(csv.reader(stream))
            verification_index = rows[0].index("verification")
            rows[0].append("verification")
            for row in rows[1:]:
                row[verification_index] = "fail"
                row.append("pass")
            with path.open("w", newline="", encoding="utf-8") as stream:
                csv.writer(stream).writerows(rows)
            with self.assertRaisesRegex(
                self.generator.ValidationError,
                "duplicate columns: verification",
            ):
                self.generate(root, paths)

    def test_caption_describes_canonical_g20_methodology(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            latex, _ = self.generate(root)
            text = latex.read_text(encoding="utf-8")
            self.assertIn("scale 20", text)
            self.assertIn("Atomic pre-ROI graph generation", text)
            self.assertIn("Timing trial 0 warmup", text)
            self.assertIn("measured trial 1 ROI", text)
            self.assertIn("bit-exact verification PASS", text)
            self.assertNotIn("scale 4", text)

    def test_rejects_noncanonical_metadata(self):
        for field, value in (
            ("scale", "4"),
            ("iterations", "1"),
            ("measured_trial", "0"),
            ("fast_forward_cpu", "timing"),
            ("roi_cpu", "atomic"),
            ("cpu_switches", "2"),
            ("cxl_link_delay", "2us"),
            ("all_memory_cxl", "false"),
        ):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                paths = self.make_summaries(root)
                self.mutate(
                    paths["200ns"],
                    lambda row: row["benchmark"] == "bfs"
                    and row["kind"] == "baseline",
                    **{field: value},
                )
                with self.assertRaisesRegex(
                    self.generator.ValidationError, field
                ):
                    self.generate(root, paths)

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

    def test_invalid_applicable_diagnostic_value_is_rejected(self):
        for label, field, value in (
            ("cxl_vanilla", "cxl_packets", "not-a-number"),
            ("amu", "cxl_packets", "1.5"),
            ("amu", "l2d_demand_misses", "nan"),
            ("cira_pgo", "cira_avg_latency", "inf"),
            ("cira_pgo", "cira_total_latency", ""),
            ("cira_pgo", "cira_total_latency", "1.5"),
            ("cira_pgo", "cira_useful", ""),
            ("cira_pgo", "cira_read_packets", "1.5"),
            ("cxl_vanilla", "cira_total_latency", "1"),
            ("amu", "cira_useful", "1"),
        ):
            with (
                self.subTest(label=label, field=field),
                tempfile.TemporaryDirectory() as tmp,
            ):
                root = Path(tmp)
                paths = self.make_summaries(root)
                self.mutate(
                    paths["200ns"],
                    lambda row: row["benchmark"] == "bfs"
                    and row["label"] == label,
                    **{field: value},
                )
                with self.assertRaisesRegex(
                    self.generator.ValidationError, field
                ):
                    self.generate(root, paths)

    def test_non_cira_latency_may_be_blank(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self.make_summaries(root)
            self.mutate(
                paths["200ns"],
                lambda row: row["benchmark"] == "bfs"
                and row["label"] == "amu",
                cira_total_latency="",
                cira_avg_latency="",
            )
            self.generate(root, paths)

    def test_cira_average_latency_may_be_fractional(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self.make_summaries(root)
            self.mutate(
                paths["200ns"],
                lambda row: row["benchmark"] == "bfs"
                and row["label"] == "cira_pgo",
                cira_avg_latency="100.25",
            )
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

    def test_outputs_reject_equivalent_paths_before_io(self):
        for alias_kind in (
            "exact",
            "relative",
            "symlink-parent",
            "symlink-file",
            "hardlink",
        ):
            with (
                self.subTest(alias_kind=alias_kind),
                tempfile.TemporaryDirectory() as tmp,
            ):
                root = Path(tmp)
                paths = self.make_summaries(root)
                real = root / "real"
                real.mkdir()
                latex = real / "publication.out"
                latex.write_text("old publication\n", encoding="utf-8")
                if alias_kind == "exact":
                    provenance = latex
                elif alias_kind == "relative":
                    provenance = Path(
                        os.path.relpath(latex, Path.cwd())
                    )
                elif alias_kind == "symlink-parent":
                    alias = root / "alias"
                    alias.symlink_to(real, target_is_directory=True)
                    provenance = alias / latex.name
                elif alias_kind == "symlink-file":
                    provenance = root / "alias.out"
                    provenance.symlink_to(latex)
                else:
                    provenance = root / "hardlink.out"
                    os.link(latex, provenance)
                with (
                    mock.patch.object(
                        self.generator.tempfile,
                        "mkstemp",
                        wraps=self.generator.tempfile.mkstemp,
                    ) as make_temp,
                    mock.patch.object(
                        self.generator.os,
                        "replace",
                        wraps=self.generator.os.replace,
                    ) as replace,
                ):
                    with self.assertRaisesRegex(
                        self.generator.ValidationError,
                        "distinct paths",
                    ):
                        self.generator.generate_outputs(
                            paths, latex, provenance
                        )
                make_temp.assert_not_called()
                replace.assert_not_called()
                self.assertEqual(
                    latex.read_text(encoding="utf-8"),
                    "old publication\n",
                )
                self.assertEqual(list(root.rglob(".*.tmp-*")), [])

    def test_paired_outputs_roll_back_second_replace_failure(self):
        for existing in (False, True):
            with (
                self.subTest(existing=existing),
                tempfile.TemporaryDirectory() as tmp,
            ):
                root = Path(tmp)
                paths = self.make_summaries(root)
                latex = root / "table.tex"
                provenance = root / "provenance.csv"
                if existing:
                    latex.write_text("old latex\n", encoding="utf-8")
                    provenance.write_text(
                        "old provenance\n", encoding="utf-8"
                    )
                original = self.generator.os.replace
                replace_calls = 0

                def fail_second_new_output(source, destination):
                    nonlocal replace_calls
                    destination = Path(destination)
                    if destination in (latex, provenance):
                        replace_calls += 1
                        if replace_calls == 2:
                            raise OSError("forced second output failure")
                    return original(source, destination)

                with mock.patch.object(
                    self.generator.os,
                    "replace",
                    side_effect=fail_second_new_output,
                ):
                    with self.assertRaisesRegex(
                        OSError, "forced second output failure"
                    ):
                        self.generator.generate_outputs(
                            paths, latex, provenance
                        )
                if existing:
                    self.assertEqual(
                        latex.read_text(encoding="utf-8"),
                        "old latex\n",
                    )
                    self.assertEqual(
                        provenance.read_text(encoding="utf-8"),
                        "old provenance\n",
                    )
                else:
                    self.assertFalse(latex.exists())
                    self.assertFalse(provenance.exists())
                self.assertEqual(list(root.rglob(".*.tmp-*")), [])

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
