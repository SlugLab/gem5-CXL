# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

import unittest
from decimal import Decimal
from types import SimpleNamespace

from scripts import generate_gapbs_g20_e2e_figure as figure


def valid_rows():
    return [
        SimpleNamespace(
            system="Vanilla CXL",
            latency_seconds=Decimal("20"),
            speedup=Decimal("1"),
        ),
        SimpleNamespace(
            system="AMU",
            latency_seconds=Decimal("30"),
            speedup=Decimal("0.6666666666666667"),
        ),
        SimpleNamespace(
            system="CIRA",
            latency_seconds=Decimal("4"),
            speedup=Decimal("5"),
        ),
        SimpleNamespace(
            system="M2NDP",
            latency_seconds=Decimal("1"),
            speedup=Decimal("20"),
        ),
    ]


def valid_sensitivity():
    values = {
        "200ns": ("0.81", "1.11"),
        "500ns": ("0.88", "1.23"),
        "1us": ("0.95", "1.37"),
        "2us": ("1.02", "1.51"),
    }
    return {
        latency: {
            "Geo.": {
                "AMU": Decimal(amu),
                "CIRA": Decimal(cira),
            }
        }
        for latency, (amu, cira) in values.items()
    }


class FigureDataTest(unittest.TestCase):
    def test_chart_data_preserves_system_order_and_separates_grains(self):
        data = figure.prepare_figure_data(
            valid_rows(),
            valid_sensitivity(),
            evidence_sha256="a" * 64,
        )

        self.assertEqual(
            data.systems,
            ("Vanilla CXL", "AMU", "CIRA", "M2NDP"),
        )
        self.assertEqual(data.latency_ns, (200, 500, 1000, 2000))
        self.assertEqual(tuple(data.sensitivity), ("AMU", "CIRA"))
        self.assertNotIn("M2NDP", data.sensitivity)
        self.assertEqual(
            data.sensitivity["AMU"],
            (0.81, 0.88, 0.95, 1.02),
        )
        self.assertEqual(data.panel_a_scale, "log")

    def test_panel_a_uses_log_only_at_ten_to_one(self):
        self.assertEqual(
            figure.choose_latency_scale((1.0, 9.99)), "linear"
        )
        self.assertEqual(
            figure.choose_latency_scale((1.0, 10.0)), "log"
        )

    def test_rejects_reordered_formal_systems(self):
        rows = valid_rows()
        rows[1], rows[2] = rows[2], rows[1]

        with self.assertRaisesRegex(
            figure.FigureDataError, "absent or out of order"
        ):
            figure.prepare_figure_data(
                rows,
                valid_sensitivity(),
                evidence_sha256="a" * 64,
            )

    def test_rejects_nonpositive_formal_latency(self):
        rows = valid_rows()
        rows[2] = SimpleNamespace(
            system="CIRA",
            latency_seconds=Decimal("0"),
            speedup=Decimal("5"),
        )

        with self.assertRaisesRegex(
            figure.FigureDataError, "finite and positive"
        ):
            figure.prepare_figure_data(
                rows,
                valid_sensitivity(),
                evidence_sha256="a" * 64,
            )

    def test_rejects_missing_geomean(self):
        sensitivity = valid_sensitivity()
        del sensitivity["1us"]["Geo."]

        with self.assertRaisesRegex(
            figure.FigureDataError, "1us/Geo./AMU"
        ):
            figure.prepare_figure_data(
                valid_rows(),
                sensitivity,
                evidence_sha256="a" * 64,
            )

    def test_rejects_unsupported_latency_key(self):
        sensitivity = valid_sensitivity()
        sensitivity["5us"] = sensitivity.pop("2us")

        with self.assertRaisesRegex(
            figure.FigureDataError, "latency keys"
        ):
            figure.prepare_figure_data(
                valid_rows(),
                sensitivity,
                evidence_sha256="a" * 64,
            )

    def test_rejects_nonpositive_sensitivity_value(self):
        sensitivity = valid_sensitivity()
        sensitivity["500ns"]["Geo."]["CIRA"] = Decimal("-1")

        with self.assertRaisesRegex(
            figure.FigureDataError, "500ns/Geo./CIRA"
        ):
            figure.prepare_figure_data(
                valid_rows(),
                sensitivity,
                evidence_sha256="a" * 64,
            )

    def test_rejects_invalid_evidence_digest(self):
        with self.assertRaisesRegex(
            figure.FigureDataError, "evidence SHA-256"
        ):
            figure.prepare_figure_data(
                valid_rows(),
                valid_sensitivity(),
                evidence_sha256="not-a-digest",
            )


if __name__ == "__main__":
    unittest.main()
