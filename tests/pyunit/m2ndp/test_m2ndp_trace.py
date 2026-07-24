# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

import struct
import tempfile
import unittest
from pathlib import Path

from scripts import m2ndp_artifacts as artifacts
from scripts import m2ndp_pagerank_trace as trace

from test_m2ndp_build import write_meta, write_words


class PageRankTraceTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        bundle_root = self.root / "bundle"
        bundle_root.mkdir()
        write_words(bundle_root / "in_offsets.u64", "<Q", [0, 1, 3, 4])
        write_words(
            bundle_root / "in_neighbors.i32", "<i", [2, 0, 2, 1]
        )
        write_words(bundle_root / "out_degree.u32", "<I", [1, 1, 2])
        write_meta(bundle_root)
        self.bundle = artifacts.load_graph_bundle(bundle_root)
        self.reference_words = [0x3EAAAAAB, 0x3E800000, 0x3E99999A]
        reference_path = self.root / "reference.m2pr"
        artifacts.write_reference(
            reference_path,
            {
                "schema": artifacts.REFERENCE_SCHEMA,
                "graph_sha256": artifacts.EXPECTED_G20_SHA256,
                "num_nodes": 3,
                "iterations": 20,
                "measured_trial": 1,
                "binary_sha256": "a" * 64,
                "source_sha256": "b" * 64,
            },
            self.reference_words,
        )
        self.reference = artifacts.read_reference(reference_path)

    def tearDown(self):
        self.temporary.cleanup()

    def generate(self):
        return trace.generate_trace(
            bundle=self.bundle,
            reference=self.reference,
            outdir=self.root / "trace",
            trials=2,
            iterations=20,
        )

    def test_kernel_contract_and_launch_counts(self):
        result = self.generate()
        self.assertEqual(
            result.unique_kernels,
            ("K0_INIT", "K1_META", "K2_CONTRIB", "K3_PULL_DAMP"),
        )
        self.assertEqual(result.funcsim_launches, 42)
        self.assertEqual(result.ndpsim_launches, 84)
        self.assertEqual(result.measure_marker, "K0_INIT_TRIAL1")
        names = (
            self.root / "trace/0/kernelslist.g"
        ).read_text().splitlines()
        self.assertEqual(len(names), 84)
        self.assertEqual(names[42], "K0_INIT_TRIAL1")

    def test_strict_kernel_forbids_reduction_and_fma(self):
        self.generate()
        text = (
            self.root / "trace/0/K3_PULL_DAMP.traceg"
        ).read_text()
        self.assertIn("fadd f0, f0, f1", text)
        self.assertIn("fmul f0, f2, f0", text)
        self.assertNotIn("vfred", text)
        self.assertNotIn("vfmacc", text)
        self.assertNotIn("fmadd", text)

    def test_memory_map_round_trips_all_float_words(self):
        self.generate()
        parsed = trace.parse_float32_map(
            self.root / "trace/0/K3_PULL_DAMP_output.data",
            trace.SCORES_ADDR,
            self.bundle.meta.num_nodes,
        )
        self.assertEqual(parsed, self.reference_words)

    def test_constants_record_exact_float32_steps(self):
        self.generate()
        meta = trace.read_trace_meta(self.root / "trace/trace.meta.json")
        self.assertEqual(meta["damping_bits"], "0x3f59999a")
        expected_init = trace.float32_bits(
            trace.f32_div(trace.f32(1.0), trace.f32(3))
        )
        expected_base = trace.float32_bits(
            trace.f32_div(
                trace.f32_sub(trace.f32(1.0), trace.f32(0.85)),
                trace.f32(3),
            )
        )
        self.assertEqual(meta["init_score_bits"], f"0x{expected_init:08x}")
        self.assertEqual(meta["base_score_bits"], f"0x{expected_base:08x}")

    def test_launch_records_have_four_header_fields_and_declared_args(self):
        self.generate()
        launch = (
            self.root / "trace/0/K3_PULL_DAMP_launch.txt"
        ).read_text().strip()
        fields = launch.split()
        self.assertEqual(fields[:2], ["0", "3"])
        self.assertEqual(fields[6:12], [
            f"0x{trace.IN_OFFSETS_ADDR:x}",
            f"0x{trace.IN_NEIGHBORS_ADDR:x}",
            f"0x{trace.CONTRIB_ADDR:x}",
            f"0x{trace.SCORES_ADDR:x}",
            "0x3",
            "FP32",
        ])
        self.assertEqual(len(fields), 14)


if __name__ == "__main__":
    unittest.main()
