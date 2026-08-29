# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

import hashlib
import json
import struct
import tempfile
import unittest
from array import array
from pathlib import Path

from scripts import canonical_work_trace as canonical
from scripts import gap_bc_lazy_trace as bc
from scripts import lazy_work_trace as lazy
from scripts import m2ndp_workload_trace as m2ndp
from scripts import run_matched_breadth_gem5 as replay
from scripts import stratified_timing as timing


def digest(label):
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


class GapBCLazyTraceTest(unittest.TestCase):
    OFFSETS = (0, 2, 3, 4, 4)
    NEIGHBORS = (1, 2, 3, 3)

    def _build(self, root, *, compact=False):
        return bc.build_bundle(
            root,
            offsets=self.OFFSETS,
            neighbors=self.NEIGHBORS,
            source=0,
            source_sha256=digest("bc-source"),
            binary_sha256=digest("bc-binary"),
            config_sha256=digest("bc-config"),
            compact=compact,
        )

    def test_small_diamond_preserves_bfs_and_reverse_vertex_order(self):
        with tempfile.TemporaryDirectory() as temporary:
            bundle = self._build(Path(temporary) / "bc")
        self.assertEqual(bundle.meta["workload"], "gap_bc")
        self.assertEqual(
            [(row.kernel, row.parameters.get("vertex"))
             for row in bundle.invocations],
            [
                ("gap_bc_reset", None),
                ("gap_bc_source_init", None),
                ("gap_bc_bfs_vertex", 0),
                ("gap_bc_bfs_vertex", 1),
                ("gap_bc_bfs_vertex", 2),
                ("gap_bc_bfs_vertex", 3),
                ("gap_bc_reverse_vertex", 3),
                ("gap_bc_reverse_vertex", 1),
                ("gap_bc_reverse_vertex", 2),
                ("gap_bc_reverse_vertex", 0),
                ("gap_bc_normalize", None),
            ],
        )

    def test_expansion_commits_every_invocation_and_matches_declared_count(self):
        with tempfile.TemporaryDirectory() as temporary:
            bundle = self._build(Path(temporary) / "bc")
            operations = tuple(lazy.iter_operations(bundle, bc.EXPANDERS))
        self.assertEqual(
            len(operations), bundle.dynamic_work["primitive_records"]
        )
        self.assertEqual(
            sum(row.opcode == canonical.Opcode.COMMIT for row in operations),
            len(bundle.invocations),
        )
        self.assertEqual(
            sum(row.opcode == canonical.Opcode.BARRIER for row in operations),
            len(bundle.invocations),
        )

    def test_normalized_scores_are_exact_deterministic_boundary(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "bc"
            bundle = self._build(root)
            evidence = bc.expanded_evidence(bundle)
            expected = tuple(
                struct.unpack("<I", struct.pack("<f", value))[0]
                for value in (1.0, 1.0 / 6.0, 1.0 / 6.0, 0.0)
            )
            payload = struct.pack("<4I", *expected)
        self.assertEqual(evidence["boundaries"]["scores.final"], {
            "word_bits": 32,
            "count": 4,
            "raw_words": list(expected),
            "sha256": hashlib.sha256(payload).hexdigest(),
        })
        self.assertEqual(
            bundle.meta["boundary_commitments"]["scores.final"],
            hashlib.sha256(payload).hexdigest(),
        )

    def test_descriptor_hash_is_independent_of_bundle_root(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            left = self._build(root / "left")
            right = self._build(root / "right")
            left_payload = (left.root / "trace.v2.json").read_bytes()
            right_payload = (right.root / "trace.v2.json").read_bytes()
        self.assertEqual(left_payload, right_payload)
        self.assertEqual(
            hashlib.sha256(left_payload).hexdigest(),
            hashlib.sha256(right_payload).hexdigest(),
        )

    def test_invalid_csr_and_partial_vertex_slice_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaisesRegex(bc.BCTraceError, "offset"):
                bc.build_bundle(
                    root / "bad", offsets=(0, 2, 1), neighbors=(1, 0),
                    source=0, source_sha256=digest("s"),
                    binary_sha256=digest("b"),
                    config_sha256=digest("c"),
                )
            bundle = self._build(root / "good")
            invocation = next(
                row for row in bundle.invocations
                if row.kernel == "gap_bc_bfs_vertex"
                and row.parameters["vertex"] == 0
            )
            with lazy.MappedState(bundle) as state:
                with self.assertRaisesRegex(
                    lazy.LazyTraceError, "whole vertex"
                ):
                    tuple(bc.expand_slice(state, invocation, 0, 1))

    def test_descriptor_binds_phase_names_and_g20_input_shape(self):
        with tempfile.TemporaryDirectory() as temporary:
            bundle = self._build(Path(temporary) / "bc")
            descriptor = json.loads(
                (bundle.root / "trace.v2.json").read_text(encoding="utf-8")
            )
        self.assertEqual(
            descriptor["meta"]["phase_names"],
            {
                str(bc.PHASE_RESET): "bc_reset",
                str(bc.PHASE_SOURCE_INIT): "bc_source_init",
                str(bc.PHASE_BFS): "bc_bfs",
                str(bc.PHASE_REVERSE): "bc_reverse",
                str(bc.PHASE_NORMALIZE): "bc_normalize",
            },
        )
        self.assertEqual(descriptor["meta"]["nodes"], 4)
        self.assertEqual(descriptor["meta"]["directed_edges"], 4)

    def test_generic_m2ndp_lowering_accepts_bc_and_keeps_score_boundary(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bundle = self._build(root / "bc")
            tools = root / "tools"
            tools.mkdir()
            funcsim = tools / "FuncSim"
            ndpsim = tools / "NDPSim"
            funcsim.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            ndpsim.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            funcsim.chmod(0o755)
            ndpsim.chmod(0o755)
            patch = tools / "canonical.patch"
            patch.write_text("canonical\n", encoding="utf-8")
            config = tools / "m2ndp.config"
            config.write_text("num_ndp_units=1\n", encoding="utf-8")
            provenance = m2ndp.PackageProvenance(
                trace_sha256=hashlib.sha256(
                    (bundle.root / "trace.v2.json").read_bytes()
                ).hexdigest(),
                input_sha256=bundle.meta["input_sha256"],
                funcsim_path=str(funcsim), ndpsim_path=str(ndpsim),
                patch_paths=(str(patch),), config_path=str(config),
            )
            manifest_path = m2ndp.lower_bundle(
                bundle.root, root / "package", provenance=provenance
            )
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(set(manifest["output_boundaries"]), {"scores.final"})
        self.assertEqual(
            manifest["operation_count"],
            bundle.dynamic_work["primitive_records"],
        )
        self.assertEqual(manifest["dynamic_launches"], len(bundle.invocations))

    def test_compact_depth_invocations_match_vertex_trace_boundary(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            vertex = self._build(root / "vertex")
            compact = self._build(root / "compact", compact=True)
            vertex_evidence = bc.expanded_evidence(vertex)
            compact_evidence = bc.expanded_evidence(compact)
            vertex_size = (vertex.root / "trace.v2.json").stat().st_size
            compact_size = (compact.root / "trace.v2.json").stat().st_size
        self.assertEqual(
            [row.kernel for row in compact.invocations],
            [
                "gap_bc_reset", "gap_bc_source_init",
                "gap_bc_bfs_level", "gap_bc_bfs_level",
                "gap_bc_bfs_level", "gap_bc_reverse_level",
                "gap_bc_reverse_level", "gap_bc_reverse_level",
                "gap_bc_normalize",
            ],
        )
        self.assertEqual(
            compact_evidence["boundaries"], vertex_evidence["boundaries"]
        )
        self.assertLess(compact_size, vertex_size)

    def test_csr_file_builder_requires_undirected_hash_bound_graph(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            csr = root / "csr"
            csr.mkdir()
            with (csr / "in_offsets.u64").open("wb") as stream:
                array("Q", self.OFFSETS).tofile(stream)
            with (csr / "in_neighbors.i32").open("wb") as stream:
                array("i", self.NEIGHBORS).tofile(stream)
            graph_sha256 = digest("g20.sg")
            meta = {
                "schema": 1, "directed": False,
                "graph_sha256": graph_sha256,
                "num_nodes": 4, "num_directed_edges": 4,
            }
            (csr / "graph.meta.json").write_text(
                json.dumps(meta), encoding="utf-8"
            )
            bundle = bc.build_bundle_from_csr(
                root / "formal", csr_root=csr, source=0,
                graph_sha256=graph_sha256,
                source_sha256=digest("source"),
                binary_sha256=digest("binary"),
                config_sha256=digest("config"),
            )
            self.assertEqual(len(bundle.invocations), 9)
            meta["directed"] = True
            (csr / "graph.meta.json").write_text(
                json.dumps(meta), encoding="utf-8"
            )
            with self.assertRaisesRegex(bc.BCTraceError, "undirected"):
                bc.build_bundle_from_csr(
                    root / "directed", csr_root=csr, source=0,
                    graph_sha256=graph_sha256,
                    source_sha256=digest("source"),
                    binary_sha256=digest("binary"),
                    config_sha256=digest("config"),
                )

    def test_compact_level_slices_preserve_whole_vertex_operation_order(self):
        with tempfile.TemporaryDirectory() as temporary:
            bundle = self._build(Path(temporary) / "bc", compact=True)
            target = next(
                row for row in bundle.invocations
                if row.kernel == "gap_bc_bfs_level"
                and row.work_items == 2
            )

            def prefix(state):
                for invocation in bundle.invocations[:target.ordinal]:
                    tuple(bc.EXPANDERS[invocation.kernel](state, invocation, 8))

            with lazy.MappedState(bundle) as full_state:
                prefix(full_state)
                full = tuple(
                    bc.EXPANDERS[target.kernel](full_state, target, 8)
                )
            with lazy.MappedState(bundle) as sliced_state:
                prefix(sliced_state)
                left = tuple(
                    bc.expand_slice(
                        sliced_state, target, 0, 1,
                        include_controls=False,
                    )
                )
                right = tuple(
                    bc.expand_slice(
                        sliced_state, target, 1, 2,
                        include_controls=True,
                    )
                )
        self.assertEqual(left + right, full)

    def test_bc_window_partition_stops_after_selected_complete_vertices(self):
        with tempfile.TemporaryDirectory() as temporary:
            bundle = self._build(Path(temporary) / "bc", compact=True)
            state = {"fixed": 0}
            rows = tuple(replay._partition_bc_lazy_window(
                bundle, bc.PHASE_BFS,
                timing.TimingWindow(
                    stratum=0, warmup_start=0,
                    measure_start=1, measure_stop=2,
                ),
                state,
            ))
        dynamic = [operation for fixed, operation in rows if not fixed]
        fixed = [operation for is_fixed, operation in rows if is_fixed]
        self.assertTrue(dynamic)
        self.assertEqual({row.work_item for row in dynamic}, {0, 1})
        self.assertEqual(len(fixed), 6)
        self.assertEqual(state["phase_items"], 4)
        self.assertEqual(state["expansion_mode"], "bounded-gap-bc")
        self.assertLess(
            state["expanded"], bundle.dynamic_work["primitive_records"]
        )

    def test_fast_forward_matches_canonical_state_without_operation_objects(self):
        with tempfile.TemporaryDirectory() as temporary:
            bundle = self._build(Path(temporary) / "bc", compact=True)
            stop = next(
                row.ordinal for row in bundle.invocations
                if row.kernel == "gap_bc_reverse_level"
            )
            names = ("depths", "path_counts", "deltas", "scores", "queue")
            with lazy.MappedState(bundle) as canonical_state:
                for invocation in bundle.invocations[:stop]:
                    tuple(bc.EXPANDERS[invocation.kernel](
                        canonical_state, invocation, 8
                    ))
                expected = {
                    name: canonical_state.boundary_sha256(name)
                    for name in names
                }
            with lazy.MappedState(bundle) as fast_state:
                for invocation in bundle.invocations[:stop]:
                    bc.fast_forward(fast_state, invocation)
                observed = {
                    name: fast_state.boundary_sha256(name)
                    for name in names
                }
        self.assertEqual(observed, expected)


if __name__ == "__main__":
    unittest.main()
