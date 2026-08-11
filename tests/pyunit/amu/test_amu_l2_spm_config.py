# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

import ast
import unittest
from pathlib import Path
from types import SimpleNamespace


REPO = Path(__file__).resolve().parents[3]
CONFIG = (
    REPO / "configs/example/gem5_library/x86-gapbs-amu-se.py"
).read_text(encoding="utf-8")


def load_helpers():
    tree = ast.parse(CONFIG)
    wanted = {
        "configure_amu_l2_spm",
        "connect_asmc_spm_ports",
        "validate_amu_spm_cardinality",
    }
    selected = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in wanted
    ]

    class Allocation:
        def __init__(self, partition_id, ways):
            self.partition_id = partition_id
            self.ways = list(ways)

    class WayPolicy:
        def __init__(self, allocations):
            self.allocations = allocations

    class Manager:
        def __init__(self, partitioning_policies, spm_partition_id):
            self.partitioning_policies = partitioning_policies
            self.spm_partition_id = spm_partition_id

    namespace = {
        "SpmPartitionManager": Manager,
        "WayPartitioningPolicy": WayPolicy,
        "WayPolicyAllocation": Allocation,
    }
    exec(compile(ast.Module(selected, []), "<amu-spm-helpers>", "exec"), namespace)
    return namespace


class RecordingAsmc:
    def __init__(self):
        object.__setattr__(self, "connections", [])

    def __setattr__(self, name, value):
        if name == "spm_side_ports":
            self.connections.append(value)
            return
        object.__setattr__(self, name, value)


class AmuL2SpmConfigTest(unittest.TestCase):
    def test_config_declares_exact_disjoint_six_plus_two_way_split(self):
        self.assertIn("partition_id=0, ways=[0, 1, 2, 3, 4, 5]", CONFIG)
        self.assertIn("partition_id=1, ways=[6, 7]", CONFIG)
        self.assertIn("SpmPartitionManager", CONFIG)

        helpers = load_helpers()
        l2 = SimpleNamespace(assoc=8, partitioning_manager=None)
        helpers["configure_amu_l2_spm"](l2)
        manager = l2.partitioning_manager
        self.assertEqual(manager.spm_partition_id, 1)
        allocations = manager.partitioning_policies[0].allocations
        cpu_ways = set(allocations[0].ways)
        spm_ways = set(allocations[1].ways)
        self.assertFalse(cpu_ways & spm_ways)
        self.assertEqual(cpu_ways | spm_ways, set(range(8)))

    def test_partition_configuration_rejects_non_eight_way_l2(self):
        helpers = load_helpers()
        for assoc in (4, 16):
            with self.subTest(assoc=assoc), self.assertRaisesRegex(
                RuntimeError, "exactly 8-way"
            ):
                helpers["configure_amu_l2_spm"](
                    SimpleNamespace(assoc=assoc, partitioning_manager=None)
                )

    def test_each_timing_core_gets_exactly_one_private_l2_spm_port(self):
        helpers = load_helpers()
        asmc = RecordingAsmc()
        buses = [SimpleNamespace(cpu_side_ports=f"l2-{index}") for index in range(4)]
        helpers["connect_asmc_spm_ports"](asmc, buses, 4)
        self.assertEqual(asmc.connections, ["l2-0", "l2-1", "l2-2", "l2-3"])

    def test_port_cardinality_mismatch_fails_closed(self):
        helpers = load_helpers()
        for buses in (3, 5):
            with self.subTest(buses=buses), self.assertRaisesRegex(
                RuntimeError, "one private L2 bus per timing core"
            ):
                helpers["validate_amu_spm_cardinality"](4, buses)

    def test_board_routes_spm_only_to_private_l2_buses(self):
        self.assertIn("asmc.spm_side_ports", CONFIG)
        self.assertIn("cache_hierarchy.l2buses", CONFIG)
        connect = CONFIG[
            CONFIG.index("def connect_asmc_spm_ports"):
            CONFIG.index("class CXLSimpleBoard")
        ]
        self.assertIn("l2bus.cpu_side_ports", connect)
        self.assertNotIn("asmc_io_cache", connect)
        self.assertNotIn("cxl_link", connect)

    def test_only_calibrated_amu_enables_l2_partitioning(self):
        self.assertIn('args.asmc_profile != "legacy"', CONFIG)
        self.assertIn("amu_l2_spm_partition=amu_l2_spm_partition", CONFIG)
        self.assertIn("l2_assoc=8 if amu_l2_spm_partition else None", CONFIG)


if __name__ == "__main__":
    unittest.main()
