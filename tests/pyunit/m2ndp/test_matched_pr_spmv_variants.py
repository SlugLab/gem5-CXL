# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

import struct
import tempfile
import unittest
from pathlib import Path

from scripts import build_gapbs_matched_pr_spmv_variants as variants


REPO = Path(__file__).resolve().parents[3]
FIXED_SOURCE = REPO / "util/m2ndp/gapbs_pr_spmv_fixed.cc"


class MatchedVariantSourceTest(unittest.TestCase):
    def test_fixed_pull_rows_are_partitioned_across_both_cores(self):
        source = FIXED_SOURCE.read_text(encoding="utf-8")
        self.assertIn(
            "#pragma omp parallel for schedule(static)\n"
            "    for (NodeID u = 0; u < g.num_nodes(); ++u)",
            source,
        )
        self.assertNotIn("schedule(dynamic, 16384)", source)

    def test_amu_batches_loads_but_commits_float_adds_in_csr_order(self):
        generated = variants.transform_source(
            FIXED_SOURCE.read_text(encoding="utf-8"), "amu"
        )

        node_load = generated.index(
            "load_values(node_addrs, nodes, amu_count)"
        )
        score_load = generated.index(
            "load_values(score_addrs, scores_batch, amu_count)"
        )
        ordered_add = generated.index(
            "incoming_total = incoming_total + scores_batch[amu_i]"
        )
        self.assertLess(node_load, score_load)
        self.assertLess(score_load, ordered_add)
        self.assertEqual(
            generated.count(
                "incoming_total = incoming_total + scores_batch[amu_i]"
            ),
            1,
        )
        self.assertIn("constexpr int kPageRankIterations = 20;", generated)

    def test_cira_prefetches_future_row_batches_before_ordered_adds(self):
        generated = variants.transform_source(
            FIXED_SOURCE.read_text(encoding="utf-8"), "cira"
        )

        future = generated.index(
            "GAPBS_CIRA_FUTURE_BLOCK(g, u, pf_begin, pf_count)"
        )
        boundary = generated.index("u % GAPBS_CIRA_ROW_BATCH == 0")
        prefetch = generated.index(
            "GAPBS_CIRA_PREFETCH_IN_CSR_INDEXED_ROWS("
            "g, pf_begin, pf_count, outgoing_contrib)"
        )
        current = generated.index("auto neigh = g.in_neigh(u)")
        ordered_add = generated.index(
            "incoming_total = incoming_total + outgoing_contrib[v]"
        )
        self.assertLess(boundary, future)
        self.assertLess(future, prefetch)
        self.assertLess(prefetch, current)
        self.assertLess(current, ordered_add)
        self.assertEqual(
            generated.count(
                "incoming_total = incoming_total + outgoing_contrib[v]"
            ),
            1,
        )
        self.assertNotIn("GAPBS_CIRA_PREFETCH_IN_CSR_INDEXED_ROW(", generated)

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

    def test_cira_compile_command_sets_aggressive_row_batch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            command = variants.compile_command(
                kind="cira",
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

        self.assertIn("-DGAPBS_CIRA_ROW_BATCH=256", command)


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
