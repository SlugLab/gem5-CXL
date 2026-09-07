#!/usr/bin/env python3

import csv
import tempfile
import unittest
from pathlib import Path

from scripts import generate_g12_m2ndp_latency_spectrum as figure


class GenerateG12M2ndpLatencySpectrumTest(unittest.TestCase):
    def _write_rows(self, path):
        fields = (
            "workload", "latency", "system", "status",
            "measurement_kind", "graph_sha256", "time_ms",
        )
        with path.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            for workload in figure.WORKLOADS:
                for index, latency in enumerate(figure.LATENCIES):
                    writer.writerow({
                        "workload": workload,
                        "latency": latency,
                        "system": "m2ndp",
                        "status": "pass",
                        "measurement_kind": "measured",
                        "graph_sha256": figure.G12_GRAPH_SHA256,
                        "time_ms": str(1 + index / 10),
                    })

    def test_load_requires_eight_accepted_measured_g12_rows(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "raw.csv"
            self._write_rows(source)
            rows = figure.load_rows(source)
            self.assertEqual(len(rows), 8)
            self.assertEqual(rows[("pr_spmv", "200ns")], 1.0)

    def test_render_writes_all_paper_formats(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "raw.csv"
            self._write_rows(source)
            output = root / "figure"
            figure.render(figure.load_rows(source), output)
            for suffix in (".pdf", ".svg", ".png"):
                self.assertGreater(output.with_suffix(suffix).stat().st_size, 0)


if __name__ == "__main__":
    unittest.main()
