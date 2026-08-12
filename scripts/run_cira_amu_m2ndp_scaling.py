#!/usr/bin/env python3
# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

"""Run the formal four-scale Vanilla/AMU/CIRA/M2NDP comparison."""

import argparse
import csv
import dataclasses
import hashlib
import json
import re
import subprocess
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path

try:
    from scripts import cross_system_contract as contract
except ImportError:
    import cross_system_contract as contract


REPO = Path(__file__).resolve().parents[1]
PROFILE = "pr-scaling-4thread-1us"
SCALES = (4, 12, 14, 20)
SYSTEMS = ("vanilla", "amu", "cira", "m2ndp")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class ScalingError(RuntimeError):
    """A scaling point violates the formal experiment contract."""


@dataclasses.dataclass(frozen=True)
class MatrixEntry:
    scale: int
    system: str
    latency: str = "1us"
    full_e2e: bool = True

    def __post_init__(self):
        if self.scale not in SCALES:
            raise ScalingError(f"unsupported graph scale: {self.scale}")
        if self.system not in SYSTEMS:
            raise ScalingError(f"unsupported system: {self.system}")
        if self.latency != "1us" or self.full_e2e is not True:
            raise ScalingError("formal scaling points must be full E2E at 1us")

    @property
    def key(self):
        return f"g{self.scale}:{self.system}"


def build_matrix():
    return tuple(
        MatrixEntry(scale, system)
        for scale in SCALES
        for system in SYSTEMS
    )


def _sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _code_sha256():
    digest = hashlib.sha256()
    for path in (
        Path(__file__).resolve(),
        REPO / "scripts/compare_gapbs_cxl_amu_cira.py",
        REPO / "scripts/gapbs_pr_experiment_profiles.py",
        REPO / "scripts/run_gapbs_matched_pr_spmv_variants.py",
        REPO / "scripts/run_m2ndp_g20_pr_spmv.py",
    ):
        relative = path.relative_to(REPO).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(8, "little"))
        digest.update(relative)
        digest.update(bytes.fromhex(_sha256_file(path)))
    return digest.hexdigest()


def _load_json(path, label):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ScalingError(f"invalid {label}: {error}") from error
    if not isinstance(value, dict):
        raise ScalingError(f"{label} must be a JSON object")
    return value


def load_inputs(path):
    value = _load_json(path, "frozen input manifest")
    if value.get("schema") != 1 or value.get("status") != "accepted":
        raise ScalingError("frozen input manifest is not accepted schema 1")
    graphs = value.get("graphs")
    if not isinstance(graphs, list) or tuple(
        row.get("scale") for row in graphs if isinstance(row, dict)
    ) != SCALES:
        raise ScalingError("frozen graphs must be ordered g4,g12,g14,g20")
    for row in graphs:
        if row.get("num_nodes") != 1 << row["scale"]:
            raise ScalingError(f"g{row['scale']} node count differs from scale")
        for field, hash_field in (
            ("path", "sha256"),
            ("manifest", "manifest_sha256"),
        ):
            candidate = Path(row.get(field, ""))
            expected = row.get(hash_field)
            if not candidate.is_absolute() or not candidate.is_file():
                raise ScalingError(f"g{row['scale']} {field} is missing")
            if not isinstance(expected, str) or _SHA256.fullmatch(expected) is None:
                raise ScalingError(f"g{row['scale']} {hash_field} is invalid")
            if _sha256_file(candidate) != expected:
                raise ScalingError(f"g{row['scale']} {field} SHA-256 changed")
    return value


def _graph_for(entry, options):
    inputs = load_inputs(options.inputs)
    return next(row for row in inputs["graphs"] if row["scale"] == entry.scale)


def command_for(entry, options):
    graph = _graph_for(entry, options)
    common = [
        "--graph", str(Path(graph["path"]).resolve()),
        "--graph-scale", str(entry.scale),
        "--profile", PROFILE,
        "--graph-manifest", str(Path(graph["manifest"]).resolve()),
        "--cxl-link-delay", "1us",
        "--gem5", str(Path(options.gem5).resolve()),
        "--timeout", str(options.timeout),
    ]
    scale_root = Path(options.root).resolve() / "scales" / f"g{entry.scale}"
    if entry.system in {"vanilla", "m2ndp"}:
        command = [
            sys.executable,
            str(REPO / "scripts/run_m2ndp_g20_pr_spmv.py"),
            *common,
            "--cxlmemuring", str(Path(options.cxlmemuring).resolve()),
            "--m2ndp-root", str(Path(options.m2ndp_root).resolve()),
            "--outdir", str(scale_root / "m2ndp"),
        ]
        if entry.system == "vanilla":
            command.extend(("--stop-after", "gem5_baseline"))
            if (
                getattr(options, "resume", False)
                and (scale_root / "m2ndp/status.json").is_file()
            ):
                command.append("--resume")
        else:
            command.append("--resume")
        return command
    command = [
        sys.executable,
        str(REPO / "scripts/run_gapbs_matched_pr_spmv_variants.py"),
        *common,
        "--config", str(Path(options.config).resolve()),
        "--variants-build",
        str(Path(options.variants_build_root).resolve() / f"g{entry.scale}"),
        "--kind", entry.system,
        "--checkpoint-root", str(scale_root / "checkpoints" / entry.system),
        "--outdir", str(scale_root / entry.system),
    ]
    if entry.system == "amu":
        command.extend((
            "--asmc-profile", "paper-calibrated",
            "--asmc-calibration-manifest",
            str(Path(options.calibration).resolve()),
        ))
    return command


def validate_config(path):
    try:
        lines = Path(path).read_text(
            encoding="utf-8", errors="replace"
        ).splitlines()
    except OSError as error:
        raise ScalingError(f"cannot read gem5 config: {error}") from error
    fields = {}
    for line in lines:
        if "=" in line:
            key, value = line.split("=", 1)
            fields.setdefault(key.strip(), []).append(value.strip())
    if fields.get("delay") != ["1000000"]:
        raise ScalingError(
            f"CXL delay is {fields.get('delay')!r}, expected ['1000000']"
        )
    if "num_cpus" in fields and fields["num_cpus"] != ["4"]:
        raise ScalingError("gem5 config does not use four cores")
    if "all_memory_cxl" in fields and fields["all_memory_cxl"] != ["true"]:
        raise ScalingError("gem5 config is not all-memory-CXL")
    return fields


def validate_checkpoint_manifest(manifest):
    if not isinstance(manifest, dict):
        raise ScalingError("checkpoint manifest must be an object")
    if manifest.get("boundary") != "trial0_entry":
        raise ScalingError("checkpoint boundary must be trial0_entry")
    return manifest


def validate_rank_bits(reference, actual, *, expected_words):
    expected_size = expected_words * 4
    reference = Path(reference)
    actual = Path(actual)
    for label, path in (("reference", reference), ("result", actual)):
        if not path.is_file() or path.stat().st_size != expected_size:
            raise ScalingError(
                f"{label} rank image must contain {expected_words} u32 words"
            )
    word = 0
    with reference.open("rb") as left, actual.open("rb") as right:
        while True:
            a = left.read(1024 * 1024)
            b = right.read(1024 * 1024)
            if not a:
                break
            if a != b:
                for offset in range(0, len(a), 4):
                    if a[offset:offset + 4] != b[offset:offset + 4]:
                        raise ScalingError(
                            f"rank bit mismatch at word {word + offset // 4}"
                        )
            word += len(a) // 4
    return _sha256_file(reference)


def _integer(row, field):
    try:
        return int(row[field])
    except (KeyError, TypeError, ValueError) as error:
        raise ScalingError(f"{field} is not an integer") from error


def _decimal(row, field):
    try:
        value = Decimal(str(row[field]))
    except (KeyError, InvalidOperation) as error:
        raise ScalingError(f"{field} is not a decimal") from error
    if not value.is_finite():
        raise ScalingError(f"{field} must be finite")
    return value


def _per_core(row, field):
    try:
        values = tuple(int(value) for value in str(row[field]).split(";"))
    except (KeyError, ValueError) as error:
        raise ScalingError(f"{field} is not a per-core integer vector") from error
    return values


def validate_mechanism_row(system, row):
    if row.get("status") != "ok" or row.get("verification") != "pass":
        raise ScalingError(f"{system} functional verification did not pass")
    if system == "vanilla":
        return row
    if system == "amu":
        issued = _integer(row, "asmc_loads")
        completed = _integer(row, "asmc_completed")
        errors = sum(_integer(row, field) for field in (
            "asmc_queue_full_errors",
            "asmc_spm_full_errors",
            "asmc_translation_errors",
            "asmc_pending_errors",
            "asmc_spm_flag_errors",
        ))
        if errors:
            raise ScalingError(f"AMU error counters are nonzero: {errors}")
        if issued <= 0 or issued != completed:
            raise ScalingError("AMU issued/completed work differs")
        return row
    if system == "cira":
        issued = _integer(row, "cira_prefetches")
        completed = _integer(row, "cira_completed")
        errors = sum(_integer(row, field) for field in (
            "cira_rejected_queue_full",
            "cira_rejected_csr_index_queue_full",
            "cira_dropped_csr_descriptors",
        ))
        per_core_issued = _per_core(row, "cira_issued_per_core")
        per_core_completed = _per_core(row, "cira_completed_per_core")
        if errors:
            raise ScalingError(f"CIRA rejected or dropped work: {errors}")
        if (
            issued <= 0
            or completed <= 0
            or len(per_core_issued) != 4
            or len(per_core_completed) != 4
            or any(value <= 0 for value in per_core_issued)
        ):
            raise ScalingError("CIRA requires four active cores")
        if per_core_issued != per_core_completed:
            raise ScalingError("CIRA per-core issued/completed work differs")
        return row
    if system == "m2ndp":
        if (
            _integer(row, "funcsim_compared") <= 0
            or _integer(row, "funcsim_mismatched") != 0
        ):
            raise ScalingError("M2NDP FuncSim bit-exact gate failed")
        if row.get("calibration_pass") != "pass":
            raise ScalingError("M2NDP calibration did not pass")
        if _decimal(row, "calibration_residual_ns") > _decimal(
            row, "calibration_link_period_ns"
        ):
            raise ScalingError("M2NDP calibration residual exceeds one link cycle")
        if _integer(row, "kernel_launches") <= 0:
            raise ScalingError("M2NDP has no kernel launches")
        return row
    raise ScalingError(f"unsupported system: {system}")


def new_state(options):
    input_hash = _sha256_file(options.inputs)
    calibration_hash = _sha256_file(options.calibration)
    return {
        "schema": 1,
        "status": "timing_in_progress",
        "profile": PROFILE,
        "code_sha256": _code_sha256(),
        "inputs_sha256": input_hash,
        "calibration_sha256": calibration_hash,
        "gem5_sha256": _sha256_file(options.gem5),
        "config_sha256": _sha256_file(options.config),
        "points": {
            entry.key: {
                "scale": entry.scale,
                "system": entry.system,
                "latency": entry.latency,
                "full_e2e": entry.full_e2e,
                "status": "pending",
                "outputs": {},
            }
            for entry in build_matrix()
        },
    }


def record_pass(state, entry, output_hashes):
    if entry.key not in state.get("points", {}):
        raise ScalingError(f"unknown matrix point: {entry.key}")
    if not isinstance(output_hashes, dict) or not output_hashes:
        raise ScalingError("point output hashes are missing")
    for label, digest in output_hashes.items():
        if not isinstance(label, str) or _SHA256.fullmatch(str(digest)) is None:
            raise ScalingError(f"invalid output SHA-256 for {label}")
    point = state["points"][entry.key]
    point["status"] = "passed"
    point["outputs"] = dict(sorted(output_hashes.items()))
    return state


def is_complete(state):
    points = state.get("points", {})
    return (
        set(points) == {entry.key for entry in build_matrix()}
        and all(row.get("status") == "passed" for row in points.values())
    )


def _read_single_csv(path):
    try:
        with Path(path).open(newline="", encoding="utf-8") as stream:
            rows = list(csv.DictReader(stream))
    except OSError as error:
        raise ScalingError(f"cannot read summary {path}: {error}") from error
    if len(rows) != 1:
        raise ScalingError(f"{path} must contain exactly one data row")
    return rows[0]


def _point_outputs(entry, options):
    root = Path(options.root).resolve() / "scales" / f"g{entry.scale}"
    if entry.system == "vanilla":
        base = root / "m2ndp"
        summary = base / "gem5/run/summary.csv"
        row = _read_single_csv(summary)
        validate_mechanism_row("vanilla", row)
        rank = base / "reference/scores.raw"
        validate_rank_bits(rank, rank, expected_words=1 << entry.scale)
        return {"summary": _sha256_file(summary), "rank": _sha256_file(rank)}
    if entry.system in {"amu", "cira"}:
        base = root / entry.system
        summary = base / "summary.csv"
        row = _read_single_csv(summary)
        validate_mechanism_row(entry.system, row)
        run_dir = Path(row["run_dir"])
        validate_config(run_dir / "config.ini")
        evidence = base / "evidence.json"
        evidence_value = _load_json(evidence, f"{entry.system} evidence")
        variant_manifest = (
            Path(options.variants_build_root).resolve()
            / f"g{entry.scale}/manifest.json"
        )
        variant_value = _load_json(
            variant_manifest, f"g{entry.scale} variant manifest"
        )
        vanilla_build_manifest = root / "m2ndp/build/manifest.json"
        if (
            variant_value.get("baseline_manifest_sha256")
            != _sha256_file(vanilla_build_manifest)
        ):
            raise ScalingError(
                f"g{entry.scale} variant baseline differs from Vanilla build"
            )
        variant_rank = Path(
            evidence_value["runs"][entry.system]["reference_raw"]
        )
        vanilla_rank = root / "m2ndp/reference/scores.raw"
        validate_rank_bits(
            vanilla_rank,
            variant_rank,
            expected_words=1 << entry.scale,
        )
        return {
            "summary": _sha256_file(summary),
            "evidence": _sha256_file(evidence),
            "variant_manifest": _sha256_file(variant_manifest),
            "rank": _sha256_file(variant_rank),
        }
    base = root / "m2ndp"
    summary = base / "summary.csv"
    row = _read_single_csv(summary)
    # The publisher's individual parsers already enforce strict FuncSim,
    # calibration, and NDPSim gates; preserve those proof artifacts here.
    if row.get("verification") != "pass" or row.get("funcsim_strict") != "pass":
        raise ScalingError("M2NDP publication verification did not pass")
    calibration = _load_json(
        base / "calibration/calibration.json", "M2NDP calibration"
    )
    trace_meta = _load_json(base / "trace/trace.meta.json", "M2NDP trace")
    validate_mechanism_row("m2ndp", {
        "status": "ok",
        "verification": row.get("verification"),
        "funcsim_compared": row.get("funcsim_compared"),
        "funcsim_mismatched": 0,
        "calibration_pass": (
            "pass" if calibration.get("passed") is True else "fail"
        ),
        "calibration_residual_ns": calibration.get("residual_ns"),
        "calibration_link_period_ns": calibration.get("link_period_ns"),
        "kernel_launches": trace_meta.get("ndpsim_launches"),
    })
    validate_rank_bits(
        base / "reference/scores.raw",
        base / "funcsim/scores.u32",
        expected_words=1 << entry.scale,
    )
    status = _load_json(base / "status.json", "M2NDP stage state")
    required = ("funcsim", "calibration", "ndpsim", "publish")
    if any(status.get("stages", {}).get(stage, {}).get("status") != "passed"
           for stage in required):
        raise ScalingError("M2NDP required stages did not all pass")
    return {
        "summary": _sha256_file(summary),
        "rank": _sha256_file(base / "funcsim/scores.u32"),
        "status": _sha256_file(base / "status.json"),
    }


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inputs", type=Path, required=True)
    parser.add_argument("--calibration", type=Path, required=True)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--gem5", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--cxlmemuring", type=Path, required=True)
    parser.add_argument("--m2ndp-root", type=Path, required=True)
    parser.add_argument("--variants-build-root", type=Path, required=True)
    parser.add_argument("--timeout", type=int, default=0)
    parser.add_argument("--resume", action="store_true")
    return parser.parse_args(argv)


def main(argv=None):
    options = parse_args(argv)
    state_path = Path(options.root).resolve() / "state.json"
    complete_path = Path(options.root).resolve() / "complete.json"
    failed_path = Path(options.root).resolve() / "failed.json"
    try:
        load_inputs(options.inputs)
        if options.timeout < 0:
            raise ScalingError("--timeout must be nonnegative")
        expected = new_state(options)
        if state_path.exists():
            if not options.resume:
                raise ScalingError(f"state exists; use --resume: {state_path}")
            state = _load_json(state_path, "scaling state")
            for field in (
                "schema",
                "profile",
                "code_sha256",
                "inputs_sha256",
                "calibration_sha256",
                "gem5_sha256",
                "config_sha256",
            ):
                if state.get(field) != expected[field]:
                    raise ScalingError("resume state identity differs")
        else:
            if options.resume:
                raise ScalingError("--resume requested but scaling state is missing")
            state = expected
            contract.atomic_write_json(state_path, state)
        for entry in build_matrix():
            if state["points"][entry.key]["status"] == "passed":
                current = _point_outputs(entry, options)
                if current != state["points"][entry.key]["outputs"]:
                    raise ScalingError(
                        f"{entry.key} passed outputs changed before resume"
                    )
                continue
            command = command_for(entry, options)
            completed = subprocess.run(command, cwd=REPO, check=False)
            if completed.returncode != 0:
                raise ScalingError(
                    f"{entry.key} exited {completed.returncode}"
                )
            record_pass(state, entry, _point_outputs(entry, options))
            contract.atomic_write_json(state_path, state)
        if not is_complete(state):
            raise ScalingError("scaling state stopped before 16/16 passed")
        state["status"] = "complete"
        contract.atomic_write_json(state_path, state)
        contract.atomic_write_json(complete_path, state)
        failed_path.unlink(missing_ok=True)
        print(f"SCALING_COMPLETE points=16 manifest={complete_path}")
        return 0
    except (ScalingError, OSError, KeyError) as error:
        complete_path.unlink(missing_ok=True)
        failure = {"schema": 1, "status": "failed", "error": str(error)}
        contract.atomic_write_json(failed_path, failure)
        print(f"SCALING_FAILED error={error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
