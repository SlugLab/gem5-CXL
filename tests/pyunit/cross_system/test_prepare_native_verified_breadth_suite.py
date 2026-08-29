# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from scripts import prepare_native_verified_breadth_suite as prepare


class NativeVerifiedBreadthPreparationTest(unittest.TestCase):
    def test_formal_npb_uses_descriptor_commitment_without_expansion(self):
        bundle = SimpleNamespace(
            meta={"boundary_commitments": {"residual.iter1": "a" * 64}},
            dynamic_work={"primitive_records": 15_900_152_600},
        )
        capture = {"boundaries": (object(),)}
        with (
            mock.patch.object(
                prepare.builder,
                "_validate_npb_boundary_commitments",
                return_value=[{"native": 1}],
            ) as validate,
            mock.patch.object(
                prepare.builder.npb, "expanded_evidence"
            ) as forbidden_expansion,
        ):
            evidence = prepare.native_verified_npb_evidence(
                capture, "cg", bundle
            )
        validate.assert_called_once_with(
            capture, "cg", bundle.meta["boundary_commitments"]
        )
        forbidden_expansion.assert_not_called()
        self.assertEqual(
            evidence["expansion_policy"],
            "lazy-descriptor-native-verified",
        )
        self.assertEqual(evidence["primitive_records"], 15_900_152_600)
        self.assertEqual(
            evidence["lazy_boundary_map"],
            {"residual.iter1": "a" * 64},
        )

    def test_formal_npb_descriptor_requires_positive_work(self):
        bundle = SimpleNamespace(
            meta={"boundary_commitments": {"residual.iter1": "a" * 64}},
            dynamic_work={"primitive_records": 0},
        )
        with self.assertRaisesRegex(
            prepare.PreparationError, "primitive record count"
        ):
            prepare.native_verified_npb_evidence({}, "cg", bundle)

    def test_prepared_manifest_contract_is_exactly_six_workloads(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            code = root / "action.py"
            config = root / "config.ini"
            code.write_text("pass\n", encoding="utf-8")
            config.write_text("[system]\n", encoding="utf-8")
            workloads = {
                name: {
                    "trace_sha256": prepare.sha256_text(name),
                    "phases": {"roi": 8},
                    "actions": prepare.action_layout(
                        name, ("roi",), action_driver=code,
                    ),
                }
                for name in prepare.WORKLOADS
            }
            manifest = prepare.prepared_manifest(
                root=root,
                workloads=workloads,
                code_files={"action_driver": prepare.file_record(code)},
                config_files={"gem5_config": prepare.file_record(config)},
            )
        self.assertEqual(manifest["status"], "verified")
        self.assertEqual(set(manifest["workloads"]), set(prepare.WORKLOADS))
        self.assertEqual(manifest["threads"], 4)
        self.assertTrue(manifest["all_memory_cxl"])
        self.assertEqual(
            tuple(manifest["functional_systems"]),
            ("vanilla", "amu", "cira", "m2ndp-funcsim"),
        )
        self.assertEqual(
            tuple(manifest["timing_systems"]),
            ("vanilla", "amu", "cira", "m2ndp"),
        )
        for row in manifest["workloads"].values():
            self.assertTrue(row["actions"]["reference"]["command"])
            for system in manifest["functional_systems"]:
                self.assertTrue(
                    row["actions"]["functional"][system]["command"]
                )
            for phase in row["phases"]:
                for system in manifest["timing_systems"]:
                    action = row["actions"]["window"][phase][system]
                    self.assertIn("--cxl-link-delay", action["command"])
                    self.assertIn("{{cxl_link_delay}}", action["command"])

    def test_resume_rejects_checkpoint_not_bound_by_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            checkpoint = root / "cg.native-verified.json"
            checkpoint.write_text('{"status":"verified"}\n', encoding="utf-8")
            state = {"workloads": {"cg": {"status": "pending"}}}
            with self.assertRaisesRegex(
                prepare.PreparationError, "not committed by state"
            ):
                prepare._load_committed_checkpoint(
                    checkpoint, state, "cg"
                )

    def test_resume_rejects_checkpoint_hash_drift(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            checkpoint = root / "cg.native-verified.json"
            checkpoint.write_text('{"status":"verified"}\n', encoding="utf-8")
            state = {
                "workloads": {
                    "cg": {
                        "status": "verified",
                        "checkpoint": str(checkpoint.resolve()),
                        "checkpoint_sha256": "0" * 64,
                    }
                }
            }
            with self.assertRaisesRegex(
                prepare.PreparationError, "checkpoint hash differs"
            ):
                prepare._load_committed_checkpoint(
                    checkpoint, state, "cg"
                )

    def test_checkpoint_record_requires_native_verification_gate(self):
        with self.assertRaisesRegex(
            prepare.PreparationError, "checkpoint status differs"
        ):
            prepare._validate_npb_checkpoint_record(
                {"class": "D"}, Path("/source"), Path("/output"), "cg",
                {
                    "class": "D",
                    "allocated_bytes": 1,
                    "parameter_sha256": "1" * 64,
                },
            )


if __name__ == "__main__":
    unittest.main()
