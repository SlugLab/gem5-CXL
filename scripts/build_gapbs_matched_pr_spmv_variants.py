#!/usr/bin/env python3
# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

"""Build bit-exact AMU/CIRA variants of the fixed-20 PageRank baseline."""

import argparse
import hashlib
import os
import shutil
import struct
import subprocess
from pathlib import Path

try:
    from scripts import build_gapbs_amu_cxlmemuring as amu_builder
    from scripts import build_gapbs_cira_cxlmemuring as cira_builder
    from scripts import build_gapbs_m2ndp_pr_spmv as baseline_builder
    from scripts import m2ndp_artifacts as artifacts
except ImportError:
    import build_gapbs_amu_cxlmemuring as amu_builder
    import build_gapbs_cira_cxlmemuring as cira_builder
    import build_gapbs_m2ndp_pr_spmv as baseline_builder
    import m2ndp_artifacts as artifacts


REPO = Path(__file__).resolve().parents[1]
FIXED_SOURCE = REPO / "util/m2ndp/gapbs_pr_spmv_fixed.cc"
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
    "      for (auto v_it = neigh.begin(); v_it != neigh.end();) {\n"
    "        const NodeID *node_addrs[GAPBS_AMU_BATCH_SIZE];\n"
    "        NodeID nodes[GAPBS_AMU_BATCH_SIZE];\n"
    "        const ScoreT *score_addrs[GAPBS_AMU_BATCH_SIZE];\n"
    "        ScoreT scores_batch[GAPBS_AMU_BATCH_SIZE];\n"
    "        size_t amu_count = 0;\n"
    "        for (; v_it != neigh.end() && amu_count < GAPBS_AMU_BATCH_SIZE;\n"
    "             ++v_it)\n"
    "          node_addrs[amu_count++] = &*v_it;\n"
    "        gapbs_amu::load_values(node_addrs, nodes, amu_count);\n"
    "        for (size_t amu_i = 0; amu_i < amu_count; ++amu_i) {\n"
    "          if (static_cast<uint64_t>(nodes[amu_i]) >=\n"
    "              static_cast<uint64_t>(g.num_nodes())) {\n"
    "            std::fprintf(stderr,\n"
    "                         \"AMU_INVALID_NODE node=%lld num_nodes=%lld\\n\",\n"
    "                         static_cast<long long>(nodes[amu_i]),\n"
    "                         static_cast<long long>(g.num_nodes()));\n"
    "            score_addrs[amu_i] = &outgoing_contrib[0];\n"
    "            m5_fail(0, 2);\n"
    "            continue;\n"
    "          }\n"
    "          score_addrs[amu_i] = &outgoing_contrib[nodes[amu_i]];\n"
    "        }\n"
    "        gapbs_amu::load_values(score_addrs, scores_batch, amu_count);\n"
    "        for (size_t amu_i = 0; amu_i < amu_count; ++amu_i)\n"
    "          incoming_total = incoming_total + scores_batch[amu_i];\n"
    "      }"
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
            f"-DGAPBS_CIRA_NODE_DISTANCE={cira_prefetch_distance}",
            f"-DGAPBS_CIRA_ROW_BATCH={cira_row_batch}",
            f"-DGAPBS_CIRA_MAX_OUTSTANDING={cira_max_outstanding}",
            "-DGAPBS_CIRA_RANGE_LIMIT=0",
            "-DGAPBS_CIRA_USE_CSR=1",
            "-DGAPBS_CIRA_CSR_BLOCK_ROWS=0",
            "-I",
            str(REPO / "util/cira"),
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


def build_variant(
    *,
    kind,
    baseline_build,
    outdir,
    reference_raw,
    cxx,
    m5_library,
    amu_batch_size,
    cira_prefetch_distance,
    cira_row_batch,
    cira_max_outstanding,
):
    variant_root = Path(outdir) / kind
    gapbs_root = variant_root / "src/gapbs"
    _copy_source(Path(baseline_build) / "src/gapbs", gapbs_root)
    generated_dir = variant_root / "generated"
    binary_dir = variant_root / "bin"
    generated_dir.mkdir(parents=True, exist_ok=True)
    binary_dir.mkdir(parents=True, exist_ok=True)
    baseline_builder.write_experiment_header(
        generated_dir / "m2ndp_experiment_config.h", reference_raw
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
    )
    baseline_builder.run(command)
    return {
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


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline-build", type=Path, required=True)
    parser.add_argument("--outdir", type=Path, required=True)
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

    cira_distance, profiles, override = cira_builder.resolve_profile(
        args.cxlmemuring, "pr_spmv", args.cira_prefetch_distance
    )
    reference_dir = args.outdir / "reference"
    reference_dir.mkdir(parents=True, exist_ok=True)
    variant_rows = []
    for kind in ("amu", "cira"):
        variant_rows.append(
            build_variant(
                kind=kind,
                baseline_build=args.baseline_build,
                outdir=args.outdir,
                reference_raw=reference_dir / f"{kind}.u32",
                cxx=args.cxx,
                m5_library=args.m5_library,
                amu_batch_size=args.amu_batch_size,
                cira_prefetch_distance=cira_distance,
                cira_row_batch=args.cira_row_batch,
                cira_max_outstanding=args.cira_max_outstanding,
            )
        )

    manifest = {
        "schema": 1,
        "benchmark": "pr_spmv",
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
        "cira_row_batch": args.cira_row_batch,
        "cira_profile_mode": "override-non-pgo" if override else "pgo",
        "cira_profiles": profiles,
        "cira_profile_sha256": {
            profile: artifacts.sha256_file(profile) for profile in profiles
        },
        "cira_max_outstanding": args.cira_max_outstanding,
        "variants": variant_rows,
    }
    artifacts.atomic_write_json(args.outdir / "manifest.json", manifest)
    print(f"Wrote {args.outdir / 'manifest.json'}")


if __name__ == "__main__":
    main()
