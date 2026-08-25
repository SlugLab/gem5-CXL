# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

import hashlib
import dataclasses
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from scripts import build_matched_breadth_workloads as builder
from scripts import canonical_work_trace as trace
from scripts import generate_mcfreg2_state as generator
from scripts import mcfreg2
from test_mcfreg2 import MCFREG2Test


class MatchedRegionBuildTest(unittest.TestCase):
    def setUp(self):
        if shutil.which("g++") is None:
            self.skipTest("g++ is unavailable")
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def make_verified_formal_record(self):
        helper = MCFREG2Test()
        helper.root = self.root
        package_path = helper.write_semantic_fixture("formal-mcf.reg2")
        package = mcfreg2.read_package(package_path)
        source = self.root / "formal-mcf-source.c"
        source.write_text("int formal_mcf_source;\n", encoding="ascii")
        identity = {
            "source_commit": "2b30de22399402d8c44bd74b8ebf743b6a6a55e9",
            "source_tree_sha256": "1" * 64,
            "input_sha256": "2" * 64,
            "common_patch_sha256": "3" * 64,
            "capture_patch_sha256": "4" * 64,
            "compiler_sha256": "5" * 64,
        }
        final_state_sha256 = "6" * 64
        mcf_output_sha256 = "7" * 64
        replacements = {
            "PROVENANCE": generator._canonical_json({
                "schema": 1, **identity,
            }),
            "FINAL": generator._canonical_json({
                "schema": 1,
                "initial_state_sha256": "8" * 64,
                "final_state_sha256": final_state_sha256,
                "final_network_words": [0],
                "mcf_output_bytes": 1,
                "mcf_output_sha256": mcf_output_sha256,
                "peak_allocated_bytes": 345_000_000,
            }),
        }
        package = dataclasses.replace(
            package,
            sections=tuple(
                dataclasses.replace(
                    section,
                    data=replacements[mcfreg2.SECTION_NAMES[section.section_type]],
                    element_count=1,
                    element_size=len(
                        replacements[mcfreg2.SECTION_NAMES[section.section_type]]
                    ),
                )
                if mcfreg2.SECTION_NAMES[section.section_type] in replacements
                else section
                for section in package.sections
            ),
        )
        package_sha256 = mcfreg2.write_package(package_path, package)
        validation = {
            "schema": 2,
            "status": "accepted",
            "identity": identity,
            "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            "package_sha256": package_sha256,
            "primary_package_sha256": package_sha256,
            "replay_package_sha256": package_sha256,
            "primary_replay_equal": True,
            "native_outputs_equal": True,
            "boundary_mismatches": 0,
            "authority_final_state_sha256": final_state_sha256,
            "capture_primary_final_state_sha256": final_state_sha256,
            "capture_replay_final_state_sha256": final_state_sha256,
            "authority_mcf_output_sha256": mcf_output_sha256,
            "capture_primary_mcf_output_sha256": mcf_output_sha256,
            "capture_replay_mcf_output_sha256": mcf_output_sha256,
            "peak_allocated_bytes": 345_000_000,
        }
        validation_path = self.root / "formal-validation.json"
        validation_path.write_bytes(generator._canonical_json(validation))

        files = {}
        for name in (
            "amg_values", "amg_index", "lulesh_values", "lulesh_index",
        ):
            path = self.root / name
            path.write_bytes(b"\x00" * (16 if "index" in name else 8))
            files[name] = path.resolve()

        def digest(path):
            return hashlib.sha256(path.read_bytes()).hexdigest()

        record = {
            "schema": 1,
            "status": "accepted",
            "workloads": {
                "mcf": {
                    "input": str(package_path.resolve()),
                    "input_sha256": package_sha256,
                    "source": str(source.resolve()),
                    "source_sha256": digest(source),
                    "allocated_bytes": 345_000_000,
                    "synthetic": False,
                    "format": "MCFREG2",
                    "source_commit": identity["source_commit"],
                    "source_tree_sha256": identity["source_tree_sha256"],
                    "validation": str(validation_path.resolve()),
                    "validation_sha256": digest(validation_path),
                },
                "amg_gather": {
                    "input": str(files["amg_values"]),
                    "input_sha256": digest(files["amg_values"]),
                    "index": str(files["amg_index"]),
                    "index_sha256": digest(files["amg_index"]),
                    "allocated_bytes": 1 << 30,
                },
                "lulesh_scatter": {
                    "input": str(files["lulesh_values"]),
                    "input_sha256": digest(files["lulesh_values"]),
                    "index": str(files["lulesh_index"]),
                    "index_sha256": digest(files["lulesh_index"]),
                    "allocated_bytes": 1 << 30,
                },
            },
        }
        manifest = self.root / "formal-inputs.json"
        manifest.write_text(json.dumps(record) + "\n", encoding="utf-8")
        return manifest

    def test_fixture_builds_strict_four_backend_manifest(self):
        manifest = builder.build_fixture_suite(self.root / "build")

        self.assertEqual(manifest["threads"], 4)
        self.assertEqual(
            manifest["flags"],
            ["-O3", "-fopenmp", "-ffp-contract=off", "-fno-fast-math"],
        )
        self.assertNotIn("-march=native", manifest["command_flags"])
        self.assertEqual(
            set(manifest["binaries"]),
            {
                f"{workload}:{backend}"
                for workload in ("mcf", "spatter")
                for backend in ("reference", "vanilla", "amu", "cira")
            },
        )
        for row in manifest["binaries"].values():
            self.assertTrue(Path(row["path"]).is_file())
            self.assertRegex(row["sha256"], r"^[0-9a-f]{64}$")
            self.assertRegex(row["source_sha256"], r"^[0-9a-f]{64}$")
            self.assertRegex(row["trace_abi_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(
            set(manifest["binaries"]["mcf:reference"]["source_files"]),
            {
                "mcf_regions.cc", "mcfreg2.cc", "mcfreg2.hh",
                "mcfreg2_format.h", "mcfreg2_state.cc",
                "mcfreg2_state.hh", "mcfreg2_kernels.cc",
                "mcfreg2_kernels.hh",
            },
        )
        self.assertEqual(
            manifest["shared_objects"]["binaries"], manifest["binaries"]
        )
        self.assertEqual(
            set(manifest["latency_action_layouts"]),
            {"mcf", "amg_gather", "lulesh_scatter"},
        )

    def test_latency_action_layout_shares_functional_and_templates_timing(self):
        layout = builder.latency_action_layout("mcf", ("pricing",))
        for system, action in layout["functional"].items():
            rendered = json.dumps(action, sort_keys=True)
            self.assertNotIn("{{cxl_link_delay}}", rendered)
            for label in ("200ns", "500ns", "1us", "2us"):
                self.assertNotIn(f"/{label}/", rendered)
            self.assertIn(f"shared/functional/mcf/{system}", action["evidence"])
        for action in layout["window"]["pricing"].values():
            self.assertIn("{{cxl_link_delay}}", action["command"])
            self.assertIn("{{cxl_link_delay}}", action["evidence"])

    def test_reference_mcf_records_both_hotspots_and_complete_boundaries(self):
        build = builder.build_fixture_suite(self.root / "build")
        outputs = builder.run_fixture_references(build, self.root / "runs")
        mcf = trace.read_bundle(outputs["mcf"])

        self.assertEqual(mcf.meta["phases"], ["pricing_kernel", "price_out_impl"])
        self.assertGreater(mcf.meta["phase_work"]["pricing_kernel"], 0)
        self.assertGreater(mcf.meta["phase_work"]["price_out_impl"], 0)
        self.assertEqual(mcf.meta["phase_invocations"], {
            "pricing_kernel": 2,
            "price_out_impl": 3,
        })
        self.assertEqual(mcf.meta["state_shape"], {
            "nodes": 4, "arcs": 4, "price_out_boundaries": 3,
        })
        self.assertEqual(set(mcf.outputs), {
            "objective", "flow", "cost", "potential", "predecessor",
            "depth", "orientation", "tree",
        })
        self.assertTrue(all(len(words) > 0 for words in mcf.outputs.values()))
        self.assertEqual(len(mcf.outputs["flow"]), 4 * 3)
        self.assertEqual(len(mcf.outputs["potential"]), 4 * 3)
        self.assertEqual(
            mcf.meta["trace_sha256"],
            "def6c2c55fd2615fbf603804c05ade0107d46d2083ec903e7b216c79d7602b01",
        )
        self.assertEqual(mcf.meta["trace_records"], 101)
        self.assertEqual(set(mcf.meta["initial_memory"]), {
            "arcs", "potential", "predecessor", "depth", "orientation",
            "tree", "objective", "pricing_offsets", "pricing_index",
            "price_out_index",
        })
        pricing = [
            operation for operation in mcf.operations
            if operation.phase == 1
        ]
        self.assertEqual(
            [operation.address for operation in pricing[:8]],
            [
                builder.MCF_BASES["pricing_offsets"],
                builder.MCF_BASES["pricing_offsets"] + 8,
                builder.MCF_BASES["pricing_index"],
                builder.MCF_BASES["arc"],
                builder.MCF_BASES["arc"] + 8,
                builder.MCF_BASES["arc"] + 16,
                builder.MCF_BASES["potential"],
                builder.MCF_BASES["potential"] + 8,
            ],
        )
        self.assertEqual(
            [operation.operand1 for operation in pricing[:8]],
            [0, 0, 2, 3, 3, 3, 4, 5],
        )
        price_out_loads = {
            operation.address for operation in mcf.operations
            if operation.phase == 2 and operation.opcode == trace.Opcode.LOAD_U64
        }
        self.assertTrue(any(
            builder.MCF_BASES["price_out_index"] <= address <
            builder.MCF_BASES["price_out_index"] + 3 * 8
            for address in price_out_loads
        ))
        self.assertTrue(any(
            builder.MCF_BASES["arc"] + 24 == address
            for address in price_out_loads
        ))
        self.assertTrue(any(
            builder.MCF_BASES["depth"] <= address <
            builder.MCF_BASES["depth"] + 4 * 8
            for address in price_out_loads
        ))
        self.assertEqual(mcf.outputs["flow"], (
            1, 1, 0, 0, 1, 1, 1, 0, 1, 1, 1, 0,
        ))
        self.assertEqual(mcf.outputs["objective"], (
            (1 << 64) - 6, (1 << 64) - 12, (1 << 64) - 12,
        ))

    def test_real_order_gather_and_duplicate_scatter_are_bit_exact(self):
        build = builder.build_fixture_suite(self.root / "build")
        outputs = builder.run_fixture_references(build, self.root / "runs")
        gather = trace.read_bundle(outputs["amg_gather"])
        scatter = trace.read_bundle(outputs["lulesh_scatter"])

        self.assertEqual(gather.meta["phases"], ["amg_gather"])
        self.assertEqual(scatter.meta["phases"], ["lulesh_scatter"])
        self.assertEqual(
            scatter.meta["duplicate_policy"], "canonical_program_order"
        )
        expected_bits = builder.fixture_gather_expected_bits()
        self.assertEqual(gather.outputs["destination"], expected_bits)
        self.assertEqual(
            gather.meta["trace_sha256"],
            "9ce8139d9b57dfb1629966a781391c51e766c4693a566da14df2ef43b4501223",
        )
        for index in range(0, len(gather.operations), 3):
            index_load, value_load, store = gather.operations[index:index + 3]
            self.assertEqual(index_load.opcode, trace.Opcode.LOAD_U64)
            self.assertEqual(value_load.opcode, trace.Opcode.LOAD_F32)
            self.assertEqual(value_load.operand1, index_load.sequence + 1)
            self.assertEqual(store.opcode, trace.Opcode.STORE_F32)
        self.assertEqual(
            scatter.meta["trace_sha256"],
            "6bcb6b74e9f15421a2abcf8ab1c0317250692ce3089f15dc0a47f9b9bbda1fd8",
        )
        self.assertEqual(
            scatter.outputs["destination"],
            builder.fixture_scatter_expected_bits(),
        )

        faulty = builder.run_faulty_scatter_reversed_duplicates(
            build, self.root / "faulty"
        )
        with self.assertRaisesRegex(trace.TraceError, "destination"):
            builder.verify_reference_bundle(outputs["lulesh_scatter"], faulty)

    def test_formal_mode_rejects_fixture_and_synthetic_inputs(self):
        with self.assertRaisesRegex(builder.BuildError, "formal.*fixture"):
            builder.validate_mode(formal=True, fixture=True, synthetic=False)
        with self.assertRaisesRegex(builder.BuildError, "formal.*synthetic"):
            builder.validate_mode(formal=True, fixture=False, synthetic=True)

    def test_manifest_is_stable_json_and_binds_fixture_inputs(self):
        manifest = builder.build_fixture_suite(self.root / "build")
        on_disk = json.loads(
            (self.root / "build/manifest.json").read_text(encoding="utf-8")
        )
        self.assertEqual(manifest, on_disk)
        self.assertEqual(set(manifest["inputs"]), {
            "mcf", "amg_values", "amg_index", "lulesh_values",
            "lulesh_index",
        })
        for row in manifest["inputs"].values():
            self.assertRegex(row["sha256"], r"^[0-9a-f]{64}$")
        self.assertRegex(manifest["input_manifest_sha256"], r"^[0-9a-f]{64}$")

    def test_formal_inputs_require_mcfreg2_and_paper_allocations(self):
        manifest = self.make_verified_formal_record()
        inputs, digest = builder.load_formal_inputs(manifest)

        self.assertEqual(inputs["mcf"]["format"], "MCFREG2")
        self.assertEqual(inputs["mcf"]["boundary_mismatches"], 0)
        self.assertRegex(digest, r"^[0-9a-f]{64}$")
        record = json.loads(manifest.read_text(encoding="utf-8"))
        record["workloads"]["amg_gather"]["allocated_bytes"] = 1024
        manifest.write_text(json.dumps(record) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(builder.BuildError, "amg_gather.*allocated"):
            builder.load_formal_inputs(manifest)

    def test_formal_mcfreg2_builds_verified_reference_bundle(self):
        record = self.make_verified_formal_record()
        inputs, digest = builder.load_formal_inputs(record)
        manifest = builder.build_suite(
            self.root / "formal-build",
            inputs=inputs,
            input_manifest_sha256=digest,
        )
        outputs = builder.run_formal_references(
            manifest, self.root / "formal-runs"
        )
        bundle = trace.read_bundle(outputs["mcf"])
        self.assertEqual(bundle.meta["input_format"], "MCFREG2")
        self.assertEqual(bundle.meta["boundary_mismatches"], 0)


if __name__ == "__main__":
    unittest.main()
