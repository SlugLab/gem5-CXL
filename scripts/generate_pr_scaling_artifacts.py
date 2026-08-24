#!/usr/bin/env python3
# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

"""Publish raw data and plots from complete formal PR-scaling evidence."""

import argparse
import csv
import dataclasses
import hashlib
import io
import json
import os
import re
import shutil
import sys
import tempfile
from decimal import Decimal, InvalidOperation
from pathlib import Path

try:
    from scripts import pr_offload_contract as gate_contract
except ImportError:
    import pr_offload_contract as gate_contract


SCALES = (4, 12, 14, 20)
PERFORMANCE_SCALES = (12, 14, 20)
SYSTEMS = ("vanilla", "amu", "cira", "m2ndp")
ACCELERATORS = ("amu", "cira", "m2ndp")
PROFILE = "pr-scaling-4thread-1us"
TICKS_PER_SECOND = Decimal(10**12)
MIN_ACCELERATOR_SPEEDUP = gate_contract.MIN_SPEEDUP
MAX_ACCELERATOR_SPEEDUP = gate_contract.MAX_SPEEDUP
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class ArtifactError(RuntimeError):
    """Formal scaling evidence or publication violates its contract."""


@dataclasses.dataclass(frozen=True)
class RawRow:
    scale: int
    system: str
    latency_seconds: Decimal
    speedup: Decimal
    native_time_kind: str
    native_time_count: int
    output_elements: int
    outputs: dict
    mechanism: dict


@dataclasses.dataclass(frozen=True)
class ScalingData:
    rows: tuple[RawRow, ...]
    source: dict
    source_record: dict


def _sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256(value, label):
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ArtifactError(f"{label} SHA-256 is invalid")
    return value


def _decimal(value, label):
    if isinstance(value, (bool, float)):
        raise ArtifactError(f"{label} must be an exact decimal")
    try:
        result = Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ArtifactError(f"{label} is not a decimal") from error
    if not result.is_finite() or result <= 0:
        raise ArtifactError(f"{label} must be finite and positive")
    return result


def _integer(value, label):
    if (
        isinstance(value, bool)
        or not isinstance(value, str)
        or not value.isdigit()
        or int(value) <= 0
    ):
        raise ArtifactError(f"{label} must be a positive integer")
    return int(value)


def _close(left, right):
    return abs(left - right) <= max(
        abs(right) * Decimal("1e-18"), Decimal("1e-24")
    )


def _native_time(system, mechanism, latency):
    if system == "m2ndp":
        count = _integer(
            mechanism.get("ndpsim_measured_cycles"),
            "m2ndp ndpsim_measured_cycles",
        )
        period = _decimal(
            mechanism.get("ndpsim_core_period_seconds"),
            "m2ndp NDPSim core period",
        )
        native_seconds = Decimal(count) * period
        kind = "ndpsim_cycles"
    else:
        count = _integer(
            mechanism.get("sim_ticks"), f"{system} sim_ticks"
        )
        native_seconds = Decimal(count) / TICKS_PER_SECOND
        kind = "gem5_ticks"
    if not _close(native_seconds, latency):
        raise ArtifactError(
            f"{system} native timing differs from latency_seconds"
        )
    return kind, count


def _load_json(path):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ArtifactError(f"invalid scaling evidence: {error}") from error
    if not isinstance(value, dict):
        raise ArtifactError("scaling evidence must be an object")
    return value


def load_data(path):
    path = Path(path).resolve()
    value = _load_json(path)
    if (
        value.get("schema") != 1
        or value.get("status") != "complete"
        or value.get("profile") != PROFILE
    ):
        raise ArtifactError("scaling evidence is not complete formal data")
    if value.get("performance_gate") != {
        "status": "passed",
        "checked_points": len(PERFORMANCE_SCALES) * 3,
        "policies": {
            system: gate_contract.performance_policy(system)
            for system in ACCELERATORS
        },
        "offenders": [],
    }:
        raise ArtifactError("scaling evidence performance gate did not pass")
    for field in (
        "graph_set_sha256",
        "g20_graph_sha256",
        "inputs_sha256",
        "calibration_sha256",
        "code_sha256",
        "gem5_sha256",
        "config_sha256",
    ):
        _sha256(value.get(field), field.removesuffix("_sha256"))
    points = value.get("points")
    expected = {
        f"g{scale}:{system}" for scale in SCALES for system in SYSTEMS
    }
    if not isinstance(points, dict) or set(points) != expected:
        raise ArtifactError("scaling evidence must contain exactly 16 points")
    rows = []
    for scale in SCALES:
        baseline = points[f"g{scale}:vanilla"]
        baseline_seconds = _decimal(
            baseline.get("latency_seconds"), f"g{scale} Vanilla latency"
        )
        for system in SYSTEMS:
            point = points[f"g{scale}:{system}"]
            if (
                point.get("status") != "passed"
                or point.get("scale") != scale
                or point.get("system") != system
                or point.get("latency") != "1us"
                or point.get("full_e2e") is not True
            ):
                raise ArtifactError(f"g{scale}:{system} is not formal PASS")
            mechanism = point.get("mechanism")
            if (
                not isinstance(mechanism, dict)
                or mechanism.get("verification") != "pass"
            ):
                raise ArtifactError(
                    f"g{scale}:{system} verification is not pass"
                )
            outputs = point.get("outputs")
            if (
                not isinstance(outputs, dict)
                or not {"rank", "summary"}.issubset(outputs)
            ):
                raise ArtifactError(f"g{scale}:{system} outputs are incomplete")
            for name, digest in outputs.items():
                _sha256(digest, f"g{scale}:{system} {name}")
            output_elements = point.get("output_elements")
            if (
                not isinstance(output_elements, int)
                or isinstance(output_elements, bool)
                or output_elements != 1 << scale
            ):
                raise ArtifactError(
                    f"g{scale}:{system} output element count differs"
                )
            seconds = _decimal(
                point.get("latency_seconds"),
                f"g{scale}:{system} latency",
            )
            speedup = baseline_seconds / seconds
            if (
                scale in PERFORMANCE_SCALES
                and system != "vanilla"
                and not gate_contract.performance_accepted(system, speedup)
            ):
                raise ArtifactError(
                    f"g{scale}:{system} performance gate did not pass"
                )
            stored = _decimal(
                point.get("speedup"), f"g{scale}:{system} stored speedup"
            )
            if not _close(stored, speedup):
                raise ArtifactError(
                    f"g{scale}:{system} stored speedup differs"
                )
            kind, count = _native_time(system, mechanism, seconds)
            rows.append(
                RawRow(
                    scale,
                    system,
                    seconds,
                    speedup,
                    kind,
                    count,
                    output_elements,
                    dict(sorted(outputs.items())),
                    dict(sorted(mechanism.items())),
                )
            )
    return ScalingData(
        rows=tuple(rows),
        source=value,
        source_record={"path": str(path), "sha256": _sha256_file(path)},
    )


def _row_dict(row, data):
    source = data.source
    return {
        "scale": row.scale,
        "system": row.system,
        "latency_seconds": str(row.latency_seconds),
        "speedup": str(row.speedup),
        "native_time_kind": row.native_time_kind,
        "native_time_count": row.native_time_count,
        "output_elements": row.output_elements,
        "verification": row.mechanism["verification"],
        "rank_sha256": row.outputs["rank"],
        "summary_sha256": row.outputs["summary"],
        "graph_set_sha256": source["graph_set_sha256"],
        "g20_graph_sha256": source["g20_graph_sha256"],
        "inputs_sha256": source["inputs_sha256"],
        "calibration_sha256": source["calibration_sha256"],
        "code_sha256": source["code_sha256"],
        "gem5_sha256": source["gem5_sha256"],
        "config_sha256": source["config_sha256"],
        "outputs": row.outputs,
        "mechanism": row.mechanism,
    }


def _raw_json_bytes(data):
    value = {
        "schema": 1,
        "status": "pass",
        "profile": PROFILE,
        "row_count": len(data.rows),
        "source": data.source_record,
        "source_evidence": data.source,
        "rows": [_row_dict(row, data) for row in data.rows],
    }
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _csv_bytes(data):
    fields = (
        "scale", "system", "latency_seconds", "speedup",
        "native_time_kind", "native_time_count", "output_elements",
        "verification", "rank_sha256", "summary_sha256",
        "graph_set_sha256", "g20_graph_sha256", "inputs_sha256",
        "calibration_sha256", "code_sha256", "gem5_sha256",
        "config_sha256", "outputs_json", "mechanism_json",
    )
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    for row in data.rows:
        value = _row_dict(row, data)
        value["outputs_json"] = json.dumps(
            value.pop("outputs"), sort_keys=True, separators=(",", ":")
        )
        value["mechanism_json"] = json.dumps(
            value.pop("mechanism"), sort_keys=True, separators=(",", ":")
        )
        writer.writerow(value)
    return stream.getvalue().encode()


def _fmt(value, digits=6):
    return f"{value:.{digits}f}"


def _table_bytes(data):
    lines = [
        r"\begin{tabular}{lrrrr}",
        r"\toprule",
        r"Scale & Vanilla (s) & AMU & CIRA & M2NDP \\",
        r"\midrule",
    ]
    lookup = {(row.scale, row.system): row for row in data.rows}
    for scale in SCALES:
        base = lookup[(scale, "vanilla")]
        lines.append(
            f"g{scale} & {_fmt(base.latency_seconds)} & "
            + " & ".join(
                f"{_fmt(lookup[(scale, system)].latency_seconds)} "
                f"({_fmt(lookup[(scale, system)].speedup, 3)}$\\times$)"
                for system in ACCELERATORS
            )
            + r" \\"
        )
    lines.extend((r"\bottomrule", r"\end{tabular}", ""))
    return "\n".join(lines).encode()


def _plot_setup():
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import numpy as np
    except ImportError as error:
        raise ArtifactError(f"Matplotlib is unavailable: {error}") from error
    matplotlib.rcParams.update({
        "font.family": "DejaVu Sans",
        "font.size": 8,
        "axes.titlesize": 9,
        "axes.labelsize": 8,
        "legend.fontsize": 7,
        "svg.hashsalt": "pr-scaling-formal-v1",
        "pdf.compression": 9,
    })
    return plt, np


def _save(fig, pdf, svg, title):
    metadata = {
        "Title": title,
        "Creator": "generate_pr_scaling_artifacts.py",
        "CreationDate": None,
        "ModDate": None,
    }
    fig.savefig(pdf, format="pdf", metadata=metadata, bbox_inches="tight")
    fig.savefig(
        svg, format="svg", metadata={"Title": title, "Date": None},
        bbox_inches="tight",
    )


def _render_plots(data, root):
    plt, np = _plot_setup()
    colors = {
        "vanilla": "#555555", "amu": "#3569a8",
        "cira": "#d07a21", "m2ndp": "#6f7f35",
    }
    markers = {"amu": "o", "cira": "s", "m2ndp": "^"}
    lookup = {(row.scale, row.system): row for row in data.rows}

    fig, ax = plt.subplots(figsize=(4.4, 3.0))
    for system in ACCELERATORS:
        ax.plot(
            SCALES, [float(lookup[(scale, system)].speedup) for scale in SCALES],
            marker=markers[system], color=colors[system], label=system.upper(),
        )
    ax.axhline(1, color=colors["vanilla"], linewidth=0.8)
    ax.set(xticks=SCALES, xticklabels=[f"g{x}" for x in SCALES],
           xlabel="Graph scale", ylabel="Speedup vs. Vanilla CXL")
    ax.grid(axis="y", color="#dddddd", linewidth=0.6)
    ax.legend(frameon=False, ncol=3)
    _save(fig, root / "pr-scaling-speedup.pdf", root / "pr-scaling-speedup.svg",
          "PR scaling speedup")
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(4.4, 3.0))
    for system in SYSTEMS:
        ax.plot(
            SCALES,
            [float(lookup[(scale, system)].latency_seconds) for scale in SCALES],
            marker="o", color=colors[system], label=system.upper(),
        )
    ax.set_yscale("log")
    ax.set(xticks=SCALES, xticklabels=[f"g{x}" for x in SCALES],
           xlabel="Graph scale", ylabel="End-to-end latency (s)")
    ax.grid(axis="y", color="#dddddd", linewidth=0.6)
    ax.legend(frameon=False, ncol=2)
    _save(fig, root / "pr-scaling-latency.pdf", root / "pr-scaling-latency.svg",
          "PR scaling end-to-end latency")
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(4.8, 3.0))
    x = np.arange(len(SCALES))
    width = 0.24
    for index, system in enumerate(ACCELERATORS):
        ax.bar(
            x + (index - 1) * width,
            [float(lookup[(scale, system)].speedup) for scale in SCALES],
            width, color=colors[system], label=system.upper(),
        )
    ax.axhline(1, color=colors["vanilla"], linewidth=0.8)
    ax.set(xticks=x, xticklabels=[f"g{s}" for s in SCALES],
           xlabel="Graph scale", ylabel="Speedup vs. Vanilla CXL")
    ax.legend(frameon=False, ncol=3)
    _save(fig, root / "pr-scaling-grouped.pdf", root / "pr-scaling-grouped.svg",
          "PR scaling grouped comparison")
    plt.close(fig)

    matrix = np.array([
        [float(lookup[(scale, system)].speedup) for scale in SCALES]
        for system in SYSTEMS
    ])
    fig, ax = plt.subplots(figsize=(4.8, 2.7))
    image = ax.imshow(matrix, cmap="YlGnBu", aspect="auto")
    ax.set_xticks(range(len(SCALES)), [f"g{s}" for s in SCALES])
    ax.set_yticks(range(len(SYSTEMS)), [s.upper() for s in SYSTEMS])
    for row in range(len(SYSTEMS)):
        for column in range(len(SCALES)):
            ax.text(column, row, f"{matrix[row, column]:.2f}x",
                    ha="center", va="center", fontsize=7)
    fig.colorbar(image, ax=ax, label="Speedup vs. Vanilla CXL")
    _save(fig, root / "pr-scaling-heatmap.pdf", root / "pr-scaling-heatmap.svg",
          "PR scaling speedup heatmap")
    plt.close(fig)


def _write(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    _fsync_directory(path.parent)


def _fsync_directory(path):
    descriptor = os.open(
        Path(path), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _relatives():
    return (
        "pr-scaling-raw.json", "pr-scaling-raw.csv",
        "pr-scaling-evidence.json", "pr-scaling-table.tex",
        "fig/pr-scaling-speedup.pdf", "fig/pr-scaling-speedup.svg",
        "fig/pr-scaling-latency.pdf", "fig/pr-scaling-latency.svg",
        "fig/pr-scaling-grouped.pdf", "fig/pr-scaling-grouped.svg",
        "fig/pr-scaling-heatmap.pdf", "fig/pr-scaling-heatmap.svg",
    )


def publish(data, output_root, *, fail_after_promotions=None):
    if not isinstance(data, ScalingData) or len(data.rows) != 16:
        raise ArtifactError("publication data is incomplete")
    output_root = Path(output_root).resolve()
    output_root.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".pr-scaling-stage-", dir=output_root.parent))
    backup = Path(tempfile.mkdtemp(prefix=".pr-scaling-backup-", dir=output_root.parent))
    promoted = []
    backed_up = []
    try:
        _write(stage / "pr-scaling-raw.json", _raw_json_bytes(data))
        _write(stage / "pr-scaling-raw.csv", _csv_bytes(data))
        _write(stage / "pr-scaling-table.tex", _table_bytes(data))
        (stage / "fig").mkdir()
        _render_plots(data, stage / "fig")
        hashes = {
            relative: _sha256_file(stage / relative)
            for relative in _relatives()
            if relative != "pr-scaling-evidence.json"
        }
        evidence = {
            "schema": 1, "status": "pass", "row_count": 16,
            "source": data.source_record, "artifacts": hashes,
        }
        _write(
            stage / "pr-scaling-evidence.json",
            (json.dumps(evidence, sort_keys=True, separators=(",", ":")) + "\n").encode(),
        )
        output_root.mkdir(parents=True, exist_ok=True)
        for relative in _relatives():
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
            if (
                fail_after_promotions is not None
                and len(promoted) == fail_after_promotions
            ):
                raise ArtifactError("injected promotion failure")
        return {
            relative: {
                "path": str(output_root / relative),
                "sha256": _sha256_file(output_root / relative),
            }
            for relative in _relatives()
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
        if isinstance(error, ArtifactError):
            raise
        raise ArtifactError(f"publication failed: {error}") from error
    finally:
        shutil.rmtree(stage, ignore_errors=True)
        shutil.rmtree(backup, ignore_errors=True)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scaling", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv=None):
    options = parse_args(argv)
    try:
        publish(load_data(options.scaling), options.output_root)
    except ArtifactError as error:
        print(f"PR_SCALING_PUBLICATION_FAILED error={error}", file=sys.stderr)
        return 1
    print(f"PR_SCALING_PUBLICATION_PASS root={options.output_root.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
