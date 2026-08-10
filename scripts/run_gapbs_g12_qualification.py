#!/usr/bin/env python3
# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

"""Qualify real-CXL g12 PageRank and freeze the first useful CIRA lead."""

import argparse
import csv
import json
import os
import subprocess
import sys
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

try:
    from scripts import build_gapbs_m2ndp_pr_spmv as baseline_builder
    from scripts import build_gapbs_matched_pr_spmv_variants as variant_builder
    from scripts import cira_lead_policy
    from scripts import compare_gapbs_cxl_amu_cira as comparison
    from scripts import gapbs_pr_experiment_profiles as profiles
    from scripts import m2ndp_artifacts as artifacts
    from scripts import run_gapbs_matched_pr_spmv_variants as matched_runner
except ImportError:
    import build_gapbs_m2ndp_pr_spmv as baseline_builder
    import build_gapbs_matched_pr_spmv_variants as variant_builder
    import cira_lead_policy
    import compare_gapbs_cxl_amu_cira as comparison
    import gapbs_pr_experiment_profiles as profiles
    import m2ndp_artifacts as artifacts
    import run_gapbs_matched_pr_spmv_variants as matched_runner


REPO = Path(__file__).resolve().parents[1]
DEFAULT_ROOT = Path("/mnt/disk0/gem5-CXL-g14-eval")
REAL_CXL_FIELDS = comparison.REAL_CXL_FIELDS
CANDIDATES = cira_lead_policy.CANDIDATE_1US_LEADS
ACTIONS = (
    "vanilla-1us",
    "amu-1us",
    "cira-lead-1-1us",
    "cira-lead-2-1us",
    "cira-lead-4-1us",
    "cira-lead-8-1us",
    "freeze-cira-policy",
)


class QualificationError(RuntimeError):
    """Qualification state or evidence violated the frozen contract."""


def _integer(row, field):
    try:
        return int(row[field])
    except (KeyError, TypeError, ValueError) as error:
        raise QualificationError(
            f"{field} is not an integer: {row.get(field)!r}"
        ) from error


def cira_candidate_passes(row):
    try:
        return (
            _integer(row, "issued_csr_prefetches") > 0
            and _integer(row, "completed_prefetches") > 0
            and all(
                _integer(row, f"issued_csr_prefetches_core{core}") > 0
                for core in range(4)
            )
            and all(
                _integer(row, f"completed_prefetches_core{core}") > 0
                for core in range(4)
            )
            and _integer(row, "useful_prefetches")
            > _integer(row, "late_prefetches")
            and _integer(row, "rejected_queue_full") == 0
            and _integer(row, "rejected_csr_index_queue_full") == 0
            and _integer(row, "dropped_csr_descriptors") == 0
            and row.get("timing_csr_traversal") == "true"
        )
    except QualificationError:
        return False


def select_first_passing(candidate_rows):
    for lead in CANDIDATES:
        row = candidate_rows.get(lead)
        if row is not None and cira_candidate_passes(row):
            return lead
    return None


def classify_g12_traffic(row):
    real = True
    for field in REAL_CXL_FIELDS:
        try:
            real = real and int(row[field]) > 0
        except (KeyError, TypeError, ValueError):
            real = False
    return {
        "g12_real_cxl": bool(real),
        "g12_cache_resident": not bool(real),
    }


def passed_record(*, command, input_hashes, output_hashes, result):
    return {
        "status": "passed",
        "command": list(command),
        "input_hashes": dict(sorted(input_hashes.items())),
        "output_hashes": dict(sorted(output_hashes.items())),
        "result": _json_safe(result),
    }


def _json_safe(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def validate_resumed_record(
    record, *, command, input_hashes, output_hashes
):
    if record.get("status") != "passed":
        raise QualificationError("resume record is not passed")
    if record.get("command") != list(command):
        raise QualificationError("resume command differs from recorded command")
    if record.get("input_hashes") != dict(sorted(input_hashes.items())):
        raise QualificationError("resume input or binary hash differs")
    if record.get("output_hashes") != dict(sorted(output_hashes.items())):
        raise QualificationError("resume output hash differs")
    return record.get("result")


def freeze_policy(
    path, *, selected_1us_lead, source_profile, result_hashes
):
    if selected_1us_lead not in CANDIDATES:
        raise QualificationError("selected CIRA lead is outside frozen candidates")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": 1,
        "source_profile": source_profile,
        "selected_1us_lead_blocks": selected_1us_lead,
        "row_block_size": cira_lead_policy.ROW_BLOCK_SIZE,
        "candidate_1us_lead_blocks": list(CANDIDATES),
        "result_hashes": dict(sorted(result_hashes.items())),
    }
    try:
        descriptor = os.open(
            path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644
        )
    except FileExistsError as error:
        raise QualificationError(f"policy already exists: {path}") from error
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, sort_keys=True, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return path


def _load_json(path, label):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise QualificationError(f"invalid {label}: {error}") from error
    if not isinstance(value, dict):
        raise QualificationError(f"{label} must be a JSON object")
    return value


def _hash_files(paths):
    return {
        name: artifacts.sha256_file(Path(path))
        for name, path in sorted(paths.items())
    }


def _split_per_core(value, label):
    fields = str(value).split(";")
    if len(fields) != 4:
        raise QualificationError(f"{label} does not contain four cores")
    return fields


def normalize_cira_row(row, lead):
    issued = _split_per_core(row.get("cira_csr_per_core", ""), "issued CSR")
    completed = _split_per_core(
        row.get("cira_completed_per_core", ""), "completed prefetch"
    )
    normalized = {
        "lead_blocks": lead,
        "issued_csr_prefetches": row.get("cira_csr_prefetches", 0),
        "completed_prefetches": row.get("cira_completed", 0),
        "useful_prefetches": row.get("cira_useful", 0),
        "late_prefetches": row.get("cira_late", 0),
        "rejected_queue_full": row.get("cira_rejected_queue_full", 0),
        "rejected_csr_index_queue_full": row.get(
            "cira_rejected_csr_index_queue_full", 0
        ),
        "dropped_csr_descriptors": row.get(
            "cira_dropped_csr_descriptors", 0
        ),
        "timing_csr_traversal": (
            "true" if int(row.get("cira_timing_csr_traversal", 0)) == 1
            else "false"
        ),
    }
    for core in range(4):
        normalized[f"issued_csr_prefetches_core{core}"] = issued[core]
        normalized[f"completed_prefetches_core{core}"] = completed[core]
    return normalized


def _pending_record():
    return {
        "status": "pending",
        "command": [],
        "input_hashes": {},
        "output_paths": {},
        "output_hashes": {},
        "result": None,
        "error": None,
    }


def _profile_actions(prefix=""):
    return (
        f"{prefix}vanilla-1us",
        f"{prefix}amu-1us",
        *(f"{prefix}cira-lead-{lead}-1us" for lead in CANDIDATES),
    )


def _new_state(contract):
    actions = (*ACTIONS, *_profile_actions("g14-"))
    return {
        "schema": 1,
        "contract": contract,
        "actions": {action: _pending_record() for action in actions},
    }


def _write_csv(path, row):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.unlink(missing_ok=True)
    comparison.write_summary(temporary, [row])
    os.replace(temporary, path)


def _baseline_manifest_paths(build):
    manifest_path = Path(build) / "manifest.json"
    manifest = _load_json(manifest_path, "baseline build manifest")
    return (
        manifest_path,
        Path(build) / "bin/pr_spmv",
        Path(manifest["reference_raw_path"]),
    )


def _ensure_baseline_build(options, tag):
    build = options.root / "qualification" / tag / "baseline-build"
    reference = options.root / "qualification" / tag / "raw/vanilla.u32"
    manifest_path = build / "manifest.json"
    if not manifest_path.exists():
        baseline_builder.main(
            [
                "--cxlmemuring", str(options.cxlmemuring),
                "--outdir", str(build),
                "--reference-raw", str(reference),
                "--m5-library", str(options.m5_library),
                "--cxx", options.cxx,
            ]
        )
    _, binary, recorded_reference = _baseline_manifest_paths(build)
    if recorded_reference.resolve() != reference.resolve():
        raise QualificationError("baseline raw output path changed")
    manifest = _load_json(manifest_path, "baseline build manifest")
    if artifacts.sha256_file(binary) != manifest["binary_sha256"]["pr_spmv"]:
        raise QualificationError("baseline binary hash changed")
    return build, binary, reference


def _ensure_variant_build(options, tag, baseline_build, lead):
    build = options.root / "qualification" / tag / f"variants/lead-{lead}"
    manifest_path = build / "manifest.json"
    if not manifest_path.exists():
        variant_builder.main(
            [
                "--baseline-build", str(baseline_build),
                "--outdir", str(build),
                "--cxlmemuring", str(options.cxlmemuring),
                "--m5-library", str(options.m5_library),
                "--cxx", options.cxx,
                "--cira-prefetch-distance", str(lead),
                "--cira-row-batch", "64",
            ]
        )
    manifest, variants = matched_runner.load_manifest(manifest_path)
    if int(manifest.get("cira_lead_blocks", -1)) != lead:
        raise QualificationError("variant CIRA lead differs from build path")
    return build, manifest_path, variants


def _compare_options(options, profile, manifest_path, graph, outdir):
    return SimpleNamespace(
        profile=profile.name,
        graph_manifest=manifest_path,
        gem5=options.gem5,
        config=options.config,
        graph=graph,
        graph_scale=profile.graph_scale,
        cxl_link_delay="1us",
        checkpoint_root=options.root / "qualification/checkpoints",
        outdir=outdir,
        timeout=options.timeout,
        smoke_test=False,
    )


def _common_row_gate(row, kind, profile):
    expected = {
        "benchmark": "pr_spmv",
        "kind": kind,
        "status": "ok",
        "verification": "pass",
        "scale": profile.graph_scale,
        "iterations": 2,
        "measured_trial": 1,
        "roi_cpu": "timing",
        "cores": 4,
        "cxl_link_delay": "1us",
        "all_memory_cxl": True,
        "checkpoint_restores": 1,
        "graph_sha256": profile.graph_sha256,
    }
    for field, wanted in expected.items():
        if row.get(field) != wanted:
            raise QualificationError(
                f"{kind} {field}={row.get(field)!r}, expected {wanted!r}"
            )
    if _integer(row, "sim_ticks") <= 0:
        raise QualificationError(f"{kind} sim_ticks must be positive")


def _action_contract(options, profile, graph_manifest, binary, build_manifest, action):
    command = (
        "comparison.run_one", action, str(binary.resolve()),
        str(Path(graph_manifest).resolve()), "--cores", "4", "--trials", "2",
        "--iterations", "20", "--latency", "1us",
    )
    inputs = _hash_files(
        {
            "binary": binary,
            "build_manifest": build_manifest,
            "config": options.config,
            "gem5": options.gem5,
            "graph": Path(profile_graph(profile, graph_manifest)),
            "graph_manifest": graph_manifest,
        }
    )
    return command, inputs


def profile_graph(profile, graph_manifest):
    manifest = profiles.load_graph_manifest(graph_manifest)
    if manifest.graph_sha256 != profile.graph_sha256:
        raise QualificationError("profile and graph manifest hashes differ")
    return manifest.graph


def _current_output_hashes(record):
    return _hash_files(
        {name: Path(path) for name, path in record["output_paths"].items()}
    )


def _run_or_resume_action(
    *, options, state, action, profile, graph_manifest, binary,
    build_manifest, raw, kind, lead=None, baseline_raw=None
):
    command, inputs = _action_contract(
        options, profile, graph_manifest, binary, build_manifest, action
    )
    record = state["actions"][action]
    if record["status"] == "passed":
        return validate_resumed_record(
            record,
            command=command,
            input_hashes=inputs,
            output_hashes=_current_output_hashes(record),
        )
    if record["status"] not in {"pending", "failed", "running"}:
        raise QualificationError(f"invalid action state for {action}")
    state["actions"][action] = {
        **_pending_record(), "status": "running", "command": list(command),
        "input_hashes": inputs,
    }
    artifacts.atomic_write_json(options.status, state)
    raw.parent.mkdir(parents=True, exist_ok=True)
    raw.unlink(missing_ok=True)
    run_root = options.root / "qualification/runs" / action
    compare_options = _compare_options(
        options, profile, graph_manifest, Path(profile_graph(profile, graph_manifest)),
        run_root,
    )
    compare_args = matched_runner.make_compare_args(compare_options)
    row = comparison.run_one(
        compare_args, "pr_spmv", action, binary.parent, kind
    )
    _common_row_gate(row, kind, profile)
    if kind in {"amu", "cira"}:
        matched_runner.validate_row(
            row, kind, smoke_test=False, profile=profile, latency="1us"
        )
    if not raw.is_file():
        raise QualificationError(f"{action} did not produce raw vector")
    if baseline_raw is not None:
        variant_builder.validate_raw_outputs(
            baseline_raw, {kind: raw}, profile.num_nodes
        )
    summary = run_root / "summary.csv"
    _write_csv(summary, row)
    run_dir = Path(row["run_dir"])
    output_paths = {
        "summary": str(summary.resolve()),
        "raw": str(raw.resolve()),
        "config": str((run_dir / "config.ini").resolve()),
        "checkpoint_manifest": str(Path(row["checkpoint_manifest"]).resolve()),
    }
    result = normalize_cira_row(row, lead) if kind == "cira" else row
    output_hashes = _hash_files(output_paths)
    state["actions"][action] = {
        **passed_record(
            command=command, input_hashes=inputs,
            output_hashes=output_hashes, result=result,
        ),
        "output_paths": output_paths,
    }
    artifacts.atomic_write_json(options.status, state)
    return result


def _load_or_create_state(options, contract):
    if options.status.exists():
        if not options.resume:
            raise QualificationError(
                f"qualification state exists; use --resume: {options.status}"
            )
        state = _load_json(options.status, "qualification state")
        if state.get("schema") != 1 or state.get("contract") != contract:
            raise QualificationError("resume contract differs")
        return state
    state = _new_state(contract)
    artifacts.atomic_write_json(options.status, state)
    return state


def _run_profile(options, state, *, tag, profile_name, graph_manifest, prefix=""):
    profile = profiles.load_frozen_profile(profile_name, graph_manifest)
    graph = Path(profile_graph(profile, graph_manifest))
    baseline_build, baseline_binary, baseline_raw = _ensure_baseline_build(
        options, tag
    )
    baseline_manifest = baseline_build / "manifest.json"
    vanilla = _run_or_resume_action(
        options=options, state=state, action=f"{prefix}vanilla-1us",
        profile=profile, graph_manifest=graph_manifest,
        binary=baseline_binary, build_manifest=baseline_manifest,
        raw=baseline_raw, kind="baseline",
    )
    _, lead_one_manifest, lead_one_variants = _ensure_variant_build(
        options, tag, baseline_build, 1
    )
    amu_variant = lead_one_variants["amu"]
    _run_or_resume_action(
        options=options, state=state, action=f"{prefix}amu-1us",
        profile=profile, graph_manifest=graph_manifest,
        binary=Path(amu_variant["binary"]), build_manifest=lead_one_manifest,
        raw=Path(amu_variant["reference_raw"]), kind="amu",
        baseline_raw=baseline_raw,
    )
    candidates = {}
    for lead in CANDIDATES:
        _, variant_manifest, variants = _ensure_variant_build(
            options, tag, baseline_build, lead
        )
        cira = variants["cira"]
        result = _run_or_resume_action(
            options=options, state=state,
            action=f"{prefix}cira-lead-{lead}-1us",
            profile=profile, graph_manifest=graph_manifest,
            binary=Path(cira["binary"]), build_manifest=variant_manifest,
            raw=Path(cira["reference_raw"]), kind="cira", lead=lead,
            baseline_raw=baseline_raw,
        )
        candidates[lead] = result
        if cira_candidate_passes(result):
            break
    return profile, vanilla, candidates


def _contract(options):
    files = {
        "g12_manifest": options.g12_manifest,
        "gem5": options.gem5,
        "config": options.config,
        "m5_library": options.m5_library,
    }
    if options.g14_manifest.is_file():
        files["g14_manifest"] = options.g14_manifest
    return {
        "profile": "g12-4thread-qualification",
        "cores": 4,
        "threads": 4,
        "trials": 2,
        "page_rank_iterations": 20,
        "latency": "1us",
        "all_memory_cxl": True,
        "files": {name: str(Path(path).resolve()) for name, path in files.items()},
        "hashes": _hash_files(files),
    }


def _result_hashes(state):
    hashes = {}
    for action, record in state["actions"].items():
        if record.get("status") != "passed":
            continue
        for name, digest in record.get("output_hashes", {}).items():
            hashes[f"{action}:{name}"] = digest
    return hashes


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--gem5", type=Path, default=REPO / "build/X86/gem5.opt")
    parser.add_argument(
        "--config", type=Path,
        default=REPO / "configs/example/gem5_library/x86-gapbs-amu-se.py",
    )
    parser.add_argument(
        "--cxlmemuring", type=Path,
        default=Path("/home/victoryang00/CXLMemUring"),
    )
    parser.add_argument(
        "--m5-library", type=Path,
        default=REPO / "util/m5/build/x86/out/libm5.a",
    )
    parser.add_argument("--cxx", default=os.environ.get("CXX", "g++"))
    parser.add_argument("--timeout", type=int, default=0)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args(argv)
    args.root = args.root.resolve()
    args.gem5 = args.gem5.resolve()
    args.config = args.config.resolve()
    args.cxlmemuring = args.cxlmemuring.resolve()
    args.m5_library = args.m5_library.resolve()
    args.g12_manifest = args.root / "graphs/g12.manifest.json"
    args.g14_manifest = args.root / "graphs/g14.manifest.json"
    args.status = args.root / "qualification/status.json"
    args.qualification = args.root / "qualification/qualification.json"
    args.policy = args.root / "policy/cira-lead.json"
    return args


def _validate_options(options):
    if options.timeout < 0:
        raise QualificationError("--timeout must be nonnegative")
    for label, path in (
        ("gem5", options.gem5), ("config", options.config),
        ("m5 library", options.m5_library),
        ("g12 manifest", options.g12_manifest),
    ):
        if not Path(path).is_file():
            raise QualificationError(f"{label} is missing: {path}")
    if not options.cxlmemuring.is_dir():
        raise QualificationError(f"CXLMemUring is missing: {options.cxlmemuring}")


def main(argv=None):
    options = parse_args(argv)
    try:
        _validate_options(options)
        options.status.parent.mkdir(parents=True, exist_ok=True)
        state = _load_or_create_state(options, _contract(options))
        _, vanilla, candidates = _run_profile(
            options, state, tag="g12", profile_name="g12-4thread-qualification",
            graph_manifest=options.g12_manifest,
        )
        traffic = classify_g12_traffic(vanilla)
        selected = select_first_passing(candidates)
        source_profile = "g12-4thread-qualification"
        if traffic["g12_cache_resident"]:
            if not options.g14_manifest.is_file():
                raise QualificationError(
                    "g12 is cache-resident and g14 manifest is missing"
                )
            _, g14_vanilla, candidates = _run_profile(
                options, state, tag="g14-preformal",
                profile_name="g14-4thread-sweep",
                graph_manifest=options.g14_manifest, prefix="g14-",
            )
            try:
                comparison.require_real_cxl(g14_vanilla)
            except comparison.StatsError as error:
                raise QualificationError(
                    "g14 pre-formal Vanilla did not reach real CXL memory"
                ) from error
            selected = select_first_passing(candidates)
            source_profile = "g14-4thread-sweep"
        if selected is None:
            raise QualificationError("no CIRA lead passed qualification")
        qualification = {
            "schema": 1,
            **traffic,
            "source_profile": source_profile,
            "selected_1us_lead_blocks": selected,
            "raw_bit_exact": True,
            "candidate_results": candidates,
            "result_hashes": _result_hashes(state),
        }
        artifacts.atomic_write_json(options.qualification, qualification)
        result_hashes = {
            **qualification["result_hashes"],
            "qualification": artifacts.sha256_file(options.qualification),
        }
        freeze = state["actions"]["freeze-cira-policy"]
        command = (
            "freeze-cira-policy", str(selected), source_profile,
            str(options.policy),
        )
        inputs = {"qualification": result_hashes["qualification"]}
        if freeze["status"] == "passed":
            validate_resumed_record(
                freeze, command=command, input_hashes=inputs,
                output_hashes=_current_output_hashes(freeze),
            )
        else:
            expected_policy = {
                "schema": 1,
                "source_profile": source_profile,
                "selected_1us_lead_blocks": selected,
                "row_block_size": cira_lead_policy.ROW_BLOCK_SIZE,
                "candidate_1us_lead_blocks": list(CANDIDATES),
                "result_hashes": dict(sorted(result_hashes.items())),
            }
            if options.policy.exists():
                if _load_json(options.policy, "CIRA lead policy") != expected_policy:
                    raise QualificationError(
                        "existing CIRA lead policy differs after interrupted freeze"
                    )
            else:
                freeze_policy(
                    options.policy, selected_1us_lead=selected,
                    source_profile=source_profile, result_hashes=result_hashes,
                )
            outputs = {"policy": str(options.policy.resolve())}
            state["actions"]["freeze-cira-policy"] = {
                **passed_record(
                    command=command, input_hashes=inputs,
                    output_hashes=_hash_files(outputs), result={
                        "selected_1us_lead_blocks": selected,
                        "source_profile": source_profile,
                    },
                ),
                "output_paths": outputs,
            }
            artifacts.atomic_write_json(options.status, state)
        print(
            "G12_QUALIFICATION_COMPLETE "
            f"lead={selected} source_profile={source_profile} "
            f"qualification={options.qualification} policy={options.policy}"
        )
        return 0
    except (
        QualificationError, artifacts.EvidenceError, profiles.ProfileError,
        matched_runner.VariantRunError, variant_builder.VariantEvidenceError,
        comparison.StatsError, OSError, KeyError,
        subprocess.SubprocessError,
    ) as error:
        print(f"G12_QUALIFICATION_FAILED error={error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
