#!/usr/bin/env python3
"""Plot timing-CPU CIRA/Vortex dispatch sensitivity to CXL link delay.

The input directories are completed gem5 outdirs.  Each point is accepted only
when its stats contain a positive simulated time and positive CXL-link monitor
traffic.  This is deliberately an end-to-end *dispatch* figure, not a claim
about whole-workload acceleration.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
from pathlib import Path

import matplotlib.pyplot as plt


TICKS_PER_MS = 1_000_000_000
STAT_PATTERNS = {
    "sim_ticks": re.compile(r"^simTicks\s+(\d+)\s", re.MULTILINE),
    "read_samples": re.compile(
        r"vortex_cxl_latency_monitor\.readLatencyHist::samples\s+(\d+)"
    ),
    "read_rtt_ticks": re.compile(
        r"vortex_cxl_latency_monitor\.readLatencyHist::mean\s+(\d+)"
    ),
    "write_samples": re.compile(
        r"vortex_cxl_latency_monitor\.writeLatencyHist::samples\s+(\d+)"
    ),
    "write_rtt_ticks": re.compile(
        r"vortex_cxl_latency_monitor\.writeLatencyHist::mean\s+(\d+)"
    ),
}


def _integer(stats: str, name: str) -> int:
    match = STAT_PATTERNS[name].search(stats)
    if match is None:
        raise ValueError(f"missing {name} in stats")
    value = int(match.group(1))
    if value <= 0:
        raise ValueError(f"non-positive {name} in stats")
    return value


def _parse_point(spec: str) -> dict[str, object]:
    try:
        latency_ns_text, outdir_text = spec.split("=", 1)
        latency_ns = int(latency_ns_text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "--run must have the form <single-link-latency-ns>=<gem5-outdir>"
        ) from exc
    outdir = Path(outdir_text).resolve()
    stats_path = outdir / "stats.txt"
    if latency_ns <= 0 or not stats_path.is_file():
        raise argparse.ArgumentTypeError(f"invalid run: {spec}")
    stats = stats_path.read_text(encoding="utf-8")
    row = {
        "link_latency_ns": latency_ns,
        "sim_ticks": _integer(stats, "sim_ticks"),
        "read_samples": _integer(stats, "read_samples"),
        "read_rtt_ticks": _integer(stats, "read_rtt_ticks"),
        "write_samples": _integer(stats, "write_samples"),
        "write_rtt_ticks": _integer(stats, "write_rtt_ticks"),
        "stats_path": str(stats_path),
    }
    # A request and a response must each traverse the configured SerialLink.
    # Do not silently accept monitor values that are too small to demonstrate
    # the configured link was on the request path.
    expected_rtt_ticks = 2 * latency_ns * 1_000
    if row["read_rtt_ticks"] < expected_rtt_ticks:
        raise argparse.ArgumentTypeError(
            f"CXL read RTT below two link traversals for {stats_path}"
        )
    return row


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", action="append", required=True, type=_parse_point)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    rows = sorted(args.run, key=lambda row: row["link_latency_ns"])
    if len({row["link_latency_ns"] for row in rows}) != len(rows):
        parser.error("each link latency may appear only once")

    x = [row["link_latency_ns"] / 1_000 for row in rows]
    y = [row["sim_ticks"] / TICKS_PER_MS for row in rows]
    labels = [
        f"{value:g} µs" if value >= 1 else f"{value * 1_000:g} ns"
        for value in x
    ]

    plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 10})
    figure, axis = plt.subplots(figsize=(6.3, 3.65))
    axis.plot(
        x, y, color="#2563EB", marker="o", linewidth=2.2, markersize=6,
        markeredgecolor="white", markeredgewidth=0.9,
    )
    axis.set_title("CIRA JIT/Vortex dispatch over a modeled CXL link", pad=10)
    axis.set_xlabel("Single-link CXL latency")
    axis.set_ylabel("End-to-end simulated dispatch time (ms)")
    axis.set_xticks(x, labels)
    axis.set_ylim(bottom=0)
    axis.grid(axis="y", color="#E5E7EB", linewidth=0.8)
    axis.spines[["top", "right"]].set_visible(False)
    for x_value, y_value in zip(x, y):
        axis.annotate(
            f"{y_value:.1f}", (x_value, y_value), xytext=(0, 8),
            textcoords="offset points", ha="center", color="#1F2937", fontsize=9,
        )
    figure.tight_layout()

    output = args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    figure.savefig(output, dpi=240, bbox_inches="tight", facecolor="white")
    figure.savefig(output.with_suffix(".pdf"), bbox_inches="tight", facecolor="white")
    plt.close(figure)

    csv_path = output.with_suffix(".csv")
    with csv_path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(
            stream, fieldnames=rows[0].keys(), lineterminator="\n"
        )
        writer.writeheader()
        writer.writerows(rows)
    output.with_suffix(".manifest.json").write_text(
        json.dumps(
            {
                "schema": 1,
                "scope": "timing-CPU CIRA/Vortex JIT dispatch microbenchmark",
                "link_model": "SerialLink request plus response",
                "csv": csv_path.name,
                "runs": rows,
            },
            indent=2,
            sort_keys=True,
        ) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
