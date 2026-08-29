# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

import importlib.util
import hashlib
import tempfile
import unittest
from pathlib import Path

from scripts import gap_bc_lazy_trace as bc
from scripts import stratified_timing as timing


SCRIPT = Path(__file__).resolve().parents[3] / "scripts/prepare_gap_bc_formal_trace.py"
SPEC = importlib.util.spec_from_file_location("prepare_gap_bc", SCRIPT)
prepare = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(prepare)


class PrepareGapBCFormalTraceTest(unittest.TestCase):
    def test_native_log_requires_one_source_and_verification_pass(self):
        source = prepare.parse_native_verification(
            "Source:               756607\nVerification:           PASS\n"
        )
        self.assertEqual(source, 756607)
        with self.assertRaisesRegex(prepare.PrepareError, "verification"):
            prepare.parse_native_verification(
                "Source: 756607\nVerification: FAILED\n"
            )
        with self.assertRaisesRegex(prepare.PrepareError, "source"):
            prepare.parse_native_verification(
                "Source: 1\nSource: 2\nVerification: PASS\n"
            )

    def test_config_identity_binds_source_threads_and_iteration(self):
        left = prepare.config_identity(756607, threads=4, iterations=1)
        same = prepare.config_identity(756607, threads=4, iterations=1)
        other = prepare.config_identity(756608, threads=4, iterations=1)
        self.assertEqual(left, same)
        self.assertNotEqual(left, other)
        self.assertEqual(len(left), 64)

    def test_sampling_plans_cover_complete_bfs_and_reverse_vertex_sets(self):
        digest = lambda value: hashlib.sha256(value.encode()).hexdigest()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bundle = bc.build_bundle(
                root / "trace", offsets=(0, 2, 3, 4, 4),
                neighbors=(1, 2, 3, 3), source=0,
                source_sha256=digest("source"),
                binary_sha256=digest("binary"),
                config_sha256=digest("config"), compact=True,
            )
            descriptor_sha256 = hashlib.sha256(
                (bundle.root / "trace.v2.json").read_bytes()
            ).hexdigest()
            records = prepare.write_sampling_plans(
                bundle, root / "windows", descriptor_sha256
            )
            bfs = timing.read_plan(records["bc_bfs"]["path"])
            reverse = timing.read_plan(records["bc_reverse"]["path"])
        self.assertEqual(bfs.work_items, 4)
        self.assertEqual(reverse.work_items, 4)
        self.assertEqual(bfs.trace_sha256, descriptor_sha256)
        self.assertEqual(reverse.trace_sha256, descriptor_sha256)


if __name__ == "__main__":
    unittest.main()
