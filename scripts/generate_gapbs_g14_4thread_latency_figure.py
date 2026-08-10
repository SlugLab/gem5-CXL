#!/usr/bin/env python3
# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

"""Render the validated g14/four-thread latency sweep."""

import dataclasses
import io
import math
import os
import re
from pathlib import Path

os.environ.setdefault("MPLCONFIGDIR", "/tmp/gapbs-g14-matplotlib")
import matplotlib
matplotlib.use("Agg", force=True)
from matplotlib import pyplot as plt

try:
    from scripts import generate_gapbs_g14_4thread_latency_results as results
except ImportError:
    import generate_gapbs_g14_4thread_latency_results as results


SERIES = ("AMU", "CIRA", "M2NDP")
SYSTEMS = {"AMU": "amu", "CIRA": "cira", "M2NDP": "m2ndp"}
COLORS = {"AMU": "#0072B2", "CIRA": "#D18F00", "M2NDP": "#6B8E23"}
STYLES = {"AMU": ("-", "o"), "CIRA": ("--", "s"), "M2NDP": (":", "^")}


class FigureDataError(ValueError):
    pass


@dataclasses.dataclass(frozen=True)
class FigureData:
    latency_ns: tuple[int, ...]
    series: dict[str, tuple[float, ...]]
    y_scale: str
    evidence_sha256: str


def _positive(value, label):
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as error:
        raise FigureDataError(f"{label} must be positive") from error
    if not math.isfinite(number) or number <= 0:
        raise FigureDataError(f"{label} must be positive")
    return number


def prepare_figure_data(rows, *, evidence_sha256):
    if not re.fullmatch(r"[0-9a-f]{64}", evidence_sha256):
        raise FigureDataError("evidence SHA-256 is invalid")
    rows = tuple(dict(row) for row in rows)
    if len(rows) != 16:
        raise FigureDataError("figure requires 16 validated rows")
    graph_hashes = {row.get("graph_sha256") for row in rows}
    manifest_hashes = {row.get("profile_manifest_sha256") for row in rows}
    if len(graph_hashes) != 1 or len(manifest_hashes) != 1:
        raise FigureDataError("figure rows have mixed graph provenance")
    try:
        ordered = results.validate_matrix(
            rows, graph_sha256=next(iter(graph_hashes)),
            profile_manifest_sha256=next(iter(manifest_hashes)),
        )
    except results.PublicationError as error:
        raise FigureDataError(str(error)) from error
    by_key = {(row["latency"], row["system"]): row for row in ordered}
    series = {
        label: tuple(_positive(by_key[(latency, SYSTEMS[label])]["speedup"], label)
                     for latency in results.LATENCIES)
        for label in SERIES
    }
    values = [1.0, *(value for points in series.values() for value in points)]
    return FigureData(tuple(results.LATENCY_NS[item] for item in results.LATENCIES),
                      series, "log" if max(values) / min(values) >= 20 else "linear",
                      evidence_sha256)


def render_figure(rows, *, evidence_sha256):
    data = prepare_figure_data(rows, evidence_sha256=evidence_sha256)
    description = (
        f"Evidence SHA-256: {data.evidence_sha256}; g14 PageRank; "
        "4 timing cores/threads; all CXL; 2 trials; trial 1 measured; "
        "20 synchronous double-buffered iterations; bit-exact"
    )
    rc = {"font.family": "DejaVu Sans", "font.size": 8, "pdf.fonttype": 42,
          "svg.fonttype": "none", "svg.hashsalt": "gapbs-g14-4thread-latency"}
    with plt.rc_context(rc):
        canvas, axis = plt.subplots(figsize=(7.0, 3.2))
        try:
            for label in SERIES:
                linestyle, marker = STYLES[label]
                axis.plot(data.latency_ns, data.series[label], color=COLORS[label],
                          linestyle=linestyle, marker=marker, linewidth=1.7,
                          markersize=5, markerfacecolor="white", label=label)
            axis.axhline(1.0, color="#666666", linestyle=(0, (4, 2)), linewidth=1)
            axis.set_xticks(data.latency_ns, ("200", "500", "1,000", "2,000"))
            axis.set_xlabel("CXL link latency (ns)")
            axis.set_ylabel("Speedup vs. matched Vanilla CXL")
            axis.set_yscale(data.y_scale)
            axis.grid(axis="y", color="#D9D9D9", linewidth=0.55)
            axis.spines[["top", "right"]].set_visible(False)
            axis.legend(frameon=False, ncol=3, loc="upper left")
            canvas.suptitle("GAPBS PageRank speedup vs. CXL latency",
                            x=0.10, y=0.97, ha="left", fontweight="bold")
            canvas.text(0.10, 0.89,
                        "g14 · 4 threads · all-CXL · bit-exact",
                        ha="left", fontsize=7.2, color="#4A4A4A")
            canvas.subplots_adjust(left=0.12, right=0.98, bottom=0.20, top=0.78)
            pdf = io.BytesIO()
            svg = io.BytesIO()
            canvas.savefig(pdf, format="pdf", dpi=300,
                           metadata={"Title": "g14 CXL latency sweep",
                                     "Subject": description,
                                     "Keywords": description,
                                     "CreationDate": None, "ModDate": None})
            canvas.savefig(svg, format="svg", dpi=300,
                           metadata={"Title": "g14 CXL latency sweep",
                                     "Description": description, "Date": None})
            return pdf.getvalue(), svg.getvalue()
        finally:
            plt.close(canvas)


def _write(path, payload):
    path = Path(path)
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
    outdir = Path(outdir)
    _write(outdir / results.PDF_NAME, pdf)
    _write(outdir / results.SVG_NAME, svg)
    return outdir / results.PDF_NAME, outdir / results.SVG_NAME

