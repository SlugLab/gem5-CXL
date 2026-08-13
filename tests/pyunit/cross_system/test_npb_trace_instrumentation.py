# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

import shutil
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from scripts import build_matched_breadth_workloads as builder
from scripts import lazy_work_trace as lazy


NPB_ROOT = Path(
    "/home/victoryang00/CXLMemUring/bench/npb/NPB3.4/NPB3.4-OMP"
)


class NpbTraceInstrumentationTest(unittest.TestCase):
    def setUp(self):
        if not NPB_ROOT.is_dir():
            self.skipTest("pinned NPB3.4-OMP source is unavailable")
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_cg_patch_applies_zero_fuzz_and_preserves_arithmetic(self):
        original = (NPB_ROOT / "CG/cg.f").read_text(encoding="utf-8")
        patched = builder.apply_npb_patch(NPB_ROOT, "cg", self.root / "cg")
        patched_text = (patched / "CG/cg.f").read_text(encoding="utf-8")

        self.assertEqual(builder._transform_cg(original), patched_text)

        self.assertEqual(
            builder.arithmetic_fingerprint(original),
            builder.arithmetic_fingerprint(patched_text),
        )
        evidence = builder.inspect_npb_patch(patched_text, "cg")
        self.assertEqual(evidence["threads"], 4)
        self.assertTrue(evidence["runtime_thread_guard"])
        self.assertTrue(evidence["fixed_reduction_tree"])
        self.assertEqual(evidence["phases"], {
            "cg_spmv", "cg_vector_update", "cg_dot", "cg_conj_grad",
        })
        self.assertNotIn("reduction(+:rho)", patched_text)
        self.assertNotIn("reduction(+:d)", patched_text)
        self.assertNotIn("reduction(+:sum)", patched_text)
        self.assertNotIn("matched_trace_load_", patched_text)
        self.assertNotIn("matched_trace_store_", patched_text)
        self.assertNotIn("matched_trace_binary_", patched_text)
        self.assertNotIn("matched_dump_f64", patched_text)
        self.assertIn("call matched_array_image_u32", patched_text)
        self.assertIn("call matched_array_image_f64", patched_text)
        self.assertIn("call matched_invocation", patched_text)
        self.assertIn("call matched_boundary_sha256", patched_text)

    def test_mg_patch_applies_zero_fuzz_and_preserves_grid_arithmetic(self):
        original = (NPB_ROOT / "MG/mg.f").read_text(encoding="utf-8")
        patched = builder.apply_npb_patch(NPB_ROOT, "mg", self.root / "mg")
        patched_text = (patched / "MG/mg.f").read_text(encoding="utf-8")

        self.assertEqual(builder._transform_mg(original), patched_text)

        self.assertEqual(
            builder.arithmetic_fingerprint(original),
            builder.arithmetic_fingerprint(patched_text),
        )
        evidence = builder.inspect_npb_patch(patched_text, "mg")
        self.assertEqual(evidence["threads"], 4)
        self.assertTrue(evidence["runtime_thread_guard"])
        self.assertTrue(evidence["fixed_reduction_tree"])
        self.assertEqual(evidence["phases"], {
            "mg_resid", "mg_rprj3", "mg_interp", "mg_psinv", "mg_norm2u3",
        })
        self.assertNotIn("reduction(+:s)", patched_text)
        self.assertNotIn("reduction(max:rnmu)", patched_text)
        self.assertNotIn("matched_trace_load_", patched_text)
        self.assertNotIn("matched_trace_store_", patched_text)
        self.assertNotIn("matched_trace_binary_", patched_text)
        self.assertNotIn("matched_dump_f64", patched_text)
        self.assertIn("call matched_array_image_f64", patched_text)
        self.assertIn("call matched_invocation", patched_text)
        self.assertIn("call matched_boundary_sha256", patched_text)

    def test_hooks_expose_bounded_descriptor_capture_and_no_primitive_api(self):
        header = builder.NPB_TRACE_HOOKS.read_text(encoding="utf-8")
        implementation = builder.NPB_TRACE_IMPLEMENTATION.read_text(
            encoding="utf-8"
        )
        self.assertIn("matched_array_image_", header)
        self.assertIn("matched_invocation_", header)
        self.assertIn("matched_boundary_sha256_", header)
        self.assertIn("matched_reduce_sum4_", header)
        self.assertIn("matched_reduce_max4_", header)
        self.assertIn("matched_allocation_probe_", header)
        self.assertNotIn("matched_trace_load_", header)
        self.assertNotIn("matched_trace_store_", header)
        self.assertNotIn("matched_trace_binary_", header)
        self.assertNotIn("pendingRecords", implementation)
        self.assertIn("lanes[0] + lanes[1]", implementation)
        self.assertIn("lanes[2] + lanes[3]", implementation)
        self.assertIn("Sha256", implementation)
        self.assertNotIn("tolerance", implementation.lower())

    def test_fixture_official_verifiers_and_raw_boundaries_pass(self):
        result = builder.build_and_run_npb_fixture(
            NPB_ROOT, self.root / "fixture", workloads=("cg", "mg"),
            expand=False,
        )
        self.assertEqual(result["threads"], 4)
        for workload in ("cg", "mg"):
            row = result["workloads"][workload]
            self.assertEqual(row["class"], "S")
            self.assertEqual(row["official_verification"], "pass")
            self.assertEqual(row["raw_verification"], "pass")
            self.assertEqual(row["runtime_threads"], 4)
            self.assertGreater(row["measured_allocated_bytes"], 0)
            self.assertRegex(
                row["boundary_map_sha256"], r"^[0-9a-f]{64}$"
            )
            self.assertRegex(row["parameter_sha256"], r"^[0-9a-f]{64}$")
            self.assertGreater(row["boundary_count"], 0)
            self.assertRegex(row["descriptor_sha256"], r"^[0-9a-f]{64}$")
            self.assertEqual(row["expanded_sha256"], "pending")
            self.assertGreater(row["expanded_records"], 0)
            descriptor = Path(row["descriptor_file"])
            bundle = lazy.read_bundle(descriptor.parent)
            capture = builder._parse_npb_capture(Path(row["capture_file"]))
            crosswalk, expectations = builder._npb_boundary_expectations(
                capture, workload,
            )
            self.assertEqual(len(crosswalk), row["boundary_count"])
            self.assertTrue(expectations)
            if workload == "cg":
                self.assertIn("r.update_zr.iter101", expectations)
                self.assertIn("r.spmv.iter126", expectations)
                self.assertIn(
                    "scalar.rnorm.residual_norm.iter126", expectations,
                )
            else:
                self.assertIn("u.l1.psinv.iter501", expectations)
                self.assertIn("u.l2.interp.iter402", expectations)
                self.assertIn("scalar.rnm2.norm2u3.iter2", expectations)
            self.assertEqual(bundle.meta["workload"], f"npb_{workload}")
            self.assertEqual(
                bundle.dynamic_work["primitive_records"],
                row["expanded_records"],
            )
            image_bytes = sum(
                (descriptor.parent / array.path).stat().st_size
                for array in bundle.arrays
            )
            self.assertLess(
                Path(row["capture_file"]).stat().st_size,
                image_bytes + 2 * 1024 * 1024,
            )
            self.assertFalse(list(descriptor.parent.glob("*.trace.bin")))
        self.assertGreaterEqual(
            result["workloads"]["cg"]["boundary_count"], 25 * 5
        )
        self.assertTrue(
            {110, 111, 112, 113, 114, 115, 116}.issubset(
                result["workloads"]["cg"]["boundary_ids"]
            )
        )
        self.assertEqual(
            {invocation.phase for invocation in bundle.invocations},
            {201, 202, 203, 204, 205},
        )
        self.assertTrue(
            {201, 202, 203, 204, 205, 206}.issubset(
                result["workloads"]["mg"]["boundary_ids"]
            )
        )

    def test_one_flipped_residual_bit_fails_raw_verification(self):
        result = builder.build_and_run_npb_fixture(
            NPB_ROOT, self.root / "fixture", workloads=("cg",), expand=False
        )
        reference = Path(result["workloads"]["cg"]["capture_file"])
        actual = self.root / "flipped.bin"
        payload = bytearray(reference.read_bytes())
        payload[72] ^= 1
        actual.write_bytes(payload)

        with self.assertRaisesRegex(builder.BuildError, "array SHA-256"):
            builder._parse_npb_capture(actual)

    def test_formal_build_rejects_dirty_source_or_wrong_parameters(self):
        with self.assertRaisesRegex(builder.BuildError, "source tree is dirty"):
            builder.validate_npb_formal_source(
                NPB_ROOT,
                expected_commit="35cd0e4a895da7dea0316fac34b4da9ab5d7cba5",
                parameter_files={
                    "cg": NPB_ROOT / "CG/npbparams.h",
                    "mg": NPB_ROOT / "MG/npbparams.h",
                },
                expected_parameter_hashes={"cg": "0" * 64, "mg": "1" * 64},
                allocated_bytes={"cg": 12_800_000_000, "mg": 12_800_000_000},
            )

    def test_formal_build_rejects_sub_paper_allocation(self):
        source = self.root / "clean-npb"
        source.mkdir()
        (source / ".git").mkdir()
        parameters = {}
        hashes = {}
        for workload in ("cg", "mg"):
            parameter = source / f"{workload}.npbparams.h"
            parameter.write_text(f"class {workload}\n", encoding="utf-8")
            parameters[workload] = parameter
            hashes[workload] = builder._sha256_file(parameter)
        with mock.patch.object(
            builder, "_git_read",
            side_effect=["", "35cd0e4a895da7dea0316fac34b4da9ab5d7cba5\n"],
        ):
            with self.assertRaisesRegex(builder.BuildError, "12.8 GB"):
                builder.validate_npb_formal_source(
                    source,
                    expected_commit=(
                        "35cd0e4a895da7dea0316fac34b4da9ab5d7cba5"
                    ),
                    parameter_files=parameters,
                    expected_parameter_hashes=hashes,
                    allocated_bytes={"cg": 1, "mg": 12_800_000_000},
                )

    def test_formal_build_requires_matching_allocation_probe(self):
        source = self.root / "clean-npb"
        source.mkdir()
        (source / ".git").mkdir()
        parameters = {}
        hashes = {}
        for workload in ("cg", "mg"):
            parameter = source / f"{workload}.npbparams.h"
            parameter.write_text(f"class {workload}\n", encoding="utf-8")
            parameters[workload] = parameter
            hashes[workload] = builder._sha256_file(parameter)
        with mock.patch.object(
            builder, "_git_read",
            side_effect=["", "35cd0e4a895da7dea0316fac34b4da9ab5d7cba5\n"],
        ):
            with self.assertRaisesRegex(builder.BuildError, "probe.*!="):
                builder.validate_npb_formal_source(
                    source,
                    expected_commit=(
                        "35cd0e4a895da7dea0316fac34b4da9ab5d7cba5"
                    ),
                    parameter_files=parameters,
                    expected_parameter_hashes=hashes,
                    allocated_bytes={
                        "cg": 12_800_000_000, "mg": 12_800_000_000,
                    },
                    measured_allocated_bytes={
                        "cg": 12_800_000_001, "mg": 12_800_000_000,
                    },
                )


if __name__ == "__main__":
    unittest.main()
