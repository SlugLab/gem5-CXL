#!/usr/bin/env python3
# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

"""Collect AMU paper-profile proxies and emit a calibrated manifest."""

import argparse
import csv
import datetime
import json
import os
import platform
import re
import shlex
import socket
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
    value = _register_checksum(record)
    payload = struct.pack("<Q", value)
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
    return f"{value:016x}"


def _register_checksum(record):
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
    return (high << 32) | low


def _ini_sections(path):
    sections = {}
    current = None
    for lineno, line in enumerate(
        Path(path).read_text(encoding="utf-8").splitlines(), start=1
    ):
        if line.startswith("[") and line.endswith("]"):
            current = line[1:-1]
            if current in sections:
                raise calibration.CalibrationError(
                    f"duplicate config section {current}: {path}:{lineno}"
                )
            sections[current] = {}
        elif current is not None and "=" in line:
            key, value = line.split("=", 1)
            if key in sections[current]:
                raise calibration.CalibrationError(
                    f"duplicate config key {current}.{key}: {path}:{lineno}"
                )
            sections[current][key] = value
    return sections


def _gate_config(record, binary):
    config_path = Path(record["run_dir"]) / "config.ini"
    sections = _ini_sections(_require_file(config_path, "gate config"))
    links = [
        (name, values)
        for name, values in sections.items()
        if re.fullmatch(r"board\.cxl_mem_link\d+", name)
    ]
    if len(links) != 1 or links[0][1].get("delay") != "5000000":
        raise calibration.CalibrationError(
            f"GUPS gate requires exactly one 5us CXL link: {config_path}"
        )
    link_name, link = links[0]
    cores = [
        name
        for name in sections
        if re.fullmatch(r"board\.processor\.cores\d*\.core", name)
    ]
    if len(cores) != 1:
        raise calibration.CalibrationError(
            f"GUPS gate requires exactly one CPU core: {config_path}"
        )
    workloads = [
        values
        for name, values in sections.items()
        if re.fullmatch(
            r"board\.processor\.cores\d*\.core\.workload", name
        )
    ]
    executables = {values.get("executable") for values in workloads}
    expected_binary = str(Path(binary).resolve())
    if executables != {expected_binary}:
        raise calibration.CalibrationError(
            f"GUPS gate binary mismatch in {config_path}"
        )
    commands = [values.get("cmd", "") for values in workloads]
    try:
        command = shlex.split(commands[0]) if len(commands) == 1 else []
    except ValueError as error:
        raise calibration.CalibrationError(
            f"GUPS gate cannot parse workload command in {config_path}"
        ) from error
    workload_positions = [
        index for index, value in enumerate(command) if value == "--workload"
    ]
    if (
        len(workload_positions) != 1
        or workload_positions[0] + 1 >= len(command)
        or command[workload_positions[0] + 1] != "gups"
    ):
        raise calibration.CalibrationError(
            f"GUPS gate workload mismatch in {config_path}"
        )
    has_amu = "--amu" in command
    if has_amu != (record["kind"] == "amu"):
        raise calibration.CalibrationError(
            f"GUPS gate kind mismatch in {config_path}"
        )
    if record["kind"] == "amu":
        asmc = sections.get("board.asmc", {})
        adapter = sections.get("board.asmc_io_cache", {})
        membus = sections.get("board.cache_hierarchy.membus", {})
        if (
            asmc.get("mem_side_port") != "board.asmc_io_cache.cpu_side"
            or adapter.get("cpu_side") != "board.asmc.mem_side_port"
        ):
            raise calibration.CalibrationError(
                f"GUPS gate ASMC adapter topology mismatch in {config_path}"
            )
        adapter_match = re.fullmatch(
            r"board\.cache_hierarchy\.membus\.cpu_side_ports\[(\d+)\]",
            adapter.get("mem_side", ""),
        )
        membus_cpu = membus.get("cpu_side_ports", "").split()
        if (
            adapter_match is None
            or int(adapter_match.group(1)) >= len(membus_cpu)
            or membus_cpu[int(adapter_match.group(1))]
                != "board.asmc_io_cache.mem_side"
        ):
            raise calibration.CalibrationError(
                f"GUPS gate ASMC-to-membus topology mismatch in {config_path}"
            )
        link_match = re.fullmatch(
            r"board\.cache_hierarchy\.membus\.mem_side_ports\[(\d+)\]",
            link.get("cpu_side_port", ""),
        )
        membus_mem = membus.get("mem_side_ports", "").split()
        if (
            link_match is None
            or int(link_match.group(1)) >= len(membus_mem)
            or membus_mem[int(link_match.group(1))]
                != f"{link_name}.cpu_side_port"
        ):
            raise calibration.CalibrationError(
                f"GUPS gate membus-to-CXL topology mismatch in {config_path}"
            )
        device_match = re.fullmatch(
            r"(board\.cxl_device_xbar\d+)\.cpu_side_ports\[(\d+)\]",
            link.get("mem_side_port", ""),
        )
        if device_match is None:
            raise calibration.CalibrationError(
                f"GUPS gate CXL device topology mismatch in {config_path}"
            )
        device = sections.get(device_match.group(1), {})
        device_cpu = device.get("cpu_side_ports", "").split()
        device_index = int(device_match.group(2))
        if (
            device_index >= len(device_cpu)
            or device_cpu[device_index] != f"{link_name}.mem_side_port"
            or not device.get("mem_side_ports", "").split()
        ):
            raise calibration.CalibrationError(
                f"GUPS gate CXL-to-memory topology mismatch in {config_path}"
            )
    return config_path


def _gate_stat(stats, suffix):
    matches = [value for name, value in stats.items() if name.endswith(suffix)]
    if len(matches) != 1:
        raise calibration.CalibrationError(
            f"GUPS gate expected one stat ending {suffix}, got {len(matches)}"
        )
    return matches[0]


def _gate_stat_or_zero(stats, suffix):
    matches = [value for name, value in stats.items() if name.endswith(suffix)]
    if len(matches) > 1:
        raise calibration.CalibrationError(
            f"GUPS gate expected at most one stat ending {suffix}, "
            f"got {len(matches)}"
        )
    return matches[0] if matches else 0


def _gate_stats(record):
    try:
        from scripts import compare_gapbs_cxl_amu_cira as comparison
    except ImportError:
        import compare_gapbs_cxl_amu_cira as comparison
    stats_path = Path(record["run_dir"]) / "stats.txt"
    try:
        return comparison.parse_stats(stats_path)
    except comparison.StatsError as error:
        raise calibration.CalibrationError(str(error)) from error


def _execution_input_manifest(gem5, config, m5_library):
    inputs = {}
    for label, path in (
        ("gem5", gem5),
        ("config", config),
        ("m5_library", m5_library),
    ):
        resolved = _require_file(path, label.replace("_", " ")).resolve()
        inputs[label] = {
            "path": str(resolved),
            "sha256": calibration.sha256_file(resolved),
        }
    return inputs


def _git_provenance(repo=REPO):
    repo = Path(repo).resolve()

    def git_output(arguments, label):
        try:
            result = subprocess.run(
                ["git", "-C", str(repo), *arguments],
                check=True,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except (OSError, subprocess.CalledProcessError) as error:
            raise calibration.CalibrationError(
                f"cannot capture git {label} for {repo}"
            ) from error
        return result.stdout.strip()

    status = git_output(
        ["status", "--porcelain", "--untracked-files=all"], "status"
    )
    if status:
        raise calibration.CalibrationError(
            f"dirty worktree rejected: {repo}\n{status}"
        )
    commit = git_output(["rev-parse", "HEAD"], "commit")
    branch = git_output(["rev-parse", "--abbrev-ref", "HEAD"], "branch")
    if not re.fullmatch(r"[0-9a-f]{40}", commit) or not branch:
        raise calibration.CalibrationError(
            f"invalid git provenance for {repo}"
        )
    return {"commit": commit, "branch": branch, "clean": True}


def _collection_manifest_path(options):
    configured = getattr(options, "collection_manifest", None)
    if configured is not None:
        return Path(configured)
    return Path(options.outdir) / "collection_manifest.json"


def _reject_existing(path, label):
    path = Path(path)
    if os.path.lexists(path):
        raise calibration.CalibrationError(f"{label} already exists: {path}")


def _collection_input_manifest(options, proxy=None):
    inputs = {}
    sources = (
        ("gem5", options.gem5, "gem5 binary"),
        ("config", options.config, "gem5 config"),
        ("m5_library", options.m5_library, "m5 library"),
        ("amu_pdf", options.pdf, "AMU PDF"),
        ("cira_csv", options.cira_csv, "hardware CSV"),
    )
    if proxy is not None:
        sources = (*sources, ("proxy", proxy, "proxy binary"))
    for key, path, label in sources:
        resolved = _require_file(path, label).resolve()
        inputs[key] = {
            "path": str(resolved),
            "sha256": calibration.sha256_file(resolved),
        }
    return inputs


def _verify_collection_inputs(inputs):
    for key, frozen in inputs.items():
        if not isinstance(frozen, dict) or set(frozen) != {"path", "sha256"}:
            raise calibration.CalibrationError(
                f"invalid frozen collection input: {key}"
            )
        path = _require_file(frozen["path"], f"frozen {key}").resolve()
        if (
            str(path) != frozen["path"]
            or calibration.sha256_file(path) != frozen["sha256"]
        ):
            raise calibration.CalibrationError(
                f"collection input changed: {key}"
            )


def _verify_collection_manifest(path, frozen_sha256):
    path = Path(path)
    if (
        not path.is_file()
        or calibration.sha256_file(path) != frozen_sha256
    ):
        raise calibration.CalibrationError(
            f"in-progress collection manifest changed: {path}"
        )


def _utc_now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat().replace(
        "+00:00", "Z"
    )


def _collection_manifest(options, plan, git, inputs, manifest_path):
    uname = platform.uname()
    runs = [
        {
            "workload": record["workload"],
            "latency": record["latency"],
            "kind": record["kind"],
            "run_dir": str(Path(record["run_dir"]).resolve()),
            "argv": list(record["command"]),
        }
        for record in plan["runs"]
    ]
    return {
        "schema": "amu-paper-calibration-collection",
        "version": 1,
        "status": "in_progress",
        "git": git,
        "inputs": inputs,
        "plan": {
            "build_argv": list(plan["build"]),
            "runs": runs,
            "expected_simulations": len(WORKLOADS) * len(LATENCIES) * 2,
            "expected_measurement_rows": len(WORKLOADS) * len(LATENCIES),
        },
        "host": {
            "hostname": socket.gethostname(),
            "platform": f"{uname.system}-{uname.release}-{uname.machine}",
            "machine": uname.machine,
            "python": platform.python_version(),
        },
        "timestamps": {"started_utc": _utc_now()},
        "outputs": {
            "outdir": str(Path(options.outdir).resolve()),
            "measurements": str(Path(options.measurements).resolve()),
            "collection_manifest": str(Path(manifest_path).resolve()),
        },
        "actual": {"completed_simulations": 0, "measurement_rows": 0},
    }


def _verify_execution_input_manifest(inputs):
    required = {"gem5", "config", "m5_library"}
    if not isinstance(inputs, dict) or set(inputs) != required:
        raise calibration.CalibrationError(
            "GUPS gate execution-input manifest is incomplete"
        )
    for label in sorted(required):
        record = inputs[label]
        if not isinstance(record, dict) or set(record) != {"path", "sha256"}:
            raise calibration.CalibrationError(
                f"GUPS gate execution-input record is invalid: {label}"
            )
        path = _require_file(record["path"], label.replace("_", " ")).resolve()
        if str(path) != record["path"] or calibration.sha256_file(path) != record["sha256"]:
            raise calibration.CalibrationError(
                f"GUPS gate execution input changed: {label}"
            )


def validate_gups_gate(baseline, amu, binary, *, execution_inputs):
    _verify_execution_input_manifest(execution_inputs)
    binary = _require_file(binary, "gate binary").resolve()
    binary_hash = calibration.sha256_file(binary)
    evidence = {}
    checksums = {}
    for record in (baseline, amu):
        kind = record["kind"]
        if kind not in {"baseline", "amu"}:
            raise calibration.CalibrationError("invalid GUPS gate kind")
        config_path = _gate_config(record, binary)
        log_path = _require_file(
            Path(record["run_dir"]) / "gem5.log", "gate log"
        )
        log = log_path.read_text(encoding="utf-8", errors="strict")
        if (
            "Verification: PASS" not in log
            or "GAPBS_VERIFICATION_EXIT_CAUSE cause=m5_exit instruction encountered"
            not in log
        ):
            raise calibration.CalibrationError(
                f"GUPS gate verification marker missing: {log_path}"
            )
        raw_path = _require_file(record["raw"], "gate checksum")
        payload = raw_path.read_bytes()
        if len(payload) != 8:
            raise calibration.CalibrationError(
                f"GUPS gate checksum is not 8 bytes: {raw_path}"
            )
        raw_value = struct.unpack("<Q", payload)[0]
        register_value = _register_checksum(record)
        if raw_value != register_value:
            raise calibration.CalibrationError(
                f"GUPS gate raw/register checksum mismatch: {raw_path}"
            )
        checksums[kind] = raw_value
        stats_path = Path(record["run_dir"]) / "stats.txt"
        command_path = _require_file(
            Path(record["run_dir"]) / "command.txt", "gate command"
        )
        stats = _gate_stats(record)
        if _gate_stat(stats, "simTicks") <= 0:
            raise calibration.CalibrationError("GUPS gate ROI has no ticks")
        evidence[kind] = {
            "config_sha256": calibration.sha256_file(config_path),
            "stats_sha256": calibration.sha256_file(stats_path),
            "log_sha256": calibration.sha256_file(log_path),
            "command_sha256": calibration.sha256_file(command_path),
            "checksum_sha256": calibration.sha256_file(raw_path),
            "checksum": f"{raw_value:016x}",
        }
    if checksums["baseline"] != checksums["amu"]:
        raise calibration.CalibrationError(
            "GUPS gate baseline/AMU checksum mismatch"
        )

    stats = _gate_stats(amu)
    issued_loads = _gate_stat(stats, ".issuedLoads")
    issued_stores = _gate_stat(stats, ".issuedStores")
    completed_loads = _gate_stat(stats, ".completedLoads")
    completed_stores = _gate_stat(stats, ".completedStores")
    if (
        issued_loads != 65536
        or issued_stores != 65536
        or issued_loads != completed_loads
        or issued_stores != completed_stores
    ):
        raise calibration.CalibrationError(
            "GUPS gate issued/completed accounting mismatch"
        )
    integral = _gate_stat(stats, ".outstandingIntegral")
    occupancy_ticks = _gate_stat(stats, ".occupancyTicks")
    if occupancy_ticks <= 0:
        raise calibration.CalibrationError(
            "GUPS gate occupancyTicks must be positive"
        )
    average = float(integral / occupancy_ticks)
    reported_average = float(_gate_stat(stats, ".avgOutstanding"))
    if abs(average - reported_average) > max(abs(average), 1) * 1e-6:
        raise calibration.CalibrationError(
            "GUPS gate avgOutstanding does not match raw integral"
        )
    peak = _gate_stat(stats, ".maxObservedOutstanding")
    if average <= 130:
        raise calibration.CalibrationError(
            "GUPS 5us average outstanding must be greater than 130"
        )
    if peak > 256:
        raise calibration.CalibrationError(
            "GUPS 5us peak outstanding exceeds 256"
        )
    for suffix in (".rejectedQueueFull", ".rejectedSpmFull", ".translationFaults"):
        if _gate_stat(stats, suffix) != 0:
            raise calibration.CalibrationError(
                f"GUPS gate nonzero failure counter {suffix}"
            )
    if _gate_stat(stats, ".farSpmFlagPackets") != 0:
        raise calibration.CalibrationError(
            "GUPS gate observed SPM_ACCESS on the far route"
        )
    if _gate_stat(stats, ".spmMissingFlagPackets") != 0:
        raise calibration.CalibrationError(
            "GUPS gate observed an unflagged SPM packet"
        )
    expected_route_counts = {
        ".farReadPackets": issued_loads,
        ".farWritePackets": issued_stores,
        ".spmReadPackets": issued_loads + issued_stores,
        ".spmWritePackets": issued_loads,
    }
    for suffix, expected in expected_route_counts.items():
        observed = _gate_stat(stats, suffix)
        if observed != expected:
            raise calibration.CalibrationError(
                f"GUPS gate route count mismatch {suffix}: "
                f"expected {expected:g}, got {observed:g}"
            )
    read_uncacheable = _gate_stat(
        stats, ".asmc_io_cache.ReadReq.mshrUncacheable::asmc"
    )
    read_cached = sum(
        _gate_stat_or_zero(stats, suffix)
        for suffix in (
            ".asmc_io_cache.ReadReq.hits::asmc",
            ".asmc_io_cache.ReadReq.misses::asmc",
            ".asmc_io_cache.ReadReq.accesses::asmc",
        )
    )
    write_hits = _gate_stat_or_zero(
        stats, ".asmc_io_cache.WriteReq.hits::asmc"
    )
    write_misses = _gate_stat(
        stats, ".asmc_io_cache.WriteReq.misses::asmc"
    )
    write_accesses = _gate_stat(
        stats, ".asmc_io_cache.WriteReq.accesses::asmc"
    )
    if read_uncacheable != issued_loads or read_cached != 0:
        raise calibration.CalibrationError(
            "GUPS gate far adapter cached ReadReq traffic"
        )
    if (
        write_hits != 0
        or write_misses != issued_stores
        or write_accesses != issued_stores
    ):
        raise calibration.CalibrationError(
            "GUPS gate far adapter cached WriteReq traffic"
        )
    return {
        "schema": 1,
        "status": "PASS",
        "workload": "gups",
        "latency_ticks": 5000000,
        "cores": 1,
        "binary": str(binary),
        "binary_sha256": binary_hash,
        "execution_inputs": execution_inputs,
        "checksum": f"{checksums['amu']:016x}",
        "average_outstanding": float(average),
        "peak_outstanding": int(peak),
        "issued_loads": int(issued_loads),
        "issued_stores": int(issued_stores),
        "evidence": evidence,
    }


def run_gate(options):
    plan = collect_plan(options)
    plan["runs"] = [
        record
        for record in plan["runs"]
        if record["workload"] == "gups" and record["latency"] == "5us"
    ]
    if options.dry_run:
        print(json.dumps(plan, indent=2, sort_keys=True))
        return 0
    _require_file(options.gem5, "gem5 binary")
    _require_file(options.config, "gem5 config")
    _require_file(options.m5_library, "m5 library")
    if options.proof.exists():
        raise calibration.CalibrationError(
            f"GUPS gate proof already exists: {options.proof}"
        )
    if options.outdir.exists() and any(options.outdir.iterdir()):
        raise calibration.CalibrationError(
            f"GUPS gate output directory is not empty: {options.outdir}"
        )
    execution_inputs = _execution_input_manifest(
        options.gem5, options.config, options.m5_library
    )
    Path(plan["binary"]).parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(plan["build"], check=True)
    records = {}
    for record in plan["runs"]:
        run_dir = Path(record["run_dir"])
        run_dir.mkdir(parents=True, exist_ok=True)
        Path(record["raw"]).unlink(missing_ok=True)
        (run_dir / "command.txt").write_text(
            shlex.join(record["command"]) + "\n", encoding="utf-8"
        )
        with (run_dir / "gem5.log").open("w", encoding="utf-8") as log:
            subprocess.run(
                record["command"], stdout=log,
                stderr=subprocess.STDOUT, check=True
            )
        _materialize_register_checksum(record)
        records[record["kind"]] = record
    proof = validate_gups_gate(
        records["baseline"], records["amu"], plan["binary"],
        execution_inputs=execution_inputs,
    )
    atomic_write_json(options.proof, proof)
    print(
        "AMU_GUPS_5US_GATE_PASS "
        f"average_outstanding={proof['average_outstanding']:.6f} "
        f"peak={proof['peak_outstanding']} proof={options.proof}"
    )
    return 0


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

    git = _git_provenance()
    manifest_path = _collection_manifest_path(options)
    outdir = Path(options.outdir)
    measurements = Path(options.measurements)
    targets = {
        "evidence output directory": outdir,
        "measurements file": measurements,
        "collection manifest": manifest_path,
    }
    resolved_targets = [path.resolve() for path in targets.values()]
    if len(set(resolved_targets)) != len(resolved_targets):
        raise calibration.CalibrationError(
            "collection output paths must be distinct"
        )
    for label, path in targets.items():
        _reject_existing(path, label)

    prebuild_inputs = _collection_input_manifest(options)
    Path(plan["binary"]).parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(plan["build"], check=True)
    _verify_collection_inputs(prebuild_inputs)
    inputs = _collection_input_manifest(options, plan["binary"])
    manifest = _collection_manifest(
        options, plan, git, inputs, manifest_path
    )
    if len(plan["runs"]) != manifest["plan"]["expected_simulations"]:
        raise calibration.CalibrationError(
            "collection plan does not contain exactly 36 simulations"
        )
    atomic_write_json(manifest_path, manifest)
    frozen_manifest_sha256 = calibration.sha256_file(manifest_path)

    completed_runs = 0
    rows = []
    try:
        for record in plan["runs"]:
            run_dir = Path(record["run_dir"])
            run_dir.mkdir(parents=True, exist_ok=True)
            (run_dir / "command.txt").write_text(
                shlex.join(record["command"]) + "\n", encoding="utf-8"
            )
            with (run_dir / "gem5.log").open("w", encoding="utf-8") as log:
                subprocess.run(
                    record["command"], stdout=log,
                    stderr=subprocess.STDOUT, check=True
                )
            _materialize_register_checksum(record)
            _verify_collection_manifest(
                manifest_path, frozen_manifest_sha256
            )
            completed_runs += 1
        rows = _measurement_rows(plan)
        if len(rows) != manifest["plan"]["expected_measurement_rows"]:
            raise calibration.CalibrationError(
                "collection did not produce exactly 18 measurement rows"
            )
        _verify_collection_inputs(inputs)
        _verify_collection_manifest(manifest_path, frozen_manifest_sha256)
        write_measurements(measurements, rows)
        manifest["status"] = "complete"
        manifest["timestamps"]["completed_utc"] = _utc_now()
        manifest["actual"] = {
            "completed_simulations": completed_runs,
            "measurement_rows": len(rows),
        }
        atomic_write_json(manifest_path, manifest)
    except Exception as error:
        manifest["status"] = "failed"
        manifest["failure_reason"] = f"{type(error).__name__}: {error}"
        manifest["timestamps"]["failed_utc"] = _utc_now()
        manifest["actual"] = {
            "completed_simulations": completed_runs,
            "measurement_rows": len(rows),
        }
        try:
            atomic_write_json(manifest_path, manifest)
        except Exception:
            pass
        raise
    print(f"AMU_PAPER_COLLECTION_PASS measurements={options.measurements}")
    return 0


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    collect = subparsers.add_parser("collect")
    collect.add_argument("--gem5", type=Path, required=True)
    collect.add_argument("--config", type=Path, required=True)
    collect.add_argument("--m5-library", type=Path, required=True)
    collect.add_argument("--pdf", type=Path, required=True)
    collect.add_argument("--cira-csv", type=Path, required=True)
    collect.add_argument("--cxx", default=os.environ.get("CXX", "g++"))
    collect.add_argument("--outdir", type=Path, required=True)
    collect.add_argument("--measurements", type=Path, required=True)
    collect.add_argument("--collection-manifest", type=Path)
    collect.add_argument("--iterations", type=int, default=1)
    collect.add_argument("--dry-run", action="store_true")
    gate = subparsers.add_parser("gate")
    gate.add_argument("--gem5", type=Path, required=True)
    gate.add_argument("--config", type=Path, required=True)
    gate.add_argument("--m5-library", type=Path, required=True)
    gate.add_argument("--cxx", default=os.environ.get("CXX", "g++"))
    gate.add_argument("--outdir", type=Path, required=True)
    gate.add_argument("--proof", type=Path, required=True)
    gate.add_argument("--iterations", type=int, default=1)
    gate.add_argument("--dry-run", action="store_true")
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
    if options.command in {"collect", "gate"}:
        if options.iterations <= 0:
            raise calibration.CalibrationError("iterations must be positive")
    if options.command == "gate":
        return run_gate(options)
    if options.command == "collect":
        return run_collect(options)
    manifest = build_manifest(options)
    atomic_write_json(options.output, manifest)
    print(f"AMU_CIRA_CALIBRATION_PASS output={options.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
