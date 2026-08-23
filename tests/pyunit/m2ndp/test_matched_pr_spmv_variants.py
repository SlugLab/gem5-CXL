# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

import json
import struct
import tempfile
import unittest
from pathlib import Path

from scripts import build_gapbs_matched_pr_spmv_variants as variants


REPO = Path(__file__).resolve().parents[3]
OFFLOAD_SOURCE = REPO / "util/pr_offload/gapbs_pr_spmv_offload.cc"


class MatchedVariantSourceTest(unittest.TestCase):
    def test_recorded_root_is_embedded_before_compilation(self):
        source = (REPO / "scripts/build_gapbs_matched_pr_spmv_variants.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("embedded_reference_raw=", source)
        self.assertIn("recorded_reference_dir", source)
        self.assertNotIn("def transform_source(", source)
        self.assertNotIn("_AMU_PULL_LOOP", source)
        self.assertNotIn("_CIRA_PULL_LOOP", source)
        self.assertEqual(
            variants.amu_builder.PR_ROW_OFFLOAD_SOURCE,
            variants.cira_builder.PR_ROW_OFFLOAD_SOURCE,
        )

    def test_rebase_manifest_paths_records_final_root(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            staging = root / ".g4.staging"
            final = root / "g4"
            manifest = {"variants": [{
                "binary": str((staging / "amu/bin/pr_spmv").resolve()),
                "reference_raw": str((staging / "reference/amu.u32").resolve()),
                "generated_source": str(
                    (staging / "amu/generated/pr_spmv_offload.cc").resolve()
                ),
                "command": ["g++", str(staging / "amu/generated/pr_spmv_offload.cc")],
            }]}
            rebased = variants.rebase_output_paths(manifest, staging, final)

        row = rebased["variants"][0]
        self.assertEqual(row["binary"], str((final / "amu/bin/pr_spmv").resolve()))
        self.assertEqual(
            row["reference_raw"], str((final / "reference/amu.u32").resolve())
        )
        self.assertIn(str(staging), " ".join(row["command"]))

    def test_calibrated_build_policy_binds_mode_source_and_hoist(self):
        calibration = {
            "schema": 2,
            "sources": {
                "amu_pdf": {"sha256": variants.calibration.AMU_PDF_SHA256},
                "cira_csv": {
                    "sha256": variants.calibration.CIRA_CSV_SHA256,
                    "rows": {"pr_spmv": {
                        "A": {"verification": "PASS", "return_code": 0,
                              "mean_time_ms": 11},
                        "B": {"verification": "PASS", "return_code": 0,
                              "mean_time_ms": 10},
                        "C": {"verification": "PASS", "return_code": 0,
                              "mean_time_ms": 12},
                    }},
                },
            },
            "amu": {"validation": {"status": "PASS"}},
            "cira": {"primary": {"selected_source_mode": "B"}},
            "near_data_pr": {"formal_speedup_is_fit_target": False},
        }
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "calibration.json"
            path.write_text(json.dumps(calibration), encoding="utf-8")
            policy = variants.resolve_cira_build_policy(
                path, "pgo-selected", source_row=None
            )
        self.assertEqual(policy["source_row"], "B")
        self.assertEqual(policy["lead_blocks"], 32)
        self.assertTrue(policy["hoist_decision"]["emit_prefetch"])

    def test_standalone_candidate_build_policy_is_not_pgo(self):
        calibration = {
            "schema": 2,
            "sources": {
                "amu_pdf": {"sha256": variants.calibration.AMU_PDF_SHA256},
                "cira_csv": {
                    "sha256": variants.calibration.CIRA_CSV_SHA256,
                    "rows": {"pr_spmv": {
                        name: {
                            "verification": "PASS", "return_code": 0,
                            "mean_time_ms": 10,
                        }
                        for name in ("A", "B", "C")
                    }},
                },
            },
            "amu": {"validation": {"status": "PASS"}},
            "cira": {"primary": {"selected_source_mode": "B"}},
            "near_data_pr": {"formal_speedup_is_fit_target": False},
        }
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "calibration.json"
            path.write_text(json.dumps(calibration), encoding="utf-8")
            selected = variants.resolve_cira_build_policy(
                path, "candidate", source_row="C"
            )
        self.assertEqual(selected["mode"], "candidate")
        self.assertEqual(selected["source_row"], "C")
        definitions = variants.policy_compile_definitions("candidate", "C")
        self.assertIn("-DPR_CIRA_POLICY_PGO=1", definitions)
        self.assertIn("-DPR_CIRA_SOURCE_ROW=2", definitions)

    def test_common_source_has_ordered_two_phase_descriptor_execution(self):
        source = OFFLOAD_SOURCE.read_text(encoding="utf-8")
        contribution = source.index("PR_ROW_CONTRIB")
        pull = source.index("PR_ROW_PULL", contribution)
        swap = source.index("scores.swap(nextScores)", pull)
        self.assertLess(contribution, pull)
        self.assertLess(pull, swap)
        self.assertNotIn("incoming_total +=", source)
        self.assertNotIn("load_value(", source)

    def test_compile_commands_select_exactly_one_offload_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            common = {
                "cxx": "g++", "source": root / "pr.cc",
                "gapbs_root": root / "gapbs",
                "generated_dir": root / "generated",
                "output": root / "bin/pr", "m5_library": root / "libm5.a",
            }
            amu = variants.compile_command(kind="amu", **common)
            cira = variants.compile_command(
                kind="cira", cira_mode="pgo-selected",
                cira_source_row="B", **common
            )
        for command in (amu, cira):
            self.assertIn("-ffp-contract=off", command)
            self.assertIn("-fno-fast-math", command)
            self.assertNotIn("-ffast-math", command)
            self.assertIn(str(REPO / "util/pr_offload"), command)
        self.assertIn("-DPR_OFFLOAD_AMU=1", amu)
        self.assertNotIn("-DPR_OFFLOAD_CIRA=1", amu)
        self.assertIn("-DPR_OFFLOAD_CIRA=1", cira)
        self.assertNotIn("-DPR_OFFLOAD_AMU=1", cira)
        self.assertIn("-DPR_CIRA_POLICY_PGO=1", cira)
        self.assertIn("-DPR_CIRA_SOURCE_ROW=1", cira)


class MatchedVariantBitGateTest(unittest.TestCase):
    @staticmethod
    def write_words(path, words):
        path.write_bytes(b"".join(struct.pack("<I", word) for word in words))

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
                reference, {"amu": amu, "cira": cira}, expected_words=3
            )
        self.assertEqual(evidence["compared_words"], 3)
        self.assertEqual(evidence["mismatches"], {"amu": 0, "cira": 0})
        self.assertEqual(evidence["sha256"]["baseline"], evidence["sha256"]["amu"])
        self.assertEqual(evidence["sha256"]["baseline"], evidence["sha256"]["cira"])

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
                variants.validate_raw_outputs(reference, {"amu": amu}, 2)


if __name__ == "__main__":
    unittest.main()
