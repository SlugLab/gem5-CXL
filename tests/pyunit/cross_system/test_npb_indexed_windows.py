# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

import dataclasses
import importlib.util
import os
import tempfile
import unittest
from pathlib import Path

from scripts import canonical_work_trace as canonical
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

    def test_cg_descriptor_migration_adds_nonzeros_without_copying_images(self):
        self.require_indexer()
        source = self.cg_case.full_cg_bundle()
        self.assertTrue(any(
            invocation.kernel == "npb_cg_spmv"
            and "nonzeros" not in invocation.parameters
            for invocation in source.invocations
        ))
        destination_root = Path(self.cg_case.root) / "migrated-cg"
        migrated = indexed.migrate_cg_descriptor(
            source.root, destination_root
        )
        nonzeros = next(
            array.count for array in migrated.arrays
            if array.name == "colidx"
        )
        self.assertTrue(all(
            invocation.parameters["nonzeros"] == nonzeros
            for invocation in migrated.invocations
            if invocation.kernel == "npb_cg_spmv"
        ))
        self.assertEqual(
            migrated.dynamic_work, source.dynamic_work
        )
        for source_array, migrated_array in zip(
            source.arrays, migrated.arrays
        ):
            self.assertEqual(
                os.stat(source.root / source_array.path).st_ino,
                os.stat(migrated.root / migrated_array.path).st_ino,
            )

    def test_safe_partial_spmv_and_zero3_equal_sequential_operations(self):
        self.require_indexer()
        bundle = self.cg_bundle()
        invocation = next(
            row for row in bundle.invocations
            if row.kernel == "npb_cg_spmv"
        )
        with lazy.MappedState(bundle) as expected_state:
            expected = tuple(npb.expand_cg_spmv(
                expected_state, invocation, 4
            ))
        expected = tuple(
            operation for operation in expected
            if operation.work_item in {1, 2}
            and operation.opcode not in {
                canonical.Opcode.BARRIER, canonical.Opcode.COMMIT
            }
        )
        with lazy.MappedState(bundle) as sliced_state:
            actual = tuple(npb.expand_slice(
                sliced_state, invocation, 1, 3, 4
            ))
        self.assertEqual(actual, expected)

        for kernel in ("npb_cg_init", "npb_cg_update_p"):
            invocation = next(
                row for row in bundle.invocations if row.kernel == kernel
            )
            with lazy.MappedState(bundle) as expected_state:
                expected = tuple(npb.EXPANDERS[kernel](
                    expected_state, invocation, 4
                ))
            expected = tuple(
                operation for operation in expected
                if 1 <= operation.work_item < 3
                and operation.opcode not in {
                    canonical.Opcode.BARRIER, canonical.Opcode.COMMIT
                }
            )
            with lazy.MappedState(bundle) as sliced_state:
                actual = tuple(npb.expand_slice(
                    sliced_state, invocation, 1, 3, 4
                ))
            self.assertEqual(actual, expected)

        invocation = next(
            row for row in bundle.invocations
            if row.kernel == "npb_cg_normalize"
        )
        with lazy.MappedState(bundle) as expected_state:
            expected_state.store_scalar("norm1", npb.raw_f64(1.0))
            expected_state.store_scalar("norm2", npb.raw_f64(4.0))
            complete = tuple(npb.expand_cg_normalize(
                expected_state, invocation, 4
            ))
        vector_operations = complete[7:-1]
        expected = tuple(
            operation for operation in vector_operations
            if 1 <= operation.work_item < 3
        )
        with lazy.MappedState(bundle) as sliced_state:
            sliced_state.store_scalar("norm2", npb.raw_f64(4.0))
            actual = tuple(npb.expand_slice(
                sliced_state, invocation, 1, 3, 4
            ))
        self.assertEqual(actual, expected)

        lane_kernels = (
            "npb_cg_dot",
            "npb_cg_update_zr",
            "npb_cg_residual_norm",
            "npb_cg_outer_dots",
        )
        for kernel in lane_kernels:
            invocation = next(
                row for row in bundle.invocations if row.kernel == kernel
            )
            with lazy.MappedState(bundle) as expected_state:
                if kernel == "npb_cg_update_zr":
                    expected_state.store_scalar("alpha", npb.raw_f64(0.5))
                complete = tuple(npb.EXPANDERS[kernel](
                    expected_state, invocation, 4
                ))
            expected = tuple(
                operation for operation in complete
                if operation.work_item == 1
                and operation.opcode not in {
                    canonical.Opcode.BARRIER, canonical.Opcode.COMMIT
                }
            )
            with lazy.MappedState(bundle) as sliced_state:
                if kernel == "npb_cg_update_zr":
                    sliced_state.store_scalar("alpha", npb.raw_f64(0.5))
                actual = tuple(npb.expand_slice(
                    sliced_state, invocation, 1, 2, 4
                ))
            self.assertEqual(actual, expected)

        mg = self.mg_case.full_vcycle_bundle()
        resid = next(
            row for row in mg.invocations if row.kernel == "npb_mg_resid"
        )
        realized = npb.safe_work_item_range(
            resid, 5, 7, stratum_first=4, stratum_stop=8
        )
        self.assertEqual(realized, (4, 8))
        with lazy.MappedState(mg) as expected_state:
            complete = tuple(npb.expand_mg_resid(
                expected_state, resid, 4
            ))
        selected_work_items = {1, 49, 50, 51, 52}
        expected = tuple(
            operation for operation in complete
            if operation.work_item in selected_work_items
            and operation.opcode not in {
                canonical.Opcode.BARRIER, canonical.Opcode.COMMIT
            }
        )
        with lazy.MappedState(mg) as sliced_state:
            actual = tuple(npb.expand_slice(
                sliced_state, resid, *realized, 4
            ))
        self.assertEqual(actual, expected)

        psinv = next(
            row for row in mg.invocations if row.kernel == "npb_mg_psinv"
        )
        realized = npb.safe_work_item_range(
            psinv, 3, 4, stratum_first=2, stratum_stop=4
        )
        self.assertEqual(realized, (2, 4))
        with lazy.MappedState(mg) as expected_state:
            complete = tuple(npb.expand_mg_psinv(
                expected_state, psinv, 4
            ))
        n1 = psinv.parameters["n1"]
        rows = (psinv.parameters["n2"] - 2) * (
            psinv.parameters["n3"] - 2
        )
        interior_prefix = complete[
            :1 + rows * (14 * n1 + 15 * (n1 - 2))
        ]
        expected = tuple(
            operation for operation in interior_prefix
            if operation.work_item in {1, 25, 26}
            and operation.opcode not in {
                canonical.Opcode.BARRIER, canonical.Opcode.COMMIT
            }
        )
        with lazy.MappedState(mg) as sliced_state:
            actual = tuple(npb.expand_slice(
                sliced_state, psinv, *realized, 4
            ))
        self.assertEqual(actual, expected)

        rprj3 = next(
            row for row in mg.invocations if row.kernel == "npb_mg_rprj3"
        )
        realized = npb.safe_work_item_range(
            rprj3, 3, 4, stratum_first=2, stratum_stop=4
        )
        self.assertEqual(realized, (2, 4))
        with lazy.MappedState(mg) as expected_state:
            complete = tuple(npb.expand_mg_rprj3(
                expected_state, rprj3, 4
            ))
        m1 = rprj3.parameters["m1j"]
        rows = (rprj3.parameters["m2j"] - 2) * (
            rprj3.parameters["m3j"] - 2
        )
        interior_prefix = complete[
            :1 + rows * (14 * (m1 - 1) + 30 * (m1 - 2))
        ]
        expected = tuple(
            operation for operation in interior_prefix
            if operation.work_item in {1, 25, 26}
            and operation.opcode not in {
                canonical.Opcode.BARRIER, canonical.Opcode.COMMIT
            }
        )
        with lazy.MappedState(mg) as sliced_state:
            actual = tuple(npb.expand_slice(
                sliced_state, rprj3, *realized, 4
            ))
        self.assertEqual(actual, expected)

        norm = next(
            row for row in mg.invocations if row.kernel == "npb_mg_norm2u3"
        )
        realized = npb.safe_work_item_range(
            norm, 20, 25, stratum_first=16, stratum_stop=32
        )
        self.assertEqual(realized, (16, 32))
        with lazy.MappedState(mg) as expected_state:
            complete = tuple(npb.expand_mg_norm2u3(
                expected_state, norm, 4
            ))
        expected = tuple(
            operation for operation in complete
            if 16 <= operation.work_item < 32
            and operation.opcode not in {
                canonical.Opcode.BARRIER, canonical.Opcode.COMMIT
            }
        )
        with lazy.MappedState(mg) as sliced_state:
            actual = tuple(npb.expand_slice(
                sliced_state, norm, *realized, 4
            ))
        self.assertEqual(actual, expected)

        zero = lazy.Invocation(0, 200, "npb_mg_zero3", 1, 64, {
            "u": "vc_coarse_u", "n1": 4, "n2": 4, "n3": 4,
            "boundaries": [],
        })
        with lazy.MappedState(mg) as expected_state:
            expected = tuple(npb.expand_mg_zero3(expected_state, zero, 4))
        expected = tuple(
            operation for operation in expected
            if 7 <= operation.work_item < 13
            and operation.opcode not in {
                canonical.Opcode.BARRIER, canonical.Opcode.COMMIT
            }
        )
        with lazy.MappedState(mg) as sliced_state:
            actual = tuple(npb.expand_slice(
                sliced_state, zero, 7, 13, 4
            ))
        self.assertEqual(actual, expected)

    def test_full_safe_slice_matches_every_fixture_expander(self):
        self.require_indexer()
        cg = self.cg_bundle()
        with (
            lazy.MappedState(cg) as expected_state,
            lazy.MappedState(cg) as actual_state,
        ):
            for invocation in cg.invocations:
                with self.subTest(kernel=invocation.kernel):
                    expected = tuple(npb.EXPANDERS[invocation.kernel](
                        expected_state, invocation, 4
                    ))
                    actual = tuple(npb.expand_slice(
                        actual_state,
                        invocation,
                        0,
                        invocation.work_items,
                        4,
                    ))
                    self.assertEqual(actual, expected)

        mg = self.mg_case.full_vcycle_bundle()
        with (
            lazy.MappedState(mg) as expected_state,
            lazy.MappedState(mg) as actual_state,
        ):
            for invocation in mg.invocations:
                with self.subTest(kernel=invocation.kernel):
                    expected = tuple(npb.EXPANDERS[invocation.kernel](
                        expected_state, invocation, 4
                    ))
                    actual = tuple(npb.expand_slice(
                        actual_state,
                        invocation,
                        0,
                        invocation.work_items,
                        4,
                    ))
                    self.assertEqual(actual, expected)

    def test_safe_ranges_reject_empty_and_cross_stratum_alignment(self):
        self.require_indexer()
        bundle = self.cg_bundle()
        reduction = next(
            row for row in bundle.invocations
            if row.kernel == "npb_cg_dot"
        )
        with self.assertRaisesRegex(lazy.LazyTraceError, "empty"):
            npb.safe_work_item_range(
                reduction, 1, 1, stratum_first=0, stratum_stop=3
            )
        with self.assertRaisesRegex(lazy.LazyTraceError, "stratum"):
            npb.safe_work_item_range(
                reduction, 0, 2, stratum_first=1, stratum_stop=2
            )
        self.assertEqual(
            npb.safe_work_item_range(
                reduction, 1, 2, stratum_first=0, stratum_stop=3
            ),
            (1, 2),
        )

    def test_dependency_closure_contains_cg_indirection_and_mg_halo(self):
        self.require_indexer()
        bundle = self.cg_bundle()
        spmv = next(
            row for row in bundle.invocations
            if row.kernel == "npb_cg_spmv"
        )
        with self.assertRaisesRegex(lazy.LazyTraceError, "state"):
            npb.dependency_closure(spmv, 1, 3)
        with lazy.MappedState(bundle) as state:
            closure = npb.dependency_closure(spmv, 1, 3, state=state)
        self.assertIn(("rowstr", 1), closure.array_words)
        self.assertIn(("colidx", 2), closure.array_words)
        self.assertTrue(any(
            name == "p" for name, _ in closure.array_words
        ))

        mg = self.mg_case.full_vcycle_bundle()
        resid = next(
            row for row in mg.invocations
            if row.kernel == "npb_mg_resid"
        )
        closure = npb.dependency_closure(resid, 0, 4)
        self.assertTrue(closure.has_complete_halo)
        self.assertTrue(any(name == "vc_fine_u" for name, _ in closure.array_words))
        self.assertTrue(any(name == "vc_fine_v" for name, _ in closure.array_words))

    def test_every_fixture_kernel_has_a_fail_closed_dependency_resolver(self):
        self.require_indexer()
        cg = self.cg_bundle()
        with lazy.MappedState(cg) as state:
            for invocation in cg.invocations:
                with self.subTest(kernel=invocation.kernel):
                    closure = npb.dependency_closure(
                        invocation,
                        0,
                        invocation.work_items,
                        state=state,
                    )
                    self.assertIsInstance(closure, npb.DependencyClosure)

        mg = self.mg_case.full_vcycle_bundle()
        for invocation in mg.invocations:
            with self.subTest(kernel=invocation.kernel):
                closure = npb.dependency_closure(
                    invocation, 0, invocation.work_items
                )
                self.assertIsInstance(closure, npb.DependencyClosure)
        zero = lazy.Invocation(0, 200, "npb_mg_zero3", 1, 64, {
            "u": "vc_coarse_u", "n1": 4, "n2": 4, "n3": 4,
            "boundaries": [],
        })
        self.assertIsInstance(
            npb.dependency_closure(zero, 0, zero.work_items),
            npb.DependencyClosure,
        )


if __name__ == "__main__":
    unittest.main()
