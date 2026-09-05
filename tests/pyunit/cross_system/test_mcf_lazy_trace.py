# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

import hashlib
import struct
import tempfile
import unittest
from pathlib import Path

from scripts import canonical_work_trace as canonical
from scripts import lazy_work_trace as lazy
from scripts import lazy_workload_registry as registry
from scripts import mcf_lazy_trace as mcf
from scripts import run_matched_breadth_gem5 as replay
from scripts import stratified_timing as timing


def digest(payload):
    return hashlib.sha256(payload).hexdigest()


class McfLazyTraceTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def _image(self, name, role, base, values):
        payload = struct.pack(f"<{len(values)}Q", *values)
        path = self.root / f"{name}.u64"
        path.write_bytes(payload)
        return lazy.ArrayImage(
            name, role, "u64", len(values), base, path.name,
            digest(payload),
        )

    def bundle(self):
        mask = (1 << 64) - 1
        arrays = (
            self._image("costs", "input", 0x100000000,
                        ((-5) & mask, 10, 2, 1)),
            self._image("tail_potentials", "input", 0x200000000,
                        (0, 20, (-3) & mask, 5)),
            self._image("head_potentials", "input", 0x300000000,
                        (0, 0, 4, 9)),
            self._image("idents", "input", 0x400000000,
                        (1, 2, 1, 2)),
            self._image("candidate_count", "state", 0x500000000, (0,)),
            self._image(
                "best_violation", "state", 0x500000008,
                ((1 << 63) - 1,),
            ),
        )
        meta = {
            "schema": 2,
            "workload": "mcf",
            "source_sha256": digest(b"source"),
            "binary_sha256": digest(b"binary"),
            "config_sha256": digest(b"config"),
            "initial_scalars": {},
            "scalar_addresses": {},
            "phase_names": {"401": "pricing_kernel"},
            "boundary_commitments": {},
        }
        meta["input_sha256"] = lazy.initial_state_sha256(meta, arrays)
        invocation = lazy.Invocation(
            ordinal=0, phase=401, kernel="mcf_pricing_window",
            iteration=0, work_items=4,
            parameters={
                "candidate_count": 2,
                "best_violation": -10,
                "record_count": 32,
            },
        )
        return lazy.LazyBundle(
            self.root, meta, arrays, (invocation,),
            {"primitive_records": 32},
        )

    def test_full_expansion_preserves_pricing_order_and_boundaries(self):
        bundle = self.bundle()
        operations = tuple(lazy.iter_operations(bundle, mcf.EXPANDERS))
        self.assertEqual(len(operations), 32)
        self.assertEqual(
            [row.opcode for row in operations[:7]],
            [
                canonical.Opcode.LOAD_U64,
                canonical.Opcode.LOAD_U64,
                canonical.Opcode.LOAD_U64,
                canonical.Opcode.LOAD_U64,
                canonical.Opcode.I64_ADD,
                canonical.Opcode.I64_ADD,
                canonical.Opcode.I64_MIN,
            ],
        )
        mask = (1 << 64) - 1
        # reduced = cost - tail_potential + head_potential, modulo 2^64.
        self.assertEqual(
            [(operations[i].operand0, operations[i].operand1,
              operations[i].result) for i in (4, 5, 6)],
            [
                ((-5) & mask, 0, (-5) & mask),
                ((-5) & mask, 0, (-5) & mask),
                ((1 << 63) - 1, (-5) & mask, (-5) & mask),
            ],
        )
        self.assertEqual(
            [row.opcode for row in operations[-4:]],
            [
                canonical.Opcode.STORE_U64,
                canonical.Opcode.STORE_U64,
                canonical.Opcode.BARRIER,
                canonical.Opcode.COMMIT,
            ],
        )
        self.assertEqual(operations[-4].result, 2)
        self.assertEqual(operations[-3].result, (-10) & mask)

    def test_fast_forward_then_slice_matches_full_dynamic_operations(self):
        bundle = self.bundle()
        invocation = bundle.invocations[0]
        with lazy.MappedState(bundle) as full_state:
            full = tuple(mcf.expand_slice(
                full_state, invocation, 0, 4, include_controls=False
            ))
        with lazy.MappedState(bundle) as sliced_state:
            mcf.fast_forward(sliced_state, invocation, 0, 2)
            sliced = tuple(mcf.expand_slice(
                sliced_state, invocation, 2, 4, include_controls=False
            ))
            self.assertEqual(sliced, full[14:])
            self.assertEqual(
                sliced_state.load_raw("candidate_count", 0)[1], 2
            )
            self.assertEqual(
                sliced_state.load_raw("best_violation", 0)[1],
                (-10) & ((1 << 64) - 1),
            )

    def test_registry_dispatches_mcf_and_exports_fixed_controls(self):
        bundle = self.bundle()
        invocation = bundle.invocations[0]
        self.assertIs(registry.module_for_kernel(invocation.kernel), mcf)
        controls = registry.fixed_controls(invocation)
        self.assertEqual(len(controls), 4)
        self.assertEqual(controls[0].result, 2)
        self.assertEqual(
            registry.boundary_specs(bundle, invocation),
            (
                ("candidate_count", 64, 1, 0x500000000),
                ("best_violation", 64, 1, 0x500000008),
            ),
        )

    def test_window_partition_expands_only_prefix_and_selected_items(self):
        bundle = self.bundle()
        state = {"fixed": 0}
        rows = tuple(replay._partition_sliceable_lazy_window(
            bundle, mcf.PHASE_PRICING,
            timing.TimingWindow(
                stratum=0, warmup_start=1, measure_start=2, measure_stop=4
            ),
            state,
        ))
        dynamic = [row for fixed, row in rows if not fixed]
        fixed = [row for is_fixed, row in rows if is_fixed]
        self.assertEqual(len(dynamic), 21)
        self.assertEqual({row.work_item for row in dynamic}, {0, 1, 2})
        self.assertEqual(len(fixed), 4)
        self.assertEqual(state["expanded"], 25)
        self.assertEqual(state["expansion_mode"], "bounded-mcf")


if __name__ == "__main__":
    unittest.main()
