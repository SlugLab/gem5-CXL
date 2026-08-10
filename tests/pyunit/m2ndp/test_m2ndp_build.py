# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

import struct
import importlib
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import m2ndp_artifacts as artifacts


REPO = Path(__file__).resolve().parents[3]


def write_words(path, fmt, values):
    path.write_bytes(
        b"".join(struct.pack(fmt, value) for value in values)
    )


def write_meta(root, *, nodes=3, edges=4):
    artifacts.atomic_write_json(
        root / "graph.meta.json",
        {
            "schema": 1,
            "graph_sha256": artifacts.EXPECTED_G20_SHA256,
            "num_nodes": nodes,
            "num_directed_edges": edges,
            "directed": True,
        },
    )


def write_valid_bundle(root):
    write_words(root / "in_offsets.u64", "<Q", [0, 1, 3, 4])
    write_words(root / "in_neighbors.i32", "<i", [2, 0, 2, 1])
    write_words(root / "out_degree.u32", "<I", [1, 1, 2])
    write_meta(root)


class GraphBundleTest(unittest.TestCase):
    def test_valid_bundle_preserves_neighbor_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_valid_bundle(root)
            bundle = artifacts.load_graph_bundle(root)
        self.assertEqual(bundle.in_offsets, (0, 1, 3, 4))
        self.assertEqual(bundle.in_neighbors, (2, 0, 2, 1))
        self.assertEqual(bundle.out_degree, (1, 1, 2))

    def test_bundle_rejects_bad_terminal_offset(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_valid_bundle(root)
            write_words(root / "in_offsets.u64", "<Q", [0, 1, 3, 5])
            with self.assertRaisesRegex(
                artifacts.EvidenceError, "terminal CSR offset"
            ):
                artifacts.load_graph_bundle(root)

    def test_bundle_rejects_nonmonotonic_offsets(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_valid_bundle(root)
            write_words(root / "in_offsets.u64", "<Q", [0, 3, 2, 4])
            with self.assertRaisesRegex(
                artifacts.EvidenceError, "non-monotonic"
            ):
                artifacts.load_graph_bundle(root)

    def test_bundle_rejects_neighbor_outside_vertex_range(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_valid_bundle(root)
            write_words(root / "in_neighbors.i32", "<i", [2, 0, 3, 1])
            with self.assertRaisesRegex(
                artifacts.EvidenceError, "neighbor.*outside"
            ):
                artifacts.load_graph_bundle(root)

    def test_bundle_rejects_wrong_component_size(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_valid_bundle(root)
            (root / "out_degree.u32").write_bytes(b"\x00")
            with self.assertRaisesRegex(
                artifacts.EvidenceError, "out_degree.*byte size"
            ):
                artifacts.load_graph_bundle(root)

    def test_bundle_components_are_not_read_whole_into_memory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_valid_bundle(root)
            with mock.patch.object(
                Path,
                "read_bytes",
                side_effect=AssertionError("whole-file read"),
            ):
                bundle = artifacts.load_graph_bundle(root)
                self.assertEqual(tuple(bundle.in_neighbors), (2, 0, 2, 1))

    def test_finalize_meta_hashes_source_graph(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            graph = root / "tiny.sg"
            graph.write_bytes(b"serialized graph")
            meta = artifacts.finalize_graph_meta(
                root,
                graph,
                "M2NDP_GRAPH_EXPORT nodes=3 directed_edges=4 directed=1\n",
            )
            stored = artifacts.load_graph_meta(root / "graph.meta.json")
            expected_hash = artifacts.sha256_file(graph)
        self.assertEqual(meta, stored)
        self.assertEqual(meta.graph_sha256, expected_hash)

    def test_finalize_meta_rejects_duplicate_export_marker(self):
        marker = (
            "M2NDP_GRAPH_EXPORT nodes=3 directed_edges=4 directed=1\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            graph = root / "tiny.sg"
            graph.write_bytes(b"serialized graph")
            with self.assertRaisesRegex(
                artifacts.EvidenceError, "exactly one"
            ):
                artifacts.finalize_graph_meta(root, graph, marker + marker)


class MatchedPageRankSourceTest(unittest.TestCase):
    def test_copied_gapbs_builder_preserves_generated_scale_node_space(self):
        builder = importlib.import_module(
            "scripts.build_gapbs_m2ndp_pr_spmv"
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "src/builder.h"
            source.parent.mkdir(parents=True)
            source.write_text(
                "      } else if (cli_.scale() != -1) {\n"
                "        Generator<NodeID_, DestID_> gen(cli_.scale(), cli_.degree());\n"
                "        el = gen.GenerateEL(cli_.uniform());\n"
                "      }\n"
                "      g = MakeGraphFromEL(el);\n",
                encoding="utf-8",
            )

            builder.patch_generated_graph_node_count(root)
            patched = source.read_text(encoding="utf-8")

        self.assertIn("num_nodes_ = int64_t{1} << cli_.scale();", patched)
        self.assertLess(
            patched.index("num_nodes_ = int64_t{1} << cli_.scale();"),
            patched.index("g = MakeGraphFromEL(el);"),
        )

    def test_fixed_source_has_matched_roi_contract(self):
        source = (
            REPO / "util/m2ndp/gapbs_pr_spmv_fixed.cc"
        ).read_text()
        self.assertIn("constexpr int kPageRankIterations = 20;", source)
        self.assertLess(
            source.index("pvector<ScoreT> scores(g.num_nodes())"),
            source.index("m5_work_begin(trial, 0)"),
        )
        self.assertLess(
            source.index("m5_work_end(trial, 0)"),
            source.rindex("CommitScoreBits("),
        )
        self.assertNotIn("fsync(descriptor)", source)
        self.assertNotIn("O_TRUNC", source)
        self.assertNotIn("OpenReferenceOutput", source)
        self.assertIn("m5_write_file(", source)
        self.assertIn("_mm_clflush(", source)
        self.assertIn("_mm_mfence()", source)
        self.assertLess(
            source.index("_mm_mfence()"),
            source.index("m5_write_file("),
        )
        self.assertNotIn("reduction(+ : error)", source)
        self.assertNotIn("if (error <", source)
        self.assertIn("const ScoreT product = kDamp * incoming_total;", source)
        self.assertIn("scores[u] = base_score + product;", source)
        self.assertIn("PRVerifier", source)
        self.assertIn("Verification: PASS", source)

    def test_builder_manifest_and_float_flags_are_fixed(self):
        builder = importlib.import_module(
            "scripts.build_gapbs_m2ndp_pr_spmv"
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            command = builder.page_rank_compile_command(
                cxx="g++",
                gapbs_root=root / "gapbs",
                generated_dir=root / "generated",
                output=root / "bin/pr_spmv",
                m5_library=root / "libm5.a",
            )
            manifest = builder.build_manifest(
                reference_raw=root / "reference/scores.u32",
                compiler="g++ 13",
                flags=command[1:],
            )
        self.assertIn("-ffp-contract=off", command)
        self.assertIn("-fno-fast-math", command)
        self.assertEqual(manifest["page_rank_iterations"], 20)
        self.assertTrue(manifest["fixed_iterations"])
        self.assertFalse(manifest["convergence_reduction"])
        self.assertFalse(manifest["fp_contract"])
        self.assertTrue(Path(manifest["reference_raw_path"]).is_absolute())

    def test_reference_dump_uses_checkpoint_safe_m5_pseudo_op(self):
        source = (
            REPO / "util/m2ndp/gapbs_pr_spmv_fixed.cc"
        ).read_text()
        self.assertLess(
            source.index("m5_work_end(trial, 0)"),
            source.rindex("CommitScoreBits("),
        )
        self.assertNotIn("open(path.c_str()", source)
        self.assertIn("m5_write_file(", source)


if __name__ == "__main__":
    unittest.main()
