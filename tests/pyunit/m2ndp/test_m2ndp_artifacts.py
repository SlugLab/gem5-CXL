# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

import json
import struct
import tempfile
import unittest
from pathlib import Path

from scripts import gapbs_pr_experiment_profiles as profiles
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
    def test_funcsim_compares_every_named_typed_boundary_bit_exact(self):
        expected = {
            "rank": {"element_type": "f32", "word_bits": 32,
                     "raw_words": [0x3F800000, 0x80000000]},
            "residual": {"element_type": "f64", "word_bits": 64,
                         "raw_words": [0x3FF0000000000000]},
            "parent": {"element_type": "i64", "word_bits": 64,
                       "raw_words": [0xFFFFFFFFFFFFFFFF, 7]},
        }
        evidence = artifacts.compare_funcsim_boundaries(
            expected, expected, returncode=0,
            expected_launches=9, completed_launches=9,
        )
        self.assertEqual(evidence["status"], "pass")
        self.assertEqual(evidence["boundary_count"], 3)
        self.assertEqual(evidence["compared_words"], 5)

    def test_funcsim_boundary_mismatch_is_a_nonzero_exit(self):
        expected = {
            "rank": {"element_type": "f32", "word_bits": 32,
                     "raw_words": [0x3F800000]},
        }
        observed = {
            "rank": {"element_type": "f32", "word_bits": 32,
                     "raw_words": [0x3F800001]},
        }
        self.assertEqual(
            artifacts.funcsim_boundary_exit_code(expected, observed), 2
        )
        with self.assertRaisesRegex(artifacts.EvidenceError, r"rank\[0\]"):
            artifacts.compare_funcsim_boundaries(
                expected, observed, returncode=2,
                expected_launches=1, completed_launches=1,
            )

    def test_funcsim_rejects_missing_boundary_or_launch(self):
        boundary = {
            "rank": {"element_type": "f32", "word_bits": 32,
                     "raw_words": [0]},
        }
        with self.assertRaisesRegex(artifacts.EvidenceError, "boundary set"):
            artifacts.compare_funcsim_boundaries(
                boundary, {}, returncode=2,
                expected_launches=1, completed_launches=1,
            )
        with self.assertRaisesRegex(artifacts.EvidenceError, "launch"):
            artifacts.compare_funcsim_boundaries(
                boundary, boundary, returncode=0,
                expected_launches=2, completed_launches=1,
            )

    def test_ndpsim_timing_requires_funcsim_pass_and_one_cycle_calibration(self):
        functional = {
            "status": "pass", "boundary_count": 2,
            "compared_words": 8, "expected_launches": 3,
            "completed_launches": 3, "returncode": 0,
        }
        calibration = {
            "passed": True, "cxl_delay": "1us",
            "target_ns": "2012.652", "measured_ns": "2012.625",
            "residual_ns": "0.027", "link_period_ns": "0.125",
            "target_cxl_boundary_ticks": 2_012_652,
        }
        self.assertTrue(
            artifacts.require_ndpsim_timing_gate(functional, calibration)
        )
        with self.assertRaisesRegex(artifacts.EvidenceError, "link cycle"):
            artifacts.require_ndpsim_timing_gate(
                functional,
                {**calibration, "measured_ns": "2012.800",
                 "residual_ns": "0.148"},
            )
        with self.assertRaisesRegex(artifacts.EvidenceError, "FuncSim"):
            artifacts.require_ndpsim_timing_gate(
                {**functional, "status": "failed"}, calibration
            )

    def test_g4_metadata_passes_formal_profile(self):
        meta = artifacts.GraphMeta(
            graph_sha256=profiles.G4_SHA256,
            num_nodes=16,
            num_directed_edges=64,
            directed=False,
        )
        artifacts.validate_profile_graph(
            meta, profiles.get_profile("g4-4thread-sweep")
        )

    def test_profile_graph_rejects_wrong_node_count(self):
        meta = artifacts.GraphMeta(
            graph_sha256=profiles.G4_SHA256,
            num_nodes=15,
            num_directed_edges=64,
            directed=False,
        )
        with self.assertRaisesRegex(
            artifacts.EvidenceError, "node count"
        ):
            artifacts.validate_profile_graph(
                meta, profiles.get_profile("g4-4thread-sweep")
            )

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

    def test_publication_g20_preserves_undirected_flag(self):
        meta = artifacts.GraphMeta(
            graph_sha256=artifacts.EXPECTED_G20_SHA256,
            num_nodes=1048576,
            num_directed_edges=31399382,
            directed=False,
        )
        artifacts.validate_publication_graph(meta, smoke_test=False)

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
