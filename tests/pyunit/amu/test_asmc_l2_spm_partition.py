# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

import re
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[3]
REQUEST = (REPO / "src/mem/request.hh").read_text(encoding="utf-8")
POLICY_PY = (
    REPO
    / "src/mem/cache/tags/partitioning_policies/PartitioningPolicies.py"
).read_text(encoding="utf-8")
SCONSCRIPT = (
    REPO / "src/mem/cache/tags/partitioning_policies/SConscript"
).read_text(encoding="utf-8")
POLICY_HH = REPO / (
    "src/mem/cache/tags/partitioning_policies/spm_partition_manager.hh"
)
POLICY_CC = REPO / (
    "src/mem/cache/tags/partitioning_policies/spm_partition_manager.cc"
)


class SpmPartitionContractTest(unittest.TestCase):
    def test_request_has_dedicated_collision_free_spm_flag(self):
        match = re.search(
            r"\bSPM_ACCESS\s*=\s*(0x[0-9A-Fa-f]+)", REQUEST
        )
        self.assertIsNotNone(match)
        spm_value = int(match.group(1), 16)
        self.assertNotEqual(spm_value, 0)
        self.assertEqual(spm_value & (spm_value - 1), 0)

        named_flags = re.findall(
            r"^\s*([A-Z][A-Z0-9_]*)\s*=\s*(0x[0-9A-Fa-f]+)\s*,",
            REQUEST,
            re.MULTILINE,
        )
        same_value = [name for name, value in named_flags
                      if int(value, 16) == spm_value]
        self.assertEqual(same_value, ["SPM_ACCESS"])
        self.assertIn("bool isSpmAccess() const", REQUEST)
        self.assertIn("_flags.isSet(SPM_ACCESS)", REQUEST)

    def test_partition_manager_selects_only_flagged_requests(self):
        self.assertTrue(POLICY_HH.is_file())
        self.assertTrue(POLICY_CC.is_file())
        source = POLICY_CC.read_text(encoding="utf-8")
        self.assertIn("pkt->req->isSpmAccess()", source)
        self.assertIn("return spmPartitionId", source)
        self.assertIn("return 0", source)

    def test_python_simobject_exposes_spm_partition_id(self):
        self.assertIn("class SpmPartitionManager(PartitionManager)", POLICY_PY)
        self.assertIn('type = "SpmPartitionManager"', POLICY_PY)
        self.assertIn("spm_partition_id = Param.UInt64", POLICY_PY)

    def test_sconscript_builds_policy(self):
        self.assertIn("'SpmPartitionManager'", SCONSCRIPT)
        self.assertIn("Source('spm_partition_manager.cc')", SCONSCRIPT)


if __name__ == "__main__":
    unittest.main()
