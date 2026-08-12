# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from scripts import cross_system_contract as contract


def digest(label):
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def identity(suffix="base"):
    return contract.ExperimentIdentity(
        code_sha256=digest(f"code-{suffix}"),
        input_manifest_sha256=digest(f"input-{suffix}"),
        calibration_manifest_sha256=digest(f"calibration-{suffix}"),
        trace_sha256=digest(f"trace-{suffix}"),
        config_sha256=digest(f"config-{suffix}"),
    )


class ContractTest(unittest.TestCase):
    def test_identity_binds_every_semantic_input(self):
        value = identity()
        self.assertEqual(len(value.digest()), 64)
        changed = identity("changed")
        self.assertNotEqual(value.digest(), changed.digest())

    def test_identity_rejects_malformed_digest(self):
        with self.assertRaisesRegex(contract.ContractError, "code SHA-256"):
            contract.ExperimentIdentity(
                code_sha256="not-a-digest",
                input_manifest_sha256="b" * 64,
                calibration_manifest_sha256="c" * 64,
                trace_sha256="d" * 64,
                config_sha256="e" * 64,
            )

    def test_timing_before_functional_pass_is_illegal(self):
        with self.assertRaisesRegex(contract.ContractError, "transition"):
            contract.transition(
                {"status": "planned"}, "timing_in_progress"
            )

    def test_legal_path_reaches_complete(self):
        state = {"status": "planned"}
        state = contract.transition(state, "functional_pass")
        state = contract.transition(state, "timing_in_progress")
        state = contract.transition(state, "complete")
        self.assertEqual(state["status"], "complete")
        self.assertIn("complete", contract.TERMINAL)

    def test_changed_identity_requires_a_fresh_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            contract.bind_root(root, identity())
            with self.assertRaisesRegex(
                contract.ContractError, "fresh evidence root"
            ):
                contract.bind_root(root, identity("changed"))

    def test_atomic_json_is_canonical_and_replaces_existing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "record.json"
            contract.atomic_write_json(path, {"z": 1, "a": 2})
            self.assertEqual(path.read_bytes(), b'{"a":2,"z":1}\n')
            contract.atomic_write_json(path, {"replacement": True})
            self.assertEqual(
                json.loads(path.read_text(encoding="utf-8")),
                {"replacement": True},
            )
            self.assertEqual(list(path.parent.glob(f".{path.name}.*")), [])

    def test_newer_corrupt_checkpoint_does_not_hide_newest_valid(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = root / "first.bin"
            second = root / "second.bin"
            corrupt = root / "corrupt.bin"
            first.write_bytes(b"first")
            second.write_bytes(b"second")
            corrupt.write_bytes(b"changed")
            identity_sha256 = identity().digest()
            records = [
                self.record(1, identity_sha256, first, digest("first")),
                self.record(2, identity_sha256, second, digest("second")),
                self.record(3, identity_sha256, corrupt, digest("expected")),
            ]
            selected, rejected = contract.select_resume_checkpoint(
                records, identity_sha256
            )
            self.assertEqual(selected["sequence"], 2)
            self.assertEqual([row["sequence"] for row in rejected], [3])

    def test_identity_drift_selects_no_checkpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "phase.bin"
            output.write_bytes(b"phase")
            record = self.record(
                1, identity().digest(), output, digest("phase")
            )
            selected, rejected = contract.select_resume_checkpoint(
                [record], identity("changed").digest()
            )
            self.assertIsNone(selected)
            self.assertEqual(rejected, (record,))

    @staticmethod
    def record(sequence, identity_sha256, path, sha256):
        return {
            "sequence": sequence,
            "identity_sha256": identity_sha256,
            "boundary": "window",
            "outputs": {
                "payload": {"path": str(path.resolve()), "sha256": sha256}
            },
        }


if __name__ == "__main__":
    unittest.main()
