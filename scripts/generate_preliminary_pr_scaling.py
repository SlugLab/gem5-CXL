#!/usr/bin/env python3
# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

"""Publish a source-bound preliminary PageRank scaling figure.

This publisher deliberately omits g20 until a measured point exists.  It
does not estimate missing coordinates or promote incomplete campaign state.
"""

import argparse
import csv
import hashlib
import json
from decimal import Decimal, InvalidOperation
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.ticker import FuncFormatter, FixedLocator


SYSTEMS = ("vanilla", "amu", "cira", "m2ndp")
SCALES = (4, 12, 14)
COLORS = {
    "vanilla": "#4B5563",
    "amu": "#D97706",
    "cira": "#2563EB",
    "m2ndp": "#7C3AED",
}
MARKERS = {"vanilla": "o", "amu": "s", "cira": "D", "m2ndp": "^"}
LABELS = {
    "vanilla": "Vanilla CXL", "amu": "AMU",
    "cira": "CIRA", "m2ndp": r"M$^2$NDP",
}


class ScalingPublicationError(RuntimeError):
    """The preliminary evidence is missing, duplicated, or malformed."""


def _sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _decimal(value, label):
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ScalingPublicationError(f"{label} is not numeric") from error
    if not result.is_finite() or result <= 0:
        raise ScalingPublicationError(f"{label} must be positive and finite")
    return result


def _read_csv(path):
    try:
        with Path(path).open(newline="", encoding="utf-8") as stream:
            return list(csv.DictReader(stream))
    except OSError as error:
        raise ScalingPublicationError(f"cannot read g4 CSV: {error}") from error


def _read_json(path):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ScalingPublicationError(
            f"cannot read campaign state: {error}"
        ) from error
    if not isinstance(value, dict):
        raise ScalingPublicationError("campaign state must be an object")
    return value


def _row(scale, system, seconds, vanilla, *, verification, source_path,
         source_sha256, evidence_scope, bit_exact):
    return {
        "benchmark": "pr_spmv",
        "scale": scale,
        "vertices": 1 << scale,
        "latency": "1us",
        "system": system,
        "latency_seconds": format(seconds, "f"),
        "speedup": format(vanilla / seconds, "f"),
        "verification": verification,
        "bit_exact": bit_exact,
        "evidence_scope": evidence_scope,
        "source_path": str(source_path),
        "source_sha256": source_sha256,
    }


def collect_rows(g4_csv, campaign_state):
    """Collect measured g4/g12/g14 rows and recompute every speedup."""
    g4_csv = Path(g4_csv).resolve()
    campaign_state = Path(campaign_state).resolve()
    selected = [
        row for row in _read_csv(g4_csv)
        if row.get("benchmark") == "pr_spmv" and row.get("latency") == "1us"
    ]
    by_system = {row.get("system"): row for row in selected}
    if len(selected) != 4 or set(by_system) != set(SYSTEMS):
        raise ScalingPublicationError("g4 CSV must contain four unique 1us rows")
    g4_seconds = {
        system: _decimal(by_system[system].get("latency_seconds"), f"g4 {system}")
        for system in SYSTEMS
    }
    rows = [
        _row(
            4, system, g4_seconds[system], g4_seconds["vanilla"],
            verification=by_system[system].get("verification", "unknown"),
            bit_exact=by_system[system].get("bit_exact", "unknown"),
            evidence_scope="g4_latency_sweep",
            source_path=by_system[system].get("source_path", str(g4_csv)),
            source_sha256=by_system[system].get(
                "source_sha256", _sha256_file(g4_csv)
            ),
        )
        for system in SYSTEMS
    ]

    state = _read_json(campaign_state)
    points = state.get("points")
    if not isinstance(points, dict):
        raise ScalingPublicationError("campaign points are missing")
    campaign_names = {
        "vanilla": "vanilla", "amu": "amu",
        "cira": "cira-few-shot", "m2ndp": "m2ndp",
    }
    for scale in (12, 14):
        evidence = {}
        point_rows = {}
        for system, name in campaign_names.items():
            key = f"g{scale}:{name}"
            point = points.get(key)
            if not isinstance(point, dict) or point.get("status") != "passed":
                raise ScalingPublicationError(f"campaign point is not passed: {key}")
            row_evidence = point.get("evidence")
            if (
                not isinstance(row_evidence, dict)
                or row_evidence.get("verification") != "pass"
            ):
                raise ScalingPublicationError(
                    f"campaign point verification is not pass: {key}"
                )
            artifacts = point.get("artifacts")
            if not isinstance(artifacts, dict) or not artifacts:
                raise ScalingPublicationError(
                    f"campaign point artifacts are missing: {key}"
                )
            source_path, source_sha256 = sorted(artifacts.items())[0]
            if not isinstance(source_sha256, str) or len(source_sha256) != 64:
                raise ScalingPublicationError(
                    f"campaign point artifact hash is invalid: {key}"
                )
            evidence[system] = _decimal(
                row_evidence.get("seconds"), f"g{scale} {system}"
            )
            point_rows[system] = (row_evidence, source_path, source_sha256)
        vanilla = evidence["vanilla"]
        for system in SYSTEMS:
            row_evidence, source_path, source_sha256 = point_rows[system]
            rows.append(_row(
                scale, system, evidence[system], vanilla,
                verification=row_evidence["verification"],
                bit_exact="not_checked_by_publisher",
                evidence_scope="asymmetric_offload_campaign",
                source_path=source_path,
                source_sha256=source_sha256,
            ))
    return rows


def _atomic_text(path, text):
    path = Path(path)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def _write_csv(path, rows):
    path = Path(path)
    temporary = path.with_name(f".{path.name}.tmp")
    with temporary.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(
            stream, fieldnames=tuple(rows[0]), lineterminator="\n"
        )
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(path)


def _speedup_tick(value, _position):
    return f"{value:g}×"


def _render(rows, output_stem):
    by_key = {(row["scale"], row["system"]): row for row in rows}
    x_positions = {scale: index for index, scale in enumerate((4, 12, 14, 20))}
    offsets = {"vanilla": -0.24, "amu": -0.08, "cira": 0.08, "m2ndp": 0.24}
    fig, axis = plt.subplots(figsize=(7.05, 3.15), constrained_layout=True)
    for system in SYSTEMS:
        x_values = [x_positions[scale] + offsets[system] for scale in SCALES]
        y_values = [float(Decimal(by_key[(scale, system)]["speedup"])) for scale in SCALES]
        axis.scatter(
            x_values, y_values, label=LABELS[system], color=COLORS[system],
            marker=MARKERS[system], s=48, linewidths=0.8, edgecolors="white",
            zorder=3,
        )
        for scale, x_value, y_value in zip(SCALES, x_values, y_values):
            label_offset = 15 if scale == 12 and system == "cira" else 6
            axis.annotate(
                f"{y_value:.2f}×", (x_value, y_value), xytext=(0, label_offset),
                textcoords="offset points", ha="center", va="bottom",
                fontsize=6.3, color=COLORS[system],
            )
    axis.set_yscale("log", base=2)
    ticks = (0.03125, 0.0625, 0.125, 0.25, 0.5, 1, 2, 4, 8, 16, 32)
    axis.yaxis.set_major_locator(FixedLocator(ticks))
    axis.yaxis.set_major_formatter(FuncFormatter(_speedup_tick))
    axis.axhline(1, color="#374151", linewidth=0.9, linestyle="--", zorder=1)
    axis.grid(axis="y", color="#D1D5DB", linewidth=0.55, alpha=0.75)
    axis.set_axisbelow(True)
    axis.set_xlim(-0.55, 3.45)
    axis.set_ylim(0.025, 32)
    axis.set_xticks(tuple(x_positions.values()), ("g4", "g12", "g14", "g20"))
    axis.set_xlabel("Graph scale (vertices = $2^g$)")
    axis.set_ylabel("Speedup vs. Vanilla CXL (log$_2$)")
    axis.set_title("Preliminary PageRank scaling at 1 µs modeled CXL latency")
    axis.text(
        x_positions[20], 0.045, "pending", ha="center", va="center",
        fontsize=7.2, color="#6B7280", style="italic",
    )
    axis.legend(
        loc="upper left", ncol=4, frameon=False, fontsize=7.2,
        handletextpad=0.4, columnspacing=1.0,
    )
    for spine in ("top", "right"):
        axis.spines[spine].set_visible(False)
    for suffix in ("pdf", "svg", "png"):
        kwargs = {"dpi": 220} if suffix == "png" else {}
        fig.savefig(f"{output_stem}.{suffix}", **kwargs)
    plt.close(fig)
    svg_path = Path(f"{output_stem}.svg")
    svg_text = svg_path.read_text(encoding="utf-8")
    _atomic_text(
        svg_path,
        "\n".join(line.rstrip() for line in svg_text.splitlines()) + "\n",
    )


def publish(g4_csv, campaign_state, outdir):
    outdir = Path(outdir).resolve()
    outdir.mkdir(parents=True, exist_ok=True)
    rows = collect_rows(g4_csv, campaign_state)
    csv_path = outdir / "pagerank-scaling-preliminary.csv"
    json_path = outdir / "pagerank-scaling-preliminary.json"
    stem = outdir / "pagerank-scaling-preliminary"
    _write_csv(csv_path, rows)
    _atomic_text(json_path, json.dumps({
        "schema": 1,
        "status": "preliminary",
        "measured_scales": list(SCALES),
        "pending_scales": [20],
        "rows": rows,
    }, sort_keys=True, separators=(",", ":")) + "\n")
    _render(rows, stem)
    sources = {
        "g4_csv": {
            "path": str(Path(g4_csv).resolve()),
            "sha256": _sha256_file(g4_csv),
        },
        "campaign_state": {
            "path": str(Path(campaign_state).resolve()),
            "sha256": _sha256_file(campaign_state),
        },
    }
    outputs = {
        path.name: {"path": path.name, "sha256": _sha256_file(path)}
        for path in (
            csv_path, json_path, stem.with_suffix(".pdf"),
            stem.with_suffix(".svg"), stem.with_suffix(".png"),
        )
    }
    manifest = {
        "schema": 1,
        "status": "preliminary",
        "measured_scales": list(SCALES),
        "pending_scales": [20],
        "correctness_policy": "consume_passed_points_without_full-spectrum_gate",
        "sources": sources,
        "outputs": outputs,
    }
    manifest_path = outdir / "publication-manifest.json"
    _atomic_text(
        manifest_path,
        json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n",
    )
    return manifest


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--g4-csv", type=Path, required=True)
    parser.add_argument("--campaign-state", type=Path, required=True)
    parser.add_argument("--outdir", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv=None):
    options = parse_args(argv)
    try:
        manifest = publish(options.g4_csv, options.campaign_state, options.outdir)
    except (ScalingPublicationError, OSError) as error:
        print(f"PRELIMINARY_PR_SCALING_FAILED error={error}")
        return 1
    print(
        "PRELIMINARY_PR_SCALING_PASS "
        f"measured={manifest['measured_scales']} pending={manifest['pending_scales']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
