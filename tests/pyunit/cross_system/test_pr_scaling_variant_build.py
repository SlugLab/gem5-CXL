# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import pr_scaling_variant_build as stage
from scripts import cira_lead_policy


def sha256_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class PRScalingVariantBuildTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.baseline = self.root / "baseline"
        self.baseline.mkdir()
        (self.baseline / "manifest.json").write_text(
            '{"schema": 1}\n', encoding="utf-8"
        )
        self.calibration = self.root / "calibration.json"
        self.calibration.write_text(
            '{"schema": 1, "status": "PASS"}\n', encoding="utf-8"
        )
        self.output = self.root / "g4"
        self.output.mkdir()
        self.binaries = {}
        rows = []
        for kind in ("amu", "cira"):
            binary = self.output / kind / "bin/pr_spmv"
            binary.parent.mkdir(parents=True)
            reference = self.output / "reference" / f"{kind}.u32"
            header = self.output / kind / "generated/m2ndp_experiment_config.h"
            header.parent.mkdir(parents=True)
            header.write_text(
                f'#define M2NDP_REFERENCE_RAW_PATH "{reference.resolve()}"\n',
                encoding="utf-8",
            )
            binary.write_bytes(
                f"{kind}-binary\0{reference.resolve()}\0".encode()
            )
            self.binaries[kind] = binary
            rows.append({
                "kind": kind,
                "binary": str(binary.resolve()),
                "binary_sha256": sha256_file(binary),
                "reference_raw": str(reference.resolve()),
            })
        self.manifest = {
            "schema": 1,
            "benchmark": "pr_spmv",
            "graph_scale": 12,
            "page_rank_iterations": 20,
            "fixed_iterations": True,
            "fp_contract": False,
            "fast_math": False,
            "baseline_manifest_sha256": sha256_file(
                self.baseline / "manifest.json"
            ),
            "cira_mode": "pgo-selected",
            "cira_policy_latency_ns": 1000,
            "cira_policy": {
                "calibration_manifest_sha256": sha256_file(
                    self.calibration
                ),
                "base_1us_lead_blocks": 32,
                "scale_derived": cira_lead_policy.effective_lead_for_scale(
                    12, num_threads=4, calibrated_lead_blocks=32
                ),
            },
            "variants": rows,
        }
        self.write_manifest()

    def write_manifest(self):
        (self.output / "manifest.json").write_text(
            json.dumps(self.manifest, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def validate(self):
        return stage.validate_variant_build(
            self.output,
            baseline_build=self.baseline,
            calibration=self.calibration,
            graph_scale=12,
        )

    def write_staged_build(self, staging, final):
        value = json.loads(json.dumps(self.manifest))
        rows = []
        for kind in ("amu", "cira"):
            physical = staging / kind / "bin/pr_spmv"
            physical.parent.mkdir(parents=True, exist_ok=True)
            reference = final / "reference" / f"{kind}.u32"
            header = staging / kind / "generated/m2ndp_experiment_config.h"
            header.parent.mkdir(parents=True)
            header.write_text(
                f'#define M2NDP_REFERENCE_RAW_PATH "{reference.resolve()}"\n',
                encoding="utf-8",
            )
            physical.write_bytes(
                f"atomic-{kind}\0{reference.resolve()}\0".encode()
            )
            rows.append({
                "kind": kind,
                "binary": str((final / kind / "bin/pr_spmv").resolve()),
                "binary_sha256": sha256_file(physical),
                "reference_raw": str(reference.resolve()),
            })
        value["variants"] = rows
        (staging / "manifest.json").write_text(
            json.dumps(value, sort_keys=True) + "\n", encoding="utf-8"
        )

    def test_validate_build_binds_all_semantic_inputs_and_binaries(self):
        record = self.validate()
        self.assertEqual(record["cira_mode"], "pgo-selected")
        self.assertEqual(record["cira_policy_latency_ns"], 1000)
        self.assertEqual(set(record["binary_sha256"]), {"amu", "cira"})
        self.assertEqual(
            record["manifest_sha256"],
            sha256_file(self.output / "manifest.json"),
        )

    def test_validate_build_rejects_semantic_identity_drift(self):
        cases = (
            ("baseline_manifest_sha256", "0" * 64, "baseline"),
            ("cira_mode", "legacy", "mode"),
            ("cira_policy_latency_ns", 500, "latency"),
        )
        for field, value, message in cases:
            with self.subTest(field=field):
                original = self.manifest[field]
                self.manifest[field] = value
                self.write_manifest()
                with self.assertRaisesRegex(stage.VariantBuildError, message):
                    self.validate()
                self.manifest[field] = original
                self.write_manifest()

    def test_validate_build_rejects_calibration_drift(self):
        self.manifest["cira_policy"]["calibration_manifest_sha256"] = "0" * 64
        self.write_manifest()
        with self.assertRaisesRegex(stage.VariantBuildError, "calibration"):
            self.validate()

    def test_validate_build_rejects_missing_or_duplicate_kind(self):
        self.manifest["variants"] = self.manifest["variants"][:1]
        self.write_manifest()
        with self.assertRaisesRegex(stage.VariantBuildError, "AMU and CIRA"):
            self.validate()

    def test_validate_build_rejects_missing_or_changed_binary(self):
        self.binaries["amu"].write_bytes(b"changed")
        with self.assertRaisesRegex(stage.VariantBuildError, "binary hash"):
            self.validate()
        self.binaries["amu"].unlink()
        with self.assertRaisesRegex(stage.VariantBuildError, "binary.*missing"):
            self.validate()

    def test_validate_build_rejects_staging_reference_embedded_in_binary(self):
        row = next(
            item for item in self.manifest["variants"]
            if item["kind"] == "amu"
        )
        stale = self.root / ".g4.staging-old/reference/amu.u32"
        self.binaries["amu"].write_bytes(f"amu-binary\0{stale}\0".encode())
        row["binary_sha256"] = sha256_file(self.binaries["amu"])
        self.write_manifest()
        with self.assertRaisesRegex(
            stage.VariantBuildError, "embedded reference path"
        ):
            self.validate()

    def test_build_command_pins_policy_and_final_recorded_root(self):
        command = stage.build_command(
            baseline_build=self.baseline,
            staging=self.root / ".g4.staging",
            final=self.output,
            cxlmemuring=self.root / "CXLMemUring",
            m5_library=self.root / "libm5.a",
            calibration=self.calibration,
            graph_scale=12,
        )
        expected = {
            "--cira-mode": "pgo-selected",
            "--cira-policy-latency-ns": "1000",
            "--cira-row-batch": "64",
            "--m5-library": str((self.root / "libm5.a").resolve()),
            "--recorded-outdir": str(self.output.resolve()),
            "--graph-scale": "12",
        }
        for option, value in expected.items():
            self.assertEqual(command[command.index(option) + 1], value)

    def test_ensure_build_validates_staging_then_atomically_publishes(self):
        final = self.root / "atomic-g12"

        def fake_run(command, **_kwargs):
            staging = Path(command[command.index("--outdir") + 1])
            recorded = Path(command[command.index("--recorded-outdir") + 1])
            self.write_staged_build(staging, recorded)
            return mock.Mock(returncode=0)

        with mock.patch.object(stage.subprocess, "run", side_effect=fake_run):
            record = stage.ensure_variant_build(
                final,
                baseline_build=self.baseline,
                cxlmemuring=self.root / "CXLMemUring",
                m5_library=self.root / "libm5.a",
                calibration=self.calibration,
                graph_scale=12,
                log=self.root / "variant.log",
            )

        self.assertTrue((final / "manifest.json").is_file())
        self.assertEqual(set(record["binary_sha256"]), {"amu", "cira"})
        self.assertEqual(
            record["command"][record["command"].index("--recorded-outdir") + 1],
            str(final.resolve()),
        )
        self.assertEqual(list(self.root.glob(".atomic-g12.staging-*")), [])

    def test_ensure_build_rejects_existing_invalid_final_without_overwrite(self):
        final = self.root / "invalid-g14"
        final.mkdir()
        marker = final / "do-not-overwrite"
        marker.write_text("preserve\n", encoding="utf-8")

        with mock.patch.object(stage.subprocess, "run") as run:
            with self.assertRaisesRegex(stage.VariantBuildError, "manifest"):
                stage.ensure_variant_build(
                    final,
                    baseline_build=self.baseline,
                    cxlmemuring=self.root / "CXLMemUring",
                    m5_library=self.root / "libm5.a",
                    calibration=self.calibration,
                    graph_scale=12,
                    log=self.root / "variant.log",
                )

        run.assert_not_called()
        self.assertEqual(marker.read_text(encoding="utf-8"), "preserve\n")

    def test_ensure_build_failure_cleans_only_its_staging_directory(self):
        final = self.root / "failed-g20"
        with mock.patch.object(
            stage.subprocess, "run", return_value=mock.Mock(returncode=7)
        ):
            with self.assertRaisesRegex(stage.VariantBuildError, "exited 7"):
                stage.ensure_variant_build(
                    final,
                    baseline_build=self.baseline,
                    cxlmemuring=self.root / "CXLMemUring",
                    m5_library=self.root / "libm5.a",
                    calibration=self.calibration,
                    graph_scale=12,
                    log=self.root / "variant.log",
                )

        self.assertFalse(final.exists())
        self.assertEqual(list(self.root.glob(".failed-g20.staging-*")), [])

    def test_validate_build_rejects_scale_or_derived_policy_drift(self):
        self.manifest["graph_scale"] = 14
        self.write_manifest()
        with self.assertRaisesRegex(stage.VariantBuildError, "graph scale"):
            self.validate()

        self.manifest["graph_scale"] = 12
        self.manifest["cira_policy"]["scale_derived"][
            "effective_rows"
        ] = 2048
        self.write_manifest()
        with self.assertRaisesRegex(stage.VariantBuildError, "derived"):
            self.validate()


if __name__ == "__main__":
    unittest.main()
