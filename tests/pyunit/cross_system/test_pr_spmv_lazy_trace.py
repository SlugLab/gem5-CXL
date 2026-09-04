# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

import hashlib
import struct
import tempfile
import unittest
from array import array
from pathlib import Path

from scripts import canonical_work_trace as canonical
from scripts import lazy_work_trace as lazy
from scripts import pr_spmv_lazy_trace as pagerank


def digest(value):
    if isinstance(value, str):
        value = value.encode("utf-8")
    return hashlib.sha256(value).hexdigest()


class PageRankLazyTraceTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def build(self, *, iterations=1):
        # Incoming CSR: 2 -> 0, 0 -> 1, and 0,1 -> 2.
        return pagerank.build_bundle(
            self.root / f"trace-{iterations}",
            offsets=(0, 1, 2, 4),
            neighbors=(2, 0, 0, 1),
            out_degrees=(2, 1, 1),
            graph_sha256=digest("g14.sg"),
            source_sha256=digest("source"),
            binary_sha256=digest("binary"),
            config_sha256=digest("config"),
            graph_scale=14,
            iterations=iterations,
            verify_expansion=True,
        )

    def test_one_iteration_preserves_pagerank_float_order(self):
        bundle = self.build()
        operations = tuple(lazy.iter_operations(
            bundle, pagerank.EXPANDERS, batch_work_items=2
        ))
        opcodes = [row.opcode for row in operations]
        self.assertEqual(opcodes[0], canonical.Opcode.BARRIER)
        self.assertEqual(opcodes[1:5], [
            canonical.Opcode.LOAD_F32,
            canonical.Opcode.LOAD_U32,
            canonical.Opcode.F32_DIV,
            canonical.Opcode.STORE_F32,
        ])
        contribution_barrier = 1 + 4 * 3
        self.assertEqual(
            opcodes[contribution_barrier], canonical.Opcode.BARRIER
        )
        self.assertEqual(opcodes[-2:], [
            canonical.Opcode.BARRIER, canonical.Opcode.COMMIT,
        ])

        expected_contribution = pagerank.f32(pagerank.f32(1.0 / 3.0) / 2.0)
        divide = operations[3]
        self.assertEqual(divide.operand0, pagerank.raw_f32(1.0 / 3.0))
        self.assertEqual(divide.operand1, pagerank.raw_f32(2.0))
        self.assertEqual(divide.result, pagerank.raw_f32(expected_contribution))

        stores = [
            row for row in operations
            if row.opcode == canonical.Opcode.STORE_F32
        ]
        final_scores = stores[-3:]
        initial = pagerank.f32(1.0 / 3.0)
        contributions = (
            pagerank.f32(initial / 2.0), initial, initial,
        )
        damping = pagerank.f32(0.85)
        base = pagerank.f32(
            pagerank.f32(1.0 - damping) / pagerank.f32(3.0)
        )
        expected = (
            pagerank.f32(pagerank.f32(damping * contributions[2]) + base),
            pagerank.f32(pagerank.f32(damping * contributions[0]) + base),
            pagerank.f32(
                pagerank.f32(
                    damping * pagerank.f32(
                        contributions[0] + contributions[1]
                    )
                ) + base
            ),
        )
        self.assertEqual(
            [row.result for row in final_scores],
            [pagerank.raw_f32(value) for value in expected],
        )

    def test_bundle_separates_graph_and_initial_state_identities(self):
        bundle = self.build(iterations=20)
        self.assertEqual(bundle.meta["workload"], "pr_spmv")
        self.assertEqual(bundle.meta["graph_scale"], 14)
        self.assertEqual(bundle.meta["iterations"], 20)
        self.assertEqual(bundle.meta["graph_sha256"], digest("g14.sg"))
        self.assertEqual(
            bundle.meta["input_sha256"],
            lazy.initial_state_sha256(bundle.meta, bundle.arrays),
        )
        self.assertNotEqual(
            bundle.meta["input_sha256"], bundle.meta["graph_sha256"]
        )
        self.assertEqual(len(bundle.invocations), 20)

    def test_zero_degree_vertex_has_finite_zero_contribution(self):
        bundle = pagerank.build_bundle(
            self.root / "zero-degree",
            offsets=(0, 0, 1), neighbors=(1,), out_degrees=(0, 1),
            graph_sha256=digest("g14.sg"),
            source_sha256=digest("source"),
            binary_sha256=digest("binary"),
            config_sha256=digest("config"), graph_scale=14,
            iterations=1, verify_expansion=True,
        )
        operations = tuple(lazy.iter_operations(bundle, pagerank.EXPANDERS))
        first_vertex = [
            operation for operation in operations
            if operation.work_item == 0
            and operation.opcode in {
                canonical.Opcode.F32_DIV, canonical.Opcode.STORE_F32,
            }
        ]
        self.assertEqual(
            [operation.opcode for operation in first_vertex[:1]],
            [canonical.Opcode.STORE_F32],
        )
        self.assertEqual(first_vertex[0].result, pagerank.raw_f32(0.0))

    def test_build_from_csr_rejects_graph_hash_drift(self):
        csr = self.root / "csr"
        csr.mkdir()
        (csr / "in_offsets.u64").write_bytes(struct.pack("<4Q", 0, 1, 2, 4))
        (csr / "in_neighbors.i32").write_bytes(struct.pack("<4I", 2, 0, 0, 1))
        (csr / "out_degree.u32").write_bytes(struct.pack("<3I", 2, 1, 1))
        (csr / "graph.meta.json").write_text(
            '{"schema":1,"directed":false,"num_nodes":3,'
            '"num_directed_edges":4,"graph_sha256":"' + digest("g14.sg")
            + '"}\n', encoding="utf-8",
        )
        graph = self.root / "g14.sg"
        graph.write_bytes(b"g14.sg")
        with self.assertRaisesRegex(
            pagerank.PageRankTraceError, "G14 graph identity differs"
        ):
            pagerank.build_bundle_from_csr(
                self.root / "bad-trace", csr_root=csr,
                graph_path=graph, graph_sha256="0" * 64,
                source_sha256=digest("source"),
                binary_sha256=digest("binary"),
                config_sha256=digest("config"), graph_scale=14,
                iterations=20,
            )

    def test_g14_sized_descriptor_stays_bounded(self):
        import tracemalloc

        nodes = 1 << 14
        offsets = array("Q", range(nodes + 1))
        neighbors = array("I", range(nodes))
        out_degrees = array("I", [1]) * nodes
        tracemalloc.start()
        try:
            bundle = pagerank.build_bundle(
                self.root / "bounded", offsets=offsets,
                neighbors=neighbors, out_degrees=out_degrees,
                graph_sha256=digest("g14.sg"),
                source_sha256=digest("source"),
                binary_sha256=digest("binary"),
                config_sha256=digest("config"), graph_scale=14,
                iterations=20, verify_expansion=False,
            )
            _current, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertEqual(len(bundle.invocations), 20)
        self.assertLess(peak, 256 * 1024 * 1024)


if __name__ == "__main__":
    unittest.main()
