#!/usr/bin/env python3
# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

"""Validate a four-latency GAPBS baseline/AMU/CIRA sweep."""

import argparse
import csv
import math
import re
from collections import namedtuple
from pathlib import Path


EXPECTED_LATENCIES = {
    "200ns": 200_000,
    "500ns": 500_000,
    "1us": 1_000_000,
    "2us": 2_000_000,
}
EXPECTED_BENCHMARKS = ("bfs", "bc", "pr", "sssp")
EXPECTED_KINDS = ("baseline", "amu", "cira")
ValidationResult = namedtuple(
    "ValidationResult", "row_count amu_rows cira_rows"
)


class ValidationError(RuntimeError):
    pass


def parse_first_stats_section(path):
    stats = {}
    in_section = False
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("---------- Begin Simulation Statistics"):
            if in_section:
                break
            in_section = True
            continue
        if in_section and line.startswith("---------- End Simulation Statistics"):
            break
        if not in_section:
            continue
        fields = line.split()
        if len(fields) < 2:
            continue
        try:
            stats[fields[0]] = float(fields[1])
        except ValueError:
            continue
    if not in_section:
        raise ValidationError(f"{path}: missing simulation statistics section")
    return stats


def require_counter(stats, name, path):
    if name not in stats:
        raise ValidationError(f"{path}: missing {name} in first ROI stats section")
    value = stats[name]
    if not value.is_integer():
        raise ValidationError(f"{path}: {name} is not an integer: {value}")
    return int(value)


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
            rows = list(csv.DictReader(stream))
        if len(rows) != 12:
            raise ValidationError(f"{summary}: expected 12 rows, found {len(rows)}")

        observed = [(row["benchmark"], row["kind"]) for row in rows]
        expected = [
            (benchmark, kind)
            for benchmark in EXPECTED_BENCHMARKS
            for kind in EXPECTED_KINDS
        ]
        if sorted(observed) != sorted(expected):
            raise ValidationError(
                f"{summary}: expected one baseline/amu/cira row per workload"
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
