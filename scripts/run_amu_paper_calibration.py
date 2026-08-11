#!/usr/bin/env python3
# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

"""Collect AMU paper-profile proxies and emit a calibrated manifest."""

import argparse
import csv
import json
import os
import re
import struct
import subprocess
import tempfile
from pathlib import Path

try:
    from scripts import amu_cira_calibration as calibration
except ImportError:
    import amu_cira_calibration as calibration


MEASUREMENT_FIELDS = (
    "workload",
    "latency_us",
    "simulated_normalized_time",
    "normalizer_cycles",
    "metadata_accesses",
    "id_batch_refills",
    "completions",
    "target",
    "paper_baseline_normalized",
    "weight",
    "average_mlp",
    "baseline_checksum",
    "amu_checksum",
)
LATENCY_KEYS = {0.1: "0.1", 0.2: "0.2", 0.5: "0.5", 1.0: "1", 2.0: "2", 5.0: "5"}
WORKLOADS = ("gups", "hj", "stream")
LATENCIES = ("0.1us", "0.2us", "0.5us", "1us", "2us", "5us")
REPO = Path(__file__).resolve().parents[1]
PROXY_SOURCE = REPO / "util/amu/amu_paper_profile.cc"
CHECKSUM_MAGIC = 0x414D5531
WORKLOAD_TAGS = {"gups": 1, "hj": 2, "stream": 3}
M5SUM_RE = re.compile(
    r"m5sum\(" + r",\s*".join([r"(0x[0-9a-fA-F]+|0)"] * 6) + r"\)"
)


def atomic_write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def load_json(path):
    try:
        with Path(path).open("r", encoding="utf-8") as stream:
            value = json.load(stream)
    except (OSError, json.JSONDecodeError) as error:
        raise calibration.CalibrationError(f"cannot read JSON {path}") from error
    if not isinstance(value, dict):
        raise calibration.CalibrationError(f"JSON root is not an object: {path}")
    return value


def write_measurements(path, rows):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent, text=True
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=MEASUREMENT_FIELDS)
            writer.writeheader()
            for row in rows:
                missing = [field for field in MEASUREMENT_FIELDS if field not in row]
                if missing:
                    raise calibration.CalibrationError(
                        "measurement missing fields: " + ", ".join(missing)
                    )
                writer.writerow({field: row[field] for field in MEASUREMENT_FIELDS})
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def load_measurements(path):
    path = Path(path)
    rows = []
    try:
        with path.open("r", encoding="utf-8", newline="") as stream:
            reader = csv.DictReader(stream)
            if tuple(reader.fieldnames or ()) != MEASUREMENT_FIELDS:
                raise calibration.CalibrationError(
                    f"{path}: measurement header differs from frozen schema"
                )
            rows.extend(reader)
    except OSError as error:
        raise calibration.CalibrationError(
            f"cannot read AMU measurements {path}"
        ) from error
    if not rows:
        raise calibration.CalibrationError("AMU measurements are empty")
    return rows


def parse_latency(value):
    text = str(value).strip().lower()
    if text.endswith("us"):
        text = text[:-2]
    try:
        latency = float(text)
    except ValueError as error:
        raise argparse.ArgumentTypeError("latency must use microseconds") from error
    if latency not in LATENCY_KEYS:
        raise argparse.ArgumentTypeError(
            "latency must be one of 0.1us, 0.2us, 0.5us, 1us, 2us, 5us"
        )
    return latency


def _validate_measurement_evidence(rows):
    expected = {
        (workload, float(latency))
        for workload, values in calibration.AMU_TABLE4.items()
        for latency in values
    }
    actual = set()
    gups_5us_mlp = None
    normalized = []
    for index, source in enumerate(rows):
        row = dict(source)
        try:
            workload = row["workload"]
            latency = float(row["latency_us"])
            target = float(row["target"])
            paper_baseline = float(row["paper_baseline_normalized"])
            average_mlp = float(row["average_mlp"])
        except (KeyError, TypeError, ValueError) as error:
            raise calibration.CalibrationError(
                f"measurement {index}: invalid evidence field"
            ) from error
        identity = (workload, latency)
        if identity in actual:
            raise calibration.CalibrationError(
                f"duplicate measurement {workload}@{latency:g}"
            )
        actual.add(identity)
        if workload not in calibration.AMU_TABLE4 or latency not in LATENCY_KEYS:
            raise calibration.CalibrationError(
                f"measurement {index}: not an AMU Table 4 point"
            )
        paper = calibration.AMU_TABLE4[workload][LATENCY_KEYS[latency]]
        if target != paper["amu"] or paper_baseline != paper["baseline"]:
            raise calibration.CalibrationError(
                f"measurement {index}: Table 4 target drift"
            )
        if row.get("baseline_checksum") != row.get("amu_checksum"):
            raise calibration.CalibrationError(
                f"measurement {index}: baseline/AMU checksum mismatch"
            )
        if not row.get("baseline_checksum"):
            raise calibration.CalibrationError(
                f"measurement {index}: empty checksum"
            )
        if workload == "gups" and latency == 5.0:
            gups_5us_mlp = average_mlp
        normalized.append(row)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise calibration.CalibrationError(
            f"AMU Table 4 matrix mismatch missing={missing} extra={extra}"
        )
    if gups_5us_mlp is None or gups_5us_mlp <= 130:
        raise calibration.CalibrationError(
            "GUPS 5us MLP must be greater than 130"
        )
    return normalized, gups_5us_mlp


def _validate_holdout_trend(rows, fit):
    holdout_key, holdout = next(iter(fit["holdout_residuals"].items()))
    workload, latency_text = holdout_key.split("@", 1)
    latency = float(latency_text)
    selected = fit["parameters"]
    validated = calibration._validated_measurements(rows)
    holdout_row = next(
        row
        for row in validated
        if row["workload"] == workload and row["latency_us"] == latency
    )
    prediction = calibration.predict_normalized_time(
        holdout_row, selected
    )
    candidates = [row for row in validated if row["workload"] == workload]
    lower = [row for row in candidates if row["latency_us"] < latency]
    upper = [row for row in candidates if row["latency_us"] > latency]
    neighbors = []
    if lower:
        neighbors.append(max(lower, key=lambda row: row["latency_us"]))
    if upper:
        neighbors.append(min(upper, key=lambda row: row["latency_us"]))
    if not neighbors:
        raise calibration.CalibrationError("AMU holdout has no latency neighbor")
    for neighbor in neighbors:
        neighbor_prediction = calibration.predict_normalized_time(
            neighbor, selected
        )
        target_delta = holdout_row["target"] - neighbor["target"]
        predicted_delta = prediction - neighbor_prediction
        if target_delta * predicted_delta < 0:
            raise calibration.CalibrationError(
                "AMU proxy holdout latency trend has the wrong sign"
            )
    return "PASS"


def _validate_fit(fit, gups_5us_mlp, rows):
    holdout = next(iter(fit["holdout_residuals"].values()))
    if holdout["relative_error"] > 0.25:
        raise calibration.CalibrationError(
            "AMU proxy holdout relative error exceeds 25%"
        )
    return {
        "status": "PASS",
        "proxy_relative_error_bound": 0.25,
        "holdout_relative_error": holdout["relative_error"],
        "gups_5us_average_mlp": gups_5us_mlp,
        "gups_5us_mlp_gate": ">130",
        "holdout_trend": _validate_holdout_trend(rows, fit),
        "table4_role": "trend-and-holdout-validation",
    }


def build_manifest(options):
    rows, gups_5us_mlp = _validate_measurement_evidence(
        load_measurements(options.measurements)
    )
    fit = calibration.fit_amu_control_costs(
        rows,
        holdout={
            "workload": options.holdout_workload,
            "latency_us": options.holdout_latency,
        },
    )
    validation = _validate_fit(fit, gups_5us_mlp, rows)
    amu_source = calibration.load_amu_source(options.pdf)
    cira_source = calibration.load_cira_source(options.cira_csv)
    return {
        "schema": 1,
        "sources": {
            "amu_pdf": amu_source,
            "cira_csv": cira_source,
        },
        "measurements": {
            "path": str(Path(options.measurements).resolve()),
            "sha256": calibration.sha256_file(options.measurements),
            "points": len(rows),
        },
        "amu": {
            "paper_isa": "RISC-V",
            "proxy_isa": "x86",
            "proxy_limitation": (
                "The formal x86 proxy imports exposed paper parameters; "
                "Table 4 is validation, not exact RISC-V reproduction."
            ),
            "fit": fit,
            "validation": validation,
            "formal_profile": {
                "spm_bytes": amu_source["direct"]["spm_bytes"],
                "pending_entries_per_state_machine": amu_source["direct"][
                    "pending_entries"
                ],
                "id_batch_entries": amu_source["direct"]["id_batch_entries"],
                **fit["parameters"],
            },
        },
        "cira": {
            "verified_workloads": cira_source["verified_workloads"],
            "excluded_workloads": cira_source["excluded_workloads"],
            "primary": cira_source["primary"],
            "geomean": cira_source["geomean"],
            "mode_mapping": cira_source["classification"],
        },
    }


def fit_arguments(measurements, pdf, cira_csv, output):
    return [
        "fit",
        "--measurements",
        str(measurements),
        "--pdf",
        str(pdf),
        "--cira-csv",
        str(cira_csv),
        "--holdout-workload",
        "stream",
        "--holdout-latency",
        "2us",
        "--output",
        str(output),
    ]


def collect_plan(options):
    binary = options.outdir / "bin/amu_paper_profile"
    build = [
        options.cxx,
        "-std=c++17",
        "-O3",
        "-Wall",
        "-Wextra",
        "-static",
        "-no-pie",
        "-ffp-contract=off",
        "-fno-fast-math",
        "-I",
        str(REPO / "include"),
        "-I",
        str(REPO / "util/amu"),
        str(PROXY_SOURCE),
        str(options.m5_library),
        "-o",
        str(binary),
    ]
    runs = []
    for workload in WORKLOADS:
        for latency in LATENCIES:
            for kind in ("baseline", "amu"):
                run_dir = options.outdir / "runs" / workload / latency / kind
                raw = run_dir / "checksum.u64"
                arguments = (
                    f"--workload {workload} --iterations {options.iterations} "
                    f"--raw-output {raw}"
                    + (" --amu" if kind == "amu" else "")
                )
                command = [
                    str(options.gem5),
                    "--debug-flags=PseudoInst",
                    f"--outdir={run_dir}",
                    str(options.config),
                    "--binary",
                    str(binary),
                    "--arguments",
                    arguments,
                    "--cpu",
                    "o3",
                    "--cores",
                    "1",
                    "--clk",
                    "3GHz",
                    "--l1d-size",
                    "32KiB",
                    "--l1i-size",
                    "32KiB",
                    "--l2-size",
                    "256KiB",
                    "--l1-mshrs",
                    "48",
                    "--l2-mshrs",
                    "48",
                    "--disable-hw-prefetchers",
                    "--cxl-memory",
                    "--cxl-link-delay",
                    latency,
                    "--roi-work-events",
                    "--continue-after-roi",
                    "--require-m5-verification-exit",
                    "--asmc-spm-size",
                    "64KiB",
                    "--asmc-max-outstanding",
                    "256",
                    "--asmc-max-send-queue",
                    "512",
                ]
                if kind == "baseline":
                    command.append("--no-asmc")
                runs.append(
                    {
                        "workload": workload,
                        "latency": latency,
                        "kind": kind,
                        "run_dir": str(run_dir),
                        "raw": str(raw),
                        "command": command,
                    }
                )
    return {"build": build, "binary": str(binary), "runs": runs}


def _materialize_register_checksum(record):
    log_path = Path(record["run_dir"]) / "gem5.log"
    try:
        text = log_path.read_text(encoding="utf-8", errors="strict")
    except OSError as error:
        raise calibration.CalibrationError(
            f"cannot read checksum transport log {log_path}"
        ) from error
    markers = []
    for match in M5SUM_RE.finditer(text):
        values = tuple(int(value, 0) for value in match.groups())
        if values[2] == CHECKSUM_MAGIC:
            markers.append(values)
    if len(markers) != 1:
        raise calibration.CalibrationError(
            f"expected one register checksum marker in {log_path}, "
            f"got {len(markers)}"
        )
    low, high, _, workload_tag, kind_tag, reserved = markers[0]
    expected_workload = WORKLOAD_TAGS[record["workload"]]
    expected_kind = 1 if record["kind"] == "amu" else 0
    if (
        low > 0xFFFFFFFF
        or high > 0xFFFFFFFF
        or workload_tag != expected_workload
        or kind_tag != expected_kind
        or reserved != 0
    ):
        raise calibration.CalibrationError(
            f"register checksum marker metadata mismatch in {log_path}"
        )
    payload = struct.pack("<Q", (high << 32) | low)
    raw_path = Path(record["raw"])
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{raw_path.name}.", dir=raw_path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, raw_path)
    finally:
        temporary.unlink(missing_ok=True)
    return f"{(high << 32) | low:016x}"


def _require_file(path, label):
    path = Path(path)
    if not path.is_file():
        raise calibration.CalibrationError(f"missing {label}: {path}")
    return path


def _stat_by_suffix(stats, suffix, *, required=True):
    matches = [(name, value) for name, value in stats.items() if name.endswith(suffix)]
    if len(matches) != 1:
        if not required and not matches:
            return 0
        raise calibration.CalibrationError(
            f"expected one ROI stat ending {suffix}, got {len(matches)}"
        )
    return float(matches[0][1])


def _parse_run(record):
    try:
        from scripts import compare_gapbs_cxl_amu_cira as comparison
    except ImportError:
        import compare_gapbs_cxl_amu_cira as comparison
    run_dir = Path(record["run_dir"])
    try:
        stats = comparison.parse_stats(run_dir / "stats.txt")
    except comparison.StatsError as error:
        raise calibration.CalibrationError(str(error)) from error
    raw = _require_file(record["raw"], "proxy checksum")
    payload = raw.read_bytes()
    if len(payload) != 8:
        raise calibration.CalibrationError(
            f"proxy checksum must contain 8 bytes: {raw}"
        )
    return {
        "roi_ticks": _stat_by_suffix(stats, "simTicks"),
        "checksum": f"{struct.unpack('<Q', payload)[0]:016x}",
        "metadata_accesses": _stat_by_suffix(
            stats, ".metadataAccesses", required=record["kind"] == "amu"
        ),
        "id_batch_refills": _stat_by_suffix(
            stats, ".idBatchRefills", required=record["kind"] == "amu"
        ),
        "completions": _stat_by_suffix(
            stats, ".completedLoads", required=record["kind"] == "amu"
        )
        + _stat_by_suffix(
            stats, ".completedStores", required=record["kind"] == "amu"
        ),
        "average_mlp": _stat_by_suffix(
            stats, ".avgOutstanding", required=record["kind"] == "amu"
        ),
    }


def _measurement_rows(plan):
    parsed = {}
    for record in plan["runs"]:
        parsed[(record["workload"], record["latency"], record["kind"])] = (
            _parse_run(record)
        )
    rows = []
    core_period_ticks = 1000 / 3
    for workload in WORKLOADS:
        normalizer_ticks = parsed[(workload, "0.1us", "baseline")]["roi_ticks"]
        normalizer_cycles = normalizer_ticks / core_period_ticks
        for latency in LATENCIES:
            baseline = parsed[(workload, latency, "baseline")]
            amu = parsed[(workload, latency, "amu")]
            latency_value = parse_latency(latency)
            target = calibration.AMU_TABLE4[workload][LATENCY_KEYS[latency_value]]
            rows.append(
                {
                    "workload": workload,
                    "latency_us": latency_value,
                    "simulated_normalized_time": amu["roi_ticks"] / normalizer_ticks,
                    "normalizer_cycles": normalizer_cycles,
                    "metadata_accesses": int(amu["metadata_accesses"]),
                    "id_batch_refills": int(amu["id_batch_refills"]),
                    "completions": int(amu["completions"]),
                    "target": target["amu"],
                    "paper_baseline_normalized": target["baseline"],
                    "weight": 1.0,
                    "average_mlp": amu["average_mlp"],
                    "baseline_checksum": baseline["checksum"],
                    "amu_checksum": amu["checksum"],
                }
            )
    return rows


def run_collect(options):
    plan = collect_plan(options)
    if options.dry_run:
        print(json.dumps(plan, indent=2, sort_keys=True))
        return 0
    _require_file(options.gem5, "gem5 binary")
    _require_file(options.config, "gem5 config")
    _require_file(options.m5_library, "m5 library")
    Path(plan["binary"]).parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(plan["build"], check=True)
    for record in plan["runs"]:
        run_dir = Path(record["run_dir"])
        run_dir.mkdir(parents=True, exist_ok=True)
        Path(record["raw"]).unlink(missing_ok=True)
        with (run_dir / "gem5.log").open("w", encoding="utf-8") as log:
            subprocess.run(
                record["command"], stdout=log, stderr=subprocess.STDOUT, check=True
            )
        _materialize_register_checksum(record)
    write_measurements(options.measurements, _measurement_rows(plan))
    print(f"AMU_PAPER_COLLECTION_PASS measurements={options.measurements}")
    return 0


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    collect = subparsers.add_parser("collect")
    collect.add_argument("--gem5", type=Path, required=True)
    collect.add_argument("--config", type=Path, required=True)
    collect.add_argument("--m5-library", type=Path, required=True)
    collect.add_argument("--cxx", default=os.environ.get("CXX", "g++"))
    collect.add_argument("--outdir", type=Path, required=True)
    collect.add_argument("--measurements", type=Path, required=True)
    collect.add_argument("--iterations", type=int, default=1)
    collect.add_argument("--dry-run", action="store_true")
    fit = subparsers.add_parser("fit")
    fit.add_argument("--measurements", type=Path, required=True)
    fit.add_argument("--pdf", type=Path, required=True)
    fit.add_argument("--cira-csv", type=Path, required=True)
    fit.add_argument("--holdout-workload", choices=tuple(calibration.AMU_TABLE4), required=True)
    fit.add_argument("--holdout-latency", type=parse_latency, required=True)
    fit.add_argument("--output", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv=None):
    options = parse_args(argv)
    if options.command == "collect":
        if options.iterations <= 0:
            raise calibration.CalibrationError("iterations must be positive")
        return run_collect(options)
    manifest = build_manifest(options)
    atomic_write_json(options.output, manifest)
    print(f"AMU_CIRA_CALIBRATION_PASS output={options.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
