import importlib.util
import json
import os
import subprocess
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


class CriuCommandTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.job = checkpoint.CaptureJob(
            name="amu",
            unit="amu.service",
            root_pid=123,
            snapshot_root=self.root,
        )

    def tearDown(self):
        self.temporary.cleanup()

    def test_probe_leaves_process_running_but_final_dump_does_not(self):
        probe = checkpoint.criu_dump_command(self.job, probe=True)
        final = checkpoint.criu_dump_command(self.job, probe=False)

        self.assertIn("--leave-running", probe)
        self.assertNotIn("--leave-running", final)
        for command in (probe, final):
            self.assertEqual(command[0], "criu")
            self.assertIn("--tree", command)
            self.assertIn("123", command)
            self.assertIn(str(self.job.image_dir), command)
            self.assertIn(str(self.job.work_dir), command)

    def test_preflight_requires_tools_space_and_live_roots(self):
        with self.assertRaisesRegex(
            checkpoint.CheckpointError, "CRIU executable is missing"
        ):
            checkpoint.validate_preflight(
                criu_path=None,
                crit_path="/usr/bin/crit",
                free_bytes=64 * 1024**3,
                root_pids_alive={"amu": True, "m2ndp": True},
            )
        with self.assertRaisesRegex(
            checkpoint.CheckpointError, "at least 32 GiB"
        ):
            checkpoint.validate_preflight(
                criu_path="/usr/sbin/criu",
                crit_path="/usr/bin/crit",
                free_bytes=31 * 1024**3,
                root_pids_alive={"amu": True, "m2ndp": True},
            )
        with self.assertRaisesRegex(
            checkpoint.CheckpointError, "root process is not alive: m2ndp"
        ):
            checkpoint.validate_preflight(
                criu_path="/usr/sbin/criu",
                crit_path="/usr/bin/crit",
                free_bytes=64 * 1024**3,
                root_pids_alive={"amu": True, "m2ndp": False},
            )

    def test_failed_dump_is_not_published(self):
        class FailedRunner:
            def __call__(self, command, **kwargs):
                return subprocess.CompletedProcess(
                    command, 17, stdout="", stderr="unsupported fd"
                )

        with self.assertRaisesRegex(
            checkpoint.CheckpointError, "CRIU dump failed with status 17"
        ):
            checkpoint.run_criu_dump(
                self.job,
                probe=False,
                runner=FailedRunner(),
            )
        self.assertFalse(self.job.manifest_path.exists())
        self.assertFalse(self.job.final_image_dir.exists())

    def test_successful_final_dump_is_atomically_published(self):
        binary = self.root / "NDPSim"
        binary.write_bytes(b"binary")

        class SuccessfulRunner:
            def __call__(inner_self, command, **kwargs):
                (self.job.image_dir / "inventory.img").write_bytes(
                    b"inventory"
                )
                (self.job.image_dir / "pstree.img").write_bytes(b"pstree")
                return subprocess.CompletedProcess(
                    command, 0, stdout="", stderr=""
                )

        manifest = checkpoint.capture_job(
            self.job,
            probe=False,
            inputs=[binary],
            progress={"kind": "launch", "value": 19},
            host={
                "hostname": "gpu01",
                "kernel_release": checkpoint.platform.release(),
                "boot_id": "boot-a",
                "criu_version": "Version: 4.2",
            },
            process_tree=[
                {
                    "pid": 123,
                    "ppid": 1,
                    "cmdline": ["NDPSim", "--serial-launch"],
                }
            ],
            runner=SuccessfulRunner(),
        )

        self.assertTrue(self.job.manifest_path.is_file())
        self.assertTrue(self.job.final_image_dir.is_dir())
        self.assertFalse(self.job.staging_root.exists())
        checkpoint.validate_manifest(
            manifest,
            manifest_path=self.job.manifest_path,
            require_same_kernel=True,
        )

    def test_probe_is_validated_then_removed_without_publication(self):
        class SuccessfulRunner:
            def __call__(inner_self, command, **kwargs):
                (self.job.image_dir / "inventory.img").write_bytes(
                    b"inventory"
                )
                (self.job.image_dir / "pstree.img").write_bytes(b"pstree")
                return subprocess.CompletedProcess(
                    command, 0, stdout="", stderr=""
                )

        result = checkpoint.capture_job(
            self.job,
            probe=True,
            inputs=[],
            progress={"kind": "probe", "value": 1},
            host={},
            process_tree=[{"pid": 123, "ppid": 1, "cmdline": ["runner"]}],
            runner=SuccessfulRunner(),
        )
        self.assertEqual(result, {"probe": "passed"})
        self.assertFalse(self.job.staging_root.exists())
        self.assertFalse(self.job.final_root.exists())

    def test_captures_current_process_tree(self):
        tree = checkpoint.capture_process_tree(os.getpid())
        by_pid = {entry["pid"]: entry for entry in tree}
        self.assertIn(os.getpid(), by_pid)
        self.assertTrue(by_pid[os.getpid()]["cmdline"])

    def test_resolves_positive_systemd_main_pid(self):
        def runner(command, **kwargs):
            self.assertEqual(
                command,
                [
                    "systemctl",
                    "show",
                    "--property",
                    "MainPID",
                    "--value",
                    "amu.service",
                ],
            )
            return subprocess.CompletedProcess(
                command, 0, stdout="205393\n", stderr=""
            )

        self.assertEqual(
            checkpoint.resolve_main_pid("amu.service", runner=runner),
            205393,
        )

    def test_cli_declares_preflight_probe_dump_and_validate(self):
        parser = checkpoint.build_parser()
        for action in ("preflight", "probe", "dump", "validate"):
            argv = [action, "--root", str(self.root)]
            if action in ("probe", "dump"):
                argv += ["--job", "amu"]
            options = parser.parse_args(argv)
            self.assertEqual(options.action, action)


if __name__ == "__main__":
    unittest.main()
