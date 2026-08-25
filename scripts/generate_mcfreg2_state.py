# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

"""Generate provenance-bound MCFREG2 state packages."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path, PurePosixPath


REPO = Path(__file__).resolve().parents[1]
MATCHED_ROOT = REPO / "util/amu/matched_workloads"
COMMON_PATCH = MATCHED_ROOT / "spec_mcf_common.patch"
CAPTURE_PATCH = MATCHED_ROOT / "spec_mcf_capture.patch"
CAPTURE_RUNTIME = (
    MATCHED_ROOT / "mcf_capture.h",
    MATCHED_ROOT / "mcf_capture.c",
)
NATIVE_SOURCES = (
    "implicit.c",
    "mcf.c",
    "mcfutil.c",
    "output.c",
    "pbeampp.c",
    "pbla.c",
    "pflowup.c",
    "psimplex.c",
    "pstart.c",
    "readmin.c",
    "treeup.c",
    "main_wrapper.c",
    "mcf_capture.c",
)


class GenerationError(RuntimeError):
    """Formal MCF state generation cannot continue safely."""


def _git_command(root, *arguments):
    root = Path(root).resolve()
    return (
        "git",
        "-c",
        f"safe.directory={root}",
        "-C",
        str(root),
        *arguments,
    )


def _git_output(root, *arguments):
    try:
        return subprocess.check_output(
            _git_command(root, *arguments),
            text=True,
            stderr=subprocess.STDOUT,
        ).strip()
    except subprocess.CalledProcessError as error:
        raise GenerationError(
            f"cannot inspect MCF source: {error.output.strip()}"
        ) from error


def _git_bytes(root, *arguments):
    try:
        return subprocess.check_output(
            _git_command(root, *arguments), stderr=subprocess.STDOUT
        )
    except subprocess.CalledProcessError as error:
        output = error.output.decode("utf-8", errors="replace").strip()
        raise GenerationError(f"cannot inspect MCF source: {output}") from error


def _sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _source_subdir(value):
    if not isinstance(value, str) or not value:
        raise GenerationError("MCF source subdirectory is invalid")
    relative = PurePosixPath(value)
    if relative.is_absolute() or ".." in relative.parts or "." in relative.parts:
        raise GenerationError("MCF source subdirectory must be normalized")
    return relative


def _tracked_files(source_root, source_subdir):
    payload = _git_bytes(
        source_root, "ls-files", "-z", "--", source_subdir.as_posix()
    )
    names = [name for name in payload.split(b"\0") if name]
    if not names:
        raise GenerationError("MCF source has no tracked files")
    result = []
    for raw_name in names:
        try:
            name = raw_name.decode("utf-8")
        except UnicodeDecodeError as error:
            raise GenerationError("MCF tracked path is not UTF-8") from error
        relative = PurePosixPath(name)
        if relative.is_absolute() or ".." in relative.parts:
            raise GenerationError("MCF tracked path escapes the source root")
        if not relative.is_relative_to(source_subdir):
            raise GenerationError("Git returned a file outside the MCF path")
        path = source_root.joinpath(*relative.parts)
        if not path.is_file():
            raise GenerationError(f"MCF tracked file is missing: {relative}")
        result.append((relative, path))
    return tuple(sorted(result, key=lambda item: item[0].as_posix()))


def _tree_identity(files):
    digest = hashlib.sha256()
    rows = []
    for relative, path in files:
        payload = path.read_bytes()
        file_digest = hashlib.sha256(payload).hexdigest()
        encoded = relative.as_posix().encode("utf-8")
        digest.update(encoded)
        digest.update(b"\0")
        digest.update(str(len(payload)).encode("ascii"))
        digest.update(b"\0")
        digest.update(payload)
        rows.append({
            "path": relative.as_posix(),
            "bytes": len(payload),
            "sha256": file_digest,
        })
    return digest.hexdigest(), tuple(rows)


def freeze_source(
    *,
    source_root,
    expected_commit,
    source_subdir,
    input_path,
    expected_input_sha256,
    destination,
):
    source_root = Path(source_root).resolve()
    destination = Path(destination)
    if destination.exists():
        raise GenerationError(
            f"MCF frozen destination already exists: {destination}"
        )
    if not source_root.is_dir():
        raise GenerationError(f"MCF source root does not exist: {source_root}")
    repository_root = Path(
        _git_output(source_root, "rev-parse", "--show-toplevel")
    ).resolve()
    if repository_root != source_root:
        raise GenerationError("MCF source root is not the Git repository root")
    actual_commit = _git_output(source_root, "rev-parse", "HEAD")
    if actual_commit != expected_commit:
        raise GenerationError(
            "MCF source commit differs: "
            f"expected {expected_commit}, found {actual_commit}"
        )
    subdir = _source_subdir(source_subdir)
    status = _git_output(
        source_root, "status", "--porcelain", "--", subdir.as_posix()
    )
    if status:
        raise GenerationError(f"MCF source path is dirty:\n{status}")
    files = _tracked_files(source_root, subdir)
    tree_digest, file_rows = _tree_identity(files)

    input_path = Path(input_path).resolve()
    source_path = source_root.joinpath(*subdir.parts).resolve()
    try:
        input_relative_to_source = input_path.relative_to(source_path)
        input_relative_to_repo = input_path.relative_to(source_root)
    except ValueError as error:
        raise GenerationError("MCF input is outside the source path") from error
    if not input_path.is_file():
        raise GenerationError(f"MCF input does not exist: {input_path}")
    tracked_names = {row[0].as_posix() for row in files}
    if input_relative_to_repo.as_posix() not in tracked_names:
        raise GenerationError("MCF input is not a tracked source file")
    actual_input_sha256 = _sha256_file(input_path)
    if actual_input_sha256 != expected_input_sha256:
        raise GenerationError(
            "MCF input SHA-256 differs: "
            f"expected {expected_input_sha256}, found {actual_input_sha256}"
        )

    copied_root = destination.resolve()
    try:
        for relative, source in files:
            target = copied_root.joinpath(*relative.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            if _sha256_file(target) != _sha256_file(source):
                raise GenerationError(
                    f"MCF frozen copy SHA-256 differs: {relative}"
                )
    except Exception:
        shutil.rmtree(copied_root, ignore_errors=True)
        raise

    copied_input = copied_root / source_path.relative_to(source_root)
    copied_input = copied_input / input_relative_to_source
    return {
        "schema": 1,
        "source_root": str(source_root),
        "source_subdir": subdir.as_posix(),
        "source_commit": actual_commit,
        "source_tree_sha256": tree_digest,
        "tracked_file_count": len(files),
        "tracked_files": list(file_rows),
        "input": str(input_path),
        "input_relative": input_relative_to_repo.as_posix(),
        "input_sha256": actual_input_sha256,
        "input_bytes": input_path.stat().st_size,
        "copied_source_root": str(copied_root),
        "copied_input": str(copied_input),
    }


def _run_checked(command, *, cwd, label):
    completed = subprocess.run(
        [str(item) for item in command],
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    if completed.returncode != 0:
        raise GenerationError(
            f"{label} exited {completed.returncode}:\n{completed.stdout}"
        )
    return completed.stdout


def prepare_native_source(*, frozen, capture_enabled):
    copied_root = Path(frozen["copied_source_root"]).resolve()
    source_dir = copied_root / frozen["source_subdir"]
    if not source_dir.is_dir():
        raise GenerationError(f"frozen MCF source is missing: {source_dir}")
    if not COMMON_PATCH.is_file():
        raise GenerationError(f"common MCF patch is missing: {COMMON_PATCH}")
    patch_sha256 = _sha256_file(COMMON_PATCH)
    _run_checked(
        (
            "git",
            "apply",
            "--check",
            "--unidiff-zero",
            "--whitespace=error-all",
            COMMON_PATCH,
        ),
        cwd=source_dir,
        label="common MCF patch check",
    )
    _run_checked(
        (
            "git",
            "apply",
            "--unidiff-zero",
            "--whitespace=error-all",
            COMMON_PATCH,
        ),
        cwd=source_dir,
        label="common MCF patch apply",
    )
    capture_patch_sha256 = None
    if capture_enabled:
        if not CAPTURE_PATCH.is_file():
            raise GenerationError(
                f"capture MCF patch is missing: {CAPTURE_PATCH}"
            )
        capture_patch_sha256 = _sha256_file(CAPTURE_PATCH)
        capture_command = (
            "git",
            "apply",
            "--unidiff-zero",
            "--whitespace=error-all",
            CAPTURE_PATCH,
        )
        _run_checked(
            capture_command[:2] + ("--check",) + capture_command[2:],
            cwd=source_dir,
            label="capture MCF patch check",
        )
        _run_checked(
            capture_command,
            cwd=source_dir,
            label="capture MCF patch apply",
        )
    runtime_rows = []
    for source in CAPTURE_RUNTIME:
        if not source.is_file():
            raise GenerationError(f"MCF capture runtime is missing: {source}")
        target = source_dir / source.name
        shutil.copy2(source, target)
        digest = _sha256_file(source)
        if _sha256_file(target) != digest:
            raise GenerationError(
                f"MCF capture runtime copy differs: {source.name}"
            )
        runtime_rows.append({"name": source.name, "sha256": digest})
    return {
        **frozen,
        "source_dir": str(source_dir),
        "capture_enabled": bool(capture_enabled),
        "common_patch": str(COMMON_PATCH.resolve()),
        "common_patch_sha256": patch_sha256,
        "capture_patch": (
            str(CAPTURE_PATCH.resolve()) if capture_enabled else None
        ),
        "capture_patch_sha256": capture_patch_sha256,
        "capture_runtime": runtime_rows,
    }


def _compiler_identity(compiler):
    path = Path(compiler).resolve()
    if not path.is_file():
        raise GenerationError(f"C compiler does not exist: {path}")
    version = _run_checked((path, "--version"), cwd=REPO, label="compiler")
    return {"path": str(path), "version": version.splitlines()[0]}


def build_native(*, prepared, output, compiler):
    source_dir = Path(prepared["source_dir"]).resolve()
    output = Path(output).resolve()
    if output.exists():
        raise GenerationError(f"native MCF output already exists: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    missing = [name for name in NATIVE_SOURCES if not (source_dir / name).is_file()]
    if missing:
        raise GenerationError(f"native MCF source is missing: {missing[0]}")
    compiler_row = _compiler_identity(compiler)
    command = [
        compiler_row["path"],
        "-O2",
        "-std=gnu11",
        "-I",
        str(source_dir),
    ]
    if prepared["capture_enabled"]:
        command.append("-DMCF_CAPTURE_EVENTS=1")
    command.extend(str(source_dir / name) for name in NATIVE_SOURCES)
    command.extend(("-o", str(output)))
    build_output = _run_checked(command, cwd=source_dir, label="native MCF build")
    if not output.is_file():
        raise GenerationError("native MCF build did not create its binary")
    row = {
        "schema": 1,
        "binary": str(output),
        "binary_sha256": _sha256_file(output),
        "compiler": compiler_row,
        "command": command,
        "stdout": build_output,
        "common_patch_sha256": prepared["common_patch_sha256"],
        "source_tree_sha256": prepared["source_tree_sha256"],
        "capture_enabled": prepared["capture_enabled"],
    }
    (output.parent / f"{output.name}.build.json").write_text(
        json.dumps(row, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return output


def run_native(*, binary, input_path, output_root):
    binary = Path(binary).resolve()
    input_path = Path(input_path).resolve()
    output_root = Path(output_root).resolve()
    if not binary.is_file():
        raise GenerationError(f"native MCF binary is missing: {binary}")
    if not input_path.is_file():
        raise GenerationError(f"native MCF input is missing: {input_path}")
    if output_root.exists():
        raise GenerationError(
            f"native MCF run root already exists: {output_root}"
        )
    output_root.mkdir(parents=True)
    command = (
        str(binary),
        "--input",
        str(input_path),
        "--output-root",
        str(output_root),
    )
    completed = subprocess.run(
        command,
        cwd=output_root,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    (output_root / "stdout.log").write_text(
        completed.stdout, encoding="utf-8"
    )
    (output_root / "stderr.log").write_text(
        completed.stderr, encoding="utf-8"
    )
    if completed.returncode != 0:
        raise GenerationError(
            f"native MCF run exited {completed.returncode}:\n"
            f"{completed.stderr}{completed.stdout}"
        )
    record = output_root / "run.json"
    if not record.is_file():
        raise GenerationError("native MCF run did not write run.json")
    try:
        value = json.loads(record.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise GenerationError(f"invalid native MCF run.json: {error}") from error
    if value.get("input") != str(input_path):
        raise GenerationError("native MCF run used a different input")
    value["command"] = list(command)
    value["stdout"] = str((output_root / "stdout.log").resolve())
    value["stderr"] = str((output_root / "stderr.log").resolve())
    value["binary_sha256"] = _sha256_file(binary)
    return value
