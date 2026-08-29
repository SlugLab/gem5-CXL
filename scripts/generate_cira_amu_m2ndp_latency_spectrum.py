#!/usr/bin/env python3
# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

"""Publish the validated six-workload CXL latency spectrum."""

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

try:
    from scripts import cross_system_contract as contract
    from scripts import cxl_latency_spectrum as latency
    from scripts import run_cira_amu_m2ndp_breadth as breadth
    from scripts import run_cira_amu_m2ndp_latency_spectrum as spectrum
except ImportError:
    import cross_system_contract as contract
    import cxl_latency_spectrum as latency
    import run_cira_amu_m2ndp_breadth as breadth
    import run_cira_amu_m2ndp_latency_spectrum as spectrum


SYSTEMS = breadth.TIMING_SYSTEMS
ACCELERATORS = SYSTEMS[1:]
WORKLOADS = breadth.WORKLOADS
WORKLOAD_LABELS = {
    "pr_spmv": "PageRank g20",
    "mcf": "MCF",
    "amg_gather": "AMG Gather",
    "lulesh_scatter": "LULESH Scatter",
    "npb_cg": "NPB CG",
    "npb_mg": "NPB MG",
}
LATENCY_LABELS = {
    "200ns": "200 ns", "500ns": "500 ns", "1us": "1 µs", "2us": "2 µs",
}
SYSTEM_LABELS = {
    "vanilla": "Vanilla", "amu": "AMU", "cira": "CIRA", "m2ndp": "M²NDP",
}
COLORS = {
    "vanilla": "#4d4d4d",
    "amu": "#3569a8",
    "cira": "#d19a2b",
    "m2ndp": "#d06b26",
}
STYLES = {
    "vanilla": ("-", "D"),
    "amu": ("-", "o"),
    "cira": ("--", "s"),
    "m2ndp": (":", "^"),
}
HATCHES = {"amu": "", "cira": "//", "m2ndp": "xx"}


class PublicationError(RuntimeError):
    """Formal evidence or atomic publication violates the figure contract."""


@dataclasses.dataclass(frozen=True)
class SpectrumRow:
    latency: str
    latency_ticks: int
    workload: str
    system: str
    seconds: Decimal
    speedup: Decimal
    ci_low: Decimal | None
    ci_high: Decimal | None
    evidence_type: str
    evidence_sha256: str
    output_sha256: str
    window_count: int
    workers: int = 4
    all_memory_cxl: bool = True
    verification: str = "bit-exact"


@dataclasses.dataclass(frozen=True)
class SpectrumData:
    rows: tuple[SpectrumRow, ...]
    geometric_means: dict
    input_records: dict


def _sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _load_json(path, label):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PublicationError(f"invalid {label}: {error}") from error
    if not isinstance(value, dict):
        raise PublicationError(f"{label} must be a JSON object")
    return value


def _decimal(value, label, *, allow_zero=False):
    if isinstance(value, (bool, float)):
        raise PublicationError(f"{label} must be an exact decimal")
    try:
        result = Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as error:
        raise PublicationError(f"{label} is not an exact decimal") from error
    if not result.is_finite() or result < 0 or (not allow_zero and result == 0):
        raise PublicationError(f"{label} must be finite and positive")
    return result


def _close(left, right):
    return abs(left - right) <= max(abs(right) * Decimal("1e-12"), Decimal("1e-18"))


def _functional_output_sha256(workload_row, system):
    key = "m2ndp-funcsim" if system == "m2ndp" else system
    try:
        outputs = workload_row["functional"][key]["outputs"]
    except (KeyError, TypeError) as error:
        raise PublicationError(f"{system} functional outputs are missing") from error
    if (
        not isinstance(outputs, dict)
        or not outputs
        or any(
            not isinstance(value, str) or len(value) != 64
            for value in outputs.values()
        )
    ):
        raise PublicationError(f"{system} functional output hashes are invalid")
    return hashlib.sha256(contract.canonical_json(outputs)).hexdigest()


def _validate_aggregate(value, complete_path):
    if (
        value.get("schema") != 1
        or value.get("status") != "complete"
        or value.get("coordinate_count") != len(spectrum.coordinates())
        or set(value.get("latencies", {})) != set(latency.LABELS)
    ):
        raise PublicationError("aggregate latency matrix is not complete")
    try:
        shared = spectrum.validate_shared(value.get("shared"))
        qualification = spectrum._validate_qualification_record(
            value.get("qualification"), shared["calibration"]["sha256"]
        )
        stored_identity = contract.ExperimentIdentity(**value["identity"])
        expected_identity = spectrum._aggregate_identity(shared, qualification)
    except (
        KeyError, TypeError, contract.ContractError, spectrum.SpectrumError
    ) as error:
        raise PublicationError(f"aggregate identity differs: {error}") from error
    if (
        stored_identity != expected_identity
        or value.get("identity_sha256") != expected_identity.digest()
    ):
        raise PublicationError("aggregate identity differs")
    return shared, qualification, {
        "aggregate": {
            "path": str(Path(complete_path).resolve()),
            "sha256": _sha256_file(complete_path),
        },
        "qualification": qualification,
        **shared,
    }


def _child_rows(aggregate, shared, label):
    row = aggregate["latencies"][label]
    try:
        validated = spectrum.validate_child(
            row.get("child_root"), label, shared,
            expected_identity_sha256=row.get("identity_sha256"),
        )
    except spectrum.SpectrumError as error:
        raise PublicationError(str(error)) from error
    if row.get("complete") != validated["complete"]:
        raise PublicationError("child complete manifest hash differs")
    child = _load_json(validated["complete"]["path"], f"{label} child complete")
    if (
        child.get("status") != "complete"
        or child.get("cxl_link_delay") != label
        or child.get("cxl_link_delay_ticks") != latency.ticks(label)
        or tuple(child.get("workload_order", ())) != WORKLOADS
        or set(child.get("workloads", {})) != set(WORKLOADS)
        or set(child.get("results", {})) != set(WORKLOADS)
    ):
        raise PublicationError(f"{label} child workload matrix differs")
    rows = []
    for workload in WORKLOADS:
        workload_row = child["workloads"][workload]
        if not breadth.functional_complete(workload_row.get("functional")):
            raise PublicationError(f"{label}:{workload} functional bit-exact gate failed")
        result = child["results"][workload]
        if (
            result.get("status") != "complete"
            or result.get("publishable") is not True
            or result.get("level") not in breadth.LEVELS
            or set(result.get("absolute_seconds", {})) != set(SYSTEMS)
            or set(result.get("systems", {})) != set(ACCELERATORS)
        ):
            raise PublicationError(f"{label}:{workload} timing result is not publishable")
        absolute = {
            system: _decimal(
                result["absolute_seconds"][system],
                f"{label}:{workload}:{system} absolute latency",
            )
            for system in SYSTEMS
        }
        baseline = absolute["vanilla"]
        for system in SYSTEMS:
            computed = baseline / absolute[system]
            low = None
            high = None
            if system == "vanilla":
                speedup = Decimal(1)
            else:
                observed = result["systems"][system]
                if observed.get("publishable") is not True:
                    raise PublicationError(
                        f"{label}:{workload}:{system} confidence interval is inconclusive"
                    )
                speedup = _decimal(
                    observed.get("speedup"),
                    f"{label}:{workload}:{system} stored speedup",
                )
                low = _decimal(observed.get("ci_low"), "CI low")
                high = _decimal(observed.get("ci_high"), "CI high")
                relative = _decimal(
                    observed.get("relative_half_width"),
                    "relative half width", allow_zero=True,
                )
                if not _close(speedup, computed):
                    raise PublicationError(
                        f"{label}:{workload}:{system} stored speedup differs"
                    )
                if relative > Decimal("0.05") or not low <= speedup <= high:
                    raise PublicationError(
                        f"{label}:{workload}:{system} confidence interval differs"
                    )
            if system == "vanilla" and not _close(speedup, computed):
                raise PublicationError(
                    f"{label}:{workload}:{system} stored speedup differs"
                )
            rows.append(SpectrumRow(
                latency=label,
                latency_ticks=latency.ticks(label),
                workload=workload,
                system=system,
                seconds=absolute[system],
                speedup=computed,
                ci_low=low,
                ci_high=high,
                evidence_type="paired-stratified",
                evidence_sha256=validated["complete"]["sha256"],
                output_sha256=_functional_output_sha256(workload_row, system),
                window_count=result["level"],
            ))
    return rows, child.get("g20_graph_sha256")


def _geometric_means(rows):
    means = {}
    for label in latency.LABELS:
        means[label] = {}
        for system in ACCELERATORS:
            values = [
                row.speedup for row in rows
                if row.latency == label and row.system == system
            ]
            if len(values) != len(WORKLOADS):
                raise PublicationError("geometric-mean input matrix differs")
            means[label][system] = str(
                (sum((value.ln() for value in values), Decimal(0))
                 / Decimal(len(values))).exp()
            )
    return means


def load_complete(path):
    path = Path(path).resolve()
    aggregate = _load_json(path, "aggregate complete manifest")
    shared, _, input_records = _validate_aggregate(aggregate, path)
    rows = []
    graph_hash = None
    for label in latency.LABELS:
        child_rows, selected_graph_hash = _child_rows(aggregate, shared, label)
        if graph_hash is None:
            graph_hash = selected_graph_hash
        elif selected_graph_hash != graph_hash:
            raise PublicationError("child g20 graph identity differs")
        rows.extend(child_rows)
    if len(rows) != 96 or {
        (row.latency, row.workload, row.system) for row in rows
    } != set(spectrum.coordinates()):
        raise PublicationError("published coordinate matrix differs")
    return SpectrumData(
        rows=tuple(rows),
        geometric_means=_geometric_means(rows),
        input_records=input_records,
    )


def _csv_bytes(data):
    stream = io.StringIO(newline="")
    fields = tuple(field.name for field in dataclasses.fields(SpectrumRow))
    writer = csv.DictWriter(stream, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    for row in data.rows:
        value = dataclasses.asdict(row)
        for name in ("seconds", "speedup", "ci_low", "ci_high"):
            value[name] = "" if value[name] is None else str(value[name])
        writer.writerow(value)
    return stream.getvalue().encode("utf-8")


def _json_bytes(data):
    rows = []
    for row in data.rows:
        value = dataclasses.asdict(row)
        for name in ("seconds", "speedup", "ci_low", "ci_high"):
            value[name] = None if value[name] is None else str(value[name])
        rows.append(value)
    value = {
        "schema": 1,
        "status": "complete",
        "row_count": len(rows),
        "latencies": list(latency.LABELS),
        "workloads": list(WORKLOADS),
        "systems": list(SYSTEMS),
        "geometric_means": data.geometric_means,
        "inputs": data.input_records,
        "rows": rows,
    }
    return contract.canonical_json(value) + b"\n"


def _fmt(value, digits=3):
    return f"{value:.{digits}f}"


def _table_bytes(data):
    lines = [
        r"\begin{tabular}{llrrrr}",
        r"\toprule",
        r"Workload & System & E2E at 1 us (ms) & Speedup & 95\% CI & Geo. mean \\",
        r"\midrule",
    ]
    for workload in WORKLOADS:
        for system in ACCELERATORS:
            row = next(
                item for item in data.rows
                if item.latency == "1us" and item.workload == workload
                and item.system == system
            )
            interval = f"[{_fmt(row.ci_low)}, {_fmt(row.ci_high)}]"
            geomean = Decimal(data.geometric_means["1us"][system])
            lines.append(
                f"{WORKLOAD_LABELS[workload]} & {SYSTEM_LABELS[system]} & "
                f"{_fmt(row.seconds * Decimal(1000), 6)} & "
                f"{_fmt(row.speedup)} & {interval} & {_fmt(geomean)} \\\\"
            )
    lines.extend((r"\bottomrule", r"\end{tabular}", ""))
    return "\n".join(lines).encode("utf-8")


def _matplotlib():
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import numpy as np
    except ImportError as error:
        raise PublicationError(f"Matplotlib is unavailable: {error}") from error
    return matplotlib, plt, np


def _rows(data, workload, system):
    selected = [
        row for row in data.rows
        if row.workload == workload and row.system == system
    ]
    selected.sort(key=lambda row: latency.LABELS.index(row.latency))
    return selected


def _global_speedup_limits(selected):
    if not selected:
        raise PublicationError("speedup-axis evidence is empty")
    lower = [
        float(row.ci_low if row.ci_low is not None else row.speedup)
        for row in selected
    ]
    upper = [
        float(row.ci_high if row.ci_high is not None else row.speedup)
        for row in selected
    ]
    minimum = min(lower + [1.0])
    maximum = max(upper + [1.0])
    return max(0.0, minimum * 0.9), maximum * 1.1


def _speedup_axis(ax, selected, *, limits=None):
    bottom, top = limits or _global_speedup_limits(selected)
    ax.set_ylim(bottom=bottom, top=top)
    ax.axhline(1.0, color="#333333", linewidth=0.8, zorder=0)
    ax.grid(axis="y", color="#dddddd", linewidth=0.6)
    ax.spines[["top", "right"]].set_visible(False)


def _bar_speedup_axis(ax, selected):
    _, top = _global_speedup_limits(selected)
    ax.set_ylim(bottom=0.0, top=top)
    ax.axhline(1.0, color="#333333", linewidth=0.8, zorder=0)
    ax.grid(axis="y", color="#dddddd", linewidth=0.6)
    ax.spines[["top", "right"]].set_visible(False)


def _save_formats(figure, output_stem, title):
    output_stem.parent.mkdir(parents=True, exist_ok=True)
    metadata = {
        "Title": title,
        "Creator": "generate_cira_amu_m2ndp_latency_spectrum.py",
        "CreationDate": None,
        "ModDate": None,
    }
    figure.savefig(
        output_stem.with_suffix(".pdf"), format="pdf", metadata=metadata,
        bbox_inches="tight",
    )
    figure.savefig(
        output_stem.with_suffix(".svg"), format="svg",
        metadata={"Title": title, "Date": None}, bbox_inches="tight",
    )
    figure.savefig(
        output_stem.with_suffix(".png"), format="png", dpi=300,
        metadata={"Software": metadata["Creator"]}, bbox_inches="tight",
    )


def _plot_speedup(ax, rows, system, *, label=True):
    line, marker = STYLES[system]
    x = range(len(latency.LABELS))
    values = [float(row.speedup) for row in rows]
    low = [float(row.speedup - row.ci_low) for row in rows]
    high = [float(row.ci_high - row.speedup) for row in rows]
    ax.errorbar(
        x, values, yerr=[low, high], color=COLORS[system],
        linestyle=line, marker=marker, markerfacecolor="white",
        linewidth=1.4, markersize=4.2, capsize=2,
        label=SYSTEM_LABELS[system] if label else None,
    )


def _render_composite(data, output_stem):
    matplotlib, plt, _ = _matplotlib()
    with matplotlib.rc_context({
        "font.family": "DejaVu Sans", "font.size": 7.2,
        "axes.titlesize": 8.2, "axes.labelsize": 7.5,
        "legend.fontsize": 7, "svg.hashsalt": "cxl-spectrum-v1",
        "pdf.compression": 9,
    }):
        all_selected = [
            row for row in data.rows if row.system in ACCELERATORS
        ]
        shared_limits = _global_speedup_limits(all_selected)
        fig, axes = plt.subplots(
            2, 3, figsize=(7.1, 4.7), sharex=True, sharey=True
        )
        for ax, workload in zip(axes.flat, WORKLOADS):
            selected = []
            for system in ACCELERATORS:
                rows = _rows(data, workload, system)
                selected.extend(rows)
                _plot_speedup(ax, rows, system)
            _speedup_axis(ax, selected, limits=shared_limits)
            ax.set_title(WORKLOAD_LABELS[workload])
            ax.set_xticks(range(4), [LATENCY_LABELS[item] for item in latency.LABELS])
        for ax in axes[:, 0]:
            ax.set_ylabel("Speedup vs. Vanilla")
        for ax in axes[1, :]:
            ax.set_xlabel("Modeled CXL link latency")
        axes[0, 0].legend(frameon=False, ncol=3, loc="upper left")
        fig.suptitle("AMU, CIRA, and M²NDP latency sensitivity", fontsize=10)
        fig.subplots_adjust(left=0.08, right=0.99, bottom=0.12, top=0.90,
                            hspace=0.32, wspace=0.28)
        _save_formats(fig, output_stem, "Six-workload CXL latency spectrum")
        plt.close(fig)


def _render_one_us(data, output_stem):
    matplotlib, plt, np = _matplotlib()
    with matplotlib.rc_context({
        "font.family": "DejaVu Sans", "font.size": 7.5,
        "axes.titlesize": 9, "axes.labelsize": 8,
        "legend.fontsize": 7, "svg.hashsalt": "cxl-spectrum-v1",
        "pdf.compression": 9,
    }):
        fig, ax = plt.subplots(figsize=(7.1, 3.1))
        x = np.arange(len(WORKLOADS))
        width = 0.24
        selected = []
        for offset, system in enumerate(ACCELERATORS):
            rows = [
                row for workload in WORKLOADS
                for row in data.rows
                if row.latency == "1us" and row.workload == workload
                and row.system == system
            ]
            selected.extend(rows)
            values = [float(row.speedup) for row in rows]
            low = [float(row.speedup - row.ci_low) for row in rows]
            high = [float(row.ci_high - row.speedup) for row in rows]
            ax.bar(
                x + (offset - 1) * width, values, width,
                color=COLORS[system], edgecolor="#333333", linewidth=0.5,
                hatch=HATCHES[system], yerr=np.array([low, high]), capsize=2,
                error_kw={"linewidth": 0.7}, label=SYSTEM_LABELS[system],
            )
        _bar_speedup_axis(ax, selected)
        ax.set_xticks(x, [WORKLOAD_LABELS[item] for item in WORKLOADS],
                      rotation=20, ha="right")
        ax.set_ylabel("Speedup vs. Vanilla")
        ax.set_title("Six workloads at 1 us modeled CXL latency (paired 95% CI)")
        ax.legend(frameon=False, ncol=3, loc="upper left")
        fig.subplots_adjust(left=0.08, right=0.99, bottom=0.25, top=0.88)
        _save_formats(fig, output_stem, "Six-workload comparison at 1 us CXL")
        plt.close(fig)


def _render_standalone(data, workload, output_stem):
    matplotlib, plt, _ = _matplotlib()
    with matplotlib.rc_context({
        "font.family": "DejaVu Sans", "font.size": 8,
        "axes.titlesize": 9, "axes.labelsize": 8,
        "legend.fontsize": 7.5, "svg.hashsalt": "cxl-spectrum-v1",
        "pdf.compression": 9,
    }):
        fig, axes = plt.subplots(2, 1, figsize=(5.4, 5.2), sharex=True)
        for system in SYSTEMS:
            rows = _rows(data, workload, system)
            line, marker = STYLES[system]
            axes[0].plot(
                range(4), [float(row.seconds * Decimal(1000)) for row in rows],
                color=COLORS[system], linestyle=line, marker=marker,
                markerfacecolor="white", linewidth=1.4, markersize=4.2,
                label=SYSTEM_LABELS[system],
            )
        axes[0].set_ylim(bottom=0)
        axes[0].set_ylabel("End-to-end latency (ms)")
        axes[0].grid(axis="y", color="#dddddd", linewidth=0.6)
        axes[0].spines[["top", "right"]].set_visible(False)
        axes[0].legend(frameon=False, ncol=4, loc="upper left")
        speedup_rows = []
        for system in ACCELERATORS:
            rows = _rows(data, workload, system)
            speedup_rows.extend(rows)
            _plot_speedup(axes[1], rows, system)
        _speedup_axis(axes[1], speedup_rows)
        axes[1].set_ylabel("Speedup vs. Vanilla")
        axes[1].set_xlabel("Modeled CXL link latency")
        axes[1].set_xticks(range(4), [LATENCY_LABELS[item] for item in latency.LABELS])
        axes[1].legend(frameon=False, ncol=3, loc="upper left")
        fig.suptitle(f"{WORKLOAD_LABELS[workload]} latency spectrum", fontsize=10)
        fig.subplots_adjust(left=0.15, right=0.98, bottom=0.10, top=0.92, hspace=0.18)
        _save_formats(fig, output_stem, f"{WORKLOAD_LABELS[workload]} CXL latency spectrum")
        plt.close(fig)


def _write(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def _artifact_relatives():
    artifacts = [
        "raw/cira-amu-m2ndp-latency-spectrum.csv",
        "raw/cira-amu-m2ndp-latency-spectrum.json",
        "tex/cira-amu-m2ndp-latency-table-data.tex",
    ]
    for stem in (
        "fig/cira-amu-m2ndp-workloads-1us",
        "fig/cira-amu-m2ndp-latency-spectrum",
    ):
        artifacts.extend(f"{stem}.{suffix}" for suffix in ("pdf", "svg", "png"))
    for workload in WORKLOADS:
        artifacts.extend(
            f"fig/standalone/{workload}-latency-spectrum.{suffix}"
            for suffix in ("pdf", "svg", "png")
        )
    return tuple(artifacts)


def _build(stage, data):
    _write(stage / "raw/cira-amu-m2ndp-latency-spectrum.csv", _csv_bytes(data))
    _write(stage / "raw/cira-amu-m2ndp-latency-spectrum.json", _json_bytes(data))
    _write(stage / "tex/cira-amu-m2ndp-latency-table-data.tex", _table_bytes(data))
    _render_one_us(data, stage / "fig/cira-amu-m2ndp-workloads-1us")
    _render_composite(data, stage / "fig/cira-amu-m2ndp-latency-spectrum")
    for workload in WORKLOADS:
        _render_standalone(
            data, workload,
            stage / f"fig/standalone/{workload}-latency-spectrum",
        )
    return {
        relative: {
            "path": relative,
            "sha256": _sha256_file(stage / relative),
        }
        for relative in _artifact_relatives()
    }


def publish(data, output_root):
    if not isinstance(data, SpectrumData) or len(data.rows) != 96:
        raise PublicationError("publication data is not the complete 96-row matrix")
    output_root = Path(output_root).resolve()
    output_root.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".latency-spectrum-stage-", dir=output_root.parent))
    backup_root = Path(tempfile.mkdtemp(
        prefix=".latency-spectrum-backup-", dir=output_root.parent
    ))
    backup = backup_root / "previous"
    installed = False
    saved = False
    try:
        artifacts = _build(stage, data)
        manifest = {
            "schema": 1,
            "status": "complete",
            "row_count": len(data.rows),
            "coordinate_count": len(spectrum.coordinates()),
            "inputs": data.input_records,
            "geometric_means": data.geometric_means,
            "artifacts": artifacts,
        }
        _write(
            stage / "raw/cira-amu-m2ndp-latency-spectrum-manifest.json",
            contract.canonical_json(manifest) + b"\n",
        )
        if output_root.exists():
            os.replace(output_root, backup)
            saved = True
        os.replace(stage, output_root)
        installed = True
        for relative, record in artifacts.items():
            path = output_root / relative
            if _sha256_file(path) != record["sha256"]:
                raise PublicationError(f"published artifact hash differs: {relative}")
        return {
            **manifest,
            "artifacts": {
                relative: {
                    "path": str(output_root / relative),
                    "sha256": record["sha256"],
                }
                for relative, record in artifacts.items()
            },
            "manifest": {
                "path": str(
                    output_root / "raw/cira-amu-m2ndp-latency-spectrum-manifest.json"
                ),
                "sha256": _sha256_file(
                    output_root / "raw/cira-amu-m2ndp-latency-spectrum-manifest.json"
                ),
            },
        }
    except Exception as error:
        if installed and output_root.exists():
            shutil.rmtree(output_root)
        if saved and backup.exists():
            os.replace(backup, output_root)
        if isinstance(error, PublicationError):
            raise
        raise PublicationError(f"publication failed: {error}") from error
    finally:
        if stage.exists():
            shutil.rmtree(stage, ignore_errors=True)
        shutil.rmtree(backup_root, ignore_errors=True)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--complete", type=Path, required=True)
    parser.add_argument("--outdir", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv=None):
    options = parse_args(argv)
    try:
        result = publish(load_complete(options.complete), options.outdir)
    except PublicationError as error:
        print(f"LATENCY_SPECTRUM_PUBLICATION_FAILED error={error}")
        return 1
    print(
        "LATENCY_SPECTRUM_PUBLICATION_PASS "
        f"rows={result['row_count']} manifest={result['manifest']['path']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
