# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from scripts import freeze_cross_system_inputs as freeze


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
    mcf_input = write(root / "mcf.in", b"mcf input")
    mcf_source = write(root / "mcf.cc", b"mcf source")
    amg_input = write(root / "amg.values", b"amg values")
    amg_index = write(root / "amg.index", b"amg index")
    lulesh_input = write(root / "lulesh.values", b"lulesh values")
    lulesh_index = write(root / "lulesh.index", b"lulesh index")
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
            "synthetic": False,
        },
        "amg_gather": {
            "input": str(amg_input),
            "input_sha256": sha256(amg_input),
            "index": str(amg_index),
            "index_sha256": sha256(amg_index),
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

    def test_spatter_requires_the_index_path_not_only_its_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            value = valid_record(Path(tmp))
            value["amg_gather"].pop("index")
            with self.assertRaisesRegex(
                freeze.InputError, "amg_gather.index"
            ):
                freeze.validate_paper_record(value)

    def test_bound_record_verifies_files_hashes_and_npb_commit(self):
        with tempfile.TemporaryDirectory() as tmp:
            value = valid_record(Path(tmp))
            result = freeze.validate_bound_inputs(value)
            self.assertEqual(tuple(result), freeze.WORKLOADS)
            self.assertEqual(result["npb_cg"]["source_commit"], value["npb_cg"]["source_commit"])
            self.assertEqual(result["amg_gather"]["index"], value["amg_gather"]["index"])

    def test_bound_record_rejects_hash_drift(self):
        with tempfile.TemporaryDirectory() as tmp:
            value = valid_record(Path(tmp))
            Path(value["mcf"]["input"]).write_bytes(b"changed")
            with self.assertRaisesRegex(freeze.InputError, "mcf input SHA-256"):
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
