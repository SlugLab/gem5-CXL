#!/usr/bin/env python3
"""Fail-closed live process checkpoint orchestration for long simulations."""

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


class CheckpointError(RuntimeError):
    """Raised when live checkpoint evidence is incomplete or inconsistent."""


@dataclass(frozen=True)
class CaptureJob:
    name: str
    unit: str
    root_pid: int
    snapshot_root: Path

    @property
    def staging_root(self):
        return Path(self.snapshot_root) / (
            f".{self.name}.{self.root_pid}.capture"
        )

    @property
    def image_dir(self):
        return self.staging_root / "images"

    @property
    def work_dir(self):
        return self.staging_root / "work"

    @property
    def log_path(self):
        return self.work_dir / "dump.log"

    @property
    def final_root(self):
        return Path(self.snapshot_root) / self.name

    @property
    def final_image_dir(self):
        return self.final_root / "images"

    @property
    def manifest_path(self):
        return self.final_root / "manifest.json"


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _file_record(path):
    path = Path(path).resolve()
    return {
        "path": str(path),
        "size": path.stat().st_size,
        "sha256": sha256_file(path),
    }


def build_manifest(
    *,
    name,
    unit,
    root_pid,
    process_tree,
    inputs,
    image_dir,
    progress,
    host,
):
    if not process_tree:
        raise CheckpointError("process tree is empty")
    image_dir = Path(image_dir).resolve()
    images = {
        path.name: _file_record(path)
        for path in sorted(image_dir.iterdir())
        if path.is_file()
    }
    if not images:
        raise CheckpointError("checkpoint image directory is empty")
    return {
        "schema": 1,
        "name": name,
        "unit": unit,
        "root_pid": int(root_pid),
        "process_tree": process_tree,
        "inputs": {
            str(Path(path).resolve()): _file_record(path)
            for path in inputs
        },
        "image_dir": str(image_dir),
        "images": images,
        "progress": progress,
        "host": host,
    }


def _validate_record(record, *, kind):
    path = Path(record["path"])
    if not path.is_file():
        raise CheckpointError(f"missing {kind}: {path}")
    if path.stat().st_size != record["size"]:
        raise CheckpointError(f"{kind} size mismatch: {path}")
    if sha256_file(path) != record["sha256"]:
        raise CheckpointError(f"{kind} hash mismatch: {path}")


def validate_manifest(manifest, *, manifest_path, require_same_kernel):
    if manifest.get("schema") != 1:
        raise CheckpointError("unsupported checkpoint manifest schema")
    if not manifest.get("process_tree"):
        raise CheckpointError("process tree is empty")
    if require_same_kernel:
        captured = manifest.get("host", {}).get("kernel_release")
        current = platform.release()
        if captured != current:
            raise CheckpointError(
                f"kernel release mismatch: captured={captured} current={current}"
            )
    for record in manifest.get("inputs", {}).values():
        _validate_record(record, kind="checkpoint input")
    for record in manifest.get("images", {}).values():
        _validate_record(record, kind="checkpoint image")
    return manifest


def load_json(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise CheckpointError(f"cannot load JSON {path}: {error}") from error


def atomic_write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    os.replace(temporary, path)


def criu_dump_command(job, *, probe):
    command = [
        "criu",
        "dump",
        "--tree",
        str(job.root_pid),
        "--images-dir",
        str(job.image_dir),
        "--work-dir",
        str(job.work_dir),
        "--log-file",
        "dump.log",
        "--shell-job",
        "--file-locks",
        "--manage-cgroups=ignore",
    ]
    if probe:
        command.append("--leave-running")
    return command


def validate_preflight(
    *, criu_path, crit_path, free_bytes, root_pids_alive
):
    if not criu_path:
        raise CheckpointError("CRIU executable is missing")
    if not crit_path:
        raise CheckpointError("crit executable is missing")
    if free_bytes < 32 * 1024**3:
        raise CheckpointError(
            "checkpoint filesystem requires at least 32 GiB free"
        )
    for name, alive in root_pids_alive.items():
        if not alive:
            raise CheckpointError(f"root process is not alive: {name}")


def run_criu_dump(job, *, probe, runner=subprocess.run):
    if job.staging_root.exists():
        shutil.rmtree(job.staging_root)
    job.image_dir.mkdir(parents=True)
    job.work_dir.mkdir(parents=True)
    command = criu_dump_command(job, probe=probe)
    result = runner(
        command,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode:
        raise CheckpointError(
            f"CRIU dump failed with status {result.returncode}: "
            f"{result.stderr.strip()}"
        )
    return job.staging_root


def _read_proc_entry(pid, proc_root):
    proc_dir = Path(proc_root) / str(pid)
    stat = (proc_dir / "stat").read_text()
    close = stat.rfind(")")
    if close < 0:
        raise CheckpointError(f"invalid process stat for PID {pid}")
    fields = stat[close + 2 :].split()
    ppid = int(fields[1])
    raw_cmdline = (proc_dir / "cmdline").read_bytes()
    cmdline = [
        item.decode("utf-8", errors="surrogateescape")
        for item in raw_cmdline.split(b"\0")
        if item
    ]
    return {
        "pid": int(pid),
        "ppid": ppid,
        "cmdline": cmdline,
    }


def capture_process_tree(root_pid, proc_root="/proc"):
    root_pid = int(root_pid)
    entries = {}
    for proc_dir in Path(proc_root).iterdir():
        if not proc_dir.name.isdigit():
            continue
        try:
            entry = _read_proc_entry(int(proc_dir.name), proc_root)
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            continue
        entries[entry["pid"]] = entry
    if root_pid not in entries:
        raise CheckpointError(f"root process is not alive: PID {root_pid}")
    selected = {root_pid}
    changed = True
    while changed:
        changed = False
        for pid, entry in entries.items():
            if pid not in selected and entry["ppid"] in selected:
                selected.add(pid)
                changed = True
    return [entries[pid] for pid in sorted(selected)]


def _validate_criu_image_set(image_dir):
    image_dir = Path(image_dir)
    for name in ("inventory.img", "pstree.img"):
        path = image_dir / name
        if not path.is_file() or path.stat().st_size == 0:
            raise CheckpointError(f"missing CRIU image: {path}")


def capture_job(
    job,
    *,
    probe,
    inputs,
    progress,
    host,
    process_tree,
    runner=subprocess.run,
):
    if job.final_root.exists():
        raise CheckpointError(
            f"published checkpoint already exists: {job.final_root}"
        )
    run_criu_dump(job, probe=probe, runner=runner)
    _validate_criu_image_set(job.image_dir)
    if probe:
        shutil.rmtree(job.staging_root)
        return {"probe": "passed"}
    os.replace(job.staging_root, job.final_root)
    manifest = build_manifest(
        name=job.name,
        unit=job.unit,
        root_pid=job.root_pid,
        process_tree=process_tree,
        inputs=inputs,
        image_dir=job.final_image_dir,
        progress=progress,
        host=host,
    )
    atomic_write_json(job.manifest_path, manifest)
    return manifest


def resolve_main_pid(unit, *, runner=subprocess.run):
    command = [
        "systemctl",
        "show",
        "--property",
        "MainPID",
        "--value",
        unit,
    ]
    result = runner(
        command,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode:
        raise CheckpointError(
            f"cannot resolve MainPID for {unit}: {result.stderr.strip()}"
        )
    try:
        pid = int(result.stdout.strip())
    except ValueError as error:
        raise CheckpointError(f"invalid MainPID for {unit}") from error
    if pid <= 0:
        raise CheckpointError(f"unit has no live MainPID: {unit}")
    return pid


def build_parser():
    parser = argparse.ArgumentParser(
        description="Checkpoint and restore the live AMU and M2NDP jobs."
    )
    subparsers = parser.add_subparsers(dest="action", required=True)
    for action in ("preflight", "validate"):
        command = subparsers.add_parser(action)
        command.add_argument("--root", type=Path, required=True)
        if action == "validate":
            command.add_argument("--job", choices=("amu", "m2ndp"))
    for action in ("probe", "dump"):
        command = subparsers.add_parser(action)
        command.add_argument("--root", type=Path, required=True)
        command.add_argument(
            "--job", choices=("amu", "m2ndp"), required=True
        )
    return parser


def validate_transaction(
    transaction, *, require_ready, require_same_kernel
):
    if transaction.get("schema") != 1:
        raise CheckpointError("unsupported checkpoint transaction schema")
    if require_ready and transaction.get("state") != "ready_for_reboot":
        raise CheckpointError("transaction is not ready for reboot")
    workloads = transaction.get("workloads", {})
    for name in ("amu", "m2ndp"):
        if name not in workloads:
            raise CheckpointError(f"transaction is missing workload {name}")
        manifest_path = Path(workloads[name])
        manifest = load_json(manifest_path)
        if manifest.get("name") != name:
            raise CheckpointError(
                f"transaction workload name mismatch for {name}"
            )
        validate_manifest(
            manifest,
            manifest_path=manifest_path,
            require_same_kernel=require_same_kernel,
        )
    return transaction
