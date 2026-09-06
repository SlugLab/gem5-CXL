import hashlib
import json
import copy
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import canonical_work_trace as canonical
from scripts import prepare_g12_24cell_registry as prepare
from scripts import timing_evidence_24cell as evidence


def digest(value):
    if isinstance(value, str):
        value = value.encode("utf-8")
    return hashlib.sha256(value).hexdigest()


class PrepareG1224CellRegistryTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

        self.graph = self.root / "g12.sg"
        self.graph.write_bytes(b"g12")
        self.graph_sha256 = digest(b"g12")

        self.graph_manifest = self.root / "g12.manifest.json"
        self._write_graph_manifest()

        self.csr = self.root / "csr"
        self.csr.mkdir()
        (self.csr / "graph.meta.json").write_text(json.dumps({
            "schema": 1,
            "directed": False,
            "graph_sha256": self.graph_sha256,
            "num_nodes": 4096,
            "num_directed_edges": 96772,
        }) + "\n", encoding="utf-8")
        (self.csr / "in_neighbors.i32").write_bytes(b"\0" * (96772 * 4))
        (self.csr / "in_offsets.u64").write_bytes(b"\0" * ((4096 + 1) * 8))
        (self.csr / "out_degree.u32").write_bytes(b"\0" * (4096 * 4))

        self._patch_frozen_hashes()
        self._sources = {
            workload: self.source(workload)
            for workload in evidence.WORKLOADS
        }

    def _patch_frozen_hashes(self):
        values = {
            "G12_SHA256": self.graph_sha256,
            "G12_MANIFEST_SHA256": prepare.sha256_file(self.graph_manifest),
            "G12_CSR_SHA256": {
                name: prepare.sha256_file(self.csr / name)
                for name in prepare.G12_CSR_SHA256
            },
        }
        patcher = mock.patch.multiple(prepare, **values)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _write_graph_manifest(self, **changes):
        value = {
            "schema": 1,
            "scale": 12,
            "num_nodes": 4096,
            "directed_edges": 96772,
            "graph": str(self.graph.resolve()),
            "graph_sha256": self.graph_sha256,
        }
        value.update(changes)
        self.graph_manifest.write_text(
            json.dumps(value) + "\n", encoding="utf-8"
        )

    def file_record(self, path):
        return {
            "path": str(Path(path).resolve()),
            "sha256": prepare.sha256_file(path),
        }

    @property
    def graph_manifest_record(self):
        return self.file_record(self.graph_manifest)

    @property
    def csr_records(self):
        return {
            name: self.file_record(self.csr / name)
            for name in prepare.G12_CSR_SHA256
        }

    @property
    def graph_record(self):
        return {
            "path": str(self.graph.resolve()),
            "sha256": self.graph_sha256,
            "manifest": str(self.graph_manifest.resolve()),
            "manifest_sha256": prepare.sha256_file(self.graph_manifest),
            "scale": 12,
            "num_nodes": 4096,
            "directed_edges": 96772,
        }

    @property
    def shared_catalog(self):
        workloads = {
            workload: {
                "input": str((self.root / f"{workload}.input").resolve()),
                "input_sha256": digest(f"catalog-{workload}"),
                "source_sha256": digest(f"catalog-source-{workload}"),
            }
            for workload in ("mcf", "amg_gather", "lulesh_scatter", "npb_cg")
        }
        workloads["pr_spmv"] = {
            "input": "/stale/g20.sg",
            "input_sha256": digest("g20"),
            "scale": 20,
        }
        workloads["npb_mg"] = {"class": "D"}
        return {"schema": 1, "status": "verified", "workloads": workloads}

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

    def sources(self):
        return copy.deepcopy(self._sources)

    def build_registry(self, **changes):
        arguments = {
            "graph_path": self.graph,
            "graph_sha256": self.graph_sha256,
            "graph_manifest": self.graph_manifest_record,
            "csr_records": self.csr_records,
            "sources": self.sources(),
            "graph_scale": 12,
        }
        arguments.update(changes)
        return prepare.build_registry(**arguments)

    def test_registry_has_exact_g12_identity(self):
        registry = self.build_registry()
        self.assertEqual(registry["graph"]["scale"], 12)
        self.assertEqual(registry["graph"]["sha256"], self.graph_sha256)
        self.assertEqual(set(registry["cells"]), {
            f"{workload}:{latency}"
            for workload, latency in evidence.COORDINATES
        })

    def test_registry_rejects_non_g12_scale(self):
        for scale in (4, 14, 20):
            with self.subTest(scale=scale), self.assertRaisesRegex(
                prepare.RegistryError, "G12 graph identity differs"
            ):
                self.build_registry(graph_scale=scale)

    def test_input_manifest_selects_six_workloads_and_g12(self):
        sources = self.sources()
        value = prepare.build_input_manifest(
            shared_catalog=self.shared_catalog,
            graph_record=self.graph_record,
            registry_sources=sources,
        )
        self.assertEqual(value["status"], "accepted")
        self.assertEqual(set(value["workloads"]), set(evidence.WORKLOADS))
        for workload in ("pr_spmv", "gap_bc"):
            self.assertEqual(value["workloads"][workload]["scale"], 12)
            self.assertEqual(
                value["workloads"][workload]["sha256"], self.graph_sha256
            )
            self.assertEqual(
                value["workloads"][workload]["input_sha256"],
                sources[workload]["input_sha256"],
            )

    def test_registry_rejects_mutated_csr_before_write(self):
        records = self.csr_records
        path = self.csr / "in_neighbors.i32"
        with path.open("r+b") as stream:
            stream.seek(0)
            stream.write(b"X")
        output = self.root / "registry.json"
        with self.assertRaisesRegex(
            prepare.RegistryError, "G12 CSR in_neighbors.i32 SHA-256 differs"
        ):
            prepare.write_registry(
                output,
                graph_path=self.graph,
                graph_sha256=self.graph_sha256,
                graph_manifest=self.graph_manifest_record,
                csr_records=records,
                sources=self.sources(),
                graph_scale=12,
            )
        self.assertFalse(output.exists())

    def test_registry_rejects_mutated_graph_before_write(self):
        self.graph.write_bytes(b"changed-g12")
        output = self.root / "registry.json"
        with self.assertRaisesRegex(
            prepare.RegistryError, "G12 graph identity differs"
        ):
            prepare.write_registry(
                output,
                graph_path=self.graph,
                graph_sha256=self.graph_sha256,
                graph_manifest=self.graph_manifest_record,
                csr_records=self.csr_records,
                sources=self.sources(),
                graph_scale=12,
            )
        self.assertFalse(output.exists())

    def test_registry_rejects_csr_metadata_semantic_changes(self):
        path = self.csr / "graph.meta.json"
        for field, value in (
            ("num_nodes", 8192),
            ("num_directed_edges", 96773),
        ):
            with self.subTest(field=field):
                meta = {
                    "schema": 1,
                    "directed": False,
                    "graph_sha256": self.graph_sha256,
                    "num_nodes": 4096,
                    "num_directed_edges": 96772,
                }
                meta[field] = value
                path.write_text(json.dumps(meta) + "\n", encoding="utf-8")
                records = self.csr_records
                expected = dict(prepare.G12_CSR_SHA256)
                expected["graph.meta.json"] = records[
                    "graph.meta.json"
                ]["sha256"]
                with mock.patch.object(
                    prepare, "G12_CSR_SHA256", expected
                ), self.assertRaisesRegex(
                    prepare.RegistryError, "G12 CSR identity differs"
                ):
                    self.build_registry(csr_records=records)

    def test_registry_rejects_csr_array_size_change(self):
        path = self.csr / "out_degree.u32"
        path.write_bytes(path.read_bytes() + b"\0\0\0\0")
        records = self.csr_records
        expected = dict(prepare.G12_CSR_SHA256)
        expected["out_degree.u32"] = records["out_degree.u32"]["sha256"]
        with mock.patch.object(
            prepare, "G12_CSR_SHA256", expected
        ), self.assertRaisesRegex(
            prepare.RegistryError, "G12 CSR out_degree.u32 size differs"
        ):
            self.build_registry(csr_records=records)

    def test_registry_rejects_graph_manifest_semantic_changes(self):
        for field, value in (
            ("scale", 14),
            ("num_nodes", 8192),
            ("directed_edges", 96773),
        ):
            with self.subTest(field=field):
                self._write_graph_manifest(**{field: value})
                record = self.graph_manifest_record
                with mock.patch.object(
                    prepare, "G12_MANIFEST_SHA256", record["sha256"]
                ), self.assertRaisesRegex(
                    prepare.RegistryError,
                    "G12 graph manifest identity differs",
                ):
                    self.build_registry(graph_manifest=record)
                self._write_graph_manifest()

    def test_registry_rejects_graph_derived_source_hash(self):
        sources = self.sources()
        sources["gap_bc"]["graph_sha256"] = digest("other-graph")
        with self.assertRaisesRegex(
            prepare.RegistryError, "G12 graph identity differs"
        ):
            self.build_registry(sources=sources)


if __name__ == "__main__":
    unittest.main()
