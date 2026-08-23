#!/usr/bin/env python3
# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

"""Publish validated PR offload raw data, paper tables, and vector figures."""

import argparse
import csv
import datetime
import hashlib
import json
import os
import shutil
import tempfile
from decimal import Decimal
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
matplotlib.rcParams.update({
    "font.family": "DejaVu Sans",
    "font.size": 8,
    "axes.titlesize": 9,
    "axes.labelsize": 8,
    "legend.fontsize": 7,
    "svg.hashsalt": "pr-offload-formal-v1",
})
import matplotlib.pyplot as plt  # noqa: E402

try:
    from scripts import pr_offload_contract as contract
except ImportError:
    import pr_offload_contract as contract


EXPECTED_OUTPUTS = {
    "pr-offload-raw.json", "pr-offload-raw.csv",
    "pr-offload-evidence.json", "pr-offload-table.tex",
    "fig/pr-offload-speedup.pdf", "fig/pr-offload-speedup.svg",
    "fig/pr-offload-latency.pdf", "fig/pr-offload-latency.svg",
    "fig/cira-policy-scaling.pdf", "fig/cira-policy-scaling.svg",
    "fig/cira-phase-breakdown.pdf", "fig/cira-phase-breakdown.svg",
    "fig/cira-mechanism-breakdown.pdf",
    "fig/cira-mechanism-breakdown.svg",
}
MECHANISM_FIELDS = (
    "csr_reads", "rank_reads", "fp_compute", "queue_stall",
    "coherence", "writeback",
)
COLORS = {
    "amu": "#3978B5",
    "cira-few-shot": "#D9902F",
    "m2ndp": "#7A8E3A",
    "cira-static": "#3978B5",
    "cira-pgo": "#D9902F",
    "cira-A": "#7A8E3A",
    "cira-B": "#C95F75",
    "cira-C": "#7A6699",
}
HATCHES = ("", "//", "xx", "..", "\\", "++")


class PublishError(RuntimeError):
    """Completion evidence is unsafe to publish."""


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {key: _safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_safe(item) for item in value]
    return value


def _atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as stream:
        json.dump(_safe(value), stream, indent=2, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _load_complete(path):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PublishError(f"invalid complete evidence: {error}") from error
    if value.get("status") != "passed":
        raise PublishError("complete evidence status is not passed")
    try:
        validated = contract.validate_complete(value)
    except contract.OffloadError as error:
        raise PublishError(str(error)) from error
    stored_gate = value.get("performance_gate")
    expected_gate = _safe(validated["performance_gate"])
    if stored_gate != expected_gate:
        raise PublishError("stored performance gate differs from recomputation")
    oracle = value.get("oracle")
    if not isinstance(oracle, dict) or set(oracle) != {
        f"g{scale}" for scale in contract.SCALES
    }:
        raise PublishError("Oracle evidence is incomplete")
    for scale in contract.SCALES:
        ablations = {
            row["system"][-1]: row
            for row in validated["ablations"]
            if row["scale"] == scale and row["system"] in {
                "cira-A", "cira-B", "cira-C"
            }
        }
        few_shot = next(
            row for row in validated["primary"]
            if row["scale"] == scale and row["system"] == "cira-few-shot"
        )
        oracle_ticks = min(row["sim_ticks"] for row in ablations.values())
        regret = Decimal(few_shot["sim_ticks"]) / Decimal(oracle_ticks) - 1
        if oracle[f"g{scale}"] != {
            "oracle_ticks": oracle_ticks, "regret": str(regret)
        }:
            raise PublishError(f"g{scale} Oracle evidence differs")
    for row in validated["primary"] + validated["ablations"]:
        if row["system"].startswith("cira"):
            mechanism = row.get("mechanism")
            if not isinstance(mechanism, dict) or set(mechanism) != set(
                MECHANISM_FIELDS
            ):
                raise PublishError("CIRA mechanism evidence is incomplete")
            if any(
                isinstance(value, bool) or not isinstance(value, int)
                or value < 0 for value in mechanism.values()
            ):
                raise PublishError("CIRA mechanism evidence is invalid")
    validated["oracle"] = oracle
    return validated


def _rows(data):
    vanilla = {
        row["scale"]: row for row in data["primary"]
        if row["system"] == "vanilla"
    }
    rows = []
    for category, source in (
        ("primary", data["primary"]), ("ablation", data["ablations"])
    ):
        for row in source:
            speedup = vanilla[row["scale"]]["seconds"] / row["seconds"]
            rows.append({
                "category": category,
                "scale": row["scale"],
                "system": row["system"],
                "seconds": str(row["seconds"]),
                "milliseconds": str(row["seconds"] * Decimal(1000)),
                "speedup": str(speedup),
                "raw_sha256": row["raw_sha256"],
                "native_count": str(
                    row.get("sim_ticks", row.get("ndpsim_cycles"))
                ),
                "phase_total_ns": row.get("phase_total_ns", ""),
                **{
                    f"phase_{name}": row.get("phases", {}).get(name, "")
                    for name in contract.CIRA_PHASES
                },
                **{
                    f"mechanism_{name}": row.get("mechanism", {}).get(name, "")
                    for name in MECHANISM_FIELDS
                },
            })
    return rows


def _save_figure(figure, base):
    base.parent.mkdir(parents=True, exist_ok=True)
    fixed = datetime.datetime(2026, 8, 22, tzinfo=datetime.timezone.utc)
    figure.savefig(
        base.with_suffix(".pdf"), bbox_inches="tight",
        metadata={"Creator": "gem5-CXL PR offload publisher",
                  "CreationDate": fixed, "ModDate": fixed},
    )
    figure.savefig(
        base.with_suffix(".svg"), bbox_inches="tight",
        metadata={"Creator": "gem5-CXL PR offload publisher",
                  "Date": "2026-08-22"},
    )
    plt.close(figure)


def phase_milliseconds(nanoseconds):
    return float(nanoseconds) / 1e6


def _grouped_bar(rows, systems, value, title, ylabel, output):
    figure, axis = plt.subplots(figsize=(5.4, 2.7))
    width = 0.22
    centers = list(range(len(contract.SCALES)))
    for index, system in enumerate(systems):
        values = [
            float(next(row[value] for row in rows
                       if row["scale"] == scale and row["system"] == system))
            for scale in contract.SCALES
        ]
        offsets = [center + (index - (len(systems) - 1) / 2) * width
                   for center in centers]
        bars = axis.bar(
            offsets, values, width, label=system.replace("cira-", "CIRA ").upper()
            if system == "amu" else system.replace("cira-", "CIRA "),
            color=COLORS[system], edgecolor="#30343B", linewidth=0.6,
            hatch=HATCHES[index],
        )
        axis.bar_label(bars, fmt="%.2f", padding=2, fontsize=6)
    axis.set_xticks(centers, [f"g{scale}" for scale in contract.SCALES])
    axis.set_ylabel(ylabel)
    axis.set_title(title, loc="left")
    axis.grid(axis="y", color="#D8DCE2", linewidth=0.5)
    axis.set_axisbelow(True)
    maximum = max(
        float(row[value]) for row in rows if row["system"] in systems
    )
    axis.set_ylim(0, maximum * 1.22)
    axis.legend(
        frameon=False, ncol=min(3, len(systems)), loc="upper center",
        bbox_to_anchor=(0.5, 1.16),
    )
    _save_figure(figure, output)


def _phase_figure(data, output):
    source = data["primary"] + data["ablations"]
    policy_order = (
        "cira-static", "cira-pgo", "cira-few-shot",
        "cira-A", "cira-B", "cira-C",
    )
    rows = [
        next(row for row in source
             if row["scale"] == scale and row["system"] == system)
        for scale in contract.SCALES for system in policy_order
    ]
    short = {
        "cira-static": "Static", "cira-pgo": "PGO",
        "cira-few-shot": "Few-shot", "cira-A": "A",
        "cira-B": "B", "cira-C": "C",
    }
    figure, axis = plt.subplots(figsize=(8.2, 3.2))
    labels = [f"g{row['scale']}\n{short[row['system']]}" for row in rows]
    bottoms = [0.0] * len(rows)
    phase_colors = ("#3978B5", "#D9902F", "#7A8E3A", "#C95F75", "#7A6699", "#9AA1AA")
    for index, name in enumerate(contract.CIRA_PHASES):
        values = [phase_milliseconds(row["phases"][name]) for row in rows]
        axis.bar(range(len(rows)), values, bottom=bottoms, label=name,
                 color=phase_colors[index], edgecolor="#30343B", linewidth=0.35,
                 hatch=HATCHES[index])
        bottoms = [left + value for left, value in zip(bottoms, values)]
    axis.set_xticks(range(len(rows)), labels, rotation=45, ha="right")
    axis.set_ylabel("E2E latency (ms)")
    axis.set_title("CIRA additive phase breakdown", loc="left")
    axis.set_ylim(0, max(bottoms) * 1.15)
    axis.legend(frameon=False, ncol=6, loc="upper center",
                bbox_to_anchor=(0.5, 1.17))
    axis.grid(axis="y", color="#D8DCE2", linewidth=0.5)
    axis.set_axisbelow(True)
    _save_figure(figure, output)


def _mechanism_figure(data, output):
    source = data["primary"] + data["ablations"]
    policy_order = (
        "cira-static", "cira-pgo", "cira-few-shot",
        "cira-A", "cira-B", "cira-C",
    )
    rows = [
        next(row for row in source
             if row["scale"] == scale and row["system"] == system)
        for scale in contract.SCALES for system in policy_order
    ]
    maxima = {
        name: max(row["mechanism"][name] for row in rows) or 1
        for name in MECHANISM_FIELDS
    }
    matrix = [
        [row["mechanism"][name] / maxima[name] for name in MECHANISM_FIELDS]
        for row in rows
    ]
    figure, axis = plt.subplots(figsize=(5.9, 4.2))
    image = axis.imshow(matrix, cmap="Blues", vmin=0, vmax=1, aspect="auto")
    axis.set_xticks(range(len(MECHANISM_FIELDS)),
                    [name.replace("_", " ") for name in MECHANISM_FIELDS],
                    rotation=30, ha="right")
    axis.set_yticks(range(len(rows)),
                    [f"g{row['scale']} {row['system'].replace('cira-', '')}"
                     for row in rows])
    axis.set_title("CIRA mechanism counters (normalized, non-additive)", loc="left")
    figure.colorbar(image, ax=axis, label="Normalized to per-counter maximum")
    _save_figure(figure, output)


def _write_table(data, path):
    rows = _rows(data)
    systems = ("vanilla", "amu", "cira-few-shot", "m2ndp")
    lines = [
        r"\begin{tabular}{llrr}",
        r"\toprule",
        r"Scale & System & E2E (ms) & Speedup \\",
        r"\midrule",
    ]
    for scale in contract.SCALES:
        for system in systems:
            row = next(item for item in rows
                       if item["scale"] == scale and item["system"] == system)
            name = system.replace("cira-few-shot", r"CIRA Few-shot")
            name = name.replace("vanilla", "Vanilla").replace("amu", "AMU").replace("m2ndp", r"M$^2$NDP")
            lines.append(
                f"g{scale} & {name} & {Decimal(row['milliseconds']):.3f} & "
                f"{Decimal(row['speedup']):.3f}$\\times$ \\\\"
            )
        if scale != contract.SCALES[-1]:
            lines.append(r"\midrule")
    lines.extend((r"\bottomrule", r"\end{tabular}"))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def render_all(data, staging):
    staging = Path(staging)
    staging.mkdir(parents=True, exist_ok=True)
    rows = _rows(data)
    _atomic_json(staging / "pr-offload-raw.json", {
        "schema": 1, "rows": rows, "oracle": data["oracle"],
        "identity": data["identity"],
    })
    with (staging / "pr-offload-raw.csv").open(
        "w", newline="", encoding="utf-8"
    ) as stream:
        writer = csv.DictWriter(stream, fieldnames=tuple(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    _write_table(data, staging / "pr-offload-table.tex")
    primary_accelerated = [row for row in rows if row["category"] == "primary"]
    _grouped_bar(
        primary_accelerated,
        ("amu", "cira-few-shot", "m2ndp"),
        "speedup", "PR offload speedup", "Speedup vs. matched Vanilla",
        staging / "fig/pr-offload-speedup",
    )
    _grouped_bar(
        primary_accelerated,
        ("amu", "cira-few-shot", "m2ndp"),
        "milliseconds", "PR offload end-to-end latency", "Latency (ms)",
        staging / "fig/pr-offload-latency",
    )
    _grouped_bar(
        rows,
        ("cira-static", "cira-pgo", "cira-few-shot", "cira-A", "cira-B", "cira-C"),
        "speedup", "CIRA policy scaling", "Speedup vs. matched Vanilla",
        staging / "fig/cira-policy-scaling",
    )
    _phase_figure(data, staging / "fig/cira-phase-breakdown")
    _mechanism_figure(data, staging / "fig/cira-mechanism-breakdown")


def _promote_tree(staging, outdir, promote):
    outdir = Path(outdir).resolve()
    backup = staging.parent / "previous"
    had_previous = outdir.exists()
    if had_previous:
        os.replace(outdir, backup)
    try:
        promote(staging, outdir)
    except OSError as error:
        if outdir.exists():
            if outdir.is_dir():
                shutil.rmtree(outdir)
            else:
                outdir.unlink()
        if had_previous:
            os.replace(backup, outdir)
        raise PublishError(f"publication promotion failed: {error}") from error
    if backup.exists():
        shutil.rmtree(backup)


def publish(complete_path, outdir, *, promote=os.replace):
    data = _load_complete(complete_path)
    outdir = Path(outdir).resolve()
    outdir.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=outdir.parent) as temporary:
        staging = Path(temporary) / "staging"
        render_all(data, staging)
        generated = {
            str(path.relative_to(staging)): sha256_file(path)
            for path in sorted(staging.rglob("*")) if path.is_file()
        }
        evidence = {
            "schema": 1,
            "status": "passed",
            "complete": str(Path(complete_path).resolve()),
            "complete_sha256": sha256_file(complete_path),
            "outputs": generated,
        }
        _atomic_json(staging / "pr-offload-evidence.json", evidence)
        actual = {
            str(path.relative_to(staging))
            for path in staging.rglob("*") if path.is_file()
        }
        if actual != EXPECTED_OUTPUTS:
            raise PublishError("rendered output set differs")
        _promote_tree(staging, outdir, promote)
    return outdir


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--complete", type=Path, required=True)
    parser.add_argument("--outdir", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        publish(args.complete, args.outdir)
    except (PublishError, OSError, ValueError, KeyError) as error:
        print(f"error: {error}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
