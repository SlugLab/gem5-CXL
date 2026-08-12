# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from scripts import build_matched_breadth_workloads as builder
from scripts import canonical_work_trace as trace


class MatchedRegionBuildTest(unittest.TestCase):
    def setUp(self):
        if shutil.which("g++") is None:
            self.skipTest("g++ is unavailable")
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

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
            "1e0265f05bf7d0027adb8ae3454394fb16f85a5fce4d03f26a97a427f32b08b5",
        )
        self.assertEqual(mcf.meta["trace_records"], 26)
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
            "44993de93f6a914e49747218f9080becf42a12d1b9f5559cb1781d6241f8cbf7",
        )
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

    def test_formal_inputs_require_mcf_source_and_paper_allocations(self):
        files = {}
        for name in (
            "mcf", "mcf_source", "amg_values", "amg_index",
            "lulesh_values", "lulesh_index",
        ):
            path = self.root / name
            path.write_bytes(name.encode("utf-8"))
            files[name] = path.resolve()
        for name in ("amg_values", "lulesh_values"):
            files[name].write_bytes(b"\x00" * 8)
        for name in ("amg_index", "lulesh_index"):
            files[name].write_bytes(b"\x00" * 16)
        def row(path):
            return {
                "input": str(path),
                "input_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }

        record = {
            "schema": 1,
            "status": "accepted",
            "workloads": {
                "mcf": {
                    **row(files["mcf"]),
                    "source": str(files["mcf_source"]),
                    "source_sha256": hashlib.sha256(
                        files["mcf_source"].read_bytes()
                    ).hexdigest(),
                    "allocated_bytes": 345_000_000,
                    "synthetic": False,
                },
                "amg_gather": {
                    **row(files["amg_values"]),
                    "index": str(files["amg_index"]),
                    "index_sha256": hashlib.sha256(
                        files["amg_index"].read_bytes()
                    ).hexdigest(),
                    "allocated_bytes": 1 << 30,
                },
                "lulesh_scatter": {
                    **row(files["lulesh_values"]),
                    "index": str(files["lulesh_index"]),
                    "index_sha256": hashlib.sha256(
                        files["lulesh_index"].read_bytes()
                    ).hexdigest(),
                    "allocated_bytes": 1 << 30,
                },
            },
        }
        manifest = self.root / "inputs.json"
        manifest.write_text(json.dumps(record) + "\n", encoding="utf-8")

        inputs, digest = builder.load_formal_inputs(manifest)

        self.assertEqual(inputs["mcf_source"]["path"], str(files["mcf_source"]))
        self.assertRegex(digest, r"^[0-9a-f]{64}$")
        record["workloads"]["amg_gather"]["allocated_bytes"] = 1024
        manifest.write_text(json.dumps(record) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(builder.BuildError, "amg_gather.*allocated"):
            builder.load_formal_inputs(manifest)

    def test_formal_cli_fails_closed_without_real_spec_mcf(self):
        files = {}
        for name in (
            "mcf", "mcf_source", "amg_values", "amg_index",
            "lulesh_values", "lulesh_index",
        ):
            path = self.root / f"formal-{name}"
            path.write_bytes(b"\x00" * (8 if "index" in name else 4))
            files[name] = path.resolve()

        def digest(path):
            return hashlib.sha256(path.read_bytes()).hexdigest()

        record = {
            "schema": 1, "status": "accepted", "workloads": {
                "mcf": {
                    "input": str(files["mcf"]),
                    "input_sha256": digest(files["mcf"]),
                    "source": str(files["mcf_source"]),
                    "source_sha256": digest(files["mcf_source"]),
                    "allocated_bytes": 345_000_000, "synthetic": False,
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
        status = builder.main([
            "--formal", "--inputs", str(manifest),
            "--outdir", str(self.root / "formal-build"),
        ])
        self.assertEqual(status, 1)
        self.assertFalse((self.root / "formal-build/manifest.json").exists())


if __name__ == "__main__":
    unittest.main()
