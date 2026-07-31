#!/usr/bin/env python3
# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

"""Render the validated GAPBS g20 paper figure."""

import dataclasses
import io
import math
import os
import re


os.environ.setdefault("MPLCONFIGDIR", "/tmp/gapbs-matplotlib")

import matplotlib

matplotlib.use("Agg", force=True)
from matplotlib import pyplot as plt


SYSTEMS = ("Vanilla CXL", "AMU", "CIRA", "M2NDP")
LATENCY_KEYS = ("200ns", "500ns", "1us", "2us")
LATENCY_NS = (200, 500, 1000, 2000)
SENSITIVITY_SERIES = ("AMU", "CIRA")
TITLE = "End-to-end PageRank latency and CXL-link sensitivity"
FIGURE_SIZE = (7.0, 3.2)
COLORS = {
    "Vanilla CXL": "#7A7A7A",
    "AMU": "#0072B2",
    "CIRA": "#E69F00",
    "M2NDP": "#009E73",
}
HATCHES = {
    "Vanilla CXL": "",
    "AMU": "///",
    "CIRA": "...",
    "M2NDP": "xxx",
}


class FigureDataError(ValueError):
    """Validated table data cannot satisfy the chart contract."""


@dataclasses.dataclass(frozen=True)
class FigureData:
    systems: tuple
    formal_latencies: tuple
    formal_speedups: tuple
    latency_ns: tuple
    sensitivity: dict
    evidence_sha256: str
    panel_a_scale: str


def _positive_float(value, context):
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as error:
        raise FigureDataError(
            f"{context} must be finite and positive"
        ) from error
    if not math.isfinite(number) or number <= 0:
        raise FigureDataError(
            f"{context} must be finite and positive"
        )
    return number


def choose_latency_scale(values):
    numbers = tuple(
        _positive_float(value, "formal latencies") for value in values
    )
    if not numbers:
        raise FigureDataError(
            "formal latencies must be finite and positive"
        )
    return "log" if max(numbers) / min(numbers) >= 10.0 else "linear"


def prepare_figure_data(rows, sensitivity, *, evidence_sha256):
    if not re.fullmatch(r"[0-9a-f]{64}", evidence_sha256):
        raise FigureDataError("evidence SHA-256 is invalid")

    rows = tuple(rows)
    if tuple(getattr(row, "system", None) for row in rows) != SYSTEMS:
        raise FigureDataError("formal systems are absent or out of order")

    formal_latencies = tuple(
        _positive_float(row.latency_seconds, f"{row.system} latency")
        for row in rows
    )
    formal_speedups = tuple(
        _positive_float(row.speedup, f"{row.system} speedup")
        for row in rows
    )

    if not isinstance(sensitivity, dict) or set(sensitivity) != set(
        LATENCY_KEYS
    ):
        raise FigureDataError("sensitivity latency keys are incomplete")

    series_values = {}
    for series in SENSITIVITY_SERIES:
        values = []
        for latency in LATENCY_KEYS:
            context = f"{latency}/Geo./{series}"
            try:
                value = sensitivity[latency]["Geo."][series]
            except (KeyError, TypeError) as error:
                raise FigureDataError(f"missing {context}") from error
            values.append(_positive_float(value, context))
        series_values[series] = tuple(values)

    return FigureData(
        systems=SYSTEMS,
        formal_latencies=formal_latencies,
        formal_speedups=formal_speedups,
        latency_ns=LATENCY_NS,
        sensitivity=series_values,
        evidence_sha256=evidence_sha256,
        panel_a_scale=choose_latency_scale(formal_latencies),
    )


def _metadata_description(data):
    return (
        f"Evidence SHA-256: {data.evidence_sha256}; "
        f"panel_a_scale={data.panel_a_scale}; "
        "panel_a=g20 PageRank, 2 cores, all CXL, 1 us, "
        "20 iterations; panel_b=scale-4, single-core GAPBS "
        "sensitivity, not g20 evidence"
    )


def _style_axis(axis):
    axis.spines["top"].set_visible(False)
    axis.spines["right"].set_visible(False)
    axis.tick_params(colors="#202020", width=0.7)
    axis.xaxis.label.set_color("#202020")
    axis.yaxis.label.set_color("#202020")
    axis.title.set_color("#202020")


def _render_formal_panel(axis, data):
    positions = tuple(range(len(data.systems)))
    bars = axis.barh(
        positions,
        data.formal_latencies,
        color=[COLORS[system] for system in data.systems],
        edgecolor="#303030",
        linewidth=0.55,
        height=0.62,
    )
    for bar, system in zip(bars, data.systems, strict=True):
        bar.set_hatch(HATCHES[system])

    axis.set_yticks(positions, data.systems)
    axis.invert_yaxis()
    axis.set_xscale(data.panel_a_scale)
    minimum = min(data.formal_latencies)
    maximum = max(data.formal_latencies)
    if data.panel_a_scale == "log":
        axis.set_xlim(minimum * 0.45, maximum * 4.2)
        label_positions = [value * 1.06 for value in data.formal_latencies]
    else:
        axis.set_xlim(0, maximum * 1.58)
        label_positions = [value + maximum * 0.025 for value in data.formal_latencies]
    for index, (x, latency, speedup) in enumerate(
        zip(
            label_positions,
            data.formal_latencies,
            data.formal_speedups,
            strict=True,
        )
    ):
        axis.text(
            x,
            index,
            f"{latency:.6f} s  ({speedup:.2f}x)",
            ha="left",
            va="center",
            fontsize=6.8,
            color="#202020",
        )
    axis.set_xlabel("End-to-end latency (s)")
    axis.set_title("(a) Formal g20 PageRank", loc="left", fontweight="bold")
    axis.grid(axis="x", color="#D9D9D9", linewidth=0.55)
    axis.set_axisbelow(True)
    axis.text(
        0.5,
        -0.30,
        "2 cores, all CXL, 1 us, 20 iterations",
        transform=axis.transAxes,
        ha="center",
        va="top",
        fontsize=6.5,
        color="#404040",
    )
    _style_axis(axis)


def _render_sensitivity_panel(axis, data):
    styles = {
        "AMU": {"linestyle": "-", "marker": "o"},
        "CIRA": {"linestyle": "--", "marker": "s"},
    }
    for series in SENSITIVITY_SERIES:
        axis.plot(
            data.latency_ns,
            data.sensitivity[series],
            color=COLORS[series],
            linewidth=1.7,
            markersize=4.5,
            markeredgecolor="#303030",
            markeredgewidth=0.45,
            label=series,
            **styles[series],
        )
    axis.axhline(
        1.0,
        color="#777777",
        linestyle=(0, (3, 2)),
        linewidth=0.8,
        zorder=0,
    )
    axis.annotate(
        "AMU",
        (data.latency_ns[-1], data.sensitivity["AMU"][-1]),
        xytext=(5, -8),
        textcoords="offset points",
        color=COLORS["AMU"],
        fontsize=7.2,
        fontweight="bold",
    )
    axis.annotate(
        "CIRA",
        (data.latency_ns[-1], data.sensitivity["CIRA"][-1]),
        xytext=(5, 5),
        textcoords="offset points",
        color=COLORS["CIRA"],
        fontsize=7.2,
        fontweight="bold",
    )
    all_values = [
        1.0,
        *data.sensitivity["AMU"],
        *data.sensitivity["CIRA"],
    ]
    low = min(all_values)
    high = max(all_values)
    padding = max(0.08, (high - low) * 0.18)
    axis.set_ylim(max(0, low - padding), high + padding)
    axis.set_xticks(data.latency_ns, ("200", "500", "1,000", "2,000"))
    axis.set_xlabel("CXL latency (ns)")
    axis.set_ylabel("Geo. speedup vs. Vanilla CXL")
    axis.set_title("(b) Link-latency sensitivity", loc="left", fontweight="bold")
    axis.grid(axis="y", color="#D9D9D9", linewidth=0.55)
    axis.set_axisbelow(True)
    axis.text(
        0.5,
        -0.30,
        "scale-4, single-core GAPBS; not g20 evidence",
        transform=axis.transAxes,
        ha="center",
        va="top",
        fontsize=6.5,
        color="#404040",
    )
    _style_axis(axis)


def render_figure(rows, sensitivity, *, evidence_sha256):
    data = prepare_figure_data(
        rows, sensitivity, evidence_sha256=evidence_sha256
    )
    rc = {
        "font.family": "DejaVu Sans",
        "font.size": 8,
        "axes.labelsize": 7.5,
        "axes.titlesize": 8.5,
        "xtick.labelsize": 6.8,
        "ytick.labelsize": 7.2,
        "pdf.fonttype": 42,
        "svg.fonttype": "none",
        "svg.hashsalt": "gapbs-g20-e2e",
    }
    with plt.rc_context(rc):
        canvas, (formal_axis, sensitivity_axis) = plt.subplots(
            1, 2, figsize=FIGURE_SIZE
        )
        try:
            canvas.patch.set_facecolor("white")
            _render_formal_panel(formal_axis, data)
            _render_sensitivity_panel(sensitivity_axis, data)
            canvas.suptitle(
                TITLE,
                x=0.5,
                y=0.97,
                fontsize=10,
                fontweight="bold",
                color="#202020",
            )
            canvas.subplots_adjust(
                left=0.105,
                right=0.95,
                bottom=0.25,
                top=0.80,
                wspace=0.48,
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
