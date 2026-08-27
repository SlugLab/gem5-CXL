# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

import hashlib
import json
import struct
import tempfile
import unittest
from pathlib import Path

from scripts import generate_formal_spatter_inputs as generator


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class FormalSpatterExpansionTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def write_trace(self, records):
        path = self.root / "trace.json"
        path.write_text(json.dumps(records) + "\n", encoding="utf-8")
        return path

    def test_load_records_selects_kernel_in_source_order(self):
        path = self.write_trace([
            {"kernel": "Gather", "count": 9, "delta": 1,
             "pattern": [0]},
            {"kernel": "Scatter", "count": 2, "delta": 10,
             "pattern": [0, 2]},
            {"kernel": "Scatter", "count": 1, "delta": 1,
             "pattern": [1, 3]},
        ])
        records = generator.load_records(path, sha256(path), "Scatter")
        self.assertEqual([row.count for row in records], [2, 1])
        self.assertEqual([row.pattern for row in records], [(0, 2), (1, 3)])

    def test_load_records_rejects_source_hash_drift(self):
        path = self.write_trace([
            {"kernel": "Gather", "count": 1, "delta": 1,
             "pattern": [0]},
        ])
        with self.assertRaisesRegex(generator.GenerationError, "SHA-256"):
            generator.load_records(path, "0" * 64, "Gather")

    def test_load_records_rejects_malformed_records(self):
        valid = {
            "kernel": "Gather", "count": 1, "delta": 1,
            "pattern": [0],
        }
        mutations = (
            ("count", -1, "count"),
            ("count", True, "count"),
            ("delta", -1, "delta"),
            ("delta", False, "delta"),
            ("pattern", [], "pattern"),
            ("pattern", [-1], "pattern"),
            ("pattern", [True], "pattern"),
            ("kernel", 7, "kernel"),
        )
        for field, replacement, message in mutations:
            with self.subTest(field=field, replacement=replacement):
                row = dict(valid)
                row[field] = replacement
                path = self.write_trace([row])
                with self.assertRaisesRegex(generator.GenerationError, message):
                    generator.load_records(path, sha256(path), "Gather")

    def test_load_records_rejects_empty_selection_and_u64_overflow(self):
        path = self.write_trace([
            {"kernel": "Gather", "count": 1, "delta": 1,
             "pattern": [0]},
        ])
        with self.assertRaisesRegex(generator.GenerationError, "selection"):
            generator.load_records(path, sha256(path), "Scatter")
        path = self.write_trace([
            {"kernel": "Gather", "count": 2, "delta": (1 << 64) - 1,
             "pattern": [1]},
        ])
        with self.assertRaisesRegex(generator.GenerationError, "64-bit"):
            generator.load_records(path, sha256(path), "Gather")

    def test_layout_preserves_record_order_and_separates_epochs(self):
        path = self.write_trace([
            {"kernel": "Scatter", "count": 2, "delta": 10,
             "pattern": [0, 2]},
            {"kernel": "Scatter", "count": 1, "delta": 1,
             "pattern": [1, 3]},
        ])
        layout = generator.layout(
            generator.load_records(path, sha256(path), "Scatter")
        )
        expected = [0, 2, 10, 12, 14, 16, 17, 19, 27, 29, 31, 33]
        self.assertEqual(list(generator.indices(layout, epochs=2)), expected)
        self.assertEqual(layout.index_count, 6)
        self.assertEqual(layout.index_span, 17)
        self.assertLess(max(expected[:6]), min(expected[6:]))

    def test_resident_bytes_and_minimum_whole_epochs_are_exact(self):
        path = self.write_trace([
            {"kernel": "Gather", "count": 2, "delta": 10,
             "pattern": [0, 2]},
            {"kernel": "Gather", "count": 1, "delta": 1,
             "pattern": [1, 3]},
        ])
        layout = generator.layout(
            generator.load_records(path, sha256(path), "Gather")
        )
        self.assertEqual(generator.resident_bytes(layout, 1, "gather"), 140)
        self.assertEqual(generator.resident_bytes(layout, 1, "scatter"), 140)
        self.assertEqual(generator.required_epochs(layout, "gather", 140), 1)
        self.assertEqual(generator.required_epochs(layout, "gather", 141), 2)
        self.assertEqual(generator.required_epochs(layout, "scatter", 141), 2)

    def test_value_bits_are_finite_normal_and_position_deterministic(self):
        observed = [generator.value_bits(position) for position in range(100)]
        self.assertEqual(observed, [
            generator.value_bits(position) for position in range(100)
        ])
        self.assertGreater(len(set(observed)), 90)
        for bits in observed:
            self.assertEqual(bits & 0x7f800000, 0x3f000000)


class FormalSpatterArtifactTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "source.json"
        self.source.write_text(json.dumps([
            {"kernel": "Gather", "count": 2, "delta": 10,
             "pattern": [0, 2]},
            {"kernel": "Gather", "count": 1, "delta": 1,
             "pattern": [1, 3]},
        ]) + "\n", encoding="utf-8")

    def spec(self, *, workload="amg_gather", mode="gather"):
        return generator.GenerationSpec(
            workload=workload,
            mode=mode,
            selected_kernel="Gather",
            source_trace=self.source.resolve(),
            source_trace_sha256=sha256(self.source),
            source_commit="a" * 40,
            minimum_bytes=1,
        )

    def validation(self, artifacts):
        return {
            "schema": 1,
            "status": "accepted",
            "workload": artifacts.workload,
            "values_sha256": artifacts.values_sha256,
            "index_sha256": artifacts.index_sha256,
            "destination_sha256": "d" * 64,
            "reference_binary_sha256": "b" * 64,
        }

    def test_generate_once_writes_exact_little_endian_streams(self):
        artifacts = generator.generate_once(self.spec(), self.root / "once")
        expected_index = (0, 2, 10, 12, 14, 16)
        expected_values = tuple(generator.value_bits(i) for i in range(17))
        self.assertEqual(
            artifacts.index_path.read_bytes(),
            struct.pack("<6Q", *expected_index),
        )
        self.assertEqual(
            artifacts.values_path.read_bytes(),
            struct.pack("<17I", *expected_values),
        )
        self.assertEqual(artifacts.epochs, 1)
        self.assertEqual(artifacts.index_count, 6)
        self.assertEqual(artifacts.values_count, 17)
        self.assertEqual(artifacts.maximum_index, 16)
        self.assertEqual(artifacts.resident_bytes, 140)

    def test_two_independent_generations_have_identical_identity(self):
        first = generator.generate_once(self.spec(), self.root / "first")
        second = generator.generate_once(self.spec(), self.root / "second")
        generator.compare_generations(first, second)
        self.assertEqual(first.values_sha256, second.values_sha256)
        self.assertEqual(first.index_sha256, second.index_sha256)

    def test_compare_generations_rejects_post_generation_tampering(self):
        first = generator.generate_once(self.spec(), self.root / "first")
        second = generator.generate_once(self.spec(), self.root / "second")
        second.values_path.write_bytes(b"changed")
        with self.assertRaisesRegex(generator.GenerationError, "regeneration"):
            generator.compare_generations(first, second)

    def test_generate_twice_keeps_primary_and_removes_replay(self):
        verified = generator.generate_twice(self.spec(), self.root / "stage")
        self.assertTrue(verified.artifacts.values_path.is_file())
        self.assertFalse((self.root / "stage/replay").exists())
        self.assertEqual(
            verified.provenance["independent_regeneration"]["status"],
            "pass",
        )

    def test_promotion_requires_accepted_matching_validation(self):
        verified = generator.generate_twice(self.spec(), self.root / "stage")
        with self.assertRaisesRegex(generator.GenerationError, "validation"):
            generator.promote_validated(
                verified, {"status": "failed"}, self.root / "published"
            )
        self.assertFalse((self.root / "published/amg_gather").exists())

    def test_promotion_is_content_addressed_and_rejects_conflict(self):
        verified = generator.generate_twice(self.spec(), self.root / "stage")
        published = generator.promote_validated(
            verified, self.validation(verified.artifacts),
            self.root / "published",
        )
        self.assertEqual(published.name, verified.artifact_id)
        self.assertTrue((published / "values.f32le").is_file())
        self.assertTrue((published / "index.u64le").is_file())
        self.assertTrue((published / "provenance.json").is_file())
        self.assertTrue((published / "validation.json").is_file())

        (published / "values.f32le").write_bytes(b"conflict")
        replacement = generator.generate_twice(
            self.spec(), self.root / "replacement"
        )
        with self.assertRaisesRegex(generator.GenerationError, "conflict"):
            generator.promote_validated(
                replacement, self.validation(replacement.artifacts),
                self.root / "published",
            )


if __name__ == "__main__":
    unittest.main()
