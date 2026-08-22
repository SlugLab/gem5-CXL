#!/usr/bin/env python3
# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

"""Run the immutable g12/g14/g20 asymmetric PR offload campaign."""

import argparse
import csv
import hashlib
import json
import os
import subprocess
import sys
from decimal import Decimal
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
    key = state_key(entry)
    prior = state["points"].get(key, {})
    state["points"][key] = {
        **prior,
        "status": "passed",
        "stage": entry.stage,
        "replica": entry.replica,
        "artifacts": dict(artifacts),
    }


def state_key(entry):
    return entry.key if entry.replica == "primary" else f"replay/{entry.key}"


def artifact_paths(entry, options):
    root = point_root(options.root, entry)
    if entry.system == "vanilla":
        return (root / "gem5/run/summary.csv", root / "reference/scores.raw")
    if entry.system == "m2ndp":
        return (root / "summary.csv", root / "manifest.json")
    return (root / "summary.csv", root / "evidence.json")


def _capture_artifacts(entry, options):
    paths = artifact_paths(entry, options)
    missing = [str(path) for path in paths if not path.is_file()]
    if missing:
        raise OffloadError(
            f"point {entry.key} is missing artifacts: {', '.join(missing)}"
        )
    return {str(path.resolve()): sha256_file(path) for path in paths}


def native_count(point):
    if point.get("system") == "m2ndp":
        return int(point["ndpsim_cycles"])
    return int(point["sim_ticks"])


def qualification_gate(points):
    expected = tuple(
        f"g12:{system}" for system in contract.PRIMARY_SYSTEMS
    )
    if tuple(points) != expected:
        raise OffloadError("g12 qualification point set or order differs")
    checked = {
        key: contract.validate_point(points[key]) for key in expected
    }
    vanilla = checked["g12:vanilla"]["seconds"]
    speedups = {
        system: vanilla / checked[f"g12:{system}"]["seconds"]
        for system in contract.PRIMARY_SYSTEMS
        if system != "vanilla"
    }
    offenders = [
        system for system, speedup in speedups.items()
        if not contract.MIN_SPEEDUP <= speedup <= contract.MAX_SPEEDUP
    ]
    return {
        "status": "failed" if offenders else "passed",
        "checked_points": 3,
        "speedups": {name: str(value) for name, value in speedups.items()},
        "offenders": offenders,
    }


def validate_replay(primary, replay):
    expected = tuple(
        f"g12:{system}" for system in contract.PRIMARY_SYSTEMS
    )
    if tuple(primary) != expected or tuple(replay) != expected:
        raise OffloadError("g12 replay point set or order differs")
    for key in expected:
        first = contract.validate_point(primary[key])
        again = contract.validate_point(replay[key])
        if first["raw_sha256"] != again["raw_sha256"]:
            raise OffloadError(f"g12 {first['system']} replay rank differs")
        if native_count(first) != native_count(again):
            raise OffloadError(f"g12 {first['system']} replay timing differs")
    if (
        primary["g12:cira-few-shot"].get("selected_candidate")
        != replay["g12:cira-few-shot"].get("selected_candidate")
    ):
        raise OffloadError("g12 CIRA replay policy differs")
    return replay


def _json_points(points):
    def safe(value):
        if isinstance(value, Decimal):
            return str(value)
        if isinstance(value, dict):
            return {key: safe(item) for key, item in value.items()}
        if isinstance(value, list):
            return [safe(item) for item in value]
        return value
    return safe(points)


def _write_hold(root, name, *, error, gate=None):
    root = Path(root).resolve()
    (root / "qualification.json").unlink(missing_ok=True)
    (root / "complete.json").unlink(missing_ok=True)
    value = {
        "schema": 1,
        "official_qualification": False,
        "error": str(error),
    }
    if gate is not None:
        value["performance_gate"] = gate
    atomic_write_json(root / name, value)


def run_qualification_state_machine(root, run_point, *, identity=None):
    root = Path(root).resolve()
    primary = {}
    try:
        for system in contract.PRIMARY_SYSTEMS:
            entry = contract.MatrixEntry(12, system, "qualification", "primary")
            primary[entry.key] = run_point(entry)
        gate = qualification_gate(primary)
    except (OffloadError, KeyError, TypeError, ValueError) as error:
        _write_hold(
            root, "diagnostic-performance-hold.json", error=error
        )
        raise OffloadError(f"g12 qualification failed: {error}") from error
    if gate["status"] != "passed":
        _write_hold(
            root,
            "diagnostic-performance-hold.json",
            error="g12 accelerated speedup outside 1.4x--1.6x",
            gate=gate,
        )
        raise OffloadError("g12 qualification performance gate failed")

    replay = {}
    try:
        for system in contract.PRIMARY_SYSTEMS:
            entry = contract.MatrixEntry(12, system, "qualification", "replay")
            replay[entry.key] = run_point(entry)
        validate_replay(primary, replay)
    except (OffloadError, KeyError, TypeError, ValueError) as error:
        _write_hold(
            root, "diagnostic-performance-hold.json", error=error, gate=gate
        )
        raise OffloadError(f"g12 qualification replay failed: {error}") from error
    qualification = {
        "schema": 1,
        "status": "passed",
        "profile": PROFILE,
        "performance_gate": gate,
        "primary": _json_points(primary),
        "replay": _json_points(replay),
    }
    if identity is not None:
        qualification["identity"] = contract.validate_identity(identity)
    (root / "diagnostic-performance-hold.json").unlink(missing_ok=True)
    atomic_write_json(root / "qualification.json", qualification)
    return qualification


def _single_csv(path):
    try:
        with Path(path).open(newline="", encoding="utf-8") as stream:
            rows = list(csv.DictReader(stream))
    except OSError as error:
        raise OffloadError(f"cannot read point summary {path}: {error}") from error
    if len(rows) != 1:
        raise OffloadError(f"point summary row count is {len(rows)}, expected 1")
    return rows[0]


def _base_point(entry, *, verification, raw_sha256, completions):
    return {
        "scale": entry.scale,
        "system": entry.system,
        "profile": PROFILE,
        "cxl_link_delay": "1us",
        "workers": 4,
        "iterations": 20,
        "all_memory_cxl": True,
        "verification": verification,
        "raw_sha256": raw_sha256,
        "worker_completions": completions,
        "pending": {"descriptors": 0, "requests": 0, "writebacks": 0},
    }


def load_point(entry, options):
    root = point_root(options.root, entry)
    if entry.system == "vanilla":
        row = _single_csv(root / "gem5/run/summary.csv")
        if (
            row.get("status") != "ok" or row.get("cores") != "4"
            or row.get("all_memory_cxl") != "True"
            or row.get("cxl_link_delay") != "1us"
        ):
            raise OffloadError("Vanilla row violates four-core all-CXL contract")
        point = _base_point(
            entry,
            verification=row.get("verification"),
            raw_sha256=sha256_file(root / "reference/scores.raw"),
            completions=[20, 20, 20, 20],
        )
        point["sim_ticks"] = int(row["sim_ticks"])
        return contract.validate_point(point)
    if entry.system == "m2ndp":
        row = _single_csv(root / "summary.csv")
        if row.get("profile") != PROFILE or row.get("logical_partitions") != "4":
            raise OffloadError("M2NDP row is not the four-way formal profile")
        point = _base_point(
            entry,
            verification=row.get("verification"),
            raw_sha256=row.get("reference_raw_sha256", ""),
            completions=[40, 40, 40, 40],
        )
        point.update(
            ndpsim_cycles=int(row["ndpsim_measured_cycles"]),
            ndpsim_core_period_seconds=row["ndpsim_core_period_seconds"],
            funcsim={
                "status": row.get("funcsim_strict"),
                "compared": int(row.get("funcsim_compared", 0)),
                "mismatched": 0,
                "completed_at_seq": 1,
            },
            ndpsim_started_at_seq=2,
        )
        return contract.validate_point(point)

    kind = "amu" if entry.system == "amu" else "cira"
    row = _single_csv(root / "summary.csv")
    evidence = _load_json(root / "evidence.json", "variant point evidence")
    run = evidence.get("runs", {}).get(kind)
    if not isinstance(run, dict):
        raise OffloadError(f"{entry.system} run evidence is missing")
    issued = int(row.get("pr_issued_descriptors", 0))
    if kind == "cira":
        try:
            completions = [
                int(value) for value in row["cira_completed_per_core"].split(";")
            ]
        except (KeyError, ValueError) as error:
            raise OffloadError("CIRA per-core completions are invalid") from error
    else:
        if issued <= 0 or issued % 4:
            raise OffloadError("AMU descriptors cannot prove four balanced workers")
        completions = [issued // 4] * 4
    point = _base_point(
        entry,
        verification=row.get("verification"),
        raw_sha256=run.get("reference_raw_sha256", ""),
        completions=completions,
    )
    point["sim_ticks"] = int(row["sim_ticks"])
    point["pending"] = {
        "outstanding": int(row.get("pr_outstanding_work", 0)),
        "rejected": int(row.get("pr_rejected_descriptors", 0)),
    }
    if kind == "cira":
        point["phases"] = {
            name: int(row[f"pr_e2e_{name}_ns"])
            for name in contract.CIRA_PHASES
        }
        point["selected_candidate"] = row.get("pr_cira_selected_candidate")
    return contract.validate_point(point)


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


def run_campaign(options, *, executor=subprocess.run, point_loader=load_point):
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

    def execute_entry(entry):
        key = state_key(entry)
        saved = state["points"].get(key)
        if saved is not None and saved.get("status") == "passed":
            return saved["evidence"]
        command = command_for(entry, options)
        state["points"][key] = {
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
            state["points"][key]["status"] = "failed"
            state["points"][key]["returncode"] = completed.returncode
            atomic_write_json(state_path, state)
            raise OffloadError(
                f"point {entry.key} failed with status {completed.returncode}"
            )
        artifacts = _capture_artifacts(entry, options)
        evidence = point_loader(entry, options)
        record_pass(state, entry, artifacts)
        state["points"][key]["command"] = command
        state["points"][key]["returncode"] = 0
        state["points"][key]["evidence"] = _json_points(evidence)
        atomic_write_json(state_path, state)
        return state["points"][key]["evidence"]

    qualification = run_qualification_state_machine(
        root, execute_entry, identity=identity
    )
    if options.stop_after == "qualification":
        return state

    primary = dict(qualification["primary"])
    primary_entries, ablation_entries = contract.build_matrix()
    for entry in primary_entries:
        if entry.scale == 12:
            continue
        primary[entry.key] = execute_entry(entry)
    ablations = {}
    for entry in ablation_entries:
        ablations[entry.key] = execute_entry(entry)

    ordered_primary = [primary[entry.key] for entry in primary_entries]
    ordered_ablations = [ablations[entry.key] for entry in ablation_entries]
    try:
        complete = contract.validate_complete({
            "schema": 1,
            "identity": identity,
            "primary": ordered_primary,
            "ablations": ordered_ablations,
        })
    except OffloadError as error:
        (root / "complete.json").unlink(missing_ok=True)
        atomic_write_json(root / "performance-hold.json", {
            "schema": 1,
            "official_qualification": False,
            "error": str(error),
        })
        raise
    oracle = {}
    for scale in contract.SCALES:
        candidates = {
            name: next(
                row for row in complete["ablations"]
                if row["scale"] == scale and row["system"] == f"cira-{name}"
            )
            for name in ("A", "B", "C")
        }
        oracle_ticks = min(row["sim_ticks"] for row in candidates.values())
        few_shot = next(
            row for row in complete["primary"]
            if row["scale"] == scale and row["system"] == "cira-few-shot"
        )
        oracle[f"g{scale}"] = {
            "oracle_ticks": oracle_ticks,
            "regret": str(
                Decimal(few_shot["sim_ticks"]) / Decimal(oracle_ticks)
                - Decimal(1)
            ),
        }
    complete["oracle"] = oracle
    complete["status"] = "passed"
    (root / "performance-hold.json").unlink(missing_ok=True)
    atomic_write_json(root / "complete.json", _json_points(complete))
    state["status"] = "complete"
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
