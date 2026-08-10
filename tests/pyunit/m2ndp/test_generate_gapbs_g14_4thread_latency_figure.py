# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

import unittest

from scripts import generate_gapbs_g14_4thread_latency_figure as figure
try:
    from test_generate_gapbs_g14_4thread_latency_results import make_valid_rows
except ModuleNotFoundError:
    from m2ndp.test_generate_gapbs_g14_4thread_latency_results import make_valid_rows


class FigureTest(unittest.TestCase):
    def test_deterministic_four_point_bit_exact_figure(self):
        rows = make_valid_rows()
        first = figure.render_figure(rows, evidence_sha256="e" * 64)
        second = figure.render_figure(rows, evidence_sha256="e" * 64)
        self.assertEqual(first, second)
        pdf, svg = first
        self.assertTrue(pdf.startswith(b"%PDF"))
        self.assertIn(b"g14", svg)
        self.assertIn(("e" * 64).encode(), pdf)


if __name__ == "__main__":
    unittest.main()
