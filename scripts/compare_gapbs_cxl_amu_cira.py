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
from decimal import Decimal, InvalidOperation
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
DEFAULT_GEM5 = REPO / "build" / "X86" / "gem5.opt"
DEFAULT_CONFIG = (
    REPO / "configs" / "example" / "gem5_library" / "x86-gapbs-amu-se.py"
)

CXL_PACKET_STAT_PREFIX = "board.cache_hierarchy.membus.pktCount_"
CXL_BYTE_STAT_PREFIX = "board.cache_hierarchy.membus.pktSize_"
CXL_STAT_SUFFIX = "::board.cxl_mem_link0.cpu_side_port"
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
SWITCH_DIAGNOSTIC_STATS = {
    "l1d_demand_misses": (
        "board.cache_hierarchy.l1d-cache-0.demandMisses::"
        "processor.switch.core.data"
    ),
    "l2d_demand_hits": (
        "board.cache_hierarchy.l2-cache-0.demandHits::"
        "processor.switch.core.data"
    ),
    "l2d_demand_misses": (
        "board.cache_hierarchy.l2-cache-0.demandMisses::"
        "processor.switch.core.data"
    ),
    "l2i_demand_hits": (
        "board.cache_hierarchy.l2-cache-0.demandHits::"
        "processor.switch.core.inst"
    ),
    "l2i_demand_misses": (
        "board.cache_hierarchy.l2-cache-0.demandMisses::"
        "processor.switch.core.inst"
    ),
}
DIAGNOSTIC_FAMILY_TOTALS = {
    "l1d_demand_misses": (
        "board.cache_hierarchy.l1d-cache-0.demandMisses::total"
    ),
    "l2d_demand_hits": (
        "board.cache_hierarchy.l2-cache-0.demandHits::total"
    ),
    "l2d_demand_misses": (
        "board.cache_hierarchy.l2-cache-0.demandMisses::total"
    ),
    "l2i_demand_hits": (
        "board.cache_hierarchy.l2-cache-0.demandHits::total"
    ),
    "l2i_demand_misses": (
        "board.cache_hierarchy.l2-cache-0.demandMisses::total"
    ),
}
SWITCH_CACHE_REQUESTORS = (
    "processor.switch.core.data",
    "processor.switch.core.inst",
)
CIRA_LATENCY_STATS = {
    "cira_total_latency": "board.cira.totalLatency",
    "cira_avg_latency": "board.cira.avgLatency",
}
OWNED_COUNTER_STATS = {
    "asmc_loads": ("amu", "board.asmc.issuedLoads"),
    "asmc_completed": ("amu", "board.asmc.completedLoads"),
    "cira_prefetches": ("cira", "board.cira.issuedPrefetches"),
    "cira_completed": ("cira", "board.cira.completedPrefetches"),
    "cira_indexed_prefetches": (
        "cira",
        "board.cira.issuedIndexedPrefetches",
    ),
    "cira_csr_prefetches": ("cira", "board.cira.issuedCsrPrefetches"),
    "cira_useful": ("cira", "board.cira.usefulPrefetches"),
    "cira_late": ("cira", "board.cira.latePrefetches"),
    "cira_read_packets": ("cira", "board.cira.readPackets"),
    "cira_read_bytes": ("cira", "board.cira.readBytes"),
}
CPU_SWITCH_MARKER = "Switching from fast-forward CPU to timing CPU!"
SUMMARY_FIELDS = (
    "benchmark",
    "label",
    "kind",
    "status",
    "verification",
    "scale",
    "iterations",
    "measured_trial",
    "fast_forward_cpu",
    "roi_cpu",
    "cpu_switches",
    "cxl_link_delay",
    "all_memory_cxl",
    "sim_ticks",
    "sim_insts",
    "speedup_vs_cxl",
    "asmc_loads",
    "asmc_completed",
    "cira_prefetches",
    "cira_completed",
    "cira_indexed_prefetches",
    "cira_csr_prefetches",
    "cira_useful",
    "cira_late",
    "cira_read_packets",
    "cira_read_bytes",
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
)


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
    if not path.is_file():
        raise StatsError(f"missing stats file: {path}")
    in_first_section = False
    saw_begin = False
    saw_end = False
    for line in path.read_text(errors="replace").splitlines():
        if line.startswith("---------- Begin Simulation Statistics"):
            if in_first_section:
                break
            saw_begin = True
            in_first_section = True
            continue
        if line.startswith("---------- End Simulation Statistics"):
            if not saw_begin:
                raise StatsError(
                    f"{path}: missing Begin marker before End marker"
                )
            if not in_first_section:
                break
            saw_end = True
            break
        if not in_first_section:
            continue
        if not line or line.startswith("#") or line.startswith("---"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        try:
            stats[parts[0]] = Decimal(parts[1])
        except InvalidOperation:
            pass
    if not saw_begin:
        raise StatsError(f"{path}: missing Begin marker")
    if not saw_end:
        raise StatsError(f"{path}: missing End marker")
    if not stats:
        raise StatsError(f"{path}: empty first ROI stats section")
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


def unique_directional_stat(stats, prefix):
    candidates = [
        (name, value)
        for name, value in stats.items()
        if name.startswith(prefix) and name.endswith(CXL_STAT_SUFFIX)
    ]
    if len(candidates) != 1:
        raise StatsError(
            f"expected exactly one ROI statistic matching {prefix}*"
            f"{CXL_STAT_SUFFIX}; found {len(candidates)}"
        )
    name, value = candidates[0]
    cell = name[len(prefix) : -len(CXL_STAT_SUFFIX)]
    return cell, value


def directional_stat_pair(stats):
    packet_cell, packets = unique_directional_stat(
        stats, CXL_PACKET_STAT_PREFIX
    )
    byte_cell, byte_count = unique_directional_stat(stats, CXL_BYTE_STAT_PREFIX)
    if packet_cell != byte_cell:
        raise StatsError(
            "CXL packet/byte directional identity mismatch: "
            f"{packet_cell!r} != {byte_cell!r}"
        )
    return packets, byte_count


def cache_demand_vector(stats, cache_root, family, field):
    prefix = f"{cache_root}.{family}"
    total_name = f"{prefix}::total"
    cells = {
        name.split("::", 1)[1]: Decimal(str(value))
        for name, value in stats.items()
        if name.startswith(f"{prefix}::") and name != total_name
    }
    unknown = sorted(
        requestor
        for requestor in cells
        if requestor not in SWITCH_CACHE_REQUESTORS
    )
    if unknown:
        if any(
            requestor.startswith(f"{allowed}.")
            for requestor in unknown
            for allowed in SWITCH_CACHE_REQUESTORS
        ):
            raise StatsError(
                f"ambiguous timing switch requestor for {field}: "
                + ", ".join(unknown)
            )
        raise StatsError(
            f"unknown cache requestor for {field}: "
            + ", ".join(unknown)
        )
    total = (
        Decimal(str(stats[total_name]))
        if total_name in stats
        else None
    )
    if total is None and cells:
        raise StatsError(f"missing required ROI statistic: {total_name}")
    if total is not None:
        cell_sum = sum(cells.values(), Decimal(0))
        if cell_sum != total:
            raise StatsError(
                f"{family} family total {total} does not match "
                f"requestor cells {cell_sum}"
            )
    return {"family": family, "cells": cells, "total": total}


def resolve_demand_identity(accesses, hits, misses, context):
    if accesses is None:
        raise StatsError(
            f"missing exact switch requestor identity or demandAccesses "
            f"proof for {context}"
        )
    if hits is None and misses is None:
        if accesses != 0:
            raise StatsError(
                f"omitted nonzero demandHits/demandMisses for {context}: "
                f"demandAccesses={accesses}"
            )
        hits = misses = Decimal(0)
    elif hits is None:
        inferred = accesses - misses
        if inferred < 0:
            raise StatsError(
                f"demandAccesses identity mismatch for {context}: "
                f"{accesses} < demandMisses {misses}"
            )
        if inferred > 0:
            raise StatsError(
                f"omitted nonzero demandHits for {context}: {inferred}"
            )
        hits = Decimal(0)
    elif misses is None:
        inferred = accesses - hits
        if inferred < 0:
            raise StatsError(
                f"demandAccesses identity mismatch for {context}: "
                f"{accesses} < demandHits {hits}"
            )
        if inferred > 0:
            raise StatsError(
                f"omitted nonzero demandMisses for {context}: {inferred}"
            )
        misses = Decimal(0)
    if accesses != hits + misses:
        raise StatsError(
            f"demandAccesses identity mismatch for {context}: "
            f"{accesses} != {hits} + {misses}"
        )
    return accesses, hits, misses


def semantic_cache_diagnostic(stats, stat_name, field):
    family_prefix, expected_requestor = stat_name.split("::", 1)
    cache_root, target_family = family_prefix.rsplit(".", 1)
    cache_stats = [
        name for name in stats if name.startswith(f"{cache_root}.")
    ]
    legacy = [
        name
        for name in cache_stats
        if "::processor.cores.core." in name
    ]
    if legacy:
        raise StatsError(
            f"legacy cache requestor for {field}: "
            + ", ".join(sorted(legacy))
        )
    vectors = {
        family: cache_demand_vector(stats, cache_root, family, field)
        for family in ("demandAccesses", "demandHits", "demandMisses")
    }
    resolved = {}
    resolved["total"] = resolve_demand_identity(
        vectors["demandAccesses"]["total"],
        vectors["demandHits"]["total"],
        vectors["demandMisses"]["total"],
        f"{cache_root} totals",
    )
    for requestor in SWITCH_CACHE_REQUESTORS:
        values = [
            vector["cells"].get(
                requestor,
                Decimal(0) if vector["total"] is not None else None,
            )
            for vector in (
                vectors["demandAccesses"],
                vectors["demandHits"],
                vectors["demandMisses"],
            )
        ]
        resolved[requestor] = resolve_demand_identity(
            *values, f"{cache_root}::{requestor}"
        )
    family_index = {
        "demandAccesses": 0,
        "demandHits": 1,
        "demandMisses": 2,
    }
    for family, index in family_index.items():
        cell_sum = sum(
            (
                resolved[requestor][index]
                for requestor in SWITCH_CACHE_REQUESTORS
            ),
            Decimal(0),
        )
        if cell_sum != resolved["total"][index]:
            raise StatsError(
                f"{family} requestor/total identity mismatch for "
                f"{cache_root}: {cell_sum} != "
                f"{resolved['total'][index]}"
            )
    return resolved[expected_requestor][family_index[target_family]]


def cache_diagnostic(stats, field, fast_forward=False):
    family_total = DIAGNOSTIC_FAMILY_TOTALS[field]
    stat_name = (
        SWITCH_DIAGNOSTIC_STATS[field]
        if fast_forward
        else DIAGNOSTIC_STATS[field]
    )
    if not fast_forward:
        if family_total not in stats:
            raise StatsError(
                f"missing required ROI statistic: {family_total}"
            )
        return stats.get(stat_name, 0)

    return semantic_cache_diagnostic(stats, stat_name, field)


def extract_diagnostic_metrics(stats, kind, fast_forward=False):
    if kind == "cira":
        for name in CIRA_LATENCY_STATS.values():
            if name not in stats:
                raise StatsError(f"missing required ROI statistic: {name}")
    packets, byte_count = directional_stat_pair(stats)
    metrics = {"cxl_packets": packets, "cxl_bytes": byte_count}
    metrics.update(
        {
            field: cache_diagnostic(stats, field, fast_forward)
            for field in DIAGNOSTIC_STATS
        }
    )
    metrics.update(
        {
            field: stats.get(stat_name, 0)
            for field, stat_name in CIRA_LATENCY_STATS.items()
        }
    )
    return metrics


def extract_owned_metrics(stats, kind):
    metrics = {}
    for field, (owner, stat_name) in OWNED_COUNTER_STATS.items():
        if kind != owner:
            metrics[field] = 0
            continue
        if stat_name not in stats:
            raise StatsError(f"missing required ROI statistic: {stat_name}")
        metrics[field] = stats[stat_name]
    return metrics


def parse_cpu_switches(path):
    if not path.exists():
        return 0
    return sum(
        line == CPU_SWITCH_MARKER
        for line in path.read_text(errors="replace").splitlines()
    )


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
        "--scale",
        str(args.scale),
        "--iterations",
        str(args.iterations),
        "--measure-trial",
        str(args.measure_trial),
        "--cores",
        str(args.cores),
        "--cxl-memory",
        "--cxl-link-delay",
        args.cxl_link_delay,
    ]
    if args.fast_forward_cpu is not None:
        cmd += ["--fast-forward-cpu", args.fast_forward_cpu]

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
    owned_metrics = extract_owned_metrics(stats, kind)
    diagnostic_metrics = extract_diagnostic_metrics(
        stats, kind, fast_forward=bool(args.fast_forward_cpu)
    )
    if (
        kind == "cira"
        and proc.returncode == 0
        and owned_metrics["cira_prefetches"] == 0
        and owned_metrics["cira_indexed_prefetches"] == 0
        and owned_metrics["cira_csr_prefetches"] == 0
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
        "scale": args.scale,
        "iterations": args.iterations,
        "measured_trial": args.measure_trial,
        "fast_forward_cpu": args.fast_forward_cpu or "",
        "roi_cpu": args.cpu,
        "cpu_switches": parse_cpu_switches(log_path),
        "cxl_link_delay": args.cxl_link_delay,
        "all_memory_cxl": True,
        "sim_ticks": stats.get("simTicks", ""),
        "sim_insts": stats.get("simInsts", ""),
        **owned_metrics,
        **diagnostic_metrics,
        "run_dir": str(run_dir),
    }


def write_summary(path, rows):
    baseline_ticks = {
        row["benchmark"]: row.get("sim_ticks")
        for row in rows
        if row.get("kind") == "baseline" and row.get("sim_ticks")
    }
    with path.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=SUMMARY_FIELDS)
        writer.writeheader()
        for row in rows:
            out = {field: row.get(field, "") for field in SUMMARY_FIELDS}
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
    parser.add_argument(
        "--fast-forward-cpu",
        choices=["atomic"],
        help="Use this CPU before switching to --cpu at trial 0 begin.",
    )
    parser.add_argument("--measure-trial", type=int, default=0)
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

    if args.iterations <= 0:
        parser.error("--iterations must be positive")
    if args.measure_trial < 0 or args.measure_trial >= args.iterations:
        parser.error("--measure-trial must be less than iterations")
    if args.fast_forward_cpu and not args.roi_work_events:
        parser.error("--fast-forward-cpu requires --roi-work-events")
    if args.fast_forward_cpu and args.cpu != "timing":
        parser.error("--fast-forward-cpu requires --cpu timing")
    if args.fast_forward_cpu and (
        args.iterations != 2 or args.measure_trial != 1
    ):
        parser.error(
            "--fast-forward-cpu requires --iterations 2 and --measure-trial 1"
        )
    if args.fast_forward_cpu and args.scale != 20:
        parser.error("--fast-forward-cpu requires --scale 20")
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
    if args.verify and not args.dry_run and any(
        row["status"] != "ok" or row["verification"] != "pass"
        for row in rows
    ):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
