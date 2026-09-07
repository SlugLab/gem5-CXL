#!/usr/bin/env python3
# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

"""Render the accepted G12 M2NDP PR/BC latency spectrum.

Chart contract: show how measured M2NDP kernel time changes from 200 ns to
2 us for the two accepted G12 graph workloads.  Each panel uses its own time
axis because the analytical question is latency sensitivity within a workload,
not absolute cross-workload ranking.
"""

from __future__ import annotations

import argparse
import csv
import os
from pathlib import Path


os.environ.setdefault("SOURCE_DATE_EPOCH", "0")
os.environ.setdefault("MPLCONFIGDIR", "/tmp/g12-m2ndp-matplotlib")

G12_GRAPH_SHA256 = (
    "759003842b672ad90eabbd5b045980e9ddf43a95bffb01b318db7fc4b8b551f1"
)
WORKLOADS = ("pr_spmv", "gap_bc")
WORKLOAD_LABELS = {
    "pr_spmv": "PageRank SpMV",
    "gap_bc": "GAP BC",
}
LATENCIES = ("200ns", "500ns", "1us", "2us")
LATENCY_LABELS = ("200 ns", "500 ns", "1 µs", "2 µs")
DEFAULT_INPUT = Path(
    "/mnt/disk0/gem5-CXL-eval/cira-amu-m2ndp-spectrum/shared/"
    "g12-graph-m2ndp-r1/publication/g12-m2ndp-measured-raw.csv"
)
DEFAULT_OUTPUT = DEFAULT_INPUT.parent / "g12-m2ndp-latency-spectrum"


def load_rows(path: Path) -> dict[tuple[str, str], float]:
    """Load exactly the eight accepted, measured G12 M2NDP coordinates."""
    result: dict[tuple[str, str], float] = {}
    with Path(path).open(encoding="utf-8", newline="") as stream:
        for line, row in enumerate(csv.DictReader(stream), start=2):
            coordinate = (row.get("workload", ""), row.get("latency", ""))
            if coordinate[0] not in WORKLOADS or coordinate[1] not in LATENCIES:
                raise ValueError(f"line {line}: unexpected coordinate {coordinate}")
            if coordinate in result:
                raise ValueError(f"line {line}: duplicate coordinate {coordinate}")
            if row.get("system") != "m2ndp":
                raise ValueError(f"line {line}: system is not m2ndp")
            if row.get("status") != "pass":
                raise ValueError(f"line {line}: row is not accepted")
            if row.get("measurement_kind") != "measured":
                raise ValueError(f"line {line}: row is not measured")
            if row.get("graph_sha256") != G12_GRAPH_SHA256:
                raise ValueError(f"line {line}: graph identity differs")
            try:
                time_ms = float(row["time_ms"])
            except (KeyError, TypeError, ValueError) as error:
                raise ValueError(f"line {line}: invalid time_ms") from error
            if time_ms <= 0:
                raise ValueError(f"line {line}: time_ms must be positive")
            result[coordinate] = time_ms
    expected = {(workload, latency) for workload in WORKLOADS for latency in LATENCIES}
    if set(result) != expected:
        missing = sorted(expected - set(result))
        raise ValueError(f"expected eight coordinates; missing={missing}")
    return result


def render(rows: dict[tuple[str, str], float], output: Path) -> None:
    """Write PDF, SVG, and PNG using the manuscript's compact Matplotlib style."""
    import matplotlib

    matplotlib.use("Agg")
    matplotlib.rcParams["svg.hashsalt"] = "g12-m2ndp-latency-spectrum-v1"
    import matplotlib.pyplot as plt

    colors = ("#2369A1", "#D27A18")
    with matplotlib.rc_context({
        "font.family": "DejaVu Sans",
        "font.size": 8.2,
        "axes.titlesize": 8.8,
        "axes.labelsize": 8.2,
        "axes.edgecolor": "#4A4A4A",
        "axes.labelcolor": "#252525",
        "xtick.color": "#333333",
        "ytick.color": "#333333",
        "pdf.fonttype": 42,
        "ps.fonttype": 42,
        "pdf.compression": 9,
    }):
        figure, axes = plt.subplots(1, 2, figsize=(6.9, 2.45))
        x = list(range(len(LATENCIES)))
        for ax, workload, color in zip(axes, WORKLOADS, colors):
            values = [rows[(workload, latency)] for latency in LATENCIES]
            increase = (values[-1] / values[0] - 1.0) * 100.0
            ax.plot(
                x, values, color=color, marker="o", linewidth=1.6,
                markersize=4.5, markerfacecolor="white", markeredgewidth=1.2,
            )
            span = max(values) - min(values)
            pad = max(span * 0.32, max(values) * 0.025)
            ax.set_ylim(min(values) - pad, max(values) + pad)
            ax.set_xticks(x, LATENCY_LABELS)
            ax.set_title(f"{WORKLOAD_LABELS[workload]}  (+{increase:.1f}%)")
            ax.set_xlabel("CXL round-trip latency")
            ax.grid(axis="y", color="#DDDDDD", linewidth=0.6)
            ax.spines[["top", "right"]].set_visible(False)
            for index, value in enumerate(values):
                alignment = "left" if index == 0 else "right" if index == len(values) - 1 else "center"
                ax.annotate(
                    f"{value:.3f}", (index, value), xytext=(0, 6),
                    textcoords="offset points", ha=alignment, va="bottom",
                    fontsize=7.0, color="#252525",
                )
        axes[0].set_ylabel("M²NDP kernel time (ms)")
        figure.suptitle("G12 measured M²NDP latency spectrum", y=0.98, fontsize=9.2)
        figure.text(
            0.995, 0.01, "Independent y-axes; lower is better",
            ha="right", va="bottom", fontsize=6.7, color="#555555",
        )
        figure.tight_layout(rect=(0, 0.06, 1, 0.91))
        output = Path(output)
        output.parent.mkdir(parents=True, exist_ok=True)
        metadata = {
            "Title": "G12 measured M2NDP latency spectrum",
            "Creator": Path(__file__).name,
            "CreationDate": None,
            "ModDate": None,
        }
        figure.savefig(
            output.with_suffix(".pdf"), format="pdf", metadata=metadata,
            bbox_inches="tight",
        )
        figure.savefig(
            output.with_suffix(".svg"), format="svg",
            metadata={"Title": metadata["Title"], "Date": None},
            bbox_inches="tight",
        )
        figure.savefig(
            output.with_suffix(".png"), format="png", dpi=300,
            metadata={"Software": metadata["Creator"]}, bbox_inches="tight",
        )
        plt.close(figure)


def main(arguments=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    options = parser.parse_args(arguments)
    render(load_rows(options.input), options.output)
    print(f"G12_M2NDP_FIGURE_WRITTEN output={options.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
