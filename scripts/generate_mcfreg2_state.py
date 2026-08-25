# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

"""Generate provenance-bound MCFREG2 state packages."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath

try:
    from scripts import mcfreg2
except ImportError:  # Support direct execution from the scripts directory.
    import mcfreg2


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
STATE_MAGIC = b"MCFSTATE2"
STATE_NETWORK_WORDS = 22
STATE_NODE_BYTES = 176
STATE_ARC_BYTES = 96


class GenerationError(RuntimeError):
    """Formal MCF state generation cannot continue safely."""


FORMAL_MINIMUM_ALLOCATED_BYTES = 345_000_000


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
    return {
        "path": str(path),
        "version": version.splitlines()[0],
        "sha256": _sha256_file(path),
    }


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


def _canonical_json(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii") + b"\n"


def _read_json(path, label):
    path = Path(path)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise GenerationError(f"invalid {label}: {error}") from error
    if not isinstance(value, dict):
        raise GenerationError(f"invalid {label}: root is not an object")
    return value


def _read_json_lines(path, label):
    path = Path(path)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
        rows = [json.loads(line) for line in lines]
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise GenerationError(f"invalid {label}: {error}") from error
    if not rows or not all(isinstance(row, dict) for row in rows):
        raise GenerationError(f"invalid {label}: event stream is empty")
    return rows


def _checked_state_end(offset, count, width, total, label):
    if count < 0 or width < 0 or count > (1 << 64) - 1:
        raise GenerationError(f"invalid MCF state {label} count")
    if count and width > ((1 << 64) - 1) // count:
        raise GenerationError(f"MCF state {label} size overflows")
    end = offset + count * width
    if end > total:
        raise GenerationError(f"MCF state {label} is truncated")
    return end


def _read_native_state(path):
    path = Path(path)
    try:
        payload = path.read_bytes()
    except OSError as error:
        raise GenerationError(f"cannot read native MCF state: {error}") from error
    network_bytes = STATE_NETWORK_WORDS * 8
    network_end = len(STATE_MAGIC) + network_bytes
    if len(payload) < network_end or not payload.startswith(STATE_MAGIC):
        raise GenerationError("native MCF state header differs")
    words = struct.unpack_from(
        f"<{STATE_NETWORK_WORDS}Q", payload, len(STATE_MAGIC)
    )
    n = words[0]
    max_m = words[2]
    m = words[3]
    if n == (1 << 64) - 1 or m == 0 or max_m < m:
        raise GenerationError("native MCF state counts are invalid")
    nodes_begin = network_end
    nodes_end = _checked_state_end(
        nodes_begin, n + 1, STATE_NODE_BYTES, len(payload), "nodes"
    )
    active_end = _checked_state_end(
        nodes_end, m, STATE_ARC_BYTES, len(payload), "active arcs"
    )
    dummy_end = _checked_state_end(
        active_end, n, STATE_ARC_BYTES, len(payload), "dummy arcs"
    )
    if dummy_end != len(payload):
        raise GenerationError("native MCF state has trailing bytes")
    return {
        "payload": payload,
        "sha256": hashlib.sha256(payload).hexdigest(),
        "network": payload[len(STATE_MAGIC):network_end],
        "nodes": payload[nodes_begin:nodes_end],
        "active_arcs": payload[nodes_end:active_end],
        "dummy_arcs": payload[active_end:dummy_end],
        "n": n,
        "nodes_count": n + 1,
        "m": m,
        "max_m": max_m,
        "words": words,
    }


def _ordered_frames(pricing_rows, price_out_rows):
    frames = []
    for phase, rows in (("pricing", pricing_rows), ("price_out", price_out_rows)):
        grouped = {}
        for row in rows:
            if not isinstance(row.get("call"), int) or row["call"] < 0:
                raise GenerationError(f"{phase} event has invalid call ordinal")
            grouped.setdefault(row["call"], []).append(row)
        if sorted(grouped) != list(range(len(grouped))):
            raise GenerationError(f"{phase} call ordinals are not contiguous")
        for ordinal, frame_rows in sorted(grouped.items()):
            if (
                frame_rows[0].get("kind") != "BEGIN"
                or frame_rows[-1].get("kind") != "END"
            ):
                raise GenerationError(f"{phase} call frame is incomplete")
            order = frame_rows[0].get("order")
            if not isinstance(order, int) or order < 0:
                raise GenerationError(f"{phase} call order is invalid")
            frames.append({
                "phase": phase,
                "ordinal": ordinal,
                "order": order,
                "rows": frame_rows,
            })
    frames.sort(key=lambda frame: frame["order"])
    if [frame["order"] for frame in frames] != list(range(len(frames))):
        raise GenerationError("native MCF call order is not contiguous")
    return frames


def _frame_sections(frames):
    events = []
    call_index = []
    boundaries = []
    basket_rows = []
    delta_rows = []
    for frame in frames:
        start = len(events)
        normalized = []
        for row in frame["rows"]:
            event = {
                "phase_name": frame["phase"],
                "ordinal": frame["ordinal"],
                **row,
            }
            normalized.append(event)
            events.append(event)
            if row.get("kind") == "BASKET":
                basket_rows.append(event)
            if row.get("kind") in {
                "ARC_STATE", "ARENA_REMAP", "ADJACENCY"
            }:
                delta_rows.append(event)
        live_in = [
            row for row in normalized
            if row.get("kind") == "BEGIN"
            or (row.get("kind") == "BASKET" and row.get("phase") == "live_in")
        ]
        live_out = [
            row for row in normalized
            if row.get("kind") == "END"
            or (row.get("kind") == "BASKET" and row.get("phase") == "live_out")
            or row.get("kind") in {"ARC_STATE", "ARENA_REMAP", "ADJACENCY"}
        ]
        call_index.append({
            "phase": frame["phase"],
            "ordinal": frame["ordinal"],
            "order": frame["order"],
            "event_begin": start,
            "event_count": len(normalized),
        })
        boundaries.append({
            "phase": frame["phase"],
            "ordinal": frame["ordinal"],
            "order": frame["order"],
            "pre_sha256": hashlib.sha256(_canonical_json(live_in)).hexdigest(),
            "post_sha256": hashlib.sha256(
                _canonical_json(live_out)
            ).hexdigest(),
        })
    return events, call_index, boundaries, basket_rows, delta_rows


def _validate_identity(identity):
    required = (
        "source_commit",
        "source_tree_sha256",
        "input_sha256",
        "common_patch_sha256",
        "capture_patch_sha256",
        "compiler_sha256",
    )
    if not isinstance(identity, dict) or set(identity) != set(required):
        raise GenerationError("MCF package identity fields differ")
    if not isinstance(identity["source_commit"], str) or len(
        identity["source_commit"]
    ) != 40:
        raise GenerationError("MCF package source commit is invalid")
    for name in required[1:]:
        value = identity[name]
        if not isinstance(value, str) or len(value) != 64:
            raise GenerationError(f"MCF package {name} is invalid")
        try:
            bytes.fromhex(value)
        except ValueError as error:
            raise GenerationError(f"MCF package {name} is invalid") from error
    return dict(identity)


def assemble_capture_package(*, run_root, identity, output):
    run_root = Path(run_root).resolve()
    identity = _validate_identity(identity)
    run = _read_json(run_root / "run.json", "native MCF run.json")
    if run.get("capture_enabled") is not True:
        raise GenerationError("MCF package source is not a capture run")
    initial = _read_native_state(run_root / "initial.state")
    final = _read_native_state(run_root / "final.state")
    pricing = _read_json_lines(run_root / "pricing.jsonl", "pricing journal")
    price_out = _read_json_lines(
        run_root / "price_out.jsonl", "price-out journal"
    )
    frames = _ordered_frames(pricing, price_out)
    events, call_index, boundaries, basket, deltas = _frame_sections(frames)
    pricing_calls = sum(frame["phase"] == "pricing" for frame in frames)
    price_out_calls = sum(frame["phase"] == "price_out" for frame in frames)
    if (
        pricing_calls != run.get("pricing_calls")
        or price_out_calls != run.get("price_out_calls")
    ):
        raise GenerationError("native MCF run call counts differ")
    output_path = run_root / "mcf.out"
    if not output_path.is_file():
        raise GenerationError("native MCF output is missing")
    provenance = {
        "schema": 1,
        **identity,
        "roi_begin": run.get("roi_begin"),
        "roi_end": run.get("roi_end"),
        "capture_enabled": True,
    }
    final_row = {
        "schema": 1,
        "initial_state_sha256": initial["sha256"],
        "final_state_sha256": final["sha256"],
        "final_network_words": list(final["words"]),
        "mcf_output_bytes": output_path.stat().st_size,
        "mcf_output_sha256": _sha256_file(output_path),
        "peak_allocated_bytes": run.get("peak_allocated_bytes"),
    }
    sections = {
        "PROVENANCE": _canonical_json(provenance),
        "NETWORK": initial["network"],
        "NODES": initial["nodes"],
        "ARCS": initial["active_arcs"] + initial["dummy_arcs"],
        "BASKET": _canonical_json({"schema": 1, "rows": basket}),
        "CALL_INDEX": _canonical_json({"schema": 1, "rows": call_index}),
        "EVENTS": b"".join(_canonical_json(row) for row in events),
        "DELTAS": _canonical_json({"schema": 1, "rows": deltas}),
        "BOUNDARIES": _canonical_json({"schema": 1, "rows": boundaries}),
        "FINAL": _canonical_json(final_row),
    }
    package = mcfreg2.new_package(
        nodes=initial["nodes_count"],
        active_arcs=initial["m"],
        dummy_arcs=initial["n"],
        arena_capacity=initial["max_m"],
        pricing_calls=pricing_calls,
        price_out_calls=price_out_calls,
        event_count=len(events),
        sections=sections,
        section_layouts={
            "NETWORK": (STATE_NETWORK_WORDS, 8),
            "NODES": (initial["nodes_count"], STATE_NODE_BYTES),
            "ARCS": (
                initial["m"] + initial["n"],
                STATE_ARC_BYTES,
            ),
            "BASKET": (len(basket), 0),
            "CALL_INDEX": (len(call_index), 0),
            "EVENTS": (len(events), 0),
            "DELTAS": (len(deltas), 0),
            "BOUNDARIES": (len(boundaries), 0),
        },
    )
    digest = mcfreg2.write_package(output, package)
    parsed = mcfreg2.read_package(output)
    if parsed.header.event_count != len(events):
        raise GenerationError("written MCFREG2 event count differs")
    return {
        "package": str(Path(output).resolve()),
        "package_sha256": digest,
        "pricing_calls": pricing_calls,
        "price_out_calls": price_out_calls,
        "event_count": len(events),
        "initial_state_sha256": initial["sha256"],
        "final_state_sha256": final["sha256"],
        "mcf_output_sha256": final_row["mcf_output_sha256"],
        "peak_allocated_bytes": final_row["peak_allocated_bytes"],
    }


def _atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(_canonical_json(value))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _same_file(left, right):
    left = Path(left)
    right = Path(right)
    return (
        left.is_file()
        and right.is_file()
        and left.stat().st_size == right.stat().st_size
        and _sha256_file(left) == _sha256_file(right)
    )


def _link_or_copy(source, destination):
    try:
        os.link(source, destination)
        return destination
    except OSError:
        return shutil.copy2(source, destination)


def _clone_tree(source, destination):
    source = Path(source).resolve()
    if not source.is_dir():
        raise GenerationError(f"publication source root is missing: {source}")
    shutil.copytree(source, destination, copy_function=_link_or_copy)


def _tree_hashes(root):
    root = Path(root)
    return [
        {
            "path": path.relative_to(root).as_posix(),
            "bytes": path.stat().st_size,
            "sha256": _sha256_file(path),
        }
        for path in sorted(root.rglob("*"))
        if path.is_file()
    ]


def _fsync_tree(root):
    root = Path(root)
    for path in root.rglob("*"):
        if path.is_file():
            with path.open("rb") as stream:
                os.fsync(stream.fileno())
    directories = [root, *(path for path in root.rglob("*") if path.is_dir())]
    for path in reversed(directories):
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _publication_bytes(*roots):
    total = 1024 * 1024
    names = (
        "initial.state",
        "final.state",
        "pricing.jsonl",
        "price_out.jsonl",
        "mcf.out",
    )
    for root in roots:
        root = Path(root)
        for name in names:
            path = root / name
            if path.is_file():
                total += path.stat().st_size * 4
    return total


def _run_independent_replay(package, staging):
    cxx = shutil.which("g++")
    if cxx is None:
        raise GenerationError("independent_replay: g++ is unavailable")
    cxx = str(Path(cxx).resolve())
    binary = staging / "mcfreg2-replayer"
    compile_command = (
        cxx,
        "-std=c++17",
        "-O2",
        "-Wall",
        "-Wextra",
        "-Werror",
        "-I",
        str(MATCHED_ROOT),
        str(MATCHED_ROOT / "mcf_regions.cc"),
        str(MATCHED_ROOT / "mcfreg2.cc"),
        "-o",
        str(binary),
    )
    compile_output = _run_checked(
        compile_command, cwd=REPO, label="independent_replay compile"
    )
    output_root = staging / "replay-validation"
    output_root.mkdir()
    trace = output_root / "canonical.trace"
    run_command = (
        str(binary),
        "--input",
        str(package),
        "--output-root",
        str(output_root),
        "--trace",
        str(trace),
    )
    run_output = _run_checked(
        run_command, cwd=REPO, label="independent_replay run"
    )
    replay_record = _read_json(
        output_root / "mcfreg2-replay.json", "C++ replay validation"
    )
    if (
        replay_record.get("status") != "verified"
        or replay_record.get("boundary_mismatches") != 0
    ):
        raise GenerationError("independent_replay: validation differs")
    return {
        "compiler": {
            "path": cxx,
            "sha256": _sha256_file(cxx),
            "version": _run_checked(
                (cxx, "--version"), cwd=REPO, label="C++ compiler"
            ).splitlines()[0],
        },
        "compile_command": list(compile_command),
        "compile_stdout": compile_output,
        "run_command": list(run_command),
        "run_stdout": run_output,
        "binary_sha256": _sha256_file(binary),
        "trace_sha256": _sha256_file(trace),
        "validation_sha256": _sha256_file(
            output_root / "mcfreg2-replay.json"
        ),
        **replay_record,
    }


def generate_candidate(
    *,
    authority_root,
    capture_primary_root,
    capture_replay_root,
    identity,
    evidence_root,
    source_record=None,
):
    evidence_root = Path(evidence_root).resolve()
    evidence_root.mkdir(parents=True, exist_ok=True)
    staging = None
    gate = "preflight"
    try:
        identity = _validate_identity(identity)
        authority_root = Path(authority_root).resolve()
        primary_root = Path(capture_primary_root).resolve()
        replay_root = Path(capture_replay_root).resolve()
        source_record = (
            Path(source_record).resolve() if source_record is not None else None
        )
        if source_record is not None and not source_record.is_file():
            raise GenerationError("preflight: source record is missing")
        required_bytes = _publication_bytes(primary_root, replay_root)
        available_bytes = shutil.disk_usage(evidence_root).free
        if available_bytes < required_bytes:
            raise GenerationError(
                "preflight: insufficient disk space for atomic publication"
            )
        staging = Path(
            tempfile.mkdtemp(prefix=".candidate-", dir=evidence_root)
        )
        gate = "native_equivalence"
        authority_run = _read_json(
            authority_root / "run.json", "authority MCF run.json"
        )
        primary_run = _read_json(
            primary_root / "run.json", "primary MCF run.json"
        )
        replay_run = _read_json(
            replay_root / "run.json", "replay MCF run.json"
        )
        for name in ("final.state", "mcf.out"):
            if not _same_file(authority_root / name, primary_root / name):
                raise GenerationError(
                    f"native_equivalence: authority/primary {name} differs"
                )
            if not _same_file(authority_root / name, replay_root / name):
                raise GenerationError(
                    f"native_equivalence: authority/replay {name} differs"
                )
        peaks = tuple(
            run.get("peak_allocated_bytes")
            for run in (authority_run, primary_run, replay_run)
        )
        if (
            any(
                not isinstance(peak, int) or isinstance(peak, bool) or peak <= 0
                for peak in peaks
            )
            or len(set(peaks)) != 1
        ):
            raise GenerationError(
                "native_equivalence: authority/capture peak allocation differs"
            )
        gate = "capture_determinism"
        primary = assemble_capture_package(
            run_root=primary_root,
            identity=identity,
            output=staging / "primary.reg2",
        )
        replay = assemble_capture_package(
            run_root=replay_root,
            identity=identity,
            output=staging / "replay.reg2",
        )
        if not _same_file(primary["package"], replay["package"]):
            raise GenerationError(
                "capture_determinism: primary/replay package bytes differ"
            )
        digest = primary["package_sha256"]
        if digest != replay["package_sha256"]:
            raise GenerationError(
                "capture_determinism: primary/replay package SHA-256 differs"
            )
        shutil.copy2(staging / "primary.reg2", staging / "mcf.reg2")
        gate = "independent_replay"
        replay_validation = _run_independent_replay(
            staging / "mcf.reg2", staging
        )
        _clone_tree(authority_root, staging / "authority")
        _clone_tree(primary_root, staging / "capture-primary")
        _clone_tree(replay_root, staging / "capture-replay")
        source_sha256 = None
        if source_record is not None:
            shutil.copy2(source_record, staging / "source.json")
            source_sha256 = _sha256_file(staging / "source.json")
        authority_final_sha256 = _sha256_file(authority_root / "final.state")
        primary_final_sha256 = _sha256_file(primary_root / "final.state")
        replay_final_sha256 = _sha256_file(replay_root / "final.state")
        authority_output_sha256 = _sha256_file(authority_root / "mcf.out")
        primary_output_sha256 = _sha256_file(primary_root / "mcf.out")
        replay_output_sha256 = _sha256_file(replay_root / "mcf.out")
        manifest = {
            "schema": 1,
            "status": "candidate",
            "identity": identity,
            "package_sha256": digest,
            "primary_package_sha256": primary["package_sha256"],
            "replay_package_sha256": replay["package_sha256"],
            "authority_final_state_sha256": authority_final_sha256,
            "authority_mcf_output_sha256": authority_output_sha256,
            "independent_replay": replay_validation,
            "published_runs": {
                "authority": _tree_hashes(staging / "authority"),
                "capture_primary": _tree_hashes(staging / "capture-primary"),
                "capture_replay": _tree_hashes(staging / "capture-replay"),
            },
        }
        validation = {
            "schema": 2,
            "status": "accepted",
            "identity": identity,
            "package_sha256": digest,
            "primary_package_sha256": primary["package_sha256"],
            "replay_package_sha256": replay["package_sha256"],
            "primary_replay_equal": True,
            "native_outputs_equal": True,
            "boundary_mismatches": replay_validation[
                "boundary_mismatches"
            ],
            "pricing_calls": primary["pricing_calls"],
            "price_out_calls": primary["price_out_calls"],
            "event_count": primary["event_count"],
            "authority_final_state_sha256": authority_final_sha256,
            "capture_primary_final_state_sha256": primary_final_sha256,
            "capture_replay_final_state_sha256": replay_final_sha256,
            "authority_mcf_output_sha256": authority_output_sha256,
            "capture_primary_mcf_output_sha256": primary_output_sha256,
            "capture_replay_mcf_output_sha256": replay_output_sha256,
            "peak_allocated_bytes": primary["peak_allocated_bytes"],
            "canonical_trace_sha256": replay_validation["trace_sha256"],
            "replay_validation_sha256": replay_validation[
                "validation_sha256"
            ],
        }
        if source_sha256 is not None:
            validation["source_sha256"] = source_sha256
        _atomic_json(staging / "manifest.json", manifest)
        _atomic_json(staging / "validation.json", validation)
        final_root = evidence_root / digest
        if source_sha256 is not None:
            _atomic_json(staging / "candidate-record.json", {
                "schema": 1,
                "status": "candidate",
                "workload": "mcf",
                "record": {
                    "input": str(final_root / "mcf.reg2"),
                    "input_sha256": digest,
                    "allocated_bytes": primary["peak_allocated_bytes"],
                    "source": str(final_root / "source.json"),
                    "source_sha256": source_sha256,
                    "synthetic": False,
                    "format": "MCFREG2",
                    "source_commit": identity["source_commit"],
                    "source_tree_sha256": identity["source_tree_sha256"],
                    "validation": str(final_root / "validation.json"),
                    "validation_sha256": _sha256_file(
                        staging / "validation.json"
                    ),
                },
            })
        _fsync_tree(staging)
        if final_root.exists():
            existing_manifest = _read_json(
                final_root / "manifest.json", "existing MCF manifest"
            )
            if (
                existing_manifest.get("identity") != identity
                or not _same_file(
                    final_root / "mcf.reg2", staging / "mcf.reg2"
                )
                or not _same_file(
                    final_root / "validation.json",
                    staging / "validation.json",
                )
            ):
                raise GenerationError(
                    "publication: existing accepted root identity differs"
                )
            shutil.rmtree(staging)
        else:
            os.replace(staging, final_root)
            root_fd = os.open(evidence_root, os.O_RDONLY)
            try:
                os.fsync(root_fd)
            finally:
                os.close(root_fd)
        return {
            "accepted_root": str(final_root),
            "package_sha256": digest,
            "package": str(final_root / "mcf.reg2"),
            "primary_package": str(final_root / "primary.reg2"),
            "replay_package": str(final_root / "replay.reg2"),
            "manifest": str(final_root / "manifest.json"),
            "validation": str(final_root / "validation.json"),
            "candidate": (
                str(final_root / "candidate-record.json")
                if source_sha256 is not None
                else None
            ),
        }
    except Exception as error:
        if staging is not None and staging.exists():
            shutil.rmtree(staging)
        failure = {
            "schema": 1,
            "status": "failed_input",
            "first_failed_gate": gate,
            "error": str(error),
        }
        try:
            _atomic_json(evidence_root / "failed-input.json", failure)
        except Exception:
            pass
        if isinstance(error, GenerationError):
            if gate not in str(error):
                raise GenerationError(f"{gate}: {error}") from error
            raise
        raise GenerationError(f"{gate}: {error}") from error


def _resolve_compiler(compiler):
    resolved = shutil.which(str(compiler))
    if resolved is None:
        path = Path(compiler).resolve()
        if not path.is_file():
            raise GenerationError(f"C compiler is missing: {compiler}")
        resolved = str(path)
    return str(Path(resolved).resolve())


def _available_memory_bytes():
    try:
        for line in Path("/proc/meminfo").read_text(encoding="ascii").splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) * 1024
    except (OSError, UnicodeDecodeError, ValueError, IndexError):
        pass
    pages = os.sysconf("SC_AVPHYS_PAGES")
    page_size = os.sysconf("SC_PAGE_SIZE")
    return int(pages) * int(page_size)


def preflight(
    *,
    source_root,
    source_commit,
    source_subdir,
    input_path,
    input_sha256,
    output_root,
    compiler="cc",
):
    output_root = Path(output_root).resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    compiler = _resolve_compiler(compiler)
    scratch = Path(tempfile.mkdtemp(prefix=".preflight-", dir=output_root))
    try:
        frozen = freeze_source(
            source_root=source_root,
            expected_commit=source_commit,
            source_subdir=source_subdir,
            input_path=input_path,
            expected_input_sha256=input_sha256,
            destination=scratch / "frozen",
        )
        probe = scratch / "lp64.c"
        probe.write_text(
            "#include <stdint.h>\n"
            "int main(void) { return sizeof(long) == 8 && "
            "sizeof(void *) == 8 && sizeof(int64_t) == 8 ? 0 : 1; }\n",
            encoding="ascii",
        )
        binary = scratch / "lp64"
        _run_checked(
            (compiler, "-std=c11", "-Wall", "-Wextra", "-Werror", probe,
             "-o", binary),
            cwd=scratch,
            label="LP64 probe compile",
        )
        _run_checked((binary,), cwd=scratch, label="LP64 probe run")
        disk = shutil.disk_usage(output_root).free
        memory = _available_memory_bytes()
        required_disk = max(1 << 30, frozen["input_bytes"] * 16384)
        required_memory = max(512 << 20, frozen["input_bytes"] * 1024)
        if disk < required_disk:
            raise GenerationError(
                "preflight: insufficient disk capacity: "
                f"required={required_disk} available={disk}"
            )
        if memory < required_memory:
            raise GenerationError(
                "preflight: insufficient memory capacity: "
                f"required={required_memory} available={memory}"
            )
        return {
            "schema": 1,
            "status": "ready",
            "source_commit": frozen["source_commit"],
            "source_tree_sha256": frozen["source_tree_sha256"],
            "tracked_file_count": frozen["tracked_file_count"],
            "input_sha256": frozen["input_sha256"],
            "input_bytes": frozen["input_bytes"],
            "lp64": True,
            "compiler": _compiler_identity(compiler),
            "available_disk_bytes": disk,
            "required_disk_bytes": required_disk,
            "available_memory_bytes": memory,
            "required_memory_bytes": required_memory,
        }
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def _source_record(frozen):
    return {
        name: frozen[name]
        for name in (
            "schema",
            "source_root",
            "source_subdir",
            "source_commit",
            "source_tree_sha256",
            "tracked_file_count",
            "tracked_files",
            "input",
            "input_relative",
            "input_sha256",
            "input_bytes",
        )
    }


def _attach_binary(run_root, binary):
    run_root = Path(run_root)
    binary = Path(binary)
    shutil.copy2(binary, run_root / "native-mcf")
    build_record = binary.parent / f"{binary.name}.build.json"
    if not build_record.is_file():
        raise GenerationError("native MCF build record is missing")
    shutil.copy2(build_record, run_root / "native-mcf.build.json")


def generate_evidence(
    *,
    source_root,
    source_commit,
    source_subdir,
    input_path,
    input_sha256,
    output_root,
    compiler="cc",
):
    output_root = Path(output_root).resolve()
    readiness = preflight(
        source_root=source_root,
        source_commit=source_commit,
        source_subdir=source_subdir,
        input_path=input_path,
        input_sha256=input_sha256,
        output_root=output_root,
        compiler=compiler,
    )
    compiler = _resolve_compiler(compiler)
    work = Path(tempfile.mkdtemp(prefix=".generation-", dir=output_root))
    try:
        authority_frozen = freeze_source(
            source_root=source_root,
            expected_commit=source_commit,
            source_subdir=source_subdir,
            input_path=input_path,
            expected_input_sha256=input_sha256,
            destination=work / "authority-source",
        )
        authority_prepared = prepare_native_source(
            frozen=authority_frozen, capture_enabled=False
        )
        authority_binary = build_native(
            prepared=authority_prepared,
            output=work / "bin/authority-mcf",
            compiler=compiler,
        )
        authority_run = work / "authority-run"
        run_native(
            binary=authority_binary,
            input_path=input_path,
            output_root=authority_run,
        )
        _attach_binary(authority_run, authority_binary)

        capture_frozen = freeze_source(
            source_root=source_root,
            expected_commit=source_commit,
            source_subdir=source_subdir,
            input_path=input_path,
            expected_input_sha256=input_sha256,
            destination=work / "capture-source",
        )
        capture_prepared = prepare_native_source(
            frozen=capture_frozen, capture_enabled=True
        )
        capture_binary = build_native(
            prepared=capture_prepared,
            output=work / "bin/capture-mcf",
            compiler=compiler,
        )
        primary_run = work / "capture-primary-run"
        replay_run = work / "capture-replay-run"
        for run_root in (primary_run, replay_run):
            run_native(
                binary=capture_binary,
                input_path=input_path,
                output_root=run_root,
            )
            _attach_binary(run_root, capture_binary)
        identity = {
            "source_commit": capture_prepared["source_commit"],
            "source_tree_sha256": capture_prepared["source_tree_sha256"],
            "input_sha256": input_sha256,
            "common_patch_sha256": capture_prepared[
                "common_patch_sha256"
            ],
            "capture_patch_sha256": capture_prepared[
                "capture_patch_sha256"
            ],
            "compiler_sha256": readiness["compiler"]["sha256"],
        }
        source_record = work / "source.json"
        _atomic_json(source_record, _source_record(capture_frozen))
        result = generate_candidate(
            authority_root=authority_run,
            capture_primary_root=primary_run,
            capture_replay_root=replay_run,
            identity=identity,
            evidence_root=output_root,
            source_record=source_record,
        )
        shutil.rmtree(work)
        return {**result, "status": "accepted", "preflight": readiness}
    except Exception as error:
        failure = {
            "schema": 1,
            "status": "failed_input",
            "first_failed_gate": "generation",
            "error": str(error),
            "work_root": str(work),
        }
        _atomic_json(output_root / "failed-input.json", failure)
        if isinstance(error, GenerationError):
            raise
        raise GenerationError(f"generation: {error}") from error


def validate_candidate(path):
    try:
        from scripts import freeze_cross_system_inputs as freezer
    except ImportError:
        import freeze_cross_system_inputs as freezer
    candidate = _read_json(path, "MCF candidate record")
    if (
        candidate.get("schema") != 1
        or candidate.get("status") != "candidate"
        or candidate.get("workload") != "mcf"
    ):
        raise GenerationError("MCF candidate record identity differs")
    try:
        freezer.validate_mcf_record(candidate.get("record"))
    except freezer.InputError as error:
        raise GenerationError(f"MCF candidate qualification failed: {error}") from error
    return candidate["record"]


def _verify_published_tree(root, rows, label):
    if not isinstance(rows, list):
        raise GenerationError(f"{label} artifact manifest is invalid")
    actual = _tree_hashes(root)
    if actual != rows:
        raise GenerationError(f"{label} artifact tree differs")


def verify_accepted(output_root):
    output_root = Path(output_root).resolve()
    roots = [
        path for path in output_root.iterdir()
        if path.is_dir()
        and len(path.name) == 64
        and all(character in "0123456789abcdef" for character in path.name)
        and (path / "mcf.reg2").is_file()
    ]
    if len(roots) != 1:
        raise GenerationError("accepted MCFREG2 root count differs")
    root = roots[0]
    package_sha256 = _sha256_file(root / "mcf.reg2")
    if root.name != package_sha256:
        raise GenerationError("accepted root/package SHA-256 differs")
    try:
        mcfreg2.read_package(root / "mcf.reg2")
    except mcfreg2.FormatError as error:
        raise GenerationError(f"accepted MCFREG2 package is invalid: {error}") from error
    validation = _read_json(root / "validation.json", "MCF validation")
    manifest = _read_json(root / "manifest.json", "MCF manifest")
    if validation.get("schema") != 2 or validation.get("status") != "accepted":
        raise GenerationError("accepted MCF validation identity differs")
    if validation.get("package_sha256") != package_sha256:
        raise GenerationError("accepted MCF validation package differs")
    if manifest.get("identity") != validation.get("identity"):
        raise GenerationError("accepted MCF manifest identity differs")
    validate_candidate(root / "candidate-record.json")
    published = manifest.get("published_runs", {})
    _verify_published_tree(
        root / "authority", published.get("authority"), "authority"
    )
    _verify_published_tree(
        root / "capture-primary",
        published.get("capture_primary"),
        "capture-primary",
    )
    _verify_published_tree(
        root / "capture-replay",
        published.get("capture_replay"),
        "capture-replay",
    )
    for directory, prefix in (
        ("authority", "authority"),
        ("capture-primary", "capture_primary"),
        ("capture-replay", "capture_replay"),
    ):
        if _sha256_file(root / directory / "final.state") != validation.get(
            f"{prefix}_final_state_sha256"
        ):
            raise GenerationError(f"{directory} final state differs")
        if _sha256_file(root / directory / "mcf.out") != validation.get(
            f"{prefix}_mcf_output_sha256"
        ):
            raise GenerationError(f"{directory} mcf.out differs")
    replay_root = Path(
        tempfile.mkdtemp(prefix=".verify-", dir=output_root)
    )
    try:
        replay = _run_independent_replay(root / "mcf.reg2", replay_root)
        if (
            replay.get("boundary_mismatches") != 0
            or replay.get("trace_sha256")
            != validation.get("canonical_trace_sha256")
        ):
            raise GenerationError("independent verification replay differs")
    finally:
        shutil.rmtree(replay_root, ignore_errors=True)
    return {
        "schema": 1,
        "status": "verified",
        "accepted_root": str(root),
        "package_sha256": package_sha256,
        "validation_sha256": _sha256_file(root / "validation.json"),
        "manifest_sha256": _sha256_file(root / "manifest.json"),
        "peak_allocated_bytes": validation["peak_allocated_bytes"],
        "pricing_calls": validation["pricing_calls"],
        "price_out_calls": validation["price_out_calls"],
        "event_count": validation["event_count"],
        "boundary_mismatches": validation["boundary_mismatches"],
    }


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    def source_arguments(command):
        command.add_argument("--source-root", type=Path, required=True)
        command.add_argument("--source-commit", required=True)
        command.add_argument("--source-subdir", required=True)
        command.add_argument("--input", type=Path, required=True)
        command.add_argument("--input-sha256", required=True)
        command.add_argument("--output-root", type=Path, required=True)
        command.add_argument("--compiler", default="cc")

    source_arguments(subparsers.add_parser("preflight"))
    source_arguments(subparsers.add_parser("generate"))
    verify = subparsers.add_parser("verify")
    verify.add_argument("--output-root", type=Path, required=True)
    verify.add_argument("--accepted", action="store_true", required=True)
    candidate = subparsers.add_parser("validate-candidate")
    candidate.add_argument("--candidate", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv=None):
    options = parse_args(argv)
    try:
        if options.command in ("preflight", "generate"):
            arguments = {
                "source_root": options.source_root,
                "source_commit": options.source_commit,
                "source_subdir": options.source_subdir,
                "input_path": options.input,
                "input_sha256": options.input_sha256,
                "output_root": options.output_root,
                "compiler": options.compiler,
            }
            result = (
                preflight(**arguments)
                if options.command == "preflight"
                else generate_evidence(**arguments)
            )
            print(_canonical_json(result).decode("ascii"), end="")
        elif options.command == "verify":
            print(
                _canonical_json(verify_accepted(options.output_root)).decode(
                    "ascii"
                ),
                end="",
            )
        else:
            record = validate_candidate(options.candidate)
            print(
                "MCF_CANDIDATE=validated "
                f"package_sha256={record['input_sha256']}"
            )
        return 0
    except (GenerationError, OSError) as error:
        print(f"MCFREG2_FAILED error={error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
