# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

import dataclasses
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import run_24cell_timing_evidence as runner
from scripts import timing_evidence_24cell as evidence


def _digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class Run24CellTimingEvidenceTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.identity = runner.CampaignIdentity(
            repository_commit=_digest("commit"),
            code_sha256=_digest("code"),
            input_manifest_sha256=_digest("inputs"),
            prepared_manifest_sha256=_digest("prepared"),
            replay_binary_sha256=_digest("replay"),
            gem5_sha256=_digest("gem5"),
            m5_library_sha256=_digest("m5"),
            funcsim_sha256=_digest("funcsim"),
            ndpsim_sha256=_digest("ndpsim"),
            gem5_config_sha256=_digest("config"),
            calibration_sha256=tuple(
                (latency, _digest(latency)) for latency in evidence.LATENCIES
            ),
        )

    def tearDown(self):
        self.temporary.cleanup()

    def test_new_state_has_exact_24_cell_matrix(self):
        state = runner.new_state(self.identity)
        self.assertEqual(set(state["cells"]), {
            f"{workload}:{latency}"
            for workload, latency in evidence.COORDINATES
        })
        self.assertTrue(all(
            row["status"] == "pending" for row in state["cells"].values()
        ))

    def test_resume_rejects_changed_binary_hash(self):
        state = runner.new_state(self.identity)
        changed = dataclasses.replace(
            self.identity, gem5_sha256=_digest("changed")
        )
        with self.assertRaisesRegex(runner.CampaignError, "identity differs"):
            runner.resume_state(state, changed)

    def _file_record(self, name, payload=b"payload"):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        return {"path": str(path), "sha256": evidence.sha256_file(path)}

    def _registry_cell(self):
        return {
            "input_sha256": _digest("input"),
            "trace": self._file_record(
                "trace/trace.meta.json",
                json.dumps({
                    "workload": "pr_spmv",
                    "input_sha256": _digest("input"),
                }).encode("utf-8"),
            ),
            "fixed_trace": self._file_record(
                "fixed/trace.meta.json",
                json.dumps({
                    "workload": "pr_spmv",
                    "input_sha256": _digest("input"),
                }).encode("utf-8"),
            ),
            "window_manifest": self._file_record("window.json", b"window"),
            "phase": 0,
            "window_index": 0,
            "m2ndp_package": self._file_record("package/manifest.json", b"package"),
            "functional_evidence": self._file_record(
                "package/funcsim-evidence.json", b"functional"
            ),
        }

    @staticmethod
    def _host_result():
        return {
            "schema": 1, "status": "pass", "system": "cira-inline",
            "binary_sha256": _digest("replay"),
            "gem5_sha256": _digest("gem5"),
            "config_sha256": _digest("host-config"),
            "row": {
                "verification": "pass", "offload_disabled": True,
                "host_region_cumulative_ticks": 1234,
                "host_region_entry_count": 17,
                "sim_freq_hz": 1_000_000_000_000,
                "issued_loads": 0, "completed_loads": 0,
                "issued_per_core": [0, 0, 0, 0],
                "completed_per_core": [0, 0, 0, 0],
            },
        }

    @staticmethod
    def _cira_result():
        return {
            "schema": 1, "status": "pass", "system": "cira",
            "binary_sha256": _digest("replay"),
            "gem5_sha256": _digest("gem5"),
            "config_sha256": _digest("cira-config"),
            "row": {
                "verification": "pass",
                "sim_freq_hz": 1_000_000_000_000,
                "issued_prefetches": 8, "completed_prefetches": 8,
                "issued_per_core": [2, 2, 2, 2],
                "completed_per_core": [2, 2, 2, 2],
                "generic_prefetch": {
                    "first_issue_tick": 100, "last_completion_tick": 220,
                    "busy_ticks": 120,
                    "first_issue_ticks_per_core": [100, 101, 102, 103],
                    "last_completion_ticks_per_core": [210, 211, 219, 220],
                    "busy_ticks_per_core": [110, 110, 117, 117],
                    "span_valid_per_core": [1, 1, 1, 1],
                },
                "pr_descriptor_metrics": {
                    "applicable": False, "compute_ticks": 0,
                    "queue_stall_ticks": 0,
                    "compute_ticks_per_core": [0, 0, 0, 0],
                    "queue_stall_ticks_per_core": [0, 0, 0, 0],
                    "issued": 0, "completed": 0,
                    "issued_per_core": [0, 0, 0, 0],
                    "completed_per_core": [0, 0, 0, 0],
                },
            },
        }

    @staticmethod
    def _m2ndp_result():
        return {
            "schema": 1, "status": "pass", "workload": "pr_spmv",
            "latency": "200ns", "cycles": 1000,
            "core_period_ns": "0.5", "kernel_time_ns": "500",
            "execution_origin": "fresh",
        }

    def test_execute_cell_launches_two_replays_and_one_m2ndp(self):
        state = runner.new_state(self.identity)
        cell = self._registry_cell()
        replay_launch = mock.Mock(side_effect=(
            self._host_result(), self._cira_result(),
        ))
        m2ndp_launch = mock.Mock(return_value=self._m2ndp_result())
        calibration = evidence.CalibrationRow(
            latency="200ns", gem5_round_trip_ns="412.254",
            selected_link_latency=1397, core_period_ns="0.5",
            link_period_ns="0.125", m2ndp_round_trip_ns="412.25",
            residual_ns="0.004", residual_ps="4",
            evidence_path="/calibration.json", evidence_sha256=_digest("200ns"),
        )

        runner.execute_cell(
            state, self.identity, "pr_spmv", "200ns", cell,
            calibration, root=self.root / "campaign",
            replay_launcher=replay_launch, m2ndp_launcher=m2ndp_launch,
        )

        self.assertEqual(replay_launch.call_count, 2)
        first, second = replay_launch.call_args_list
        self.assertEqual(first.kwargs["system"], "cira-inline")
        self.assertIs(first.kwargs["require_device_timing"], False)
        self.assertEqual(second.kwargs["system"], "cira")
        self.assertIs(second.kwargs["require_device_timing"], True)
        m2ndp_launch.assert_called_once()
        status = state["cells"]["pr_spmv:200ns"]
        self.assertEqual(status["status"], "complete")
        self.assertTrue(runner.cell_complete(
            status, self.identity, "pr_spmv", "200ns"
        ))

    def test_complete_resume_skips_launch_and_stale_hash_fails(self):
        state = runner.new_state(self.identity)
        cell = self._registry_cell()
        calibration = evidence.CalibrationRow(
            latency="200ns", gem5_round_trip_ns="1",
            selected_link_latency=1, core_period_ns="0.5",
            link_period_ns="0.125", m2ndp_round_trip_ns="1",
            residual_ns="0", residual_ps="0", evidence_path="x",
            evidence_sha256=_digest("200ns"),
        )
        replay_launch = mock.Mock(side_effect=(
            self._host_result(), self._cira_result(),
        ))
        m2ndp_launch = mock.Mock(return_value=self._m2ndp_result())
        root = self.root / "campaign"
        runner.execute_cell(
            state, self.identity, "pr_spmv", "200ns", cell,
            calibration, root=root, replay_launcher=replay_launch,
            m2ndp_launcher=m2ndp_launch,
        )
        runner.execute_cell(
            state, self.identity, "pr_spmv", "200ns", cell,
            calibration, root=root, replay_launcher=replay_launch,
            m2ndp_launcher=m2ndp_launch,
        )
        self.assertEqual(replay_launch.call_count, 2)
        self.assertEqual(m2ndp_launch.call_count, 1)

        record = state["cells"]["pr_spmv:200ns"]
        Path(record["evidence"]["host_inline"]["path"]).write_text(
            "changed\n", encoding="utf-8"
        )
        with self.assertRaisesRegex(runner.CampaignError, "stale complete cell"):
            runner.execute_cell(
                state, self.identity, "pr_spmv", "200ns", cell,
                calibration, root=root, replay_launcher=replay_launch,
                m2ndp_launcher=m2ndp_launch,
            )

    def test_failed_attempt_is_preserved_and_resume_uses_new_attempt(self):
        state = runner.new_state(self.identity)
        cell = self._registry_cell()
        calibration = evidence.CalibrationRow(
            latency="200ns", gem5_round_trip_ns="1",
            selected_link_latency=1, core_period_ns="0.5",
            link_period_ns="0.125", m2ndp_round_trip_ns="1",
            residual_ns="0", residual_ps="0", evidence_path="x",
            evidence_sha256=_digest("200ns"),
        )
        replay_launch = mock.Mock(side_effect=RuntimeError("interrupted"))
        campaign = self.root / "retry-campaign"
        with self.assertRaisesRegex(RuntimeError, "interrupted"):
            runner.execute_cell(
                state, self.identity, "pr_spmv", "200ns", cell,
                calibration, root=campaign, replay_launcher=replay_launch,
                m2ndp_launcher=mock.Mock(),
            )
        failed = state["cells"]["pr_spmv:200ns"]
        self.assertEqual(failed["attempt"], 1)
        self.assertTrue(Path(failed["attempt_root"]).is_dir())

        replay_launch = mock.Mock(side_effect=(
            self._host_result(), self._cira_result(),
        ))
        runner.execute_cell(
            state, self.identity, "pr_spmv", "200ns", cell,
            calibration, root=campaign, replay_launcher=replay_launch,
            m2ndp_launcher=mock.Mock(return_value=self._m2ndp_result()),
        )
        complete = state["cells"]["pr_spmv:200ns"]
        self.assertEqual(complete["attempt"], 2)
        self.assertTrue(
            (campaign / "cells/pr_spmv/200ns/attempts/0001").is_dir()
        )
        self.assertTrue(
            (campaign / "cells/pr_spmv/200ns/attempts/0002").is_dir()
        )

    def test_registry_requires_exact_matrix_and_hashes(self):
        registry = {
            "schema": 1, "status": "verified",
            "cells": {
                f"{workload}:{latency}": {
                    **self._registry_cell(),
                    "trace": self._file_record(
                        f"{workload}/{latency}/trace/trace.meta.json",
                        json.dumps({
                            "workload": workload,
                            "input_sha256": _digest(f"{workload}-input"),
                        }).encode("utf-8"),
                    ),
                    "fixed_trace": self._file_record(
                        f"{workload}/{latency}/fixed/trace.meta.json",
                        json.dumps({
                            "workload": workload,
                            "input_sha256": _digest(f"{workload}-input"),
                        }).encode("utf-8"),
                    ),
                    "input_sha256": _digest(f"{workload}-input"),
                }
                for workload, latency in evidence.COORDINATES
            },
        }
        path = self.root / "registry.json"
        path.write_text(json.dumps(registry) + "\n", encoding="utf-8")
        self.assertEqual(len(runner.load_registry(path)), 24)
        registry["cells"].pop("npb_cg:2us")
        path.write_text(json.dumps(registry) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(runner.CampaignError, "exact 24-cell matrix"):
            runner.load_registry(path)

    def test_registry_rejects_trace_workload_or_input_relabeling(self):
        cell = self._registry_cell()
        with self.assertRaisesRegex(runner.CampaignError, "trace workload differs"):
            runner._validate_registry_cell("mcf:200ns", cell)

        cell = self._registry_cell()
        cell["input_sha256"] = _digest("different-input")
        with self.assertRaisesRegex(runner.CampaignError, "trace input SHA-256 differs"):
            runner._validate_registry_cell("pr_spmv:200ns", cell)

    def test_default_cira_launcher_requires_device_timing(self):
        cell = self._registry_cell()
        calibration = self._file_record("calibration.json", b"calibration")
        with mock.patch.object(
            runner.replay, "run", return_value={"status": "pass"}
        ) as launched:
            runner._launch_replay(
                system="cira", workload="pr_spmv", latency="200ns",
                cell=cell, root=self.root / "cira-run",
                require_device_timing=True,
                binary=self.root / "trace-replay", gem5=self.root / "gem5",
                calibration_paths={"200ns": Path(calibration["path"])},
            )
        options = launched.call_args.args[0]
        self.assertIs(options.require_device_timing, True)
        self.assertEqual(options.system, "cira")
        self.assertEqual(options.fixed_trace, Path(cell["fixed_trace"]["path"]).parent)


if __name__ == "__main__":
    unittest.main()
