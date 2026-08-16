#!/usr/bin/env python3
# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

"""Qualify g12 AMU/CIRA before any formal PR scaling run."""

import argparse
import json
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

try:
    from scripts import cross_system_contract as contract
    from scripts import run_cira_amu_m2ndp_scaling as scaling
except ImportError:
    import cross_system_contract as contract
    import run_cira_amu_m2ndp_scaling as scaling


PROFILE = "pr-scaling-g12-qualification"
SCALE = 12
SYSTEMS = ("vanilla", "amu", "cira")


class QualificationError(RuntimeError):
    """The pre-formal qualification contract was violated."""


def build_identity(options, inputs, variant_manifest):
    try:
        graph_hash = next(
            row["sha256"] for row in inputs["graphs"]
            if row["scale"] == SCALE
        )
    except (KeyError, StopIteration, TypeError) as error:
        raise QualificationError("qualification inputs have no g12 graph") from error
    variant_manifest = Path(variant_manifest).resolve()
    return {
        "code_sha256": scaling._code_sha256(),
        "inputs_sha256": scaling._sha256_file(options.inputs),
        "calibration_sha256": scaling._sha256_file(options.calibration),
        "gem5_sha256": scaling._sha256_file(options.gem5),
        "m5_library_sha256": scaling._sha256_file(options.m5_library),
        "config_sha256": scaling._sha256_file(options.config),
        "g12_graph_sha256": graph_hash,
        "variant_manifest": str(variant_manifest),
        "variant_manifest_sha256": scaling._sha256_file(variant_manifest),
    }


def _positive_decimal(value, label):
    try:
        result = Decimal(str(value))
    except Exception as error:
        raise QualificationError(f"{label} is not a decimal") from error
    if not result.is_finite() or result <= 0:
        raise QualificationError(f"{label} must be finite and positive")
    return result


def _decimal_text(value):
    return format(value.normalize(), "f")


def evaluate_gate(points):
    expected = {f"g{SCALE}:{system}" for system in SYSTEMS}
    if not isinstance(points, dict) or set(points) != expected:
        raise QualificationError("qualification requires exactly three g12 points")
    rank_hash = None
    for key in sorted(expected):
        row = points[key]
        if (
            not isinstance(row, dict)
            or row.get("status") != "passed"
            or row.get("mechanism", {}).get("verification") != "pass"
        ):
            raise QualificationError(f"{key} correctness did not pass")
        try:
            current_rank = row["outputs"]["rank"]
        except (KeyError, TypeError) as error:
            raise QualificationError(f"{key} rank evidence is missing") from error
        if rank_hash is None:
            rank_hash = current_rank
        elif current_rank != rank_hash:
            raise QualificationError("qualification rank hashes differ")

    baseline = _positive_decimal(
        points["g12:vanilla"].get("latency_seconds"),
        "g12 Vanilla latency",
    )
    speedups = {}
    offenders = []
    for system in ("amu", "cira"):
        key = f"g12:{system}"
        seconds = _positive_decimal(
            points[key].get("latency_seconds"), f"{key} latency"
        )
        speedup = baseline / seconds
        stored = _positive_decimal(points[key].get("speedup"), f"{key} speedup")
        if stored != speedup:
            raise QualificationError(f"{key} stored speedup differs")
        speedups[system] = _decimal_text(speedup)
        if not (
            scaling.MIN_ACCELERATOR_SPEEDUP
            <= speedup
            <= scaling.MAX_ACCELERATOR_SPEEDUP
        ):
            offenders.append({
                "point": key,
                "speedup": _decimal_text(speedup),
                "minimum": str(scaling.MIN_ACCELERATOR_SPEEDUP),
                "maximum": str(scaling.MAX_ACCELERATOR_SPEEDUP),
            })
    return {
        "status": "hold" if offenders else "passed",
        "checked_points": 2,
        "speedups": speedups,
        "offenders": offenders,
    }


def _new_state(options, inputs):
    identity = {
        "code_sha256": scaling._code_sha256(),
        "inputs_sha256": scaling._sha256_file(options.inputs),
        "calibration_sha256": scaling._sha256_file(options.calibration),
        "gem5_sha256": scaling._sha256_file(options.gem5),
        "m5_library_sha256": scaling._sha256_file(options.m5_library),
        "config_sha256": scaling._sha256_file(options.config),
        "g12_graph_sha256": next(
            row["sha256"] for row in inputs["graphs"]
            if row["scale"] == SCALE
        ),
    }
    return {
        "schema": 1,
        "status": "qualification_in_progress",
        "profile": PROFILE,
        **identity,
        "variant_builds": {
            "g12": {
                "status": "pending", "command": [], "inputs": {},
                "outputs": {}, "log": None, "error": None,
            }
        },
        "points": {
            f"g12:{system}": {
                "scale": SCALE, "system": system, "latency": "1us",
                "full_e2e": True, "status": "pending", "outputs": {},
                "latency_seconds": None, "speedup": None,
                "output_elements": 1 << SCALE, "mechanism": {},
            }
            for system in SYSTEMS
        },
    }


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inputs", type=Path, required=True)
    parser.add_argument("--calibration", type=Path, required=True)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--gem5", type=Path, required=True)
    parser.add_argument("--m5-library", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--cxlmemuring", type=Path, required=True)
    parser.add_argument("--m2ndp-root", type=Path, required=True)
    parser.add_argument("--variants-build-root", type=Path, required=True)
    parser.add_argument("--timeout", type=int, default=0)
    parser.add_argument("--resume", action="store_true")
    return parser.parse_args(argv)


def main(argv=None):
    options = parse_args(argv)
    root = Path(options.root).resolve()
    state_path = root / "state.json"
    qualification_path = root / "qualification.json"
    hold_path = root / "performance-hold.json"
    failed_path = root / "failed.json"
    try:
        inputs = scaling.load_inputs(options.inputs)
        if options.timeout < 0:
            raise QualificationError("--timeout must be nonnegative")
        expected = _new_state(options, inputs)
        if state_path.exists():
            if not options.resume:
                raise QualificationError(f"state exists; use --resume: {state_path}")
            state = scaling._load_json(state_path, "qualification state")
            for field in (
                "schema", "profile", "code_sha256", "inputs_sha256",
                "calibration_sha256", "gem5_sha256", "m5_library_sha256",
                "config_sha256", "g12_graph_sha256",
            ):
                if state.get(field) != expected[field]:
                    raise QualificationError("qualification resume identity differs")
        else:
            if options.resume:
                raise QualificationError("--resume requested but state is missing")
            root.mkdir(parents=True, exist_ok=True)
            state = expected
            contract.atomic_write_json(state_path, state)

        for system in SYSTEMS:
            entry = scaling.MatrixEntry(SCALE, system)
            point = state["points"][entry.key]
            if point["status"] == "passed":
                if (
                    scaling._point_outputs(entry, options) != point["outputs"]
                    or scaling._point_measurement(entry, options)["latency_seconds"]
                    != point["latency_seconds"]
                ):
                    raise QualificationError(f"{entry.key} outputs changed")
                continue
            failed_path.unlink(missing_ok=True)
            if scaling.needs_variant_build(entry):
                scaling.ensure_variants_for_scale(SCALE, state, options)
            completed = subprocess.run(
                scaling.command_for(entry, options),
                cwd=scaling.REPO,
                check=False,
            )
            if completed.returncode != 0:
                raise QualificationError(
                    f"{entry.key} exited {completed.returncode}"
                )
            scaling.record_pass(
                state,
                entry,
                scaling._point_outputs(entry, options),
                **scaling._point_measurement(entry, options),
            )
            contract.atomic_write_json(state_path, state)

        gate = evaluate_gate(state["points"])
        variant_manifest = (
            Path(options.variants_build_root).resolve() / "g12/manifest.json"
        )
        terminal = {
            **state,
            **build_identity(options, inputs, variant_manifest),
            "performance_gate": gate,
        }
        if gate["status"] == "hold":
            terminal["status"] = "performance_hold"
            contract.atomic_write_json(state_path, terminal)
            contract.atomic_write_json(hold_path, terminal)
            qualification_path.unlink(missing_ok=True)
            failed_path.unlink(missing_ok=True)
            print(
                "G12_QUALIFICATION_HOLD "
                f"offenders={len(gate['offenders'])}"
            )
            return 0
        terminal["status"] = "passed"
        contract.atomic_write_json(state_path, terminal)
        contract.atomic_write_json(qualification_path, terminal)
        hold_path.unlink(missing_ok=True)
        failed_path.unlink(missing_ok=True)
        print(f"G12_QUALIFICATION_PASS manifest={qualification_path}")
        return 0
    except (
        QualificationError,
        scaling.ScalingError,
        OSError,
        KeyError,
    ) as error:
        qualification_path.unlink(missing_ok=True)
        failure = {"schema": 1, "status": "failed", "error": str(error)}
        contract.atomic_write_json(failed_path, failure)
        print(f"G12_QUALIFICATION_FAILED error={error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())