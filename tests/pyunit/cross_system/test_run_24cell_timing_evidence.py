# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

import dataclasses
import hashlib
import json
import threading
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
            funcsim_sha256=None,
            ndpsim_sha256=None,
            gem5_config_sha256=_digest("config"),
            calibration_sha256=tuple(
                (latency, _digest(latency)) for latency in evidence.LATENCIES
            ),
        )

    def tearDown(self):
        self.temporary.cleanup()

    def _file_record(self, name, payload=b"payload"):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        return {"path": str(path), "sha256": evidence.sha256_file(path)}

    def _registry_cell(self, workload="pr_spmv", latency="200ns"):
        input_sha256 = _digest(f"{workload}-input")
        identity = json.dumps({
            "workload": workload,
            "input_sha256": input_sha256,
        }).encode("utf-8")
        return {
            "input_sha256": input_sha256,
            "trace": self._file_record(
                f"{workload}/{latency}/trace/trace.meta.json", identity
            ),
            "fixed_trace": self._file_record(
                f"{workload}/{latency}/fixed/trace.meta.json", identity
            ),
            "window_manifest": self._file_record(
                f"{workload}/{latency}/window.json", b"window"
            ),
            "phase": 0,
            "window_index": 0,
        }

    def _calibration(self, latency="200ns"):
        return evidence.CalibrationRow(
            latency=latency, gem5_round_trip_ns="1",
            selected_link_latency=1, core_period_ns="0.5",
            link_period_ns="0.125", m2ndp_round_trip_ns="1",
            residual_ns="0", residual_ps=0, evidence_path="x",
            evidence_sha256=_digest(latency),
        )

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

    @classmethod
    def _launcher(cls, **kwargs):
        return cls._host_result() if kwargs["stage"] == "host_inline" else cls._cira_result()

    def test_new_state_has_independent_replay_stages(self):
        state = runner.new_state(self.identity)
        self.assertEqual(set(state["cells"]), {
            f"{workload}:{latency}"
            for workload, latency in evidence.COORDINATES
        })
        for row in state["cells"].values():
            self.assertEqual(set(row["stages"]), set(runner.REPLAY_STAGES))
            self.assertTrue(all(
                stage["status"] == "pending"
                for stage in row["stages"].values()
            ))
            self.assertNotIn("m2ndp", row["stages"])

    def test_resume_rejects_changed_binary_hash(self):
        state = runner.new_state(self.identity)
        changed = dataclasses.replace(
            self.identity, gem5_sha256=_digest("changed")
        )
        with self.assertRaisesRegex(runner.CampaignError, "identity differs"):
            runner.resume_state(state, changed)

    def test_host_success_survives_cira_failure(self):
        state = runner.new_state(self.identity)
        launch = mock.Mock(side_effect=(
            self._host_result(), RuntimeError("interrupted"),
        ))
        campaign = self.root / "campaign"
        with self.assertRaisesRegex(RuntimeError, "interrupted"):
            runner.execute_cell(
                state, self.identity, "pr_spmv", "200ns",
                self._registry_cell(), self._calibration(), root=campaign,
                replay_launcher=launch, state_lock=threading.Lock(),
                stages=runner.REPLAY_STAGES,
            )
        cell = state["cells"]["pr_spmv:200ns"]
        self.assertEqual(cell["stages"]["host_inline"]["status"], "complete")
        self.assertEqual(cell["stages"]["cira_runtime"]["status"], "failed")
        record = cell["stages"]["host_inline"]["evidence"]
        self.assertEqual(evidence.sha256_file(Path(record["path"])), record["sha256"])
        self.assertNotIn("m2ndp", cell["stages"])

    def test_retry_skips_complete_host_and_only_reruns_cira(self):
        state = runner.new_state(self.identity)
        campaign = self.root / "retry"
        first = mock.Mock(side_effect=(
            self._host_result(), RuntimeError("interrupted"),
        ))
        with self.assertRaises(RuntimeError):
            runner.execute_cell(
                state, self.identity, "pr_spmv", "200ns",
                self._registry_cell(), self._calibration(), root=campaign,
                replay_launcher=first, state_lock=threading.Lock(),
                stages=runner.REPLAY_STAGES,
            )
        second = mock.Mock(return_value=self._cira_result())
        runner.execute_cell(
            state, self.identity, "pr_spmv", "200ns",
            self._registry_cell(), self._calibration(), root=campaign,
            replay_launcher=second, state_lock=threading.Lock(),
            stages=runner.REPLAY_STAGES,
        )
        second.assert_called_once()
        self.assertEqual(second.call_args.kwargs["stage"], "cira_runtime")
        cell = state["cells"]["pr_spmv:200ns"]
        self.assertEqual(cell["stages"]["host_inline"]["attempt"], 1)
        self.assertEqual(cell["stages"]["cira_runtime"]["attempt"], 2)
        self.assertTrue(runner.cell_complete(
            cell, self.identity, "pr_spmv", "200ns"
        ))

    def _registry(self):
        graph = self._file_record("graph/g14.sg", b"g14 graph")
        return {
            "schema": 1, "status": "verified",
            "graph": {**graph, "scale": 14},
            "cells": {
                f"{workload}:{latency}": self._registry_cell(workload, latency)
                for workload, latency in evidence.COORDINATES
            },
        }

    def test_replay_only_registry_requires_exact_matrix_and_graph_hash(self):
        registry = self._registry()
        path = self.root / "registry.json"
        path.write_text(json.dumps(registry) + "\n", encoding="utf-8")
        self.assertEqual(len(runner.load_registry(path)), 24)
        Path(registry["graph"]["path"]).write_bytes(b"changed")
        with self.assertRaisesRegex(runner.CampaignError, "graph SHA-256 differs"):
            runner.load_registry(path)

    def test_registry_rejects_trace_workload_or_input_relabeling(self):
        cell = self._registry_cell()
        with self.assertRaisesRegex(runner.CampaignError, "trace workload differs"):
            runner._validate_registry_cell("mcf:200ns", cell)
        cell = self._registry_cell()
        cell["input_sha256"] = _digest("different-input")
        with self.assertRaisesRegex(runner.CampaignError, "trace input SHA-256 differs"):
            runner._validate_registry_cell("pr_spmv:200ns", cell)

    def test_parallel_coordinates_commit_all_eight_stages(self):
        coordinates = [("pr_spmv", latency) for latency in evidence.LATENCIES]
        state = runner.new_state(self.identity)
        registry = {
            f"{workload}:{latency}": self._registry_cell(workload, latency)
            for workload, latency in coordinates
        }
        calibrations = {
            latency: self._calibration(latency) for latency in evidence.LATENCIES
        }
        campaign = self.root / "parallel"
        runner.execute_coordinates(
            state, self.identity, coordinates, registry, calibrations,
            root=campaign, replay_launcher=self._launcher,
            stages=runner.REPLAY_STAGES, jobs=2,
        )
        for workload, latency in coordinates:
            cell = state["cells"][f"{workload}:{latency}"]
            self.assertTrue(all(
                cell["stages"][stage]["status"] == "complete"
                for stage in runner.REPLAY_STAGES
            ))
        saved = json.loads((campaign / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(saved, state)

    def test_parse_args_supports_replay_only_tools_and_jobs(self):
        calibrations = sum(
            (["--calibration", latency] for latency in evidence.LATENCIES), []
        )
        options = runner.parse_args([
            "--inputs", "inputs.json", "--prepared", "prepared.json",
            *calibrations, "--gem5", "gem5", "--m5-library", "m5.a",
            "--root", "campaign", "--systems", "host-inline,cira",
            "--jobs", "2",
        ])
        self.assertIsNone(options.funcsim)
        self.assertIsNone(options.ndpsim)
        self.assertEqual(options.stages, runner.REPLAY_STAGES)
        self.assertEqual(options.jobs, 2)

    def test_default_cira_launcher_requires_device_timing(self):
        cell = self._registry_cell()
        calibration = self._file_record("calibration.json", b"calibration")
        with mock.patch.object(
            runner.replay, "run", return_value={"status": "pass"}
        ) as launched:
            runner._launch_replay(
                stage="cira_runtime", system="cira", workload="pr_spmv",
                latency="200ns", cell=cell, root=self.root / "cira-run",
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
