# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

import importlib.util
import unittest
from pathlib import Path


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


if __name__ == "__main__":
    unittest.main()
