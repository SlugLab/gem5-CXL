# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

import struct
import tempfile
import unittest
from types import SimpleNamespace
from pathlib import Path

from scripts import m2ndp_artifacts as artifacts
from scripts import m2ndp_pagerank_trace as trace

try:
    from test_m2ndp_build import write_meta, write_words
except ModuleNotFoundError:
    from m2ndp.test_m2ndp_build import write_meta, write_words


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
        path = self.root / "trace/0/K3_PULL_DAMP_output.data"
        parsed = trace.parse_float32_map(
            path,
            trace.SCORES_ADDR,
            self.bundle.meta.num_nodes,
        )
        self.assertEqual(parsed, self.reference_words)
        before, after = trace.flip_float32_bit_for_test(
            path, trace.SCORES_ADDR, index=0, bit=0
        )
        self.assertEqual(before, self.reference_words[0])
        self.assertEqual(after, self.reference_words[0] ^ 1)
        parsed = trace.parse_float32_map(
            path,
            trace.SCORES_ADDR,
            self.bundle.meta.num_nodes,
        )
        self.assertEqual(parsed[0], self.reference_words[0] ^ 1)

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
        self.assertEqual(fields[:2], ["1", "3"])
        self.assertEqual(fields[6:12], [
            f"0x{trace.IN_OFFSETS_ADDR:x}",
            f"0x{trace.IN_NEIGHBORS_ADDR:x}",
            f"0x{trace.CONTRIB_ADDR:x}",
            f"0x{trace.SCORES_ADDR:x}",
            "0x3",
            "FP32",
        ])
        self.assertEqual(len(fields), 14)

    def test_trace_manifest_binds_profile_latency_and_vanilla_raw(self):
        profile = SimpleNamespace(
            name="g14-4thread-sweep",
            graph_sha256=self.bundle.meta.graph_sha256,
            num_nodes=self.bundle.meta.num_nodes,
            page_rank_iterations=20,
            trials=2,
            measured_trial=1,
        )
        result = trace.generate_trace(
            bundle=self.bundle,
            reference=self.reference,
            outdir=self.root / "bound-trace",
            trials=2,
            iterations=20,
            profile=profile,
            profile_manifest_sha256="c" * 64,
            cxl_link_delay="2us",
            vanilla_raw_sha256="d" * 64,
        )
        meta = trace.read_trace_meta(result.meta_path)
        self.assertEqual(meta["profile"], "g14-4thread-sweep")
        self.assertEqual(meta["profile_manifest_sha256"], "c" * 64)
        self.assertEqual(meta["cxl_link_delay"], "2us")
        self.assertEqual(meta["vanilla_raw_sha256"], "d" * 64)
        self.assertEqual(meta["measured_trial"], 1)
        self.assertEqual(meta["stage_sequence"], list(trace.UNIQUE_KERNELS))
        self.assertEqual(set(meta["kernel_sha256"]), set(trace.UNIQUE_KERNELS))

    def test_frozen_g14_trace_contract_rejects_cross_latency_reuse(self):
        profile = SimpleNamespace(
            name="g14-4thread-sweep",
            graph_sha256="a" * 64,
            num_nodes=1 << 14,
            page_rank_iterations=20,
            trials=2,
            measured_trial=1,
        )
        meta = {
            "profile": profile.name,
            "profile_manifest_sha256": "b" * 64,
            "cxl_link_delay": "500ns",
            "vanilla_raw_sha256": "c" * 64,
            "graph_sha256": profile.graph_sha256,
            "num_nodes": 1 << 14,
            "num_directed_edges": 12345,
            "trials": 2,
            "measured_trial": 1,
            "iterations": 20,
            "stage_sequence": list(trace.UNIQUE_KERNELS),
            "measure_marker": "K0_INIT_TRIAL1",
            "kernel_sha256": {
                name: format(index + 1, "064x")
                for index, name in enumerate(trace.UNIQUE_KERNELS)
            },
        }
        trace.validate_trace_binding(
            meta,
            profile=profile,
            profile_manifest_sha256="b" * 64,
            cxl_link_delay="500ns",
            vanilla_raw_sha256="c" * 64,
            directed_edges=12345,
        )
        for field, value in (
            ("profile", "g12-4thread-qualification"),
            ("cxl_link_delay", "1us"),
            ("vanilla_raw_sha256", "d" * 64),
            ("graph_sha256", "e" * 64),
        ):
            with self.subTest(field=field), self.assertRaises(
                artifacts.EvidenceError
            ):
                candidate = dict(meta, **{field: value})
                trace.validate_trace_binding(
                    candidate,
                    profile=profile,
                    profile_manifest_sha256="b" * 64,
                    cxl_link_delay="500ns",
                    vanilla_raw_sha256="c" * 64,
                    directed_edges=12345,
                )

    def test_formal_trace_is_four_way_double_buffered_and_roi_starts_at_k2(self):
        profile = SimpleNamespace(
            name="pr-offload-4thread-1us",
            graph_scale=12,
            graph_sha256=self.bundle.meta.graph_sha256,
            num_nodes=self.bundle.meta.num_nodes,
            cores=4,
            threads=4,
            logical_partitions=4,
            latencies=("1us",),
            page_rank_iterations=20,
            trials=2,
            measured_trial=1,
        )
        result = trace.generate_trace(
            bundle=self.bundle,
            reference=self.reference,
            outdir=self.root / "formal-trace",
            trials=2,
            iterations=20,
            profile=profile,
            profile_manifest_sha256="c" * 64,
            cxl_link_delay="1us",
            vanilla_raw_sha256="d" * 64,
        )
        meta = trace.read_trace_meta(result.meta_path)
        self.assertEqual(meta["logical_partitions"], 4)
        self.assertEqual(meta["partition_bounds"], [[0, 1], [1, 2], [2, 3], [3, 3]])
        self.assertTrue(meta["double_buffered"])
        self.assertEqual(result.measure_marker, "K2_CONTRIB_TRIAL1_GROUP")
        self.assertEqual(meta["measure_marker"], result.measure_marker)
        self.assertEqual(result.funcsim_launches, 165)
        self.assertEqual(result.ndpsim_launches, 84)
        self.assertEqual(meta["timing_commands_per_trial"], 42)
        self.assertEqual(meta["timing_launch_records_per_trial"], 165)

        names = (self.root / "formal-trace/0/kernelslist.g").read_text().splitlines()
        self.assertEqual(len(names), 84)
        self.assertEqual(names[42], "K0_INIT_TRIAL1_GROUP")
        self.assertEqual(names[44], "K2_CONTRIB_TRIAL1_GROUP")

        launch_lines = (
            self.root
            / "formal-trace/0/K2_CONTRIB_TRIAL1_GROUP_launch.txt"
        ).read_text().splitlines()
        self.assertEqual(len(launch_lines), 4)
        self.assertEqual(
            [line.split()[9:11] for line in launch_lines],
            [
                ["0x0", "0x1"],
                ["0x1", "0x1"],
                ["0x2", "0x1"],
                ["0x3", "0x0"],
            ],
        )

        first_k2 = (self.root / "formal-trace/0/K2_CONTRIB_TRIAL1_PART0_launch.txt").read_text().split()
        final_k3 = (self.root / "formal-trace/0/K3_PULL_DAMP_TRIAL1_ITER19_PART3_launch.txt").read_text().split()
        self.assertEqual(first_k2[6], f"0x{trace.SCORES_A_ADDR:x}")
        self.assertEqual(final_k3[9], f"0x{trace.SCORES_A_ADDR:x}")
        self.assertEqual(first_k2[9:11], ["0x0", "0x1"])
        self.assertEqual(final_k3[10:12], ["0x3", "0x0"])

        trace_dir = self.root / "formal-trace/0"
        self.assertEqual(
            (trace_dir / "K0_INIT_TRIAL0_GROUP_input.data").read_bytes(),
            (trace_dir / "K0_INIT_input.data").read_bytes(),
        )
        self.assertEqual(
            (
                trace_dir
                / "K3_PULL_DAMP_TRIAL1_ITER19_GROUP_output.data"
            ).read_bytes(),
            (trace_dir / "K3_PULL_DAMP_output.data").read_bytes(),
        )


if __name__ == "__main__":
    unittest.main()
