#!/usr/bin/env python3
# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

"""Calibrate M2NDP's host link to a gem5 all-CXL 64-byte load."""

import argparse
import csv
import dataclasses
import hashlib
import json
import re
import shlex
import shutil
import subprocess
from decimal import Decimal, InvalidOperation
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = (
    REPO / "configs/example/gem5_library/x86-gapbs-amu-se.py"
)
PROBE_SOURCE = REPO / "util/m2ndp/cxl_latency_probe.c"
M5_LIBRARY = REPO / "util/m5/build/x86/out/libm5.a"
M5_INCLUDE = REPO / "include"
M2NDP_CONFIG_RELATIVE = Path("config/performance/M2NDP")
PACKET_PREFIX = "board.cache_hierarchy.membus.pktCount_"
BYTE_PREFIX = "board.cache_hierarchy.membus.pktSize_"
CXL_SUFFIX = "::board.cxl_mem_link0.cpu_side_port"


class CalibrationError(RuntimeError):
    """Raised when calibration evidence is absent or inconsistent."""


@dataclasses.dataclass(frozen=True)
class SearchSample:
    link_latency: int
    measured_ns: Decimal
    residual_ns: Decimal


@dataclasses.dataclass(frozen=True)
class SearchResult:
    target_ns: Decimal
    link_period_ns: Decimal
    link_latency: int
    measured_ns: Decimal
    samples: tuple


@dataclasses.dataclass(frozen=True)
class DerivedConfig:
    config_path: Path
    link_path: Path
    core_period_ns: Decimal
    link_period_ns: Decimal
    official_link_latency: int


@dataclasses.dataclass(frozen=True)
class Gem5ProbeEvidence:
    sim_ticks: int
    request_count: int
    response_count: int
    round_trip_packets: int
    request_bytes: int
    target_ns: Decimal


def _positive_decimal(value, name):
    try:
        result = Decimal(value)
    except (InvalidOperation, ValueError) as error:
        raise CalibrationError(f"{name} is not decimal") from error
    if not result.is_finite() or result <= 0:
        raise CalibrationError(f"{name} must be positive and finite")
    return result


def require_residual(*, target_ns, measured_ns, link_period_ns):
    target_ns = _positive_decimal(target_ns, "target latency")
    measured_ns = _positive_decimal(measured_ns, "measured latency")
    link_period_ns = _positive_decimal(link_period_ns, "link period")
    residual = abs(target_ns - measured_ns)
    if residual > link_period_ns:
        raise CalibrationError(
            f"residual {residual} ns is outside one link clock "
            f"({link_period_ns} ns)"
        )
    return residual


def search_link_latency(
    *,
    target_ns,
    link_period_ns,
    simulate,
    low,
    high,
):
    target_ns = _positive_decimal(target_ns, "target latency")
    link_period_ns = _positive_decimal(link_period_ns, "link period")
    if low < 0 or high < low:
        raise CalibrationError("invalid link-latency search bounds")

    by_latency = {}

    def sample(link_latency):
        if link_latency not in by_latency:
            measured = _positive_decimal(
                simulate(link_latency), "M2NDP measured latency"
            )
            by_latency[link_latency] = SearchSample(
                link_latency,
                measured,
                abs(measured - target_ns),
            )
        return by_latency[link_latency]

    left = int(low)
    right = int(high)
    while left <= right:
        midpoint = (left + right) // 2
        observation = sample(midpoint)
        if observation.measured_ns < target_ns:
            left = midpoint + 1
        elif observation.measured_ns > target_ns:
            right = midpoint - 1
        else:
            left = midpoint
            right = midpoint
            break

    for candidate in {left, right, left - 1, right + 1}:
        if low <= candidate <= high:
            sample(int(candidate))

    samples = tuple(by_latency[key] for key in sorted(by_latency))
    best = min(
        samples,
        key=lambda item: (item.residual_ns, item.link_latency),
    )
    require_residual(
        target_ns=target_ns,
        measured_ns=best.measured_ns,
        link_period_ns=link_period_ns,
    )
    return SearchResult(
        target_ns,
        link_period_ns,
        best.link_latency,
        best.measured_ns,
        samples,
    )


def _single_key(lines, key, separator):
    found = []
    for index, line in enumerate(lines):
        body = line.split("//", 1)[0].strip()
        if separator not in body:
            continue
        name, value = body.split(separator, 1)
        if name.strip() == key:
            found.append((index, value.strip()))
    if len(found) != 1:
        raise CalibrationError(
            f"{key} entry count is {len(found)}, expected 1"
        )
    return found[0]


def _replace_assignment(path, key, value, *, separator="="):
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    index, _ = _single_key(lines, key, separator)
    line = lines[index]
    newline = "\n" if line.endswith("\n") else ""
    content = line[:-1] if newline else line
    comment = ""
    if "//" in content:
        content, suffix = content.split("//", 1)
        comment = "//" + suffix
    indent = content[: len(content) - len(content.lstrip())]
    comment = f" {comment}" if comment else ""
    lines[index] = (
        f"{indent}{key}{separator}{value}{comment}{newline}"
    )
    path.write_text("".join(lines), encoding="utf-8")


def _replace_link_latency(path, value):
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    index, old_value = _single_key(lines, "link_latency", "=")
    try:
        official = int(old_value.rstrip(";").strip())
    except ValueError as error:
        raise CalibrationError("link_latency is not an integer") from error
    line = lines[index]
    newline = "\n" if line.endswith("\n") else ""
    content = line[:-1] if newline else line
    comment = ""
    if "//" in content:
        _, suffix = content.split("//", 1)
        comment = " // " + suffix.strip()
    indent = content[: len(content) - len(content.lstrip())]
    lines[index] = (
        f"{indent}link_latency = {int(value)};{comment}{newline}"
    )
    path.write_text("".join(lines), encoding="utf-8")
    return official


def _parse_frequencies(config_path):
    lines = config_path.read_text(encoding="utf-8").splitlines()
    _, value = _single_key(lines, "freq", "=")
    components = [part.strip() for part in value.split(",")]
    if len(components) < 4:
        raise CalibrationError("freq requires at least four components")
    try:
        core_mhz = Decimal(components[0])
        link_mhz = Decimal(components[3])
    except InvalidOperation as error:
        raise CalibrationError("freq contains a non-decimal component") from error
    if core_mhz <= 0 or link_mhz <= 0:
        raise CalibrationError("freq components must be positive")
    return Decimal(1000) / core_mhz, Decimal(1000) / link_mhz


def _validate_copied_paths(config_path):
    lines = config_path.read_text(encoding="utf-8").splitlines()
    for key in (
        "ramulator_config",
        "cxl_link_config",
        "local_cross_bar_config",
    ):
        _, value = _single_key(lines, key, "=")
        relative = Path(value)
        if relative.is_absolute():
            raise CalibrationError(f"{key} must remain a relative path")
        resolved = config_path.parent / relative
        if not resolved.is_file():
            raise CalibrationError(f"{key} target is missing: {resolved}")


def derive_config(source_dir, target_dir, *, link_latency):
    source_dir = Path(source_dir)
    target_dir = Path(target_dir)
    if not source_dir.is_dir():
        raise CalibrationError(f"M2NDP config directory is missing: {source_dir}")
    if target_dir.exists():
        raise CalibrationError(f"derived config directory already exists: {target_dir}")
    shutil.copytree(source_dir, target_dir)
    config_path = target_dir / "m2ndp.config"
    link_path = target_dir / "cxl_link.icnt"
    for path in (config_path, link_path):
        if not path.is_file():
            raise CalibrationError(f"required M2NDP config is missing: {path}")
    _replace_assignment(config_path, "max_kernel_launch", "128")
    official = _replace_link_latency(link_path, link_latency)
    _validate_copied_paths(config_path)
    core_period_ns, link_period_ns = _parse_frequencies(config_path)
    return DerivedConfig(
        config_path,
        link_path,
        core_period_ns,
        link_period_ns,
        official,
    )


def set_link_latency(link_path, value):
    _replace_link_latency(Path(link_path), int(value))


def parse_m2ndp_probe(
    text,
    *,
    returncode,
    expected_request_bytes,
    core_period_ns,
):
    if returncode != 0:
        raise CalibrationError(
            f"M2NDP probe exit status {returncode}, expected 0"
        )
    marker = re.findall(
        r"^M2NDP_CXL_PROBE request_bytes=(\d+) requests=(\d+)\s*$",
        text,
        flags=re.MULTILINE,
    )
    if len(marker) != 1:
        raise CalibrationError(
            f"M2NDP probe marker count is {len(marker)}, expected 1"
        )
    request_bytes, requests = (int(value) for value in marker[0])
    if request_bytes != expected_request_bytes:
        raise CalibrationError(
            f"M2NDP probe request size is {request_bytes}, "
            f"expected {expected_request_bytes}"
        )
    if requests != 1:
        raise CalibrationError(
            f"M2NDP probe request count is {requests}, expected 1"
        )
    latency = re.findall(
        r"\bMemory request latency:\s*(\d+)\s*$",
        text,
        flags=re.MULTILINE,
    )
    if len(latency) != 1:
        raise CalibrationError(
            f"M2NDP latency marker count is {len(latency)}, expected 1"
        )
    cycles = int(latency[0])
    if cycles <= 0:
        raise CalibrationError("M2NDP probe latency must be positive")
    return Decimal(cycles) * _positive_decimal(
        core_period_ns, "M2NDP core period"
    )


def _parse_first_stats_section(text):
    sections = re.findall(
        r"^-+ Begin Simulation Statistics -+\s*$"
        r"(.*?)"
        r"^-+ End Simulation Statistics\s+-+\s*$",
        text,
        flags=re.MULTILINE | re.DOTALL,
    )
    if len(sections) != 1:
        raise CalibrationError(
            f"gem5 stats section count is {len(sections)}, expected 1"
        )
    stats = {}
    for line in sections[0].splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        try:
            stats[parts[0]] = Decimal(parts[1])
        except InvalidOperation:
            continue
    return stats


def _single_directional(stats, prefix):
    found = [
        (name, value)
        for name, value in stats.items()
        if name.startswith(prefix) and name.endswith(CXL_SUFFIX)
    ]
    if len(found) != 1:
        raise CalibrationError(
            f"gem5 directional {prefix} statistic count is "
            f"{len(found)}, expected 1"
        )
    return found[0][1]


def parse_gem5_probe_stats(text):
    stats = _parse_first_stats_section(text)
    sim_ticks = stats.get("simTicks")
    if sim_ticks is None or sim_ticks != sim_ticks.to_integral_value():
        raise CalibrationError("gem5 simTicks is missing or non-integral")
    sim_ticks = int(sim_ticks)
    if sim_ticks <= 0:
        raise CalibrationError("gem5 simTicks must be positive")
    round_trip_packets = _single_directional(stats, PACKET_PREFIX)
    request_bytes = _single_directional(stats, BYTE_PREFIX)
    request_count = sum(
        value
        for name, value in stats.items()
        if name.startswith("board.cache_hierarchy.membus.transDist::Read")
        and name.endswith("Req")
    )
    response_count = stats.get(
        "board.cache_hierarchy.membus.transDist::ReadResp",
        Decimal(0),
    )
    if request_count != 1:
        raise CalibrationError(
            f"gem5 read request count is {request_count}, expected 1"
        )
    if response_count != 1:
        raise CalibrationError(
            f"gem5 read response count is {response_count}, expected 1"
        )
    if round_trip_packets != 2:
        raise CalibrationError(
            "gem5 directional round-trip packet count is "
            f"{round_trip_packets}, expected 2"
        )
    if request_bytes != 64:
        raise CalibrationError(
            f"gem5 directional request bytes is {request_bytes}, expected 64"
        )
    return Gem5ProbeEvidence(
        sim_ticks=sim_ticks,
        request_count=int(request_count),
        response_count=int(response_count),
        round_trip_packets=int(round_trip_packets),
        request_bytes=int(request_bytes),
        target_ns=Decimal(sim_ticks) / Decimal(1000),
    )


def _run(command, *, cwd=None, log_path=None):
    print("+", shlex.join(str(item) for item in command), flush=True)
    completed = subprocess.run(
        [str(item) for item in command],
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    if log_path is not None:
        Path(log_path).write_text(completed.stdout, encoding="utf-8")
    return completed


def _sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_config_tree(root):
    root = Path(root)
    digest = hashlib.sha256()
    files = [path for path in sorted(root.rglob("*")) if path.is_file()]
    if not files:
        raise CalibrationError(f"configuration tree is empty: {root}")
    for path in files:
        relative = path.relative_to(root).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(8, "little"))
        digest.update(relative)
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    return digest.hexdigest()


def build_gem5_probe(output, *, cc="gcc", m5_library=M5_LIBRARY):
    m5_library = Path(m5_library)
    if not m5_library.is_file():
        raise CalibrationError(f"m5 library is missing: {m5_library}")
    command = [
        cc,
        "-std=c11",
        "-O2",
        "-Wall",
        "-Wextra",
        "-static",
        "-no-pie",
        "-I",
        M5_INCLUDE,
        PROBE_SOURCE,
        m5_library,
        "-o",
        output,
    ]
    completed = _run(command)
    if completed.returncode != 0:
        raise CalibrationError(
            f"probe compilation failed:\n{completed.stdout}"
        )


def run_gem5_probe(*, gem5, binary, outdir, cxl_delay):
    gem5_out = Path(outdir) / "gem5"
    gem5_out.mkdir(parents=True, exist_ok=True)
    command = [
        gem5,
        "-d",
        gem5_out,
        DEFAULT_CONFIG,
        "--binary",
        binary,
        "--arguments",
        "",
        "--scale",
        "1",
        "--iterations",
        "1",
        "--measure-trial",
        "0",
        "--cores",
        "1",
        "--cpu",
        "timing",
        "--cxl-memory",
        "--cxl-link-delay",
        cxl_delay,
        "--cxl-link-req-size",
        "64",
        "--cxl-link-resp-size",
        "64",
        "--roi-work-events",
        "--no-asmc",
        "--disable-hw-prefetchers",
    ]
    completed = _run(
        command,
        cwd=REPO,
        log_path=Path(outdir) / "gem5.log",
    )
    if completed.returncode != 0:
        raise CalibrationError(
            f"gem5 probe exit status {completed.returncode}, expected 0"
        )
    return parse_gem5_probe_stats(
        (gem5_out / "stats.txt").read_text(encoding="utf-8")
    )


def _write_samples(path, samples):
    with Path(path).open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(
            stream,
            fieldnames=("link_latency", "measured_ns", "residual_ns"),
        )
        writer.writeheader()
        for sample in samples:
            writer.writerow(
                {
                    "link_latency": sample.link_latency,
                    "measured_ns": str(sample.measured_ns),
                    "residual_ns": str(sample.residual_ns),
                }
            )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gem5", type=Path, required=True)
    parser.add_argument("--m2ndp-root", type=Path, required=True)
    parser.add_argument("--m2ndp-tools", type=Path, required=True)
    parser.add_argument("--outdir", type=Path, required=True)
    parser.add_argument("--cxl-delay", default="1us")
    parser.add_argument("--cc", default="gcc")
    parser.add_argument("--m5-library", type=Path, default=M5_LIBRARY)
    parser.add_argument("--search-low", type=int, default=2)
    parser.add_argument("--search-high", type=int, default=65536)
    args = parser.parse_args()

    outdir = args.outdir.resolve()
    outdir.mkdir(parents=True, exist_ok=True)
    probe_binary = outdir / "cxl_latency_probe"
    build_gem5_probe(
        probe_binary,
        cc=args.cc,
        m5_library=args.m5_library.resolve(),
    )
    gem5_evidence = run_gem5_probe(
        gem5=args.gem5.resolve(),
        binary=probe_binary,
        outdir=outdir,
        cxl_delay=args.cxl_delay,
    )

    source_config_dir = (
        args.m2ndp_root.resolve() / M2NDP_CONFIG_RELATIVE
    )
    source_config_sha256 = _sha256(source_config_dir / "m2ndp.config")
    source_link_sha256 = _sha256(source_config_dir / "cxl_link.icnt")
    derived = derive_config(
        source_config_dir,
        outdir / "config",
        link_latency=0,
    )
    timing_probe = args.m2ndp_tools.resolve() / "bin/M2NDPCXLProbe"
    if not timing_probe.is_file():
        raise CalibrationError(f"M2NDP timing probe is missing: {timing_probe}")
    sample_logs = outdir / "probe_logs"
    sample_logs.mkdir()

    def simulate(link_latency):
        set_link_latency(derived.link_path, link_latency)
        completed = _run(
            [
                timing_probe,
                "--config",
                derived.config_path,
                "--num_reqs",
                "1",
                "--request_bytes",
                "64",
            ],
            cwd=args.m2ndp_root.resolve(),
            log_path=sample_logs / f"link_latency_{link_latency}.log",
        )
        return parse_m2ndp_probe(
            completed.stdout,
            returncode=completed.returncode,
            expected_request_bytes=64,
            core_period_ns=derived.core_period_ns,
        )

    result = search_link_latency(
        target_ns=gem5_evidence.target_ns,
        link_period_ns=derived.link_period_ns,
        simulate=simulate,
        low=args.search_low,
        high=args.search_high,
    )
    set_link_latency(derived.link_path, result.link_latency)
    _write_samples(outdir / "samples.csv", result.samples)
    residual = require_residual(
        target_ns=result.target_ns,
        measured_ns=result.measured_ns,
        link_period_ns=result.link_period_ns,
    )
    payload = {
        "schema": 1,
        "passed": True,
        "request_bytes": 64,
        "request_count": gem5_evidence.request_count,
        "response_count": gem5_evidence.response_count,
        "round_trip_packets": gem5_evidence.round_trip_packets,
        "cxl_delay": args.cxl_delay,
        "target_sim_ticks": gem5_evidence.sim_ticks,
        "target_ns": str(result.target_ns),
        "measured_ns": str(result.measured_ns),
        "measured_core_cycles": str(
            result.measured_ns / derived.core_period_ns
        ),
        "residual_ns": str(residual),
        "core_period_ns": str(derived.core_period_ns),
        "link_period_ns": str(derived.link_period_ns),
        "official_link_latency": derived.official_link_latency,
        "selected_link_latency": result.link_latency,
        "config_sha256": sha256_config_tree(derived.config_path.parent),
        "source_m2ndp_config_sha256": source_config_sha256,
        "source_cxl_link_config_sha256": source_link_sha256,
        "derived_m2ndp_config_sha256": _sha256(derived.config_path),
        "derived_cxl_link_config_sha256": _sha256(derived.link_path),
        "gem5_binary_sha256": _sha256(args.gem5.resolve()),
        "m2ndp_probe_sha256": _sha256(timing_probe),
    }
    (outdir / "calibration.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(payload, indent=2, sort_keys=True))


if __name__ == "__main__":
    try:
        main()
    except (CalibrationError, OSError) as error:
        raise SystemExit(f"calibration failed: {error}") from error
