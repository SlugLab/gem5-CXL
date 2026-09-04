# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

import csv
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from scripts import publish_24cell_timing_evidence as publisher
from scripts import run_24cell_timing_evidence as runner
from scripts import timing_evidence_24cell as evidence


def _digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class Publish24CellTimingEvidenceTest(unittest.TestCase):
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

    def _campaign(self):
        calibrations = {
            "200ns": self._calibration("200ns", 1397, "412.254", "412.25", "0.004"),
            "500ns": self._calibration("500ns", 3798, "1012.32", "1012.375", "0.055"),
            "1us": self._calibration("1us", 7799, "2012.652", "2012.625", "0.027"),
            "2us": self._calibration("2us", 15801, "4012.65", "4012.624999998", "0.025000002"),
        }
        identity = runner.CampaignIdentity(
            repository_commit=_digest("commit"), code_sha256=_digest("code"),
            input_manifest_sha256=_digest("inputs"),
            prepared_manifest_sha256=_digest("prepared"),
            replay_binary_sha256=_digest("replay"),
            gem5_sha256=_digest("gem5"), m5_library_sha256=_digest("m5"),
            funcsim_sha256=_digest("funcsim"), ndpsim_sha256=_digest("ndpsim"),
            gem5_config_sha256=_digest("config"),
            calibration_sha256=tuple(
                (latency, evidence.sha256_file(calibrations[latency]))
                for latency in evidence.LATENCIES
            ),
        )
        campaign = self.root / "campaign"
        state = runner.new_state(identity)
        for index, (workload, latency) in enumerate(evidence.COORDINATES, 1):
            cell = campaign / "cells" / workload / latency
            cell.mkdir(parents=True)
            raw = cell / "raw-evidence.json"
            raw.write_text(json.dumps({"cell": f"{workload}:{latency}"}) + "\n")
            raw_hash = evidence.sha256_file(raw)
            calibration = calibrations[latency]
            common = {
                "schema": 1, "status": "pass", "workload": workload,
                "latency": latency,
                "campaign_identity_sha256": identity.digest(),
                "calibration_evidence_path": str(calibration),
                "calibration_evidence_sha256": evidence.sha256_file(calibration),
                "source_evidence_path": str(raw),
                "source_evidence_sha256": raw_hash,
            }
            payloads = {
                "m2ndp": {
                    **common, "cycles": 1000 + index,
                    "core_period_ns": "0.5",
                    "kernel_time_ns": evidence.cycles_to_ns(1000 + index, "0.5"),
                    "execution_origin": "verified_reuse",
                },
                "host_inline": {
                    **common, "system": "cira-inline",
                    "offload_disabled": True,
                    "host_region_cumulative_ticks": 2000 + index,
                    "host_region_entry_count": 10 + index,
                    "sim_freq_hz": 1_000_000_000_000,
                },
                "cira_runtime": {
                    **common, "system": "cira",
                    "sim_freq_hz": 1_000_000_000_000,
                    "generic_prefetch": {
                        "first_issue_tick": 100,
                        "last_completion_tick": 400 + index,
                        "busy_ticks": 300 + index,
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
                },
            }
            names = {
                "m2ndp": "m2ndp-evidence.json",
                "host_inline": "host-inline-evidence.json",
                "cira_runtime": "cira-runtime-evidence.json",
            }
            records = {}
            for name, payload in payloads.items():
                path = cell / names[name]
                runner.atomic_write_json(path, payload)
                records[name] = {
                    "path": str(path), "sha256": evidence.sha256_file(path),
                }
            state["cells"][f"{workload}:{latency}"] = {
                "workload": workload, "latency": latency,
                "status": "complete", "identity_sha256": identity.digest(),
                "evidence": records,
            }
        state["status"] = "complete"
        runner.atomic_write_json(campaign / "state.json", state)
        runner.atomic_write_json(campaign / "complete.json", state)
        return campaign

    def test_publication_is_deterministic_and_has_24_complete_rows(self):
        campaign = self._campaign()
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
        self.assertEqual(rows[0]["host_region_cumulative_ns"], "2.001")
        self.assertEqual(rows[0]["cira_device_busy_ns"], "0.301")
        self.assertEqual(rows[0]["pr_compute_ticks_core3"], "0")

    def test_publication_rejects_incomplete_or_source_hash_drift(self):
        campaign = self._campaign()
        state = json.loads((campaign / "complete.json").read_text())
        state["cells"]["npb_cg:2us"]["status"] = "failed"
        runner.atomic_write_json(campaign / "complete.json", state)
        with self.assertRaisesRegex(publisher.PublishError, "complete"):
            publisher.publish(campaign, self.root / "incomplete")

        runner.atomic_write_json(
            campaign / "complete.json",
            json.loads((campaign / "state.json").read_text()),
        )
        compact = campaign / "cells/pr_spmv/200ns/host-inline-evidence.json"
        record = json.loads(compact.read_text())
        Path(record["source_evidence_path"]).write_text("changed\n")
        with self.assertRaisesRegex(publisher.PublishError, "source evidence SHA-256"):
            publisher.publish(campaign, self.root / "stale")


if __name__ == "__main__":
    unittest.main()
