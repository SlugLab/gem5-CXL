# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

import dataclasses
import gzip
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import mcfreg2


class MCFStreamingEventsTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def make_package(
        self, rows=None, *, stored_events=None, declared_event_count=None
    ):
        if rows is None:
            rows = (
                {"kind": "BEGIN", "call": 0, "order": 0},
                {"kind": "END", "call": 0, "selected_arc_id": 4},
            )
        encoded = b"".join(
            json.dumps(row, sort_keys=True).encode("utf-8") + b"\n"
            for row in rows
        )
        if stored_events is None:
            stored_events = gzip.compress(encoded, mtime=0)
        sections = {
            name: f"{name.lower()}\n".encode("ascii")
            for name in mcfreg2.REQUIRED_SECTIONS
        }
        sections["EVENTS"] = stored_events
        event_count = (
            len(rows)
            if declared_event_count is None
            else declared_event_count
        )
        package = mcfreg2.new_package(
            nodes=4,
            active_arcs=6,
            dummy_arcs=3,
            arena_capacity=16,
            pricing_calls=1,
            price_out_calls=0,
            event_count=event_count,
            sections=sections,
            section_layouts={"EVENTS": (len(rows), 0)},
            section_schemas={"EVENTS": 3},
        )
        path = self.root / f"fixture-{len(tuple(self.root.iterdir()))}.reg2"
        mcfreg2.write_package(path, package)
        return path, list(rows)

    def event_entry(self, path):
        package = mcfreg2.read_package(
            path, lazy_section_names=("EVENTS",)
        )
        return next(
            entry
            for entry in package.directory
            if entry.section_type == mcfreg2.SECTION_TYPES["EVENTS"]
        )

    def test_stream_events_never_reads_the_whole_section(self):
        path, expected = self.make_package()
        with mock.patch.object(
            mcfreg2.SectionView,
            "read",
            side_effect=AssertionError("EVENTS must stay lazy"),
        ):
            self.assertEqual(
                [item.row for item in mcfreg2.stream_events(path)], expected
            )

    def test_stream_events_rejects_corruption_by_stored_digest(self):
        path, _ = self.make_package()
        entry = self.event_entry(path)
        image = bytearray(path.read_bytes())
        image[entry.offset + entry.stored_bytes - 1] ^= 0x01
        path.write_bytes(image)
        with self.assertRaisesRegex(mcfreg2.FormatError, "EVENTS SHA-256"):
            list(mcfreg2.stream_events(path))

    def test_stream_events_rejects_truncated_gzip(self):
        encoded = b'{"kind":"BEGIN","call":0}\n'
        path, _ = self.make_package(
            rows=({"kind": "BEGIN", "call": 0},),
            stored_events=gzip.compress(encoded, mtime=0)[:-4],
        )
        with self.assertRaisesRegex(mcfreg2.FormatError, "gzip stream"):
            list(mcfreg2.stream_events(path))

    def test_stream_events_rejects_malformed_json(self):
        path, _ = self.make_package(
            rows=({"kind": "BEGIN", "call": 0},),
            stored_events=gzip.compress(b"{not-json}\n", mtime=0),
        )
        with self.assertRaisesRegex(mcfreg2.FormatError, "row 1"):
            list(mcfreg2.stream_events(path))

    def test_stream_events_rejects_wrong_event_count(self):
        path, _ = self.make_package(declared_event_count=3)
        with self.assertRaisesRegex(mcfreg2.FormatError, "event count"):
            list(mcfreg2.stream_events(path))

    def test_stream_events_rejects_trailing_data(self):
        row = {"kind": "BEGIN", "call": 0}
        encoded = json.dumps(row).encode("utf-8") + b"\n"
        path, _ = self.make_package(
            rows=(row,),
            stored_events=gzip.compress(encoded, mtime=0) + b"trailing",
        )
        with self.assertRaisesRegex(mcfreg2.FormatError, "gzip stream"):
            list(mcfreg2.stream_events(path))


if __name__ == "__main__":
    unittest.main()
