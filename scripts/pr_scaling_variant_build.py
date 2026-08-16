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

try:
    from scripts import cira_lead_policy
except ImportError:
    import cira_lead_policy


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
    graph_scale,
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
        "--graph-scale", str(graph_scale),
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


def _validate_embedded_reference(
    kind, row, binary, *, physical_root, recorded_root,
):
    try:
        expected = str(Path(row["reference_raw"]).resolve())
    except KeyError as error:
        raise VariantBuildError(
            f"{kind} embedded reference path is missing"
        ) from error
    header = (
        Path(physical_root).resolve()
        / kind / "generated/m2ndp_experiment_config.h"
    )
    try:
        header_text = header.read_text(encoding="utf-8")
        binary_bytes = Path(binary).read_bytes()
    except (OSError, UnicodeDecodeError) as error:
        raise VariantBuildError(
            f"{kind} embedded reference path evidence is unreadable: {error}"
        ) from error
    if f'M2NDP_REFERENCE_RAW_PATH "{expected}"' not in header_text:
        raise VariantBuildError(
            f"{kind} embedded reference path differs in generated header"
        )
    if expected.encode() not in binary_bytes:
        raise VariantBuildError(
            f"{kind} embedded reference path differs in binary"
        )
    physical_root = Path(physical_root).resolve()
    recorded_root = Path(recorded_root).resolve()
    staged_reference = physical_root / "reference" / f"{kind}.u32"
    if (
        physical_root != recorded_root
        and str(staged_reference).encode() in binary_bytes
    ):
        raise VariantBuildError(
            f"{kind} binary retains a staging embedded reference path"
        )


def validate_variant_build(
    output, *, baseline_build, calibration, graph_scale, recorded_root=None,
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
        "graph_scale": graph_scale,
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
        "graph_scale": "graph scale",
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
    expected_derived = cira_lead_policy.effective_lead_for_scale(
        graph_scale,
        num_threads=4,
        calibrated_lead_blocks=policy.get("base_1us_lead_blocks", 0),
    )
    if policy.get("scale_derived") != expected_derived:
        raise VariantBuildError("scale-derived CIRA policy differs")
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
        _validate_embedded_reference(
            kind,
            row,
            binary,
            physical_root=output,
            recorded_root=recorded_root,
        )
        binary_hashes[kind] = actual_hash
    return {
        "manifest_sha256": sha256_file(manifest_path),
        "baseline_manifest_sha256": expected["baseline_manifest_sha256"],
        "calibration_sha256": calibration_hash,
        "cira_mode": manifest["cira_mode"],
        "cira_policy_latency_ns": manifest["cira_policy_latency_ns"],
        "graph_scale": graph_scale,
        "cira_policy": policy,
        "binary_sha256": binary_hashes,
    }


def ensure_variant_build(
    final, *, baseline_build, cxlmemuring, m5_library, calibration,
    graph_scale, log,
):
    final = Path(final).resolve()
    if final.exists():
        return validate_variant_build(
            final,
            baseline_build=baseline_build,
            calibration=calibration,
            graph_scale=graph_scale,
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
        graph_scale=graph_scale,
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
            graph_scale=graph_scale,
            recorded_root=final,
        )
        os.rename(staging, final)
        result = validate_variant_build(
            final,
            baseline_build=baseline_build,
            calibration=calibration,
            graph_scale=graph_scale,
        )
        result["command"] = [str(item) for item in command]
        return result
    finally:
        if staging.exists():
            shutil.rmtree(staging)
