#!/usr/bin/env python3
#
# Build and run the CXL address-generation motivation microbenchmark.

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
SRC = REPO / "tests" / "test-progs" / "cxl-addrgen-motivation" / (
    "addrgen_motivation.c"
)
M5_LIB = REPO / "util" / "m5" / "build" / "x86" / "out" / "libm5.a"
DEFAULT_BINARY = REPO / "m5out" / "cxl_addrgen_motivation" / (
    "addrgen_motivation"
)


MODE_DESCRIPTIONS = {
    "known": "CPU computes data addresses locally; many independent misses.",
    "indirect": "CPU fetches remote index metadata before issuing data loads.",
    "double_indirect": (
        "CPU fetches two remote metadata levels before issuing data loads."
    ),
    "chase": "Each remote load returns the next address; one serial stream.",
    "chase_parallel": "Multiple independent pointer chains expose address MLP.",
}


def run(cmd, cwd=REPO, timeout=None, stdout=None):
    print(" ".join(str(part) for part in cmd), flush=True)
    return subprocess.run(
        cmd,
        cwd=cwd,
        stdout=stdout,
        stderr=subprocess.STDOUT if stdout is not None else None,
        text=True,
        check=False,
        timeout=timeout,
    )


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
            pass
    return stats


def stat(stats, key, default=0.0):
    return stats.get(key, default)


def sum_stats(stats, include, exclude=()):
    total = 0.0
    for key, value in stats.items():
        if all(token in key for token in include) and not any(
            token in key for token in exclude
        ):
            total += value
    return total


def ratio(num, den):
    if not den:
        return ""
    return num / den


def build_binary(args):
    binary = args.binary.resolve()
    if args.no_build:
        return binary

    if not M5_LIB.exists():
        sys.exit(f"gem5 m5 library not found: {M5_LIB}")
    binary.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        args.cc,
        "-std=c11",
        "-O3",
        "-Wall",
        "-Wextra",
        "-static",
        "-no-pie",
        "-I",
        str(REPO / "include"),
        str(SRC),
        str(M5_LIB),
        "-o",
        str(binary),
    ]
    proc = run(cmd)
    if proc.returncode != 0:
        sys.exit(proc.returncode)
    return binary


def mode_arguments(args, mode):
    streams = 1 if mode == "chase" else args.streams
    mode_args = [
        "--mode",
        mode,
        "--nodes",
        str(args.nodes),
        "--accesses",
        str(args.accesses),
        "--streams",
        str(streams),
        "--seed",
        str(args.seed),
    ]
    if args.no_flush_workload:
        mode_args.append("--no-flush")
    return mode_args


def run_mode(args, binary, mode):
    run_dir = args.outdir / mode
    run_dir.mkdir(parents=True, exist_ok=True)
    workload_args = " ".join(mode_arguments(args, mode))
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
        "1",
        "--cxl-memory",
        "--cxl-link-delay",
        args.cxl_link_delay,
        "--disable-hw-prefetchers",
        "--roi-work-events",
        "--no-asmc",
    ]
    for name, value in (
        ("--l1-mshrs", args.l1_mshrs),
        ("--l1-tgts-per-mshr", args.l1_tgts_per_mshr),
        ("--l2-mshrs", args.l2_mshrs),
        ("--l2-tgts-per-mshr", args.l2_tgts_per_mshr),
    ):
        if value is not None:
            cmd.extend([name, str(value)])

    if args.dry_run:
        return {
            "mode": mode,
            "status": "dry-run",
            "run_dir": str(run_dir),
        }

    with (run_dir / "gem5.log").open("w") as log:
        proc = run(cmd, timeout=args.timeout, stdout=log)
    stats = parse_stats(run_dir / "stats.txt")
    sim_ticks = stat(stats, "simTicks", "")
    sim_insts = stat(stats, "simInsts", "")
    pkt_count = sum_stats(
        stats,
        ("pktCount", "board.cxl_mem_link0.cpu_side_port"),
    )
    pkt_bytes = sum_stats(
        stats,
        ("pktSize", "board.cxl_mem_link0.cpu_side_port"),
    )
    l1d_misses = stat(
        stats, "board.cache_hierarchy.l1d-cache-0.overallMisses::total", ""
    )
    l2_misses = stat(
        stats, "board.cache_hierarchy.l2-cache-0.overallMisses::total", ""
    )
    l2_avg_mshr = stat(
        stats,
        "board.cache_hierarchy.l2-cache-0.overallAvgMshrMissLatency::total",
        "",
    )
    cycles = stat(stats, "board.processor.cores.core.numCycles", "")
    ipc = ratio(sim_insts, cycles) if sim_insts != "" and cycles != "" else ""
    ticks_per_access = ratio(sim_ticks, args.accesses) if sim_ticks != "" else ""
    packets_per_access = ratio(pkt_count, args.accesses)
    packets_per_us = ratio(pkt_count, sim_ticks / 1_000_000.0) if sim_ticks else ""
    useful_accesses_per_us = (
        ratio(args.accesses, sim_ticks / 1_000_000.0) if sim_ticks else ""
    )

    return {
        "mode": mode,
        "description": MODE_DESCRIPTIONS[mode],
        "status": "ok" if proc.returncode == 0 else f"exit-{proc.returncode}",
        "sim_ticks": sim_ticks,
        "sim_insts": sim_insts,
        "cycles": cycles,
        "ipc": ipc,
        "ticks_per_access": ticks_per_access,
        "cxl_packets": pkt_count,
        "cxl_packet_bytes": pkt_bytes,
        "cxl_packets_per_access": packets_per_access,
        "cxl_packets_per_us": packets_per_us,
        "useful_accesses_per_us": useful_accesses_per_us,
        "l1d_misses": l1d_misses,
        "l2_misses": l2_misses,
        "l2_avg_mshr_miss_latency_ticks": l2_avg_mshr,
        "run_dir": str(run_dir),
    }


def fmt(value, precision=2):
    if value == "" or value is None:
        return ""
    if isinstance(value, str):
        return value
    return f"{value:.{precision}f}"


def write_summary(path, rows):
    known_ticks = next(
        (
            row["sim_ticks"]
            for row in rows
            if row["mode"] == "known" and row.get("sim_ticks")
        ),
        "",
    )
    fields = [
        "mode",
        "description",
        "status",
        "sim_ticks",
        "sim_insts",
        "cycles",
        "ipc",
        "ticks_per_access",
        "slowdown_vs_known",
        "speedup_vs_known",
        "cxl_packets",
        "cxl_packet_bytes",
        "cxl_packets_per_access",
        "cxl_packets_per_us",
        "useful_accesses_per_us",
        "l1d_misses",
        "l2_misses",
        "l2_avg_mshr_miss_latency_ticks",
        "run_dir",
    ]
    with path.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            out = {field: row.get(field, "") for field in fields}
            ticks = row.get("sim_ticks")
            if known_ticks and ticks:
                out["slowdown_vs_known"] = ticks / known_ticks
                out["speedup_vs_known"] = known_ticks / ticks
            writer.writerow(out)


def write_latex(path, rows):
    lines = [
        r"\begin{table}[t]",
        r"\caption{CXL address-generation motivation. All modes use remote "
        r"CXL memory and are normalized to the CPU-known-address stream.}",
        r"\label{tab:cxl_addrgen_motivation}",
        r"\centering",
        r"\begin{tabular}{@{}llrrr@{}}",
        r"\hline",
        r"\textbf{Mode} & \textbf{Remote addr metadata} & "
        r"\textbf{Norm. time} & \textbf{Useful access/$\mu$s} & "
        r"\textbf{CXL pkt/$\mu$s} \\",
        r"\hline",
    ]
    known_ticks = next(
        (
            row["sim_ticks"]
            for row in rows
            if row["mode"] == "known" and row.get("sim_ticks")
        ),
        "",
    )
    display = {
        "known": "Known address",
        "indirect": "Remote index",
        "double_indirect": "Two-level index",
        "chase": "Pointer chase",
        "chase_parallel": "Parallel chase",
    }
    metadata = {
        "known": "0",
        "indirect": "1",
        "double_indirect": "2",
        "chase": "serial next",
        "chase_parallel": "parallel next",
    }
    for row in rows:
        ticks = row.get("sim_ticks")
        norm = ticks / known_ticks if known_ticks and ticks else ""
        lines.append(
            f"{display.get(row['mode'], row['mode'])} & "
            f"{metadata.get(row['mode'], '')} & "
            f"{fmt(norm)}$\\times$ & "
            f"{fmt(row.get('useful_accesses_per_us'))} & "
            f"{fmt(row.get('cxl_packets_per_us'))} \\\\"
        )
    lines += [
        r"\hline",
        r"\end{tabular}%",
        r"\end{table}",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(
        description="Run CXL address-generation motivation experiment."
    )
    parser.add_argument("--gem5", type=Path, default=DEFAULT_GEM5)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--binary", type=Path, default=DEFAULT_BINARY)
    parser.add_argument("--cc", default=os.environ.get("CC", "gcc"))
    parser.add_argument("--no-build", action="store_true")
    parser.add_argument(
        "--modes", default="known,indirect,double_indirect,chase,chase_parallel"
    )
    parser.add_argument("--nodes", type=int, default=32768)
    parser.add_argument("--accesses", type=int, default=4096)
    parser.add_argument("--streams", type=int, default=16)
    parser.add_argument("--seed", type=lambda x: int(x, 0), default=0x1234)
    parser.add_argument(
        "--no-flush-workload",
        action="store_true",
        help="Do not issue CLFLUSH before the ROI. Useful with O3 builds "
        "where CLFLUSH trips the load tracking assert.",
    )
    parser.add_argument(
        "--cpu", choices=["timing", "o3", "minor"], default="o3"
    )
    parser.add_argument("--cxl-link-delay", default="1us")
    parser.add_argument("--l1-mshrs", type=int, default=64)
    parser.add_argument("--l1-tgts-per-mshr", type=int, default=64)
    parser.add_argument("--l2-mshrs", type=int, default=64)
    parser.add_argument("--l2-tgts-per-mshr", type=int, default=64)
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--outdir",
        type=Path,
        default=REPO
        / "m5out"
        / "cxl_addrgen_motivation"
        / dt.datetime.now().strftime("%Y%m%d-%H%M%S"),
    )
    args = parser.parse_args()

    if not args.gem5.exists() and not args.dry_run:
        sys.exit(f"gem5 binary not found: {args.gem5}")
    if not args.config.exists():
        sys.exit(f"config not found: {args.config}")

    args.outdir.mkdir(parents=True, exist_ok=True)
    binary = build_binary(args)
    modes = [mode.strip() for mode in args.modes.split(",") if mode.strip()]
    unknown = sorted(set(modes) - set(MODE_DESCRIPTIONS))
    if unknown:
        sys.exit(f"unknown mode(s): {', '.join(unknown)}")

    rows = [run_mode(args, binary, mode) for mode in modes]
    summary = args.outdir / "summary.csv"
    table = args.outdir / "addrgen_motivation_table.tex"
    write_summary(summary, rows)
    write_latex(table, rows)
    print(f"Wrote {summary}")
    print(f"Wrote {table}")


if __name__ == "__main__":
    main()
