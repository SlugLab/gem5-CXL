#!/usr/bin/env python3
# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

"""Build the matched fixed-20 GAPBS PageRank reference binary."""

import argparse
import json
import os
import shlex
import shutil
import subprocess
from pathlib import Path

try:
    from scripts import m2ndp_artifacts as artifacts
except ImportError:
    import m2ndp_artifacts as artifacts


REPO = Path(__file__).resolve().parents[1]
DEFAULT_CXLMEMURING = (REPO / ".." / "CXLMemUring").resolve()
DEFAULT_M5_LIBRARY = (
    REPO / "util/m5/build/x86/out/libm5.a"
)
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


def run(command, *, cwd=None):
    print("+", shlex.join(str(item) for item in command), flush=True)
    subprocess.run(command, cwd=cwd, check=True)


def page_rank_compile_command(
    *, cxx, gapbs_root, generated_dir, output, m5_library
):
    return [
        cxx,
        *COMMON_FLAGS,
        "-I",
        str(Path(gapbs_root) / "src"),
        "-I",
        str(REPO / "include"),
        "-I",
        str(generated_dir),
        str(REPO / "util/m2ndp/gapbs_pr_spmv_fixed.cc"),
        str(m5_library),
        "-o",
        str(output),
    ]


def build_manifest(*, reference_raw, compiler, flags):
    return {
        "schema": 1,
        "page_rank_iterations": 20,
        "fixed_iterations": True,
        "convergence_reduction": False,
        "fp_contract": False,
        "reference_raw_path": str(Path(reference_raw).resolve()),
        "compiler": compiler,
        "flags": [str(flag) for flag in flags],
    }


def hash_source_tree(root):
    return {
        str(path.relative_to(root)): artifacts.sha256_file(path)
        for path in sorted(Path(root).rglob("*"))
        if path.is_file() and path.suffix in {".cc", ".h"}
    }


def compiler_version(cxx):
    return subprocess.check_output(
        [cxx, "--version"], text=True
    ).splitlines()[0]


def write_experiment_header(path, reference_raw):
    reference = str(Path(reference_raw).resolve())
    escaped = (
        reference.replace("\\", "\\\\").replace('"', '\\"')
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "#ifndef M2NDP_EXPERIMENT_CONFIG_H\n"
        "#define M2NDP_EXPERIMENT_CONFIG_H\n"
        f'#define M2NDP_REFERENCE_RAW_PATH "{escaped}"\n'
        "#endif\n"
    )


def copy_gapbs_source(cxlmemuring, outdir):
    source = Path(cxlmemuring) / "bench/gapbs"
    if not (source / "src/pr_spmv.cc").is_file():
        raise SystemExit(f"GAPBS source missing under {source}")
    destination = Path(outdir) / "src/gapbs"
    if destination.exists():
        shutil.rmtree(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(
        source,
        destination,
        ignore=shutil.ignore_patterns(".git"),
    )
    return destination


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--cxlmemuring", type=Path, default=DEFAULT_CXLMEMURING
    )
    parser.add_argument("--outdir", type=Path, required=True)
    parser.add_argument("--reference-raw", type=Path, required=True)
    parser.add_argument("--cxx", default=os.environ.get("CXX", "g++"))
    parser.add_argument(
        "--m5-library", type=Path, default=DEFAULT_M5_LIBRARY
    )
    args = parser.parse_args(argv)

    if not args.m5_library.is_file():
        raise SystemExit(f"gem5 m5 library missing: {args.m5_library}")
    args.outdir.mkdir(parents=True, exist_ok=True)
    args.reference_raw.parent.mkdir(parents=True, exist_ok=True)
    binary_dir = args.outdir / "bin"
    generated_dir = args.outdir / "generated"
    binary_dir.mkdir(parents=True, exist_ok=True)
    gapbs_root = copy_gapbs_source(args.cxlmemuring, args.outdir)
    header = generated_dir / "m2ndp_experiment_config.h"
    write_experiment_header(header, args.reference_raw)

    pr_output = binary_dir / "pr_spmv"
    pr_command = page_rank_compile_command(
        cxx=args.cxx,
        gapbs_root=gapbs_root,
        generated_dir=generated_dir,
        output=pr_output,
        m5_library=args.m5_library,
    )
    run(pr_command)

    exporter_output = binary_dir / "export_gapbs_graph"
    exporter_command = [
        args.cxx,
        "-std=c++11",
        "-O2",
        "-Wall",
        "-fopenmp",
        "-I",
        str(gapbs_root / "src"),
        str(REPO / "util/m2ndp/export_gapbs_graph.cc"),
        "-o",
        str(exporter_output),
    ]
    run(exporter_command)
    run(
        ["make", "-C", str(gapbs_root), "converter", f"CXX={args.cxx}"]
    )
    converter_output = binary_dir / "converter"
    shutil.copy2(gapbs_root / "converter", converter_output)

    manifest = build_manifest(
        reference_raw=args.reference_raw,
        compiler=compiler_version(args.cxx),
        flags=pr_command[1:],
    )
    manifest.update(
        {
            "cxlmemuring": str(args.cxlmemuring.resolve()),
            "source_copy": str(gapbs_root.resolve()),
            "compiler_input_sha256": hash_source_tree(gapbs_root),
            "generated_header_sha256": artifacts.sha256_file(header),
            "matched_source_sha256": artifacts.sha256_file(
                REPO / "util/m2ndp/gapbs_pr_spmv_fixed.cc"
            ),
            "exporter_source_sha256": artifacts.sha256_file(
                REPO / "util/m2ndp/export_gapbs_graph.cc"
            ),
            "builder_script_sha256": artifacts.sha256_file(Path(__file__)),
            "m5_library_sha256": artifacts.sha256_file(args.m5_library),
            "binary_sha256": {
                "pr_spmv": artifacts.sha256_file(pr_output),
                "export_gapbs_graph": artifacts.sha256_file(exporter_output),
                "converter": artifacts.sha256_file(converter_output),
            },
            "commands": {
                "pr_spmv": [str(item) for item in pr_command],
                "export_gapbs_graph": [
                    str(item) for item in exporter_command
                ],
            },
        }
    )
    artifacts.atomic_write_json(args.outdir / "manifest.json", manifest)
    print(f"Wrote {args.outdir / 'manifest.json'}")


if __name__ == "__main__":
    main()
