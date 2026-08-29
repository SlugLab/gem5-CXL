# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

import hashlib
import struct
import tempfile
import unittest
from pathlib import Path

from scripts import canonical_work_trace as canonical
from scripts import native_verified_window_trace as windows
from scripts import run_matched_breadth_gem5 as replay


def digest(label):
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


class NativeVerifiedWindowTraceTest(unittest.TestCase):
    def test_spatter_gather_materializes_only_selected_real_items(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            values = root / "values.f32le"
            index = root / "index.u64le"
            values.write_bytes(struct.pack("<6I", *(
                0x3F800000, 0x40000000, 0x40400000,
                0x40800000, 0x40A00000, 0x40C00000,
            )))
            index.write_bytes(struct.pack("<6Q", 5, 4, 3, 2, 1, 0))
            materialized = windows.materialize_spatter_window(
                kind="gather",
                values_path=values,
                index_path=index,
                source_trace_sha256=digest("formal-spatter"),
                input_sha256=digest("formal-input"),
                warmup_start=1,
                measure_start=3,
                measure_stop=5,
                outdir=root / "window",
            )
            dynamic = canonical.read_bundle(materialized.root)
            fixed = canonical.read_bundle(materialized.fixed_root)
            replay.load_prepared_window_trace(
                materialized.root, materialized.fixed_root
            )
            binary = replay.build_replay_binary(
                root / "native-build", native=True
            )
            dynamic_result = replay.run_native_replay(
                binary, system="vanilla", trace=materialized.root,
                outdir=root / "native-dynamic",
            )
            amu_result = replay.run_native_replay(
                binary, system="amu", trace=materialized.root,
                outdir=root / "native-dynamic-amu",
            )
            fixed_result = replay.run_native_replay(
                binary, system="vanilla", trace=materialized.fixed_root,
                outdir=root / "native-fixed",
            )
        self.assertEqual(materialized.warmup_items, 2)
        self.assertEqual(materialized.measured_items, 2)
        self.assertEqual(len(dynamic.operations), 12)
        self.assertEqual(
            [row.work_item for row in dynamic.operations[::3]],
            [0, 1, 2, 3],
        )
        self.assertEqual(
            [row.operand0 for row in dynamic.operations[1::3]],
            [0x40A00000, 0x40800000, 0x40400000, 0x40000000],
        )
        self.assertEqual(
            [row.operand1 for row in dynamic.operations[1::3]],
            [1, 4, 7, 10],
        )
        self.assertEqual(
            [row.opcode for row in fixed.operations],
            [canonical.Opcode.BARRIER, canonical.Opcode.COMMIT],
        )
        self.assertEqual(dynamic_result["verification"], "pass")
        self.assertEqual(amu_result["verification"], "pass")
        self.assertGreater(amu_result["max_observed_outstanding"], 1)
        self.assertEqual(fixed_result["verification"], "pass")

    def test_spatter_window_rejects_out_of_range_coordinate(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            values = root / "values.f32le"
            index = root / "index.u64le"
            values.write_bytes(struct.pack("<2I", 1, 2))
            index.write_bytes(struct.pack("<2Q", 0, 1))
            with self.assertRaisesRegex(
                windows.WindowTraceError, "coordinate"
            ):
                windows.materialize_spatter_window(
                    kind="scatter",
                    values_path=values,
                    index_path=index,
                    source_trace_sha256=digest("formal-spatter"),
                    input_sha256=digest("formal-input"),
                    warmup_start=0,
                    measure_start=1,
                    measure_stop=3,
                    outdir=root / "window",
                )


if __name__ == "__main__":
    unittest.main()
