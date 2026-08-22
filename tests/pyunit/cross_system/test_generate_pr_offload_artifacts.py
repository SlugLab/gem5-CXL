# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

import copy
import hashlib
import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from scripts import generate_pr_offload_artifacts as publisher
from scripts import pr_offload_contract as contract


def digest(label):
    return hashlib.sha256(label.encode()).hexdigest()


def safe(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {key: safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [safe(item) for item in value]
    return value


class PublisherTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.complete = self.root / "complete.json"
        self.write_complete(self.fixture())

    def point(self, scale, system):
        point = {
            "scale": scale, "system": system,
            "profile": contract.FORMAL_PROFILE,
            "cxl_link_delay": "1us", "workers": 4, "iterations": 20,
            "all_memory_cxl": True, "verification": "pass",
            "raw_sha256": digest(f"g{scale}-raw"),
            "worker_completions": [40, 40, 40, 40],
            "pending": {"all": 0},
            "mechanism": {
                "csr_reads": scale * 100, "rank_reads": scale * 20,
                "fp_compute": scale * 10, "queue_stall": scale,
                "coherence": scale * 5, "writeback": scale * 2,
            },
        }
        if system == "vanilla":
            point["sim_ticks"] = 1_500_000
        elif system == "m2ndp":
            point.update(
                ndpsim_cycles=1_000_000,
                ndpsim_core_period_seconds="1e-12",
                funcsim={"status": "pass", "compared": 1 << scale,
                         "mismatched": 0, "completed_at_seq": 1},
                ndpsim_started_at_seq=2,
            )
        else:
            point["sim_ticks"] = 1_000_000
        if system.startswith("cira"):
            point["phases"] = {
                "formation": 100_000, "sampling": 100_000,
                "selection": 100_000, "jit": 100_000,
                "execution": 500_000, "drain": 100_000,
            }
            if system != "cira-few-shot":
                point["phases"].update(sampling=0, selection=0, jit=0,
                                       execution=800_000)
            point["selected_candidate"] = "B"
        return point

    def fixture(self):
        identity = {
            key: digest(key) for key in contract.IDENTITY_HASH_FIELDS
        } | {"m2ndp_commit": "1" * 40}
        validated = contract.validate_complete({
            "schema": 1, "identity": identity,
            "primary": [self.point(s, system) for s in contract.SCALES
                        for system in contract.PRIMARY_SYSTEMS],
            "ablations": [self.point(s, system) for s in contract.SCALES
                          for system in contract.CIRA_ABLATIONS],
        })
        validated.update(
            status="passed",
            oracle={f"g{s}": {"oracle_ticks": 1_000_000, "regret": "0"}
                    for s in contract.SCALES},
        )
        return safe(validated)

    def write_complete(self, value):
        self.complete.write_text(json.dumps(value, sort_keys=True) + "\n")

    def test_publishes_exact_outputs_and_is_deterministic(self):
        first = self.root / "first"
        second = self.root / "second"
        publisher.publish(self.complete, first)
        publisher.publish(self.complete, second)
        self.assertEqual(
            {str(path.relative_to(first)) for path in first.rglob("*") if path.is_file()},
            publisher.EXPECTED_OUTPUTS,
        )
        for relative in publisher.EXPECTED_OUTPUTS:
            self.assertEqual(
                publisher.sha256_file(first / relative),
                publisher.sha256_file(second / relative),
            )

    def test_mutations_fail_closed(self):
        base = self.fixture()
        mutations = []
        missing = copy.deepcopy(base); missing["primary"].pop(); mutations.append(missing)
        rank = copy.deepcopy(base); rank["primary"][1]["raw_sha256"] = "f" * 64; mutations.append(rank)
        speedup = copy.deepcopy(base); speedup["performance_gate"][0]["speedup"] = "1.49"; mutations.append(speedup)
        phase = copy.deepcopy(base); phase["primary"][2]["phases"]["drain"] -= 1; mutations.append(phase)
        pending = copy.deepcopy(base); pending["primary"][1]["pending"]["all"] = 1; mutations.append(pending)
        funcsim = copy.deepcopy(base); funcsim["primary"][3]["funcsim"]["status"] = "missing"; mutations.append(funcsim)
        gate = copy.deepcopy(base); gate["performance_gate"][0]["accepted"] = False; mutations.append(gate)
        source = copy.deepcopy(base); source["identity"]["source_sha256"] = "bad"; mutations.append(source)
        for index, value in enumerate(mutations):
            path = self.root / f"mutated-{index}.json"
            path.write_text(json.dumps(value) + "\n")
            with self.subTest(index=index), self.assertRaises(publisher.PublishError):
                publisher.publish(path, self.root / f"out-{index}")

    def test_promotion_failure_restores_previous_tree(self):
        outdir = self.root / "published"
        outdir.mkdir()
        sentinel = outdir / "old.txt"
        sentinel.write_text("old\n")

        def fail(source, destination):
            raise OSError("injected promotion failure")

        with self.assertRaisesRegex(publisher.PublishError, "promotion"):
            publisher.publish(self.complete, outdir, promote=fail)
        self.assertEqual(sentinel.read_text(), "old\n")


if __name__ == "__main__":
    unittest.main()
