#!/usr/bin/env python3
#
# Run local GAPBS binaries under the CXL/AMU/CIRA timing config and summarize
# speedups against the CXL-only baseline.

import argparse
import csv
import datetime as dt
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from decimal import Decimal, InvalidOperation
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from gapbs_checkpoint import (  # noqa: E402
    CheckpointError,
    build_identity,
    identity_key,
    load_manifest,
    sha256_file,
    validate_reuse,
    write_manifest,
)
from gapbs_pr_experiment_profiles import (  # noqa: E402
    PROFILES as EXPERIMENT_PROFILES,
    ProfileError,
    get_profile,
    require_latency,
)


DEFAULT_GEM5 = REPO / "build" / "X86" / "gem5.opt"
DEFAULT_CONFIG = (
    REPO / "configs" / "example" / "gem5_library" / "x86-gapbs-amu-se.py"
)
CHECKPOINT_CONFIG_DEPENDENCIES = (
    REPO / "configs" / "example" / "gem5_library" / "gapbs_roi_state.py",
)

CXL_PACKET_STAT_PREFIX = "board.cache_hierarchy.membus.pktCount_"
CXL_BYTE_STAT_PREFIX = "board.cache_hierarchy.membus.pktSize_"
CXL_STAT_SUFFIX = "::board.cxl_mem_link0.cpu_side_port"
MEM_CTRL_EXACT_STATS = {
    "mem_ctrl_read_reqs": "board.memory.mem_ctrl.readReqs",
    "mem_ctrl_read_bursts": "board.memory.mem_ctrl.readBursts",
    "mem_ctrl_bytes_read": "board.memory.mem_ctrl.bytesReadSys",
}
REAL_CXL_FIELDS = (
    "mem_ctrl_read_reqs",
    "mem_ctrl_read_bursts",
    "mem_ctrl_bytes_read",
    "mem_ctrl_cpu_data_reads",
)
MEM_CTRL_REQUESTOR_PREFIX = (
    "board.memory.mem_ctrl.requestorReadAccesses::"
)
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
CIRA_EVIDENCE_STATS = {
    "cira_coalesced": "board.cira.coalescedPrefetches",
    "cira_rejected_queue_full": "board.cira.rejectedQueueFull",
    "cira_dropped_csr_descriptors": "board.cira.droppedCsrDescriptors",
    "cira_csr_queue_high_watermark": "board.cira.csrQueueHighWatermark",
    "cira_csr_index_read_packets": "board.cira.csrIndexReadPackets",
    "cira_csr_index_read_bytes": "board.cira.csrIndexReadBytes",
    "cira_completed_csr_index_reads": "board.cira.completedCsrIndexReads",
    "cira_rejected_csr_index_queue_full": (
        "board.cira.rejectedCsrIndexQueueFull"
    ),
    "cira_timing_csr_traversal": "board.cira.timingCsrTraversalEnabled",
}
CIRA_PER_CORE_STATS = {
    "cira_csr_per_core": "board.cira.issuedCsrPrefetchesPerCore",
    "cira_issued_per_core": "board.cira.issuedPrefetchesPerCore",
    "cira_completed_per_core": "board.cira.completedPrefetchesPerCore",
    "cira_useful_per_core": "board.cira.usefulPrefetchesPerCore",
}
CPU_SWITCH_MARKER = "Switching from fast-forward CPU to timing CPU!"
CHECKPOINT_SAVE_MARKER = "GAPBS_CHECKPOINT_SAVED path="
CHECKPOINT_RESTORE_MARKER = "GAPBS_CHECKPOINT_RESTORED path="
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
    "cores",
    "cpu_switches",
    "cxl_link_delay",
    "all_memory_cxl",
    "graph_path",
    "graph_scale",
    "graph_sha256",
    "checkpoint_id",
    "checkpoint_manifest",
    "checkpoint_binary_sha256",
    "checkpoint_restores",
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
    "cira_csr_per_core",
    "cira_issued_per_core",
    "cira_completed_per_core",
    "cira_useful_per_core",
    "cira_coalesced",
    "cira_rejected_queue_full",
    "cira_dropped_csr_descriptors",
    "cira_csr_queue_high_watermark",
    "cira_csr_index_read_packets",
    "cira_csr_index_read_bytes",
    "cira_completed_csr_index_reads",
    "cira_rejected_csr_index_queue_full",
    "cira_timing_csr_traversal",
    "cxl_packets",
    "cxl_bytes",
    *REAL_CXL_FIELDS,
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


def directional_stat_pair(stats, num_cores=1, kind="baseline"):
    if num_cores > 1:
        expected_cells = {
            f"board.cache_hierarchy.l2-cache-{core}.mem_side_port"
            for core in range(num_cores)
        }
        if kind == "amu":
            expected_cells.add("board.asmc_io_cache.mem_side_port")
        directional = {}
        for label, prefix in (
            ("packet", CXL_PACKET_STAT_PREFIX),
            ("byte", CXL_BYTE_STAT_PREFIX),
        ):
            directional[label] = {
                name[len(prefix) : -len(CXL_STAT_SUFFIX)]: value
                for name, value in stats.items()
                if name.startswith(prefix) and name.endswith(CXL_STAT_SUFFIX)
            }
            actual_cells = set(directional[label])
            valid = (
                actual_cells == expected_cells
                if label == "packet"
                else actual_cells <= expected_cells
            )
            if not valid:
                raise StatsError(
                    f"expected directional CXL cells {sorted(expected_cells)} "
                    f"for {label} statistics; found {sorted(actual_cells)}"
                )
        return (
            sum(directional["packet"].values(), Decimal(0)),
            sum(directional["byte"].values(), Decimal(0)),
        )

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


def _counter(stats, name):
    if name not in stats:
        raise StatsError(f"missing required ROI statistic: {name}")
    value = Decimal(str(stats[name]))
    if not value.is_finite() or value < 0 or value != value.to_integral_value():
        raise StatsError(f"ROI counter {name} is not a nonnegative integer")
    return value


def extract_real_cxl_metrics(stats, num_cores=1):
    if num_cores < 1:
        raise StatsError(f"num_cores must be positive; got {num_cores}")
    metrics = {
        field: _counter(stats, stat_name)
        for field, stat_name in MEM_CTRL_EXACT_STATS.items()
    }
    if num_cores == 1:
        accepted = {
            "processor.cores.core.data",
            "processor.cores0.core.data",
        }
    else:
        accepted = {
            f"processor.cores{core}.core.data" for core in range(num_cores)
        }
    cpu_data_reads = Decimal(0)
    for name in stats:
        if not name.startswith(MEM_CTRL_REQUESTOR_PREFIX):
            continue
        requestor = name[len(MEM_CTRL_REQUESTOR_PREFIX):]
        if requestor in accepted:
            cpu_data_reads += _counter(stats, name)
        elif requestor.startswith("processor.cores") and ".core.data" in requestor:
            raise StatsError(f"ambiguous CPU data requestor: {name}")
    metrics["mem_ctrl_cpu_data_reads"] = cpu_data_reads
    return metrics


def require_real_cxl(metrics):
    for field in REAL_CXL_FIELDS:
        if field not in metrics:
            raise StatsError(f"{field} is missing from real-CXL evidence")
        value = Decimal(str(metrics[field]))
        if not value.is_finite() or value <= 0:
            raise StatsError(f"{field} must be positive in measured ROI")


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


CORE_DIAGNOSTIC_LAYOUT = {
    "l1d_demand_misses": ("l1d-cache", "data", "demandMisses"),
    "l2d_demand_hits": ("l2-cache", "data", "demandHits"),
    "l2d_demand_misses": ("l2-cache", "data", "demandMisses"),
    "l2i_demand_hits": ("l2-cache", "inst", "demandHits"),
    "l2i_demand_misses": ("l2-cache", "inst", "demandMisses"),
}


def validate_core_requestor_identities(stats, num_cores):
    for core in range(num_cores):
        for cache_name, role in (
            ("l1d-cache", "data"),
            ("l2-cache", "data"),
            ("l2-cache", "inst"),
        ):
            prefix = (
                f"board.cache_hierarchy.{cache_name}-{core}."
            )
            requestor = f"processor.cores{core}.core.{role}"
            names = [
                f"{prefix}{family}::{requestor}"
                for family in (
                    "demandAccesses",
                    "demandHits",
                    "demandMisses",
                )
            ]
            if names[0] not in stats and any(
                name in stats for name in names[1:]
            ):
                raise StatsError(
                    "missing exact core requestor identity: " + names[0]
                )


def validate_cache_family_total(stats, cache_root, family):
    prefix = f"{cache_root}.{family}::"
    total_name = f"{prefix}total"
    cells = {
        name: Decimal(str(value))
        for name, value in stats.items()
        if name.startswith(prefix) and name != total_name
    }
    if not cells and total_name not in stats:
        return
    if total_name not in stats:
        raise StatsError(f"missing required ROI statistic: {total_name}")
    cell_sum = sum(cells.values(), Decimal(0))
    total = Decimal(str(stats[total_name]))
    if cell_sum != total:
        raise StatsError(
            f"{family} family total {total} does not match "
            f"requestor cells {cell_sum} for {cache_root}"
        )


def core_cache_diagnostic(stats, field, num_cores):
    cache_name, role, target_family = CORE_DIAGNOSTIC_LAYOUT[field]
    family_index = {
        "demandAccesses": 0,
        "demandHits": 1,
        "demandMisses": 2,
    }
    result = Decimal(0)
    for core in range(num_cores):
        cache_root = f"board.cache_hierarchy.{cache_name}-{core}"
        requestor = f"processor.cores{core}.core.{role}"
        accesses_name = f"{cache_root}.demandAccesses::{requestor}"
        for family in ("demandAccesses", "demandHits", "demandMisses"):
            validate_cache_family_total(stats, cache_root, family)
            ambiguous = [
                name
                for name in stats
                if name.startswith(f"{cache_root}.{family}::{requestor}.")
            ]
            if ambiguous:
                raise StatsError(
                    f"ambiguous core requestor for {field}: "
                    + ", ".join(sorted(ambiguous))
                )
        if accesses_name not in stats:
            target_cells = [
                f"{cache_root}.{family}::{requestor}"
                for family in ("demandHits", "demandMisses")
            ]
            if any(name in stats for name in target_cells):
                raise StatsError(
                    f"missing exact core requestor identity for {field}: "
                    f"{accesses_name}"
                )
            continue
        values = []
        for family in ("demandAccesses", "demandHits", "demandMisses"):
            name = f"{cache_root}.{family}::{requestor}"
            values.append(
                Decimal(str(stats[name])) if name in stats else None
            )
        resolved = resolve_demand_identity(
            *values, f"{cache_root}::{requestor}"
        )
        result += resolved[family_index[target_family]]
    return result


def extract_diagnostic_metrics(
    stats, kind, fast_forward=False, num_cores=1
):
    if num_cores < 1:
        raise StatsError(f"num_cores must be positive; got {num_cores}")
    if num_cores > 1 and fast_forward:
        raise StatsError("multi-core cache diagnostics do not support switching")
    if num_cores > 1:
        validate_core_requestor_identities(stats, num_cores)
    if kind == "cira":
        for name in CIRA_LATENCY_STATS.values():
            if name not in stats:
                raise StatsError(f"missing required ROI statistic: {name}")
    packets, byte_count = directional_stat_pair(stats, num_cores, kind)
    metrics = {"cxl_packets": packets, "cxl_bytes": byte_count}
    metrics.update(
        {
            field: (
                core_cache_diagnostic(stats, field, num_cores)
                if num_cores > 1
                else cache_diagnostic(stats, field, fast_forward)
            )
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


def extract_cira_evidence(stats, kind, num_cores):
    if num_cores < 1:
        raise StatsError(f"num_cores must be positive; got {num_cores}")

    if kind != "cira":
        return {
            **{field: "" for field in CIRA_PER_CORE_STATS},
            **{field: 0 for field in CIRA_EVIDENCE_STATS},
        }

    evidence = {}
    for field, stat_name in CIRA_EVIDENCE_STATS.items():
        if stat_name not in stats:
            raise StatsError(f"missing required ROI statistic: {stat_name}")
        evidence[field] = stats[stat_name]

    for field, stat_root in CIRA_PER_CORE_STATS.items():
        values = []
        for core in range(num_cores):
            stat_name = f"{stat_root}::{core}"
            if stat_name not in stats:
                raise StatsError(
                    f"missing required ROI statistic: {stat_name}"
                )
            values.append(str(stats[stat_name]))
        evidence[field] = ";".join(values)
    return evidence


def cira_evidence_failure(evidence, num_cores):
    if (
        evidence["cira_rejected_queue_full"] != 0
        or evidence["cira_dropped_csr_descriptors"] != 0
        or evidence["cira_rejected_csr_index_queue_full"] != 0
    ):
        return "cira-rejected-work"

    if (
        evidence["cira_timing_csr_traversal"] != 1
        or evidence["cira_csr_index_read_packets"] <= 0
        or evidence["cira_completed_csr_index_reads"] <= 0
    ):
        return "cira-invalid-csr-timing-path"
    if (
        evidence["cira_csr_index_read_packets"]
        != evidence["cira_completed_csr_index_reads"]
    ):
        return "cira-incomplete-csr-index-reads"

    issued = [
        Decimal(value)
        for value in evidence["cira_issued_per_core"].split(";")
    ]
    completed = [
        Decimal(value)
        for value in evidence["cira_completed_per_core"].split(";")
    ]
    descriptors = [
        Decimal(value)
        for value in evidence["cira_csr_per_core"].split(";")
    ]
    if (len(issued) != num_cores or len(completed) != num_cores or
            len(descriptors) != num_cores):
        raise StatsError("CIRA per-core evidence width does not match cores")
    if any(value <= 0 for value in descriptors):
        return "inactive-cira-core"
    if issued != completed:
        return "cira-incomplete-work"
    return None


def parse_cpu_switches(path):
    if not path.exists():
        return 0
    return sum(
        line == CPU_SWITCH_MARKER
        for line in path.read_text(errors="replace").splitlines()
    )


def parse_marker_count(path, marker):
    if not path.exists():
        return 0
    return sum(
        line.startswith(marker)
        for line in path.read_text(errors="replace").splitlines()
    )


def parse_marker_values(path, marker):
    if not path.exists():
        return []
    return [
        line[len(marker) :]
        for line in path.read_text(errors="replace").splitlines()
        if line.startswith(marker)
    ]


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


def append_kind_args(cmd, args, kind):
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
            "--cira-max-csr-walk-queue",
            str(args.cira_max_csr_walk_queue),
            "--cira-max-csr-index-reads",
            str(args.cira_max_csr_index_reads),
            "--cira-csr-lines-per-turn",
            str(args.cira_csr_lines_per_turn),
            "--cira-max-completed-lines",
            str(args.cira_max_completed_lines),
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


def checkpoint_model_parameters(args, kind):
    parameters = {
        "kind": kind,
        "env": "\0".join(args.env),
    }
    if kind == "amu":
        parameters.update(
            {
                "asmc_spm_size": args.asmc_spm_size,
                "asmc_granularity": args.asmc_granularity,
                "asmc_max_outstanding": args.asmc_max_outstanding,
                "asmc_max_send_queue": args.asmc_max_send_queue,
                "asmc_issue_latency": args.asmc_issue_latency,
                "asmc_completion_latency": args.asmc_completion_latency,
                "asmc_latency": args.asmc_latency,
            }
        )
    elif kind == "cira":
        parameters.update(
            {
                "cira_max_outstanding": args.cira_max_outstanding,
                "cira_max_send_queue": args.cira_max_send_queue,
                "cira_max_csr_walk_queue": args.cira_max_csr_walk_queue,
                "cira_max_csr_index_reads": args.cira_max_csr_index_reads,
                "cira_csr_lines_per_turn": args.cira_csr_lines_per_turn,
                "cira_max_completed_lines": args.cira_max_completed_lines,
                "cira_issue_latency": args.cira_issue_latency,
                "cira_completion_latency": args.cira_completion_latency,
            }
        )
    return parameters


def checkpoint_workload_arguments(args):
    arguments = [
        "-f",
        str(args.graph.resolve()),
        "-n",
        str(args.iterations),
    ]
    if args.verify:
        arguments.append("-v")
    return arguments


def checkpoint_common_command(
    args,
    *,
    binary,
    workload_arguments,
    outdir,
    cpu,
    link_delay,
):
    return [
        str(args.gem5),
        f"--outdir={outdir}",
        str(args.config),
        "--binary",
        str(binary),
        "--arguments",
        shlex.join(workload_arguments),
        "--cpu",
        cpu,
        "--scale",
        str(args.graph_scale),
        "--iterations",
        str(args.iterations),
        "--measure-trial",
        str(args.measure_trial),
        "--cores",
        str(args.cores),
        "--mem-size",
        args.mem_size,
        "--cxl-link-delay",
        link_delay,
        "--roi-work-events",
    ]


def checkpoint_identity(args, binary, kind, workload_arguments):
    return build_identity(
        binary=binary,
        graph=args.graph,
        graph_scale=args.graph_scale,
        arguments=workload_arguments,
        cores=args.cores,
        memory_size=args.mem_size,
        gem5=args.gem5,
        config=args.config,
        config_dependencies=CHECKPOINT_CONFIG_DEPENDENCIES,
        kind=kind,
        model_parameters=checkpoint_model_parameters(args, kind),
    )


def ensure_checkpoint(args, binary, kind, run_dir, workload_arguments):
    identity = checkpoint_identity(
        args, binary, kind, workload_arguments
    )
    checkpoint_id = identity_key(identity)
    checkpoint_dir = args.checkpoint_root / checkpoint_id
    manifest_path = checkpoint_dir / "manifest.json"

    if args.dry_run:
        temporary = (
            args.checkpoint_root / f".{checkpoint_id}.tmp-dry-run"
        )
        save_cmd = checkpoint_common_command(
            args,
            binary=binary,
            workload_arguments=workload_arguments,
            outdir=temporary / "gem5-out",
            cpu="atomic",
            link_delay="0ns",
        )
        save_cmd += ["--checkpoint-save", str(temporary)]
        append_kind_args(save_cmd, args, kind)
        for env in args.env:
            save_cmd += ["--env", env]
        print(" ".join(save_cmd), flush=True)
        return checkpoint_dir, manifest_path, identity, checkpoint_id

    if checkpoint_dir.exists():
        if not args.reuse_checkpoints:
            raise CheckpointError(
                f"checkpoint exists but reuse is disabled: {checkpoint_dir}"
            )
        validate_reuse(checkpoint_dir, identity)
        return checkpoint_dir, manifest_path, identity, checkpoint_id

    args.checkpoint_root.mkdir(parents=True, exist_ok=True)
    temporary = Path(
        tempfile.mkdtemp(
            prefix=f".{checkpoint_id}.tmp-",
            dir=args.checkpoint_root,
        )
    )
    save_log = temporary / "checkpoint.log"
    save_cmd = checkpoint_common_command(
        args,
        binary=binary,
        workload_arguments=workload_arguments,
        outdir=temporary / "gem5-out",
        cpu="atomic",
        link_delay="0ns",
    )
    save_cmd += ["--checkpoint-save", str(temporary)]
    append_kind_args(save_cmd, args, kind)
    for env in args.env:
        save_cmd += ["--env", env]
    print(" ".join(save_cmd), flush=True)
    try:
        with save_log.open("w") as log:
            proc = subprocess.run(
                save_cmd,
                cwd=REPO,
                env=os.environ.copy(),
                stdout=log,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=None if args.timeout == 0 else args.timeout,
            )
        if proc.returncode != 0:
            raise CheckpointError(
                f"checkpoint save exited {proc.returncode}: {save_log}"
            )
        marker_count = parse_marker_count(
            save_log, CHECKPOINT_SAVE_MARKER
        )
        if marker_count != 1:
            raise CheckpointError(
                f"checkpoint save marker count is {marker_count}, expected 1"
            )
        if not (temporary / "m5.cpt").is_file():
            raise CheckpointError(
                f"missing checkpoint payload: {temporary / 'm5.cpt'}"
            )
        write_manifest(temporary, identity)
        os.replace(temporary, checkpoint_dir)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    validate_reuse(checkpoint_dir, identity)
    return checkpoint_dir, manifest_path, identity, checkpoint_id


def append_cache_args(cmd, args):
    if args.disable_hw_prefetchers:
        cmd.append("--disable-hw-prefetchers")
    add_optional(cmd, "--l1-mshrs", args.l1_mshrs)
    add_optional(cmd, "--l1-tgts-per-mshr", args.l1_tgts_per_mshr)
    add_optional(cmd, "--l2-mshrs", args.l2_mshrs)
    add_optional(cmd, "--l2-tgts-per-mshr", args.l2_tgts_per_mshr)


def run_one_checkpoint(args, benchmark, label, binary_dir, kind):
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

    workload_arguments = checkpoint_workload_arguments(args)
    try:
        (
            checkpoint_dir,
            manifest_path,
            identity,
            checkpoint_id,
        ) = ensure_checkpoint(
            args,
            binary,
            kind,
            run_dir,
            workload_arguments,
        )
    except (CheckpointError, OSError, subprocess.SubprocessError) as error:
        return {
            "benchmark": benchmark,
            "label": label,
            "kind": kind,
            "status": "checkpoint-failed",
            "graph_path": str(args.graph),
            "graph_scale": args.graph_scale,
            "graph_sha256": sha256_file(args.graph),
            "run_dir": str(run_dir),
            "error": str(error),
        }

    cmd = checkpoint_common_command(
        args,
        binary=binary,
        workload_arguments=workload_arguments,
        outdir=run_dir,
        cpu="timing",
        link_delay=args.cxl_link_delay,
    )
    cmd += [
        "--cxl-memory",
        "--continue-after-roi",
        "--require-m5-verification-exit",
        "--checkpoint-restore",
        str(checkpoint_dir),
    ]
    append_cache_args(cmd, args)
    append_kind_args(cmd, args, kind)
    for env in args.env:
        cmd += ["--env", env]

    print(" ".join(cmd), flush=True)
    provenance = {
        "scale": args.graph_scale,
        "iterations": args.iterations,
        "measured_trial": args.measure_trial,
        "fast_forward_cpu": "",
        "roi_cpu": "timing",
        "cores": args.cores,
        "cxl_link_delay": args.cxl_link_delay,
        "all_memory_cxl": True,
        "graph_path": str(args.graph),
        "graph_scale": args.graph_scale,
        "graph_sha256": identity["graph_sha256"],
        "checkpoint_id": checkpoint_id,
        "checkpoint_manifest": str(manifest_path),
        "checkpoint_binary_sha256": identity["binary_sha256"],
    }
    if args.dry_run:
        return {
            "benchmark": benchmark,
            "label": label,
            "kind": kind,
            "status": "dry-run",
            "cpu_switches": 0,
            "checkpoint_restores": 1,
            **provenance,
            "run_dir": str(run_dir),
        }

    try:
        with log_path.open("w") as log:
            proc = subprocess.run(
                cmd,
                cwd=REPO,
                env=os.environ.copy(),
                stdout=log,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=None if args.timeout == 0 else args.timeout,
            )
    except (subprocess.TimeoutExpired, OSError) as error:
        return {
            "benchmark": benchmark,
            "label": label,
            "kind": kind,
            "status": (
                "restore-timeout"
                if isinstance(error, subprocess.TimeoutExpired)
                else "restore-launch-failed"
            ),
            "verification": "missing",
            "cpu_switches": parse_cpu_switches(log_path),
            "checkpoint_restores": parse_marker_count(
                log_path, CHECKPOINT_RESTORE_MARKER
            ),
            **provenance,
            "error": str(error),
            "run_dir": str(run_dir),
        }

    verification = parse_verification(log_path)
    status = "ok" if proc.returncode == 0 else f"exit-{proc.returncode}"
    restore_paths = parse_marker_values(
        log_path, CHECKPOINT_RESTORE_MARKER
    )
    checkpoint_restores = len(restore_paths)
    cpu_switches = parse_cpu_switches(log_path)
    if proc.returncode == 0 and checkpoint_restores != 1:
        status = "checkpoint-restore-marker-invalid"
    elif (
        proc.returncode == 0
        and Path(restore_paths[0]).resolve() != checkpoint_dir.resolve()
    ):
        status = "checkpoint-restore-path-invalid"
    if proc.returncode == 0 and cpu_switches != 0:
        status = "unexpected-cpu-switch"
    if proc.returncode == 0 and args.verify:
        if verification == "fail":
            status = "verification-failed"
        elif verification == "missing":
            status = "verification-missing"

    try:
        stats = parse_stats(run_dir / "stats.txt")
        owned_metrics = extract_owned_metrics(stats, kind)
        cira_evidence = extract_cira_evidence(stats, kind, args.cores)
        diagnostic_metrics = extract_diagnostic_metrics(
            stats, kind, fast_forward=False, num_cores=args.cores
        )
    except StatsError:
        stats = {}
        owned_metrics = {}
        cira_evidence = {}
        diagnostic_metrics = {}
        if status == "ok":
            status = "invalid-stats"

    if (
        kind == "cira"
        and status == "ok"
        and owned_metrics["cira_prefetches"] == 0
        and owned_metrics["cira_indexed_prefetches"] == 0
        and owned_metrics["cira_csr_prefetches"] == 0
        and not env_flag_enabled(args.env, "CIRA_GAPBS_DEVICE_OFFLOAD")
        and not args.allow_zero_cira
    ):
        status = "no-cira-events"

    if kind == "cira" and status == "ok":
        evidence_failure = cira_evidence_failure(cira_evidence, args.cores)
        if evidence_failure != "inactive-cira-core" or not args.allow_zero_cira:
            status = evidence_failure or status

    return {
        "benchmark": benchmark,
        "label": label,
        "kind": kind,
        "status": status,
        "verification": verification,
        "cpu_switches": cpu_switches,
        "checkpoint_restores": checkpoint_restores,
        "sim_ticks": stats.get("simTicks", ""),
        "sim_insts": stats.get("simInsts", ""),
        **provenance,
        **owned_metrics,
        **cira_evidence,
        **diagnostic_metrics,
        "run_dir": str(run_dir),
    }


def run_one(args, benchmark, label, binary_dir, kind):
    if args.graph is not None:
        return run_one_checkpoint(
            args, benchmark, label, binary_dir, kind
        )

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

    append_kind_args(cmd, args, kind)

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
    cira_evidence = extract_cira_evidence(stats, kind, args.cores)
    diagnostic_metrics = extract_diagnostic_metrics(
        stats,
        kind,
        fast_forward=bool(args.fast_forward_cpu),
        num_cores=args.cores,
    )
    real_cxl_metrics = extract_real_cxl_metrics(
        stats, num_cores=args.cores
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

    if kind == "cira" and status == "ok":
        evidence_failure = cira_evidence_failure(cira_evidence, args.cores)
        if evidence_failure != "inactive-cira-core" or not args.allow_zero_cira:
            status = evidence_failure or status

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
        "cores": args.cores,
        "cpu_switches": parse_cpu_switches(log_path),
        "cxl_link_delay": args.cxl_link_delay,
        "all_memory_cxl": True,
        "sim_ticks": stats.get("simTicks", ""),
        "sim_insts": stats.get("simInsts", ""),
        **owned_metrics,
        **cira_evidence,
        **diagnostic_metrics,
        **real_cxl_metrics,
        "run_dir": str(run_dir),
    }


def write_summary(path, rows):
    baseline_ticks = {
        row["benchmark"]: row.get("sim_ticks")
        for row in rows
        if (
            row.get("kind") == "baseline"
            and row.get("status") == "ok"
            and row.get("sim_ticks")
        )
    }
    with path.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=SUMMARY_FIELDS)
        writer.writeheader()
        for row in rows:
            out = {field: row.get(field, "") for field in SUMMARY_FIELDS}
            base = baseline_ticks.get(row["benchmark"])
            ticks = row.get("sim_ticks")
            if row.get("status") == "ok" and base and ticks:
                out["speedup_vs_cxl"] = base / ticks
            writer.writerow(out)


def validate_checkpoint_profile(args):
    profile = get_profile(getattr(args, "profile", "g20-2thread-1us"))
    if args.smoke_test:
        return profile
    require_latency(profile, args.cxl_link_delay)
    expected = {
        "graph_scale": profile.graph_scale,
        "cores": profile.cores,
        "iterations": profile.trials,
        "measure_trial": profile.measured_trial,
    }
    for field, value in expected.items():
        if getattr(args, field) != value:
            raise ProfileError(
                f"{field}={getattr(args, field)!r}, expected {value!r} "
                f"for {profile.name}"
            )
    thread_setting = f"OMP_NUM_THREADS={profile.threads}"
    omp_settings = [
        value for value in args.env if value.startswith("OMP_NUM_THREADS=")
    ]
    if not omp_settings:
        args.env.append(thread_setting)
    elif omp_settings != [thread_setting]:
        raise ProfileError(
            f"OMP_NUM_THREADS must be {profile.threads} for {profile.name}"
        )
    return profile


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
    parser.add_argument("--mem-size", default="4GiB")
    parser.add_argument(
        "--graph",
        type=Path,
        help="Serialized GAPBS graph loaded with -f in checkpoint mode.",
    )
    parser.add_argument("--graph-scale", type=int)
    parser.add_argument(
        "--profile",
        choices=tuple(EXPERIMENT_PROFILES),
        default="g20-2thread-1us",
    )
    parser.add_argument(
        "--checkpoint-root",
        type=Path,
        help="Content-addressed checkpoint storage root.",
    )
    parser.add_argument(
        "--reuse-checkpoints",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    parser.add_argument(
        "--smoke-test",
        action="store_true",
        help="Allow a non-scale-20 graph for validation-only runs.",
    )
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
    parser.add_argument("--cira-max-csr-walk-queue", type=int, default=4096)
    parser.add_argument("--cira-max-csr-index-reads", type=int, default=1024)
    parser.add_argument("--cira-csr-lines-per-turn", type=int, default=64)
    parser.add_argument("--cira-max-completed-lines", type=int, default=65536)
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
    if args.graph is not None:
        args.graph = args.graph.resolve()
        if not args.graph.is_file():
            parser.error(f"--graph does not exist: {args.graph}")
        if args.checkpoint_root is None:
            parser.error("--graph requires --checkpoint-root")
        args.checkpoint_root = args.checkpoint_root.resolve()
        if args.fast_forward_cpu:
            parser.error("checkpoint mode rejects --fast-forward-cpu")
        if not args.roi_work_events:
            parser.error("checkpoint mode requires --roi-work-events")
        if not args.verify:
            parser.error("checkpoint mode requires --verify")
        if args.cpu != "timing":
            parser.error("checkpoint mode requires --cpu timing")
        if (args.iterations, args.measure_trial) != (2, 1):
            parser.error(
                "checkpoint mode requires --iterations 2 "
                "and --measure-trial 1"
            )
        if args.graph_scale is None:
            args.graph_scale = args.scale
        try:
            validate_checkpoint_profile(args)
        except ProfileError as error:
            parser.error(str(error))
        args.scale = args.graph_scale
    elif args.checkpoint_root is not None:
        parser.error("--checkpoint-root requires --graph")
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
