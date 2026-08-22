# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

"""Build and run the deterministic Vanilla/AMU/CIRA PR-row smoke proof."""

import re
import subprocess
import tempfile
from decimal import Decimal, InvalidOperation
from pathlib import Path


REPO = Path(__file__).resolve().parents[3]
WORKLOAD = Path(__file__).with_name("pr_row_offload_smoke.cc")
CONFIG = REPO / "configs/example/gem5_library/x86-gapbs-amu-se.py"
BITS_RE = re.compile(
    r"^PR_ROW_ITER_BITS mode=(vanilla|amu|cira) iteration=([012]) "
    r"words=([0-9a-f]{8}(?:,[0-9a-f]{8}){5})$"
)


class SmokeError(RuntimeError):
    pass


def validate_word_rows(rows):
    expected = rows.get("vanilla")
    if expected is None or len(expected) != 3:
        raise SmokeError("missing Vanilla bit-exact reference")
    for mode in ("amu", "cira"):
        if rows.get(mode) != expected:
            raise SmokeError(f"{mode} output is not bit-exact")


def parse_words(output, mode):
    rows = [None, None, None]
    for line in output.splitlines():
        match = BITS_RE.fullmatch(line)
        if match is None or match.group(1) != mode:
            continue
        iteration = int(match.group(2))
        if rows[iteration] is not None:
            raise SmokeError(f"duplicate {mode} iteration {iteration}")
        rows[iteration] = tuple(
            int(word, 16) for word in match.group(3).split(",")
        )
    if any(row is None for row in rows):
        raise SmokeError(f"missing {mode} iteration marker")
    if f"PR_ROW_VERIFY mode={mode} status=PASS" not in output:
        raise SmokeError(f"missing {mode} verification marker")
    return rows


def parse_stats(path):
    stats = {}
    for line in path.read_text(errors="replace").splitlines():
        fields = line.split()
        if len(fields) >= 2:
            try:
                value = Decimal(fields[1])
                if value.is_finite():
                    stats[fields[0]] = int(value)
            except (InvalidOperation, ValueError, OverflowError):
                pass
    return stats


def compile_binary(root, mode, m5_library, *defines):
    binary = root / (mode + ("-" + "-".join(defines) if defines else ""))
    command = [
        "g++", "-std=c++17", "-O2", "-Wall", "-Wextra", "-static",
        "-no-pie", "-ffp-contract=off", "-fno-fast-math",
        "-I", str(REPO / "include"), "-I", str(REPO / "util/amu"),
        "-I", str(REPO / "util/cira"), "-I", str(REPO / "util/pr_offload"),
        f"-DPR_MODE_{mode.upper()}=1",
        *[f"-D{name}=1" for name in defines],
        str(WORKLOAD), str(m5_library), "-o", str(binary),
    ]
    completed = subprocess.run(command, cwd=REPO, capture_output=True, text=True)
    if completed.returncode != 0:
        raise SmokeError(completed.stdout + completed.stderr)
    return binary


def gem5_command(gem5, binary, outdir, mode, *, queue_entries=32):
    command = [
        str(gem5), f"--outdir={outdir}", str(CONFIG), "--binary", str(binary),
        "--arguments", "", "--scale", "1", "--iterations", "1",
        "--cores", "4", "--cpu", "timing", "--mem-size", "4GiB",
        "--disable-hw-prefetchers", "--cxl-memory", "--cxl-link-delay", "1us",
        "--roi-work-events", "--continue-after-roi",
        "--require-m5-verification-exit",
    ]
    if mode == "vanilla":
        command.append("--no-asmc")
    elif mode == "amu":
        command += [
            "--asmc-pr-descriptor-entries", str(queue_entries),
            "--asmc-pr-read-entries", "1024",
        ]
    else:
        command += [
            "--no-asmc", "--cira", "--cira-to-l2",
            "--cira-pr-descriptor-entries", str(queue_entries),
            "--cira-pr-csr-read-entries", "256",
            "--cira-pr-coherent-entries", "256",
        ]
    return command


def run_case(gem5, binary, root, mode, *, queue_entries=32):
    outdir = root / (binary.name + "-m5out")
    completed = subprocess.run(
        gem5_command(gem5, binary, outdir, mode,
                     queue_entries=queue_entries),
        cwd=REPO, capture_output=True, text=True, timeout=300,
    )
    return completed, outdir, completed.stdout + completed.stderr


def validate_mechanism(stats, mode):
    if mode == "amu":
        prefix = "board.asmc"
        issued = stats[f"{prefix}.issuedPrDescriptors"]
        completed = stats[f"{prefix}.completedPrDescriptors"]
        if issued != 24 or completed != issued:
            raise SmokeError("AMU descriptors are not balanced")
        if stats[f"{prefix}.prReadPackets"] <= 0 or \
                stats[f"{prefix}.prWritePackets"] <= 0:
            raise SmokeError("AMU data path is inactive")
    elif mode == "cira":
        prefix = "board.cira"
        issued = stats[f"{prefix}.issuedPrDescriptors"]
        completed = stats[f"{prefix}.completedPrDescriptors"]
        if issued != 24 or completed != issued or \
                stats[f"{prefix}.prOutstandingWork"] != 0:
            raise SmokeError("CIRA descriptors are not balanced and drained")
        for core in range(4):
            if stats[f"{prefix}.issuedPrDescriptorsPerCore::{core}"] != 6:
                raise SmokeError("CIRA core is inactive")
            if stats[f"{prefix}.completedPrDescriptorsPerCore::{core}"] != 6:
                raise SmokeError("CIRA core completion differs")


def run_smoke(*, gem5, m5_library):
    gem5 = Path(gem5).resolve()
    m5_library = Path(m5_library).resolve()
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        rows = {}
        mode_proofs = {}
        for mode in ("vanilla", "amu", "cira"):
            binary = compile_binary(root, mode, m5_library)
            completed, outdir, output = run_case(gem5, binary, root, mode)
            if completed.returncode != 0:
                raise SmokeError(output)
            rows[mode] = parse_words(output, mode)
            stats = parse_stats(outdir / "stats.txt")
            validate_mechanism(stats, mode)
            config = (outdir / "config.ini").read_text(errors="replace")
            if "delay=1000000" not in config:
                raise SmokeError("CXL delay is not exactly 1000000 ticks")
            mode_proofs[mode] = {"issued": stats.get(
                f"board.{ 'asmc' if mode == 'amu' else 'cira' }.issuedPrDescriptors",
                0,
            )}
        validate_word_rows(rows)

        failed = []
        bit_binary = compile_binary(root, "vanilla", m5_library, "PR_INJECT_BIT")
        result, _, _ = run_case(gem5, bit_binary, root, "vanilla")
        if result.returncode == 0:
            raise SmokeError("changed-bit injection did not fail")
        failed.append("changed-bit")

        queue_binary = compile_binary(
            root, "cira", m5_library, "PR_INJECT_QUEUE"
        )
        result, _, _ = run_case(
            gem5, queue_binary, root, "cira", queue_entries=1
        )
        if result.returncode == 0:
            raise SmokeError("queue-capacity injection did not fail")
        failed.append("queue-capacity")

        unfinished = compile_binary(
            root, "cira", m5_library, "PR_INJECT_UNFINISHED"
        )
        result, outdir, _ = run_case(gem5, unfinished, root, "cira")
        stats = parse_stats(outdir / "stats.txt")
        if stats.get("board.cira.issuedPrDescriptors", 0) == \
                stats.get("board.cira.completedPrDescriptors", 0):
            raise SmokeError("unfinished-write injection did not fail closed")
        failed.append("unfinished-write")

        return {
            "status": "PASS", "delay_ticks": 1_000_000,
            "modes": mode_proofs, "failed_injections": failed,
            "rows": rows,
        }
