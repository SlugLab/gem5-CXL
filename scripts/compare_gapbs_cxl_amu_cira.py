#!/usr/bin/env python3
#
# Run local GAPBS binaries under the CXL/AMU/CIRA timing config and summarize
# speedups against the CXL-only baseline.

import argparse
import csv
import datetime as dt
import os
import subprocess
import sys
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
DEFAULT_GEM5 = REPO / "build" / "X86" / "gem5.opt"
DEFAULT_CONFIG = (
    REPO / "configs" / "example" / "gem5_library" / "x86-gapbs-amu-se.py"
)

CXL_PACKET_STAT = "board.cache_hierarchy.membus.pktCount::total"
CXL_BYTE_STAT = "board.cache_hierarchy.membus.pktSize::total"
DIAGNOSTIC_STATS = {
    "l1d_demand_misses": (
        "board.cache_hierarchy.l1d-cache-0.demandMisses::total"
    ),
    "l2d_demand_hits": (
        "board.cache_hierarchy.l2-cache-0.demandHits::"
        "processor.cores.core.data"
    ),
    "l2d_demand_misses": (
        "board.cache_hierarchy.l2-cache-0.demandMisses::"
        "processor.cores.core.data"
    ),
    "l2i_demand_hits": (
        "board.cache_hierarchy.l2-cache-0.demandHits::"
        "processor.cores.core.inst"
    ),
    "l2i_demand_misses": (
        "board.cache_hierarchy.l2-cache-0.demandMisses::"
        "processor.cores.core.inst"
    ),
}
CIRA_LATENCY_STATS = {
    "cira_total_latency": "board.cira.totalLatency",
    "cira_avg_latency": "board.cira.avgLatency",
}


class StatsError(RuntimeError):
    pass


def parse_label_path(value):
    if "=" not in value:
        raise argparse.ArgumentTypeError("expected LABEL=PATH")
    label, path = value.split("=", 1)
    if not label:
        raise argparse.ArgumentTypeError("label must not be empty")
    return label, Path(path)


def parse_stats(path):
    stats = {}
    if not path.exists():
        return stats
    in_first_section = False
    for line in path.read_text(errors="replace").splitlines():
        if line.startswith("---------- Begin Simulation Statistics"):
            if in_first_section:
                break
            in_first_section = True
            continue
        if in_first_section and line.startswith(
            "---------- End Simulation Statistics"
        ):
            break
        if not line or line.startswith("#") or line.startswith("---"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        try:
            stats[parts[0]] = float(parts[1])
        except ValueError:
            pass
    return stats


def parse_verification(path):
    if not path.exists():
        return "missing"
    verification = "missing"
    for line in path.read_text(errors="replace").splitlines():
        if "Verification: PASS" in line:
            verification = "pass"
        elif "Verification: FAIL" in line:
            return "fail"
    return verification


def extract_diagnostic_metrics(stats, kind):
    for name in (CXL_PACKET_STAT, CXL_BYTE_STAT):
        if name not in stats:
            raise StatsError(f"missing required ROI statistic: {name}")
    if kind == "cira":
        for name in CIRA_LATENCY_STATS.values():
            if name not in stats:
                raise StatsError(f"missing required ROI statistic: {name}")
    metrics = {
        "cxl_packets": stats[CXL_PACKET_STAT],
        "cxl_bytes": stats[CXL_BYTE_STAT],
    }
    metrics.update(
        {
            field: stats.get(stat_name, 0)
            for field, stat_name in DIAGNOSTIC_STATS.items()
        }
    )
    metrics.update(
        {
            field: stats.get(stat_name, 0)
            for field, stat_name in CIRA_LATENCY_STATS.items()
        }
    )
    return metrics


def add_optional(cmd, name, value):
    if value is not None:
        cmd += [name, str(value)]


def has_env(envs, key):
    return any(env == key or env.startswith(f"{key}=") for env in envs)


def env_flag_enabled(envs, key):
    for env in envs:
        if env == key:
            return True
        prefix = f"{key}="
        if env.startswith(prefix):
            value = env[len(prefix):]
            return value != "" and value != "0"
    return False


def run_one(args, benchmark, label, binary_dir, kind):
    binary = (binary_dir / benchmark).resolve()
    run_dir = args.outdir / benchmark / label
    run_dir.mkdir(parents=True, exist_ok=True)
    log_path = run_dir / "gem5.log"

    if not binary.exists():
        return {
            "benchmark": benchmark,
            "label": label,
            "kind": kind,
            "status": "missing-binary",
            "run_dir": str(run_dir),
        }

    workload_args = f"-g {args.scale} -n {args.iterations}"
    if args.verify:
        workload_args += " -v"
    cmd = [
        str(args.gem5),
        f"--outdir={run_dir}",
        str(args.config),
        "--binary",
        str(binary),
        "--arguments",
        workload_args,
        "--cpu",
        args.cpu,
        "--cores",
        str(args.cores),
        "--cxl-memory",
        "--cxl-link-delay",
        args.cxl_link_delay,
    ]

    if args.disable_hw_prefetchers:
        cmd.append("--disable-hw-prefetchers")
    if args.roi_work_events:
        cmd.append("--roi-work-events")
    if args.verify and args.roi_work_events:
        cmd.append("--continue-after-roi")
    add_optional(cmd, "--l1-mshrs", args.l1_mshrs)
    add_optional(cmd, "--l1-tgts-per-mshr", args.l1_tgts_per_mshr)
    add_optional(cmd, "--l2-mshrs", args.l2_mshrs)
    add_optional(cmd, "--l2-tgts-per-mshr", args.l2_tgts_per_mshr)
    for env in args.env:
        cmd += ["--env", env]

    if kind == "baseline":
        cmd.append("--no-asmc")
    elif kind == "amu":
        cmd += [
            "--asmc-spm-size",
            args.asmc_spm_size,
            "--asmc-granularity",
            str(args.asmc_granularity),
            "--asmc-max-outstanding",
            str(args.asmc_max_outstanding),
            "--asmc-max-send-queue",
            str(args.asmc_max_send_queue),
            "--asmc-issue-latency",
            args.asmc_issue_latency,
            "--asmc-completion-latency",
            args.asmc_completion_latency,
            "--asmc-latency",
            args.asmc_latency,
        ]
    elif kind == "cira":
        cmd += [
            "--no-asmc",
            "--cira",
            "--cira-to-l2",
            "--cira-max-outstanding",
            str(args.cira_max_outstanding),
            "--cira-max-send-queue",
            str(args.cira_max_send_queue),
            "--cira-issue-latency",
            args.cira_issue_latency,
            "--cira-completion-latency",
            args.cira_completion_latency,
        ]
        if (
            not has_env(args.env, "CIRA_GEM5_M5OPS")
            and not has_env(args.env, "CIRA_GAPBS_GEM5_M5OPS")
        ):
            cmd += ["--env", "CIRA_GEM5_M5OPS=1"]
    else:
        raise ValueError(kind)

    print(" ".join(cmd), flush=True)
    if args.dry_run:
        return {
            "benchmark": benchmark,
            "label": label,
            "kind": kind,
            "status": "dry-run",
            "run_dir": str(run_dir),
        }

    env = os.environ.copy()
    with log_path.open("w") as log:
        proc = subprocess.run(
            cmd,
            cwd=REPO,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=args.timeout,
        )

    stats = parse_stats(run_dir / "stats.txt")
    verification = parse_verification(log_path)
    status = "ok" if proc.returncode == 0 else f"exit-{proc.returncode}"
    if proc.returncode == 0 and args.verify:
        if verification == "fail":
            status = "verification-failed"
        elif verification == "missing":
            status = "verification-missing"
    cira_prefetches = stats.get("board.cira.issuedPrefetches", 0)
    cira_indexed_prefetches = stats.get("board.cira.issuedIndexedPrefetches", 0)
    cira_csr_prefetches = stats.get("board.cira.issuedCsrPrefetches", 0)
    diagnostic_metrics = extract_diagnostic_metrics(stats, kind)
    if (
        kind == "cira"
        and proc.returncode == 0
        and cira_prefetches == 0
        and cira_indexed_prefetches == 0
        and cira_csr_prefetches == 0
        and not env_flag_enabled(args.env, "CIRA_GAPBS_DEVICE_OFFLOAD")
        and not args.allow_zero_cira
    ):
        status = "no-cira-events"

    return {
        "benchmark": benchmark,
        "label": label,
        "kind": kind,
        "status": status,
        "verification": verification,
        "sim_ticks": stats.get("simTicks", ""),
        "sim_insts": stats.get("simInsts", ""),
        "asmc_loads": stats.get("board.asmc.issuedLoads", 0),
        "cira_prefetches": cira_prefetches,
        "cira_indexed_prefetches": cira_indexed_prefetches,
        "cira_csr_prefetches": cira_csr_prefetches,
        "cira_completed": stats.get("board.cira.completedPrefetches", 0),
        **diagnostic_metrics,
        "run_dir": str(run_dir),
    }


def write_summary(path, rows):
    baseline_ticks = {
        row["benchmark"]: row.get("sim_ticks")
        for row in rows
        if row.get("kind") == "baseline" and row.get("sim_ticks")
    }
    fields = [
        "benchmark",
        "label",
        "kind",
        "status",
        "verification",
        "sim_ticks",
        "sim_insts",
        "speedup_vs_cxl",
        "asmc_loads",
        "cira_prefetches",
        "cira_indexed_prefetches",
        "cira_csr_prefetches",
        "cira_completed",
        "cxl_packets",
        "cxl_bytes",
        "l1d_demand_misses",
        "l2d_demand_hits",
        "l2d_demand_misses",
        "l2i_demand_hits",
        "l2i_demand_misses",
        "cira_total_latency",
        "cira_avg_latency",
        "run_dir",
    ]
    with path.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            out = {field: row.get(field, "") for field in fields}
            base = baseline_ticks.get(row["benchmark"])
            ticks = row.get("sim_ticks")
            if base and ticks:
                out["speedup_vs_cxl"] = base / ticks
            writer.writerow(out)


def main():
    parser = argparse.ArgumentParser(
        description="Compare CXL-only, AMU, and CIRA local GAPBS binaries."
    )
    parser.add_argument("--gem5", type=Path, default=DEFAULT_GEM5)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--baseline-bin-dir", type=Path, required=True)
    parser.add_argument("--amu-bin-dir", type=Path)
    parser.add_argument(
        "--cira-bin-dir",
        type=parse_label_path,
        action="append",
        default=[],
        help="CIRA label and binary directory, as LABEL=PATH.",
    )
    parser.add_argument("--benchmarks", default="bc,sssp,pr")
    parser.add_argument("--scale", type=int, default=4)
    parser.add_argument("--iterations", type=int, default=1)
    parser.add_argument(
        "--cpu", choices=["atomic", "timing", "o3", "minor"], default="timing"
    )
    parser.add_argument("--cores", type=int, default=1)
    parser.add_argument("--cxl-link-delay", default="1us")
    parser.add_argument("--disable-hw-prefetchers", action="store_true")
    parser.add_argument("--l1-mshrs", type=int)
    parser.add_argument("--l1-tgts-per-mshr", type=int)
    parser.add_argument("--l2-mshrs", type=int)
    parser.add_argument("--l2-tgts-per-mshr", type=int)
    parser.add_argument("--asmc-spm-size", default="256KiB")
    parser.add_argument("--asmc-granularity", type=int, default=8)
    parser.add_argument("--asmc-max-outstanding", type=int, default=256)
    parser.add_argument("--asmc-max-send-queue", type=int, default=512)
    parser.add_argument("--asmc-issue-latency", default="1ns")
    parser.add_argument("--asmc-completion-latency", default="0ns")
    parser.add_argument("--asmc-latency", default="0ns")
    parser.add_argument("--cira-max-outstanding", type=int, default=256)
    parser.add_argument("--cira-max-send-queue", type=int, default=1024)
    parser.add_argument("--cira-issue-latency", default="1ns")
    parser.add_argument("--cira-completion-latency", default="0ns")
    parser.add_argument(
        "--roi-work-events",
        action="store_true",
        help="Reset stats at m5_work_begin and stop at m5_work_end.",
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="Run GAPBS verification after dumping kernel ROI stats.",
    )
    parser.add_argument("--env", action="append", default=[])
    parser.add_argument(
        "--allow-zero-cira",
        action="store_true",
        help="Do not mark CIRA runs with zero issued prefetches invalid.",
    )
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--outdir",
        type=Path,
        default=REPO
        / "m5out"
        / "gapbs_cxl_amu_cira"
        / dt.datetime.now().strftime("%Y%m%d-%H%M%S"),
    )
    args = parser.parse_args()

    if not args.gem5.exists() and not args.dry_run:
        sys.exit(f"gem5 binary not found: {args.gem5}")
    if not args.config.exists():
        sys.exit(f"config not found: {args.config}")

    benchmarks = [b.strip() for b in args.benchmarks.split(",") if b.strip()]
    args.outdir.mkdir(parents=True, exist_ok=True)

    rows = []
    for benchmark in benchmarks:
        rows.append(
            run_one(args, benchmark, "cxl_vanilla", args.baseline_bin_dir, "baseline")
        )
        if args.amu_bin_dir is not None:
            rows.append(run_one(args, benchmark, "amu", args.amu_bin_dir, "amu"))
        for label, bin_dir in args.cira_bin_dir:
            rows.append(run_one(args, benchmark, label, bin_dir, "cira"))

    summary = args.outdir / "summary.csv"
    write_summary(summary, rows)
    print(f"Wrote {summary}")
    if args.verify and any(
        row["status"] != "ok" or row["verification"] != "pass"
        for row in rows
    ):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
