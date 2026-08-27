# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

import hashlib
import json
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


if __name__ == "__main__":
    unittest.main()
