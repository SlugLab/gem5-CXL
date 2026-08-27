# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

import dataclasses
import hashlib
import json
import struct
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from scripts import build_matched_breadth_workloads as builder
from scripts import cross_system_contract as contract
from scripts import freeze_cross_system_inputs as freeze
from scripts import generate_formal_spatter_inputs as spatter
from scripts import generate_mcfreg2_state as generator
from scripts import mcfreg2
from test_mcfreg2 import MCFREG2Test


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path.resolve()


def make_git_source(root):
    source = root / "npb"
    source.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=source, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=source,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"], cwd=source, check=True
    )
    cg = write(source / "config/cg.params", b"CG exact parameters\n")
    mg = write(source / "config/mg.params", b"MG exact parameters\n")
    subprocess.run(["git", "add", "."], cwd=source, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "fixture"], cwd=source, check=True
    )
    commit = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=source, text=True
    ).strip()
    return source.resolve(), commit, cg, mg


def make_spatter_record(root, workload):
    root = Path(root)
    mode = "gather" if workload == "amg_gather" else "scatter"
    kernel = "Gather" if mode == "gather" else "Scatter"
    trace_path = write(
        root / f"{workload}.source.json",
        (json.dumps([
            {"kernel": kernel, "count": 1, "delta": 1,
             "pattern": [0, 3]},
        ]) + "\n").encode("utf-8"),
    )
    values_count = 4 if mode == "gather" else 2
    values = struct.pack(
        f"<{values_count}I",
        *(spatter.value_bits(index) for index in range(values_count)),
    )
    index = struct.pack("<2Q", 0, 3)
    values_sha = hashlib.sha256(values).hexdigest()
    index_sha = hashlib.sha256(index).hexdigest()
    identity = {
        "schema": 1,
        "source_kind": "official_spatter_application_trace",
        "workload": workload,
        "mode": mode,
        "selected_kernel": kernel,
        "source_trace": str(trace_path),
        "source_trace_sha256": sha256(trace_path),
        "source_commit": "a" * 40,
        "generator_sha256": sha256(Path(spatter.__file__)),
        "expansion_version": spatter.EXPANSION_VERSION,
        "selection_rule": f"all {kernel} records in source order",
        "minimum_bytes": 1,
        "epochs": 1,
        "values_count": values_count,
        "index_count": 2,
        "maximum_index": 3,
        "resident_bytes": 40,
        "values_sha256": values_sha,
        "index_sha256": index_sha,
    }
    artifact_id = hashlib.sha256(contract.canonical_json(identity)).hexdigest()
    artifact = root / workload / artifact_id
    artifact.mkdir(parents=True)
    values_path = write(artifact / "values.f32le", values)
    index_path = write(artifact / "index.u64le", index)
    binary = write(root / "spatter-reference", b"reference binary")
    matched = Path(freeze.__file__).resolve().parents[1] / "util/amu/matched_workloads"
    validation = {
        "schema": 1,
        "status": "accepted",
        "workload": workload,
        "mode": mode,
        "values_sha256": values_sha,
        "index_sha256": index_sha,
        "destination_sha256": "d" * 64,
        "output_words": 2 if mode == "gather" else 4,
        "reference_binary": str(binary),
        "reference_binary_sha256": sha256(binary),
        "reference_source_sha256": sha256(matched / "spatter_regions.cc"),
        "trace_abi_sha256": sha256(matched / "canonical_trace.hh"),
        "command_sha256": "1" * 64,
        "stdout_sha256": "2" * 64,
    }
    validation_path = artifact / "validation.json"
    contract.atomic_write_json(validation_path, validation)
    provenance = {
        **identity,
        "status": "accepted",
        "artifact_id": artifact_id,
        "artifacts": {
            "values": {
                "name": "values.f32le", "sha256": values_sha,
                "size_bytes": len(values),
            },
            "index": {
                "name": "index.u64le", "sha256": index_sha,
                "size_bytes": len(index),
            },
        },
        "independent_regeneration": {
            "status": "pass", "values_sha256": values_sha,
            "index_sha256": index_sha,
        },
        "validation": {
            "name": "validation.json", "sha256": sha256(validation_path),
        },
    }
    provenance_path = artifact / "provenance.json"
    contract.atomic_write_json(provenance_path, provenance)
    return {
        "input": str(values_path),
        "input_sha256": values_sha,
        "index": str(index_path),
        "index_sha256": index_sha,
        "allocated_bytes": 1 << 30,
        "synthetic": False,
        "provenance": str(provenance_path.resolve()),
        "provenance_sha256": sha256(provenance_path),
        "validation": str(validation_path.resolve()),
        "validation_sha256": sha256(validation_path),
        "artifact_id": artifact_id,
    }


def use_fixture_spatter_capacity(value):
    for workload in ("amg_gather", "lulesh_scatter"):
        provenance = json.loads(
            Path(value[workload]["provenance"]).read_text(encoding="utf-8")
        )
        value[workload]["allocated_bytes"] = provenance["resident_bytes"]


def valid_record(root):
    graph = write(root / "g20.sg", b"graph")
    mcf_source = write(root / "mcf.cc", b"mcf source")
    source_commit = "2b30de22399402d8c44bd74b8ebf743b6a6a55e9"
    source_tree_sha256 = "1" * 64
    identity = {
        "source_commit": source_commit,
        "source_tree_sha256": source_tree_sha256,
        "input_sha256": "2" * 64,
        "common_patch_sha256": "3" * 64,
        "capture_patch_sha256": "4" * 64,
        "compiler_sha256": "5" * 64,
    }
    final_state_sha256 = "6" * 64
    mcf_output_sha256 = "7" * 64
    provenance = generator._canonical_json({
        "schema": 1, **identity,
    })
    final = generator._canonical_json({
        "schema": 1,
        "initial_state_sha256": "8" * 64,
        "final_state_sha256": final_state_sha256,
        "final_network_words": [0],
        "mcf_output_bytes": 1,
        "mcf_output_sha256": mcf_output_sha256,
        "peak_allocated_bytes": 345_000_000,
    })
    mcf_input = root / "mcf.reg2"
    helper = MCFREG2Test()
    package = helper.strict_semantic_fixture(pricing_only=False)
    replacements = {
        mcfreg2.SECTION_TYPES["PROVENANCE"]: provenance,
        mcfreg2.SECTION_TYPES["FINAL"]: final,
    }
    package = dataclasses.replace(
        package,
        sections=tuple(
            dataclasses.replace(
                section,
                data=replacements[section.section_type],
                element_size=len(replacements[section.section_type]),
            )
            if section.section_type in replacements else section
            for section in package.sections
        ),
    )
    mcfreg2.write_package(
        mcf_input,
        package,
    )
    mcf_input = mcf_input.resolve()
    validation = {
        "schema": 2,
        "status": "accepted",
        "identity": identity,
        "source_sha256": sha256(mcf_source),
        "package_sha256": sha256(mcf_input),
        "primary_package_sha256": sha256(mcf_input),
        "replay_package_sha256": sha256(mcf_input),
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
    validation_path = write(
        root / "validation.json",
        generator._canonical_json(validation),
    )
    amg = make_spatter_record(root, "amg_gather")
    lulesh = make_spatter_record(root, "lulesh_scatter")
    npb_root, commit, cg_params, mg_params = make_git_source(root)
    return {
        "pr_spmv": {
            "input": str(graph),
            "input_sha256": sha256(graph),
            "allocated_bytes": 240_000_000,
            "scale": 20,
        },
        "mcf": {
            "input": str(mcf_input),
            "input_sha256": sha256(mcf_input),
            "allocated_bytes": 345_000_000,
            "source": str(mcf_source),
            "source_sha256": sha256(mcf_source),
            "format": "MCFREG2",
            "source_commit": source_commit,
            "source_tree_sha256": source_tree_sha256,
            "validation": str(validation_path),
            "validation_sha256": sha256(validation_path),
            "synthetic": False,
        },
        "amg_gather": amg,
        "lulesh_scatter": lulesh,
        "npb_cg": {
            "source_root": str(npb_root),
            "source_commit": commit,
            "parameter_file": str(cg_params),
            "parameter_sha256": sha256(cg_params),
            "allocated_bytes": 12_800_000_000,
            "class": "paper-exact",
        },
        "npb_mg": {
            "source_root": str(npb_root),
            "source_commit": commit,
            "parameter_file": str(mg_params),
            "parameter_sha256": sha256(mg_params),
            "allocated_bytes": 12_800_000_000,
            "class": "paper-exact",
        },
    }


class FreezeInputTest(unittest.TestCase):
    def test_structural_but_semantically_arbitrary_mcf_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            value = valid_record(Path(tmp))
            row = value["mcf"]
            package_path = Path(row["input"])
            package = mcfreg2.read_package(package_path)
            package = dataclasses.replace(
                package,
                sections=tuple(
                    dataclasses.replace(
                        section, data=b"arbitrary\n", element_count=1,
                        element_size=0,
                    )
                    if section.section_type ==
                    mcfreg2.SECTION_TYPES["EVENTS"] else section
                    for section in package.sections
                ),
                header=dataclasses.replace(package.header, event_count=1),
            )
            row["input_sha256"] = mcfreg2.write_package(
                package_path, package
            )
            validation_path = Path(row["validation"])
            validation = json.loads(
                validation_path.read_text(encoding="utf-8")
            )
            for name in (
                "package_sha256",
                "primary_package_sha256",
                "replay_package_sha256",
            ):
                validation[name] = row["input_sha256"]
            validation_path.write_bytes(generator._canonical_json(validation))
            row["validation_sha256"] = sha256(validation_path)
            with self.assertRaisesRegex(freeze.InputError, "semantic replay"):
                freeze.validate_mcf_record(row)

    def test_mcf_package_toctou_during_replay_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            value = valid_record(Path(tmp))
            row = value["mcf"]

            def mutate_after_snapshot(package_path, output_root):
                Path(row["input"]).write_bytes(b"changed during replay")
                return {
                    "boundary_mismatches": 0,
                    "operations": 1,
                    "price_out_calls": 1,
                    "pricing_calls": 1,
                    "status": "verified",
                    "trace_sha256": "1" * 64,
                }

            with mock.patch.object(
                freeze, "run_strict_mcfreg2_replay",
                side_effect=mutate_after_snapshot,
            ):
                with self.assertRaisesRegex(
                    freeze.InputError, "changed during semantic replay"
                ):
                    freeze.validate_mcf_record(row)

    def test_npb_paper_minimum_is_12_8_gb(self):
        self.assertEqual(
            freeze.MINIMUM_ALLOCATED_BYTES["npb_cg"], 12_800_000_000
        )
        self.assertEqual(
            freeze.MINIMUM_ALLOCATED_BYTES["npb_mg"], 12_800_000_000
        )
        with tempfile.TemporaryDirectory() as tmp:
            value = valid_record(Path(tmp))
            value["npb_cg"]["allocated_bytes"] = 12_000_000_000
            with self.assertRaisesRegex(freeze.InputError, "paper input size"):
                freeze.validate_paper_record(value)

    @mock.patch.object(freeze.subprocess, "check_output", return_value="clean\n")
    def test_git_inspection_scopes_safe_directory_to_source_root(self, check):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp).resolve()
            self.assertEqual(
                freeze._git_output(source, "rev-parse", "HEAD"), "clean"
            )
            command = check.call_args.args[0]
            self.assertEqual(command[:4], (
                "git", "-c", f"safe.directory={source}", "-C",
            ))
            self.assertEqual(
                command[4:], (str(source), "rev-parse", "HEAD")
            )

    def test_missing_paper_record_fails_instead_of_inferring_size(self):
        with tempfile.TemporaryDirectory() as tmp:
            options = SimpleNamespace(
                paper_input_record=Path(tmp) / "missing.json",
                graph_manifests=(),
            )
            with self.assertRaisesRegex(freeze.InputError, "paper input record"):
                freeze.freeze_inputs(options)

    def test_frozen_output_is_consumable_by_formal_npb_builder(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paper = write(root / "paper.json", b"{}\n")
            graph_paths = tuple(
                write(root / f"g{scale}.json", b"{}\n")
                for scale in (4, 12, 14, 20)
            )
            rows = {
                name: {
                    "source_root": str(root / "npb"),
                    "source_commit": "a" * 40,
                    "parameter_file": str(root / f"{short}.params"),
                    "parameter_sha256": "b" * 64,
                    "allocated_bytes": 12_800_000_000,
                    "class": "D",
                }
                for short, name in (("cg", "npb_cg"), ("mg", "npb_mg"))
            }
            graphs = tuple(
                SimpleNamespace(
                    scale=scale,
                    graph=str(root / f"g{scale}.sg"),
                    graph_sha256=f"{scale:064x}",
                    num_nodes=1 << scale,
                    directed_edges=scale,
                )
                for scale in (4, 12, 14, 20)
            )
            options = SimpleNamespace(
                paper_input_record=paper,
                graph_manifests=graph_paths,
            )
            with mock.patch.object(
                freeze, "validate_bound_inputs", return_value=rows,
            ), mock.patch.object(
                freeze.profiles, "load_scaling_graphs", return_value=graphs,
            ):
                frozen = freeze.freeze_inputs(options)
            frozen_path = root / "inputs.json"
            frozen_path.write_text(json.dumps(frozen), encoding="utf-8")

            loaded, _digest = builder.load_frozen_npb_inputs(frozen_path)

            self.assertEqual(set(loaded), {"cg", "mg"})

    def test_npb_requires_parameter_hash_and_allocated_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            value = valid_record(Path(tmp))
            value["npb_cg"].pop("parameter_sha256")
            with self.assertRaisesRegex(
                freeze.InputError, "npb_cg.parameter_sha256"
            ):
                freeze.validate_paper_record(value)

    def test_synthetic_mcf_hardware_microbenchmark_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            value = valid_record(Path(tmp))
            value["mcf"]["synthetic"] = True
            with self.assertRaisesRegex(freeze.InputError, "synthetic"):
                freeze.validate_paper_record(value)

    def test_mcf_requires_accepted_bit_exact_mcfreg2_evidence(self):
        mutations = (
            ("format", "MCFREG1", "MCFREG2"),
            ("validation", None, "validation"),
            ("status", "candidate", "accepted"),
            ("boundary_mismatches", 1, "boundary"),
            ("primary_replay_equal", False, "primary/replay"),
        )
        for field, replacement, message in mutations:
            with self.subTest(field=field), tempfile.TemporaryDirectory() as tmp:
                value = valid_record(Path(tmp))
                if field == "format":
                    value["mcf"][field] = replacement
                elif field == "validation":
                    value["mcf"].pop(field)
                else:
                    path = Path(value["mcf"]["validation"])
                    validation = json.loads(path.read_text(encoding="utf-8"))
                    validation[field] = replacement
                    path.write_bytes(generator._canonical_json(validation))
                    value["mcf"]["validation_sha256"] = sha256(path)
                with self.assertRaisesRegex(freeze.InputError, message):
                    freeze.validate_bound_inputs(value)

    def test_mcf_rejects_package_validation_hash_and_allocation_drift(self):
        with tempfile.TemporaryDirectory() as tmp:
            value = valid_record(Path(tmp))
            value["mcf"]["input_sha256"] = "0" * 64
            with self.assertRaisesRegex(freeze.InputError, "package|input"):
                freeze.validate_bound_inputs(value)

        with tempfile.TemporaryDirectory() as tmp:
            value = valid_record(Path(tmp))
            value["mcf"]["validation_sha256"] = "0" * 64
            with self.assertRaisesRegex(freeze.InputError, "validation"):
                freeze.validate_bound_inputs(value)

        with tempfile.TemporaryDirectory() as tmp:
            value = valid_record(Path(tmp))
            value["mcf"]["allocated_bytes"] = 344_999_999
            with self.assertRaisesRegex(freeze.InputError, "allocated"):
                freeze.validate_bound_inputs(value)

    def test_spatter_requires_the_index_path_not_only_its_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            value = valid_record(Path(tmp))
            value["amg_gather"].pop("index")
            with self.assertRaisesRegex(
                freeze.InputError, "amg_gather.index"
            ):
                freeze.validate_paper_record(value)

    def test_spatter_rejects_provenance_source_hash_tampering(self):
        with tempfile.TemporaryDirectory() as tmp:
            value = valid_record(Path(tmp))
            path = Path(value["amg_gather"]["provenance"])
            provenance = json.loads(path.read_text(encoding="utf-8"))
            provenance["source_trace_sha256"] = "0" * 64
            contract.atomic_write_json(path, provenance)
            value["amg_gather"]["provenance_sha256"] = sha256(path)
            with self.assertRaisesRegex(freeze.InputError, "provenance|source"):
                freeze.validate_bound_inputs(value)

    def test_spatter_rejects_failed_reference_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            value = valid_record(Path(tmp))
            path = Path(value["lulesh_scatter"]["validation"])
            validation = json.loads(path.read_text(encoding="utf-8"))
            validation["status"] = "failed"
            contract.atomic_write_json(path, validation)
            value["lulesh_scatter"]["validation_sha256"] = sha256(path)
            use_fixture_spatter_capacity(value)
            with mock.patch.dict(
                freeze.MINIMUM_ALLOCATED_BYTES,
                {"amg_gather": 1, "lulesh_scatter": 1},
            ):
                with self.assertRaisesRegex(freeze.InputError, "validation"):
                    freeze.validate_bound_inputs(value)

    def test_spatter_rejects_declared_allocation_drift(self):
        with tempfile.TemporaryDirectory() as tmp:
            value = valid_record(Path(tmp))
            value["amg_gather"]["allocated_bytes"] = (1 << 30) + 1
            with self.assertRaisesRegex(freeze.InputError, "allocated"):
                freeze.validate_bound_inputs(value)

    def test_bound_record_verifies_files_hashes_and_npb_commit(self):
        with tempfile.TemporaryDirectory() as tmp:
            value = valid_record(Path(tmp))
            use_fixture_spatter_capacity(value)
            with mock.patch.dict(
                freeze.MINIMUM_ALLOCATED_BYTES,
                {"amg_gather": 1, "lulesh_scatter": 1},
            ):
                result = freeze.validate_bound_inputs(value)
            self.assertEqual(tuple(result), freeze.WORKLOADS)
            self.assertEqual(result["npb_cg"]["source_commit"], value["npb_cg"]["source_commit"])
            self.assertEqual(result["amg_gather"]["index"], value["amg_gather"]["index"])

    def test_bound_record_rejects_hash_drift(self):
        with tempfile.TemporaryDirectory() as tmp:
            value = valid_record(Path(tmp))
            Path(value["mcf"]["input"]).write_bytes(b"changed")
            with self.assertRaisesRegex(
                freeze.InputError, "mcf package/input SHA-256"
            ):
                freeze.validate_bound_inputs(value)

    def test_cli_writes_terminal_failed_input_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root / "inputs.json"
            status = freeze.main(
                [
                    "--paper-input-record", str(root / "missing.json"),
                    "--output", str(output),
                ]
            )
            self.assertEqual(status, 2)
            failure = json.loads(
                output.with_name("failed-input.json").read_text(encoding="utf-8")
            )
            self.assertEqual(failure["status"], "failed_input")
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
