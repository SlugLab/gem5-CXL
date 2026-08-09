# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

import tempfile
import unittest
from pathlib import Path

from scripts import generate_gapbs_g4_4thread_latency_figure as figure
from test_generate_gapbs_g4_4thread_latency_results import make_valid_rows


class FigureDataTest(unittest.TestCase):
    def test_figure_has_four_latency_points_per_mechanism(self):
        data = figure.prepare_figure_data(
            make_valid_rows(), evidence_sha256="a" * 64
        )
        self.assertEqual(data.latency_ns, (200, 500, 1000, 2000))
        self.assertEqual(set(data.series), {"AMU", "CIRA", "M2NDP"})
        self.assertTrue(
            all(len(values) == 4 for values in data.series.values())
        )

    def test_vanilla_is_explicit_one_x_reference(self):
        data = figure.prepare_figure_data(
            make_valid_rows(), evidence_sha256="a" * 64
        )
        self.assertEqual(
            data.vanilla_reference, (1.0, 1.0, 1.0, 1.0)
        )

    def test_evidence_digest_must_be_sha256(self):
        with self.assertRaisesRegex(figure.FigureDataError, "SHA-256"):
            figure.prepare_figure_data(
                make_valid_rows(), evidence_sha256="invalid"
            )


class FigureRenderingTest(unittest.TestCase):
    def test_render_is_deterministic_and_embeds_evidence(self):
        rows = make_valid_rows()
        digest = "c" * 64

        first_pdf, first_svg = figure.render_figure(
            rows, evidence_sha256=digest
        )
        second_pdf, second_svg = figure.render_figure(
            rows, evidence_sha256=digest
        )

        self.assertEqual(first_pdf, second_pdf)
        self.assertEqual(first_svg, second_svg)
        self.assertTrue(first_pdf.startswith(b"%PDF"))
        self.assertIn(digest.encode(), first_pdf)
        self.assertIn(digest.encode(), first_svg)

    def test_atomic_write_emits_both_vector_formats(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = figure.write_figure(
                make_valid_rows(),
                evidence_sha256="d" * 64,
                outdir=Path(tmp),
            )

            self.assertGreater(paths.pdf.stat().st_size, 1000)
            self.assertGreater(paths.svg.stat().st_size, 1000)


if __name__ == "__main__":
    unittest.main()
