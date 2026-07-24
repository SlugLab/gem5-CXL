#!/usr/bin/env python3
#
# Build plain GAPBS binaries from the CXLMemUring checkout for local gem5 SE
# CXL baseline runs.

import argparse
import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
DEFAULT_CXLMEMURING = (REPO / ".." / "CXLMemUring").resolve()
DEFAULT_OUTDIR = REPO / "m5out" / "gapbs_baseline_bins"
M5_LIB = REPO / "util" / "m5" / "build" / "x86" / "out" / "libm5.a"
GAPBS_KERNELS = ["bc", "bfs", "cc", "cc_sv", "pr", "pr_spmv", "sssp", "tc"]


def run(cmd):
    print("+", " ".join(str(c) for c in cmd))
    subprocess.run(cmd, check=True)


def read_text(path):
    return Path(path).read_text(encoding="utf-8")


def write_text(path, data):
    Path(path).write_text(data, encoding="utf-8")


def replace_once(data, old, new, path):
    if old not in data:
        raise RuntimeError(f"Expected snippet not found in {path}: {old[:80]!r}")
    return data.replace(old, new, 1)


def copy_gapbs_source(cxlmemuring, outdir):
    src = cxlmemuring / "bench" / "gapbs"
    if not (src / "src" / "bfs.cc").exists():
        raise SystemExit(f"GAPBS source missing under {src}")

    dst = outdir / "src" / "gapbs"
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns(".git"))
    return dst


def patch_benchmark_roi_markers(src_dir):
    path = src_dir / "src" / "benchmark.h"
    data = read_text(path)
    data = replace_once(
        data,
        '#include "writer.h"\n',
        '#include "writer.h"\n\n#include <gem5/m5ops.h>\n',
        path,
    )
    data = replace_once(
        data,
        "    trial_timer.Start();\n"
        "    auto result = kernel(g);\n"
        "    trial_timer.Stop();\n",
        "    m5_work_begin(iter, 0);\n"
        "    trial_timer.Start();\n"
        "    auto result = kernel(g);\n"
        "    trial_timer.Stop();\n"
        "    m5_work_end(iter, 0);\n",
        path,
    )
    data = replace_once(
        data,
        '      PrintLabel("Verification",\n'
        '                 verify(std::ref(g), std::ref(result)) ? "PASS" : "FAIL");\n',
        "      bool verification_passed =\n"
        "          verify(std::ref(g), std::ref(result));\n"
        '      PrintLabel("Verification",\n'
        '                 verification_passed ? "PASS" : "FAIL");\n'
        "      if (!verification_passed)\n"
        "        m5_fail(0, 1);\n",
        path,
    )
    data = replace_once(
        data,
        '  PrintTime("Average Time", total_seconds / cli.num_trials());\n',
        "  if (cli.do_verify())\n"
        "    m5_exit(0);\n"
        '  PrintTime("Average Time", total_seconds / cli.num_trials());\n',
        path,
    )
    write_text(path, data)


def build_benchmark(src_dir, out_bin_dir, benchmark, cxx, extra_cxxflags):
    src = src_dir / "src" / f"{benchmark}.cc"
    out = out_bin_dir / benchmark
    cmd = [
        cxx,
        "-std=c++11",
        "-O3",
        "-Wall",
        "-fopenmp",
        "-static",
        "-no-pie",
        "-I",
        str(src_dir / "src"),
        "-I",
        str(REPO / "include"),
        *extra_cxxflags,
        str(src),
        str(M5_LIB),
        "-o",
        str(out),
    ]
    run(cmd)
    return out


def cxl_commit(cxlmemuring):
    try:
        return subprocess.check_output(
            ["git", "-C", str(cxlmemuring), "rev-parse", "HEAD"],
            text=True,
        ).strip()
    except Exception:
        return "unknown"


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_tree(root, suffixes):
    root = Path(root)
    return {
        path.relative_to(root).as_posix(): sha256_file(path)
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.suffix in suffixes
    }


def git_repository_state(repo):
    try:
        commit = subprocess.check_output(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            text=True,
        ).strip()
        status = subprocess.check_output(
            ["git", "-C", str(repo), "status", "--porcelain"],
            text=True,
        )
        return {"commit": commit, "dirty": bool(status)}
    except (OSError, subprocess.CalledProcessError):
        return {"commit": "unknown", "dirty": None}


def main():
    parser = argparse.ArgumentParser(
        description="Build plain GAPBS binaries for gem5 CXL baseline runs."
    )
    parser.add_argument("--cxlmemuring", type=Path, default=DEFAULT_CXLMEMURING)
    parser.add_argument("--outdir", type=Path, default=DEFAULT_OUTDIR)
    parser.add_argument("--benchmarks", default="bfs,bc,pr,sssp")
    parser.add_argument("--cxx", default=os.environ.get("CXX", "g++"))
    parser.add_argument(
        "--extra-cxxflags",
        default=os.environ.get("GAPBS_BASELINE_EXTRA_CXXFLAGS", ""),
    )
    parser.add_argument(
        "--roi-work-markers",
        action="store_true",
        help="Add m5_work_begin/end around each GAPBS kernel trial.",
    )
    args = parser.parse_args()

    if not M5_LIB.exists():
        raise SystemExit(f"gem5 m5 library not found: {M5_LIB}")

    benchmarks = GAPBS_KERNELS if args.benchmarks == "all" else [
        b.strip() for b in args.benchmarks.split(",") if b.strip()
    ]
    unknown = sorted(set(benchmarks) - set(GAPBS_KERNELS))
    if unknown:
        raise SystemExit(f"Unknown GAPBS benchmark(s): {', '.join(unknown)}")

    args.outdir.mkdir(parents=True, exist_ok=True)
    src_dir = copy_gapbs_source(args.cxlmemuring, args.outdir)
    if args.roi_work_markers:
        patch_benchmark_roi_markers(src_dir)

    out_bin_dir = args.outdir / "bin"
    out_bin_dir.mkdir(parents=True, exist_ok=True)
    extra_cxxflags = args.extra_cxxflags.split() if args.extra_cxxflags else []

    binaries = [
        build_benchmark(src_dir, out_bin_dir, benchmark, args.cxx,
                        extra_cxxflags)
        for benchmark in benchmarks
    ]

    gapbs_source = args.cxlmemuring / "bench" / "gapbs"
    gapbs_state = git_repository_state(gapbs_source)

    manifest = {
        "cxlmemuring": str(args.cxlmemuring),
        "cxlmemuring_commit": cxl_commit(args.cxlmemuring),
        "gapbs_source": str(gapbs_source),
        "gapbs_commit": gapbs_state["commit"],
        "gapbs_dirty": gapbs_state["dirty"],
        "source_copy": str(src_dir),
        "binaries": [str(path) for path in binaries],
        "benchmark_source_sha256": {
            benchmark: sha256_file(src_dir / "src" / f"{benchmark}.cc")
            for benchmark in benchmarks
        },
        "compiler_input_sha256": sha256_tree(src_dir, (".cc", ".h")),
        "builder_script_sha256": sha256_file(Path(__file__)),
        "m5_library_sha256": sha256_file(M5_LIB),
        "gem5_include_sha256": sha256_tree(REPO / "include" / "gem5", (".h",)),
        "instrumentation_include_sha256": {},
        "binary_sha256": {
            benchmark: sha256_file(binary)
            for benchmark, binary in zip(benchmarks, binaries)
        },
        "roi_work_markers": args.roi_work_markers,
        "instrumentation": "plain GAPBS baseline",
    }
    manifest_path = args.outdir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n",
                             encoding="utf-8")
    print(f"Wrote {manifest_path}")


if __name__ == "__main__":
    main()
