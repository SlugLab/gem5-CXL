# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

import copy
import hashlib
import unittest
from decimal import Decimal

from scripts import pr_offload_contract as contract


def digest(label):
    return hashlib.sha256(label.encode()).hexdigest()


class OffloadContractTest(unittest.TestCase):
    def identity(self):
        return {
            key: digest(key)
            for key in contract.IDENTITY_HASH_FIELDS
        } | {"m2ndp_commit": "1" * 40}

    def point(self, scale, system):
        point = {
            "scale": scale,
            "system": system,
            "profile": contract.FORMAL_PROFILE,
            "cxl_link_delay": "1us",
            "workers": 4,
            "iterations": 20,
            "all_memory_cxl": True,
            "verification": "pass",
            "raw_sha256": digest(f"g{scale}-raw"),
            "worker_completions": [20, 20, 20, 20],
            "pending": {"descriptors": 0, "requests": 0, "writebacks": 0},
        }
        if system == "vanilla":
            point["sim_ticks"] = 1500
        elif system == "m2ndp":
            point.update(
                ndpsim_cycles=1000,
                ndpsim_core_period_seconds="1e-12",
                funcsim={
                    "status": "pass", "compared": 1 << scale,
                    "mismatched": 0, "completed_at_seq": 7,
                },
                ndpsim_started_at_seq=8,
            )
        else:
            point["sim_ticks"] = 1000
        if system.startswith("cira"):
            point["phases"] = {
                "formation": 100, "sampling": 100, "selection": 100,
                "jit": 100, "execution": 500, "drain": 100,
            }
            point["phase_total_ns"] = 1000
            point["selected_candidate"] = "B"
        return point

    def test_matrix_has_twelve_primary_fifteen_ablations_and_no_oracle(self):
        primary, ablations = contract.build_matrix()
        self.assertEqual(len(primary), 12)
        self.assertEqual(len(ablations), 15)
        self.assertEqual(
            {entry.key for entry in primary},
            {f"g{s}:{system}" for s in (12, 14, 20)
             for system in contract.PRIMARY_SYSTEMS},
        )
        self.assertFalse(any("oracle" in entry.system for entry in primary + ablations))

    def test_identity_is_exact_and_changed_byte_prevents_resume(self):
        identity = contract.validate_identity(self.identity())
        contract.require_resume_identity(identity, copy.deepcopy(identity))
        changed = copy.deepcopy(identity)
        changed["gem5_sha256"] = digest("changed")
        with self.assertRaisesRegex(contract.OffloadError, "identity"):
            contract.require_resume_identity(identity, changed)
        missing = copy.deepcopy(identity)
        missing.pop("policy_sha256")
        with self.assertRaises(contract.OffloadError):
            contract.validate_identity(missing)

    def test_point_recomputes_native_time_and_rejects_stored_speedup(self):
        vanilla = contract.validate_point(self.point(12, "vanilla"))
        m2ndp = contract.validate_point(self.point(12, "m2ndp"))
        self.assertEqual(vanilla["seconds"], Decimal("1.5e-9"))
        self.assertEqual(m2ndp["seconds"], Decimal("1e-9"))
        bad = self.point(12, "amu")
        bad["speedup"] = "1.5"
        with self.assertRaisesRegex(contract.OffloadError, "stored speedup"):
            contract.validate_point(bad)

    def test_point_fails_closed_on_bits_phase_queue_topology_and_funcsim(self):
        cases = []
        bit = self.point(12, "amu"); bit["verification"] = "fail"; cases.append(bit)
        phase = self.point(12, "cira-few-shot"); phase["phases"]["drain"] = 99; cases.append(phase)
        missing_total = self.point(12, "cira-few-shot"); missing_total.pop("phase_total_ns"); cases.append(missing_total)
        queue = self.point(12, "amu"); queue["pending"]["requests"] = 1; cases.append(queue)
        topology = self.point(12, "amu"); topology["all_memory_cxl"] = False; cases.append(topology)
        funcsim = self.point(12, "m2ndp"); funcsim["funcsim"]["completed_at_seq"] = 9; cases.append(funcsim)
        workers = self.point(12, "amu"); workers["worker_completions"] = [20, 20, 20]; cases.append(workers)
        for point in cases:
            with self.subTest(point=point), self.assertRaises(contract.OffloadError):
                contract.validate_point(point)

    def test_complete_requires_exact_rows_raw_equality_and_nine_speedups(self):
        primary = [
            self.point(scale, system)
            for scale in contract.SCALES
            for system in contract.PRIMARY_SYSTEMS
        ]
        ablations = [
            self.point(scale, system)
            for scale in contract.SCALES
            for system in contract.CIRA_ABLATIONS
        ]
        complete = contract.validate_complete({
            "schema": 1,
            "identity": self.identity(),
            "primary": primary,
            "ablations": ablations,
        })
        self.assertEqual(len(complete["performance_gate"]), 9)
        self.assertTrue(all(
            row["speedup"] == Decimal("1.5")
            for row in complete["performance_gate"]
        ))
        changed = copy.deepcopy(primary)
        changed[1]["raw_sha256"] = digest("wrong")
        with self.assertRaisesRegex(contract.OffloadError, "raw vector"):
            contract.validate_complete({
                "schema": 1, "identity": self.identity(),
                "primary": changed, "ablations": ablations,
            })


if __name__ == "__main__":
    unittest.main()
