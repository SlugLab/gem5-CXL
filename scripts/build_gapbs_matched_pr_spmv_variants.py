#!/usr/bin/env python3
# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

"""Build matched, bit-exact AMU/CIRA PageRank row-offload binaries."""

import argparse
import hashlib
import json
import os
import shutil
import struct
from pathlib import Path

try:
    from scripts import amu_cira_calibration as calibration
    from scripts import build_gapbs_amu_cxlmemuring as amu_builder
    from scripts import build_gapbs_cira_cxlmemuring as cira_builder
    from scripts import build_gapbs_m2ndp_pr_spmv as baseline_builder
    from scripts import cira_lead_policy
    from scripts import m2ndp_artifacts as artifacts
except ImportError:
    import amu_cira_calibration as calibration
    import build_gapbs_amu_cxlmemuring as amu_builder
    import build_gapbs_cira_cxlmemuring as cira_builder
    import build_gapbs_m2ndp_pr_spmv as baseline_builder
    import cira_lead_policy
    import m2ndp_artifacts as artifacts


REPO = Path(__file__).resolve().parents[1]
FIXED_SOURCE = REPO / "util/m2ndp/gapbs_pr_spmv_fixed.cc"
OFFLOAD_SOURCE = amu_builder.PR_ROW_OFFLOAD_SOURCE
if OFFLOAD_SOURCE != cira_builder.PR_ROW_OFFLOAD_SOURCE:
    raise RuntimeError("AMU and CIRA row-offload source paths differ")
DESCRIPTOR_HEADER = REPO / "util/pr_offload/pr_row_offload.h"
CANDIDATES = {
    "A": {"row_window": 64, "lead_blocks": 1},
    "B": {"row_window": 2048, "lead_blocks": 32},
    "C": {"row_window": 1024, "lead_blocks": 16},
}
COMMON_FLAGS = (
    "-std=c++11", "-O3", "-Wall", "-fopenmp", "-static", "-no-pie",
    "-ffp-contract=off", "-fno-fast-math",
)


class VariantEvidenceError(RuntimeError):
    pass


def resolve_cira_build_policy(calibration_manifest, mode, *, source_row=None):
    path = Path(calibration_manifest).resolve()
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise VariantEvidenceError(f"invalid calibration manifest: {path}") from error
    try:
        if value["schema"] != 2:
            raise VariantEvidenceError("calibration schema must be 2")
        if value["sources"]["amu_pdf"]["sha256"] != calibration.AMU_PDF_SHA256:
            raise VariantEvidenceError("calibration AMU PDF hash differs")
        if value["sources"]["cira_csv"]["sha256"] != calibration.CIRA_CSV_SHA256:
            raise VariantEvidenceError("calibration CIRA CSV hash differs")
        if value["amu"]["validation"]["status"] != "PASS":
            raise VariantEvidenceError("AMU calibration validation did not pass")
        if value["near_data_pr"]["formal_speedup_is_fit_target"] is not False:
            raise VariantEvidenceError("formal speedup cannot be a calibration target")
        policy = cira_lead_policy.resolve_mode(value, mode, source_row=source_row)
    except (KeyError, TypeError, cira_lead_policy.LeadPolicyError) as error:
        raise VariantEvidenceError(str(error)) from error
    return {
        **policy,
        "calibration_manifest": str(path),
        "calibration_manifest_sha256": calibration.sha256_file(path),
    }


def policy_compile_definitions(mode, source_row):
    """Return one and only one CIRA runtime-policy definition set."""
    if mode in {"legacy", "static"}:
        if source_row not in {None, "A"}:
            raise VariantEvidenceError("static CIRA is fixed to source row A")
        return ["-DPR_CIRA_POLICY_STATIC=1", "-DPR_CIRA_SOURCE_ROW=0"]
    if mode == "pgo-selected":
        if source_row not in CANDIDATES:
            raise VariantEvidenceError("PGO CIRA requires a selected source row")
        return [
            "-DPR_CIRA_POLICY_PGO=1",
            f"-DPR_CIRA_SOURCE_ROW={tuple(CANDIDATES).index(source_row)}",
        ]
    if mode == "few-shot-online":
        return ["-DPR_CIRA_POLICY_FEWSHOT=1"]
    raise VariantEvidenceError(f"unknown CIRA mode {mode}")


def compile_command(
    *, kind, cxx, source, gapbs_root, generated_dir, output, m5_library,
    cira_mode="legacy", cira_source_row=None, **_legacy_options,
):
    command = [cxx, *COMMON_FLAGS]
    if kind == "amu":
        command += ["-DPR_OFFLOAD_AMU=1", "-I", str(REPO / "util/amu")]
    elif kind == "cira":
        command += [
            "-DPR_OFFLOAD_CIRA=1",
            *policy_compile_definitions(cira_mode, cira_source_row),
            "-I", str(REPO / "util/cira"),
        ]
    else:
        raise ValueError(f"unknown matched variant: {kind}")
    command += [
        "-I", str(REPO / "util/pr_offload"),
        "-I", str(Path(gapbs_root) / "src"),
        "-I", str(REPO / "include"),
        "-I", str(generated_dir),
        str(source), str(m5_library), "-o", str(output),
    ]
    return command


def _read_words(path, expected_words):
    payload = Path(path).read_bytes()
    expected_bytes = expected_words * 4
    if len(payload) != expected_bytes:
        raise VariantEvidenceError(
            f"{path}: expected {expected_bytes} bytes, got {len(payload)}"
        )
    return tuple(word[0] for word in struct.iter_unpack("<I", payload))


def validate_raw_outputs(reference, variants, expected_words):
    baseline_words = _read_words(reference, expected_words)
    evidence = {
        "compared_words": expected_words,
        "mismatches": {},
        "sha256": {"baseline": artifacts.sha256_file(reference)},
    }
    for name, path in sorted(variants.items()):
        actual_words = _read_words(path, expected_words)
        for index, (expected, actual) in enumerate(zip(baseline_words, actual_words)):
            if expected != actual:
                raise VariantEvidenceError(
                    f"{name} word {index}: expected 0x{expected:08x}, "
                    f"actual 0x{actual:08x}"
                )
        evidence["mismatches"][name] = 0
        evidence["sha256"][name] = artifacts.sha256_file(path)
    return evidence


def _copy_source(source, destination):
    if destination.exists():
        shutil.rmtree(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, destination)


def _sha256_text(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def rebase_output_paths(manifest, physical_root, recorded_root):
    physical_root = Path(physical_root).resolve()
    recorded_root = Path(recorded_root).resolve()
    result = json.loads(json.dumps(manifest))
    for row in result.get("variants", []):
        for field in ("binary", "reference_raw", "generated_source"):
            path = Path(row[field]).resolve()
            try:
                relative = path.relative_to(physical_root)
            except ValueError as error:
                raise VariantEvidenceError(
                    f"{field} is outside physical output root: {path}"
                ) from error
            row[field] = str(recorded_root / relative)
    return result


def build_variant(
    *, kind, baseline_build, outdir, reference_raw, embedded_reference_raw,
    cxx, m5_library, cira_policy=None, cira_mode="legacy", **legacy_options,
):
    variant_root = Path(outdir) / kind
    gapbs_root = variant_root / "src/gapbs"
    _copy_source(Path(baseline_build) / "src/gapbs", gapbs_root)
    generated_dir = variant_root / "generated"
    binary_dir = variant_root / "bin"
    generated_dir.mkdir(parents=True, exist_ok=True)
    binary_dir.mkdir(parents=True, exist_ok=True)
    baseline_builder.write_experiment_header(
        generated_dir / "m2ndp_experiment_config.h", embedded_reference_raw,
    )

    source_text = OFFLOAD_SOURCE.read_text(encoding="utf-8")
    source = generated_dir / "pr_spmv_offload.cc"
    source.write_text(source_text, encoding="utf-8")
    output = binary_dir / "pr_spmv"
    selected_row = cira_policy.get("source_row") if cira_policy else None
    command = compile_command(
        kind=kind, cxx=cxx, source=source, gapbs_root=gapbs_root,
        generated_dir=generated_dir, output=output, m5_library=m5_library,
        cira_mode=cira_mode, cira_source_row=selected_row, **legacy_options,
    )
    baseline_builder.run(command)
    evidence = {
        "kind": kind,
        "binary": str(output.resolve()),
        "binary_sha256": artifacts.sha256_file(output),
        "reference_raw": str(Path(reference_raw).resolve()),
        "generated_source": str(source.resolve()),
        "generated_source_sha256": _sha256_text(source_text),
        "offload_source_sha256": artifacts.sha256_file(OFFLOAD_SOURCE),
        "descriptor_header_sha256": artifacts.sha256_file(DESCRIPTOR_HEADER),
        "gapbs_source_sha256": baseline_builder.hash_source_tree(gapbs_root),
        "command": [str(item) for item in command],
        "threads": 4,
        "double_buffered": True,
        "page_rank_iterations": 20,
    }
    if kind == "cira":
        evidence["cira_policy"] = cira_policy
        evidence["cira_runtime_policy"] = cira_mode
    return evidence


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline-build", type=Path, required=True)
    parser.add_argument("--outdir", type=Path, required=True)
    parser.add_argument("--recorded-outdir", type=Path)
    parser.add_argument("--cxlmemuring", type=Path,
                        default=baseline_builder.DEFAULT_CXLMEMURING)
    parser.add_argument("--cxx", default=os.environ.get("CXX", "g++"))
    parser.add_argument("--m5-library", type=Path,
                        default=baseline_builder.DEFAULT_M5_LIBRARY)
    # Retained for CLI compatibility; row descriptors replace these knobs.
    parser.add_argument("--amu-batch-size", type=int, default=64)
    parser.add_argument("--cira-prefetch-distance", type=int, default=0)
    parser.add_argument("--cira-row-batch", type=int, default=64)
    parser.add_argument("--cira-max-outstanding", type=int, default=256)
    parser.add_argument(
        "--cira-mode",
        choices=("legacy", "static", "pgo-selected", "few-shot-online"),
        default="legacy",
    )
    parser.add_argument("--calibration-manifest", type=Path)
    parser.add_argument("--cira-source-row", choices=("A", "B", "C"))
    parser.add_argument("--cira-policy-latency-ns", type=int, default=1000)
    parser.add_argument("--graph-scale", type=int)
    args = parser.parse_args(argv)

    source_root = args.baseline_build / "src/gapbs"
    if not (source_root / "src/graph.h").is_file():
        parser.error(f"matched baseline GAPBS source missing: {source_root}")
    if not args.m5_library.is_file():
        parser.error(f"gem5 m5 library missing: {args.m5_library}")
    if args.amu_batch_size <= 0 or args.cira_row_batch <= 0:
        parser.error("batch sizes must be positive")
    if args.cira_prefetch_distance < 0 or args.cira_max_outstanding <= 0:
        parser.error("CIRA queue settings are invalid")
    if args.cira_policy_latency_ns <= 0:
        parser.error("--cira-policy-latency-ns must be positive")

    cira_policy = None
    cira_distance = args.cira_prefetch_distance
    if args.cira_mode == "legacy":
        if args.calibration_manifest is not None or args.cira_source_row is not None:
            parser.error("legacy CIRA rejects calibration/source-row options")
    else:
        if args.calibration_manifest is None:
            parser.error("calibrated CIRA mode requires --calibration-manifest")
        if args.graph_scale not in (4, 12, 14, 20):
            parser.error(
                "calibrated CIRA mode requires --graph-scale 4, 12, 14, or 20"
            )
        if args.cira_mode != "few-shot-online" and args.cira_source_row is not None:
            parser.error("only few-shot-online accepts --cira-source-row")
        policy_source = args.cira_source_row
        if args.cira_mode == "few-shot-online" and policy_source is None:
            policy_source = "A"
        try:
            cira_policy = resolve_cira_build_policy(
                args.calibration_manifest, args.cira_mode,
                source_row=policy_source,
            )
        except VariantEvidenceError as error:
            parser.error(str(error))
        base_1us_distance = cira_policy["lead_blocks"]
        cira_distance = cira_lead_policy.lead_blocks_for_latency(
            base_1us_distance, args.cira_policy_latency_ns
        )
        if (args.cira_prefetch_distance != 0 and
                args.cira_prefetch_distance != cira_distance):
            parser.error("CIRA distance override differs from calibrated policy")
        cira_policy = {
            **cira_policy,
            "base_1us_lead_blocks": base_1us_distance,
            "effective_lead_blocks": cira_distance,
            "effective_latency_ns": args.cira_policy_latency_ns,
            "scale_derived": cira_lead_policy.effective_lead_for_scale(
                args.graph_scale, num_threads=4,
                calibrated_lead_blocks=cira_distance,
            ),
            "runtime_candidates": CANDIDATES,
            "runtime_selection": args.cira_mode == "few-shot-online",
        }

    _, profiles, override = cira_builder.resolve_profile(
        args.cxlmemuring, "pr_spmv", cira_distance
    )
    reference_dir = args.outdir / "reference"
    reference_dir.mkdir(parents=True, exist_ok=True)
    recorded_reference_dir = (
        args.recorded_outdir if args.recorded_outdir is not None else args.outdir
    ) / "reference"
    variant_rows = []
    for kind in ("amu", "cira"):
        variant_rows.append(build_variant(
            kind=kind, baseline_build=args.baseline_build, outdir=args.outdir,
            reference_raw=reference_dir / f"{kind}.u32",
            embedded_reference_raw=recorded_reference_dir / f"{kind}.u32",
            cxx=args.cxx, m5_library=args.m5_library,
            amu_batch_size=args.amu_batch_size,
            cira_prefetch_distance=cira_distance,
            cira_row_batch=args.cira_row_batch,
            cira_max_outstanding=args.cira_max_outstanding,
            cira_policy=cira_policy, cira_mode=args.cira_mode,
        ))

    manifest = {
        "schema": 1,
        "benchmark": "pr_spmv",
        "graph_scale": args.graph_scale,
        "page_rank_iterations": 20,
        "fixed_iterations": True,
        "threads": 4,
        "double_buffered": True,
        "fp_contract": False,
        "fast_math": False,
        "baseline_build": str(args.baseline_build.resolve()),
        "baseline_manifest_sha256": artifacts.sha256_file(
            args.baseline_build / "manifest.json"
        ),
        "fixed_source_sha256": artifacts.sha256_file(FIXED_SOURCE),
        "offload_source_sha256": artifacts.sha256_file(OFFLOAD_SOURCE),
        "descriptor_header_sha256": artifacts.sha256_file(DESCRIPTOR_HEADER),
        "compiler": baseline_builder.compiler_version(args.cxx),
        "amu_batch_size": args.amu_batch_size,
        "cira_prefetch_distance": cira_distance,
        "cira_lead_blocks": cira_distance,
        "cira_row_batch": 64,
        "cira_profile_mode": "override-non-pgo" if override else "pgo",
        "cira_profiles": profiles,
        "cira_profile_sha256": {
            profile: artifacts.sha256_file(profile) for profile in profiles
        },
        "cira_max_outstanding": args.cira_max_outstanding,
        "cira_mode": args.cira_mode,
        "cira_policy_latency_ns": args.cira_policy_latency_ns,
        "cira_policy": cira_policy,
        "cira_candidates": CANDIDATES,
        "variants": variant_rows,
    }
    if args.recorded_outdir is not None:
        manifest = rebase_output_paths(manifest, args.outdir, args.recorded_outdir)
    artifacts.atomic_write_json(args.outdir / "manifest.json", manifest)
    print(f"Wrote {args.outdir / 'manifest.json'}")


if __name__ == "__main__":
    main()
