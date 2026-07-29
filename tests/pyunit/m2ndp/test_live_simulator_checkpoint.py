import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock


REPO = Path(__file__).resolve().parents[3]
MODULE_PATH = REPO / "scripts/live_simulator_checkpoint.py"


def load_checkpoint_module():
    spec = importlib.util.spec_from_file_location(
        "live_simulator_checkpoint", MODULE_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


checkpoint = load_checkpoint_module()


class ManifestTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.images = self.root / "images"
        self.images.mkdir()
        (self.images / "inventory.img").write_bytes(b"inventory")
        (self.images / "pstree.img").write_bytes(b"pstree")
        self.binary = self.root / "gem5.opt"
        self.binary.write_bytes(b"gem5")
        self.graph = self.root / "g20.sg"
        self.graph.write_bytes(b"graph")
        self.host = {
            "hostname": "gpu01",
            "kernel_release": "6.8.0-test",
            "boot_id": "boot-a",
            "criu_version": "Version: 4.0",
        }
        self.process_tree = [
            {
                "pid": 123,
                "ppid": 1,
                "cmdline": ["python3", "runner.py"],
            },
            {
                "pid": 124,
                "ppid": 123,
                "cmdline": [str(self.binary), "--outdir=run"],
            },
        ]

    def tearDown(self):
        self.temporary.cleanup()

    def build(self, **overrides):
        options = {
            "name": "amu",
            "unit": "gapbs-matched-pr-spmv-amu-g20-resume.service",
            "root_pid": 123,
            "process_tree": self.process_tree,
            "inputs": [self.binary, self.graph],
            "image_dir": self.images,
            "progress": {"kind": "gem5_tick", "value": 2850617862},
            "host": self.host,
        }
        options.update(overrides)
        return checkpoint.build_manifest(**options)

    def test_builds_and_validates_hashed_manifest(self):
        manifest = self.build()

        self.assertEqual(manifest["schema"], 1)
        self.assertEqual(manifest["name"], "amu")
        self.assertEqual(manifest["root_pid"], 123)
        self.assertEqual(
            manifest["images"]["inventory.img"]["sha256"],
            checkpoint.sha256_file(self.images / "inventory.img"),
        )
        checkpoint.validate_manifest(
            manifest,
            manifest_path=self.root / "manifest.json",
            require_same_kernel=False,
        )

    def test_rejects_empty_process_tree(self):
        with self.assertRaisesRegex(
            checkpoint.CheckpointError, "process tree is empty"
        ):
            self.build(process_tree=[])

    def test_rejects_missing_or_changed_image(self):
        manifest = self.build()
        (self.images / "pstree.img").unlink()
        with self.assertRaisesRegex(
            checkpoint.CheckpointError, "missing checkpoint image"
        ):
            checkpoint.validate_manifest(
                manifest,
                manifest_path=self.root / "manifest.json",
                require_same_kernel=False,
            )

        (self.images / "pstree.img").write_bytes(b"change")
        with self.assertRaisesRegex(
            checkpoint.CheckpointError, "checkpoint image hash mismatch"
        ):
            checkpoint.validate_manifest(
                manifest,
                manifest_path=self.root / "manifest.json",
                require_same_kernel=False,
            )

    def test_rejects_changed_input(self):
        manifest = self.build()
        self.graph.write_bytes(b"other")
        with self.assertRaisesRegex(
            checkpoint.CheckpointError, "checkpoint input hash mismatch"
        ):
            checkpoint.validate_manifest(
                manifest,
                manifest_path=self.root / "manifest.json",
                require_same_kernel=False,
            )

    def test_rejects_changed_kernel_when_required(self):
        manifest = self.build()
        with mock.patch.object(
            checkpoint.platform, "release", return_value="6.9.0-other"
        ):
            with self.assertRaisesRegex(
                checkpoint.CheckpointError, "kernel release mismatch"
            ):
                checkpoint.validate_manifest(
                    manifest,
                    manifest_path=self.root / "manifest.json",
                    require_same_kernel=True,
                )

    def test_transaction_requires_both_workloads(self):
        manifest = self.build()
        amu_path = self.root / "amu/manifest.json"
        amu_path.parent.mkdir()
        amu_path.write_text(json.dumps(manifest))

        with self.assertRaisesRegex(
            checkpoint.CheckpointError, "missing workload m2ndp"
        ):
            checkpoint.validate_transaction(
                {
                    "schema": 1,
                    "state": "capturing",
                    "workloads": {"amu": str(amu_path)},
                },
                require_ready=False,
                require_same_kernel=False,
            )

        m2ndp_manifest = self.build(name="m2ndp", unit="m2ndp.service")
        m2ndp_path = self.root / "m2ndp/manifest.json"
        m2ndp_path.parent.mkdir()
        m2ndp_path.write_text(json.dumps(m2ndp_manifest))
        transaction = {
            "schema": 1,
            "state": "ready_for_reboot",
            "workloads": {
                "amu": str(amu_path),
                "m2ndp": str(m2ndp_path),
            },
        }
        checkpoint.validate_transaction(
            transaction,
            require_ready=True,
            require_same_kernel=False,
        )


if __name__ == "__main__":
    unittest.main()
