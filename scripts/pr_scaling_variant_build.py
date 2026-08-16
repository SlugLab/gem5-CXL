#!/usr/bin/env python3
# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

"""Build and validate one scale's formal AMU/CIRA PR variants."""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]


class VariantBuildError(RuntimeError):
    """A scale-local variant build violates its evidence contract."""


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_json(path, label):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise VariantBuildError(f"invalid {label}: {error}") from error
    if not isinstance(value, dict):
        raise VariantBuildError(f"{label} must be a JSON object")
    return value


def build_command(
    *, baseline_build, staging, final, cxlmemuring, m5_library, calibration,
):
    return [
        sys.executable,
        str(REPO / "scripts/build_gapbs_matched_pr_spmv_variants.py"),
        "--baseline-build", str(Path(baseline_build).resolve()),
        "--outdir", str(Path(staging).resolve()),
        "--recorded-outdir", str(Path(final).resolve()),
        "--cxlmemuring", str(Path(cxlmemuring).resolve()),
        "--m5-library", str(Path(m5_library).resolve()),
        "--cira-mode", "pgo-selected",
        "--calibration-manifest", str(Path(calibration).resolve()),
        "--cira-row-batch", "64",
        "--cira-policy-latency-ns", "1000",
    ]


def _physical_output_path(recorded_path, *, physical_root, recorded_root):
    recorded_path = Path(recorded_path).resolve()
    recorded_root = Path(recorded_root).resolve()
    try:
        relative = recorded_path.relative_to(recorded_root)
    except ValueError as error:
        raise VariantBuildError(
            f"variant output is outside recorded root: {recorded_path}"
        ) from error
    return Path(physical_root).resolve() / relative


def validate_variant_build(
    output, *, baseline_build, calibration, recorded_root=None,
):
    output = Path(output).resolve()
    recorded_root = (
        output if recorded_root is None else Path(recorded_root).resolve()
    )
    manifest_path = output / "manifest.json"
    manifest = _load_json(manifest_path, "variant manifest")
    baseline_manifest = Path(baseline_build).resolve() / "manifest.json"
    expected = {
        "benchmark": "pr_spmv",
        "page_rank_iterations": 20,
        "fixed_iterations": True,
        "fp_contract": False,
        "fast_math": False,
        "baseline_manifest_sha256": sha256_file(baseline_manifest),
        "cira_mode": "pgo-selected",
        "cira_policy_latency_ns": 1000,
    }
    labels = {
        "baseline_manifest_sha256": "baseline manifest",
        "cira_mode": "CIRA mode",
        "cira_policy_latency_ns": "CIRA policy latency",
    }
    for field, expected_value in expected.items():
        if manifest.get(field) != expected_value:
            label = labels.get(field, field)
            raise VariantBuildError(
                f"{label} differs: {manifest.get(field)!r}, "
                f"expected {expected_value!r}"
            )
    policy = manifest.get("cira_policy")
    calibration_hash = sha256_file(calibration)
    if (
        not isinstance(policy, dict)
        or policy.get("calibration_manifest_sha256") != calibration_hash
    ):
        raise VariantBuildError("calibration manifest hash differs")
    rows = manifest.get("variants")
    if not isinstance(rows, list):
        raise VariantBuildError("variant manifest must contain AMU and CIRA")
    by_kind = {}
    for row in rows:
        if not isinstance(row, dict) or row.get("kind") in by_kind:
            raise VariantBuildError("variant manifest must contain AMU and CIRA")
        by_kind[row.get("kind")] = row
    if set(by_kind) != {"amu", "cira"}:
        raise VariantBuildError("variant manifest must contain AMU and CIRA")
    binary_hashes = {}
    for kind, row in sorted(by_kind.items()):
        try:
            binary = _physical_output_path(
                row["binary"],
                physical_root=output,
                recorded_root=recorded_root,
            )
        except KeyError as error:
            raise VariantBuildError(f"{kind} binary path is missing") from error
        if not binary.is_file():
            raise VariantBuildError(f"{kind} binary is missing: {binary}")
        actual_hash = sha256_file(binary)
        if row.get("binary_sha256") != actual_hash:
            raise VariantBuildError(f"{kind} binary hash differs")
        binary_hashes[kind] = actual_hash
    return {
        "manifest_sha256": sha256_file(manifest_path),
        "baseline_manifest_sha256": expected["baseline_manifest_sha256"],
        "calibration_sha256": calibration_hash,
        "cira_mode": manifest["cira_mode"],
        "cira_policy_latency_ns": manifest["cira_policy_latency_ns"],
        "binary_sha256": binary_hashes,
    }


def ensure_variant_build(
    final, *, baseline_build, cxlmemuring, m5_library, calibration, log,
):
    final = Path(final).resolve()
    if final.exists():
        return validate_variant_build(
            final,
            baseline_build=baseline_build,
            calibration=calibration,
        )
    final.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(
        prefix=f".{final.name}.staging-", dir=final.parent
    ))
    command = build_command(
        baseline_build=baseline_build,
        staging=staging,
        final=final,
        cxlmemuring=cxlmemuring,
        m5_library=m5_library,
        calibration=calibration,
    )
    try:
        log = Path(log)
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("w", encoding="utf-8") as stream:
            completed = subprocess.run(
                command,
                cwd=REPO,
                stdout=stream,
                stderr=subprocess.STDOUT,
                text=True,
                check=False,
            )
        if completed.returncode != 0:
            raise VariantBuildError(
                f"variant builder exited {completed.returncode}; see {log}"
            )
        validate_variant_build(
            staging,
            baseline_build=baseline_build,
            calibration=calibration,
            recorded_root=final,
        )
        os.rename(staging, final)
        result = validate_variant_build(
            final,
            baseline_build=baseline_build,
            calibration=calibration,
        )
        result["command"] = [str(item) for item in command]
        return result
    finally:
        if staging.exists():
            shutil.rmtree(staging)
