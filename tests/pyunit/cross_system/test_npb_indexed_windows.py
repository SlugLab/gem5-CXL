# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

import dataclasses
import importlib.util
import tempfile
import unittest
from pathlib import Path

from scripts import lazy_work_trace as lazy
from scripts import npb_lazy_trace as npb

try:
    from scripts import npb_indexed_windows as indexed
except ImportError:
    indexed = None


def _load_fixture_module():
    path = Path(__file__).with_name("test_npb_lazy_trace.py")
    spec = importlib.util.spec_from_file_location("npb_fixture_module", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


FIXTURES = _load_fixture_module()


class NPBIndexedWindowsTest(unittest.TestCase):
    def setUp(self):
        self.cg_case = FIXTURES.NpbCgLazyTraceTest("runTest")
        self.cg_case.setUp()
        self.addCleanup(self.cg_case.doCleanups)
        self.mg_case = FIXTURES.NpbMgLazyTraceTest("runTest")
        self.mg_case.setUp()
        self.addCleanup(self.mg_case.doCleanups)

    def require_indexer(self):
        self.assertIsNotNone(
            indexed, "scripts.npb_indexed_windows is missing"
        )

    def cg_bundle(self):
        bundle = self.cg_case.full_cg_bundle()
        invocations = tuple(
            dataclasses.replace(
                invocation,
                parameters={**invocation.parameters, "nonzeros": 5},
            )
            if invocation.kernel == "npb_cg_spmv" else invocation
            for invocation in bundle.invocations
        )
        return dataclasses.replace(bundle, invocations=invocations)

    def test_cardinality_matches_every_fixture_expander(self):
        self.require_indexer()
        cg = self.cg_bundle()
        with lazy.MappedState(cg) as state:
            for invocation in cg.invocations:
                with self.subTest(kernel=invocation.kernel):
                    actual = sum(
                        1 for _ in npb.EXPANDERS[invocation.kernel](
                            state, invocation, 4
                        )
                    )
                    self.assertEqual(npb.primitive_count(invocation), actual)

        mg = self.mg_case.full_vcycle_bundle()
        with lazy.MappedState(mg) as state:
            for invocation in mg.invocations:
                with self.subTest(kernel=invocation.kernel):
                    actual = sum(
                        1 for _ in npb.EXPANDERS[invocation.kernel](
                            state, invocation, 4
                        )
                    )
                    self.assertEqual(npb.primitive_count(invocation), actual)

        zero = lazy.Invocation(
            0,
            200,
            "npb_mg_zero3",
            1,
            64,
            {
                "u": "vc_coarse_u",
                "n1": 4,
                "n2": 4,
                "n3": 4,
                "boundaries": [],
            },
        )
        with lazy.MappedState(mg) as state:
            actual = sum(1 for _ in npb.expand_mg_zero3(state, zero, 4))
        self.assertEqual(npb.primitive_count(zero), actual)
        self.assertEqual(set(npb.EXPANDERS), {
            invocation.kernel for invocation in cg.invocations
        } | {invocation.kernel for invocation in mg.invocations} | {
            zero.kernel
        })

    def test_index_covers_dynamic_count_and_locates_boundaries(self):
        self.require_indexer()
        bundle = self.cg_bundle()
        index = indexed.build_index(bundle)
        self.assertEqual(index.segments[0].primitive_begin, 0)
        self.assertEqual(
            index.segments[-1].primitive_end,
            bundle.dynamic_work["primitive_records"],
        )
        for segment in index.segments:
            located = indexed.locate(index, segment.primitive_begin)
            self.assertEqual(located.segment, segment)
            self.assertEqual(located.local_offset, 0)
            located = indexed.locate(index, segment.primitive_end - 1)
            self.assertEqual(
                located.local_offset,
                segment.primitive_end - segment.primitive_begin - 1,
            )

    def test_index_rejects_terminal_count_drift_and_out_of_range_seek(self):
        self.require_indexer()
        bundle = self.cg_bundle()
        with self.assertRaisesRegex(indexed.IndexError, "terminal|count"):
            indexed.build_index(dataclasses.replace(
                bundle,
                dynamic_work={
                    "primitive_records": (
                        bundle.dynamic_work["primitive_records"] + 1
                    )
                },
            ))
        index = indexed.build_index(bundle)
        for offset in (-1, index.primitive_records):
            with self.subTest(offset=offset):
                with self.assertRaisesRegex(indexed.IndexError, "outside"):
                    indexed.locate(index, offset)


if __name__ == "__main__":
    unittest.main()
