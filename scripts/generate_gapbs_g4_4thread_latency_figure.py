#!/usr/bin/env python3
# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

"""Render the validated g4/four-thread CXL latency sweep figure."""

import argparse
import csv
import dataclasses
import io
import json
import math
import os
import re
import sys
from pathlib import Path


os.environ.setdefault("MPLCONFIGDIR", "/tmp/gapbs-g4-matplotlib")

import matplotlib

matplotlib.use("Agg", force=True)
from matplotlib import pyplot as plt

try:
    from scripts import generate_gapbs_g4_4thread_latency_results as results
    from scripts import m2ndp_artifacts as artifacts
except ImportError:
    import generate_gapbs_g4_4thread_latency_results as results
    import m2ndp_artifacts as artifacts


LATENCY_NS = (200, 500, 1000, 2000)
SERIES = ("AMU", "CIRA", "M2NDP")
SYSTEM_KEYS = {"AMU": "amu", "CIRA": "cira", "M2NDP": "m2ndp"}
PDF_NAME = "gapbs-g4-4thread-latency-sweep.pdf"
SVG_NAME = "gapbs-g4-4thread-latency-sweep.svg"
TITLE = "GAPBS PageRank speedup vs. CXL latency"
SUBTITLE = (
    "g4 · 4 timing cores/threads · all-CXL · 2 trials · "
    "20 iterations · bit-exact"
)
FIGURE_SIZE = (7.0, 3.2)
COLORS = {
    "AMU": "#0072B2",
    "CIRA": "#D18F00",
    "M2NDP": "#6B8E23",
}
STYLES = {
    "AMU": {"linestyle": "-", "marker": "o"},
    "CIRA": {"linestyle": "--", "marker": "s"},
    "M2NDP": {"linestyle": ":", "marker": "^"},
}


class FigureDataError(ValueError):
    """Validated rows cannot satisfy the chart contract."""


@dataclasses.dataclass(frozen=True)
class FigureData:
    latency_ns: tuple[int, ...]
    series: dict[str, tuple[float, ...]]
    vanilla_reference: tuple[float, ...]
    evidence_sha256: str
    y_scale: str


@dataclasses.dataclass(frozen=True)
class FigurePaths:
    pdf: Path
    svg: Path


def _positive_float(value, context):
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as error:
        raise FigureDataError(f"{context} must be finite and positive") from error
    if not math.isfinite(number) or number <= 0:
        raise FigureDataError(f"{context} must be finite and positive")
    return number


def prepare_figure_data(rows, *, evidence_sha256):
    if not re.fullmatch(r"[0-9a-f]{64}", evidence_sha256):
        raise FigureDataError("evidence SHA-256 is invalid")
    try:
        ordered = results.validate_matrix(rows)
    except results.PublicationError as error:
        raise FigureDataError(str(error)) from error
    by_key = {
        (row["latency"], row["system"]): row for row in ordered
    }
    series = {
        label: tuple(
            _positive_float(
                by_key[(latency, SYSTEM_KEYS[label])][
                    "speedup_vs_vanilla_cxl"
                ],
                f"{latency}/{label} speedup",
            )
            for latency in results.LATENCIES
        )
        for label in SERIES
    }
    vanilla = tuple(
        _positive_float(
            by_key[(latency, "vanilla")]["speedup_vs_vanilla_cxl"],
            f"{latency}/Vanilla speedup",
        )
        for latency in results.LATENCIES
    )
    if vanilla != (1.0, 1.0, 1.0, 1.0):
        raise FigureDataError("Vanilla reference must be exactly 1x")
    values = [*vanilla]
    for points in series.values():
        values.extend(points)
    y_scale = "log" if max(values) / min(values) >= 20 else "linear"
    return FigureData(
        latency_ns=LATENCY_NS,
        series=series,
        vanilla_reference=vanilla,
        evidence_sha256=evidence_sha256,
        y_scale=y_scale,
    )


def _style_axis(axis):
    axis.spines["top"].set_visible(False)
    axis.spines["right"].set_visible(False)
    axis.spines["left"].set_color("#444444")
    axis.spines["bottom"].set_color("#444444")
    axis.tick_params(colors="#202020", width=0.7)
    axis.grid(axis="y", color="#D9D9D9", linewidth=0.55)
    axis.set_axisbelow(True)


def _metadata_description(data):
    return (
        f"Evidence SHA-256: {data.evidence_sha256}; "
        "g4 PageRank; four timing cores; four threads; all CXL; "
        "two trials; trial 1 measured; 20 iterations; bit-exact; "
        f"y_scale={data.y_scale}"
    )


def render_figure(rows, *, evidence_sha256):
    data = prepare_figure_data(rows, evidence_sha256=evidence_sha256)
    rc = {
        "font.family": "DejaVu Sans",
        "font.size": 8,
        "axes.labelsize": 8,
        "axes.titlesize": 10,
        "xtick.labelsize": 7,
        "ytick.labelsize": 7,
        "pdf.fonttype": 42,
        "svg.fonttype": "none",
        "svg.hashsalt": "gapbs-g4-4thread-latency",
    }
    with plt.rc_context(rc):
        canvas, axis = plt.subplots(figsize=FIGURE_SIZE)
        try:
            canvas.patch.set_facecolor("white")
            for label in SERIES:
                axis.plot(
                    data.latency_ns,
                    data.series[label],
                    color=COLORS[label],
                    linewidth=1.7,
                    markersize=5,
                    markerfacecolor="white",
                    markeredgecolor=COLORS[label],
                    markeredgewidth=1.1,
                    **STYLES[label],
                )
                axis.annotate(
                    label,
                    (data.latency_ns[-1], data.series[label][-1]),
                    xytext=(7, 0),
                    textcoords="offset points",
                    ha="left",
                    va="center",
                    color=COLORS[label],
                    fontsize=7.5,
                    fontweight="bold",
                )
            axis.axhline(
                1.0,
                color="#6F6F6F",
                linestyle=(0, (4, 2)),
                linewidth=1.0,
                zorder=0,
            )
            axis.annotate(
                "Vanilla CXL (1×)",
                (data.latency_ns[-1], 1.0),
                xytext=(7, 0),
                textcoords="offset points",
                ha="left",
                va="center",
                color="#555555",
                fontsize=7,
            )
            axis.set_xticks(
                data.latency_ns, ("200", "500", "1,000", "2,000")
            )
            axis.set_xlim(120, 2250)
            axis.set_xlabel("CXL link latency (ns)")
            axis.set_ylabel("Speedup vs. matched Vanilla CXL")
            axis.set_yscale(data.y_scale)
            all_values = [1.0]
            for points in data.series.values():
                all_values.extend(points)
            if data.y_scale == "linear":
                axis.set_ylim(0, max(all_values) * 1.16)
            else:
                axis.set_ylim(min(all_values) * 0.72, max(all_values) * 1.35)
            _style_axis(axis)
            canvas.suptitle(
                TITLE,
                x=0.08,
                y=0.96,
                ha="left",
                fontsize=10.5,
                fontweight="bold",
                color="#202020",
            )
            canvas.text(
                0.08,
                0.89,
                SUBTITLE,
                ha="left",
                va="top",
                fontsize=7.2,
                color="#4A4A4A",
            )
            canvas.subplots_adjust(
                left=0.12, right=0.83, bottom=0.20, top=0.78
            )
            description = _metadata_description(data)
            pdf_stream = io.BytesIO()
            svg_stream = io.BytesIO()
            canvas.savefig(
                pdf_stream,
                format="pdf",
                dpi=300,
                metadata={
                    "Title": TITLE,
                    "Subject": description,
                    "Keywords": description,
                    "CreationDate": None,
                    "ModDate": None,
                },
            )
            canvas.savefig(
                svg_stream,
                format="svg",
                dpi=300,
                metadata={
                    "Title": TITLE,
                    "Description": description,
                    "Date": None,
                },
            )
            return pdf_stream.getvalue(), svg_stream.getvalue()
        finally:
            plt.close(canvas)


def _atomic_write_bytes(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    try:
        with temporary.open("wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def write_figure(rows, *, evidence_sha256, outdir):
    pdf, svg = render_figure(rows, evidence_sha256=evidence_sha256)
    outdir = Path(outdir).resolve()
    paths = FigurePaths(outdir / PDF_NAME, outdir / SVG_NAME)
    _atomic_write_bytes(paths.pdf, pdf)
    _atomic_write_bytes(paths.svg, svg)
    return paths


def _load_inputs(csv_path, evidence_path):
    try:
        with Path(csv_path).open(newline="", encoding="utf-8") as stream:
            rows = list(csv.DictReader(stream))
        evidence = json.loads(Path(evidence_path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, csv.Error, json.JSONDecodeError) as error:
        raise FigureDataError(f"invalid figure input: {error}") from error
    if evidence.get("csv_sha256") != artifacts.sha256_file(csv_path):
        raise FigureDataError("CSV/evidence SHA-256 mismatch")
    return rows, artifacts.sha256_file(evidence_path)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--outdir", type=Path)
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    try:
        rows, evidence_sha256 = _load_inputs(args.csv, args.evidence)
        paths = write_figure(
            rows,
            evidence_sha256=evidence_sha256,
            outdir=args.outdir or args.csv.resolve().parent,
        )
        print(f"Wrote {paths.pdf}")
        print(f"Wrote {paths.svg}")
        return 0
    except (FigureDataError, OSError) as error:
        print(f"G4_FIGURE_FAILED error={error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
