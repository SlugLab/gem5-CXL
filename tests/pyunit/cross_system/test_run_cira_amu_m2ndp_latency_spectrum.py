# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

import hashlib
import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from scripts import cross_system_contract as contract
from scripts import run_cira_amu_m2ndp_latency_spectrum as spectrum
from scripts import run_pr_asymmetric_offload as offload


def sha(label):
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def identity():
    return contract.ExperimentIdentity(
        code_sha256=sha("code"),
        input_manifest_sha256=sha("inputs"),
        calibration_manifest_sha256=sha("calibration"),
        trace_sha256=sha("prepared"),
        config_sha256=sha("config"),
    )


def qualification(calibration_sha256):
    vanilla_ticks = 1_600_000
    rows = {
        "g12:vanilla": {
            "scale": 12, "system": "vanilla", "sim_ticks": vanilla_ticks,
        },
        "g12:amu": {
            "scale": 12, "system": "amu",
            "sim_ticks": int(Decimal(vanilla_ticks) / Decimal("1.42")),
        },
        "g12:cira-few-shot": {
            "scale": 12, "system": "cira-few-shot",
            "sim_ticks": int(Decimal(vanilla_ticks) / Decimal("1.45")),
            "selected_candidate": "B",
        },
        "g12:m2ndp": {
            "scale": 12, "system": "m2ndp",
            "ndpsim_cycles": int(
                Decimal(vanilla_ticks) / Decimal("2.67")
            ),
            "ndpsim_core_period_seconds": "1e-12",
        },
    }
    common = {
        "profile": "pr-offload-4thread-1us",
        "cxl_link_delay": "1us",
        "workers": 4,
        "iterations": 20,
        "all_memory_cxl": True,
        "verification": "pass",
        "raw_sha256": sha("rank-bits"),
        "worker_completions": [40, 40, 40, 40],
        "pending": {"all": 0},
    }
    for row in rows.values():
        row.update({key: value for key, value in common.items() if key not in row})
    cira_ticks = rows["g12:cira-few-shot"]["sim_ticks"]
    rows["g12:cira-few-shot"]["phases"] = {
        "formation": 1,
        "sampling": 1,
        "selection": 1,
        "jit": 1,
        "execution": cira_ticks - 5,
        "drain": 1,
    }
    rows["g12:cira-few-shot"]["phase_total_ns"] = cira_ticks
    rows["g12:m2ndp"]["funcsim"] = {
        "status": "pass",
        "compared": 1 << 12,
        "mismatched": 0,
        "completed_at_seq": 1,
    }
    rows["g12:m2ndp"]["ndpsim_started_at_seq"] = 2
    return {
        "schema": 1,
        "status": "passed",
        "profile": "pr-offload-4thread-1us",
        "identity": {"calibration_sha256": calibration_sha256},
        "performance_gate": offload.qualification_gate(rows),
        "primary": rows,
        "replay": json.loads(json.dumps(rows)),
    }


class LatencySpectrumRunnerTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.shared = {}
        for name in ("inputs", "calibration", "prepared"):
            path = self.root / "shared" / f"{name}.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(name, encoding="utf-8")
            self.shared[name] = {
                "path": str(path.resolve()),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        self.qualification = self.root / "shared/qualification.json"
        contract.atomic_write_json(
            self.qualification,
            qualification(self.shared["calibration"]["sha256"]),
        )

    def _qualification_record(self):
        return spectrum.validate_qualification(
            self.qualification, self.shared["calibration"]["sha256"]
        )

    def test_matrix_is_four_latencies_by_six_workloads_by_four_systems(self):
        self.assertEqual(
            spectrum.WORKLOADS,
            (
                "pr_spmv",
                "gap_bc",
                "mcf",
                "amg_gather",
                "lulesh_scatter",
                "npb_cg",
            ),
        )
        self.assertEqual(
            spectrum.coordinates(),
            tuple(
                (latency, workload, system)
                for latency in ("200ns", "500ns", "1us", "2us")
                for workload in spectrum.WORKLOADS
                for system in ("vanilla", "amu", "cira", "m2ndp")
            ),
        )
        self.assertEqual(len(spectrum.coordinates()), 96)

    def test_new_state_binds_verified_shared_objects_and_four_roots(self):
        record = self._qualification_record()
        state = spectrum.new_state(self.shared, record, identity())
        self.assertEqual(state["status"], "planned")
        self.assertEqual(set(state["shared"]), set(self.shared))
        self.assertEqual(state["qualification"], record)
        self.assertEqual(
            state["latencies"],
            {
                label: {"status": "pending", "root": f"latency/{label}"}
                for label in ("200ns", "500ns", "1us", "2us")
            },
        )

    def test_native_verified_policy_is_bound_to_state_identity_and_command(self):
        record = self._qualification_record()
        strict_identity = spectrum._aggregate_identity(
            self.shared, record, correctness_policy="bit-exact"
        )
        relaxed_identity = spectrum._aggregate_identity(
            self.shared, record, correctness_policy="native-verified"
        )
        self.assertNotEqual(strict_identity.digest(), relaxed_identity.digest())
        state = spectrum.new_state(
            self.shared, record, relaxed_identity,
            correctness_policy="native-verified",
        )
        self.assertEqual(state["correctness_policy"], "native-verified")
        command = spectrum._child_command(
            self.shared, record, self.root / "child", "500ns", resume=False,
            correctness_policy="native-verified",
        )
        self.assertEqual(
            command[command.index("--correctness-policy") + 1],
            "native-verified",
        )
        self.assertEqual(
            command[command.index("--qualification") + 1],
            str(self.qualification.resolve()),
        )

    def test_non_content_addressed_shared_object_is_rejected(self):
        broken = dict(self.shared)
        broken["inputs"] = {"path": self.shared["inputs"]["path"]}
        with self.assertRaisesRegex(
            spectrum.SpectrumError, "shared objects are not content-addressed"
        ):
            spectrum.new_state(broken, self._qualification_record(), identity())
        broken = json.loads(json.dumps(self.shared))
        broken["prepared"]["sha256"] = sha("changed")
        with self.assertRaisesRegex(
            spectrum.SpectrumError, "shared objects are not content-addressed"
        ):
            spectrum.new_state(broken, self._qualification_record(), identity())

    def test_qualification_requires_passed_performance_gate(self):
        value = qualification(self.shared["calibration"]["sha256"])
        value["performance_gate"]["status"] = "failed"
        contract.atomic_write_json(self.qualification, value)
        with self.assertRaisesRegex(
            spectrum.SpectrumError, "qualification performance gate"
        ):
            self._qualification_record()

    def test_qualification_requires_matching_calibration(self):
        with self.assertRaisesRegex(
            spectrum.SpectrumError, "qualification calibration differs"
        ):
            spectrum.validate_qualification(self.qualification, sha("other"))

    def test_qualification_requires_identical_primary_and_replay(self):
        value = qualification(self.shared["calibration"]["sha256"])
        value["replay"]["g12:amu"]["raw_sha256"] = sha("changed-rank")
        contract.atomic_write_json(self.qualification, value)
        with self.assertRaisesRegex(
            spectrum.SpectrumError, "qualification replay differs"
        ):
            self._qualification_record()

    def _child(
        self, label, *, status="complete", parent=None,
        correctness_policy="bit-exact",
    ):
        parent = self.root if parent is None else Path(parent)
        root = parent / "latency" / label
        root.mkdir(parents=True, exist_ok=True)
        child_identity = contract.ExperimentIdentity(
            code_sha256=sha("child-code"),
            input_manifest_sha256=self.shared["inputs"]["sha256"],
            calibration_manifest_sha256=self.shared["calibration"]["sha256"],
            trace_sha256=self.shared["prepared"]["sha256"],
            config_sha256=sha(f"config-{label}"),
        )
        contract.atomic_write_json(root / "identity.json", {
            "schema": 1,
            "digest": child_identity.digest(),
            "identity": {
                "code_sha256": child_identity.code_sha256,
                "input_manifest_sha256": child_identity.input_manifest_sha256,
                "calibration_manifest_sha256": (
                    child_identity.calibration_manifest_sha256
                ),
                "trace_sha256": child_identity.trace_sha256,
                "config_sha256": child_identity.config_sha256,
            },
        })
        contract.atomic_write_json(root / f"{status}.json", {
            "schema": 1,
            "status": status,
            "identity_sha256": child_identity.digest(),
            "cxl_link_delay": label,
            "cxl_link_delay_ticks": spectrum.latency.ticks(label),
            "correctness_policy": correctness_policy,
        })
        return root, child_identity.digest()

    def test_run_invokes_all_four_children_with_one_shared_contract(self):
        campaign = self.root / "campaign"
        commands = []

        def execute(command, **_kwargs):
            commands.append(command)
            label = command[command.index("--cxl-link-delay") + 1]
            child = Path(command[command.index("--root") + 1])
            self._child(label, parent=campaign)
            self.assertEqual(child, campaign / "latency" / label)
            return SimpleNamespace(returncode=0)

        options = SimpleNamespace(
            inputs=Path(self.shared["inputs"]["path"]),
            calibration=Path(self.shared["calibration"]["path"]),
            prepared=Path(self.shared["prepared"]["path"]),
            qualification=self.qualification,
            root=campaign,
            resume=False,
        )
        with mock.patch.object(spectrum.subprocess, "run", side_effect=execute):
            complete = spectrum.run(options)
        self.assertEqual(complete["status"], "complete")
        self.assertEqual(
            [command[command.index("--cxl-link-delay") + 1]
             for command in commands],
            ["200ns", "500ns", "1us", "2us"],
        )
        for command in commands:
            self.assertEqual(
                command[command.index("--inputs") + 1],
                self.shared["inputs"]["path"],
            )
            self.assertEqual(
                command[command.index("--calibration") + 1],
                self.shared["calibration"]["path"],
            )
        self.assertEqual(
            complete["qualification"]["sha256"],
            hashlib.sha256(self.qualification.read_bytes()).hexdigest(),
        )

    def test_latency_root_with_different_bound_identity_is_rejected(self):
        child, _ = self._child("500ns")
        with self.assertRaisesRegex(
            spectrum.SpectrumError, "child campaign identity differs"
        ):
            spectrum.validate_child(
                child, "500ns", self.shared,
                expected_identity_sha256=sha("other-child"),
            )

    def test_child_requires_complete_manifest(self):
        child = self.root / "latency" / "1us"
        child.mkdir(parents=True)
        with self.assertRaisesRegex(
            spectrum.SpectrumError, "child complete manifest is missing"
        ):
            spectrum.validate_child(child, "1us", self.shared)

    def test_inconclusive_child_cannot_enter_aggregate(self):
        child, _ = self._child("2us", status="inconclusive")
        with self.assertRaisesRegex(
            spectrum.SpectrumError, "child campaign is inconclusive"
        ):
            spectrum.validate_child(child, "2us", self.shared)

    def test_native_verified_aggregate_rejects_strict_child(self):
        child, _ = self._child("2us", correctness_policy="bit-exact")
        with self.assertRaisesRegex(
            spectrum.SpectrumError, "correctness policy differs"
        ):
            spectrum.validate_child(
                child, "2us", self.shared,
                correctness_policy="native-verified",
            )

    def test_aggregate_requires_all_four_valid_children(self):
        state = spectrum.new_state(
            self.shared, self._qualification_record(), identity()
        )
        for label in ("200ns", "500ns", "1us"):
            child, _ = self._child(label)
            spectrum.record_child(state, label, child, ["breadth", label])
        with self.assertRaisesRegex(
            spectrum.SpectrumError, "all four latency campaigns"
        ):
            spectrum.complete_state(state, self.root / "aggregate")
        child, _ = self._child("2us")
        spectrum.record_child(state, "2us", child, ["breadth", "2us"])
        complete = spectrum.complete_state(state, self.root / "aggregate")
        self.assertEqual(complete["status"], "complete")
        self.assertTrue((self.root / "aggregate/complete.json").is_file())


if __name__ == "__main__":
    unittest.main()
