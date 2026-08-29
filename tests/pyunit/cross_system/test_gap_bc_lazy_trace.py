# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

import hashlib
import json
import struct
import tempfile
import unittest
from pathlib import Path

from scripts import canonical_work_trace as canonical
from scripts import gap_bc_lazy_trace as bc
from scripts import lazy_work_trace as lazy
from scripts import m2ndp_workload_trace as m2ndp


def digest(label):
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


class GapBCLazyTraceTest(unittest.TestCase):
    OFFSETS = (0, 2, 3, 4, 4)
    NEIGHBORS = (1, 2, 3, 3)

    def _build(self, root):
        return bc.build_bundle(
            root,
            offsets=self.OFFSETS,
            neighbors=self.NEIGHBORS,
            source=0,
            source_sha256=digest("bc-source"),
            binary_sha256=digest("bc-binary"),
            config_sha256=digest("bc-config"),
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


if __name__ == "__main__":
    unittest.main()
