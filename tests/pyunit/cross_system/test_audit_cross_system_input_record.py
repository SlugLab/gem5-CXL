# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

import dataclasses
import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import audit_cross_system_input_record as audit
from scripts import freeze_cross_system_inputs as freeze
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
    amg_input = write(root / "amg.values", b"amg values")
    amg_index = write(root / "amg.index", b"amg index")
    lulesh_input = write(root / "lulesh.values", b"lulesh values")
    lulesh_index = write(root / "lulesh.index", b"lulesh index")
    npb_root, commit, cg_params, mg_params = make_git_source(root)
    return {
        "pr_spmv": {
            "input": str(graph), "input_sha256": sha256(graph),
            "allocated_bytes": 240_000_000, "scale": 20,
        },
        "mcf": {
            "input": str(mcf_input), "input_sha256": sha256(mcf_input),
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
        "amg_gather": {
            "input": str(amg_input), "input_sha256": sha256(amg_input),
            "index": str(amg_index), "index_sha256": sha256(amg_index),
            "allocated_bytes": 1 << 30,
        },
        "lulesh_scatter": {
            "input": str(lulesh_input),
            "input_sha256": sha256(lulesh_input),
            "index": str(lulesh_index),
            "index_sha256": sha256(lulesh_index),
            "allocated_bytes": 1 << 30,
        },
        "npb_cg": {
            "source_root": str(npb_root), "source_commit": commit,
            "parameter_file": str(cg_params),
            "parameter_sha256": sha256(cg_params),
            "allocated_bytes": 12_800_000_000, "class": "C",
        },
        "npb_mg": {
            "source_root": str(npb_root), "source_commit": commit,
            "parameter_file": str(mg_params),
            "parameter_sha256": sha256(mg_params),
            "allocated_bytes": 12_800_000_000, "class": "C",
        },
    }


class InputAuditTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.graph = write(self.root / "g20.sg", b"graph")

    def test_template_has_exact_six_workload_shape(self):
        value = audit.template_record()
        self.assertEqual(set(value), set(freeze.WORKLOADS))
        self.assertEqual(value["pr_spmv"]["scale"], 20)
        self.assertEqual(value["mcf"]["synthetic"], False)
        self.assertEqual(value["amg_gather"]["allocated_bytes"], 1 << 30)
        self.assertEqual(value["npb_cg"]["allocated_bytes"], 12_800_000_000)

    def test_template_mcf_shape_matches_formal_required_shape(self):
        self.assertEqual(
            set(audit.template_record()["mcf"]), freeze.REQUIRED["mcf"]
        )

    @mock.patch.object(audit.subprocess, "check_output", return_value="clean\n")
    def test_git_inspection_scopes_safe_directory_to_source_root(self, check):
        source = (self.root / "npb").resolve()
        source.mkdir()
        self.assertEqual(audit._git_output(source, "rev-parse", "HEAD"), "clean")
        command = check.call_args.args[0]
        self.assertEqual(command[:4], (
            "git", "-c", f"safe.directory={source}", "-C",
        ))
        self.assertEqual(command[4:], (str(source), "rev-parse", "HEAD"))

    def test_incomplete_candidate_is_never_accepted(self):
        candidate = {"pr_spmv": {
            "input": str(self.graph),
            "input_sha256": sha256(self.graph),
            "allocated_bytes": 240_000_000,
            "scale": 20,
        }}
        result = audit.audit_record(candidate)
        self.assertEqual(result["status"], "incomplete")
        self.assertIn("mcf", result["missing_workloads"])
        self.assertEqual(
            result["workloads"]["pr_spmv"]["observed"]["input_sha256"],
            sha256(self.graph),
        )

    def test_complete_live_candidate_is_ready_but_not_accepted(self):
        candidate = valid_record(self.root)
        result = audit.audit_record(candidate)
        self.assertEqual(result["status"], "ready_for_freeze")
        self.assertNotEqual(result["status"], "accepted")
        self.assertEqual(
            freeze.validate_bound_inputs(candidate)["npb_cg"]["source_commit"],
            candidate["npb_cg"]["source_commit"],
        )

    def test_cli_writes_both_nonaccepted_records_for_missing_candidate(self):
        discovery = self.root / "discovery.json"
        template = self.root / "template.json"
        status = audit.main([
            "--candidate-record", str(self.root / "missing.json"),
            "--discovery-output", str(discovery),
            "--template-output", str(template),
        ])
        self.assertEqual(status, 2)
        self.assertEqual(json.loads(discovery.read_text())["status"], "incomplete")
        self.assertEqual(
            set(json.loads(template.read_text())), set(freeze.WORKLOADS)
        )


if __name__ == "__main__":
    unittest.main()
