# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

import hashlib
import dataclasses
import json
import os
import contextlib
import subprocess
import shutil
import struct
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from scripts import canonical_work_trace as canonical
from scripts import m2ndp_workload_trace as m2ndp
from scripts import lazy_work_trace as lazy
from scripts import npb_lazy_trace as npb


def digest(label):
    return hashlib.sha256(label.encode()).hexdigest()


def provenance(root, bundle, *, funcsim_body=None, seed_timing_output=False,
               trace_sha256=None, input_sha256=None):
    tools = root / "tools"
    tools.mkdir(parents=True, exist_ok=True)
    funcsim = tools / "FuncSim"
    ndpsim = tools / "NDPSim"
    funcsim.write_text(
        funcsim_body or "#!/bin/sh\nexit 0\n", encoding="utf-8"
    )
    ndpsim.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    funcsim.chmod(0o755)
    ndpsim.chmod(0o755)
    patch = root / "canonical.patch"
    config = root / "config" / "m2ndp.config"
    config.parent.mkdir(exist_ok=True)
    patch.write_text("canonical patch\n", encoding="utf-8")
    config.write_text("num_ndp_units=1\n", encoding="utf-8")
    (config.parent / "cxl_link.icnt").write_text(
        "link_latency = 7799;\n", encoding="utf-8"
    )
    if seed_timing_output:
        (config.parent / "ndpsim.out").write_text(
            "stale packaged output\n", encoding="utf-8"
        )
        (config.parent / "energy_ndpsim.out").write_text(
            "stale packaged energy\n", encoding="utf-8"
        )
    return m2ndp.PackageProvenance(
        trace_sha256=trace_sha256 or bundle.meta["trace_sha256"],
        input_sha256=input_sha256 or bundle.meta["input_sha256"],
        funcsim_path=str(funcsim), ndpsim_path=str(ndpsim),
        patch_paths=(str(patch),), config_path=str(config),
    )


def functional_evidence(package):
    package = Path(package)
    manifest = json.loads(package.read_text())
    provenance_record = manifest["provenance"]
    compared_words = sum(
        len(record["raw_words"])
        for record in manifest["output_boundaries"].values()
    )
    stdout = (
        "M2NDP_CANONICAL_MODE=1\n"
        f"M2NDP_CANONICAL_LAUNCHES={manifest['dynamic_launches']}\n"
        f"M2NDP_CANONICAL_BOUNDARIES={compared_words}\n"
        f"M2NDP_CANONICAL_OPERATIONS={manifest['operation_count']}\n"
        "M2NDP_CANONICAL_MATCH=PASS\n"
    )
    stdout_path = package.parent / "fixture-funcsim.stdout.log"
    stderr_path = package.parent / "fixture-funcsim.stderr.log"
    stdout_path.write_text(stdout, encoding="utf-8")
    stderr_path.write_text("", encoding="utf-8")
    return {
        "schema": 1, "status": "pass",
        "boundary_count": len(manifest["output_boundaries"]),
        "compared_words": compared_words,
        "compared_operations": manifest["operation_count"],
        "functional_gate": manifest["functional_gate"],
        "expected_launches": manifest["dynamic_launches"],
        "completed_launches": manifest["dynamic_launches"],
        "returncode": 0,
        "package_sha256": m2ndp._sha256_file(package),
        "trace_sha256": provenance_record["trace_sha256"],
        "input_sha256": provenance_record["input_sha256"],
        "funcsim_sha256": provenance_record["funcsim_sha256"],
        "config_sha256": provenance_record["config_sha256"],
        "patch_sha256": provenance_record["patch_sha256"],
        "stdout_path": str(stdout_path),
        "stderr_path": str(stderr_path),
        "stdout_sha256": hashlib.sha256(stdout.encode()).hexdigest(),
        "stderr_sha256": hashlib.sha256(b"").hexdigest(),
    }


def calibration(package, *, cxl_link_delay="1us"):
    manifest = json.loads(Path(package).read_text())
    config = Path(package).parent / manifest["timing_config"]["path"]
    links = tuple(config.parent.rglob("cxl_link.icnt"))
    assert len(links) == 1
    return {
        "passed": True, "cxl_delay": cxl_link_delay,
        "cxl_link_delay": cxl_link_delay,
        "target_ns": "2012.652", "measured_ns": "2012.625",
        "residual_ns": "0.027", "link_period_ns": "0.125",
        "target_cxl_boundary_ticks": 2_012_652,
        "derived_m2ndp_config_sha256": m2ndp._sha256_file(config),
        "derived_cxl_link_config_sha256": m2ndp._sha256_file(links[0]),
    }
def operation(opcode, sequence, *, phase=0, address=0x1000,
              left=1, right=2, result=3, work_item=None):
    return canonical.Operation(
        phase, opcode, sequence if work_item is None else work_item,
        sequence, address, left, right, result,
    )


REGIONS = {
    "pr_spmv": (
        canonical.Opcode.LOAD_F32, canonical.Opcode.F32_MUL,
        canonical.Opcode.F32_ADD, canonical.Opcode.STORE_F32,
        canonical.Opcode.COMMIT,
    ),
    "mcf": (
        canonical.Opcode.LOAD_U64, canonical.Opcode.I64_ADD,
        canonical.Opcode.I64_MIN, canonical.Opcode.STORE_U64,
        canonical.Opcode.COMMIT,
    ),
    "amg_gather": (
        canonical.Opcode.LOAD_F64, canonical.Opcode.F64_ADD,
        canonical.Opcode.STORE_F64, canonical.Opcode.COMMIT,
    ),
    "lulesh_scatter": (
        canonical.Opcode.LOAD_U32, canonical.Opcode.LOAD_F64,
        canonical.Opcode.STORE_F64, canonical.Opcode.STORE_F64,
        canonical.Opcode.COMMIT,
    ),
    "npb_cg": (
        canonical.Opcode.F64_MUL, canonical.Opcode.F64_ADD,
        canonical.Opcode.F64_DIV, canonical.Opcode.BARRIER,
        canonical.Opcode.COMMIT,
    ),
    "npb_mg": (
        canonical.Opcode.F64_SUB, canonical.Opcode.F64_MAX,
        canonical.Opcode.F64_SQRT, canonical.Opcode.F64_MOV,
        canonical.Opcode.F64_ABS, canonical.Opcode.COMMIT,
    ),
}


class M2NDPWorkloadTraceTest(unittest.TestCase):
    def test_sparse_window_memory_map_is_derived_from_operation_operands(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            trace_root = root / "trace"
            operations = (
                operation(
                    canonical.Opcode.LOAD_U64, 0,
                    address=0x1000, left=7, right=0, result=7,
                ),
                operation(
                    canonical.Opcode.STORE_U64, 1,
                    address=0x2000, left=9, right=0, result=9,
                ),
            )
            canonical.write_bundle(
                trace_root,
                {
                    "schema": 1, "workload": "amg_gather",
                    "input_sha256": digest("input"),
                    "source_sha256": digest("source"),
                    "binary_sha256": digest("binary"),
                    "config_sha256": digest("config"),
                    "phases": [{"id": 3}],
                    "output_boundaries": {},
                },
                operations, {}, initial_memory={},
            )
            package = root / "package"
            package.mkdir()
            initial, target, images = m2ndp._write_memory_map(
                canonical.read_bundle(trace_root), trace_root, package
            )
            initial_text = initial.read_text(encoding="utf-8")
            target_text = target.read_text(encoding="utf-8")
        self.assertEqual(images, [])
        self.assertIn("0x1000 7 0 0 0", initial_text)
        self.assertIn("0x2000 0 0 0 0", initial_text)
        self.assertIn("0x2000 9 0 0 0", target_text)

    def test_prepared_window_provenance_is_retained_by_lowering(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            trace_root = root / "trace"
            operations = (
                operation(
                    canonical.Opcode.LOAD_U64, 0,
                    address=0x1000, left=7, right=0, result=7,
                ),
            )
            canonical.write_bundle(
                trace_root,
                {
                    "schema": 1, "workload": "amg_gather",
                    "input_sha256": digest("input"),
                    "source_sha256": digest("source"),
                    "binary_sha256": digest("binary"),
                    "config_sha256": digest("config"),
                    "phases": [{"id": 3}],
                    "output_boundaries": {},
                    "prepared_window": {
                        "source_schema": 3,
                        "source_trace_sha256": digest("formal-trace"),
                        "phase": 3,
                        "phase_name": "amg_gather",
                        "warmup_items": 8,
                        "measured_items": 8,
                        "measure_start_item": 8,
                        "fixed_event_records": 2,
                        "fixed_trace_sha256": digest("fixed"),
                        "window_index": 0,
                        "warmup_start": 0,
                        "measure_start": 8,
                        "measure_stop": 16,
                    },
                },
                operations, {}, initial_memory={},
            )
            bundle = canonical.read_bundle(trace_root)
            package = m2ndp.lower_bundle(
                trace_root, root / "package",
                provenance=provenance(root, bundle),
            )
            manifest = json.loads(package.read_text(encoding="utf-8"))
        self.assertEqual(manifest["functional_gate"], "operation_results")
        self.assertEqual(manifest["derived_window"], {
            "source_trace_sha256": digest("formal-trace"),
            "window_index": 0,
            "warmup_start": 0,
            "measure_start": 8,
            "measure_stop": 16,
        })

    def test_lowering_table_covers_every_canonical_opcode(self):
        self.assertEqual(set(m2ndp.LOWERING), set(canonical.Opcode))

    def test_all_six_regions_lower_every_operation_once_without_fma(self):
        for name, opcodes in REGIONS.items():
            with self.subTest(name=name):
                operations = tuple(
                    operation(opcode, sequence)
                    for sequence, opcode in enumerate(opcodes)
                )
                lines = m2ndp.lower_operations(operations)
                self.assertEqual(len(lines), len(operations))
                self.assertEqual(
                    [row.sequence for row in lines],
                    list(range(len(operations))),
                )
                text = "\n".join(row.instruction for row in lines).lower()
                for forbidden in ("fmadd", "vfmacc", "vfred", "vector"):
                    self.assertNotIn(forbidden, text)

    def test_real_lazy_cg_mg_packages_preserve_recurring_phase_launches(self):
        lazy_keep_root = os.environ.get("M2NDP_REAL_LAZY_KEEP_ROOT")
        root_context = (
            contextlib.nullcontext(lazy_keep_root)
            if lazy_keep_root else tempfile.TemporaryDirectory()
        )
        with root_context as temporary:
            root = Path(temporary)
            root.mkdir(parents=True, exist_ok=True)

            def image(bundle_root, name, element_type, values, base,
                      role="input"):
                formats = {"u32": "I", "f64": "d"}
                payload = struct.pack(
                    f"<{len(values)}{formats[element_type]}", *values
                )
                path = bundle_root / f"images/{name}.{element_type}"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(payload)
                return lazy.ArrayImage(
                    name, role, element_type, len(values), base,
                    path.relative_to(bundle_root).as_posix(),
                    hashlib.sha256(payload).hexdigest(),
                )

            cg_root = root / "cg"
            cg_arrays = (
                image(cg_root, "rowstr", "u32", (0, 1), 0x1000),
                image(cg_root, "colidx", "u32", (0,), 0x2000),
                image(cg_root, "a", "f64", (2.0,), 0x3000),
                image(cg_root, "p", "f64", (3.0,), 0x4000),
                image(cg_root, "q", "f64", (0.0,), 0x5000, "state"),
            )
            cg_invocations = tuple(
                lazy.Invocation(
                    ordinal, 101, "npb_cg_spmv", ordinal + 1, 1,
                    {"rowstr": "rowstr", "colidx": "colidx",
                     "values": "a", "source": "p",
                     "destination": "q", "row_count": 1,
                     "edge_base": 0, "column_base": 0,
                     "destination_count": 1},
                )
                for ordinal in range(2)
            )
            cg_commitments = {
                f"q.spmv.iter{iteration}": hashlib.sha256(
                    struct.pack("<d", 6.0)
                ).hexdigest()
                for iteration in (1, 2)
            }
            lazy.write_bundle(
                cg_root,
                {"schema": 2, "workload": "npb_cg",
                 "source_sha256": digest("cg-source"),
                 "binary_sha256": digest("cg-binary"),
                 "config_sha256": digest("cg-config"),
                 "boundary_commitments": cg_commitments},
                cg_arrays, cg_invocations, {"primitive_records": 20},
            )

            mg_root = root / "mg"
            mg_values = tuple((index + 1) / 8.0 for index in range(8))
            mg_arrays = (
                image(mg_root, "u", "f64", mg_values, 0x8000, "state"),
            )
            mg_invocation = lazy.Invocation(
                0, 200, "npb_mg_zero3", 1, 8,
                {"u": "u", "n1": 2, "n2": 2, "n3": 2,
                 "boundaries": ["u"]},
            )
            lazy.write_bundle(
                mg_root,
                {"schema": 2, "workload": "npb_mg",
                 "source_sha256": digest("mg-source"),
                 "binary_sha256": digest("mg-binary"),
                 "config_sha256": digest("mg-config"),
                 "boundary_commitments": {
                     "u.zero3.iter1": hashlib.sha256(
                         struct.pack("<8d", *([0.0] * 8))
                     ).hexdigest(),
                 }},
                mg_arrays, (mg_invocation,), {"primitive_records": 10},
            )

            for name, trace_root, expected_launches in (
                ("cg", cg_root, 2), ("mg", mg_root, 1)
            ):
                with self.subTest(name=name):
                    bundle = lazy.read_bundle(trace_root)
                    trace_sha256 = m2ndp._sha256_file(
                        trace_root / "trace.v2.json"
                    )
                    if os.environ.get("M2NDP_REAL_FUNCSIM"):
                        package_provenance = m2ndp.PackageProvenance(
                            trace_sha256=trace_sha256,
                            input_sha256=bundle.meta["input_sha256"],
                            funcsim_path=os.environ["M2NDP_REAL_FUNCSIM"],
                            ndpsim_path=os.environ["M2NDP_REAL_NDPSIM"],
                            patch_paths=tuple(
                                item for item in os.environ[
                                    "M2NDP_REAL_PATCHES"
                                ].split(":") if item
                            ),
                            config_path=os.environ["M2NDP_REAL_CONFIG"],
                            ndpsim_config_path=os.environ.get(
                                "M2NDP_REAL_TIMING_CONFIG",
                                os.environ["M2NDP_REAL_CONFIG"],
                            ),
                        )
                    else:
                        package_provenance = provenance(
                            root / f"provenance-{name}", bundle,
                            trace_sha256=trace_sha256,
                            input_sha256=bundle.meta["input_sha256"],
                        )
                    with self.assertRaisesRegex(
                        m2ndp.TraceTranslationError,
                        "source input SHA-256 differs",
                    ):
                        m2ndp.lower_bundle(
                            trace_root, root / f"package-{name}-wrong-input",
                            provenance=dataclasses.replace(
                                package_provenance,
                                input_sha256=digest(f"{name}-wrong-input"),
                            ),
                        )
                    package = m2ndp.lower_bundle(
                        trace_root, root / f"package-{name}",
                        provenance=package_provenance,
                    )
                    manifest = json.loads(package.read_text())
                    self.assertEqual(manifest["source_schema"], 2)
                    self.assertEqual(
                        manifest["dynamic_launches"], expected_launches
                    )
                    self.assertEqual(
                        len({row["path"] for row in manifest["kernels"]}),
                        expected_launches,
                    )
                    self.assertEqual(
                        set(manifest["output_boundaries"]),
                        set(bundle.meta["boundary_commitments"]),
                    )
                    if os.environ.get("M2NDP_REAL_FUNCSIM"):
                        evidence = m2ndp.run_funcsim_package(package)
                        self.assertEqual(evidence["status"], "pass")
                        self.assertEqual(
                            evidence["compared_operations"],
                            manifest["operation_count"],
                        )

    def test_duplicate_scatter_stores_keep_canonical_sequence(self):
        operations = (
            operation(canonical.Opcode.STORE_F64, 0, address=0x4000,
                      result=0x3FF0000000000000),
            operation(canonical.Opcode.STORE_F64, 1, address=0x4000,
                      result=0x4000000000000000),
            operation(canonical.Opcode.COMMIT, 2),
        )
        lines = m2ndp.lower_operations(operations)
        stores = [row for row in lines if row.opcode == "STORE_F64"]
        self.assertEqual([row.sequence for row in stores], [0, 1])
        self.assertEqual([row.address for row in stores], [0x4000, 0x4000])

    def test_cg_reduction_tree_dependencies_are_retained(self):
        relative = canonical.LOAD_DEPENDENCY_RELATIVE_FLAG
        operations = (
            operation(canonical.Opcode.LOAD_F64, 0),
            operation(canonical.Opcode.LOAD_F64, 1,
                      right=relative | 1),
            operation(canonical.Opcode.F64_ADD, 2, left=0, right=1),
            operation(canonical.Opcode.BARRIER, 3),
            operation(canonical.Opcode.COMMIT, 4),
        )
        lines = m2ndp.lower_operations(operations)
        self.assertEqual(lines[1].dependency, relative | 1)
        self.assertEqual(lines[2].operand0, 0)
        self.assertEqual(lines[2].operand1, 1)
        self.assertTrue(lines[3].instruction.startswith("c_barrier "))

    def test_unknown_opcode_and_sequence_drift_are_rejected(self):
        with self.assertRaisesRegex(m2ndp.TraceTranslationError, "lowerable"):
            m2ndp.lower_operations((
                SimpleNamespace(sequence=0, opcode=999, phase=0,
                                work_item=0, address=0,
                                operand0=0, operand1=0, result=0),
            ))
        with self.assertRaisesRegex(m2ndp.TraceTranslationError, "sequence"):
            m2ndp.lower_operations((
                operation(canonical.Opcode.COMMIT, 1),
            ))

    def test_package_binds_provenance_launches_and_one_memory_map(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            trace_root = root / "trace"
            operations = (
                operation(canonical.Opcode.LOAD_F32, 0, phase=4),
                operation(canonical.Opcode.COMMIT, 1, phase=4),
                operation(canonical.Opcode.LOAD_F32, 2, phase=7),
                operation(canonical.Opcode.COMMIT, 3, phase=7),
            )
            meta = {
                "schema": 1, "workload": "pr_spmv",
                "input_sha256": digest("input"),
                "source_sha256": digest("source"),
                "binary_sha256": digest("binary"),
                "config_sha256": digest("trace-config"),
                "phases": [{"id": 4}, {"id": 7}],
                "output_boundaries": {},
            }
            canonical.write_bundle(trace_root, meta, operations, {})
            bundle = canonical.read_bundle(trace_root)
            package_provenance = provenance(root, bundle)
            package = m2ndp.lower_bundle(
                trace_root, root / "package", provenance=package_provenance
            )
            manifest = json.loads(package.read_text())
            self.assertEqual(manifest["operation_count"], 4)
            self.assertEqual(manifest["dynamic_launches"], 2)
            self.assertEqual(
                [event["kind"] for event in manifest["launch_events"]],
                ["fixed_launch", "dynamic", "fixed_completion"] * 2,
            )
            self.assertEqual(set(manifest["memory_map"]), {"path", "sha256"})
            self.assertNotIn("memory_map", manifest["launch_events"][0])
            for kernel in manifest["kernels"]:
                text = (root / "package" / kernel["path"]).read_text()
                timing_text = (
                    root / "package" / kernel["timing_path"]
                ).read_text()
                launch_fields = (
                    root / "package" / kernel["launch_path"]
                ).read_text().split()
                self.assertGreaterEqual(
                    int(launch_fields[4], 0), 32,
                    "M2NDP always seeds one scratchpad packet",
                )
                self.assertTrue(text.startswith("-kernel name = "))
                self.assertIn("\n-kernel id = ", text)
                self.assertIn("\nKERNELBODY:\n", text)
                self.assertNotIn("LDG", text)
                self.assertNotIn("STGG", text)
                self.assertNotIn("c_check_", timing_text)
            sequence = (root / "package" / "funcsim.sequence").read_text()
            self.assertNotIn(str(root), sequence)
            for field, value in package_provenance.as_dict().items():
                self.assertEqual(
                    manifest["provenance"][field],
                    list(value) if isinstance(value, tuple) else value,
                )

    def test_package_rejects_source_trace_hash_drift(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            trace_root = root / "trace"
            meta = {
                "schema": 1, "workload": "mcf",
                "input_sha256": digest("input"),
                "source_sha256": digest("source"),
                "binary_sha256": digest("binary"),
                "config_sha256": digest("trace-config"),
                "phases": [{"id": 0}], "output_boundaries": {},
            }
            canonical.write_bundle(
                trace_root, meta,
                (operation(canonical.Opcode.COMMIT, 0),), {},
            )
            package_provenance = provenance(root, canonical.read_bundle(trace_root))
            object.__setattr__(package_provenance, "trace_sha256", "0" * 64)
            with self.assertRaisesRegex(
                m2ndp.TraceTranslationError, "trace SHA-256"
            ):
                m2ndp.lower_bundle(
                    trace_root, root / "package", provenance=package_provenance
                )

    def test_funcsim_runner_requires_real_markers_and_all_boundary_words(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            trace_root = root / "trace"
            operations = (
                operation(canonical.Opcode.STORE_U32, 0, address=0x1004,
                          left=0x3F800000, right=0,
                          result=0x3F800000),
                operation(canonical.Opcode.COMMIT, 1, left=0, right=0,
                          result=0),
            )
            meta = {
                "schema": 1, "workload": "pr_spmv",
                "input_sha256": digest("input"),
                "source_sha256": digest("source"),
                "binary_sha256": digest("binary"),
                "config_sha256": digest("trace-config"),
                "phases": [{"id": 0}],
                "output_boundaries": {"rank": {
                    "word_bits": 32, "count": 1,
                    "probes": [{"address": 0x1004, "after_sequence": 0}],
                }},
            }
            canonical.write_bundle(
                trace_root, meta, operations, {"rank": (0x3F800000,)},
                initial_memory={"rank": {
                    "logical_base": 0x1004, "word_bits": 32,
                    "words": (0,),
                }},
            )
            bundle = canonical.read_bundle(trace_root)
            package_provenance = provenance(
                root, bundle,
                funcsim_body=(
                    "#!/bin/sh\n"
                    "echo M2NDP_CANONICAL_MODE=1\n"
                    "echo M2NDP_CANONICAL_LAUNCHES=1\n"
                    "echo M2NDP_CANONICAL_BOUNDARIES=1\n"
                    "echo M2NDP_CANONICAL_OPERATIONS=2\n"
                    "echo M2NDP_CANONICAL_MATCH=PASS\n"
                ),
            )
            package = m2ndp.lower_bundle(
                trace_root, root / "package", provenance=package_provenance
            )
            evidence = m2ndp.run_funcsim_package(package)
            self.assertEqual(evidence["compared_words"], 1)
            self.assertEqual(evidence["completed_launches"], 1)
            self.assertEqual(evidence["verification"], "pass")
            self.assertEqual(evidence["numeric_verification"], "pass")
            self.assertIs(evidence["bit_exact"], True)
            self.assertEqual(evidence["mismatched_words"], 0)
            self.assertEqual(evidence["nonfinite_words"], 0)
            memory = (root / "package" / "memory-map.data").read_text()
            self.assertIn("0x1000", memory)

            funcsim = Path(package_provenance.funcsim_path)
            funcsim.write_text("#!/bin/sh\nexit 2\n", encoding="utf-8")
            funcsim.chmod(0o755)
            with self.assertRaisesRegex(
                m2ndp.TraceTranslationError, "provenance differs"
            ):
                m2ndp.run_funcsim_package(package)

    def test_funcsim_rejects_unhashed_operation_record_drift(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            trace_root = root / "trace"
            meta = {
                "schema": 1, "workload": "pr_spmv",
                "input_sha256": digest("input"),
                "source_sha256": digest("source"),
                "binary_sha256": digest("binary"),
                "config_sha256": digest("trace-config"),
                "phases": [{"id": 0}],
                "output_boundaries": {"rank": {
                    "word_bits": 32, "count": 1,
                    "probes": [{"address": 0x1004, "after_sequence": 0}],
                }},
            }
            canonical.write_bundle(
                trace_root, meta,
                (operation(canonical.Opcode.STORE_U32, 0, address=0x1004,
                           left=0x3F800000, result=0x3F800000),),
                {"rank": (0x3F800000,)},
                initial_memory={"rank": {
                    "logical_base": 0x1004, "word_bits": 32, "words": (0,),
                }},
            )
            bundle = canonical.read_bundle(trace_root)
            package = m2ndp.lower_bundle(
                trace_root, root / "package",
                provenance=provenance(root, bundle),
            )
            (root / "package" / "operations.jsonl").write_text(
                "tampered\n", encoding="utf-8"
            )
            with mock.patch.object(m2ndp.subprocess, "run") as run:
                with self.assertRaisesRegex(
                    m2ndp.TraceTranslationError,
                    "operation records SHA-256 differs",
                ):
                    m2ndp.run_funcsim_package(package)
            run.assert_not_called()

    def test_funcsim_rejects_output_boundary_manifest_drift(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            trace_root = root / "trace"
            meta = {
                "schema": 1, "workload": "pr_spmv",
                "input_sha256": digest("input"),
                "source_sha256": digest("source"),
                "binary_sha256": digest("binary"),
                "config_sha256": digest("trace-config"),
                "phases": [{"id": 0}],
                "output_boundaries": {"rank": {
                    "word_bits": 32, "count": 1,
                    "probes": [{"address": 0x1004, "after_sequence": 0}],
                }},
            }
            canonical.write_bundle(
                trace_root, meta,
                (operation(canonical.Opcode.STORE_U32, 0, address=0x1004,
                           left=0x3F800000, result=0x3F800000),),
                {"rank": (0x3F800000,)},
                initial_memory={"rank": {
                    "logical_base": 0x1004, "word_bits": 32, "words": (0,),
                }},
            )
            bundle = canonical.read_bundle(trace_root)
            package = m2ndp.lower_bundle(
                trace_root, root / "package",
                provenance=provenance(root, bundle),
            )
            manifest = json.loads(package.read_text())
            manifest["output_boundaries"]["rank"]["raw_words"][0] ^= 1
            package.write_text(json.dumps(manifest), encoding="utf-8")
            with mock.patch.object(m2ndp.subprocess, "run") as run:
                with self.assertRaisesRegex(
                    m2ndp.TraceTranslationError,
                    "output boundary rank SHA-256 differs",
                ):
                    m2ndp.run_funcsim_package(package)
            run.assert_not_called()

    def test_derived_window_uses_its_own_operation_result_funcsim_gate(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            trace_root = root / "trace"
            meta = {
                "schema": 1, "workload": "npb_cg",
                "input_sha256": digest("input"),
                "source_sha256": digest("source"),
                "binary_sha256": digest("binary"),
                "config_sha256": digest("trace-config"),
                "phases": [{"id": 101, "name": "cg_spmv",
                            "work_items": 1}],
                "output_boundaries": {},
                "source_trace_sha256": digest("full-lazy-trace"),
                "window_index": 0, "warmup_start": 0,
                "measure_start": 0, "measure_stop": 1,
            }
            canonical.write_bundle(
                trace_root, meta,
                (operation(canonical.Opcode.F64_MOV, 0, phase=101,
                           left=0, right=0, result=0),), {},
            )
            bundle = canonical.read_bundle(trace_root)
            package = m2ndp.lower_bundle(
                trace_root, root / "package",
                provenance=provenance(
                    root, bundle,
                    funcsim_body=(
                        "#!/bin/sh\n"
                        "echo M2NDP_CANONICAL_MODE=1\n"
                        "echo M2NDP_CANONICAL_LAUNCHES=1\n"
                        "echo M2NDP_CANONICAL_BOUNDARIES=0\n"
                        "echo M2NDP_CANONICAL_OPERATIONS=1\n"
                        "echo M2NDP_CANONICAL_MATCH=PASS\n"
                    ),
                ),
            )
            manifest = json.loads(package.read_text())
            self.assertEqual(manifest["functional_gate"], "operation_results")
            self.assertEqual(
                manifest["derived_window"]["source_trace_sha256"],
                digest("full-lazy-trace"),
            )
            evidence = m2ndp.run_funcsim_package(package)
            self.assertEqual(evidence["compared_words"], 0)
            self.assertEqual(evidence["compared_operations"], 1)
            self.assertTrue(
                m2ndp.artifact_helpers.require_ndpsim_timing_gate(
                    evidence, calibration(package)
                )
            )

    def test_fixed_component_uses_operation_result_funcsim_gate(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            trace_root = root / "trace"
            canonical.write_bundle(
                trace_root,
                {
                    "schema": 1, "workload": "amg_gather",
                    "input_sha256": digest("fixed-input"),
                    "source_sha256": digest("fixed-source"),
                    "binary_sha256": digest("fixed-binary"),
                    "config_sha256": digest("fixed-config"),
                    "phases": [{"id": 3, "name": "amg_gather",
                                "work_items": 1}],
                    "output_boundaries": {}, "fixed_component": True,
                    "source_trace_sha256": digest("full-trace"),
                },
                (operation(canonical.Opcode.COMMIT, 0, phase=3),),
                {},
            )
            bundle = canonical.read_bundle(trace_root)
            package = m2ndp.lower_bundle(
                trace_root, root / "package",
                provenance=provenance(
                    root, bundle,
                    funcsim_body=(
                        "#!/bin/sh\n"
                        "echo M2NDP_CANONICAL_MODE=1\n"
                        "echo M2NDP_CANONICAL_LAUNCHES=1\n"
                        "echo M2NDP_CANONICAL_BOUNDARIES=0\n"
                        "echo M2NDP_CANONICAL_OPERATIONS=1\n"
                        "echo M2NDP_CANONICAL_MATCH=PASS\n"
                    ),
                ),
            )
            manifest = json.loads(package.read_text())
            self.assertTrue(manifest["fixed_component"])
            self.assertEqual(manifest["functional_gate"], "operation_results")
            evidence = m2ndp.run_funcsim_package(package)
            self.assertEqual(evidence["status"], "pass")
            self.assertEqual(evidence["compared_operations"], 1)

    def test_ndpsim_requires_memory_match_and_exact_launch_count(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            trace_root = root / "trace"
            meta = {
                "schema": 1, "workload": "pr_spmv",
                "input_sha256": digest("input"),
                "source_sha256": digest("source"),
                "binary_sha256": digest("binary"),
                "config_sha256": digest("trace-config"),
                "phases": [{"id": 0}],
                "output_boundaries": {"rank": {
                    "word_bits": 32, "count": 1,
                    "probes": [{"address": 0x1004, "after_sequence": 0}],
                }},
            }
            canonical.write_bundle(
                trace_root, meta,
                (operation(canonical.Opcode.STORE_U32, 0, address=0x1004,
                           left=0x3F800000, result=0x3F800000),),
                {"rank": (0x3F800000,)},
                initial_memory={"rank": {
                    "logical_base": 0x1004, "word_bits": 32, "words": (0,),
                }},
            )
            bundle = canonical.read_bundle(trace_root)
            package = m2ndp.lower_bundle(
                trace_root, root / "package",
                provenance=provenance(root, bundle),
            )
            functional = functional_evidence(package)
            timing_calibration = calibration(package)
            output = root / "package" / "timing-run" / "ndpsim.out"

            for changes in (
                {"compared_operations": 2},
                {"compared_words": 2},
                {"boundary_count": 2},
                {"functional_gate": "operation_results"},
                {"expected_launches": 2, "completed_launches": 2},
            ):
                with self.subTest(tampered_fields=sorted(changes)):
                    tampered = dict(functional)
                    tampered.update(changes)
                    with mock.patch.object(m2ndp.subprocess, "run") as run:
                        with self.assertRaisesRegex(
                            m2ndp.TraceTranslationError,
                            "FuncSim evidence cardinality differs",
                        ):
                            m2ndp.run_ndpsim_package(
                                package, functional_evidence=tampered,
                                calibration=timing_calibration,
                            )
                    run.assert_not_called()

            Path(functional["stdout_path"]).write_text(
                "tampered\n", encoding="utf-8"
            )
            with mock.patch.object(m2ndp.subprocess, "run") as run:
                with self.assertRaisesRegex(
                    m2ndp.TraceTranslationError,
                    "stdout log SHA-256 differs",
                ):
                    m2ndp.run_ndpsim_package(
                        package, functional_evidence=functional,
                        calibration=timing_calibration,
                    )
            run.assert_not_called()
            functional = functional_evidence(package)

            for stdout in (
                "EXPR FINISHED 10\nGantt info: host 0 finished NDP kernel X\n",
                "EXPR FINISHED 10\nMEMROY MATCH SUCCESS\n",
            ):
                with self.subTest(stdout=stdout):
                    shutil.rmtree(output.parent, ignore_errors=True)
                    def result(*_args, **_kwargs):
                        output.write_text("timing\n", encoding="utf-8")
                        return subprocess.CompletedProcess([], 0, stdout, "")
                    with mock.patch.object(
                        m2ndp.subprocess, "run", side_effect=result
                    ):
                        with self.assertRaisesRegex(
                            m2ndp.TraceTranslationError,
                            "timing evidence is incomplete",
                        ):
                            m2ndp.run_ndpsim_package(
                                package, functional_evidence=functional,
                                calibration=timing_calibration,
                            )

            shutil.rmtree(output.parent, ignore_errors=True)

            def successful_result(*_args, **_kwargs):
                output.write_text("timing\n", encoding="utf-8")
                return subprocess.CompletedProcess(
                    [], 0,
                    "EXPR FINISHED 10\nMEMROY MATCH SUCCESS\n"
                    "Gantt info: host 0 finished NDP kernel X\n",
                    "",
                )

            with mock.patch.object(
                m2ndp.subprocess, "run", side_effect=successful_result
            ):
                evidence = m2ndp.run_ndpsim_package(
                    package, functional_evidence=functional,
                    calibration=calibration(
                        package, cxl_link_delay="2us"
                    ),
                    cxl_link_delay="2us",
                )
            self.assertEqual(evidence["cxl_link_delay"], "2us")
            self.assertEqual(evidence["cxl_link_delay_ticks"], 2_000_000)
            self.assertEqual(evidence["memory_match"], "pass")

            shutil.rmtree(output.parent, ignore_errors=True)
            with mock.patch.object(m2ndp.subprocess, "run") as run:
                with self.assertRaisesRegex(
                    m2ndp.TraceTranslationError, "calibration CXL latency"
                ):
                    m2ndp.run_ndpsim_package(
                        package, functional_evidence=functional,
                        calibration=calibration(package),
                        cxl_link_delay="2us",
                    )
            run.assert_not_called()

    def test_ndpsim_rejects_cross_package_functional_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            packages = []
            for label in ("a", "b"):
                trace_root = root / f"trace-{label}"
                meta = {
                    "schema": 1, "workload": "pr_spmv",
                    "input_sha256": digest(f"input-{label}"),
                    "source_sha256": digest("source"),
                    "binary_sha256": digest("binary"),
                    "config_sha256": digest("trace-config"),
                    "phases": [{"id": 0}],
                    "output_boundaries": {"rank": {
                        "word_bits": 32, "count": 1,
                        "probes": [{"address": 0x1004,
                                    "after_sequence": 0}],
                    }},
                }
                canonical.write_bundle(
                    trace_root, meta,
                    (operation(canonical.Opcode.STORE_U32, 0,
                               address=0x1004, left=1, result=1),),
                    {"rank": (1,)}, initial_memory={"rank": {
                        "logical_base": 0x1004, "word_bits": 32,
                        "words": (0,),
                    }},
                )
                bundle = canonical.read_bundle(trace_root)
                packages.append(m2ndp.lower_bundle(
                    trace_root, root / f"package-{label}",
                    provenance=provenance(root / f"prov-{label}", bundle),
                ))
            with mock.patch.object(m2ndp.subprocess, "run") as run:
                with self.assertRaisesRegex(
                    m2ndp.TraceTranslationError,
                    "FuncSim evidence package differs",
                ):
                    m2ndp.run_ndpsim_package(
                        packages[1],
                        functional_evidence=functional_evidence(packages[0]),
                        calibration={
                            "passed": True, "cxl_delay": "1us",
                            "target_ns": "2012.652",
                            "measured_ns": "2012.625",
                            "residual_ns": "0.027",
                            "link_period_ns": "0.125",
                            "target_cxl_boundary_ticks": 2_012_652,
                        },
                    )
            run.assert_not_called()

    def test_ndpsim_rejects_stale_output_before_subprocess(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            trace_root = root / "trace"
            meta = {
                "schema": 1, "workload": "pr_spmv",
                "input_sha256": digest("input"),
                "source_sha256": digest("source"),
                "binary_sha256": digest("binary"),
                "config_sha256": digest("trace-config"),
                "phases": [{"id": 0}],
                "output_boundaries": {"rank": {
                    "word_bits": 32, "count": 1,
                    "probes": [{"address": 0x1004,
                                "after_sequence": 0}],
                }},
            }
            canonical.write_bundle(
                trace_root, meta,
                (operation(canonical.Opcode.STORE_U32, 0,
                           address=0x1004, left=1, result=1),),
                {"rank": (1,)}, initial_memory={"rank": {
                    "logical_base": 0x1004, "word_bits": 32, "words": (0,),
                }},
            )
            bundle = canonical.read_bundle(trace_root)
            package = m2ndp.lower_bundle(
                trace_root, root / "package",
                provenance=provenance(
                    root, bundle, seed_timing_output=True
                ),
            )
            runtime_root = root / "package/timing-run"

            def result(*_args, **_kwargs):
                self.assertFalse((runtime_root / "ndpsim.out").exists())
                self.assertFalse(
                    (runtime_root / "energy_ndpsim.out").exists()
                )
                return subprocess.CompletedProcess(
                    [], 0,
                    "EXPR FINISHED 10\nMEMROY MATCH SUCCESS\n"
                    "Gantt info: host 0 finished NDP kernel X\n",
                    "",
                )
            with mock.patch.object(m2ndp.subprocess, "run") as run:
                run.side_effect = result
                with self.assertRaisesRegex(
                    m2ndp.TraceTranslationError,
                    "timing evidence is incomplete",
                ):
                    m2ndp.run_ndpsim_package(
                        package,
                        functional_evidence=functional_evidence(package),
                        calibration=calibration(package),
                    )
            run.assert_called_once()

    @unittest.skipUnless(
        os.environ.get("M2NDP_REAL_FUNCSIM"),
        "set M2NDP_REAL_FUNCSIM for upstream integration proof",
    )
    def test_real_funcsim_executes_match_and_rejects_one_bit_mismatch(self):
        keep_root = os.environ.get("M2NDP_REAL_KEEP_ROOT")
        root_context = (
            contextlib.nullcontext(keep_root)
            if keep_root else tempfile.TemporaryDirectory()
        )
        with root_context as temporary:
            root = Path(temporary)
            root.mkdir(parents=True, exist_ok=True)
            funcsim = Path(os.environ["M2NDP_REAL_FUNCSIM"])
            ndpsim = Path(os.environ["M2NDP_REAL_NDPSIM"])
            config = Path(os.environ["M2NDP_REAL_CONFIG"])
            timing_config = Path(
                os.environ.get("M2NDP_REAL_TIMING_CONFIG", str(config))
            )
            patches = tuple(
                item for item in os.environ["M2NDP_REAL_PATCHES"].split(":")
                if item
            )
            for label, expected in (("pass", 0x40400000),
                                    ("mismatch", 0x40400001)):
                trace_root = root / f"trace-{label}"
                operations = (
                    operation(
                        canonical.Opcode.LOAD_U32, 0, address=0x1000,
                        left=0x3F800000, right=0, result=0x3F800000,
                    ),
                    operation(
                        canonical.Opcode.F32_ADD, 1, address=0,
                        left=0x3F800000, right=0x40000000,
                        result=0x40400000,
                    ),
                    operation(
                        canonical.Opcode.STORE_U32, 2, address=0x1004,
                        left=0x40400000, right=0, result=0x40400000,
                    ),
                    operation(
                        canonical.Opcode.COMMIT, 3, left=0, right=0,
                        result=0,
                    ),
                )
                meta = {
                    "schema": 1, "workload": "pr_spmv",
                    "input_sha256": digest("real-input"),
                    "source_sha256": digest("real-source"),
                    "binary_sha256": digest("real-binary"),
                    "config_sha256": digest("real-trace-config"),
                    "phases": [{"id": 0}],
                    "output_boundaries": {"rank": {
                        "word_bits": 32, "count": 1,
                        "probes": [{"address": 0x1004,
                                    "after_sequence": 2}],
                    }},
                }
                canonical.write_bundle(
                    trace_root, meta, operations, {"rank": (expected,)},
                    initial_memory={"rank": {
                        "logical_base": 0x1004, "word_bits": 32,
                        "words": (0,),
                    }, "source": {
                        "logical_base": 0x1000, "word_bits": 32,
                        "words": (0x3F800000,),
                    }},
                )
                bundle = canonical.read_bundle(trace_root)
                package_provenance = m2ndp.PackageProvenance(
                    trace_sha256=bundle.meta["trace_sha256"],
                    input_sha256=bundle.meta["input_sha256"],
                    funcsim_path=str(funcsim), ndpsim_path=str(ndpsim),
                    patch_paths=patches, config_path=str(config),
                    ndpsim_config_path=str(timing_config),
                )
                package = m2ndp.lower_bundle(
                    trace_root, root / f"package-{label}",
                    provenance=package_provenance,
                )
                if label == "pass":
                    evidence = m2ndp.run_funcsim_package(
                        package, evidence_path=root / "funcsim-evidence.json"
                    )
                    self.assertEqual(evidence["status"], "pass")
                    self.assertEqual(evidence["compared_words"], 1)
                    calibration_path = os.environ.get(
                        "M2NDP_REAL_CALIBRATION"
                    )
                    if calibration_path:
                        timing = m2ndp.run_ndpsim_package(
                            package, functional_evidence=evidence,
                            calibration=json.loads(
                                Path(calibration_path).read_text()
                            ),
                            evidence_path=root / "timing-evidence.json",
                        )
                        self.assertEqual(timing["status"], "pass")
                        self.assertGreater(timing["cycles"], 0)
                else:
                    with self.assertRaisesRegex(
                        m2ndp.TraceTranslationError, "bit-exact gate failed"
                    ):
                        m2ndp.run_funcsim_package(package)

    @unittest.skipUnless(
        os.environ.get("M2NDP_REAL_FUNCSIM"),
        "set M2NDP_REAL_FUNCSIM for upstream integration proof",
    )
    def test_real_funcsim_accepts_derived_window_operation_gate(self):
        keep_root = os.environ.get("M2NDP_REAL_DERIVED_KEEP_ROOT")
        root_context = (
            contextlib.nullcontext(keep_root)
            if keep_root else tempfile.TemporaryDirectory()
        )
        with root_context as temporary:
            root = Path(temporary)
            root.mkdir(parents=True, exist_ok=True)
            trace_root = root / "trace"
            operations = (
                operation(
                    canonical.Opcode.F32_ADD, 0, address=0,
                    left=0x3F800000, right=0x40000000,
                    result=0x40400000,
                ),
                operation(
                    canonical.Opcode.STORE_U32, 1, address=0x1004,
                    left=0x40400000, right=0, result=0x40400000,
                ),
                operation(
                    canonical.Opcode.COMMIT, 2, left=0, right=0, result=0,
                ),
            )
            canonical.write_bundle(
                trace_root,
                {
                    "schema": 1, "workload": "pr_spmv",
                    "input_sha256": digest("derived-input"),
                    "source_sha256": digest("derived-source"),
                    "binary_sha256": digest("derived-binary"),
                    "config_sha256": digest("derived-config"),
                    "phases": [{"id": 0}], "output_boundaries": {},
                    "source_trace_sha256": digest("full-trace"),
                    "window_index": 0, "warmup_start": 0,
                    "measure_start": 0, "measure_stop": 3,
                },
                operations, {},
                initial_memory={"rank": {
                    "logical_base": 0x1004, "word_bits": 32,
                    "words": (0,),
                }},
            )
            bundle = canonical.read_bundle(trace_root)
            package = m2ndp.lower_bundle(
                trace_root, root / "package",
                provenance=m2ndp.PackageProvenance(
                    trace_sha256=bundle.meta["trace_sha256"],
                    input_sha256=bundle.meta["input_sha256"],
                    funcsim_path=os.environ["M2NDP_REAL_FUNCSIM"],
                    ndpsim_path=os.environ["M2NDP_REAL_NDPSIM"],
                    patch_paths=tuple(
                        item for item in os.environ[
                            "M2NDP_REAL_PATCHES"
                        ].split(":") if item
                    ),
                    config_path=os.environ["M2NDP_REAL_CONFIG"],
                    ndpsim_config_path=os.environ.get(
                        "M2NDP_REAL_TIMING_CONFIG",
                        os.environ["M2NDP_REAL_CONFIG"],
                    ),
                ),
            )
            evidence = m2ndp.run_funcsim_package(
                package, evidence_path=root / "funcsim-evidence.json"
            )
            self.assertEqual(evidence["status"], "pass")
            self.assertEqual(evidence["functional_gate"], "operation_results")
            self.assertEqual(evidence["compared_words"], 0)
            self.assertEqual(evidence["compared_operations"], 3)
            calibration_path = os.environ.get("M2NDP_REAL_CALIBRATION")
            if calibration_path:
                timing = m2ndp.run_ndpsim_package(
                    package, functional_evidence=evidence,
                    calibration=json.loads(Path(calibration_path).read_text()),
                    evidence_path=root / "timing-evidence.json",
                )
                self.assertEqual(timing["status"], "pass")
                self.assertGreater(timing["cycles"], 0)

    def test_ndpsim_subprocess_is_unreachable_before_functional_gate(self):
        functional = {
            "status": "failed", "boundary_count": 1,
            "compared_words": 1, "expected_launches": 1,
            "completed_launches": 1, "returncode": 0,
        }
        calibration = {
            "passed": True, "cxl_delay": "1us",
            "target_ns": "2012.652", "measured_ns": "2012.625",
            "residual_ns": "0.027", "link_period_ns": "0.125",
            "target_cxl_boundary_ticks": 2_012_652,
        }
        with mock.patch.object(m2ndp.subprocess, "run") as run:
            with self.assertRaisesRegex(
                m2ndp.artifact_helpers.EvidenceError, "FuncSim"
            ):
                m2ndp.run_ndpsim_package(
                    "/does/not/exist/package.json",
                    functional_evidence=functional,
                    calibration=calibration,
                )
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
