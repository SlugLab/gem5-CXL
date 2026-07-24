# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

import struct
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import m2ndp_artifacts as artifacts


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


if __name__ == "__main__":
    unittest.main()
