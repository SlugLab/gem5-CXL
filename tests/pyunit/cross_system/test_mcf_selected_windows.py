# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

import dataclasses
import gzip
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import mcfreg2
from scripts import stratified_timing as timing

try:
    from scripts import mcf_selected_windows as selected
except ImportError:
    selected = None


class MCFStreamingEventsTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def make_package(
        self,
        rows=None,
        *,
        stored_events=None,
        declared_event_count=None,
        pricing_calls=1,
        price_out_calls=0,
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
            pricing_calls=pricing_calls,
            price_out_calls=price_out_calls,
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


class MCFSelectedWindowsTest(MCFStreamingEventsTest):
    def require_selector(self):
        self.assertIsNotNone(
            selected, "scripts.mcf_selected_windows is missing"
        )

    def selector_package(self, *, mutate=None):
        rows = []
        order = 0
        for phase in ("pricing", "price_out"):
            for call in range(128):
                begin = {
                    "kind": "CALL_BEGIN",
                    "role": "live_in",
                    "call": call,
                    "order": order,
                    "ordinal": call,
                    "phase": phase,
                }
                end = {
                    "kind": "CALL_END",
                    "role": "observed_result",
                    "call": call,
                    "order": order,
                    "ordinal": call,
                    "phase": phase,
                }
                if mutate is not None:
                    mutate(phase, call, begin, end)
                rows.extend((begin, end))
                order += 1
        return self.make_package(
            rows=tuple(rows), pricing_calls=128, price_out_calls=128
        )[0]

    def plans(self):
        digest = hashlib.sha256(b"mcf-plan").hexdigest()
        return {
            "pricing_kernel": timing.SamplingPlan(
                digest,
                "pricing_kernel",
                128,
                1,
                False,
                1,
                (
                    timing.TimingWindow(0, 0, 1, 2),
                    timing.TimingWindow(1, 2, 3, 4),
                ),
            ),
            "price_out_impl": timing.SamplingPlan(
                digest,
                "price_out_impl",
                128,
                1,
                False,
                2,
                (timing.TimingWindow(2, 4, 5, 6),),
            ),
        }

    def test_selects_union_of_windows_in_one_scan(self):
        self.require_selector()
        package = self.selector_package()
        plans = self.plans()
        with mock.patch.object(
            mcfreg2, "stream_events", wraps=mcfreg2.stream_events
        ) as stream:
            result = selected.select_windows(
                package, plans, self.root / "selected"
            )
        self.assertEqual(stream.call_count, 1)
        self.assertLess(
            result.retained_event_count, result.source_event_count
        )
        coordinate = selected.read_coordinate(
            result.root, "pricing_kernel", 0
        )
        self.assertEqual(
            coordinate.measure_start,
            plans["pricing_kernel"].windows[0].measure_start,
        )

    def test_selector_is_deterministic(self):
        self.require_selector()
        package = self.selector_package()
        first = selected.select_windows(
            package, self.plans(), self.root / "first"
        )
        second = selected.select_windows(
            package, self.plans(), self.root / "second"
        )
        self.assertEqual(first.package_sha256, second.package_sha256)
        self.assertEqual(first.index_sha256, second.index_sha256)

    def test_selector_rejects_cross_call_and_cross_phase(self):
        self.require_selector()

        def corrupt_end(phase, call, begin, end):
            if phase == "pricing" and call == 0:
                end["phase"] = "price_out"

        with self.assertRaisesRegex(selected.SelectionError, "call|phase"):
            selected.select_windows(
                self.selector_package(mutate=corrupt_end),
                self.plans(),
                self.root / "cross-call",
            )
        plans = self.plans()
        plans["unknown_phase"] = plans.pop("pricing_kernel")
        with self.assertRaisesRegex(selected.SelectionError, "phase"):
            selected.select_windows(
                self.selector_package(), plans, self.root / "cross-phase"
            )

    def test_selector_rejects_cross_stratum_and_duplicate_coordinate(self):
        self.require_selector()
        package = self.selector_package()
        plans = self.plans()
        plans["pricing_kernel"] = dataclasses.replace(
            plans["pricing_kernel"],
            windows=(timing.TimingWindow(0, 1, 2, 3),),
        )
        with self.assertRaisesRegex(selected.SelectionError, "stratum"):
            selected.select_windows(
                package, plans, self.root / "cross-stratum"
            )

        plans = self.plans()
        duplicate = plans["pricing_kernel"].windows[0]
        plans["pricing_kernel"] = dataclasses.replace(
            plans["pricing_kernel"], windows=(duplicate, duplicate)
        )
        with self.assertRaisesRegex(selected.SelectionError, "duplicate"):
            selected.select_windows(
                package, plans, self.root / "duplicate"
            )

    def test_selector_removes_crash_residue_and_enforces_budget(self):
        self.require_selector()
        package = self.selector_package()
        output = self.root / "crash"
        with mock.patch.object(
            selected,
            "_publish_directory",
            side_effect=OSError("injected publication crash"),
        ):
            with self.assertRaisesRegex(OSError, "injected"):
                selected.select_windows(package, self.plans(), output)
        self.assertFalse(output.exists())
        self.assertEqual(list(self.root.glob(".crash.*")), [])

        with self.assertRaisesRegex(selected.SelectionError, "budget"):
            selected.select_windows(
                package,
                self.plans(),
                self.root / "tiny",
                storage_limit_bytes=1,
            )

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
