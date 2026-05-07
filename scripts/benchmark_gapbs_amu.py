#!/usr/bin/env python3
#
# Run GAPBS baseline/AMU comparisons and summarize ROI speedups.

import argparse
import csv
import datetime as dt
import os
import re
import subprocess
import sys
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
DEFAULT_GEM5 = REPO / "build" / "X86" / "gem5.opt"
DEFAULT_BASELINE_CONFIG = (
    REPO / "configs" / "example" / "gem5_library" / "x86-gapbs-benchmarks.py"
)


def split_extra(value):
    if not value:
        return []
    return value.split()


def parse_stats(path):
    stats = {}
    if not path.exists():
        return stats

    for line in path.read_text(errors="replace").splitlines():
        if not line or line.startswith("#") or line.startswith("---"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        try:
            stats[parts[0]] = float(parts[1])
        except ValueError:
            continue
    return stats


def parse_roi_seconds(log_path):
    if not log_path.exists():
        return None
    pattern = re.compile(r"Simulated time in ROI:\s+([0-9.]+)s")
    for line in log_path.read_text(errors="replace").splitlines():
        match = pattern.search(line)
        if match:
            return float(match.group(1))
    return None


def metric_for(run_dir):
    stats = parse_stats(run_dir / "stats.txt")
    if "simTicks" in stats:
        return stats["simTicks"], "simTicks"
    if "simSeconds" in stats:
        return stats["simSeconds"], "simSeconds"

    roi_seconds = parse_roi_seconds(run_dir / "gem5.log")
    if roi_seconds is not None:
        return roi_seconds, "roiSecondsFromLog"

    return None, "missing"


def run_one(args, label, config, benchmark, extra_args):
    run_dir = args.outdir / benchmark / label
    run_dir.mkdir(parents=True, exist_ok=True)
    log_path = run_dir / "gem5.log"

    cmd = [
        str(args.gem5),
        f"--outdir={run_dir}",
        str(config),
        "--benchmark",
        benchmark,
    ] + extra_args

    env = os.environ.copy()
    if args.protoc:
        env["PROTOC"] = str(args.protoc)

    print(" ".join(cmd), flush=True)
    if args.dry_run:
        return {
            "benchmark": benchmark,
            "label": label,
            "status": "dry-run",
            "metric": "",
            "metric_name": "",
            "run_dir": str(run_dir),
        }

    with log_path.open("w") as log:
        proc = subprocess.run(
            cmd,
            cwd=REPO,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            text=True,
        )

    metric, metric_name = metric_for(run_dir)
    return {
        "benchmark": benchmark,
        "label": label,
        "status": "ok" if proc.returncode == 0 else f"exit-{proc.returncode}",
        "metric": metric,
        "metric_name": metric_name,
        "run_dir": str(run_dir),
    }


def write_summary(outdir, rows):
    by_benchmark = {}
    for row in rows:
        by_benchmark.setdefault(row["benchmark"], {})[row["label"]] = row

    summary_path = outdir / "summary.csv"
    with summary_path.open("w", newline="") as fh:
        writer = csv.DictWriter(
            fh,
            fieldnames=[
                "benchmark",
                "baseline_status",
                "amu_status",
                "metric_name",
                "baseline_metric",
                "amu_metric",
                "speedup",
                "baseline_dir",
                "amu_dir",
            ],
        )
        writer.writeheader()
        for benchmark in sorted(by_benchmark):
            baseline = by_benchmark[benchmark].get("baseline", {})
            amu = by_benchmark[benchmark].get("amu", {})
            baseline_metric = baseline.get("metric")
            amu_metric = amu.get("metric")
            speedup = ""
            if baseline_metric and amu_metric:
                speedup = baseline_metric / amu_metric
            writer.writerow(
                {
                    "benchmark": benchmark,
                    "baseline_status": baseline.get("status", "missing"),
                    "amu_status": amu.get("status", "missing"),
                    "metric_name": baseline.get("metric_name")
                    or amu.get("metric_name")
                    or "",
                    "baseline_metric": baseline_metric or "",
                    "amu_metric": amu_metric or "",
                    "speedup": speedup,
                    "baseline_dir": baseline.get("run_dir", ""),
                    "amu_dir": amu.get("run_dir", ""),
                }
            )
    return summary_path


def main():
    parser = argparse.ArgumentParser(
        description="Run GAPBS baseline and AMU gem5 comparisons."
    )
    parser.add_argument("--gem5", type=Path, default=DEFAULT_GEM5)
    parser.add_argument(
        "--baseline-config", type=Path, default=DEFAULT_BASELINE_CONFIG
    )
    parser.add_argument("--amu-config", type=Path, required=True)
    parser.add_argument(
        "--benchmark",
        action="append",
        required=True,
        help="GAPBS workload resource id, e.g. gapbs-bfs-test.",
    )
    parser.add_argument(
        "--baseline-extra",
        default="",
        help="Extra arguments appended to the baseline config command.",
    )
    parser.add_argument(
        "--amu-extra",
        default="",
        help="Extra arguments appended to the AMU config command.",
    )
    parser.add_argument(
        "--outdir",
        type=Path,
        default=REPO
        / "m5out"
        / "gapbs_amu"
        / dt.datetime.now().strftime("%Y%m%d-%H%M%S"),
    )
    parser.add_argument(
        "--protoc",
        type=Path,
        default=Path("/usr/bin/protoc"),
        help="Matching protoc to expose to gem5's SCons/gem5 subprocesses.",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--allow-same-config",
        action="store_true",
        help="Allow baseline and AMU configs to be identical for smoke tests.",
    )
    args = parser.parse_args()

    if args.baseline_config.resolve() == args.amu_config.resolve():
        if not args.allow_same_config:
            sys.exit(
                "Refusing to compare identical configs. Pass a timing-accurate "
                "AMU config via --amu-config, or use --allow-same-config only "
                "for a smoke test."
            )

    if not args.gem5.exists() and not args.dry_run:
        sys.exit(f"gem5 binary not found: {args.gem5}")
    if not args.baseline_config.exists():
        sys.exit(f"baseline config not found: {args.baseline_config}")
    if not args.amu_config.exists():
        sys.exit(f"AMU config not found: {args.amu_config}")

    args.outdir.mkdir(parents=True, exist_ok=True)
    rows = []
    for benchmark in args.benchmark:
        rows.append(
            run_one(
                args,
                "baseline",
                args.baseline_config,
                benchmark,
                split_extra(args.baseline_extra),
            )
        )
        rows.append(
            run_one(
                args,
                "amu",
                args.amu_config,
                benchmark,
                split_extra(args.amu_extra),
            )
        )

    summary_path = write_summary(args.outdir, rows)
    print(f"Wrote {summary_path}")


if __name__ == "__main__":
    main()
