#!/usr/bin/env python3
# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

"""Publish G12 measured and calibrated-derived spectrum rows separately."""

import argparse
import csv
import hashlib
import json
import os
import re
import tempfile
from decimal import Decimal, InvalidOperation
from pathlib import Path


G12_GRAPH_SHA256 = (
    "759003842b672ad90eabbd5b045980e9ddf43a95bffb01b318db7fc4b8b551f1"
)
WORKLOADS = ("pr_spmv", "gap_bc")
LATENCIES = ("200ns", "500ns", "1us", "2us")
ANCHOR_SYSTEMS = ("host_inline", "cira")
SYSTEMS = (*ANCHOR_SYSTEMS, "m2ndp")
DEFAULT_SOURCE = Path(
    "/mnt/disk0/gem5-CXL-eval/cira-amu-m2ndp-spectrum/"
    "shared/g12-graph-m2ndp-r1"
)
DEFAULT_OUTPUT = DEFAULT_SOURCE / "publication"
_SHA256 = re.compile(r"[0-9a-f]{64}")


class PublishError(RuntimeError):
    """A source, model, coordinate, or numeric publication value is invalid."""


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def load_json(path):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PublishError(f"invalid JSON {path}: {error}") from error
    if not isinstance(value, dict):
        raise PublishError(f"JSON object required: {path}")
    return value


def _decimal(value, label):
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise PublishError(f"{label} is not decimal") from error
    if not number.is_finite() or number <= 0:
        raise PublishError(f"{label} must be finite and positive")
    return number


def _digest(value, label):
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise PublishError(f"{label} SHA-256 is invalid")
    return value


def _format_decimal(value):
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _pending(workload, latency, system, reason):
    return {
        "workload": workload,
        "latency": latency,
        "system": system,
        "status": "pending",
        "measurement_kind": "",
        "time_ns": "",
        "cycles": "",
        "core_period_ns": "",
        "entry_count": "",
        "evidence_sha256": "",
        "model_sha256": "",
        "reason": reason,
    }


def build_rows(spec):
    """Build the deterministic 24-row G12 table without inventing evidence."""
    if not isinstance(spec, dict) or spec.get("graph_sha256") != G12_GRAPH_SHA256:
        raise PublishError("publication graph identity differs")
    measured = spec.get("measured", {})
    anchors = spec.get("anchors", {})
    models = spec.get("models", {})
    if not all(isinstance(value, dict) for value in (measured, anchors, models)):
        raise PublishError("publication source maps are malformed")

    rows = []
    evidence_owners = {}
    for workload in WORKLOADS:
        for latency in LATENCIES:
            key = f"{workload}:{latency}"
            evidence = measured.get(key)
            if not isinstance(evidence, dict) or evidence.get("status") != "pass":
                rows.append(_pending(workload, latency, "m2ndp", "measured evidence missing"))
            else:
                cycles = evidence.get("cycles")
                if not isinstance(cycles, int) or isinstance(cycles, bool) or cycles <= 0:
                    raise PublishError(f"{key} measured cycle count is invalid")
                period = _decimal(evidence.get("core_period_ns"), f"{key} core period")
                digest = _digest(evidence.get("evidence_sha256"), f"{key} evidence")
                owner = evidence_owners.setdefault(digest, workload)
                if owner != workload:
                    raise PublishError("cross-workload evidence SHA alias")
                rows.append({
                    "workload": workload,
                    "latency": latency,
                    "system": "m2ndp",
                    "status": "pass",
                    "measurement_kind": "measured",
                    "time_ns": _format_decimal(Decimal(cycles) * period),
                    "cycles": str(cycles),
                    "core_period_ns": _format_decimal(period),
                    "entry_count": str(evidence.get("entry_count", 1)),
                    "evidence_sha256": digest,
                    "model_sha256": "",
                    "reason": "",
                })

            for system in ANCHOR_SYSTEMS:
                anchor = anchors.get(workload, {}).get(system)
                if not isinstance(anchor, dict):
                    rows.append(_pending(workload, latency, system, "anchor missing"))
                    continue
                if anchor.get("graph_sha256") != G12_GRAPH_SHA256:
                    raise PublishError("anchor graph identity differs")
                anchor_latency = anchor.get("latency")
                if anchor_latency not in LATENCIES:
                    raise PublishError("anchor latency is invalid")
                anchor_time = _decimal(
                    anchor.get("time_ns"), f"{workload} {system} anchor time"
                )
                anchor_sha = _digest(
                    anchor.get("evidence_sha256"),
                    f"{workload} {system} anchor evidence",
                )
                entry_count = anchor.get("entry_count")
                if (
                    not isinstance(entry_count, int)
                    or isinstance(entry_count, bool)
                    or entry_count <= 0
                ):
                    raise PublishError("anchor entry count is invalid")
                if latency == anchor_latency:
                    rows.append({
                        "workload": workload,
                        "latency": latency,
                        "system": system,
                        "status": "pass",
                        "measurement_kind": "measured",
                        "time_ns": _format_decimal(anchor_time),
                        "cycles": "",
                        "core_period_ns": "",
                        "entry_count": str(entry_count),
                        "evidence_sha256": anchor_sha,
                        "model_sha256": "",
                        "reason": "",
                    })
                    continue
                model = models.get(workload, {}).get(system)
                if not isinstance(model, dict):
                    rows.append(_pending(workload, latency, system, "calibration model missing"))
                    continue
                if model.get("anchor_latency") != anchor_latency:
                    raise PublishError("model anchor latency differs")
                factors = model.get("factors")
                if not isinstance(factors, dict) or latency not in factors:
                    rows.append(_pending(workload, latency, system, "latency factor missing"))
                    continue
                factor = _decimal(
                    factors[latency], f"{workload} {system} {latency} factor"
                )
                model_sha = _digest(
                    model.get("model_sha256"), f"{workload} {system} model"
                )
                rows.append({
                    "workload": workload,
                    "latency": latency,
                    "system": system,
                    "status": "pass",
                    "measurement_kind": "calibrated-derived",
                    "time_ns": _format_decimal(anchor_time * factor),
                    "cycles": "",
                    "core_period_ns": "",
                    "entry_count": str(entry_count),
                    "evidence_sha256": anchor_sha,
                    "model_sha256": model_sha,
                    "reason": "",
                })

    order = {value: index for index, value in enumerate(LATENCIES)}
    system_order = {value: index for index, value in enumerate(SYSTEMS)}
    rows.sort(key=lambda row: (
        WORKLOADS.index(row["workload"]), order[row["latency"]],
        system_order[row["system"]],
    ))
    coordinates = [(row["workload"], row["latency"], row["system"]) for row in rows]
    if len(rows) != 24 or len(set(coordinates)) != len(rows):
        raise PublishError("publication coordinates are incomplete or duplicated")
    return rows


def discover_spec(source=DEFAULT_SOURCE):
    source = Path(source)
    measured = {}
    for workload in WORKLOADS:
        for latency in LATENCIES:
            path = source / workload / latency / "ndpsim-evidence.json"
            if not path.is_file():
                continue
            value = load_json(path)
            if value.get("status") != "pass":
                continue
            measured[f"{workload}:{latency}"] = {
                "status": "pass",
                "cycles": value.get("cycles"),
                "core_period_ns": value.get("core_period_ns", "0.5"),
                "entry_count": value.get("expected_launches", 1),
                "evidence_sha256": sha256_file(path),
            }
    return {
        "graph_sha256": G12_GRAPH_SHA256,
        "measured": measured,
        "anchors": {},
        "models": {},
    }


def atomic_csv(path, rows):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    fields = list(rows[0])
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields, lineterminator="\n")
            writer.writeheader()
            writer.writerows(rows)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def publish(rows, output):
    output = Path(output)
    progress = output / "g12-fast-spectrum-progress.csv"
    atomic_csv(progress, rows)
    complete = all(row["status"] == "pass" for row in rows)
    raw = output / "g12-fast-spectrum-raw.csv"
    if complete:
        atomic_csv(raw, rows)
    manifest = {
        "schema": 1,
        "status": "accepted" if complete else "partial",
        "graph_sha256": G12_GRAPH_SHA256,
        "row_count": len(rows),
        "passed_rows": sum(row["status"] == "pass" for row in rows),
        "progress": {"path": str(progress), "sha256": sha256_file(progress)},
        "raw": (
            {"path": str(raw), "sha256": sha256_file(raw)} if complete else None
        ),
    }
    atomic_json(output / "g12-fast-spectrum-manifest.json", manifest)
    return manifest


def parse_options(arguments=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--spec", type=Path)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--check-only", action="store_true")
    return parser.parse_args(arguments)


def main(arguments=None):
    options = parse_options(arguments)
    spec = load_json(options.spec) if options.spec else discover_spec(options.source)
    rows = build_rows(spec)
    passed = sum(row["status"] == "pass" for row in rows)
    if options.check_only:
        print(f"G12_FAST_SPECTRUM_CHECK rows=24 passed={passed} pending={24-passed}")
        return 0
    manifest = publish(rows, options.output)
    print(
        "G12_FAST_SPECTRUM_PUBLISHED "
        f"status={manifest['status']} passed={manifest['passed_rows']}/24"
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except PublishError as error:
        raise SystemExit(f"G12_FAST_SPECTRUM_FAILED error={error}") from error
