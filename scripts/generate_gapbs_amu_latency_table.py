#!/usr/bin/env python3
# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

"""Generate the validated GAPBS AMU/CIRA latency table and provenance CSV."""

import argparse
import csv
import math
import os
import tempfile
from decimal import Decimal, InvalidOperation
from io import StringIO
from pathlib import Path


LATENCIES = ("200ns", "500ns", "1us", "2us")
LATENCY_HEADINGS = {
    "200ns": "200 ns",
    "500ns": "500 ns",
    "1us": r"1 $\mu$s",
    "2us": r"2 $\mu$s",
}
BENCHMARKS = ("bfs", "bc", "pr", "sssp")
BENCHMARK_NAMES = {"bfs": "BFS", "bc": "BC", "pr": "PR", "sssp": "SSSP"}
LABEL_KINDS = (
    ("cxl_vanilla", "baseline"),
    ("amu", "amu"),
    ("cira_pgo", "cira"),
)
REQUIRED_FIELDS = {
    "benchmark",
    "label",
    "kind",
    "status",
    "verification",
    "sim_ticks",
    "speedup_vs_cxl",
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
DIAGNOSTIC_COUNT_FIELDS = (
    "cxl_packets",
    "cxl_bytes",
    "l1d_demand_misses",
    "l2d_demand_hits",
    "l2d_demand_misses",
    "l2i_demand_hits",
    "l2i_demand_misses",
)
CIRA_LATENCY_FIELDS = ("cira_total_latency", "cira_avg_latency")
PROVENANCE_FIRST = (
    "latency",
    "benchmark",
    "label",
    "kind",
    "status",
    "verification",
    "sim_ticks",
    "speedup_vs_cxl",
    "run_dir",
    "source_summary_path",
)
# Summary speedups are decimal serializations of baseline_ticks/config_ticks.
SPEEDUP_REL_TOLERANCE = 1e-9


class ValidationError(RuntimeError):
    pass


def latex_escape(value):
    replacements = {
        "\\": r"\textbackslash{}",
        "&": r"\&",
        "%": r"\%",
        "$": r"\$",
        "#": r"\#",
        "_": r"\_",
        "{": r"\{",
        "}": r"\}",
        "~": r"\textasciitilde{}",
        "^": r"\textasciicircum{}",
    }
    return "".join(
        replacements.get(character, character) for character in str(value)
    )


def positive_finite(row, field, context):
    try:
        value = float(row[field])
    except (KeyError, ValueError) as error:
        raise ValidationError(
            f"{context}: invalid {field}={row.get(field)!r}"
        ) from error
    if not math.isfinite(value) or value <= 0:
        raise ValidationError(
            f"{context}: {field} must be finite and positive, got {value}"
        )
    return value


def nonnegative_finite_decimal(row, field, context):
    try:
        value = Decimal(row[field])
    except (KeyError, InvalidOperation) as error:
        raise ValidationError(
            f"{context}: invalid {field}={row.get(field)!r}"
        ) from error
    if not value.is_finite() or value < 0:
        raise ValidationError(
            f"{context}: {field} must be finite and nonnegative, got {value}"
        )
    return value


def read_summary(latency, path):
    path = Path(path)
    with path.open(newline="", encoding="utf-8") as stream:
        reader = csv.DictReader(stream)
        fields = reader.fieldnames or []
        missing = sorted(REQUIRED_FIELDS - set(fields))
        if missing:
            raise ValidationError(
                f"{path}: missing columns: {', '.join(missing)}"
            )
        rows = list(reader)
    expected = [
        (benchmark, label, kind)
        for benchmark in BENCHMARKS
        for label, kind in LABEL_KINDS
    ]
    observed = [(row["benchmark"], row["label"], row["kind"]) for row in rows]
    if len(rows) != 12 or sorted(observed) != sorted(expected):
        raise ValidationError(
            f"{path}: expected exact row identities for all workloads"
        )

    indexed = {}
    for row in rows:
        identity = (row["benchmark"], row["label"], row["kind"])
        context = f"{latency}/{row['benchmark']}/{row['label']}"
        if row["status"] != "ok" or row["verification"] != "pass":
            raise ValidationError(
                f"{context}: status={row['status']!r}, "
                f"verification={row['verification']!r}"
            )
        positive_finite(row, "sim_ticks", context)
        positive_finite(row, "speedup_vs_cxl", context)
        for field in DIAGNOSTIC_COUNT_FIELDS:
            nonnegative_finite_decimal(row, field, context)
        if row["kind"] == "cira":
            for field in CIRA_LATENCY_FIELDS:
                nonnegative_finite_decimal(row, field, context)
        else:
            for field in CIRA_LATENCY_FIELDS:
                value = row[field]
                if value == "":
                    continue
                parsed = nonnegative_finite_decimal(row, field, context)
                if parsed != 0:
                    raise ValidationError(
                        f"{context}: non-CIRA {field} must be blank or zero"
                    )
        indexed[identity] = row

    for benchmark in BENCHMARKS:
        baseline = indexed[(benchmark, "cxl_vanilla", "baseline")]
        baseline_ticks = float(baseline["sim_ticks"])
        for label, kind in LABEL_KINDS:
            row = indexed[(benchmark, label, kind)]
            reported = float(row["speedup_vs_cxl"])
            recomputed = baseline_ticks / float(row["sim_ticks"])
            if not math.isclose(
                reported,
                recomputed,
                rel_tol=SPEEDUP_REL_TOLERANCE,
                abs_tol=0.0,
            ):
                raise ValidationError(
                    f"{latency}/{benchmark}/{label}: speedup mismatch: "
                    f"reported {reported:.17g}, recomputed {recomputed:.17g} "
                    f"(relative tolerance {SPEEDUP_REL_TOLERANCE:g})"
                )
    return fields, indexed


def geometric_mean(values):
    if not values or any(
        not math.isfinite(value) or value <= 0 for value in values
    ):
        raise ValidationError("geometric mean requires positive finite values")
    log_mean = math.fsum(math.log(value) for value in values) / len(values)
    return math.exp(log_mean)


def render_latex(data):
    header_groups = " & ".join(
        rf"\multicolumn{{2}}{{c}}{{{LATENCY_HEADINGS[latency]}}}"
        for latency in LATENCIES
    )
    lines = [
        r"\begin{table*}[t]",
        r"\centering",
        r"\caption{GAPBS speedup over the CXL baseline across link latencies. "
        r"Speedup is baseline ROI ticks divided by configuration ROI ticks. "
        r"All runs use scale 4 and one timing core; verification executes "
        r"outside "
        r"the ROI, and all 48 runs PASS.}",
        r"\label{tab:gapbs_vtune_cxl}",
        r"\resizebox{\textwidth}{!}{%",
        r"\begin{tabular}{@{}l*{4}{rr}@{}}",
        r"\toprule",
        rf"\textbf{{Workload}} & {header_groups}" + r" \\",
        r"\cmidrule(lr){2-3}\cmidrule(lr){4-5}"
        r"\cmidrule(lr){6-7}\cmidrule(lr){8-9}",
        r" & \textbf{AMU} & \textbf{CIRA} & \textbf{AMU} & \textbf{CIRA} & "
        r"\textbf{AMU} & \textbf{CIRA} & \textbf{AMU} & \textbf{CIRA} \\",
        r"\midrule",
    ]
    for benchmark in BENCHMARKS:
        values = []
        for latency in LATENCIES:
            for label, kind in LABEL_KINDS[1:]:
                speedup = float(
                    data[latency][(benchmark, label, kind)]["speedup_vs_cxl"]
                )
                values.append(f"{speedup:.2f}$\\times$")
        lines.append(
            f"{latex_escape(BENCHMARK_NAMES[benchmark])} & "
            + " & ".join(values)
            + r" \\"
        )
    geo_values = []
    for latency in LATENCIES:
        for label, kind in LABEL_KINDS[1:]:
            speeds = [
                float(
                    data[latency][(benchmark, label, kind)]["speedup_vs_cxl"]
                )
                for benchmark in BENCHMARKS
            ]
            geo_values.append(f"{geometric_mean(speeds):.2f}$\\times$")
    lines += [
        r"\midrule",
        "Geo. & " + " & ".join(geo_values) + r" \\",
        r"\bottomrule",
        r"\end{tabular}%",
        r"}",
        r"\end{table*}",
    ]
    return "\n".join(lines) + "\n"


def atomic_write(path, content, newline=None):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent
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


def generate_outputs(inputs, latex_output, provenance_output):
    if set(inputs) != set(LATENCIES) or len(inputs) != 4:
        raise ValidationError(
            "expected exactly four canonical latency inputs: "
            + ", ".join(LATENCIES)
        )
    data = {}
    source_fields = []
    for latency in LATENCIES:
        fields, data[latency] = read_summary(latency, inputs[latency])
        for field in fields:
            if field not in source_fields:
                source_fields.append(field)
    extra_fields = [
        field for field in source_fields if field not in PROVENANCE_FIRST
    ]
    provenance_fields = list(PROVENANCE_FIRST) + extra_fields
    rows = []
    for latency in LATENCIES:
        for benchmark in BENCHMARKS:
            for label, kind in LABEL_KINDS:
                source = data[latency][(benchmark, label, kind)]
                row = {
                    field: source.get(field, "")
                    for field in provenance_fields
                }
                row["latency"] = latency
                row["source_summary_path"] = str(Path(inputs[latency]))
                rows.append(row)
    buffer = StringIO(newline="")
    writer = csv.DictWriter(
        buffer, fieldnames=provenance_fields, lineterminator="\n"
    )
    writer.writeheader()
    writer.writerows(rows)
    latex = render_latex(data)
    atomic_write(provenance_output, buffer.getvalue(), newline="")
    atomic_write(latex_output, latex)


def parse_inputs(values):
    if len(values) != 4:
        raise ValidationError(
            "expected exactly four LATENCY=summary.csv inputs"
        )
    inputs = {}
    for value in values:
        if "=" not in value:
            raise ValidationError(
                f"invalid input {value!r}; expected LATENCY=summary.csv"
            )
        latency, path = value.split("=", 1)
        if latency in inputs:
            raise ValidationError(f"duplicate latency input: {latency}")
        inputs[latency] = Path(path)
    if set(inputs) != set(LATENCIES):
        raise ValidationError(
            "inputs must be exactly: " + ", ".join(LATENCIES)
        )
    return inputs


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input", action="append", required=True, metavar="LATENCY=SUMMARY"
    )
    parser.add_argument("--latex-output", required=True, type=Path)
    parser.add_argument("--provenance-output", required=True, type=Path)
    args = parser.parse_args()
    try:
        generate_outputs(
            parse_inputs(args.input),
            args.latex_output,
            args.provenance_output,
        )
    except (OSError, KeyError, ValidationError) as error:
        parser.exit(1, f"FAIL: {error}\n")


if __name__ == "__main__":
    main()
