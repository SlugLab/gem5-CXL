# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

import hashlib
import importlib.util
import os
import struct
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import build_matched_breadth_workloads as builder
from scripts import lazy_work_trace as lazy
from scripts import npb_indexed_windows as indexed


def _load_fixtures():
    path = Path(__file__).with_name("test_npb_lazy_trace.py")
    spec = importlib.util.spec_from_file_location("npb_sparse_fixtures", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


fixtures = _load_fixtures()


class NpbsparseCaptureTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.plan_path = self.root / "plan.bin"
        self.capture_path = self.root / "capture.bin"
        self.descriptor_sha256 = hashlib.sha256(b"descriptor").hexdigest()

    def plan(self):
        return indexed.SparseCapturePlan(
            descriptor_sha256=self.descriptor_sha256,
            entries=(
                indexed.ArrayRequest(0, 7, 1),
                indexed.ScalarRequest(0, 2),
            ),
        )

    def test_plan_round_trip_and_capture_parser_bind_raw_words(self):
        plan = self.plan()
        indexed.write_sparse_capture_plan(self.plan_path, plan)
        self.assertEqual(indexed.read_sparse_capture_plan(self.plan_path), plan)
        plan_sha256 = hashlib.sha256(self.plan_path.read_bytes()).digest()
        payload = struct.pack(
            "<8sQQ32s32s", b"NPBSPC01", 1, 2,
            bytes.fromhex(self.descriptor_sha256), plan_sha256,
        )
        payload += struct.pack("<QQQQQ", 0, 1, 7, 1, 0x11223344)
        payload += struct.pack(
            "<QQQQQ", 0, 2, 2, 0, 0x400921FB54442D18
        )
        self.capture_path.write_bytes(payload)
        parsed = builder.parse_npb_sparse_capture(
            self.capture_path, plan, plan_path=self.plan_path
        )
        self.assertEqual(parsed.request_count, 2)
        self.assertEqual(parsed.records[0].raw_word, 0x11223344)
        self.assertEqual(parsed.records[1].raw_word, 0x400921FB54442D18)

    def test_plan_and_capture_reject_reordering_and_identity_drift(self):
        with self.assertRaisesRegex(indexed.IndexError, "ordered"):
            indexed.write_sparse_capture_plan(
                self.plan_path,
                indexed.SparseCapturePlan(
                    descriptor_sha256=self.descriptor_sha256,
                    entries=(
                        indexed.ScalarRequest(1, 2),
                        indexed.ArrayRequest(0, 7, 1),
                    ),
                ),
            )
        plan = self.plan()
        indexed.write_sparse_capture_plan(self.plan_path, plan)
        payload = struct.pack(
            "<8sQQ32s32s", b"NPBSPC01", 1, 2,
            bytes.fromhex(self.descriptor_sha256), b"\0" * 32,
        )
        payload += struct.pack("<QQQQQ", 0, 1, 7, 1, 1)
        payload += struct.pack("<QQQQQ", 0, 2, 2, 0, 2)
        self.capture_path.write_bytes(payload)
        with self.assertRaisesRegex(builder.BuildError, "plan SHA-256"):
            builder.parse_npb_sparse_capture(
                self.capture_path, plan, plan_path=self.plan_path
            )

    def test_native_hook_captures_live_array_and_scalar_words_atomically(self):
        plan = self.plan()
        indexed.write_sparse_capture_plan(self.plan_path, plan)
        fixture = self.root / "fixture.cc"
        fixture.write_text(
            r'''
#include "npb_trace_hooks.h"
#include <cstdint>
int main() {
    int64_t array_id = 7, bits = 32, base = 4096, count = 3;
    int32_t values[3] = {10, 20, 30};
    matched_array_image_u32_(&array_id, &bits, &base, values, &count);
    int64_t ordinal = 0, phase = 100, kernel = 1000;
    int64_t iteration = 0, work = 1, parameter_count = 0;
    matched_invocation_(&ordinal, &phase, &kernel, &iteration, &work,
                        nullptr, &parameter_count);
    values[1] = 0x11223344;
    int64_t scalar_id = 2;
    uint64_t scalar = 0x400921FB54442D18ULL;
    matched_sparse_scalar_u64_(&scalar_id, &scalar);
    int64_t actual_phase = 101, actual_iteration = 1, actual_work = 3;
    matched_phase_begin_(&actual_phase, &actual_iteration, &actual_work);
    matched_phase_end_(&actual_phase, &actual_iteration);
    return 0;
}
''',
            encoding="utf-8",
        )
        binary = self.root / "fixture"
        source_root = Path("util/amu/matched_workloads").resolve()
        subprocess.run(
            [
                "g++", "-std=c++17", "-Wall", "-Wextra", "-Werror",
                "-I", str(source_root), str(fixture),
                str(source_root / "npb_trace_hooks.cc"),
                "-lcrypto", "-pthread", "-o", str(binary),
            ],
            check=True,
        )
        environment = dict(os.environ)
        environment.update({
            "MATCHED_NPB_SPARSE_PLAN_FILE": str(self.plan_path),
            "MATCHED_NPB_SPARSE_CAPTURE_FILE": str(self.capture_path),
        })
        subprocess.run([str(binary)], check=True, env=environment)
        self.assertTrue(self.capture_path.is_file())
        self.assertFalse(self.capture_path.with_suffix(".bin.tmp").exists())
        parsed = builder.parse_npb_sparse_capture(
            self.capture_path, plan, plan_path=self.plan_path
        )
        self.assertEqual(
            tuple(record.raw_word for record in parsed.records),
            (0x11223344, 0x400921FB54442D18),
        )

    def test_cg_spmv_plan_requests_exact_slice_inputs(self):
        case = fixtures.NpbCgLazyTraceTest("runTest")
        case.setUp()
        self.addCleanup(case.doCleanups)
        bundle = case.full_cg_bundle()
        invocation = next(
            row for row in bundle.invocations
            if row.kernel == "npb_cg_spmv"
        )
        plan = indexed.cg_spmv_sparse_plan(
            bundle, invocation.ordinal, row_first=1, row_stop=3,
            array_ids={"rowstr": 1, "colidx": 2, "a": 3, "p": 4, "q": 5},
        )
        keys = [indexed._request_key(row) for row in plan.entries]
        self.assertEqual(keys, sorted(set(keys)))
        with lazy.MappedState(bundle) as state:
            rowstr = invocation.parameters["rowstr"]
            colidx = invocation.parameters["colidx"]
            edge_first = state.load_raw(rowstr, 1)[1]
            edge_stop = state.load_raw(rowstr, 3)[1]
            columns = {
                state.load_raw(colidx, edge)[1]
                for edge in range(edge_first, edge_stop)
            }
        requested = {
            (row.array_id, row.index)
            for row in plan.entries if isinstance(row, indexed.ArrayRequest)
        }
        self.assertTrue({(1, index) for index in range(1, 4)} <= requested)
        self.assertTrue({(2, edge) for edge in range(edge_first, edge_stop)} <= requested)
        self.assertTrue({(3, edge) for edge in range(edge_first, edge_stop)} <= requested)
        self.assertTrue({(4, column) for column in columns} <= requested)
        self.assertTrue({(5, 1), (5, 2)} <= requested)

    def test_materializes_cg_spmv_without_expanding_the_prefix(self):
        case = fixtures.NpbCgLazyTraceTest("runTest")
        case.setUp()
        self.addCleanup(case.doCleanups)
        bundle = case.full_cg_bundle()
        invocation = next(
            row for row in bundle.invocations
            if row.kernel == "npb_cg_spmv"
        )
        array_ids = {
            "rowstr": 1, "colidx": 2, "a": 3, "p": 4, "q": 5,
        }
        plan = indexed.cg_spmv_sparse_plan(
            bundle, invocation.ordinal, row_first=1, row_stop=3,
            array_ids=array_ids,
        )
        indexed.write_sparse_capture_plan(self.plan_path, plan)
        names = {
            1: invocation.parameters["rowstr"],
            2: invocation.parameters["colidx"],
            3: invocation.parameters["values"],
            4: invocation.parameters["source"],
            5: invocation.parameters["destination"],
        }
        rows = []
        with lazy.MappedState(bundle) as state:
            for request in plan.entries:
                rows.append((*indexed._request_key(request),
                             state.load_raw(names[request.array_id], request.index)[1]))
        payload = struct.pack(
            "<8sQQ32s32s", b"NPBSPC01", 1, len(rows),
            bytes.fromhex(plan.descriptor_sha256),
            hashlib.sha256(self.plan_path.read_bytes()).digest(),
        ) + b"".join(struct.pack("<QQQQQ", *row) for row in rows)
        self.capture_path.write_bytes(payload)
        capture = builder.parse_npb_sparse_capture(
            self.capture_path, plan, plan_path=self.plan_path
        )
        outdir = self.root / "window"
        with mock.patch.object(
            indexed.npb, "expanded_evidence",
            side_effect=AssertionError("full prefix expansion forbidden"),
        ):
            result = indexed.materialize_cg_spmv_window(
                bundle, plan, capture, ordinal=invocation.ordinal,
                row_first=1, measure_start=2, row_stop=3,
                array_ids=array_ids, outdir=outdir,
                plan_path=self.plan_path, capture_path=self.capture_path,
                batch_work_items=2,
            )
        self.assertEqual(result["fixed_records"], 2)
        self.assertGreater(result["dynamic_records"], 0)
        self.assertLess(result["retained_bytes"], 512 * 1024 * 1024)


if __name__ == "__main__":
    unittest.main()
