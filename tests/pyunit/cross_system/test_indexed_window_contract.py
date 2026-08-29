# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

import dataclasses
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

try:
    from scripts import indexed_window_contract as contract
except ImportError:
    contract = None


def _digest(label):
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


class IndexedWindowContractTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def require_module(self):
        self.assertIsNotNone(
            contract, "scripts.indexed_window_contract is missing"
        )

    def make_index(self, *, primitive_records=18, segments=None):
        self.require_module()
        if segments is None:
            segments = (
                contract.IndexSegment(
                    0, 10, 0, 101, 1, "npb_cg_spmv", 2
                ),
                contract.IndexSegment(
                    10, 18, 1, 103, 1, "npb_cg_dot", 1
                ),
            )
        return contract.LazyIndex(
            schema=1,
            workload="npb_cg",
            descriptor_sha256=_digest("descriptor"),
            input_sha256=_digest("input"),
            source_sha256=_digest("source"),
            binary_sha256=_digest("binary"),
            config_sha256=_digest("config"),
            generator_sha256=_digest("generator"),
            primitive_records=primitive_records,
            segments=segments,
        )

    def test_index_requires_exact_prefix_coverage(self):
        index = self.make_index()
        path = contract.write_lazy_index(self.root / "index.json", index)
        self.assertEqual(contract.read_lazy_index(path), index)
        with self.assertRaisesRegex(contract.IndexedWindowError, "coverage"):
            contract.validate_lazy_index(
                dataclasses.replace(index, primitive_records=19)
            )

    def test_index_rejects_gap_overlap_and_noncontiguous_ordinal(self):
        valid = self.make_index()
        bad_segments = (
            (
                dataclasses.replace(valid.segments[0], primitive_end=9),
                valid.segments[1],
            ),
            (
                dataclasses.replace(valid.segments[0], primitive_end=11),
                valid.segments[1],
            ),
            (
                valid.segments[0],
                dataclasses.replace(valid.segments[1], ordinal=2),
            ),
        )
        for segments in bad_segments:
            with self.subTest(segments=segments):
                with self.assertRaisesRegex(
                    contract.IndexedWindowError, "coverage|ordinal"
                ):
                    contract.validate_lazy_index(
                        dataclasses.replace(valid, segments=segments)
                    )

    def test_reader_rejects_unknown_missing_and_bad_hash_fields(self):
        path = contract.write_lazy_index(
            self.root / "index.json", self.make_index()
        )
        record = json.loads(path.read_text(encoding="utf-8"))
        mutations = []
        unknown = dict(record)
        unknown["unexpected"] = 1
        mutations.append(unknown)
        missing = dict(record)
        del missing["workload"]
        mutations.append(missing)
        bad_hash = dict(record)
        bad_hash["source_sha256"] = "not-a-digest"
        mutations.append(bad_hash)
        for sequence, mutation in enumerate(mutations):
            with self.subTest(sequence=sequence):
                path.write_text(json.dumps(mutation), encoding="utf-8")
                with self.assertRaises(contract.IndexedWindowError):
                    contract.read_lazy_index(path)

    def test_reader_rejects_boolean_integer(self):
        path = contract.write_lazy_index(
            self.root / "index.json", self.make_index()
        )
        record = json.loads(path.read_text(encoding="utf-8"))
        record["segments"][0]["primitive_begin"] = False
        path.write_text(json.dumps(record), encoding="utf-8")
        with self.assertRaisesRegex(contract.IndexedWindowError, "integer"):
            contract.read_lazy_index(path)

    def test_budget_counts_retained_and_temporary_bytes(self):
        self.require_module()
        with self.assertRaisesRegex(contract.IndexedWindowError, "512 MiB"):
            contract.require_storage_budget(
                retained_bytes=500 * 1024 * 1024,
                temporary_bytes=13 * 1024 * 1024,
            )
        self.assertEqual(
            contract.require_storage_budget(
                retained_bytes=500 * 1024 * 1024,
                temporary_bytes=12 * 1024 * 1024,
            ),
            512 * 1024 * 1024,
        )
        for invalid in (-1, True):
            with self.subTest(invalid=invalid):
                with self.assertRaisesRegex(
                    contract.IndexedWindowError, "non-negative integer"
                ):
                    contract.require_storage_budget(
                        retained_bytes=invalid, temporary_bytes=0
                    )

    def test_retained_package_requires_existing_authenticated_file(self):
        self.require_module()
        missing = self.root / "missing.events.gz"
        package = contract.RetainedPackage(
            schema=1,
            workload="mcf",
            path=str(missing),
            sha256=_digest("missing"),
            retained_bytes=7,
            record_count=1,
        )
        with self.assertRaisesRegex(contract.IndexedWindowError, "missing"):
            contract.validate_retained_package(package)

        payload = self.root / "selected.events.gz"
        payload.write_bytes(b"payload")
        with self.assertRaisesRegex(contract.IndexedWindowError, "SHA-256"):
            contract.validate_retained_package(
                dataclasses.replace(package, path=str(payload))
            )
        authenticated = dataclasses.replace(
            package,
            path=str(payload),
            sha256=hashlib.sha256(payload.read_bytes()).hexdigest(),
        )
        self.assertEqual(
            contract.validate_retained_package(authenticated), authenticated
        )


if __name__ == "__main__":
    unittest.main()
