# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

import struct
import json
import tempfile
import unittest
from pathlib import Path

from scripts import build_gapbs_matched_pr_spmv_variants as variants


REPO = Path(__file__).resolve().parents[3]
FIXED_SOURCE = REPO / "util/m2ndp/gapbs_pr_spmv_fixed.cc"


class MatchedVariantSourceTest(unittest.TestCase):
    def test_recorded_root_is_embedded_before_compilation(self):
        source = (
            REPO / "scripts/build_gapbs_matched_pr_spmv_variants.py"
        ).read_text(encoding="utf-8")
        self.assertIn("embedded_reference_raw=", source)
        self.assertIn("recorded_reference_dir", source)
        self.assertIn(
            "write_experiment_header(\n"
            "        generated_dir / \"m2ndp_experiment_config.h\",\n"
            "        embedded_reference_raw",
            source,
        )

    def test_rebase_manifest_paths_records_final_root(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            staging = root / ".g4.staging"
            final = root / "g4"
            manifest = {"variants": [{
                "binary": str((staging / "amu/bin/pr_spmv").resolve()),
                "reference_raw": str(
                    (staging / "reference/amu.u32").resolve()
                ),
                "generated_source": str(
                    (staging / "amu/generated/pr_spmv.cc").resolve()
                ),
                "command": [
                    "g++", str(staging / "amu/generated/pr_spmv.cc")
                ],
            }]}

            rebased = variants.rebase_output_paths(
                manifest, staging, final
            )

        row = rebased["variants"][0]
        self.assertEqual(
            row["binary"], str((final / "amu/bin/pr_spmv").resolve())
        )
        self.assertEqual(
            row["reference_raw"],
            str((final / "reference/amu.u32").resolve()),
        )
        self.assertIn(str(staging), " ".join(row["command"]))

    def test_calibrated_build_policy_binds_mode_source_and_hoist(self):
        calibration = {
            "schema": 1,
            "sources": {
                "amu_pdf": {"sha256": variants.calibration.AMU_PDF_SHA256},
                "cira_csv": {
                    "sha256": variants.calibration.CIRA_CSV_SHA256,
                    "rows": {
                        "pr_spmv": {
                            "A": {"verification": "PASS", "return_code": 0, "mean_time_ms": 11},
                            "B": {"verification": "PASS", "return_code": 0, "mean_time_ms": 10},
                            "C": {"verification": "PASS", "return_code": 0, "mean_time_ms": 12},
                        }
                    },
                },
            },
            "amu": {"validation": {"status": "PASS"}},
            "cira": {"primary": {"selected_source_mode": "B"}},
        }
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "calibration.json"
            path.write_text(json.dumps(calibration), encoding="utf-8")
            policy = variants.resolve_cira_build_policy(
                path, "pgo-selected", source_row=None
            )
        self.assertEqual(policy["mode"], "pgo-selected")
        self.assertEqual(policy["source_row"], "B")
        self.assertEqual(policy["lead_blocks"], 32)
        self.assertTrue(policy["hoist_decision"]["emit_prefetch"])
        self.assertEqual(len(policy["calibration_manifest_sha256"]), 64)

    def test_fixed_pull_rows_are_partitioned_across_both_cores(self):
        source = FIXED_SOURCE.read_text(encoding="utf-8")
        self.assertIn(
            "#pragma omp parallel for schedule(static)\n"
            "    for (NodeID u = 0; u < g.num_nodes(); ++u)",
            source,
        )
        self.assertNotIn("schedule(dynamic, 16384)", source)

    def test_amu_line_pipeline_commits_float_adds_in_csr_order(self):
        generated = variants.transform_source(
            FIXED_SOURCE.read_text(encoding="utf-8"), "amu"
        )

        self.assertIn("gapbs_amu::thread_store().begin_trial()", generated)
        self.assertIn("gapbs_amu::thread_store().reset_iteration()", generated)
        self.assertIn("gapbs_amu::LineBatch<", generated)
        self.assertIn("current_batch.issue_all()", generated)
        self.assertIn("current_batch.wait_all()", generated)
        self.assertEqual(
            generated.count(
                "incoming_total = incoming_total + "
                "current_batch.value<ScoreT>(score_slots[i])"
            ),
            1,
        )
        self.assertIn("constexpr int kPageRankIterations = 20;", generated)
        self.assertNotIn("gapbs_amu::AsyncWindow", generated)
        self.assertNotIn("gapbs_amu::load_values(", generated)
        self.assertNotIn("gapbs_amu::load_value(", generated)
        self.assertNotIn("incoming_total +=", generated)
        self.assertEqual(generated.count("AMU_INVALID_NODE node=%lld"), 2)
        self.assertEqual(generated.count("fflush(stderr)"), 2)

        work_end = generated.index("m5_work_end(trial, 0)")
        report = generated.index("gapbs_amu::report_trial(trial)")
        self.assertLess(work_end, report)

    def test_amu_has_no_variant_only_trial_zero_priming(self):
        generated = variants.transform_source(
            FIXED_SOURCE.read_text(encoding="utf-8"), "amu"
        )

        self.assertNotIn("prime_graph_pages", generated)
        self.assertNotIn("prime_worker_stack_pages", generated)
        self.assertNotIn("prime_graph_pages", variants.amu_builder.AMU_HEADER)
        self.assertNotIn(
            "prime_worker_stack_pages", variants.amu_builder.AMU_HEADER
        )

        cira = variants.transform_source(
            FIXED_SOURCE.read_text(encoding="utf-8"), "cira"
        )
        self.assertNotIn("prime_worker_stack_pages", cira)

    def test_cira_prefetches_future_row_batches_before_ordered_adds(self):
        generated = variants.transform_source(
            FIXED_SOURCE.read_text(encoding="utf-8"), "cira"
        )

        future = generated.index(
            "GAPBS_CIRA_FUTURE_BLOCK(g, u, pf_begin, pf_count)"
        )
        prefetch = generated.index(
            "GAPBS_CIRA_PREFETCH_IN_CSR_INDEXED_ROWS("
            "g, pf_begin, pf_count, outgoing_contrib)"
        )
        current = generated.index("auto neigh = g.in_neigh(u)")
        ordered_add = generated.index(
            "incoming_total = incoming_total + outgoing_contrib[v]"
        )
        self.assertLess(future, prefetch)
        self.assertLess(prefetch, current)
        self.assertLess(current, ordered_add)
        self.assertNotIn("u % GAPBS_CIRA_ROW_BATCH", generated)
        self.assertIn(
            "(current64 - thread_begin) % GAPBS_CIRA_ROW_BATCH",
            variants.cira_builder.CIRA_HEADER,
        )
        self.assertEqual(
            generated.count(
                "incoming_total = incoming_total + outgoing_contrib[v]"
            ),
            1,
        )
        self.assertNotIn("GAPBS_CIRA_PREFETCH_IN_CSR_INDEXED_ROW(", generated)
        header = variants.cira_builder.CIRA_HEADER
        self.assertIn("GAPBS_CIRA_LEAD_BLOCKS", header)
        self.assertIn("GAPBS_CIRA_ROW_BLOCK_SIZE 64", header)
        self.assertIn(
            "current_block_begin + lead_blocks * row_block_size", header
        )

    def test_compile_commands_disable_contraction_and_fast_math(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            command = variants.compile_command(
                kind="amu",
                cxx="g++",
                source=root / "pr_spmv.cc",
                gapbs_root=root / "gapbs",
                generated_dir=root / "generated",
                output=root / "bin/pr_spmv",
                m5_library=root / "libm5.a",
                amu_batch_size=64,
                cira_prefetch_distance=16,
                cira_row_batch=256,
                cira_max_outstanding=256,
            )

        self.assertIn("-ffp-contract=off", command)
        self.assertIn("-fno-fast-math", command)
        self.assertIn("-DGAPBS_AMU_BATCH_SIZE=64", command)
        self.assertNotIn("-ffast-math", command)

    def test_cira_compile_command_sets_scale_derived_row_policy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            g4_command = variants.compile_command(
                kind="cira",
                cxx="g++",
                source=root / "pr_spmv.cc",
                gapbs_root=root / "gapbs",
                generated_dir=root / "generated",
                output=root / "bin/pr_spmv",
                m5_library=root / "libm5.a",
                amu_batch_size=64,
                cira_prefetch_distance=2,
                cira_row_batch=256,
                cira_max_outstanding=256,
                cira_lead_rows=1,
                cira_batch_rows=1,
            )
            g12_command = variants.compile_command(
                kind="cira",
                cxx="g++",
                source=root / "pr_spmv.cc",
                gapbs_root=root / "gapbs",
                generated_dir=root / "generated",
                output=root / "bin/pr_spmv",
                m5_library=root / "libm5.a",
                amu_batch_size=64,
                cira_prefetch_distance=8,
                cira_row_batch=64,
                cira_max_outstanding=256,
                cira_lead_rows=512,
                cira_batch_rows=64,
            )

        self.assertIn("-DGAPBS_CIRA_LEAD_ROWS=1", g4_command)
        self.assertIn("-DGAPBS_CIRA_BATCH_ROWS=1", g4_command)
        self.assertIn("-DGAPBS_CIRA_LEAD_ROWS=512", g12_command)
        self.assertIn("-DGAPBS_CIRA_BATCH_ROWS=64", g12_command)
        for command in (g4_command, g12_command):
            self.assertNotIn("-DGAPBS_CIRA_NODE_DISTANCE=2", command)
            self.assertFalse(any(
                flag.startswith("-DGAPBS_CIRA_LEAD_BLOCKS=")
                for flag in command
            ))


class MatchedVariantBitGateTest(unittest.TestCase):
    @staticmethod
    def write_words(path, words):
        path.write_bytes(
            b"".join(struct.pack("<I", word) for word in words)
        )

    def test_bit_gate_accepts_both_exact_variant_outputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            reference = root / "baseline.u32"
            amu = root / "amu.u32"
            cira = root / "cira.u32"
            words = [0x3E800000, 0x80000000, 0x7FC00001]
            for path in (reference, amu, cira):
                self.write_words(path, words)

            evidence = variants.validate_raw_outputs(
                reference,
                {"amu": amu, "cira": cira},
                expected_words=3,
            )

        self.assertEqual(evidence["compared_words"], 3)
        self.assertEqual(evidence["mismatches"], {"amu": 0, "cira": 0})
        self.assertEqual(
            evidence["sha256"]["baseline"],
            evidence["sha256"]["amu"],
        )
        self.assertEqual(
            evidence["sha256"]["baseline"],
            evidence["sha256"]["cira"],
        )

    def test_bit_gate_rejects_a_single_changed_bit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            reference = root / "baseline.u32"
            amu = root / "amu.u32"
            self.write_words(reference, [0x3E800000, 0x3F000000])
            self.write_words(amu, [0x3E800001, 0x3F000000])

            with self.assertRaisesRegex(
                variants.VariantEvidenceError,
                r"amu word 0: expected 0x3e800000, actual 0x3e800001",
            ):
                variants.validate_raw_outputs(
                    reference, {"amu": amu}, expected_words=2
                )


if __name__ == "__main__":
    unittest.main()
