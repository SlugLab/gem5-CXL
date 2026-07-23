#!/usr/bin/env python3
# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

"""Validate a four-latency GAPBS baseline/AMU/CIRA sweep."""

import argparse
import csv
import math
import re
from collections import namedtuple
from decimal import Decimal, InvalidOperation
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
CACHE_SUMMARY_STATS = {
    "l1d_demand_misses": (
        "board.cache_hierarchy.l1d-cache-0.demandMisses::total"
    ),
    "l2d_demand_hits": (
        "board.cache_hierarchy.l2-cache-0.demandHits::"
        "processor.cores.core.data"
    ),
    "l2d_demand_misses": (
        "board.cache_hierarchy.l2-cache-0.demandMisses::"
        "processor.cores.core.data"
    ),
    "l2i_demand_hits": (
        "board.cache_hierarchy.l2-cache-0.demandHits::"
        "processor.cores.core.inst"
    ),
    "l2i_demand_misses": (
        "board.cache_hierarchy.l2-cache-0.demandMisses::"
        "processor.cores.core.inst"
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
CIRA_LATENCY_SUMMARY_STATS = {
    "cira_total_latency": "board.cira.totalLatency",
    "cira_avg_latency": "board.cira.avgLatency",
}
REQUIRED_SUMMARY_FIELDS = {
    "benchmark",
    "label",
    "kind",
    "status",
    "verification",
    "sim_ticks",
    "sim_insts",
    "speedup_vs_cxl",
    "asmc_loads",
    "cira_prefetches",
    "cira_indexed_prefetches",
    "cira_csr_prefetches",
    "cira_completed",
    "cxl_packets",
    "cxl_bytes",
    "l1d_demand_misses",
    "l2d_demand_hits",
    "l2d_demand_misses",
    "l2i_demand_hits",
    "l2i_demand_misses",
    "cira_total_latency",
    "cira_avg_latency",
    "run_dir",
}
ValidationResult = namedtuple(
    "ValidationResult", "row_count amu_rows cira_rows"
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
        if in_section and line.startswith("---------- End Simulation Statistics"):
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


def require_counter(stats, name, path):
    if name not in stats:
        raise ValidationError(f"{path}: missing {name} in first ROI stats section")
    value = stats[name]
    if not value.is_finite() or value != value.to_integral_value():
        raise ValidationError(f"{path}: {name} is not an integer: {value}")
    return int(value)


def require_summary_counter(row, name, context):
    try:
        value = Decimal(row[name])
    except (KeyError, InvalidOperation) as error:
        raise ValidationError(
            f"{context}: invalid {name}={row.get(name)!r}"
        ) from error
    if not value.is_finite() or value != value.to_integral_value() or value < 0:
        raise ValidationError(
            f"{context}: {name} is not a nonnegative integer: {value}"
        )
    return int(value)


def require_stat_number(stats, name, path):
    if name not in stats:
        raise ValidationError(f"{path}: missing {name} in first ROI stats section")
    value = stats[name]
    if not value.is_finite() or value < 0:
        raise ValidationError(
            f"{path}: {name} is not finite and nonnegative: {value}"
        )
    return value


def require_summary_number(row, name, context):
    try:
        value = Decimal(row[name])
    except (KeyError, InvalidOperation) as error:
        raise ValidationError(
            f"{context}: invalid {name}={row.get(name)!r}"
        ) from error
    if not value.is_finite() or value < 0:
        raise ValidationError(
            f"{context}: {name} is not finite and nonnegative: {value}"
        )
    return value


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
    family_total = CACHE_FAMILY_TOTALS[field]
    require_counter(stats, family_total, path)
    requestor_stat = CACHE_SUMMARY_STATS[field]
    if requestor_stat not in stats:
        return 0
    return require_counter(stats, requestor_stat, path)


def configured_delay(path):
    text = path.read_text(encoding="utf-8")
    match = re.search(
        r"^\[board\.cxl_mem_link0\]\n(?:(?!^\[).)*?^delay=(\d+)$",
        text,
        flags=re.MULTILINE | re.DOTALL,
    )
    if not match:
        raise ValidationError(f"{path}: missing board.cxl_mem_link0 delay")
    return int(match.group(1))


def validate_sweep(sweep_root):
    sweep_root = Path(sweep_root)
    row_count = 0
    amu_rows = 0
    cira_rows = 0
    for latency, expected_delay in EXPECTED_LATENCIES.items():
        summary = sweep_root / latency / "summary.csv"
        if not summary.is_file():
            raise ValidationError(f"{summary}: missing summary")
        with summary.open(newline="", encoding="utf-8") as stream:
            reader = csv.DictReader(stream)
            fields = set(reader.fieldnames or ())
            missing = sorted(REQUIRED_SUMMARY_FIELDS - fields)
            if missing:
                raise ValidationError(
                    f"{summary}: missing columns: {', '.join(missing)}"
                )
            rows = list(reader)
        if len(rows) != 12:
            raise ValidationError(f"{summary}: expected 12 rows, found {len(rows)}")

        observed = [
            (row["benchmark"], row["label"], row["kind"]) for row in rows
        ]
        expected = [
            (benchmark, label, kind)
            for benchmark in EXPECTED_BENCHMARKS
            for label, kind in EXPECTED_LABEL_KINDS
        ]
        if sorted(observed) != sorted(expected):
            raise ValidationError(
                f"{summary}: expected exact cxl_vanilla/baseline, amu/amu, "
                "and cira_pgo/cira rows per workload"
            )

        for row in rows:
            row_count += 1
            context = f"{latency}/{row['benchmark']}/{row['label']}"
            if row["status"] != "ok" or row["verification"] != "pass":
                raise ValidationError(
                    f"{context}: status={row['status']} "
                    f"verification={row['verification']}"
                )
            try:
                speedup = float(row["speedup_vs_cxl"])
            except ValueError as error:
                raise ValidationError(
                    f"{context}: nonnumeric speedup {row['speedup_vs_cxl']!r}"
                ) from error
            if not math.isfinite(speedup):
                raise ValidationError(f"{context}: non-finite speedup {speedup}")

            run_dir = sweep_root / latency / row["benchmark"] / row["label"]
            delay = configured_delay(run_dir / "config.ini")
            if delay != expected_delay:
                raise ValidationError(
                    f"{context}: delay={delay}, expected {expected_delay}"
                )
            stats_path = run_dir / "stats.txt"
            stats = parse_first_stats_section(stats_path)
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

            if row["kind"] != "cira":
                for field in CIRA_LATENCY_SUMMARY_STATS:
                    value = row[field]
                    if value == "":
                        continue
                    try:
                        parsed = Decimal(value)
                    except InvalidOperation as error:
                        raise ValidationError(
                            f"{context}: non-CIRA row must leave {field} "
                            "blank or zero"
                        ) from error
                    if not parsed.is_finite() or parsed != 0:
                        raise ValidationError(
                            f"{context}: non-CIRA row must leave {field} "
                            "blank or zero"
                        )

            if row["kind"] == "amu":
                amu_rows += 1
                issued = require_counter(stats, "board.asmc.issuedLoads", stats_path)
                completed = require_counter(
                    stats, "board.asmc.completedLoads", stats_path
                )
                if issued <= 0 or issued != completed:
                    raise ValidationError(
                        f"{context}: issuedLoads={issued} != completedLoads={completed} "
                        "or counts are not positive"
                    )
            elif row["kind"] == "cira":
                cira_rows += 1
                issued = require_counter(
                    stats, "board.cira.issuedPrefetches", stats_path
                )
                completed = require_counter(
                    stats, "board.cira.completedPrefetches", stats_path
                )
                if issued <= 0 or issued != completed:
                    raise ValidationError(
                        f"{context}: issuedPrefetches={issued} != "
                        f"completedPrefetches={completed} or counts are not positive"
                    )
                indexed = require_counter(
                    stats, "board.cira.issuedIndexedPrefetches", stats_path
                )
                csr = require_counter(
                    stats, "board.cira.issuedCsrPrefetches", stats_path
                )
                if indexed + csr <= 0:
                    raise ValidationError(
                        f"{context}: issuedIndexedPrefetches + "
                        f"issuedCsrPrefetches = {indexed + csr}, expected > 0"
                    )
                for field, stat_name in CIRA_LATENCY_SUMMARY_STATS.items():
                    if field == "cira_total_latency":
                        reported = require_summary_counter(row, field, context)
                        exact = require_counter(stats, stat_name, stats_path)
                    else:
                        reported = require_summary_number(row, field, context)
                        exact = require_stat_number(stats, stat_name, stats_path)
                    if reported != exact:
                        raise ValidationError(
                            f"{context}: {field}={reported} != exact "
                            f"first-ROI value {exact}"
                        )
    if row_count != 48 or amu_rows != 16 or cira_rows != 16:
        raise ValidationError(
            f"expected 48 rows, 16 AMU rows, and 16 CIRA rows; got "
            f"{row_count}, {amu_rows}, and {cira_rows}"
        )
    return ValidationResult(row_count, amu_rows, cira_rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sweep_root", type=Path)
    args = parser.parse_args()
    try:
        result = validate_sweep(args.sweep_root)
    except (OSError, KeyError, ValidationError) as error:
        parser.exit(1, f"FAIL: {error}\n")
    print(
        f"PASS: {result.row_count}/48 rows; all delays and speedups valid; "
        f"{result.amu_rows} AMU leaf-load balances; "
        f"{result.cira_rows} CIRA leaf-prefetch balances with descriptor use"
    )


if __name__ == "__main__":
    main()
