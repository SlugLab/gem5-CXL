#!/usr/bin/env python3
# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

"""Publish one hash-bound CIRA/AMU/M2NDP scaling and breadth figure."""

import argparse
import csv
import dataclasses
import hashlib
import io
import json
import os
import shutil
import tempfile
from decimal import Decimal, InvalidOperation
from pathlib import Path


SCALES = (4, 12, 14, 20)
SCALING_SYSTEMS = ("vanilla", "amu", "cira", "m2ndp")
PLOTTED_SYSTEMS = ("amu", "cira", "m2ndp")
WORKLOADS = (
    "pr_spmv", "mcf", "amg_gather", "lulesh_scatter", "npb_cg", "npb_mg",
)
WORKLOAD_LABELS = {
    "pr_spmv": "PR",
    "mcf": "MCF",
    "amg_gather": "AMG Gather",
    "lulesh_scatter": "LULESH Scatter",
    "npb_cg": "NPB CG",
    "npb_mg": "NPB MG",
}
EVIDENCE_SCALING = "Full E2E, gem5 + NDPSim, 1 us CXL"
EVIDENCE_BREADTH = "Calibrated trace-driven E2E estimate, 1 us CXL"
TICKS_PER_SECOND = Decimal(10**12)


class ComparisonError(RuntimeError):
    """Publication input or atomic output failed a strict evidence gate."""


@dataclasses.dataclass(frozen=True)
class Row:
    scope: str
    item: str
    label: str
    system: str
    latency_seconds: Decimal
    speedup: Decimal | None
    ci_low: Decimal | None
    ci_high: Decimal | None
    evidence_type: str
    output_elements: int
    window_count: int | None
    mechanism: dict


@dataclasses.dataclass(frozen=True)
class ComparisonData:
    rows: tuple[Row, ...]
    scaling_scales: tuple[int, ...]
    breadth_workloads: tuple[str, ...]
    input_records: dict


def _sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_json(path, label):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ComparisonError(f"invalid {label}: {error}") from error
    if not isinstance(value, dict):
        raise ComparisonError(f"{label} must be a JSON object")
    return value


def _decimal(value, label, *, positive=True):
    if isinstance(value, bool) or isinstance(value, float):
        raise ComparisonError(f"{label} must be an exact decimal")
    try:
        result = Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ComparisonError(f"{label} is not an exact decimal") from error
    if not result.is_finite() or (positive and result <= 0):
        raise ComparisonError(f"{label} must be finite and positive")
    return result


def _positive_integer(value, label):
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ComparisonError(f"{label} must be a positive integer")
    return value


def _close(left, right):
    return abs(left - right) <= max(abs(right) * Decimal("1e-12"), Decimal("1e-18"))


def _input_record(path):
    selected = Path(path).resolve()
    return {"path": str(selected), "sha256": _sha256_file(selected)}


def _scaling_rows(value):
    if (
        value.get("schema") != 1
        or value.get("status") != "complete"
        or value.get("profile") != "pr-scaling-4thread-1us"
    ):
        raise ComparisonError("scaling evidence is not complete formal 4-thread 1-us data")
    points = value.get("points")
    expected = {f"g{scale}:{system}" for scale in SCALES for system in SCALING_SYSTEMS}
    if not isinstance(points, dict) or set(points) != expected:
        raise ComparisonError("scaling matrix is not exactly 16 points")
    rows = []
    for scale in SCALES:
        baseline = points[f"g{scale}:vanilla"]
        baseline_seconds = _decimal(
            baseline.get("latency_seconds"), f"g{scale} Vanilla latency"
        )
        for system in SCALING_SYSTEMS:
            point = points[f"g{scale}:{system}"]
            if (
                point.get("status") != "passed"
                or point.get("scale") != scale
                or point.get("system") != system
                or point.get("latency") != "1us"
                or point.get("full_e2e") is not True
            ):
                raise ComparisonError(f"g{scale}:{system} scaling point is not formal PASS")
            seconds = _decimal(
                point.get("latency_seconds"), f"g{scale}:{system} latency"
            )
            mechanism = point.get("mechanism")
            if not isinstance(mechanism, dict) or mechanism.get("verification") != "pass":
                raise ComparisonError(f"g{scale}:{system} mechanism evidence is missing")
            outputs = point.get("outputs")
            if not isinstance(outputs, dict) or not outputs or any(
                not isinstance(digest, str) or len(digest) != 64
                for digest in outputs.values()
            ):
                raise ComparisonError(f"g{scale}:{system} output hashes are invalid")
            speedup = baseline_seconds / seconds
            stored = point.get("speedup")
            if stored is not None and not _close(
                _decimal(stored, f"g{scale}:{system} stored speedup"), speedup
            ):
                raise ComparisonError(f"g{scale}:{system} stored speedup differs")
            rows.append(Row(
                "scaling", f"g{scale}", f"g{scale}", system, seconds,
                speedup, None, None, EVIDENCE_SCALING,
                _positive_integer(point.get("output_elements"), "output elements"),
                None, mechanism,
            ))
    return rows


def _functional_for_system(workload, workloads, system):
    try:
        functional = workloads[workload]["functional"]
        key = "m2ndp-funcsim" if system == "m2ndp" else system
        record = functional[key]
    except (KeyError, TypeError) as error:
        raise ComparisonError(f"{workload}:{system} functional evidence is missing") from error
    if not isinstance(record, dict) or record.get("status") != "pass":
        raise ComparisonError(f"{workload}:{system} functional evidence is not PASS")
    return record


def _breadth_rows(value):
    if value.get("schema") != 1 or value.get("status") not in {"complete", "inconclusive"}:
        raise ComparisonError("breadth evidence is not terminal")
    if tuple(value.get("workload_order", ())) != WORKLOADS:
        raise ComparisonError("breadth workload order differs")
    results = value.get("results")
    workloads = value.get("workloads")
    if not isinstance(results, dict) or set(results) != set(WORKLOADS):
        raise ComparisonError("breadth result set differs")
    if not isinstance(workloads, dict) or set(workloads) != set(WORKLOADS):
        raise ComparisonError("breadth workload evidence set differs")
    rows = []
    for workload in WORKLOADS:
        result = results[workload]
        if result.get("status") not in {"complete", "inconclusive"}:
            raise ComparisonError(f"{workload} breadth result is not terminal")
        level = _positive_integer(result.get("level"), f"{workload} window count")
        absolute = result.get("absolute_seconds")
        systems = result.get("systems")
        if not isinstance(absolute, dict) or set(absolute) != set(SCALING_SYSTEMS):
            raise ComparisonError(f"{workload} absolute timing set differs")
        if not isinstance(systems, dict) or set(systems) != set(PLOTTED_SYSTEMS):
            raise ComparisonError(f"{workload} speedup set differs")
        baseline = _decimal(absolute["vanilla"], f"{workload} Vanilla latency")
        for system in PLOTTED_SYSTEMS:
            seconds = _decimal(absolute[system], f"{workload}:{system} latency")
            observed = systems[system]
            estimate = baseline / seconds
            publishable = observed.get("publishable") is True
            speedup = None
            low = None
            high = None
            if publishable:
                speedup = _decimal(observed.get("speedup"), f"{workload}:{system} speedup")
                low = _decimal(observed.get("ci_low"), f"{workload}:{system} CI low")
                high = _decimal(observed.get("ci_high"), f"{workload}:{system} CI high")
                if not _close(speedup, estimate):
                    raise ComparisonError(f"{workload}:{system} stored speedup differs")
                if not low <= speedup <= high:
                    raise ComparisonError(f"{workload}:{system} confidence interval is invalid")
            functional = _functional_for_system(workload, workloads, system)
            output_elements = _positive_integer(
                functional.get("compared_words"),
                f"{workload}:{system} output element count",
            )
            rows.append(Row(
                "breadth", workload, WORKLOAD_LABELS[workload], system,
                seconds, speedup, low, high, EVIDENCE_BREADTH,
                output_elements, level, functional,
            ))
    return rows


def load_data(scaling_path, breadth_path):
    scaling_path = Path(scaling_path).resolve()
    breadth_path = Path(breadth_path).resolve()
    scaling = _load_json(scaling_path, "scaling evidence")
    breadth = _load_json(breadth_path, "breadth evidence")
    scaling_input = scaling.get("inputs_sha256")
    scaling_calibration = scaling.get("calibration_sha256")
    identity = breadth.get("identity", {})
    if (
        identity.get("input_manifest_sha256") != scaling_input
        or identity.get("calibration_manifest_sha256") != scaling_calibration
    ):
        raise ComparisonError("scaling and breadth evidence roots are mixed")
    rows = tuple(_scaling_rows(scaling) + _breadth_rows(breadth))
    return ComparisonData(
        rows=rows,
        scaling_scales=SCALES,
        breadth_workloads=WORKLOADS,
        input_records={
            "scaling": _input_record(scaling_path),
            "breadth": _input_record(breadth_path),
        },
    )


def _csv_bytes(data):
    stream = io.StringIO(newline="")
    fields = (
        "scope", "item", "label", "system", "latency_seconds", "speedup",
        "ci_low", "ci_high", "evidence_type", "output_elements", "window_count",
        "mechanism_json",
    )
    writer = csv.DictWriter(stream, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    for row in data.rows:
        writer.writerow({
            "scope": row.scope,
            "item": row.item,
            "label": row.label,
            "system": row.system,
            "latency_seconds": str(row.latency_seconds),
            "speedup": "" if row.speedup is None else str(row.speedup),
            "ci_low": "" if row.ci_low is None else str(row.ci_low),
            "ci_high": "" if row.ci_high is None else str(row.ci_high),
            "evidence_type": row.evidence_type,
            "output_elements": row.output_elements,
            "window_count": "" if row.window_count is None else row.window_count,
            "mechanism_json": json.dumps(
                row.mechanism, sort_keys=True, separators=(",", ":")
            ),
        })
    return stream.getvalue().encode("utf-8")


def _latex_escape(value):
    return str(value).replace("_", r"\_").replace("%", r"\%")


def _fmt(value, digits=3):
    if value is None:
        return "--"
    return f"{value:.{digits}f}"


def _table_bytes(data):
    lines = [
        r"\begin{tabular}{llrrrrl}",
        r"\toprule",
        r"Scope & Workload/scale & System & Latency (s) & Speedup & 95\% CI & Evidence \\",
        r"\midrule",
    ]
    for row in data.rows:
        interval = (
            "--" if row.ci_low is None else
            f"[{_fmt(row.ci_low)}, {_fmt(row.ci_high)}]"
        )
        lines.append(
            f"{_latex_escape(row.scope.title())} & {_latex_escape(row.label)} & "
            f"{_latex_escape(row.system.upper())} & {_fmt(row.latency_seconds, 6)} & "
            f"{_fmt(row.speedup)} & {interval} & "
            f"{_latex_escape(row.evidence_type)} \\\\"
        )
    lines.extend((r"\bottomrule", r"\end{tabular}", ""))
    return "\n".join(lines).encode("utf-8")


def _render_figure(data, pdf_path, svg_path):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import numpy as np
    except ImportError as error:
        raise ComparisonError(f"Matplotlib is unavailable: {error}") from error

    matplotlib.rcParams.update({
        "font.family": "DejaVu Sans",
        "font.size": 7.5,
        "axes.titlesize": 9,
        "axes.labelsize": 8,
        "legend.fontsize": 7,
        "svg.hashsalt": "cira-amu-m2ndp-formal-v1",
        "pdf.compression": 9,
    })
    colors = {"amu": "#3569a8", "cira": "#d07a21", "m2ndp": "#6f7f35"}
    styles = {"amu": ("-", "o"), "cira": ("--", "s"), "m2ndp": (":", "^")}
    hatches = {"amu": "", "cira": "//", "m2ndp": "xx"}
    fig, axes = plt.subplots(1, 2, figsize=(7.0, 3.1), gridspec_kw={"wspace": 0.32})
    ax = axes[0]
    for system in PLOTTED_SYSTEMS:
        rows = [row for row in data.rows if row.scope == "scaling" and row.system == system]
        line, marker = styles[system]
        ax.plot(SCALES, [float(row.speedup) for row in rows], color=colors[system],
                linestyle=line, marker=marker, markerfacecolor="white",
                linewidth=1.5, markersize=4.5, label=system.upper())
    ax.axhline(1.0, color="#333333", linewidth=0.8, zorder=0)
    ax.set_xticks(SCALES, [f"g{scale}" for scale in SCALES])
    ax.set_xlabel("Graph scale")
    ax.set_ylabel("Speedup vs. Vanilla CXL")
    ax.set_title("(a) PageRank scaling")
    ax.grid(axis="y", color="#dddddd", linewidth=0.6)
    ax.spines[["top", "right"]].set_visible(False)
    ax.legend(frameon=False, ncol=3, loc="upper left")

    ax = axes[1]
    x = np.arange(len(WORKLOADS))
    width = 0.24
    for offset_index, system in enumerate(PLOTTED_SYSTEMS):
        rows = [row for row in data.rows if row.scope == "breadth" and row.system == system]
        values = [float(row.speedup) if row.speedup is not None else 0.0 for row in rows]
        errors_low = [
            0.0 if row.speedup is None else float(row.speedup - row.ci_low)
            for row in rows
        ]
        errors_high = [
            0.0 if row.speedup is None else float(row.ci_high - row.speedup)
            for row in rows
        ]
        positions = x + (offset_index - 1) * width
        bars = ax.bar(
            positions, values, width, label=system.upper(), color=colors[system],
            edgecolor="#333333", linewidth=0.5, hatch=hatches[system],
            yerr=np.array([errors_low, errors_high]), capsize=1.8,
            error_kw={"linewidth": 0.7, "capthick": 0.7},
        )
        for bar, row in zip(bars, rows):
            if row.speedup is None:
                bar.set_visible(False)
                ax.text(bar.get_x() + bar.get_width() / 2, 0.05, "inc.",
                        rotation=90, ha="center", va="bottom", fontsize=6,
                        color="#555555")
    ax.axhline(1.0, color="#333333", linewidth=0.8, zorder=0)
    ax.set_xticks(x, [WORKLOAD_LABELS[name] for name in WORKLOADS], rotation=30, ha="right")
    ax.set_ylabel("Speedup vs. Vanilla CXL")
    ax.set_title("(b) Breadth, paired 95% CI")
    ax.grid(axis="y", color="#dddddd", linewidth=0.6, zorder=0)
    ax.spines[["top", "right"]].set_visible(False)
    ax.legend(frameon=False, ncol=3, loc="upper left")
    fig.suptitle("AMU, CIRA, and M2NDP at 1 µs CXL latency", fontsize=10, y=0.995)
    fig.subplots_adjust(left=0.08, right=0.99, bottom=0.25, top=0.84)
    metadata = {
        "Title": "CIRA AMU M2NDP scaling and breadth comparison",
        "Subject": "Hash-bound formal 1 us CXL performance evidence",
        "Creator": "generate_cira_amu_m2ndp_comparison.py",
        "CreationDate": None,
        "ModDate": None,
    }
    fig.savefig(pdf_path, format="pdf", metadata=metadata)
    fig.savefig(
        svg_path, format="svg",
        metadata={"Title": metadata["Title"], "Date": None},
    )
    plt.close(fig)


def _write(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    _fsync_directory(path.parent)


def _fsync_directory(path):
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(Path(path), flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _artifact_relatives():
    return (
        "cira-amu-m2ndp-comparison.csv",
        "cira-amu-m2ndp-evidence.json",
        "gapbs-vtune-cxl-table.tex",
        "fig/cira-amu-m2ndp-scaling-breadth.pdf",
        "fig/cira-amu-m2ndp-scaling-breadth.svg",
    )


def publish(data, output_root, *, fail_after_promotions=None):
    if not isinstance(data, ComparisonData) or len(data.rows) != 34:
        raise ComparisonError("publication data is incomplete")
    output_root = Path(output_root).resolve()
    output_root.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".cross-system-stage-", dir=output_root.parent))
    backup = Path(tempfile.mkdtemp(prefix=".cross-system-backup-", dir=output_root.parent))
    promoted = []
    backed_up = []
    try:
        _write(stage / "cira-amu-m2ndp-comparison.csv", _csv_bytes(data))
        _write(stage / "gapbs-vtune-cxl-table.tex", _table_bytes(data))
        (stage / "fig").mkdir(parents=True, exist_ok=True)
        _render_figure(
            data,
            stage / "fig/cira-amu-m2ndp-scaling-breadth.pdf",
            stage / "fig/cira-amu-m2ndp-scaling-breadth.svg",
        )
        artifact_hashes = {
            relative: _sha256_file(stage / relative)
            for relative in _artifact_relatives()
            if not relative.endswith("evidence.json")
        }
        evidence = {
            "schema": 1,
            "status": "pass",
            "row_count": len(data.rows),
            "inputs": data.input_records,
            "artifacts": artifact_hashes,
            "evidence_labels": [EVIDENCE_SCALING, EVIDENCE_BREADTH],
        }
        _write(
            stage / "cira-amu-m2ndp-evidence.json",
            (json.dumps(evidence, sort_keys=True, separators=(",", ":")) + "\n").encode(),
        )
        output_root.mkdir(parents=True, exist_ok=True)
        for relative in _artifact_relatives():
            source = stage / relative
            target = output_root / relative
            saved = backup / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                saved.parent.mkdir(parents=True, exist_ok=True)
                os.replace(target, saved)
                _fsync_directory(saved.parent)
                backed_up.append(relative)
            os.replace(source, target)
            _fsync_directory(target.parent)
            promoted.append(relative)
            if fail_after_promotions is not None and len(promoted) == fail_after_promotions:
                raise ComparisonError("injected promotion failure")
        return {
            relative: {"path": str(output_root / relative),
                       "sha256": _sha256_file(output_root / relative)}
            for relative in _artifact_relatives()
        }
    except Exception as error:
        for relative in reversed(promoted):
            (output_root / relative).unlink(missing_ok=True)
        for relative in backed_up:
            saved = backup / relative
            target = output_root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            if saved.exists():
                os.replace(saved, target)
                _fsync_directory(target.parent)
        if isinstance(error, ComparisonError):
            raise
        raise ComparisonError(f"publication failed: {error}") from error
    finally:
        shutil.rmtree(stage, ignore_errors=True)
        shutil.rmtree(backup, ignore_errors=True)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scaling", type=Path, required=True)
    parser.add_argument("--breadth", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv=None):
    options = parse_args(argv)
    try:
        publish(load_data(options.scaling, options.breadth), options.output_root)
    except ComparisonError as error:
        print(f"CROSS_SYSTEM_PUBLICATION_FAILED error={error}")
        return 1
    print(f"CROSS_SYSTEM_PUBLICATION_PASS root={options.output_root.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
