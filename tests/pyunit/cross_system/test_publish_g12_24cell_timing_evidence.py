# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

import csv
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from scripts import publish_g12_24cell_timing_evidence as publisher
from scripts import run_g12_24cell_timing_evidence as runner
from scripts import timing_evidence_24cell as evidence


def _digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class PublishG1224CellTimingEvidenceTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def _calibration(self, latency, selected, target, measured, residual):
        root = self.root / "calibrations" / latency
        config = root / "config"
        config.mkdir(parents=True)
        m2ndp = config / "m2ndp.config"
        link = config / "cxl_link.icnt"
        m2ndp.write_text("m2ndp\n", encoding="utf-8")
        link.write_text("link\n", encoding="utf-8")
        path = root / "calibration.json"
        path.write_text(json.dumps({
            "schema": 1, "passed": True, "cxl_delay": latency,
            "cxl_link_delay": latency, "target_ns": target,
            "measured_ns": measured, "residual_ns": residual,
            "selected_link_latency": selected,
            "core_period_ns": "0.5", "link_period_ns": "0.125",
            "derived_m2ndp_config_sha256": evidence.sha256_file(m2ndp),
            "derived_cxl_link_config_sha256": evidence.sha256_file(link),
        }, sort_keys=True) + "\n", encoding="utf-8")
        return path

    def _campaign(self, graph_scale=12):
        calibrations = {
            "200ns": self._calibration("200ns", 1397, "412.254", "412.25", "0.004"),
            "500ns": self._calibration("500ns", 3798, "1012.32", "1012.375", "0.055"),
            "1us": self._calibration("1us", 7799, "2012.652", "2012.625", "0.027"),
            "2us": self._calibration("2us", 15801, "4012.65", "4012.624999998", "0.025000002"),
        }
        source_identity = self.root / f"source-identity-g{graph_scale}"
        source_identity.mkdir()
        graph_sha256 = _digest(f"g{graph_scale}")
        inputs = source_identity / "inputs.json"
        prepared = source_identity / "registry.json"
        input_value = {
            "schema": 1, "status": "accepted",
            "graph": {"scale": graph_scale, "sha256": graph_sha256},
            "workloads": {
                workload: {
                    "input_sha256": _digest(f"{workload}-input"),
                    **({
                        "scale": graph_scale, "sha256": graph_sha256,
                    } if workload in {"pr_spmv", "gap_bc"} else {}),
                }
                for workload in evidence.WORKLOADS
            },
        }
        prepared_value = {
            "schema": 1, "status": "verified",
            "graph": {"scale": graph_scale, "sha256": graph_sha256},
            "cells": {
                f"{workload}:{latency}": {
                    "input_sha256": _digest(f"{workload}-input"),
                }
                for workload, latency in evidence.COORDINATES
            },
        }
        inputs.write_text(
            json.dumps(input_value, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        prepared.write_text(
            json.dumps(prepared_value, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        identity = runner.CampaignIdentity(
            repository_commit=_digest("commit"), code_sha256=_digest("code"),
            registry_preparer_sha256=_digest("preparer"),
            input_manifest_path=str(inputs),
            input_manifest_sha256=evidence.sha256_file(inputs),
            prepared_manifest_path=str(prepared),
            prepared_manifest_sha256=evidence.sha256_file(prepared),
            replay_binary_sha256=_digest("replay"),
            gem5_sha256=_digest("gem5"), m5_library_sha256=_digest("m5"),
            funcsim_sha256=None, ndpsim_sha256=None,
            gem5_config_sha256=_digest("config"),
            calibration_sha256=tuple(
                (latency, evidence.sha256_file(calibrations[latency]))
                for latency in evidence.LATENCIES
            ),
        )
        campaign = self.root / "campaign"
        state = runner.new_state(identity)
        campaign.mkdir()
        runner.atomic_write_json(campaign / "state.json", state)
        return campaign, state, identity, calibrations

    def _complete_stage(
        self, campaign, state, identity, calibrations, workload, latency, stage,
        index, host_ticks=None, cira_busy_ticks=None,
    ):
        root = campaign / "cells" / workload / latency / stage / "attempts/0001"
        root.mkdir(parents=True)
        raw = root / "replay-evidence.json"
        raw.write_text(json.dumps({"cell": f"{workload}:{latency}:{stage}"}) + "\n")
        calibration = calibrations[latency]
        common = {
            "schema": 1, "status": "pass", "workload": workload,
            "latency": latency,
            "campaign_identity_sha256": identity.digest(),
            "calibration_evidence_path": str(calibration),
            "calibration_evidence_sha256": evidence.sha256_file(calibration),
            "source_evidence_path": str(raw),
            "source_evidence_sha256": evidence.sha256_file(raw),
        }
        if stage == "host_inline":
            payload = {
                **common, "system": "cira-inline", "offload_disabled": True,
                "host_region_cumulative_ticks": (
                    2000 + index if host_ticks is None else host_ticks
                ),
                "host_region_entry_count": 10 + index,
                "sim_freq_hz": 1_000_000_000_000,
            }
            filename = "host-inline-evidence.json"
        else:
            payload = {
                **common, "system": "cira",
                "sim_freq_hz": 1_000_000_000_000,
                "issued_prefetches": 16,
                "completed_prefetches": 16,
                "generic_prefetch": {
                    "first_issue_tick": 100,
                    "last_completion_tick": 100 + (
                        300 + index
                        if cira_busy_ticks is None else cira_busy_ticks
                    ),
                    "busy_ticks": (
                        300 + index
                        if cira_busy_ticks is None else cira_busy_ticks
                    ),
                    "busy_ticks_per_core": [70, 71, 72, 73],
                },
                "issued_per_core": [4, 4, 4, 4],
                "completed_per_core": [4, 4, 4, 4],
                "pr_descriptor_metrics": {
                    "applicable": False, "compute_ticks": 0,
                    "queue_stall_ticks": 0,
                    "compute_ticks_per_core": [0, 0, 0, 0],
                    "queue_stall_ticks_per_core": [0, 0, 0, 0],
                },
            }
            filename = "cira-runtime-evidence.json"
        path = root / filename
        runner.atomic_write_json(path, payload)
        state["cells"][f"{workload}:{latency}"]["stages"][stage] = {
            "status": "complete", "attempt": 1,
            "attempt_root": str(root),
            "evidence": {
                "path": str(path), "sha256": evidence.sha256_file(path),
            },
        }
        runner._refresh_cell_status(
            state["cells"][f"{workload}:{latency}"], identity
        )

    def test_progress_has_24_rows_and_explicit_stage_status(self):
        campaign, state, identity, calibrations = self._campaign()
        self._complete_stage(
            campaign, state, identity, calibrations,
            "pr_spmv", "200ns", "host_inline", 1,
        )
        runner.atomic_write_json(campaign / "state.json", state)
        destination = self.root / "progress"
        publisher.publish(campaign, destination, progress=True)
        with (destination / "timing-24cells-progress.csv").open(newline="") as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual(len(rows), 24)
        self.assertEqual(rows[0]["host_status"], "complete")
        self.assertEqual(rows[0]["cira_status"], "pending")
        self.assertEqual(rows[0]["host_region_cumulative_ticks"], "2001")
        self.assertEqual(rows[0]["cira_device_busy_ticks"], "")
        self.assertEqual(rows[1]["host_status"], "pending")
        self.assertEqual(rows[1]["host_region_cumulative_ticks"], "")
        self.assertEqual(rows[1]["cira_device_busy_ticks"], "")
        manifest = json.loads((destination / "manifest.json").read_text())
        self.assertEqual(manifest["mode"], "progress")
        self.assertEqual(manifest["rows"], 24)

    def test_final_rejects_incomplete_host_or_cira_stage(self):
        campaign, state, identity, calibrations = self._campaign()
        self._complete_stage(
            campaign, state, identity, calibrations,
            "pr_spmv", "200ns", "host_inline", 1,
        )
        runner.atomic_write_json(campaign / "state.json", state)
        with self.assertRaisesRegex(
            publisher.PublishError, "incomplete host/CIRA cells"
        ):
            publisher.publish(campaign, self.root / "final")

    def test_final_is_deterministic_and_has_complete_host_cira_rows(self):
        campaign, state, identity, calibrations = self._campaign()
        for index, (workload, latency) in enumerate(evidence.COORDINATES, 1):
            for stage in runner.REPLAY_STAGES:
                self._complete_stage(
                    campaign, state, identity, calibrations,
                    workload, latency, stage, index,
                )
        state["status"] = "complete"
        runner.atomic_write_json(campaign / "state.json", state)
        runner.atomic_write_json(campaign / "complete.json", state)
        first = self.root / "published-a"
        second = self.root / "published-b"
        publisher.publish(campaign, first)
        publisher.publish(campaign, second)
        for name in ("timing-24cells.csv", "calibration.csv", "README.md", "manifest.json"):
            self.assertEqual((first / name).read_bytes(), (second / name).read_bytes())
        with (first / "timing-24cells.csv").open(newline="") as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual(len(rows), 24)
        self.assertEqual(
            [(row["workload"], row["latency"]) for row in rows],
            list(evidence.COORDINATES),
        )
        self.assertTrue(all(row["host_status"] == "complete" for row in rows))
        self.assertTrue(all(row["cira_status"] == "complete" for row in rows))
        self.assertEqual(rows[0]["host_region_cumulative_ns"], "2.001")
        self.assertEqual(rows[0]["cira_device_busy_ns"], "0.301")
        self.assertEqual(rows[0]["cira_issued_prefetches"], "16")
        self.assertEqual(rows[0]["cira_completed_prefetches"], "16")
        self.assertEqual(
            rows[0]["pr_compute_plus_stall_max_ticks"], "0"
        )
        self.assertEqual(rows[0]["pr_compute_ticks_core3"], "0")

    def test_progress_rejects_completed_stage_hash_drift(self):
        campaign, state, identity, calibrations = self._campaign()
        self._complete_stage(
            campaign, state, identity, calibrations,
            "pr_spmv", "200ns", "host_inline", 1,
        )
        runner.atomic_write_json(campaign / "state.json", state)
        record = state["cells"]["pr_spmv:200ns"]["stages"]["host_inline"]["evidence"]
        Path(record["path"]).write_text("changed\n")
        with self.assertRaisesRegex(publisher.PublishError, "complete stage differs"):
            publisher.publish(campaign, self.root / "stale", progress=True)



    def _complete_campaign(
        self, *, host_ticks=2000, cira_busy_ticks=1250, graph_scale=12,
    ):
        campaign, state, identity, calibrations = self._campaign(
            graph_scale=graph_scale
        )
        for index, (workload, latency) in enumerate(evidence.COORDINATES, 1):
            for stage in runner.REPLAY_STAGES:
                self._complete_stage(
                    campaign, state, identity, calibrations,
                    workload, latency, stage, index,
                    host_ticks=host_ticks,
                    cira_busy_ticks=cira_busy_ticks,
                )
        state["status"] = "complete"
        runner.atomic_write_json(campaign / "state.json", state)
        runner.atomic_write_json(campaign / "complete.json", state)
        return campaign

    def test_final_labels_g12_and_computes_exact_speedup(self):
        campaign = self._complete_campaign(
            host_ticks=2000, cira_busy_ticks=1250, graph_scale=12
        )
        destination = self.root / "published-g12"
        publisher.publish(campaign, destination)
        with (destination / "timing-24cells.csv").open(newline="") as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual(len(rows), 24)
        self.assertTrue(all(row["graph_scale"] == "12" for row in rows))
        self.assertTrue(all(
            row["host_over_cira_speedup"] == "1.6" for row in rows
        ))
        self.assertIn(
            "G12 24-cell host-inline and CIRA timing evidence",
            (destination / "README.md").read_text(encoding="utf-8"),
        )

    def test_publisher_rejects_g14_state(self):
        campaign = self._complete_campaign(graph_scale=14)
        destination = self.root / "rejected"
        with self.assertRaisesRegex(
            publisher.PublishError, "source campaign is not G12"
        ):
            publisher.publish(campaign, destination)
        self.assertFalse(destination.exists())

    def test_publisher_rejects_input_manifest_hash_drift(self):
        campaign = self._complete_campaign()
        state = json.loads(
            (campaign / "state.json").read_text(encoding="utf-8")
        )
        Path(state["identity"]["input_manifest_path"]).write_text(
            "changed\n", encoding="utf-8"
        )
        with self.assertRaisesRegex(
            publisher.PublishError, "input manifest SHA-256 differs"
        ):
            publisher.publish(campaign, self.root / "stale-input")

if __name__ == "__main__":
    unittest.main()
