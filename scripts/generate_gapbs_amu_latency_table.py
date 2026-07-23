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
    "sim_insts",
    "speedup_vs_cxl",
    "scale",
    "iterations",
    "measured_trial",
    "fast_forward_cpu",
    "roi_cpu",
    "cpu_switches",
    "cxl_link_delay",
    "all_memory_cxl",
    "asmc_loads",
    "asmc_completed",
    "cira_prefetches",
    "cira_completed",
    "cira_indexed_prefetches",
    "cira_csr_prefetches",
    "cira_useful",
    "cira_late",
    "cira_read_packets",
    "cira_read_bytes",
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
COMMON_COUNT_FIELDS = (
    "sim_insts",
    "cxl_packets",
    "cxl_bytes",
    "l1d_demand_misses",
    "l2d_demand_hits",
    "l2d_demand_misses",
    "l2i_demand_hits",
    "l2i_demand_misses",
)
CIRA_COUNT_FIELDS = (
    "cira_prefetches",
    "cira_completed",
    "cira_indexed_prefetches",
    "cira_csr_prefetches",
    "cira_useful",
    "cira_late",
    "cira_read_packets",
    "cira_read_bytes",
)
AMU_COUNT_FIELDS = ("asmc_loads", "asmc_completed")
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
CANONICAL_METADATA = {
    "scale": "20",
    "iterations": "2",
    "measured_trial": "1",
    "fast_forward_cpu": "atomic",
    "roi_cpu": "timing",
    "cpu_switches": "1",
    "all_memory_cxl": "true",
}


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


def positive_finite_decimal(row, field, context):
    value = nonnegative_finite_decimal(row, field, context)
    if value <= 0:
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


def nonnegative_integral_decimal(row, field, context):
    value = nonnegative_finite_decimal(row, field, context)
    if value != value.to_integral_value():
        raise ValidationError(
            f"{context}: {field} must be integral, got {value}"
        )
    return value


def read_summary(latency, path):
    path = Path(path)
    with path.open(newline="", encoding="utf-8") as stream:
        reader = csv.DictReader(stream)
        fields = reader.fieldnames or []
        duplicates = sorted(
            {field for field in fields if fields.count(field) > 1}
        )
        if duplicates:
            raise ValidationError(
                f"{path}: duplicate columns: {', '.join(duplicates)}"
            )
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
        for field, expected_value in CANONICAL_METADATA.items():
            actual = row[field].strip().lower()
            if actual != expected_value:
                raise ValidationError(
                    f"{context}: {field}={row[field]!r}, "
                    f"expected {expected_value!r}"
                )
        if row["cxl_link_delay"] != latency:
            raise ValidationError(
                f"{context}: cxl_link_delay={row['cxl_link_delay']!r}, "
                f"expected {latency!r}"
            )
        sim_ticks = positive_finite_decimal(row, "sim_ticks", context)
        if sim_ticks != sim_ticks.to_integral_value():
            raise ValidationError(
                f"{context}: sim_ticks must be integral, got {sim_ticks}"
            )
        positive_finite_decimal(row, "speedup_vs_cxl", context)
        for field in COMMON_COUNT_FIELDS:
            nonnegative_integral_decimal(row, field, context)
        if row["kind"] == "amu":
            for field in AMU_COUNT_FIELDS:
                nonnegative_integral_decimal(row, field, context)
            issued = nonnegative_integral_decimal(
                row, "asmc_loads", context
            )
            completed = nonnegative_integral_decimal(
                row, "asmc_completed", context
            )
            if issued <= 0 or issued != completed:
                raise ValidationError(
                    f"{context}: AMU load counts must be positive and balanced"
                )
        else:
            for field in AMU_COUNT_FIELDS:
                value = row[field]
                if value != "" and nonnegative_integral_decimal(
                    row, field, context
                ) != 0:
                    raise ValidationError(
                        f"{context}: non-AMU {field} must be blank or zero"
                    )
        if row["kind"] == "cira":
            for field in CIRA_COUNT_FIELDS:
                nonnegative_integral_decimal(row, field, context)
            issued = nonnegative_integral_decimal(
                row, "cira_prefetches", context
            )
            completed = nonnegative_integral_decimal(
                row, "cira_completed", context
            )
            csr = nonnegative_integral_decimal(
                row, "cira_csr_prefetches", context
            )
            if issued <= 0 or issued != completed:
                raise ValidationError(
                    f"{context}: CIRA leaf counts must be positive and balanced"
                )
            if csr <= 0:
                raise ValidationError(
                    f"{context}: cira_csr_prefetches must be positive"
                )
            nonnegative_integral_decimal(row, "cira_total_latency", context)
            nonnegative_finite_decimal(row, "cira_avg_latency", context)
        else:
            for field in (*CIRA_COUNT_FIELDS, *CIRA_LATENCY_FIELDS):
                value = row[field]
                if value == "":
                    continue
                if field != "cira_avg_latency":
                    parsed = nonnegative_integral_decimal(row, field, context)
                else:
                    parsed = nonnegative_finite_decimal(row, field, context)
                if parsed != 0:
                    raise ValidationError(
                        f"{context}: non-CIRA {field} must be blank or zero"
                    )
        indexed[identity] = row

    for benchmark in BENCHMARKS:
        baseline = indexed[(benchmark, "cxl_vanilla", "baseline")]
        baseline_ticks = Decimal(baseline["sim_ticks"])
        for label, kind in LABEL_KINDS:
            row = indexed[(benchmark, label, kind)]
            reported = Decimal(row["speedup_vs_cxl"])
            recomputed = baseline_ticks / Decimal(row["sim_ticks"])
            if reported != recomputed:
                raise ValidationError(
                    f"{latency}/{benchmark}/{label}: speedup mismatch: "
                    f"reported {reported}, recomputed {recomputed}"
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
        r"All runs use scale 20, Atomic pre-ROI graph generation, Timing "
        r"trial 0 warmup, measured trial 1 ROI, and bit-exact verification "
        r"PASS.}",
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


def require_distinct_output_paths(latex_output, provenance_output):
    latex = Path(latex_output)
    provenance = Path(provenance_output)
    try:
        latex_resolved = latex.resolve(strict=False)
        provenance_resolved = provenance.resolve(strict=False)
    except (OSError, RuntimeError) as error:
        raise ValidationError(
            "LaTeX and provenance outputs must use distinct paths; "
            f"could not resolve paths safely: {error}"
        ) from error
    equivalent = latex_resolved == provenance_resolved
    if not equivalent and latex.exists() and provenance.exists():
        try:
            equivalent = os.path.samefile(latex, provenance)
        except OSError as error:
            raise ValidationError(
                "LaTeX and provenance outputs must use distinct paths; "
                f"could not compare existing paths safely: {error}"
            ) from error
    if equivalent:
        raise ValidationError(
            "LaTeX and provenance outputs must use distinct paths: "
            f"{latex} and {provenance}"
        )


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


def generate_outputs(inputs, latex_output, provenance_output):
    if set(inputs) != set(LATENCIES) or len(inputs) != 4:
        raise ValidationError(
            "expected exactly four canonical latency inputs: "
            + ", ".join(LATENCIES)
        )
    require_distinct_output_paths(latex_output, provenance_output)
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
    transactional_write(
        (
            (latex_output, latex, None),
            (provenance_output, buffer.getvalue(), ""),
        )
    )


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
