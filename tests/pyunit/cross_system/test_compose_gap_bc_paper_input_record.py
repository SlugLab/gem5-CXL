# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from scripts import compose_gap_bc_paper_input_record as compose


class ComposeGapBCPaperInputTest(unittest.TestCase):
    def test_replaces_mg_and_optionally_refreshes_mcf(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            graph = root / "g20.sg"
            graph.write_bytes(b"g20")
            trace = root / "trace"
            trace.mkdir()
            (trace / "trace.v2.json").write_text("{}\n", encoding="utf-8")
            formal = root / "formal.json"
            formal.write_text(json.dumps({
                "schema": 1,
                "status": "passed",
                "benchmark": "gap_bc",
                "graph_scale": 20,
                "graph": str(graph.resolve()),
                "trace": str(trace.resolve()),
                "source_vertex": 7,
            }), encoding="utf-8")
            base = root / "base.json"
            base.write_text(json.dumps({
                name: {"old": name}
                for name in (
                    "pr_spmv", "mcf", "amg_gather", "lulesh_scatter",
                    "npb_cg", "npb_mg",
                )
            }), encoding="utf-8")
            candidate = root / "candidate.json"
            candidate.write_text(json.dumps({
                "schema": 1,
                "status": "candidate",
                "workload": "mcf",
                "record": {"current": True},
            }), encoding="utf-8")
            bundle = SimpleNamespace(arrays=(
                SimpleNamespace(count=3, element_type="u64"),
                SimpleNamespace(count=5, element_type="u32"),
            ))
            with (
                mock.patch.object(compose.lazy, "read_bundle", return_value=bundle),
                mock.patch.object(
                    compose.freeze, "validate_paper_record",
                    side_effect=lambda value: value,
                ),
            ):
                value = compose.compose(base, formal, candidate)
        self.assertEqual(set(value), set(compose.freeze.WORKLOADS))
        self.assertNotIn("npb_mg", value)
        self.assertEqual(value["mcf"], {"current": True})
        self.assertEqual(value["gap_bc"]["allocated_bytes"], 44)
        self.assertEqual(value["gap_bc"]["source_vertex"], 7)


if __name__ == "__main__":
    unittest.main()
