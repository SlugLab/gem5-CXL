#!/usr/bin/env python3
# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

"""Build bit-exact AMU/CIRA variants of the fixed-20 PageRank baseline."""

import argparse
import hashlib
import json
import os
import shutil
import struct
import subprocess
from pathlib import Path

try:
    from scripts import build_gapbs_amu_cxlmemuring as amu_builder
    from scripts import build_gapbs_cira_cxlmemuring as cira_builder
    from scripts import build_gapbs_m2ndp_pr_spmv as baseline_builder
    from scripts import amu_cira_calibration as calibration
    from scripts import cira_lead_policy
    from scripts import m2ndp_artifacts as artifacts
except ImportError:
    import build_gapbs_amu_cxlmemuring as amu_builder
    import build_gapbs_cira_cxlmemuring as cira_builder
    import build_gapbs_m2ndp_pr_spmv as baseline_builder
    import amu_cira_calibration as calibration
    import cira_lead_policy
    import m2ndp_artifacts as artifacts


REPO = Path(__file__).resolve().parents[1]
FIXED_SOURCE = REPO / "util/m2ndp/gapbs_pr_spmv_fixed.cc"
AMU_LINE_CACHE_HEADER = REPO / "util/amu/gapbs_amu_line_cache.h"
COMMON_FLAGS = (
    "-std=c++11",
    "-O3",
    "-Wall",
    "-fopenmp",
    "-static",
    "-no-pie",
    "-ffp-contract=off",
    "-fno-fast-math",
)

_PULL_LOOP = (
    "      for (NodeID v : g.in_neigh(u))\n"
    "        incoming_total = incoming_total + outgoing_contrib[v];"
)
_AMU_PULL_LOOP = (
    "      auto neigh = g.in_neigh(u);\n"
    "      auto v_it = neigh.begin();\n"
    "      NodeID current_nodes[GAPBS_AMU_BATCH_SIZE];\n"
    "      NodeID next_nodes[GAPBS_AMU_BATCH_SIZE];\n"
    "      size_t current_node_slots[GAPBS_AMU_BATCH_SIZE];\n"
    "      size_t next_node_slots[GAPBS_AMU_BATCH_SIZE];\n"
    "      size_t score_slots[GAPBS_AMU_BATCH_SIZE];\n"
    "      size_t current_count = 0;\n"
    "      size_t next_count = 0;\n"
    "      gapbs_amu::LineBatch<gapbs_amu::Gem5LineBackend> initial_batch(\n"
    "          gapbs_amu::thread_store());\n"
    "      for (; v_it != neigh.end() &&\n"
    "             current_count < GAPBS_AMU_BATCH_SIZE; ++v_it)\n"
    "        current_node_slots[current_count++] = initial_batch.add(&*v_it);\n"
    "      initial_batch.issue_all();\n"
    "      initial_batch.wait_all();\n"
    "      for (size_t i = 0; i < current_count; ++i) {\n"
    "        current_nodes[i] =\n"
    "            initial_batch.value<NodeID>(current_node_slots[i]);\n"
    "        if (static_cast<uint64_t>(current_nodes[i]) >=\n"
    "            static_cast<uint64_t>(g.num_nodes())) {\n"
    "          std::fprintf(stderr,\n"
    "                       \"AMU_INVALID_NODE node=%lld num_nodes=%lld\\n\",\n"
    "                       static_cast<long long>(current_nodes[i]),\n"
    "                       static_cast<long long>(g.num_nodes()));\n"
    "          fflush(stderr);\n"
    "          m5_fail(0, 2);\n"
    "        }\n"
    "      }\n"
    "      while (current_count != 0) {\n"
    "        gapbs_amu::LineBatch<gapbs_amu::Gem5LineBackend> current_batch(\n"
    "            gapbs_amu::thread_store());\n"
    "        for (size_t i = 0; i < current_count; ++i)\n"
    "          score_slots[i] = current_batch.add(\n"
    "              &outgoing_contrib[current_nodes[i]]);\n"
    "        for (; v_it != neigh.end() &&\n"
    "               next_count < GAPBS_AMU_BATCH_SIZE; ++v_it)\n"
    "          next_node_slots[next_count++] = current_batch.add(&*v_it);\n"
    "        current_batch.issue_all();\n"
    "        current_batch.wait_all();\n"
    "        for (size_t i = 0; i < current_count; ++i)\n"
    "          incoming_total = incoming_total + current_batch.value<ScoreT>(score_slots[i]);\n"
    "        for (size_t i = 0; i < next_count; ++i) {\n"
    "          next_nodes[i] =\n"
    "              current_batch.value<NodeID>(next_node_slots[i]);\n"
    "          if (static_cast<uint64_t>(next_nodes[i]) >=\n"
    "              static_cast<uint64_t>(g.num_nodes())) {\n"
    "            std::fprintf(stderr,\n"
    "                         \"AMU_INVALID_NODE node=%lld num_nodes=%lld\\n\",\n"
    "                         static_cast<long long>(next_nodes[i]),\n"
    "                         static_cast<long long>(g.num_nodes()));\n"
    "            fflush(stderr);\n"
    "            m5_fail(0, 2);\n"
    "          }\n"
    "        }\n"
    "        current_batch.clear();\n"
    "        for (size_t i = 0; i < next_count; ++i)\n"
    "          current_nodes[i] = next_nodes[i];\n"
    "        current_count = next_count;\n"
    "        next_count = 0;\n"
    "      }"
)

_AMU_INIT_LOOP = (
    "#pragma omp parallel\n"
    "  {\n"
    "    gapbs_amu::thread_store().begin_trial();\n"
    "#pragma omp for\n"
    "    for (NodeID node = 0; node < g.num_nodes(); ++node) {\n"
    "      scores[node] = init_score;\n"
    "      next_scores[node] = 0.0f;\n"
    "      outgoing_contrib[node] = 0.0f;\n"
    "    }\n"
    "  }"
)

_AMU_ITERATION_BEGIN = (
    "  for (int iteration = 0; iteration < kPageRankIterations; ++iteration) {\n"
    "#pragma omp parallel\n"
    "    {\n"
    "      gapbs_amu::thread_store().reset_iteration();\n"
    "#pragma omp for\n"
    "      for (NodeID node = 0; node < g.num_nodes(); ++node)\n"
    "        outgoing_contrib[node] = scores[node] / g.out_degree(node);\n"
    "#pragma omp for schedule(static)"
)
_CIRA_PULL_LOOP = (
    "      NodeID pf_begin, pf_count;\n"
    "      if (GAPBS_CIRA_FUTURE_BLOCK(g, u, pf_begin, pf_count))\n"
    "        GAPBS_CIRA_PREFETCH_IN_CSR_INDEXED_ROWS("
    "g, pf_begin, pf_count, outgoing_contrib);\n"
    "      auto neigh = g.in_neigh(u);\n"
    "      for (auto v_it = neigh.begin(); v_it != neigh.end(); ++v_it) {\n"
    "        NodeID v = *v_it;\n"
    "        incoming_total = incoming_total + outgoing_contrib[v];\n"
    "      }"
)
class VariantEvidenceError(RuntimeError):
    pass


def resolve_cira_build_policy(calibration_manifest, mode, *, source_row=None):
    path = Path(calibration_manifest).resolve()
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise VariantEvidenceError(
            f"invalid calibration manifest: {path}"
        ) from error
    try:
        if value["schema"] != 1:
            raise VariantEvidenceError("calibration schema must be 1")
        if value["sources"]["amu_pdf"]["sha256"] != calibration.AMU_PDF_SHA256:
            raise VariantEvidenceError("calibration AMU PDF hash differs")
        if value["sources"]["cira_csv"]["sha256"] != calibration.CIRA_CSV_SHA256:
            raise VariantEvidenceError("calibration CIRA CSV hash differs")
        if value["amu"]["validation"]["status"] != "PASS":
            raise VariantEvidenceError("AMU calibration validation did not pass")
        policy = cira_lead_policy.resolve_mode(
            value, mode, source_row=source_row
        )
    except (KeyError, TypeError, cira_lead_policy.LeadPolicyError) as error:
        raise VariantEvidenceError(str(error)) from error
    return {
        **policy,
        "calibration_manifest": str(path),
        "calibration_manifest_sha256": calibration.sha256_file(path),
    }


def transform_source(source, kind):
    if kind not in {"amu", "cira"}:
        raise ValueError(f"unknown matched variant: {kind}")
    include = (
        '#include "pvector.h"\n#include "amu_gapbs.h"\n'
        if kind == "amu"
        else '#include "pvector.h"\n#include "cira_gapbs.h"\n'
    )
    if source.count('#include "pvector.h"\n') != 1:
        raise VariantEvidenceError(
            "fixed source must contain exactly one pvector include"
        )
    if source.count(_PULL_LOOP) != 1:
        raise VariantEvidenceError(
            "fixed source must contain exactly one ordered pull loop"
        )
    transformed = source.replace('#include "pvector.h"\n', include, 1)
    transformed = transformed.replace(
        _PULL_LOOP,
        _AMU_PULL_LOOP if kind == "amu" else _CIRA_PULL_LOOP,
        1,
    )
    if kind == "amu":
        init_loop = (
            "#pragma omp parallel for\n"
            "  for (NodeID node = 0; node < g.num_nodes(); ++node) {\n"
            "    scores[node] = init_score;\n"
            "    next_scores[node] = 0.0f;\n"
            "    outgoing_contrib[node] = 0.0f;\n"
            "  }"
        )
        iteration_begin = (
            "  for (int iteration = 0; iteration < kPageRankIterations; "
            "++iteration) {\n"
            "#pragma omp parallel for\n"
            "    for (NodeID node = 0; node < g.num_nodes(); ++node)\n"
            "      outgoing_contrib[node] = scores[node] / g.out_degree(node);\n"
            "#pragma omp parallel for schedule(static)"
        )
        iteration_end = (
            "      next_scores[u] = base_score + product;\n"
            "    }\n"
            "    scores.swap(next_scores);\n"
            "  }\n"
            "}"
        )
        if transformed.count(init_loop) != 1:
            raise VariantEvidenceError("fixed source init loop differs")
        if transformed.count(iteration_begin) != 1:
            raise VariantEvidenceError("fixed source iteration loop differs")
        if transformed.count(iteration_end) != 1:
            raise VariantEvidenceError("fixed source iteration end differs")
        transformed = transformed.replace(init_loop, _AMU_INIT_LOOP, 1)
        transformed = transformed.replace(
            iteration_begin, _AMU_ITERATION_BEGIN, 1
        )
        transformed = transformed.replace(
            iteration_end,
            "      next_scores[u] = base_score + product;\n"
            "      }\n"
            "    }\n"
            "    scores.swap(next_scores);\n"
            "  }\n"
            "}",
            1,
        )
        work_end = "    m5_work_end(trial, 0);"
        if transformed.count(work_end) != 1:
            raise VariantEvidenceError("fixed source work-end marker differs")
        transformed = transformed.replace(
            work_end,
            work_end + "\n    gapbs_amu::report_trial(trial);",
            1,
        )
    return transformed


def compile_command(
    *,
    kind,
    cxx,
    source,
    gapbs_root,
    generated_dir,
    output,
    m5_library,
    amu_batch_size,
    cira_prefetch_distance,
    cira_row_batch,
    cira_max_outstanding,
    cira_lead_rows=None,
    cira_batch_rows=None,
):
    command = [
        cxx,
        *COMMON_FLAGS,
    ]
    if kind == "amu":
        command += [
            "-DGAPBS_AMU_MAX_OUTSTANDING=256",
            f"-DGAPBS_AMU_BATCH_SIZE={amu_batch_size}",
            "-I",
            str(REPO / "util/amu"),
        ]
    elif kind == "cira":
        command += [
            f"-DGAPBS_CIRA_PREFETCH_DISTANCE={cira_prefetch_distance}",
            "-DGAPBS_CIRA_ROW_BLOCK_SIZE=64",
            f"-DGAPBS_CIRA_MAX_OUTSTANDING={cira_max_outstanding}",
            "-DGAPBS_CIRA_RANGE_LIMIT=0",
            "-DGAPBS_CIRA_USE_CSR=1",
            "-DGAPBS_CIRA_CSR_BLOCK_ROWS=0",
            "-I",
            str(REPO / "util/cira"),
        ]
        if cira_lead_rows is None:
            command.append(
                f"-DGAPBS_CIRA_LEAD_BLOCKS={cira_prefetch_distance}"
            )
        else:
            if cira_batch_rows is None:
                raise ValueError("scale-derived CIRA batch rows are missing")
            command += [
                f"-DGAPBS_CIRA_LEAD_ROWS={cira_lead_rows}",
                f"-DGAPBS_CIRA_BATCH_ROWS={cira_batch_rows}",
            ]
    else:
        raise ValueError(f"unknown matched variant: {kind}")
    command += [
        "-I",
        str(Path(gapbs_root) / "src"),
        "-I",
        str(REPO / "include"),
        "-I",
        str(generated_dir),
        str(source),
        str(m5_library),
        "-o",
        str(output),
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
        for index, (expected, actual) in enumerate(
            zip(baseline_words, actual_words)
        ):
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
    *,
    kind,
    baseline_build,
    outdir,
    reference_raw,
    embedded_reference_raw,
    cxx,
    m5_library,
    amu_batch_size,
    cira_prefetch_distance,
    cira_row_batch,
    cira_max_outstanding,
    cira_policy=None,
):
    variant_root = Path(outdir) / kind
    gapbs_root = variant_root / "src/gapbs"
    _copy_source(Path(baseline_build) / "src/gapbs", gapbs_root)
    generated_dir = variant_root / "generated"
    binary_dir = variant_root / "bin"
    generated_dir.mkdir(parents=True, exist_ok=True)
    binary_dir.mkdir(parents=True, exist_ok=True)
    baseline_builder.write_experiment_header(
        generated_dir / "m2ndp_experiment_config.h",
        embedded_reference_raw,
    )

    if kind == "amu":
        (gapbs_root / "src/amu_gapbs.h").write_text(
            amu_builder.AMU_HEADER, encoding="utf-8"
        )
    else:
        (gapbs_root / "src/cira_gapbs.h").write_text(
            cira_builder.CIRA_HEADER, encoding="utf-8"
        )
        cira_builder.patch_graph_for_cira(gapbs_root)

    source_text = transform_source(
        FIXED_SOURCE.read_text(encoding="utf-8"), kind
    )
    source = generated_dir / "pr_spmv.cc"
    source.write_text(source_text, encoding="utf-8")
    output = binary_dir / "pr_spmv"
    command = compile_command(
        kind=kind,
        cxx=cxx,
        source=source,
        gapbs_root=gapbs_root,
        generated_dir=generated_dir,
        output=output,
        m5_library=m5_library,
        amu_batch_size=amu_batch_size,
        cira_prefetch_distance=cira_prefetch_distance,
        cira_row_batch=cira_row_batch,
        cira_max_outstanding=cira_max_outstanding,
        cira_lead_rows=(
            cira_policy["scale_derived"]["effective_rows"]
            if cira_policy is not None else None
        ),
        cira_batch_rows=(
            cira_policy["scale_derived"]["batch_rows"]
            if cira_policy is not None else None
        ),
    )
    baseline_builder.run(command)
    evidence = {
        "kind": kind,
        "binary": str(output.resolve()),
        "binary_sha256": artifacts.sha256_file(output),
        "reference_raw": str(Path(reference_raw).resolve()),
        "generated_source": str(source.resolve()),
        "generated_source_sha256": _sha256_text(source_text),
        "fixed_source_sha256": artifacts.sha256_file(FIXED_SOURCE),
        "gapbs_source_sha256": baseline_builder.hash_source_tree(gapbs_root),
        "command": [str(item) for item in command],
    }
    if kind == "cira" and cira_policy is not None:
        evidence["cira_policy"] = cira_policy
    if kind == "amu":
        evidence["amu_line_cache_sha256"] = artifacts.sha256_file(
            AMU_LINE_CACHE_HEADER
        )
    return evidence


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline-build", type=Path, required=True)
    parser.add_argument("--outdir", type=Path, required=True)
    parser.add_argument("--recorded-outdir", type=Path)
    parser.add_argument(
        "--cxlmemuring",
        type=Path,
        default=baseline_builder.DEFAULT_CXLMEMURING,
    )
    parser.add_argument("--cxx", default=os.environ.get("CXX", "g++"))
    parser.add_argument(
        "--m5-library",
        type=Path,
        default=baseline_builder.DEFAULT_M5_LIBRARY,
    )
    parser.add_argument("--amu-batch-size", type=int, default=64)
    parser.add_argument("--cira-prefetch-distance", type=int, default=0)
    parser.add_argument("--cira-row-batch", type=int, default=256)
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
    if args.amu_batch_size <= 0:
        parser.error("--amu-batch-size must be positive")
    if args.cira_prefetch_distance < 0:
        parser.error("--cira-prefetch-distance must be non-negative")
    if args.cira_row_batch <= 0:
        parser.error("--cira-row-batch must be positive")
    if args.cira_max_outstanding <= 0:
        parser.error("--cira-max-outstanding must be positive")
    if args.cira_policy_latency_ns <= 0:
        parser.error("--cira-policy-latency-ns must be positive")

    cira_policy = None
    if args.cira_mode == "legacy":
        if args.calibration_manifest is not None or args.cira_source_row is not None:
            parser.error("legacy CIRA rejects calibration/source-row options")
        cira_distance = args.cira_prefetch_distance
    else:
        if args.calibration_manifest is None:
            parser.error("calibrated CIRA mode requires --calibration-manifest")
        if args.graph_scale not in (4, 12, 14, 20):
            parser.error(
                "calibrated CIRA mode requires --graph-scale 4, 12, 14, or 20"
            )
        if args.cira_mode != "few-shot-online" and args.cira_source_row is not None:
            parser.error("only few-shot-online accepts --cira-source-row")
        try:
            cira_policy = resolve_cira_build_policy(
                args.calibration_manifest,
                args.cira_mode,
                source_row=args.cira_source_row,
            )
        except VariantEvidenceError as error:
            parser.error(str(error))
        base_1us_distance = cira_policy["lead_blocks"]
        cira_distance = cira_lead_policy.lead_blocks_for_latency(
            base_1us_distance, args.cira_policy_latency_ns
        )
        if (
            args.cira_prefetch_distance != 0
            and args.cira_prefetch_distance != cira_distance
        ):
            parser.error("CIRA distance override differs from calibrated policy")
        cira_policy = {
            **cira_policy,
            "base_1us_lead_blocks": base_1us_distance,
            "effective_lead_blocks": cira_distance,
            "effective_latency_ns": args.cira_policy_latency_ns,
            "scale_derived": cira_lead_policy.effective_lead_for_scale(
                args.graph_scale,
                num_threads=4,
                calibrated_lead_blocks=cira_distance,
            ),
        }

    cira_distance, profiles, override = cira_builder.resolve_profile(
        args.cxlmemuring, "pr_spmv", cira_distance
    )
    reference_dir = args.outdir / "reference"
    reference_dir.mkdir(parents=True, exist_ok=True)
    recorded_reference_dir = (
        args.recorded_outdir if args.recorded_outdir is not None
        else args.outdir
    ) / "reference"
    variant_rows = []
    for kind in ("amu", "cira"):
        variant_rows.append(
            build_variant(
                kind=kind,
                baseline_build=args.baseline_build,
                outdir=args.outdir,
                reference_raw=reference_dir / f"{kind}.u32",
                embedded_reference_raw=(
                    recorded_reference_dir / f"{kind}.u32"
                ),
                cxx=args.cxx,
                m5_library=args.m5_library,
                amu_batch_size=args.amu_batch_size,
                cira_prefetch_distance=cira_distance,
                cira_row_batch=args.cira_row_batch,
                cira_max_outstanding=args.cira_max_outstanding,
                cira_policy=cira_policy,
            )
        )

    manifest = {
        "schema": 1,
        "benchmark": "pr_spmv",
        "graph_scale": args.graph_scale,
        "page_rank_iterations": 20,
        "fixed_iterations": True,
        "fp_contract": False,
        "fast_math": False,
        "baseline_build": str(args.baseline_build.resolve()),
        "baseline_manifest_sha256": artifacts.sha256_file(
            args.baseline_build / "manifest.json"
        ),
        "fixed_source_sha256": artifacts.sha256_file(FIXED_SOURCE),
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
        "variants": variant_rows,
    }
    if args.recorded_outdir is not None:
        manifest = rebase_output_paths(
            manifest, args.outdir, args.recorded_outdir
        )
    artifacts.atomic_write_json(args.outdir / "manifest.json", manifest)
    print(f"Wrote {args.outdir / 'manifest.json'}")


if __name__ == "__main__":
    main()
