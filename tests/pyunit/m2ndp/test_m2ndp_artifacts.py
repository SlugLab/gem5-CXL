# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

import json
import struct
import tempfile
import unittest
from pathlib import Path

from scripts import m2ndp_artifacts as artifacts


def reference_header(num_nodes):
    return {
        "schema": artifacts.REFERENCE_SCHEMA,
        "graph_sha256": artifacts.EXPECTED_G20_SHA256,
        "num_nodes": num_nodes,
        "iterations": 20,
        "measured_trial": 1,
        "binary_sha256": "a" * 64,
        "source_sha256": "b" * 64,
    }


class M2NDPArtifactTest(unittest.TestCase):
    def test_graph_bundle_rejects_wrong_g20_hash(self):
        meta = artifacts.GraphMeta(
            graph_sha256="0" * 64,
            num_nodes=4,
            num_directed_edges=5,
            directed=True,
        )
        with self.assertRaisesRegex(
            artifacts.EvidenceError, "graph SHA-256"
        ):
            artifacts.validate_publication_graph(meta, smoke_test=False)

    def test_smoke_graph_accepts_nonpublication_hash(self):
        meta = artifacts.GraphMeta(
            graph_sha256="0" * 64,
            num_nodes=4,
            num_directed_edges=5,
            directed=True,
        )
        artifacts.validate_publication_graph(meta, smoke_test=True)

    def test_reference_container_preserves_float_bits(self):
        words = [0x3F800000, 0x80000000, 0x7FC00001]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "reference.m2pr"
            header = reference_header(len(words))
            artifacts.write_reference(path, header, words)
            actual_header, actual_words = artifacts.read_reference(path)
        self.assertEqual(actual_header, header)
        self.assertEqual(actual_words, words)

    def test_reference_rejects_truncated_words(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.m2pr"
            artifacts.write_reference(path, reference_header(2), [1, 2])
            path.write_bytes(path.read_bytes()[:-4])
            with self.assertRaisesRegex(
                artifacts.EvidenceError, "word count"
            ):
                artifacts.read_reference(path)

    def test_reference_rejects_trailing_partial_word(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.m2pr"
            artifacts.write_reference(path, reference_header(2), [1, 2])
            path.write_bytes(path.read_bytes() + b"\x00")
            with self.assertRaisesRegex(
                artifacts.EvidenceError, "partial word"
            ):
                artifacts.read_reference(path)

    def test_reference_rejects_malformed_header(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.m2pr"
            payload = b"{"
            path.write_bytes(
                artifacts.REFERENCE_MAGIC
                + struct.pack("<I", len(payload))
                + payload
            )
            with self.assertRaisesRegex(
                artifacts.EvidenceError, "reference JSON"
            ):
                artifacts.read_reference(path)

    def test_atomic_json_replaces_complete_document(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            artifacts.atomic_write_json(path, {"generation": 1})
            artifacts.atomic_write_json(
                path, {"generation": 2, "status": "passed"}
            )
            self.assertEqual(
                json.loads(path.read_text()),
                {"generation": 2, "status": "passed"},
            )
            self.assertFalse(path.with_name(path.name + ".tmp").exists())


if __name__ == "__main__":
    unittest.main()
