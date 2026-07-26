# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

import tempfile
import unittest
import json
from pathlib import Path
from types import SimpleNamespace

from scripts import run_gapbs_matched_pr_spmv_variants as runner


class MatchedVariantRunnerTest(unittest.TestCase):
    def test_compare_args_fix_publication_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            options = SimpleNamespace(
                gem5=root / "gem5.opt",
                config=root / "config.py",
                graph=root / "g20.sg",
                graph_scale=20,
                checkpoint_root=root / "checkpoints",
                outdir=root / "run",
                timeout=0,
            )

            args = runner.make_compare_args(options)

        self.assertEqual(args.iterations, 2)
        self.assertEqual(args.measure_trial, 1)
        self.assertEqual(args.cpu, "timing")
        self.assertEqual(args.cores, 2)
        self.assertEqual(args.cxl_link_delay, "1us")
        self.assertTrue(args.roi_work_events)
        self.assertTrue(args.verify)
        self.assertEqual(args.timeout, 0)

    def test_amu_row_requires_completed_owned_loads(self):
        row = self.valid_row("amu")
        row["asmc_loads"] = 32
        row["asmc_completed"] = 31

        with self.assertRaisesRegex(
            runner.VariantRunError, "AMU issued/completed"
        ):
            runner.validate_row(row, "amu", smoke_test=False)

    def test_cira_row_requires_a_real_descriptor_and_completion(self):
        row = self.valid_row("cira")
        row.update(
            cira_prefetches=0,
            cira_completed=0,
            cira_indexed_prefetches=0,
            cira_csr_prefetches=0,
        )

        with self.assertRaisesRegex(
            runner.VariantRunError, "no CIRA events"
        ):
            runner.validate_row(row, "cira", smoke_test=False)

    def test_variant_manifest_rejects_baseline_manifest_drift(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            baseline = root / "baseline"
            baseline.mkdir()
            (baseline / "manifest.json").write_text(
                '{"build": "changed"}\n', encoding="utf-8"
            )
            variant_manifest = root / "variants.json"
            variant_manifest.write_text(
                json.dumps(
                    {
                        "benchmark": "pr_spmv",
                        "page_rank_iterations": 20,
                        "fixed_iterations": True,
                        "fp_contract": False,
                        "fast_math": False,
                        "baseline_build": str(baseline),
                        "baseline_manifest_sha256": "0" * 64,
                        "fixed_source_sha256": "1" * 64,
                        "variants": [
                            {"kind": "amu"},
                            {"kind": "cira"},
                        ],
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                runner.VariantRunError, "baseline manifest hash changed"
            ):
                runner.load_manifest(variant_manifest)

    @staticmethod
    def valid_row(kind):
        return {
            "benchmark": "pr_spmv",
            "kind": kind,
            "status": "ok",
            "verification": "pass",
            "scale": 20,
            "iterations": 2,
            "measured_trial": 1,
            "roi_cpu": "timing",
            "cores": 2,
            "cxl_link_delay": "1us",
            "all_memory_cxl": True,
            "graph_sha256": (
                "ce900a7147a073835a7450e8f1afedf9f13db6833652bf2f"
                "9647819be26bedb3"
            ),
            "checkpoint_restores": 1,
            "sim_ticks": 123,
            "asmc_loads": 32 if kind == "amu" else 0,
            "asmc_completed": 32 if kind == "amu" else 0,
            "cira_prefetches": 64 if kind == "cira" else 0,
            "cira_completed": 64 if kind == "cira" else 0,
            "cira_indexed_prefetches": 0,
            "cira_csr_prefetches": 8 if kind == "cira" else 0,
        }


if __name__ == "__main__":
    unittest.main()
