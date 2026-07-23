#!/usr/bin/env python3
# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

"""Validate canonical scale-20 GAPBS CXL/AMU/CIRA evidence."""

import argparse
import configparser
import csv
import json
import os
import re
import shlex
import tempfile
from collections import namedtuple
from decimal import Decimal, InvalidOperation
from io import StringIO
from pathlib import Path


EXPECTED_LATENCIES = {
    "200ns": 200_000,
    "500ns": 500_000,
    "1us": 1_000_000,
    "2us": 2_000_000,
}
EXPECTED_BENCHMARKS = ("bfs", "bc", "pr", "sssp")
EXPECTED_LABEL_KINDS = (
    ("cxl_vanilla", "baseline"),
    ("amu", "amu"),
    ("cira_pgo", "cira"),
)
CXL_PACKET_STAT_PREFIX = "board.cache_hierarchy.membus.pktCount_"
CXL_BYTE_STAT_PREFIX = "board.cache_hierarchy.membus.pktSize_"
CXL_STAT_SUFFIX = "::board.cxl_mem_link0.cpu_side_port"
CPU_SWITCH_MARKER = "Switching from fast-forward CPU to timing CPU!"
CACHE_SUMMARY_STATS = {
    "l1d_demand_misses": (
        "board.cache_hierarchy.l1d-cache-0.demandMisses::"
        "processor.switch.core.data"
    ),
    "l2d_demand_hits": (
        "board.cache_hierarchy.l2-cache-0.demandHits::"
        "processor.switch.core.data"
    ),
    "l2d_demand_misses": (
        "board.cache_hierarchy.l2-cache-0.demandMisses::"
        "processor.switch.core.data"
    ),
    "l2i_demand_hits": (
        "board.cache_hierarchy.l2-cache-0.demandHits::"
        "processor.switch.core.inst"
    ),
    "l2i_demand_misses": (
        "board.cache_hierarchy.l2-cache-0.demandMisses::"
        "processor.switch.core.inst"
    ),
}
CACHE_FAMILY_TOTALS = {
    "l1d_demand_misses": (
        "board.cache_hierarchy.l1d-cache-0.demandMisses::total"
    ),
    "l2d_demand_hits": (
        "board.cache_hierarchy.l2-cache-0.demandHits::total"
    ),
    "l2d_demand_misses": (
        "board.cache_hierarchy.l2-cache-0.demandMisses::total"
    ),
    "l2i_demand_hits": (
        "board.cache_hierarchy.l2-cache-0.demandHits::total"
    ),
    "l2i_demand_misses": (
        "board.cache_hierarchy.l2-cache-0.demandMisses::total"
    ),
}
AMU_COUNTER_STATS = {
    "asmc_loads": "board.asmc.issuedLoads",
    "asmc_completed": "board.asmc.completedLoads",
}
CIRA_COUNTER_STATS = {
    "cira_prefetches": "board.cira.issuedPrefetches",
    "cira_completed": "board.cira.completedPrefetches",
    "cira_indexed_prefetches": "board.cira.issuedIndexedPrefetches",
    "cira_csr_prefetches": "board.cira.issuedCsrPrefetches",
    "cira_useful": "board.cira.usefulPrefetches",
    "cira_late": "board.cira.latePrefetches",
    "cira_read_packets": "board.cira.readPackets",
    "cira_read_bytes": "board.cira.readBytes",
}
CIRA_LATENCY_SUMMARY_STATS = {
    "cira_total_latency": "board.cira.totalLatency",
    "cira_avg_latency": "board.cira.avgLatency",
}
METADATA_EXPECTED = {
    "scale": "20",
    "iterations": "2",
    "measured_trial": "1",
    "fast_forward_cpu": "atomic",
    "roi_cpu": "timing",
    "cpu_switches": "1",
    "all_memory_cxl": "true",
}
REQUIRED_SUMMARY_FIELDS = {
    "benchmark",
    "label",
    "kind",
    "status",
    "verification",
    "scale",
    "iterations",
    "measured_trial",
    "fast_forward_cpu",
    "roi_cpu",
    "cpu_switches",
    "cxl_link_delay",
    "all_memory_cxl",
    "sim_ticks",
    "sim_insts",
    "speedup_vs_cxl",
    *AMU_COUNTER_STATS,
    *CIRA_COUNTER_STATS,
    "cxl_packets",
    "cxl_bytes",
    *CACHE_SUMMARY_STATS,
    *CIRA_LATENCY_SUMMARY_STATS,
    "run_dir",
}
ValidationResult = namedtuple(
    "ValidationResult", "row_count amu_rows cira_rows rows mode"
)


class ValidationError(RuntimeError):
    pass


def parse_first_stats_section(path):
    stats = {}
    in_section = False
    ended = False
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("---------- Begin Simulation Statistics"):
            if in_section:
                break
            in_section = True
            continue
        if in_section and line.startswith(
            "---------- End Simulation Statistics"
        ):
            ended = True
            break
        if not in_section:
            continue
        fields = line.split()
        if len(fields) < 2:
            continue
        try:
            stats[fields[0]] = Decimal(fields[1])
        except InvalidOperation:
            continue
    if not in_section:
        raise ValidationError(f"{path}: missing simulation statistics section")
    if not ended:
        raise ValidationError(
            f"{path}: missing End marker for first ROI stats section"
        )
    return stats


def require_decimal(mapping, name, context, *, integer=False, positive=False):
    try:
        raw = mapping[name]
    except KeyError as error:
        raise ValidationError(f"{context}: missing {name}") from error
    try:
        value = Decimal(raw)
    except InvalidOperation as error:
        raise ValidationError(
            f"{context}: invalid {name}={mapping.get(name)!r}"
        ) from error
    if not value.is_finite() or value < 0:
        raise ValidationError(
            f"{context}: {name} is not finite and nonnegative: {value}"
        )
    if integer and value != value.to_integral_value():
        raise ValidationError(f"{context}: {name} is not an integer: {value}")
    if positive and value <= 0:
        raise ValidationError(f"{context}: {name} must be positive: {value}")
    return value


def require_counter(stats, name, path):
    return int(require_decimal(stats, name, path, integer=True))


def require_summary_counter(row, name, context):
    return int(require_decimal(row, name, context, integer=True))


def require_summary_number(row, name, context):
    return require_decimal(row, name, context)


def require_directional_counter(stats, prefix, path):
    candidates = [
        (name, value)
        for name, value in stats.items()
        if name.startswith(prefix) and name.endswith(CXL_STAT_SUFFIX)
    ]
    if len(candidates) != 1:
        raise ValidationError(
            f"{path}: expected exactly one first-ROI statistic matching "
            f"{prefix}*{CXL_STAT_SUFFIX}; found {len(candidates)}"
        )
    name, _ = candidates[0]
    cell = name[len(prefix) : -len(CXL_STAT_SUFFIX)]
    return cell, require_counter(stats, name, path)


def require_directional_pair(stats, path):
    packet_cell, packets = require_directional_counter(
        stats, CXL_PACKET_STAT_PREFIX, path
    )
    byte_cell, byte_count = require_directional_counter(
        stats, CXL_BYTE_STAT_PREFIX, path
    )
    if packet_cell != byte_cell:
        raise ValidationError(
            f"{path}: CXL packet/byte directional identity mismatch: "
            f"{packet_cell!r} != {byte_cell!r}"
        )
    return packets, byte_count


def require_cache_counter(stats, field, path):
    requestor_stat = CACHE_SUMMARY_STATS[field]
    family_total = CACHE_FAMILY_TOTALS[field]
    family_prefix, expected_requestor = requestor_stat.split("::", 1)
    cache_prefix = family_prefix.rsplit(".", 1)[0] + "."
    cache_stats = [
        name for name in stats if name.startswith(cache_prefix)
    ]
    legacy = [
        name
        for name in cache_stats
        if "::processor.cores.core." in name
    ]
    if legacy:
        raise ValidationError(
            f"{path}: legacy cache requestor for {field}: "
            + ", ".join(sorted(legacy))
        )

    family_cell_prefix = family_prefix + "::"
    family_cells = {
        name: value
        for name, value in stats.items()
        if name.startswith(family_cell_prefix) and name != family_total
    }
    allowed_requestors = {
        "processor.switch.core.data",
        "processor.switch.core.inst",
    }
    unknown = [
        name
        for name in family_cells
        if name.split("::", 1)[1] not in allowed_requestors
    ]
    if unknown:
        if any(name.startswith(requestor_stat) for name in unknown):
            raise ValidationError(
                f"{path}: ambiguous timing switch requestor for {field}: "
                + ", ".join(sorted(unknown))
            )
        raise ValidationError(
            f"{path}: unknown cache requestor for {field}: "
            + ", ".join(sorted(unknown))
        )

    if family_total in stats:
        total = require_counter(stats, family_total, path)
        cell_sum = sum(
            require_counter(stats, name, path) for name in family_cells
        )
        if cell_sum != total:
            family_name = family_prefix.rsplit(".", 1)[1]
            raise ValidationError(
                f"{path}: {family_name} family total {total} does not match "
                f"requestor cells {cell_sum}"
            )
        if requestor_stat not in stats:
            return 0
        return require_counter(stats, requestor_stat, path)

    if family_cells:
        raise ValidationError(f"{path}: missing {family_total}")
    requestor_suffix = f"::{expected_requestor}"
    if not any(name.endswith(requestor_suffix) for name in cache_stats):
        raise ValidationError(
            f"{path}: missing exact switch requestor identity for "
            f"{field}: {expected_requestor}"
        )
    return 0


def parse_config(path):
    parser = configparser.ConfigParser(
        interpolation=None, strict=True, delimiters=("=",)
    )
    parser.optionxform = str
    try:
        with path.open(encoding="utf-8") as stream:
            parser.read_file(stream)
    except configparser.Error as error:
        raise ValidationError(f"{path}: invalid config.ini: {error}") from error
    return parser


def config_value(config, section, key, path):
    try:
        return config[section][key].strip()
    except KeyError as error:
        raise ValidationError(f"{path}: missing [{section}] {key}") from error


def validate_config(path, expected_delay, kind):
    config = parse_config(path)
    board_range = config_value(config, "board", "mem_ranges", path)
    link_range = config_value(
        config, "board.cxl_mem_link0", "ranges", path
    )
    dram_range = config_value(
        config, "board.memory.mem_ctrl.dram", "range", path
    )
    if not board_range or board_range != link_range or board_range != dram_range:
        raise ValidationError(
            f"{path}: range mismatch: board={board_range!r}, "
            f"CXL={link_range!r}, DRAM={dram_range!r}"
        )
    delay = config_value(config, "board.cxl_mem_link0", "delay", path)
    if delay != str(expected_delay):
        raise ValidationError(
            f"{path}: delay={delay}, expected {expected_delay}"
        )
    link_type = config_value(
        config, "board.cxl_mem_link0", "type", path
    )
    if link_type != "SerialLink":
        raise ValidationError(
            f"{path}: CXL link type={link_type!r}, expected SerialLink"
        )
    link_cpu_port = config_value(
        config, "board.cxl_mem_link0", "cpu_side_port", path
    )
    cpu_binding = re.fullmatch(
        r"board\.cache_hierarchy\.membus\.mem_side_ports\[(\d+)\]",
        link_cpu_port,
    )
    if cpu_binding is None:
        raise ValidationError(
            f"{path}: invalid CXL cpu_side_port binding: {link_cpu_port}"
        )
    link_mem_port = config_value(
        config, "board.cxl_mem_link0", "mem_side_port", path
    )
    if link_mem_port != "board.memory.mem_ctrl.port":
        raise ValidationError(
            f"{path}: invalid CXL mem_side_port binding: {link_mem_port}"
        )
    controller_port = config_value(
        config, "board.memory.mem_ctrl", "port", path
    )
    if controller_port != "board.cxl_mem_link0.mem_side_port":
        raise ValidationError(
            f"{path}: memory controller port bypasses CXL: "
            f"{controller_port}"
        )
    membus_ports = config_value(
        config, "board.cache_hierarchy.membus", "mem_side_ports", path
    ).split()
    cpu_index = int(cpu_binding.group(1))
    if any(
        "memory.mem_ctrl" in port or port == controller_port
        for port in membus_ports
    ):
        raise ValidationError(
            f"{path}: direct memory controller port on membus"
        )
    if (
        cpu_index >= len(membus_ports)
        or membus_ports[cpu_index]
        != "board.cxl_mem_link0.cpu_side_port"
    ):
        raise ValidationError(
            f"{path}: CXL cpu_side_port binding does not match "
            f"membus.mem_side_ports[{cpu_index}]"
        )
    start_type = config_value(
        config, "board.processor.start.core", "type", path
    )
    if start_type != "BaseAtomicSimpleCPU":
        raise ValidationError(
            f"{path}: starting CPU is {start_type}, expected Atomic"
        )
    switch_type = config_value(
        config, "board.processor.switch.core", "type", path
    )
    if switch_type != "BaseTimingSimpleCPU":
        raise ValidationError(
            f"{path}: switch CPU is {switch_type}, expected Timing"
        )
    workload_commands = [
        section["cmd"]
        for section in config.values()
        if "cmd" in section
    ]
    if not workload_commands:
        raise ValidationError(f"{path}: missing config workload command")
    for command in workload_commands:
        arguments = shlex.split(command)
        for option, expected, description in (
            ("-g", "20", "scale"),
            ("-n", "2", "iterations"),
        ):
            positions = [
                index
                for index, argument in enumerate(arguments)
                if argument == option
            ]
            values = [
                arguments[index + 1]
                for index in positions
                if index + 1 < len(arguments)
            ]
            if len(positions) != 1 or values != [expected]:
                raise ValidationError(
                    f"{path}: config workload {description} must be "
                    f"{option} {expected}: {command!r}"
                )
    if kind == "cira":
        target = config_value(
            config, "board.cira", "demand_probe_target", path
        )
        if target != "board.cache_hierarchy.l2-cache-0":
            raise ValidationError(
                f"{path}: demand_probe_target={target!r}, "
                "expected first private L2"
            )
        cira_port = config_value(
            config, "board.cira", "mem_side_port", path
        )
        cira_binding = re.fullmatch(
            r"board\.cache_hierarchy\.l2buses\.cpu_side_ports\[(\d+)\]",
            cira_port,
        )
        if cira_binding is None:
            raise ValidationError(
                f"{path}: CIRA mem_side_port={cira_port!r} "
                "must bind an l2buses.cpu_side_ports endpoint"
            )
        l2_cpu_ports = config_value(
            config,
            "board.cache_hierarchy.l2buses",
            "cpu_side_ports",
            path,
        ).split()
        cira_index = int(cira_binding.group(1))
        if (
            cira_index >= len(l2_cpu_ports)
            or l2_cpu_ports[cira_index] != "board.cira.mem_side_port"
        ):
            raise ValidationError(
                f"{path}: CIRA endpoint binding does not match "
                f"l2buses.cpu_side_ports[{cira_index}]"
            )
    return board_range


def parse_log(path):
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    switches = sum(line == CPU_SWITCH_MARKER for line in lines)
    verification = "missing"
    for line in lines:
        if "Verification: FAIL" in line:
            verification = "fail"
            break
        if "Verification: PASS" in line:
            verification = "pass"
    return switches, verification


def zero_or_blank(row, field, context):
    if row[field] == "":
        return
    if require_summary_number(row, field, context) != 0:
        raise ValidationError(
            f"{context}: non-owner {field} must be blank or zero"
        )


def validate_metadata(row, latency, context):
    for field, expected in METADATA_EXPECTED.items():
        actual = row[field].strip().lower()
        if actual != expected:
            raise ValidationError(
                f"{context}: {field}={row[field]!r}, expected {expected!r}"
            )
    if row["cxl_link_delay"] != latency:
        raise ValidationError(
            f"{context}: cxl_link_delay={row['cxl_link_delay']!r}, "
            f"expected {latency!r}"
        )


def validate_row(row, latency, expected_delay, expected_run_dir):
    context = f"{latency}/{row['benchmark']}/{row['label']}"
    if row["status"] != "ok" or row["verification"] != "pass":
        raise ValidationError(
            f"{context}: status={row['status']} "
            f"verification={row['verification']}"
        )
    validate_metadata(row, latency, context)
    run_dir = Path(row["run_dir"])
    if run_dir.resolve() != expected_run_dir.resolve():
        raise ValidationError(
            f"{context}: run_dir={run_dir} does not match {expected_run_dir}"
        )
    validate_config(run_dir / "config.ini", expected_delay, row["kind"])
    switches, raw_verification = parse_log(run_dir / "gem5.log")
    reported_switches = require_summary_counter(row, "cpu_switches", context)
    if switches != reported_switches:
        raise ValidationError(
            f"{context}: cpu_switches={reported_switches} != exact "
            f"switch marker count {switches}"
        )
    if raw_verification != row["verification"]:
        raise ValidationError(
            f"{context}: verification={row['verification']} != raw "
            f"{raw_verification}"
        )

    stats_path = run_dir / "stats.txt"
    stats = parse_first_stats_section(stats_path)
    sim_ticks = require_summary_counter(row, "sim_ticks", context)
    if sim_ticks <= 0:
        raise ValidationError(f"{context}: sim_ticks must be positive")
    raw_ticks = require_counter(stats, "simTicks", stats_path)
    if sim_ticks != raw_ticks:
        raise ValidationError(
            f"{context}: sim_ticks={sim_ticks} != first-ROI {raw_ticks}"
        )
    sim_insts = require_summary_counter(row, "sim_insts", context)
    raw_insts = require_counter(stats, "simInsts", stats_path)
    if sim_insts != raw_insts:
        raise ValidationError(
            f"{context}: sim_insts={sim_insts} != first-ROI {raw_insts}"
        )
    speedup = require_summary_number(row, "speedup_vs_cxl", context)
    if speedup <= 0:
        raise ValidationError(f"{context}: speedup_vs_cxl must be positive")

    packets, byte_count = require_directional_pair(stats, stats_path)
    for field, exact, unit_name in (
        ("cxl_packets", packets, "packet count"),
        ("cxl_bytes", byte_count, "byte count"),
    ):
        reported = require_summary_counter(row, field, context)
        if reported != exact:
            raise ValidationError(
                f"{context}: {field}={reported} != exact first-ROI "
                f"{unit_name} {exact}"
            )
    for field in CACHE_SUMMARY_STATS:
        reported = require_summary_counter(row, field, context)
        exact = require_cache_counter(stats, field, stats_path)
        if reported != exact:
            raise ValidationError(
                f"{context}: {field}={reported} != exact first-ROI "
                f"value {exact}"
            )

    kind = row["kind"]
    if kind == "amu":
        for field, stat_name in AMU_COUNTER_STATS.items():
            exact = require_counter(stats, stat_name, stats_path)
            reported = require_summary_counter(row, field, context)
            if reported != exact:
                raise ValidationError(
                    f"{context}: {field}={reported} != exact "
                    f"first-ROI value {exact}"
                )
        issued = require_summary_counter(row, "asmc_loads", context)
        completed = require_summary_counter(row, "asmc_completed", context)
        if issued <= 0 or issued != completed:
            raise ValidationError(
                f"{context}: issuedLoads={issued} != completedLoads="
                f"{completed} or counts are not positive"
            )
    else:
        for field in AMU_COUNTER_STATS:
            zero_or_blank(row, field, context)

    if kind == "cira":
        for field, stat_name in CIRA_COUNTER_STATS.items():
            exact = require_counter(stats, stat_name, stats_path)
            reported = require_summary_counter(row, field, context)
            if reported != exact:
                raise ValidationError(
                    f"{context}: {field}={reported} != exact "
                    f"first-ROI value {exact}"
                )
        issued = require_summary_counter(row, "cira_prefetches", context)
        completed = require_summary_counter(row, "cira_completed", context)
        if issued <= 0 or issued != completed:
            raise ValidationError(
                f"{context}: issuedPrefetches={issued} != "
                f"completedPrefetches={completed} or counts are not positive"
            )
        csr = require_summary_counter(row, "cira_csr_prefetches", context)
        if csr <= 0:
            raise ValidationError(
                f"{context}: cira_csr_prefetches={csr}, expected > 0"
            )
        for field, stat_name in CIRA_LATENCY_SUMMARY_STATS.items():
            if field == "cira_total_latency":
                reported = Decimal(
                    require_summary_counter(row, field, context)
                )
                exact = Decimal(require_counter(stats, stat_name, stats_path))
            else:
                reported = require_summary_number(row, field, context)
                exact = require_decimal(stats, stat_name, stats_path)
            if reported != exact:
                raise ValidationError(
                    f"{context}: {field}={reported} != exact "
                    f"first-ROI value {exact}"
                )
    else:
        for field in (*CIRA_COUNTER_STATS, *CIRA_LATENCY_SUMMARY_STATS):
            zero_or_blank(row, field, context)
    return {
        "latency": latency,
        **{field: row[field] for field in row},
    }


def read_summary(path):
    with path.open(newline="", encoding="utf-8") as stream:
        reader = csv.DictReader(stream)
        fields = list(reader.fieldnames or ())
        missing = sorted(REQUIRED_SUMMARY_FIELDS - set(fields))
        if missing:
            raise ValidationError(
                f"{path}: missing columns: {', '.join(missing)}"
            )
        return fields, list(reader)


def validate_speedups(rows, context):
    indexed = {
        (row["benchmark"], row["label"], row["kind"]): row for row in rows
    }
    for benchmark in sorted({row["benchmark"] for row in rows}):
        baseline = indexed[(benchmark, "cxl_vanilla", "baseline")]
        baseline_ticks = require_summary_counter(
            baseline, "sim_ticks", f"{context}/{benchmark}/cxl_vanilla"
        )
        for label, kind in (
            pair for pair in EXPECTED_LABEL_KINDS
            if (benchmark, *pair) in indexed
        ):
            row = indexed[(benchmark, label, kind)]
            ticks = require_summary_counter(
                row, "sim_ticks", f"{context}/{benchmark}/{label}"
            )
            reported = require_summary_number(
                row, "speedup_vs_cxl", f"{context}/{benchmark}/{label}"
            )
            exact = Decimal(baseline_ticks) / Decimal(ticks)
            if reported != exact:
                raise ValidationError(
                    f"{context}/{benchmark}/{label}: speedup mismatch: "
                    f"reported {reported}, recomputed {exact}"
                )


def validate_sweep(sweep_root):
    sweep_root = Path(sweep_root)
    validated = []
    amu_rows = 0
    cira_rows = 0
    for latency, expected_delay in EXPECTED_LATENCIES.items():
        summary = sweep_root / latency / "summary.csv"
        if not summary.is_file():
            raise ValidationError(f"{summary}: missing summary")
        _, rows = read_summary(summary)
        expected = [
            (benchmark, label, kind)
            for benchmark in EXPECTED_BENCHMARKS
            for label, kind in EXPECTED_LABEL_KINDS
        ]
        observed = [
            (row["benchmark"], row["label"], row["kind"]) for row in rows
        ]
        if len(rows) != 12 or sorted(observed) != sorted(expected):
            raise ValidationError(
                f"{summary}: expected exact cxl_vanilla/baseline, amu/amu, "
                "and cira_pgo/cira rows per workload"
            )
        validate_speedups(rows, latency)
        indexed = {
            (row["benchmark"], row["label"], row["kind"]): row for row in rows
        }
        for benchmark in EXPECTED_BENCHMARKS:
            for label, kind in EXPECTED_LABEL_KINDS:
                row = indexed[(benchmark, label, kind)]
                expected_run_dir = sweep_root / latency / benchmark / label
                validated.append(
                    validate_row(
                        row, latency, expected_delay, expected_run_dir
                    )
                )
                amu_rows += kind == "amu"
                cira_rows += kind == "cira"
    if len(validated) != 48 or amu_rows != 16 or cira_rows != 16:
        raise ValidationError(
            f"expected 48 rows, 16 AMU rows, and 16 CIRA rows; got "
            f"{len(validated)}, {amu_rows}, and {cira_rows}"
        )
    return ValidationResult(
        len(validated), amu_rows, cira_rows, validated, "full"
    )


def validate_pr_gate(root):
    root = Path(root)
    summary = root / "summary.csv"
    _, rows = read_summary(summary)
    expected = [
        ("pr", "cxl_vanilla", "baseline"),
        ("pr", "cira_pgo", "cira"),
    ]
    observed = [
        (row["benchmark"], row["label"], row["kind"]) for row in rows
    ]
    if len(rows) != 2 or sorted(observed) != sorted(expected):
        raise ValidationError(
            f"{summary}: PR gate requires exact baseline and cira_pgo rows"
        )
    validate_speedups(rows, "1us")
    indexed = {
        (row["benchmark"], row["label"], row["kind"]): row for row in rows
    }
    validated = []
    for label, kind in (EXPECTED_LABEL_KINDS[0], EXPECTED_LABEL_KINDS[2]):
        row = indexed[("pr", label, kind)]
        validated.append(
            validate_row(
                row,
                "1us",
                EXPECTED_LATENCIES["1us"],
                root / "pr" / label,
            )
        )
    baseline, cira = rows
    if baseline["kind"] != "baseline":
        baseline, cira = cira, baseline
    baseline_misses = require_summary_counter(
        baseline, "l2d_demand_misses", "PR gate baseline"
    )
    cira_misses = require_summary_counter(
        cira, "l2d_demand_misses", "PR gate CIRA"
    )
    useful = require_summary_counter(cira, "cira_useful", "PR gate CIRA")
    baseline_ticks = require_summary_counter(
        baseline, "sim_ticks", "PR gate baseline"
    )
    cira_ticks = require_summary_counter(cira, "sim_ticks", "PR gate CIRA")
    if baseline_misses <= 4096:
        raise ValidationError(
            "PR gate baseline l2d_demand_misses must exceed 4096"
        )
    if cira_misses >= baseline_misses:
        raise ValidationError(
            "PR gate CIRA l2d_demand_misses must be strictly lower"
        )
    if useful <= 0:
        raise ValidationError("PR gate cira_useful must be positive")
    if Decimal(baseline_ticks) / Decimal(cira_ticks) <= 1:
        raise ValidationError("PR gate baseline_ticks/cira_ticks must exceed 1")
    return ValidationResult(2, 0, 1, validated, "pr-gate")


def render_combined_csv(result):
    fields = []
    for row in result.rows:
        for field in row:
            if field not in fields:
                fields.append(field)
    buffer = StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    writer.writerows(result.rows)
    return buffer.getvalue()


def render_validation_json(result):
    payload = {
        "mode": result.mode,
        "row_count": result.row_count,
        "amu_rows": result.amu_rows,
        "cira_rows": result.cira_rows,
        "rows": result.rows,
    }
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def atomic_write(path, content, newline=None):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.tmp-", dir=path.parent
    )
    try:
        with os.fdopen(
            descriptor, "w", encoding="utf-8", newline=newline
        ) as stream:
            stream.write(content)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def transactional_write(outputs):
    staged = {}
    backups = {}
    installed = set()
    committed = False
    try:
        for path, content, newline in outputs:
            path = Path(path)
            path.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary = tempfile.mkstemp(
                prefix=f".{path.name}.tmp-", dir=path.parent
            )
            staged[path] = Path(temporary)
            with os.fdopen(
                descriptor, "w", encoding="utf-8", newline=newline
            ) as stream:
                stream.write(content)

        for path in staged:
            if not path.exists():
                backups[path] = None
                continue
            descriptor, backup = tempfile.mkstemp(
                prefix=f".{path.name}.tmp-backup-", dir=path.parent
            )
            os.close(descriptor)
            os.unlink(backup)
            os.replace(path, backup)
            backups[path] = Path(backup)

        for path, temporary in staged.items():
            os.replace(temporary, path)
            installed.add(path)
            staged[path] = None
        committed = True
    except BaseException:
        for path, backup in backups.items():
            if backup is not None:
                try:
                    path.unlink(missing_ok=True)
                    os.replace(backup, path)
                    backups[path] = None
                except OSError:
                    pass
            elif path in installed:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
        raise
    finally:
        for temporary in staged.values():
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
        for backup in backups.values():
            if backup is not None and committed:
                try:
                    backup.unlink(missing_ok=True)
                except OSError:
                    pass


def write_outputs(result, combined_output=None, validation_output=None):
    combined = render_combined_csv(result) if combined_output else None
    evidence = (
        render_validation_json(result) if validation_output else None
    )
    if combined_output and validation_output:
        transactional_write(
            (
                (combined_output, combined, ""),
                (validation_output, evidence, None),
            )
        )
    elif combined_output:
        atomic_write(combined_output, combined, newline="")
    elif validation_output:
        atomic_write(validation_output, evidence)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sweep_root", type=Path)
    parser.add_argument("--pr-gate", action="store_true")
    parser.add_argument("--combined-output", type=Path)
    parser.add_argument("--validation-output", type=Path)
    args = parser.parse_args()
    if args.pr_gate and args.combined_output:
        parser.error("--combined-output is only valid for a full sweep")
    try:
        result = (
            validate_pr_gate(args.sweep_root)
            if args.pr_gate
            else validate_sweep(args.sweep_root)
        )
        write_outputs(
            result,
            combined_output=args.combined_output,
            validation_output=args.validation_output,
        )
    except (OSError, KeyError, ValidationError) as error:
        parser.exit(1, f"FAIL: {error}\n")
    if args.pr_gate:
        print("PASS: PR@1us scale-20 CIRA discriminator")
    else:
        print(
            f"PASS: {result.row_count}/48 rows; canonical scale-20 "
            f"Atomic-to-Timing CXL evidence validated; "
            f"{result.amu_rows} AMU balances; "
            f"{result.cira_rows} CIRA balances"
        )


if __name__ == "__main__":
    main()
