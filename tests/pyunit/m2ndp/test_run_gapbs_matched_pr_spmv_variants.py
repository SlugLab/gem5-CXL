# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

import tempfile
import unittest
import json
import os
import struct
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

from scripts import run_gapbs_matched_pr_spmv_variants as runner


class MatchedVariantRunnerTest(unittest.TestCase):
    def test_formal_cira_policy_binding_rejects_mode_or_source_drift(self):
        manifest = {
            "cira_mode": "few-shot-online",
            "cira_policy": {
                "mode": "few-shot-online", "source_row": "B"
            },
        }
        self.assertEqual(
            runner.validate_cira_policy_binding(
                manifest, "few-shot-online", "B"
            )["source_row"],
            "B",
        )
        for mode, source in (("static", "B"), ("few-shot-online", "A")):
            with self.subTest(mode=mode, source=source), self.assertRaises(
                runner.VariantRunError
            ):
                runner.validate_cira_policy_binding(manifest, mode, source)

    def test_standalone_candidate_binding_is_explicit(self):
        manifest = {
            "cira_mode": "candidate",
            "cira_policy": {"mode": "candidate", "source_row": "A"},
        }
        policy = runner.validate_cira_policy_binding(
            manifest, "candidate", "A"
        )
        self.assertEqual(policy["source_row"], "A")
        manifest["cira_policy"]["mode"] = "pgo-selected"
        with self.assertRaises(runner.VariantRunError):
            runner.validate_cira_policy_binding(manifest, "candidate", "A")

    def test_formal_pr_calibration_rejects_schema_hash_and_speedup_target_drift(self):
        manifest = runner.pr_calibration_fixture_for_test()
        validated = runner.validate_pr_calibration(manifest)
        self.assertEqual(validated["near_data_pr"]["cira"]["selected_source_row"], "B")
        mutations = (
            ("schema", 1, "schema 2"),
            ("amu_hash", "0" * 64, "AMU source hash"),
            ("speedup_target", True, "speedup cannot"),
        )
        for name, value, message in mutations:
            with self.subTest(name=name):
                candidate = deepcopy(manifest)
                if name == "schema":
                    candidate["schema"] = value
                elif name == "amu_hash":
                    candidate["sources"]["amu_pdf"]["sha256"] = value
                else:
                    candidate["near_data_pr"]["formal_speedup_is_fit_target"] = value
                with self.assertRaisesRegex(runner.VariantRunError, message):
                    runner.validate_pr_calibration(candidate)

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
        self.assertEqual(args.asmc_pr_read_entries, 1024)
        self.assertEqual(
            args.asmc_max_send_queue, args.asmc_pr_read_entries
        )
        self.assertEqual(args.cira_max_csr_walk_queue, 4096)
        self.assertEqual(args.cira_csr_lines_per_turn, 64)
        self.assertEqual(args.cira_max_completed_lines, 65536)
        self.assertEqual(args.cira_max_csr_index_reads, 1024)
        self.assertTrue(args.roi_work_events)
        self.assertTrue(args.verify)
        self.assertEqual(args.timeout, 0)

    def test_g4_profile_builds_four_core_latency_specific_args(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            options = SimpleNamespace(
                profile="g4-4thread-sweep",
                gem5=root / "gem5.opt",
                config=root / "config.py",
                graph=root / "g4.sg",
                graph_scale=4,
                cxl_link_delay="500ns",
                checkpoint_root=root / "checkpoints",
                outdir=root / "run",
                timeout=0,
            )

            args = runner.make_compare_args(options)

        self.assertEqual(args.scale, 4)
        self.assertEqual(args.cores, 4)
        self.assertEqual(args.cxl_link_delay, "500ns")
        self.assertIn("OMP_NUM_THREADS=4", args.env)

    def test_manifest_backed_g12_profile_builds_four_core_args(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            graph = root / "g12.sg"
            generator = root / "converter"
            nodes = 1 << 12
            edges = 1
            graph.write_bytes(
                struct.pack("<?qq", False, edges, nodes)
                + struct.pack(f"<{nodes + 1}q", *([0] * nodes + [edges]))
                + struct.pack("<i", 0)
            )
            generator.write_bytes(b"generator")
            os.chmod(generator, 0o755)
            manifest = root / "g12.manifest.json"
            from scripts import m2ndp_artifacts as artifacts
            manifest.write_text(
                json.dumps(
                    {
                        "schema": 1,
                        "scale": 12,
                        "graph": str(graph.resolve()),
                        "graph_sha256": artifacts.sha256_file(graph),
                        "generator": str(generator.resolve()),
                        "generator_sha256": artifacts.sha256_file(generator),
                        "generator_command": [
                            str(generator.resolve()), "-g", "12", "-b",
                            str(graph.resolve()),
                        ],
                        "num_nodes": nodes,
                        "directed_edges": edges,
                    }
                ) + "\n",
                encoding="utf-8",
            )
            options = SimpleNamespace(
                profile="g12-4thread-qualification",
                graph_manifest=manifest,
                gem5=root / "gem5.opt",
                config=root / "config.py",
                graph=graph,
                graph_scale=12,
                cxl_link_delay="1us",
                checkpoint_root=root / "checkpoints",
                outdir=root / "run",
                timeout=0,
            )

            args = runner.make_compare_args(options)

        self.assertEqual(args.scale, 12)
        self.assertEqual(args.cores, 4)
        self.assertEqual(args.iterations, 2)
        self.assertIn("OMP_NUM_THREADS=4", args.env)

    def test_scaling_profile_uses_manifest_scale_and_full_cxl_warmup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            graph = root / "g12.sg"
            generator = root / "converter"
            nodes = 1 << 12
            graph.write_bytes(
                struct.pack("<?qq", False, 1, nodes)
                + struct.pack(f"<{nodes + 1}q", *([0] * nodes + [1]))
                + struct.pack("<i", 0)
            )
            generator.write_bytes(b"generator")
            os.chmod(generator, 0o755)
            from scripts import m2ndp_artifacts as artifacts
            manifest = root / "g12.manifest.json"
            manifest.write_text(json.dumps({
                "schema": 1, "scale": 12,
                "graph": str(graph.resolve()),
                "graph_sha256": artifacts.sha256_file(graph),
                "generator": str(generator.resolve()),
                "generator_sha256": artifacts.sha256_file(generator),
                "generator_command": [str(generator.resolve()), "-g", "12", "-b", str(graph.resolve())],
                "num_nodes": nodes, "directed_edges": 1,
            }) + "\n", encoding="utf-8")
            options = SimpleNamespace(
                profile="pr-scaling-4thread-1us", graph_manifest=manifest,
                gem5=root / "gem5.opt", config=root / "config.py",
                graph=graph, graph_scale=12, cxl_link_delay="1us",
                checkpoint_root=root / "checkpoints", outdir=root / "run",
                timeout=0,
            )
            profile = runner.resolve_profile(options)
            args = runner.make_compare_args(options)

        self.assertEqual(profile.graph_scale, 12)
        self.assertEqual((profile.cores, profile.threads), (4, 4))
        self.assertEqual(args.iterations, 2)
        self.assertEqual(args.measure_trial, 1)
        self.assertEqual(args.checkpoint_boundary, "trial0_entry")
        self.assertEqual(args.warmup_execution, "full_cxl_trial0")

    def test_g4_row_rejects_two_core_result(self):
        row = self.valid_row("cira")
        row.update(
            scale=4,
            cores=2,
            cxl_link_delay="500ns",
            graph_sha256=(
                "f234532690f6cfc30e993c4d9a1839e65002a618e7da20ea"
                "6a4242818b9c6c3d"
            ),
        )

        with self.assertRaisesRegex(runner.VariantRunError, "cores"):
            runner.validate_row(
                row,
                "cira",
                profile_name="g4-4thread-sweep",
                latency="500ns",
                smoke_test=False,
            )

    def test_config_delay_uses_selected_latency_ticks(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.ini"
            config.write_text("delay=500000\n", encoding="utf-8")

            ticks = runner.validate_config_delay(config, "500ns")

        self.assertEqual(ticks, 500_000)

    def test_cli_accepts_formal_g4_profile_and_latency(self):
        options = runner.parse_args(
            [
                "--gem5",
                "gem5.opt",
                "--graph",
                "g4.sg",
                "--graph-scale",
                "4",
                "--variants-build",
                "variants",
                "--kind",
                "amu",
                "--checkpoint-root",
                "checkpoints",
                "--outdir",
                "run",
                "--profile",
                "g4-4thread-sweep",
                "--cxl-link-delay",
                "2us",
            ]
        )

        self.assertEqual(options.profile, "g4-4thread-sweep")
        self.assertEqual(options.cxl_link_delay, "2us")

    def test_amu_row_requires_completed_owned_loads(self):
        row = self.valid_row("amu")
        row["asmc_loads"] = 32
        row["asmc_completed"] = 31

        with self.assertRaisesRegex(
            runner.VariantRunError, "AMU issued/completed"
        ):
            runner.validate_row(row, "amu", smoke_test=False)

    def test_amu_row_rejects_any_queue_or_translation_error(self):
        row = self.valid_row("amu")
        row["asmc_pending_errors"] = 1

        with self.assertRaisesRegex(runner.VariantRunError, "AMU error"):
            runner.validate_row(row, "amu", smoke_test=False)

    def test_amu_row_requires_line_compression_evidence(self):
        cases = (
            ("amu_line_requests", 31, "line requests differ"),
            ("amu_logical_values", 32, "fewer line requests"),
            ("amu_line_cache_hits", 0, "cache hits"),
            ("amu_coalesced_misses", 0, "coalesced misses"),
        )
        for field, value, message in cases:
            with self.subTest(field=field):
                row = self.valid_row("amu")
                row[field] = value
                with self.assertRaisesRegex(runner.VariantRunError, message):
                    runner.validate_row(row, "amu", smoke_test=False)

        g4 = self.valid_row("amu")
        g4.update(scale=4, amu_line_cache_hits=0, amu_coalesced_misses=0)
        self.assertIs(
            runner.validate_row(
                g4, "amu", smoke_test=True
            ),
            g4,
        )

    def test_amu_row_descriptors_replace_scalar_line_cache_evidence(self):
        row = self.valid_row("amu")
        row.update(
            asmc_loads=0,
            asmc_completed=0,
            amu_logical_values="",
            amu_line_requests="",
            amu_line_cache_hits="",
            amu_coalesced_misses="",
            pr_issued_descriptors=160,
            pr_completed_descriptors=160,
            pr_rows=163840,
            pr_read_packets=4126880,
            pr_write_packets=163840,
            pr_outstanding_work=0,
            pr_rejected_descriptors=0,
        )
        self.assertIs(
            runner.validate_row(row, "amu", smoke_test=False), row
        )

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

    def test_cira_row_descriptors_replace_legacy_prefetch_evidence(self):
        row = self.valid_row("cira")
        row.update(
            cira_prefetches=0,
            cira_completed=0,
            cira_indexed_prefetches=0,
            cira_csr_prefetches=0,
            pr_issued_descriptors=160,
            pr_completed_descriptors=160,
            pr_rows=163840,
            pr_read_packets=2063440,
            pr_coherent_read_packets=2063440,
            pr_write_packets=163840,
            pr_outstanding_work=0,
            pr_rejected_descriptors=0,
            pr_issued_reconfigurations=20,
            pr_completed_reconfigurations=20,
            pr_policy_formation_ticks=160000,
            cira_issued_per_core="80;80",
            cira_completed_per_core="80;80",
        )
        self.assertIs(
            runner.validate_row(row, "cira", smoke_test=False), row
        )

    def test_cira_pr_descriptor_path_does_not_require_legacy_events(self):
        source = Path(runner.comparison.__file__).read_text(encoding="utf-8")
        legacy_gate = source[source.index("if (\n        kind == \"cira\""):]
        legacy_gate = legacy_gate[
            :legacy_gate.index('if kind == "cira" and status')
        ]
        self.assertIn(
            'pr_evidence.get("pr_issued_descriptors", 0) == 0', legacy_gate
        )

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
            "asmc_queue_full_errors": 0,
            "asmc_spm_full_errors": 0,
            "asmc_translation_errors": 0,
            "asmc_pending_errors": 0,
            "asmc_spm_flag_errors": 0,
            "amu_logical_values": 96 if kind == "amu" else 0,
            "amu_line_requests": 32 if kind == "amu" else 0,
            "amu_line_cache_hits": 8 if kind == "amu" else 0,
            "amu_coalesced_misses": 56 if kind == "amu" else 0,
            "cira_prefetches": 64 if kind == "cira" else 0,
            "cira_completed": 64 if kind == "cira" else 0,
            "cira_indexed_prefetches": 0,
            "cira_csr_prefetches": 8 if kind == "cira" else 0,
        }


if __name__ == "__main__":
    unittest.main()
