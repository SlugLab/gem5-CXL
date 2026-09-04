import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from scripts import canonical_work_trace as canonical
from scripts import prepare_g14_24cell_registry as prepare
from scripts import timing_evidence_24cell as evidence


def digest(value):
    if isinstance(value, str):
        value = value.encode("utf-8")
    return hashlib.sha256(value).hexdigest()


class PrepareG1424CellRegistryTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.graph = self.root / "g14.sg"
        self.graph.write_bytes(b"g14")
        self.graph_sha256 = digest(b"g14")

    def file_record(self, path):
        return {
            "path": str(path.resolve()),
            "sha256": prepare.sha256_file(path),
        }

    def source(self, workload, input_sha256=None):
        input_sha256 = input_sha256 or digest(f"input-{workload}")
        root = self.root / workload
        root.mkdir()
        operations = (
            canonical.Operation(
                1, canonical.Opcode.BARRIER, 1, 0, 0, 0, 1, 0
            ),
            canonical.Operation(
                1, canonical.Opcode.COMMIT, 1, 1, 0, 0, 1, 0
            ),
        )
        trace = canonical.write_bundle(root, {
            "schema": 1,
            "workload": workload,
            "input_sha256": input_sha256,
            "source_sha256": digest(f"source-{workload}"),
            "binary_sha256": digest("binary"),
            "config_sha256": digest("config"),
            "phases": [{"id": 1, "name": workload, "work_items": 1}],
            "output_boundaries": {},
            "source_trace_sha256": digest("source-trace"),
        }, operations, {})
        window = root / "window.json"
        window.write_text("{}\n", encoding="utf-8")
        row = {
            "input_sha256": input_sha256,
            "trace": self.file_record(trace),
            "window_manifest": self.file_record(window),
            "phase": 1,
            "window_index": 0,
        }
        if workload in {"pr_spmv", "gap_bc"}:
            row["graph_sha256"] = self.graph_sha256
        return row

    def test_sidecar_fixed_trace_binding_is_hash_valid(self):
        row = self.source("npb_cg")
        fixed_root = self.root / "npb-fixed"
        operations = (
            canonical.Operation(
                1, canonical.Opcode.BARRIER, 1, 0, 0, 0, 1, 0
            ),
            canonical.Operation(
                1, canonical.Opcode.COMMIT, 1, 1, 0, 0, 1, 0
            ),
        )
        fixed = canonical.write_bundle(fixed_root, {
            "schema": 1,
            "workload": "npb_cg",
            "input_sha256": row["input_sha256"],
            "source_sha256": digest("source-npb_cg"),
            "binary_sha256": digest("binary"),
            "config_sha256": digest("config"),
            "source_trace_sha256": digest("source-trace"),
            "phases": [{"id": 1, "name": "npb_cg", "work_items": 1}],
            "output_boundaries": {},
        }, operations, {})
        fixed_meta = prepare._load_json(fixed, "fixed")
        sidecar = self.root / "npb-sidecar.json"
        sidecar.write_text(json.dumps({
            "source_trace_sha256": digest("source-trace"),
            "fixed_trace_sha256": fixed_meta["trace_sha256"],
        }) + "\n", encoding="utf-8")
        row["fixed_trace"] = self.file_record(fixed)
        row["materialization_record"] = self.file_record(sidecar)
        observed = prepare._validate_source(
            "npb_cg", row, self.graph_sha256
        )
        self.assertEqual(observed, row)

    def sources(self):
        return {
            workload: self.source(workload)
            for workload in evidence.WORKLOADS
        }

    def test_registry_has_exact_matrix_and_g14_identity(self):
        registry = prepare.build_registry(
            graph_path=self.graph,
            graph_sha256=self.graph_sha256,
            sources=self.sources(),
            graph_scale=14,
        )
        self.assertEqual(set(registry["cells"]), {
            f"{workload}:{latency}"
            for workload, latency in evidence.COORDINATES
        })
        self.assertEqual(registry["graph"]["scale"], 14)
        self.assertEqual(
            registry["graph"]["sha256"], self.graph_sha256
        )
        self.assertEqual(registry["status"], "verified")

    def test_registry_rejects_g20_graph_digest(self):
        with self.assertRaisesRegex(
            prepare.RegistryError, "G14 graph identity differs"
        ):
            prepare.build_registry(
                graph_path=self.graph,
                graph_sha256=prepare.G20_SHA256,
                sources=self.sources(), graph_scale=14,
            )

    def test_registry_rejects_trace_workload_relabeling(self):
        sources = self.sources()
        sources["mcf"]["trace"] = sources["amg_gather"]["trace"]
        with self.assertRaisesRegex(
            prepare.RegistryError, "trace workload differs"
        ):
            prepare.build_registry(
                graph_path=self.graph,
                graph_sha256=self.graph_sha256,
                sources=sources, graph_scale=14,
            )

    def test_registry_rejects_trace_input_relabeling_before_write(self):
        sources = self.sources()
        sources["npb_cg"]["input_sha256"] = digest("changed")
        output = self.root / "registry.json"
        with self.assertRaisesRegex(
            prepare.RegistryError, "trace input SHA-256 differs"
        ):
            prepare.write_registry(
                output,
                graph_path=self.graph,
                graph_sha256=self.graph_sha256,
                sources=sources, graph_scale=14,
            )
        self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
