#!/usr/bin/env python3
# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

"""Validate, patch, and build the pinned M2NDP simulator checkout."""

import argparse
import os
import re
import shutil
import subprocess
from pathlib import Path

try:
    from scripts import m2ndp_artifacts as artifacts
except ImportError:
    import m2ndp_artifacts as artifacts


REPO = Path(__file__).resolve().parents[1]
PATCH = REPO / "util/m2ndp/patches/0001-funcsim-strict-sequence.patch"
PATCHED_PATHS = frozenset(
    {
        "CMakeLists.txt",
        "functional_runner/main.cc",
        "perf_runner/cxl_probe_main.cc",
        "perf_runner/synthetic_traffic.cc",
        "perf_runner/synthetic_traffic.h",
        "src/memory_map.cc",
        "src/memory_map.h",
        "src/m2ndp.cc",
        "src/m2ndp_config.cc",
    }
)


class PrepareError(RuntimeError):
    pass


def git_output(root, *arguments):
    return subprocess.check_output(
        ["git", "-C", str(root), *arguments],
        text=True,
        stderr=subprocess.STDOUT,
    ).rstrip("\r\n")


def _status_path(line):
    path = line[3:]
    if " -> " in path:
        path = path.split(" -> ", 1)[1]
    return path


def validate_upstream(root):
    root = Path(root)
    commit = git_output(root, "rev-parse", "HEAD")
    if commit != artifacts.EXPECTED_M2NDP_COMMIT:
        raise PrepareError(
            "expected M2NDP commit "
            f"{artifacts.EXPECTED_M2NDP_COMMIT}, found {commit}"
        )
    status = git_output(root, "status", "--porcelain")
    dirty = {
        _status_path(line)
        for line in status.splitlines()
        if line and not line.startswith("?? build")
    }
    unrelated = sorted(dirty.difference(PATCHED_PATHS))
    if unrelated:
        raise PrepareError(
            "M2NDP checkout has unrelated local changes: "
            + ", ".join(unrelated)
        )
    return commit


def _git_apply_check(root, *arguments):
    return subprocess.run(
        ["git", "-C", str(root), "apply", *arguments, str(PATCH)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


def apply_patch(root):
    if not PATCH.is_file():
        raise PrepareError(f"strict patch missing: {PATCH}")
    forward = _git_apply_check(root, "--check")
    if forward.returncode == 0:
        subprocess.run(
            ["git", "-C", str(root), "apply", str(PATCH)],
            check=True,
        )
        return "applied"
    reverse = _git_apply_check(root, "--reverse", "--check")
    if reverse.returncode == 0:
        return "already-applied"
    raise PrepareError(
        "strict patch is neither cleanly applicable nor exactly applied: "
        + forward.stdout.strip()
    )


def require_executable(path):
    path = Path(path)
    if not path.is_file() or not os.access(path, os.X_OK):
        raise PrepareError(f"required tool is not executable: {path}")
    return path


def _copy_tool(source, destination):
    require_executable(source)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    destination.chmod(destination.stat().st_mode | 0o111)
    require_executable(destination)


def _copy_runtime_library(source, destination):
    source = Path(source)
    if not source.is_file():
        raise PrepareError(f"required runtime library is missing: {source}")
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    if not destination.is_file():
        raise PrepareError(
            f"runtime library copy is missing: {destination}"
        )


def _version(command):
    try:
        return subprocess.check_output(
            command, text=True, stderr=subprocess.STDOUT
        ).splitlines()[0]
    except (OSError, subprocess.CalledProcessError) as error:
        return f"unavailable: {error}"


def resolve_tool(command):
    resolved = shutil.which(str(command))
    if resolved is None:
        raise PrepareError(f"required build tool not found: {command}")
    return str(Path(resolved).resolve())


def validate_build_toolchain(*, conan, cmake, cc, cxx):
    tools = {
        "conan": resolve_tool(conan),
        "cmake": resolve_tool(cmake),
        "cc": resolve_tool(cc),
        "cxx": resolve_tool(cxx),
    }
    versions = {
        name: _version([path, "--version"])
        for name, path in tools.items()
    }
    conan_match = re.search(r"\bversion\s+(\d+)\.", versions["conan"])
    if conan_match is None or int(conan_match.group(1)) != 1:
        raise PrepareError(
            "M2NDP requires Conan 1.x; found " + versions["conan"]
        )
    cmake_match = re.search(r"\bversion\s+(\d+)\.", versions["cmake"])
    if cmake_match is None or int(cmake_match.group(1)) >= 4:
        raise PrepareError(
            "M2NDP dependencies require CMake 3.x; found "
            + versions["cmake"]
        )
    return tools, versions


def gcc_major(version_text):
    match = re.search(r"\b(\d+)\.\d+(?:\.\d+)?\b", version_text)
    if match is None:
        raise PrepareError(
            "could not derive GCC major from C++ compiler version: "
            + version_text
        )
    return match.group(1)


def build_state(
    *,
    root,
    commit,
    patch,
    funcsim,
    ndpsim,
    cxl_probe,
    build_commands,
    toolchain=None,
    toolchain_versions=None,
):
    tools = {
        "FuncSim": Path(funcsim),
        "NDPSim": Path(ndpsim),
        "M2NDPCXLProbe": Path(cxl_probe),
    }
    for path in tools.values():
        require_executable(path)
    return {
        "schema": 1,
        "m2ndp_root": str(Path(root).resolve()),
        "upstream_commit": commit,
        "patch_sha256": artifacts.sha256_file(Path(patch)),
        "tool_path": {
            name: str(path.resolve()) for name, path in tools.items()
        },
        "tool_sha256": {
            name: artifacts.sha256_file(path)
            for name, path in tools.items()
        },
        "build_commands": build_commands,
        "compiler": toolchain_versions
        or {
            "cc": _version([os.environ.get("CC", "gcc"), "--version"]),
            "cxx": _version([os.environ.get("CXX", "g++"), "--version"]),
            "cmake": _version(["cmake", "--version"]),
            "conan": _version(["conan", "--version"]),
        },
        "toolchain_path": toolchain or {},
    }


def build_tools(
    root,
    tools_dir,
    *,
    conan="conan",
    cmake="cmake",
    cc="gcc-13",
    cxx="g++-13",
    conan_compiler_version=None,
):
    root = Path(root)
    tools_dir = Path(tools_dir)
    environment = os.environ.copy()
    environment["CC"] = str(cc)
    environment["CXX"] = str(cxx)
    path_prefix = []
    for tool in (conan, cmake):
        tool_path = Path(str(tool))
        if tool_path.parent != Path("."):
            directory = str(tool_path.parent)
            if directory not in path_prefix:
                path_prefix.append(directory)
    if path_prefix:
        environment["PATH"] = os.pathsep.join(
            path_prefix + [environment.get("PATH", "")]
        )
    commands = [
        [
            str(conan),
            "install",
            ".",
            "--install-folder",
            "build",
            "--build=missing",
            *(
                [
                    "--settings",
                    "compiler=gcc",
                    "--settings",
                    f"compiler.version={conan_compiler_version}",
                    "--settings",
                    "compiler.libcxx=libstdc++",
                ]
                if conan_compiler_version is not None
                else []
            ),
        ],
        ["bash", "./scripts/build_functional.sh"],
        ["bash", "./scripts/build_timing.sh"],
    ]
    subprocess.run(commands[0], cwd=root, env=environment, check=True)
    subprocess.run(commands[1], cwd=root, env=environment, check=True)
    funcsim = tools_dir / "bin/FuncSim"
    _copy_tool(root / "build/bin/FuncSim", funcsim)
    subprocess.run(commands[2], cwd=root, env=environment, check=True)
    ndpsim = tools_dir / "bin/NDPSim"
    cxl_probe = tools_dir / "bin/M2NDPCXLProbe"
    _copy_tool(root / "build/bin/NDPSim", ndpsim)
    _copy_tool(root / "build/bin/M2NDPCXLProbe", cxl_probe)
    _copy_runtime_library(
        root / "build/lib/libNDPSim_lib.so",
        tools_dir / "lib/libNDPSim_lib.so",
    )
    return funcsim, ndpsim, cxl_probe, commands


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--m2ndp-root", type=Path, required=True)
    parser.add_argument("--tools-dir", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--build", action="store_true")
    parser.add_argument(
        "--conan", default=os.environ.get("CONAN", "conan")
    )
    parser.add_argument(
        "--cmake", default=os.environ.get("CMAKE", "cmake")
    )
    parser.add_argument("--cc", default=os.environ.get("CC", "gcc-13"))
    parser.add_argument("--cxx", default=os.environ.get("CXX", "g++-13"))
    args = parser.parse_args(argv)

    commit = validate_upstream(args.m2ndp_root)
    patch_status = apply_patch(args.m2ndp_root)
    validate_upstream(args.m2ndp_root)
    if not args.build:
        print(
            f"M2NDP_PREPARED commit={commit} patch={patch_status}",
            flush=True,
        )
        return
    toolchain, toolchain_versions = validate_build_toolchain(
        conan=args.conan,
        cmake=args.cmake,
        cc=args.cc,
        cxx=args.cxx,
    )
    funcsim, ndpsim, cxl_probe, commands = build_tools(
        args.m2ndp_root,
        args.tools_dir,
        conan=toolchain["conan"],
        cmake=toolchain["cmake"],
        cc=toolchain["cc"],
        cxx=toolchain["cxx"],
        conan_compiler_version=gcc_major(toolchain_versions["cxx"]),
    )
    state = build_state(
        root=args.m2ndp_root,
        commit=commit,
        patch=PATCH,
        funcsim=funcsim,
        ndpsim=ndpsim,
        cxl_probe=cxl_probe,
        build_commands=commands,
        toolchain=toolchain,
        toolchain_versions=toolchain_versions,
    )
    state["patch_status"] = patch_status
    artifacts.atomic_write_json(args.state, state)
    print(f"Wrote {args.state}", flush=True)


if __name__ == "__main__":
    main()
