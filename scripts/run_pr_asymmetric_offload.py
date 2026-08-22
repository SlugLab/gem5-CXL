#!/usr/bin/env python3
# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

"""Run the immutable g12/g14/g20 asymmetric PR offload campaign."""

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

try:
    from scripts import pr_offload_contract as contract
except ImportError:
    import pr_offload_contract as contract


REPO = Path(__file__).resolve().parents[1]
PROFILE = contract.FORMAL_PROFILE
OffloadError = contract.OffloadError


def sha256_file(path):
    digest = hashlib.sha256()
    try:
        with Path(path).open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as error:
        raise OffloadError(f"cannot hash {path}: {error}") from error
    return digest.hexdigest()


def hash_tree(path):
    path = Path(path).resolve()
    if path.is_file():
        return sha256_file(path)
    if not path.is_dir():
        raise OffloadError(f"identity path is missing: {path}")
    digest = hashlib.sha256()
    files = sorted(candidate for candidate in path.rglob("*") if candidate.is_file())
    if not files:
        raise OffloadError(f"identity tree is empty: {path}")
    for candidate in files:
        relative = candidate.relative_to(path).as_posix().encode()
        digest.update(len(relative).to_bytes(8, "little"))
        digest.update(relative)
        digest.update(bytes.fromhex(sha256_file(candidate)))
    return digest.hexdigest()


def atomic_write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _load_json(path, label):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise OffloadError(f"invalid {label}: {error}") from error
    if not isinstance(value, dict):
        raise OffloadError(f"{label} must be an object")
    return value


def select_inputs(options):
    source = _load_json(options.inputs, "source input manifest")
    graphs = source.get("graphs")
    if not isinstance(graphs, list):
        raise OffloadError("source input graphs are missing")
    selected = [row for row in graphs if row.get("scale") in contract.SCALES]
    if [row.get("scale") for row in selected] != list(contract.SCALES):
        raise OffloadError("formal graphs must be ordered g12,g14,g20")
    for row in selected:
        required = {"scale", "path", "sha256", "manifest", "manifest_sha256"}
        if not isinstance(row, dict) or not required.issubset(row):
            raise OffloadError("formal graph row is incomplete")
        if sha256_file(row["path"]) != row["sha256"]:
            raise OffloadError(f"g{row['scale']} graph hash changed")
        if sha256_file(row["manifest"]) != row["manifest_sha256"]:
            raise OffloadError(f"g{row['scale']} manifest hash changed")
    normalized = {
        "schema": 1,
        "profile": PROFILE,
        "source_inputs_sha256": sha256_file(options.inputs),
        "graphs": selected,
    }
    path = Path(options.root).resolve() / "selected-inputs.json"
    if path.exists():
        if _load_json(path, "selected input manifest") != normalized:
            raise OffloadError("selected input manifest differs")
    else:
        atomic_write_json(path, normalized)
    return normalized


def _hash_graph_set(selected):
    digest = hashlib.sha256()
    for row in selected["graphs"]:
        digest.update(str(row["scale"]).encode())
        digest.update(bytes.fromhex(row["sha256"]))
        digest.update(bytes.fromhex(row["manifest_sha256"]))
    return digest.hexdigest()


def _git_head(path):
    try:
        return subprocess.check_output(
            ["git", "-C", str(Path(path).resolve()), "rev-parse", "HEAD"],
            text=True,
            stderr=subprocess.STDOUT,
        ).strip()
    except (OSError, subprocess.CalledProcessError) as error:
        raise OffloadError(f"cannot resolve M2NDP commit: {error}") from error


def build_identity(options, selected):
    selected_path = Path(options.root).resolve() / "selected-inputs.json"
    source_files = (
        REPO / "util/pr_offload",
        REPO / "util/amu",
        REPO / "util/cira",
        REPO / "scripts/pr_offload_contract.py",
        Path(__file__).resolve(),
    )
    source_digest = hashlib.sha256()
    for path in source_files:
        source_digest.update(bytes.fromhex(hash_tree(path)))
    m2ndp_commit = getattr(options, "m2ndp_commit", None) or _git_head(
        options.m2ndp_root
    )
    identity = {
        "source_sha256": source_digest.hexdigest(),
        "gem5_sha256": sha256_file(options.gem5),
        "libm5_sha256": sha256_file(options.m5_library),
        "graph_set_sha256": _hash_graph_set(selected),
        "workload_binaries_sha256": hash_tree(options.variants_build_root),
        "m2ndp_commit": m2ndp_commit,
        "m2ndp_patches_sha256": hash_tree(REPO / "util/m2ndp/patches"),
        "m2ndp_config_sha256": sha256_file(options.config),
        "calibration_sha256": sha256_file(options.calibration),
        "policy_sha256": sha256_file(options.policy),
        "source_inputs_sha256": sha256_file(options.inputs),
        "selected_inputs_sha256": sha256_file(selected_path),
    }
    return contract.validate_identity(identity)


def build_matrix():
    primary, ablations = contract.build_matrix()
    return primary + ablations


def point_root(root, entry):
    root = Path(root).resolve()
    if entry.stage == "qualification":
        return root / "qualification" / entry.replica / entry.system
    if entry.stage == "ablation":
        return root / "ablation" / f"g{entry.scale}" / entry.system
    return root / "formal" / f"g{entry.scale}" / entry.system


def require_resume_identity(saved, live):
    return contract.require_resume_identity(saved, live)


def _graph_for(entry, options):
    selected = _load_json(
        Path(options.root).resolve() / "selected-inputs.json",
        "selected input manifest",
    )
    try:
        return next(row for row in selected["graphs"] if row["scale"] == entry.scale)
    except (KeyError, StopIteration) as error:
        raise OffloadError(f"selected g{entry.scale} graph is missing") from error


def _cira_mode(system):
    return {
        "cira-few-shot": ("few-shot-online", "B"),
        "cira-static": ("static", "A"),
        "cira-pgo": ("pgo-selected", "B"),
        "cira-A": ("pgo-selected", "A"),
        "cira-B": ("pgo-selected", "B"),
        "cira-C": ("pgo-selected", "C"),
    }[system]


def command_for(entry, options):
    graph = _graph_for(entry, options)
    root = point_root(options.root, entry)
    common = [
        "--graph", str(Path(graph["path"]).resolve()),
        "--graph-scale", str(entry.scale),
        "--profile", PROFILE,
        "--graph-manifest", str(Path(graph["manifest"]).resolve()),
        "--cxl-link-delay", "1us",
        "--gem5", str(Path(options.gem5).resolve()),
    ]
    if entry.system in {"vanilla", "m2ndp"}:
        command = [
            sys.executable,
            str(REPO / "scripts/run_m2ndp_g20_pr_spmv.py"),
            *common,
            "--cxlmemuring", str(Path(options.cxlmemuring).resolve()),
            "--m2ndp-root", str(Path(options.m2ndp_root).resolve()),
            "--m5-library", str(Path(options.m5_library).resolve()),
            "--outdir", str(root),
        ]
        if entry.system == "vanilla":
            command.extend(("--stop-after", "gem5_baseline"))
        return command
    kind = "amu" if entry.system == "amu" else "cira"
    build_root = Path(options.variants_build_root).resolve() / f"g{entry.scale}" / entry.system
    command = [
        sys.executable,
        str(REPO / "scripts/run_gapbs_matched_pr_spmv_variants.py"),
        *common,
        "--config", str(Path(options.config).resolve()),
        "--variants-build", str(build_root),
        "--kind", kind,
        "--checkpoint-root", str(root / "checkpoints"),
        "--outdir", str(root),
        "--asmc-profile", "paper-calibrated",
        "--asmc-calibration-manifest", str(Path(options.calibration).resolve()),
    ]
    if kind == "cira":
        mode, source = _cira_mode(entry.system)
        command.extend(("--cira-mode", mode, "--cira-source-row", source))
    return command


def new_state(identity):
    return {
        "schema": 1,
        "status": "in_progress",
        "identity": contract.validate_identity(identity),
        "points": {},
    }


def record_pass(state, entry, artifacts):
    if not isinstance(artifacts, dict) or not artifacts:
        raise OffloadError("passed point artifact set is empty")
    prior = state["points"].get(entry.key, {})
    state["points"][entry.key] = {
        **prior,
        "status": "passed",
        "stage": entry.stage,
        "replica": entry.replica,
        "artifacts": dict(artifacts),
    }


def artifact_paths(entry, options):
    root = point_root(options.root, entry)
    if entry.system == "vanilla":
        return (root / "gem5/run/summary.csv", root / "reference/scores.raw")
    return (root / "summary.csv", root / "evidence.json")


def _capture_artifacts(entry, options):
    paths = artifact_paths(entry, options)
    missing = [str(path) for path in paths if not path.is_file()]
    if missing:
        raise OffloadError(
            f"point {entry.key} is missing artifacts: {', '.join(missing)}"
        )
    return {str(path.resolve()): sha256_file(path) for path in paths}


def validate_resume(state, identity):
    if not isinstance(state, dict) or state.get("schema") != 1:
        raise OffloadError("campaign state schema differs")
    require_resume_identity(state.get("identity"), identity)
    for point in state.get("points", {}).values():
        if point.get("status") != "passed":
            continue
        for path, expected in point.get("artifacts", {}).items():
            if sha256_file(path) != expected:
                raise OffloadError(f"passed artifact changed: {path}")
    return state


def run_campaign(options, *, executor=subprocess.run):
    root = Path(options.root).resolve()
    selected = select_inputs(options)
    identity = build_identity(options, selected)
    state_path = root / "campaign-state.json"
    if state_path.exists():
        if not options.resume:
            raise OffloadError(f"campaign state exists; use --resume: {state_path}")
        state = validate_resume(
            _load_json(state_path, "campaign state"), identity
        )
    else:
        if options.resume:
            raise OffloadError("--resume requested but campaign state is missing")
        state = new_state(identity)
        atomic_write_json(state_path, state)

    for entry in build_matrix():
        if options.stop_after == "qualification" and entry.scale != 12:
            break
        saved = state["points"].get(entry.key)
        if saved is not None and saved.get("status") == "passed":
            continue
        command = command_for(entry, options)
        state["points"][entry.key] = {
            "status": "running",
            "stage": entry.stage,
            "replica": entry.replica,
            "command": command,
            "command_sha256": hashlib.sha256(
                json.dumps(command, separators=(",", ":")).encode()
            ).hexdigest(),
            "identity": identity,
            "artifacts": {},
        }
        atomic_write_json(state_path, state)
        completed = executor(command, check=False)
        if completed.returncode != 0:
            state["points"][entry.key]["status"] = "failed"
            state["points"][entry.key]["returncode"] = completed.returncode
            atomic_write_json(state_path, state)
            raise OffloadError(
                f"point {entry.key} failed with status {completed.returncode}"
            )
        artifacts = _capture_artifacts(entry, options)
        record_pass(state, entry, artifacts)
        state["points"][entry.key]["command"] = command
        state["points"][entry.key]["returncode"] = 0
        atomic_write_json(state_path, state)
    return state


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inputs", type=Path, required=True)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--gem5", type=Path, required=True)
    parser.add_argument("--m5-library", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--calibration", type=Path, required=True)
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--cxlmemuring", type=Path, required=True)
    parser.add_argument("--m2ndp-root", type=Path, required=True)
    parser.add_argument("--variants-build-root", type=Path, required=True)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--stop-after", choices=("qualification",))
    return parser.parse_args(argv)


def main(argv=None):
    options = parse_args(argv)
    try:
        run_campaign(options)
    except OffloadError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
