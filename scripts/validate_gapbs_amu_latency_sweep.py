#!/usr/bin/env python3
# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

"""Validate canonical scale-20 GAPBS CXL/AMU/CIRA evidence."""

import argparse
import configparser
import csv
import json
import os
import re
import shlex
import sys
import tempfile
from collections import namedtuple
from decimal import Decimal, InvalidOperation
from io import StringIO
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import compare_gapbs_cxl_amu_cira as gapbs_runner  # noqa: E402
from gapbs_checkpoint import (  # noqa: E402
    CheckpointError,
    identity_key,
    load_manifest,
    sha256_file,
)


EXPECTED_LATENCIES = {
    "200ns": 200_000,
    "500ns": 500_000,
    "1us": 1_000_000,
    "2us": 2_000_000,
}
EXPECTED_BENCHMARKS = ("bfs", "bc", "pr", "sssp")
EXPECTED_LABEL_KINDS = (
    ("cxl_vanilla", "baseline"),
    ("amu", "amu"),
    ("cira_pgo", "cira"),
)
CXL_PACKET_STAT_PREFIX = "board.cache_hierarchy.membus.pktCount_"
CXL_BYTE_STAT_PREFIX = "board.cache_hierarchy.membus.pktSize_"
CXL_STAT_SUFFIX = "::board.cxl_mem_link0.cpu_side_port"
CPU_SWITCH_MARKER = "Switching from fast-forward CPU to timing CPU!"
CHECKPOINT_RESTORE_MARKER = "GAPBS_CHECKPOINT_RESTORED path="
ROI_DUMP_MARKER = "Dump stats at the end of the measured ROI!"
STRICT_VERIFICATION_MARKER = (
    "GAPBS_VERIFICATION_EXIT_CAUSE "
    "cause=m5_exit instruction encountered"
)
EXPECTED_GRAPH_SHA256 = (
    "ce900a7147a073835a7450e8f1afedf9f13db6833652bf2f9647819be26bedb3"
)
EXPECTED_GRAPH_SIZE = 133_986_161
CACHE_SUMMARY_STATS = {
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
SWITCH_CACHE_REQUESTORS = (
    "processor.switch.core.data",
    "processor.switch.core.inst",
)
AMU_COUNTER_STATS = {
    "asmc_loads": "board.asmc.issuedLoads",
    "asmc_completed": "board.asmc.completedLoads",
}
CIRA_COUNTER_STATS = {
    "cira_prefetches": "board.cira.issuedPrefetches",
    "cira_completed": "board.cira.completedPrefetches",
    "cira_indexed_prefetches": "board.cira.issuedIndexedPrefetches",
    "cira_csr_prefetches": "board.cira.issuedCsrPrefetches",
    "cira_useful": "board.cira.usefulPrefetches",
    "cira_late": "board.cira.latePrefetches",
    "cira_read_packets": "board.cira.readPackets",
    "cira_read_bytes": "board.cira.readBytes",
    "cira_csr_index_read_packets": "board.cira.csrIndexReadPackets",
    "cira_csr_index_read_bytes": "board.cira.csrIndexReadBytes",
    "cira_completed_csr_index_reads": "board.cira.completedCsrIndexReads",
    "cira_rejected_csr_index_queue_full": (
        "board.cira.rejectedCsrIndexQueueFull"
    ),
    "cira_timing_csr_traversal": "board.cira.timingCsrTraversalEnabled",
}
CIRA_LATENCY_SUMMARY_STATS = {
    "cira_total_latency": "board.cira.totalLatency",
    "cira_avg_latency": "board.cira.avgLatency",
}
METADATA_EXPECTED = {
    "scale": "20",
    "iterations": "2",
    "measured_trial": "1",
    "fast_forward_cpu": "",
    "roi_cpu": "timing",
    "cpu_switches": "0",
    "all_memory_cxl": "true",
    "graph_scale": "20",
    "checkpoint_restores": "1",
}
REQUIRED_SUMMARY_FIELDS = {
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
    *AMU_COUNTER_STATS,
    *CIRA_COUNTER_STATS,
    "cxl_packets",
    "cxl_bytes",
    *CACHE_SUMMARY_STATS,
    *CIRA_LATENCY_SUMMARY_STATS,
    "run_dir",
}
ValidationResult = namedtuple(
    "ValidationResult", "row_count amu_rows cira_rows rows mode"
)


class ValidationError(RuntimeError):
    pass


_HASH_CACHE = {}


def verified_sha256(path):
    path = Path(path).resolve()
    stat = path.stat()
    key = (str(path), stat.st_size, stat.st_mtime_ns)
    if key not in _HASH_CACHE:
        _HASH_CACHE[key] = sha256_file(path)
    return _HASH_CACHE[key]


def parse_first_stats_section(path):
    sections = []
    stats = None
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("---------- Begin Simulation Statistics"):
            if stats is not None:
                raise ValidationError(
                    f"{path}: nested simulation statistics section"
                )
            stats = {}
            continue
        if stats is not None and line.startswith(
            "---------- End Simulation Statistics"
        ):
            sections.append(stats)
            stats = None
            continue
        if stats is None:
            continue
        fields = line.split()
        if len(fields) < 2:
            continue
        try:
            stats[fields[0]] = Decimal(fields[1])
        except InvalidOperation:
            continue
    if stats is not None:
        raise ValidationError(
            f"{path}: missing End marker for simulation statistics section"
        )
    if not sections:
        raise ValidationError(f"{path}: missing simulation statistics section")
    if len(sections) not in (1, 2):
        raise ValidationError(
            f"{path}: expected one ROI section and at most one final section; "
            f"found {len(sections)} complete sections"
        )
    if len(sections) == 2:
        roi_ticks = sections[0].get("simTicks")
        final_ticks = sections[1].get("simTicks")
        if (
            roi_ticks is None
            or final_ticks is None
            or final_ticks < roi_ticks
        ):
            raise ValidationError(
                f"{path}: final statistics section does not follow ROI"
            )
    return sections[0]


def require_decimal(mapping, name, context, *, integer=False, positive=False):
    try:
        raw = mapping[name]
    except KeyError as error:
        raise ValidationError(f"{context}: missing {name}") from error
    try:
        value = Decimal(raw)
    except InvalidOperation as error:
        raise ValidationError(
            f"{context}: invalid {name}={mapping.get(name)!r}"
        ) from error
    if not value.is_finite() or value < 0:
        raise ValidationError(
            f"{context}: {name} is not finite and nonnegative: {value}"
        )
    if integer and value != value.to_integral_value():
        raise ValidationError(f"{context}: {name} is not an integer: {value}")
    if positive and value <= 0:
        raise ValidationError(f"{context}: {name} must be positive: {value}")
    return value


def require_counter(stats, name, path):
    return int(require_decimal(stats, name, path, integer=True))


def require_summary_counter(row, name, context):
    return int(require_decimal(row, name, context, integer=True))


def require_summary_number(row, name, context):
    return require_decimal(row, name, context)


def require_directional_counter(stats, prefix, path):
    candidates = [
        (name, value)
        for name, value in stats.items()
        if name.startswith(prefix) and name.endswith(CXL_STAT_SUFFIX)
    ]
    if len(candidates) != 1:
        raise ValidationError(
            f"{path}: expected exactly one first-ROI statistic matching "
            f"{prefix}*{CXL_STAT_SUFFIX}; found {len(candidates)}"
        )
    name, _ = candidates[0]
    cell = name[len(prefix) : -len(CXL_STAT_SUFFIX)]
    return cell, require_counter(stats, name, path)


def require_directional_pair(stats, path):
    packet_cell, packets = require_directional_counter(
        stats, CXL_PACKET_STAT_PREFIX, path
    )
    byte_cell, byte_count = require_directional_counter(
        stats, CXL_BYTE_STAT_PREFIX, path
    )
    if packet_cell != byte_cell:
        raise ValidationError(
            f"{path}: CXL packet/byte directional identity mismatch: "
            f"{packet_cell!r} != {byte_cell!r}"
        )
    return packets, byte_count


def require_cache_demand_vector(stats, cache_root, family, field, path):
    prefix = f"{cache_root}.{family}"
    total_name = f"{prefix}::total"
    cell_names = {
        name.split("::", 1)[1]: name
        for name in stats
        if name.startswith(f"{prefix}::") and name != total_name
    }
    unknown = sorted(
        requestor
        for requestor in cell_names
        if requestor not in SWITCH_CACHE_REQUESTORS
    )
    if unknown:
        if any(
            requestor.startswith(f"{allowed}.")
            for requestor in unknown
            for allowed in SWITCH_CACHE_REQUESTORS
        ):
            raise ValidationError(
                f"{path}: ambiguous timing switch requestor for {field}: "
                + ", ".join(unknown)
            )
        raise ValidationError(
            f"{path}: unknown cache requestor for {field}: "
            + ", ".join(unknown)
        )
    cells = {
        requestor: require_counter(stats, name, path)
        for requestor, name in cell_names.items()
    }
    total = (
        require_counter(stats, total_name, path)
        if total_name in stats
        else None
    )
    if total is None and cells:
        raise ValidationError(f"{path}: missing {total_name}")
    if total is not None:
        cell_sum = sum(cells.values())
        if cell_sum != total:
            raise ValidationError(
                f"{path}: {family} family total {total} does not match "
                f"requestor cells {cell_sum}"
            )
    return {"family": family, "cells": cells, "total": total}


def resolve_demand_identity(accesses, hits, misses, context):
    if accesses is None:
        raise ValidationError(
            f"{context}: missing exact switch requestor identity or "
            "demandAccesses proof"
        )
    if hits is None and misses is None:
        if accesses != 0:
            raise ValidationError(
                f"{context}: omitted nonzero demandHits/demandMisses; "
                f"demandAccesses={accesses}"
            )
        hits = misses = 0
    elif hits is None:
        inferred = accesses - misses
        if inferred < 0:
            raise ValidationError(
                f"{context}: demandAccesses identity mismatch: "
                f"{accesses} < demandMisses {misses}"
            )
        if inferred > 0:
            raise ValidationError(
                f"{context}: omitted nonzero demandHits={inferred}"
            )
        hits = 0
    elif misses is None:
        inferred = accesses - hits
        if inferred < 0:
            raise ValidationError(
                f"{context}: demandAccesses identity mismatch: "
                f"{accesses} < demandHits {hits}"
            )
        if inferred > 0:
            raise ValidationError(
                f"{context}: omitted nonzero demandMisses={inferred}"
            )
        misses = 0
    if accesses != hits + misses:
        raise ValidationError(
            f"{context}: demandAccesses identity mismatch: "
            f"{accesses} != {hits} + {misses}"
        )
    return accesses, hits, misses


def require_cache_counter(stats, field, path):
    requestor_stat = CACHE_SUMMARY_STATS[field]
    family_prefix, expected_requestor = requestor_stat.split("::", 1)
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
        raise ValidationError(
            f"{path}: legacy cache requestor for {field}: "
            + ", ".join(sorted(legacy))
        )
    vectors = {
        family: require_cache_demand_vector(
            stats, cache_root, family, field, path
        )
        for family in ("demandAccesses", "demandHits", "demandMisses")
    }
    resolved = {}
    resolved["total"] = resolve_demand_identity(
        vectors["demandAccesses"]["total"],
        vectors["demandHits"]["total"],
        vectors["demandMisses"]["total"],
        f"{path}: {cache_root} totals",
    )
    for requestor in SWITCH_CACHE_REQUESTORS:
        values = [
            vector["cells"].get(
                requestor, 0 if vector["total"] is not None else None
            )
            for vector in (
                vectors["demandAccesses"],
                vectors["demandHits"],
                vectors["demandMisses"],
            )
        ]
        resolved[requestor] = resolve_demand_identity(
            *values, f"{path}: {cache_root}::{requestor}"
        )
    family_index = {
        "demandAccesses": 0,
        "demandHits": 1,
        "demandMisses": 2,
    }
    for family, index in family_index.items():
        cell_sum = sum(
            resolved[requestor][index]
            for requestor in SWITCH_CACHE_REQUESTORS
        )
        if cell_sum != resolved["total"][index]:
            raise ValidationError(
                f"{path}: {family} requestor/total identity mismatch for "
                f"{cache_root}: {cell_sum} != {resolved['total'][index]}"
            )
    return resolved[expected_requestor][family_index[target_family]]


def parse_config(path):
    parser = configparser.ConfigParser(
        interpolation=None, strict=True, delimiters=("=",)
    )
    parser.optionxform = str
    try:
        with path.open(encoding="utf-8") as stream:
            parser.read_file(stream)
    except configparser.Error as error:
        raise ValidationError(f"{path}: invalid config.ini: {error}") from error
    return parser


def config_value(config, section, key, path):
    try:
        return config[section][key].strip()
    except KeyError as error:
        raise ValidationError(f"{path}: missing [{section}] {key}") from error


def parse_binary_quantity(value, units, description, path):
    match = re.fullmatch(r"(\d+)([A-Za-z]+)", str(value))
    if match is None or match.group(2) not in units:
        raise ValidationError(
            f"{path}: invalid {description} quantity {value!r}"
        )
    return int(match.group(1)) * units[match.group(2)]


def validate_parameter(
    config, section, key, expected, description, path
):
    actual = config_value(config, section, key, path)
    if actual != str(expected):
        raise ValidationError(
            f"{path}: {description} {key}={actual}, expected {expected}"
        )


def validate_accelerator_config(config, path, kind, model_parameters):
    has_asmc = config.has_section("board.asmc")
    has_cira = config.has_section("board.cira")
    if kind == "baseline":
        if has_asmc:
            raise ValidationError(f"{path}: baseline must not contain ASMC")
        if has_cira:
            raise ValidationError(f"{path}: baseline must not contain CIRA")
        return
    if kind == "amu":
        if not has_asmc:
            raise ValidationError(f"{path}: AMU config is missing ASMC")
        if has_cira:
            raise ValidationError(f"{path}: AMU config must not contain CIRA")
        if not config.has_section("board.asmc_io_cache"):
            raise ValidationError(
                f"{path}: AMU config is missing coherent ASMC I/O cache"
            )
        cache_ranges = config_value(
            config, "board.asmc_io_cache", "addr_ranges", path
        ).split()
        if not cache_ranges:
            raise ValidationError(
                f"{path}: ASMC I/O cache addr_ranges must not be empty"
            )
        port = config_value(config, "board.asmc", "mem_side_port", path)
        if port != "board.asmc_io_cache.cpu_side":
            raise ValidationError(
                f"{path}: invalid ASMC mem_side_port binding: {port}"
            )
        cache_cpu_port = config_value(
            config, "board.asmc_io_cache", "cpu_side", path
        )
        if cache_cpu_port != "board.asmc.mem_side_port":
            raise ValidationError(
                f"{path}: ASMC coherent I/O cache CPU-side binding is missing"
            )
        cache_mem_port = config_value(
            config, "board.asmc_io_cache", "mem_side", path
        )
        binding = re.fullmatch(
            r"board\.cache_hierarchy\.membus\.cpu_side_ports\[(\d+)\]",
            cache_mem_port,
        )
        if binding is None:
            raise ValidationError(
                f"{path}: invalid ASMC I/O cache mem_side binding: "
                f"{cache_mem_port}"
            )
        cpu_ports = config_value(
            config,
            "board.cache_hierarchy.membus",
            "cpu_side_ports",
            path,
        ).split()
        index = int(binding.group(1))
        if (
            index >= len(cpu_ports)
            or cpu_ports[index] != "board.asmc_io_cache.mem_side"
        ):
            raise ValidationError(
                f"{path}: ASMC I/O cache reciprocal membus binding is missing"
            )
        size = parse_binary_quantity(
            model_parameters.get("asmc_spm_size"),
            {"B": 1, "KiB": 1024, "MiB": 1024**2},
            "ASMC SPM",
            path,
        )
        times = {
            "ps": 1,
            "ns": 1_000,
            "us": 1_000_000,
            "ms": 1_000_000_000,
            "s": 1_000_000_000_000,
        }
        expected = {
            "spm_size": size,
            "default_granularity": model_parameters.get(
                "asmc_granularity"
            ),
            "max_outstanding": model_parameters.get(
                "asmc_max_outstanding"
            ),
            "max_send_queue": model_parameters.get(
                "asmc_max_send_queue"
            ),
            "issue_latency": parse_binary_quantity(
                model_parameters.get("asmc_issue_latency"),
                times,
                "ASMC issue latency",
                path,
            ),
            "completion_latency": parse_binary_quantity(
                model_parameters.get("asmc_completion_latency"),
                times,
                "ASMC completion latency",
                path,
            ),
            "asmc_latency": parse_binary_quantity(
                model_parameters.get("asmc_latency"),
                times,
                "ASMC latency",
                path,
            ),
        }
        for key, value in expected.items():
            validate_parameter(
                config, "board.asmc", key, value, "ASMC", path
            )
        return
    if kind != "cira":
        raise ValidationError(f"{path}: unsupported kind {kind!r}")
    if has_asmc:
        raise ValidationError(f"{path}: CIRA config must not contain ASMC")
    if not has_cira:
        raise ValidationError(f"{path}: CIRA config is missing CIRA")
    times = {
        "ps": 1,
        "ns": 1_000,
        "us": 1_000_000,
        "ms": 1_000_000_000,
        "s": 1_000_000_000_000,
    }
    expected = {
        "max_outstanding": model_parameters.get("cira_max_outstanding"),
        "max_send_queue": model_parameters.get("cira_max_send_queue"),
        "issue_latency": parse_binary_quantity(
            model_parameters.get("cira_issue_latency"),
            times,
            "CIRA issue latency",
            path,
        ),
        "completion_latency": parse_binary_quantity(
            model_parameters.get("cira_completion_latency"),
            times,
            "CIRA completion latency",
            path,
        ),
    }
    for key, value in expected.items():
        validate_parameter(config, "board.cira", key, value, "CIRA", path)


def expected_workload_environment(kind, model_parameters, path):
    encoded = model_parameters.get("env")
    if not isinstance(encoded, str):
        raise ValidationError(
            f"{path}: checkpoint identity has invalid environment"
        )
    user_environment = [] if encoded == "" else encoded.split("\0")
    forbidden = {
        "OMP_NUM_THREADS",
        "OMP_THREAD_LIMIT",
        "OMP_DYNAMIC",
        "CIRA_GAPBS_DEVICE_OFFLOAD",
    }
    for entry in user_environment:
        key = entry.split("=", 1)[0]
        if key in forbidden:
            raise ValidationError(
                f"{path}: publication environment forbids {key}"
            )
    expected = ["OMP_NUM_THREADS=2"]
    if kind == "cira" and not any(
        entry.split("=", 1)[0]
        in ("CIRA_GEM5_M5OPS", "CIRA_GAPBS_GEM5_M5OPS")
        for entry in user_environment
    ):
        expected.append("CIRA_GEM5_M5OPS=1")
    expected.extend(user_environment)
    return expected


def validate_config(
    path, expected_delay, kind, expected_graph, expected_binary,
    model_parameters
):
    config = parse_config(path)
    board_range = config_value(config, "board", "mem_ranges", path)
    link_range = config_value(
        config, "board.cxl_mem_link0", "ranges", path
    )
    dram_range = config_value(
        config, "board.memory.mem_ctrl.dram", "range", path
    )
    if not board_range or board_range != link_range or board_range != dram_range:
        raise ValidationError(
            f"{path}: range mismatch: board={board_range!r}, "
            f"CXL={link_range!r}, DRAM={dram_range!r}"
        )
    delay = config_value(config, "board.cxl_mem_link0", "delay", path)
    if delay != str(expected_delay):
        raise ValidationError(
            f"{path}: delay={delay}, expected {expected_delay}"
        )
    link_type = config_value(
        config, "board.cxl_mem_link0", "type", path
    )
    if link_type != "SerialLink":
        raise ValidationError(
            f"{path}: CXL link type={link_type!r}, expected SerialLink"
        )
    link_cpu_port = config_value(
        config, "board.cxl_mem_link0", "cpu_side_port", path
    )
    cpu_binding = re.fullmatch(
        r"board\.cache_hierarchy\.membus\.mem_side_ports\[(\d+)\]",
        link_cpu_port,
    )
    if cpu_binding is None:
        raise ValidationError(
            f"{path}: invalid CXL cpu_side_port binding: {link_cpu_port}"
        )
    link_mem_port = config_value(
        config, "board.cxl_mem_link0", "mem_side_port", path
    )
    device_cpu_binding = re.fullmatch(
        r"board\.cxl_device_xbar0\.cpu_side_ports\[(\d+)\]",
        link_mem_port,
    )
    if device_cpu_binding is None:
        raise ValidationError(
            f"{path}: invalid CXL mem_side_port binding: {link_mem_port}"
        )
    device_type = config_value(
        config, "board.cxl_device_xbar0", "type", path
    )
    if device_type != "NoncoherentXBar":
        raise ValidationError(
            f"{path}: CXL device xbar type={device_type!r}, "
            "expected NoncoherentXBar"
        )
    device_cpu_ports = config_value(
        config, "board.cxl_device_xbar0", "cpu_side_ports", path
    ).split()
    link_device_index = int(device_cpu_binding.group(1))
    if (
        link_device_index >= len(device_cpu_ports)
        or device_cpu_ports[link_device_index]
        != "board.cxl_mem_link0.mem_side_port"
    ):
        raise ValidationError(
            f"{path}: CXL link is not attached to the device xbar"
        )
    controller_port = config_value(
        config, "board.memory.mem_ctrl", "port", path
    )
    if controller_port != "board.cxl_device_xbar0.mem_side_ports[0]":
        raise ValidationError(
            f"{path}: memory controller port bypasses CXL: "
            f"{controller_port}"
        )
    device_mem_ports = config_value(
        config, "board.cxl_device_xbar0", "mem_side_ports", path
    ).split()
    if device_mem_ports != ["board.memory.mem_ctrl.port"]:
        raise ValidationError(
            f"{path}: device xbar does not terminate at the controller"
        )
    if kind == "cira":
        if not config.has_section("board.cira"):
            raise ValidationError(f"{path}: missing CIRA configuration")
        csr_port = config_value(
            config, "board.cira", "csr_mem_side_port", path
        )
        csr_binding = re.fullmatch(
            r"board\.cxl_device_xbar0\.cpu_side_ports\[(\d+)\]",
            csr_port,
        )
        if csr_binding is None:
            raise ValidationError(
                f"{path}: CIRA CSR walker is not device-side of CXL"
            )
        csr_index = int(csr_binding.group(1))
        if (
            csr_index >= len(device_cpu_ports)
            or device_cpu_ports[csr_index]
            != "board.cira.csr_mem_side_port"
        ):
            raise ValidationError(
                f"{path}: CIRA CSR port binding does not match device xbar"
            )
        if config_value(
            config, "board.cira", "timing_csr_traversal", path
        ) != "true":
            raise ValidationError(
                f"{path}: CIRA timing CSR traversal is not enabled"
            )
    membus_ports = config_value(
        config, "board.cache_hierarchy.membus", "mem_side_ports", path
    ).split()
    cpu_index = int(cpu_binding.group(1))
    if any(
        "memory.mem_ctrl" in port or port == controller_port
        for port in membus_ports
    ):
        raise ValidationError(
            f"{path}: direct memory controller port on membus"
        )
    if (
        cpu_index >= len(membus_ports)
        or membus_ports[cpu_index]
        != "board.cxl_mem_link0.cpu_side_port"
    ):
        raise ValidationError(
            f"{path}: CXL cpu_side_port binding does not match "
            f"membus.mem_side_ports[{cpu_index}]"
        )
    forbidden = sorted(
        section
        for section in config.sections()
        if section.startswith("board.processor.start")
        or section.startswith("board.processor.switch")
    )
    if forbidden:
        raise ValidationError(
            f"{path}: checkpoint restore config contains start/switch CPUs: "
            + ", ".join(forbidden)
        )
    core_sections = sorted(
        section
        for section in config.sections()
        if re.fullmatch(r"board\.processor\.cores\d+\.core", section)
    )
    if core_sections != [
        "board.processor.cores0.core",
        "board.processor.cores1.core",
    ]:
        raise ValidationError(
            f"{path}: expected exact two-core processor sections; "
            f"found {core_sections}"
        )
    for section in core_sections:
        cpu_type = config_value(config, section, "type", path)
        if cpu_type != "BaseTimingSimpleCPU":
            raise ValidationError(
                f"{path}: [{section}] type={cpu_type}, expected Timing"
            )
    workload_sections = [
        section for section in config.values() if "cmd" in section
    ]
    if not workload_sections:
        raise ValidationError(f"{path}: missing config workload command")
    if len(workload_sections) != 1:
        raise ValidationError(
            f"{path}: expected one workload command; "
            f"found {len(workload_sections)}"
        )
    for workload in workload_sections:
        command = workload["cmd"]
        arguments = shlex.split(command)
        if not arguments:
            raise ValidationError(f"{path}: empty config workload command")
        if Path(arguments[0]).resolve() != Path(expected_binary).resolve():
            raise ValidationError(
                f"{path}: workload binary {arguments[0]!r} does not match "
                f"checkpoint binary {str(expected_binary)!r}"
            )
        expected_arguments = [
            str(Path(expected_binary).resolve()),
            "-f",
            str(Path(expected_graph).resolve()),
            "-n",
            "2",
            "-v",
        ]
        if arguments != expected_arguments:
            raise ValidationError(
                f"{path}: config workload must use exact argv "
                f"{expected_arguments!r}; found {arguments!r}"
            )
        environment = shlex.split(workload.get("env", ""))
        expected_environment = expected_workload_environment(
            kind, model_parameters, path
        )
        if environment != expected_environment:
            raise ValidationError(
                f"{path}: expected exact workload environment "
                f"{expected_environment!r}; found {environment!r}"
            )
    validate_accelerator_config(
        config, path, kind, model_parameters
    )
    if kind == "cira":
        target = config_value(
            config, "board.cira", "demand_probe_target", path
        )
        if target != "board.cache_hierarchy.l2-cache-0":
            raise ValidationError(
                f"{path}: demand_probe_target={target!r}, "
                "expected first private L2"
            )
        cira_port = config_value(
            config, "board.cira", "mem_side_port", path
        )
        cira_binding = re.fullmatch(
            r"board\.cache_hierarchy\.l2buses0\.cpu_side_ports\[(\d+)\]",
            cira_port,
        )
        if cira_binding is None:
            raise ValidationError(
                f"{path}: CIRA mem_side_port={cira_port!r} "
                "must bind an l2buses.cpu_side_ports endpoint"
            )
        l2_cpu_ports = config_value(
            config,
            "board.cache_hierarchy.l2buses0",
            "cpu_side_ports",
            path,
        ).split()
        cira_index = int(cira_binding.group(1))
        if (
            cira_index >= len(l2_cpu_ports)
            or l2_cpu_ports[cira_index] != "board.cira.mem_side_port"
        ):
            raise ValidationError(
                f"{path}: CIRA endpoint binding does not match "
                f"l2buses.cpu_side_ports[{cira_index}]"
            )
    return board_range


def parse_log(path):
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    switches = sum(line == CPU_SWITCH_MARKER for line in lines)
    restore_paths = [
        line[len(CHECKPOINT_RESTORE_MARKER) :]
        for line in lines
        if line.startswith(CHECKPOINT_RESTORE_MARKER)
    ]
    roi_dumps = sum(line == ROI_DUMP_MARKER for line in lines)
    strict_exits = sum(
        line == STRICT_VERIFICATION_MARKER for line in lines
    )
    verification = "missing"
    for line in lines:
        if "Verification: FAIL" in line:
            verification = "fail"
            break
        if "Verification: PASS" in line:
            verification = "pass"
    return switches, restore_paths, roi_dumps, strict_exits, verification


def zero_or_blank(row, field, context):
    if row[field] == "":
        return
    if require_summary_number(row, field, context) != 0:
        raise ValidationError(
            f"{context}: non-owner {field} must be blank or zero"
        )


def validate_metadata(row, latency, context):
    for field, expected in METADATA_EXPECTED.items():
        actual = row[field].strip().lower()
        if actual != expected:
            raise ValidationError(
                f"{context}: {field}={row[field]!r}, expected {expected!r}"
            )
    if row["cxl_link_delay"] != latency:
        raise ValidationError(
            f"{context}: cxl_link_delay={row['cxl_link_delay']!r}, "
            f"expected {latency!r}"
        )


def validate_checkpoint_manifest(row, context):
    graph = Path(row["graph_path"])
    if not graph.is_file():
        raise ValidationError(f"{context}: missing graph file: {graph}")
    graph = graph.resolve()
    graph_sha = verified_sha256(graph)
    if row["graph_sha256"] != graph_sha:
        raise ValidationError(
            f"{context}: graph_sha256={row['graph_sha256']} != {graph_sha}"
        )
    if graph_sha != EXPECTED_GRAPH_SHA256:
        raise ValidationError(
            f"{context}: graph hash is not canonical g20: {graph_sha}"
        )
    if graph.stat().st_size != EXPECTED_GRAPH_SIZE:
        raise ValidationError(
            f"{context}: graph size={graph.stat().st_size}, "
            f"expected {EXPECTED_GRAPH_SIZE}"
        )

    manifest_path = Path(row["checkpoint_manifest"])
    if manifest_path.name != "manifest.json" or not manifest_path.is_file():
        raise ValidationError(
            f"{context}: missing checkpoint manifest: {manifest_path}"
        )
    checkpoint_root = manifest_path.parent
    if not (checkpoint_root / "m5.cpt").is_file():
        raise ValidationError(
            f"{context}: missing checkpoint payload: "
            f"{checkpoint_root / 'm5.cpt'}"
        )
    try:
        manifest = load_manifest(checkpoint_root)
    except CheckpointError as error:
        raise ValidationError(f"{context}: {error}") from error
    identity = manifest.get("identity")
    if not isinstance(identity, dict):
        raise ValidationError(f"{context}: invalid checkpoint identity")
    expected_id = identity_key(identity)
    if manifest.get("checkpoint_id") != expected_id:
        raise ValidationError(f"{context}: manifest checkpoint id mismatch")
    if row["checkpoint_id"] != expected_id:
        raise ValidationError(f"{context}: summary checkpoint id mismatch")
    if checkpoint_root.name != expected_id:
        raise ValidationError(
            f"{context}: checkpoint directory name "
            f"{checkpoint_root.name!r} != {expected_id!r}"
        )

    expected_arguments = ["-f", str(graph), "-n", "2", "-v"]
    exact = {
        "schema": 2,
        "kind": row["kind"],
        "graph_path": str(graph),
        "graph_sha256": graph_sha,
        "graph_scale": 20,
        "arguments": expected_arguments,
        "cores": 2,
    }
    for field, expected in exact.items():
        if identity.get(field) != expected:
            raise ValidationError(
                f"{context}: checkpoint identity {field}="
                f"{identity.get(field)!r}, expected {expected!r}"
            )
    binary = Path(identity.get("binary_path", ""))
    if not binary.is_file():
        raise ValidationError(
            f"{context}: missing checkpoint binary: {binary}"
        )
    binary_sha = verified_sha256(binary)
    if identity.get("binary_sha256") != binary_sha:
        raise ValidationError(
            f"{context}: checkpoint binary hash mismatch"
        )
    if row["checkpoint_binary_sha256"] != binary_sha:
        raise ValidationError(
            f"{context}: summary checkpoint binary hash mismatch"
        )
    for prefix in ("gem5", "config"):
        source = Path(identity.get(f"{prefix}_path", ""))
        if not source.is_file():
            raise ValidationError(
                f"{context}: missing checkpoint {prefix}: {source}"
            )
        if identity.get(f"{prefix}_sha256") != verified_sha256(source):
            raise ValidationError(
                f"{context}: checkpoint {prefix} hash mismatch"
            )
    config_source = Path(identity["config_path"]).resolve()
    expected_dependency = (
        config_source.parent / "gapbs_roi_state.py"
    ).resolve()
    dependencies = identity.get("config_dependencies")
    if not isinstance(dependencies, dict):
        raise ValidationError(
            f"{context}: invalid checkpoint config dependencies"
        )
    expected_dependencies = {
        str(expected_dependency): (
            verified_sha256(expected_dependency)
            if expected_dependency.is_file()
            else None
        )
    }
    if dependencies != expected_dependencies:
        raise ValidationError(
            f"{context}: checkpoint config dependency hash mismatch"
        )
    return identity


def validate_row(row, latency, expected_delay, expected_run_dir):
    context = f"{latency}/{row['benchmark']}/{row['label']}"
    if row["status"] != "ok" or row["verification"] != "pass":
        raise ValidationError(
            f"{context}: status={row['status']} "
            f"verification={row['verification']}"
        )
    validate_metadata(row, latency, context)
    run_dir = Path(row["run_dir"])
    if run_dir.resolve() != expected_run_dir.resolve():
        raise ValidationError(
            f"{context}: run_dir={run_dir} does not match {expected_run_dir}"
        )
    identity = validate_checkpoint_manifest(row, context)
    validate_config(
        run_dir / "config.ini",
        expected_delay,
        row["kind"],
        identity["graph_path"],
        identity["binary_path"],
        identity["model_parameters"],
    )
    (
        switches,
        restore_paths,
        roi_dumps,
        strict_exits,
        raw_verification,
    ) = parse_log(run_dir / "gem5.log")
    reported_switches = require_summary_counter(row, "cpu_switches", context)
    if switches != reported_switches:
        raise ValidationError(
            f"{context}: cpu_switches={reported_switches} != exact "
            f"switch marker count {switches}"
        )
    reported_restores = require_summary_counter(
        row, "checkpoint_restores", context
    )
    restores = len(restore_paths)
    if restores != 1 or reported_restores != restores:
        raise ValidationError(
            f"{context}: checkpoint_restores={reported_restores} != exact "
            f"restore marker count {restores}"
        )
    expected_checkpoint = Path(row["checkpoint_manifest"]).parent.resolve()
    if Path(restore_paths[0]).resolve() != expected_checkpoint:
        raise ValidationError(
            f"{context}: restore marker path "
            f"{restore_paths[0]!r} != {str(expected_checkpoint)!r}"
        )
    if roi_dumps != 1:
        raise ValidationError(
            f"{context}: expected exactly one explicit ROI dump marker; "
            f"found {roi_dumps}"
        )
    if raw_verification != row["verification"]:
        raise ValidationError(
            f"{context}: verification={row['verification']} != raw "
            f"{raw_verification}"
        )
    if strict_exits != 1:
        raise ValidationError(
            f"{context}: expected exactly one strict m5_exit marker; "
            f"found {strict_exits}"
        )

    stats_path = run_dir / "stats.txt"
    stats = parse_first_stats_section(stats_path)
    sim_ticks = require_summary_counter(row, "sim_ticks", context)
    if sim_ticks <= 0:
        raise ValidationError(f"{context}: sim_ticks must be positive")
    raw_ticks = require_counter(stats, "simTicks", stats_path)
    if sim_ticks != raw_ticks:
        raise ValidationError(
            f"{context}: sim_ticks={sim_ticks} != first-ROI {raw_ticks}"
        )
    sim_insts = require_summary_counter(row, "sim_insts", context)
    raw_insts = require_counter(stats, "simInsts", stats_path)
    if sim_insts != raw_insts:
        raise ValidationError(
            f"{context}: sim_insts={sim_insts} != first-ROI {raw_insts}"
        )
    speedup = require_summary_number(row, "speedup_vs_cxl", context)
    if speedup <= 0:
        raise ValidationError(f"{context}: speedup_vs_cxl must be positive")

    try:
        diagnostics = gapbs_runner.extract_diagnostic_metrics(
            stats, row["kind"], num_cores=2
        )
    except gapbs_runner.StatsError as error:
        raise ValidationError(f"{stats_path}: {error}") from error
    for field in (
        "cxl_packets",
        "cxl_bytes",
        *CACHE_SUMMARY_STATS,
        *CIRA_LATENCY_SUMMARY_STATS,
    ):
        if (
            row["kind"] != "cira"
            and field in CIRA_LATENCY_SUMMARY_STATS
            and row[field] == ""
        ):
            reported = Decimal(0)
        else:
            reported = (
                require_summary_number(row, field, context)
                if field == "cira_avg_latency"
                else require_summary_counter(row, field, context)
            )
        exact = diagnostics[field]
        if reported != exact:
            raise ValidationError(
                f"{context}: {field}={reported} != exact first-ROI "
                f"value {exact}"
            )

    kind = row["kind"]
    if kind == "amu":
        for field, stat_name in AMU_COUNTER_STATS.items():
            exact = require_counter(stats, stat_name, stats_path)
            reported = require_summary_counter(row, field, context)
            if reported != exact:
                raise ValidationError(
                    f"{context}: {field}={reported} != exact "
                    f"first-ROI value {exact}"
                )
        issued = require_summary_counter(row, "asmc_loads", context)
        completed = require_summary_counter(row, "asmc_completed", context)
        if issued <= 0 or issued != completed:
            raise ValidationError(
                f"{context}: issuedLoads={issued} != completedLoads="
                f"{completed} or counts are not positive"
            )
    else:
        for field in AMU_COUNTER_STATS:
            zero_or_blank(row, field, context)

    if kind == "cira":
        for field, stat_name in CIRA_COUNTER_STATS.items():
            exact = require_counter(stats, stat_name, stats_path)
            reported = require_summary_counter(row, field, context)
            if reported != exact:
                raise ValidationError(
                    f"{context}: {field}={reported} != exact "
                    f"first-ROI value {exact}"
                )
        issued = require_summary_counter(row, "cira_prefetches", context)
        completed = require_summary_counter(row, "cira_completed", context)
        if issued <= 0 or issued != completed:
            raise ValidationError(
                f"{context}: issuedPrefetches={issued} != "
                f"completedPrefetches={completed} or counts are not positive"
            )
        csr = require_summary_counter(row, "cira_csr_prefetches", context)
        if csr <= 0:
            raise ValidationError(
                f"{context}: cira_csr_prefetches={csr}, expected > 0"
            )
        for field in (
            "cira_csr_index_read_packets",
            "cira_csr_index_read_bytes",
            "cira_completed_csr_index_reads",
        ):
            if require_summary_counter(row, field, context) <= 0:
                raise ValidationError(f"{context}: {field} must be positive")
        rejected = require_summary_counter(
            row, "cira_rejected_csr_index_queue_full", context
        )
        if rejected != 0:
            raise ValidationError(
                f"{context}: cira_rejected_csr_index_queue_full={rejected}"
            )
        timing = require_summary_counter(
            row, "cira_timing_csr_traversal", context
        )
        if timing != 1:
            raise ValidationError(
                f"{context}: cira_timing_csr_traversal={timing}, expected 1"
            )
    else:
        for field in (*CIRA_COUNTER_STATS, *CIRA_LATENCY_SUMMARY_STATS):
            zero_or_blank(row, field, context)
    return {
        "latency": latency,
        **{field: row[field] for field in row},
    }


def read_summary(path):
    with path.open(newline="", encoding="utf-8") as stream:
        reader = csv.DictReader(stream)
        fields = list(reader.fieldnames or ())
        duplicates = sorted(
            {field for field in fields if fields.count(field) > 1}
        )
        if duplicates:
            raise ValidationError(
                f"{path}: duplicate columns: {', '.join(duplicates)}"
            )
        missing = sorted(REQUIRED_SUMMARY_FIELDS - set(fields))
        if missing:
            raise ValidationError(
                f"{path}: missing columns: {', '.join(missing)}"
            )
        return fields, list(reader)


def validate_speedups(rows, context):
    indexed = {
        (row["benchmark"], row["label"], row["kind"]): row for row in rows
    }
    for benchmark in sorted({row["benchmark"] for row in rows}):
        baseline = indexed[(benchmark, "cxl_vanilla", "baseline")]
        baseline_ticks = require_summary_counter(
            baseline, "sim_ticks", f"{context}/{benchmark}/cxl_vanilla"
        )
        for label, kind in (
            pair for pair in EXPECTED_LABEL_KINDS
            if (benchmark, *pair) in indexed
        ):
            row = indexed[(benchmark, label, kind)]
            ticks = require_summary_counter(
                row, "sim_ticks", f"{context}/{benchmark}/{label}"
            )
            reported = require_summary_number(
                row, "speedup_vs_cxl", f"{context}/{benchmark}/{label}"
            )
            exact = Decimal(baseline_ticks) / Decimal(ticks)
            if reported != exact:
                raise ValidationError(
                    f"{context}/{benchmark}/{label}: speedup mismatch: "
                    f"reported {reported}, recomputed {exact}"
                )


def validate_sweep(sweep_root):
    sweep_root = Path(sweep_root)
    validated = []
    amu_rows = 0
    cira_rows = 0
    for latency, expected_delay in EXPECTED_LATENCIES.items():
        summary = sweep_root / latency / "summary.csv"
        if not summary.is_file():
            raise ValidationError(f"{summary}: missing summary")
        _, rows = read_summary(summary)
        expected = [
            (benchmark, label, kind)
            for benchmark in EXPECTED_BENCHMARKS
            for label, kind in EXPECTED_LABEL_KINDS
        ]
        observed = [
            (row["benchmark"], row["label"], row["kind"]) for row in rows
        ]
        if len(rows) != 12 or sorted(observed) != sorted(expected):
            raise ValidationError(
                f"{summary}: expected exact cxl_vanilla/baseline, amu/amu, "
                "and cira_pgo/cira rows per workload"
            )
        validate_speedups(rows, latency)
        indexed = {
            (row["benchmark"], row["label"], row["kind"]): row for row in rows
        }
        for benchmark in EXPECTED_BENCHMARKS:
            for label, kind in EXPECTED_LABEL_KINDS:
                row = indexed[(benchmark, label, kind)]
                expected_run_dir = sweep_root / latency / benchmark / label
                validated.append(
                    validate_row(
                        row, latency, expected_delay, expected_run_dir
                    )
                )
                amu_rows += kind == "amu"
                cira_rows += kind == "cira"
    if len(validated) != 48 or amu_rows != 16 or cira_rows != 16:
        raise ValidationError(
            f"expected 48 rows, 16 AMU rows, and 16 CIRA rows; got "
            f"{len(validated)}, {amu_rows}, and {cira_rows}"
        )
    return ValidationResult(
        len(validated), amu_rows, cira_rows, validated, "full"
    )


def validate_pr_gate(root):
    root = Path(root)
    summary = root / "summary.csv"
    _, rows = read_summary(summary)
    expected = [
        ("pr", "cxl_vanilla", "baseline"),
        ("pr", "cira_pgo", "cira"),
    ]
    observed = [
        (row["benchmark"], row["label"], row["kind"]) for row in rows
    ]
    if len(rows) != 2 or sorted(observed) != sorted(expected):
        raise ValidationError(
            f"{summary}: PR gate requires exact baseline and cira_pgo rows"
        )
    validate_speedups(rows, "1us")
    indexed = {
        (row["benchmark"], row["label"], row["kind"]): row for row in rows
    }
    validated = []
    for label, kind in (EXPECTED_LABEL_KINDS[0], EXPECTED_LABEL_KINDS[2]):
        row = indexed[("pr", label, kind)]
        validated.append(
            validate_row(
                row,
                "1us",
                EXPECTED_LATENCIES["1us"],
                root / "pr" / label,
            )
        )
    baseline, cira = rows
    if baseline["kind"] != "baseline":
        baseline, cira = cira, baseline
    baseline_misses = require_summary_counter(
        baseline, "l2d_demand_misses", "PR gate baseline"
    )
    cira_misses = require_summary_counter(
        cira, "l2d_demand_misses", "PR gate CIRA"
    )
    useful = require_summary_counter(cira, "cira_useful", "PR gate CIRA")
    baseline_ticks = require_summary_counter(
        baseline, "sim_ticks", "PR gate baseline"
    )
    cira_ticks = require_summary_counter(cira, "sim_ticks", "PR gate CIRA")
    if baseline_misses <= 4096:
        raise ValidationError(
            "PR gate baseline l2d_demand_misses must exceed 4096"
        )
    if cira_misses >= baseline_misses:
        raise ValidationError(
            "PR gate CIRA l2d_demand_misses must be strictly lower"
        )
    if useful <= 0:
        raise ValidationError("PR gate cira_useful must be positive")
    if Decimal(baseline_ticks) / Decimal(cira_ticks) <= 1:
        raise ValidationError("PR gate baseline_ticks/cira_ticks must exceed 1")
    return ValidationResult(2, 0, 1, validated, "pr-gate")


def render_combined_csv(result):
    fields = []
    for row in result.rows:
        for field in row:
            if field not in fields:
                fields.append(field)
    buffer = StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    writer.writerows(result.rows)
    return buffer.getvalue()


def render_validation_json(result):
    payload = {
        "mode": result.mode,
        "row_count": result.row_count,
        "amu_rows": result.amu_rows,
        "cira_rows": result.cira_rows,
        "rows": result.rows,
    }
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def atomic_write(path, content, newline=None):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.tmp-", dir=path.parent
    )
    try:
        with os.fdopen(
            descriptor, "w", encoding="utf-8", newline=newline
        ) as stream:
            stream.write(content)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def transactional_write(outputs):
    staged = {}
    backups = {}
    installed = set()
    committed = False
    try:
        for path, content, newline in outputs:
            path = Path(path)
            path.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary = tempfile.mkstemp(
                prefix=f".{path.name}.tmp-", dir=path.parent
            )
            staged[path] = Path(temporary)
            with os.fdopen(
                descriptor, "w", encoding="utf-8", newline=newline
            ) as stream:
                stream.write(content)

        for path in staged:
            if not path.exists():
                backups[path] = None
                continue
            descriptor, backup = tempfile.mkstemp(
                prefix=f".{path.name}.tmp-backup-", dir=path.parent
            )
            os.close(descriptor)
            os.unlink(backup)
            os.replace(path, backup)
            backups[path] = Path(backup)

        for path, temporary in staged.items():
            os.replace(temporary, path)
            installed.add(path)
            staged[path] = None
        committed = True
    except BaseException:
        for path, backup in backups.items():
            if backup is not None:
                try:
                    path.unlink(missing_ok=True)
                    os.replace(backup, path)
                    backups[path] = None
                except OSError:
                    pass
            elif path in installed:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
        raise
    finally:
        for temporary in staged.values():
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
        for backup in backups.values():
            if backup is not None and committed:
                try:
                    backup.unlink(missing_ok=True)
                except OSError:
                    pass


def require_distinct_output_paths(combined_output, validation_output):
    combined = Path(combined_output)
    validation = Path(validation_output)
    try:
        combined_resolved = combined.resolve(strict=False)
        validation_resolved = validation.resolve(strict=False)
    except (OSError, RuntimeError) as error:
        raise ValidationError(
            "combined and validation outputs must use distinct paths; "
            f"could not resolve paths safely: {error}"
        ) from error
    equivalent = combined_resolved == validation_resolved
    if not equivalent and combined.exists() and validation.exists():
        try:
            equivalent = os.path.samefile(combined, validation)
        except OSError as error:
            raise ValidationError(
                "combined and validation outputs must use distinct paths; "
                f"could not compare existing paths safely: {error}"
            ) from error
    if equivalent:
        raise ValidationError(
            "combined and validation outputs must use distinct paths: "
            f"{combined} and {validation}"
        )


def write_outputs(result, combined_output=None, validation_output=None):
    if combined_output and validation_output:
        require_distinct_output_paths(
            combined_output, validation_output
        )
    combined = render_combined_csv(result) if combined_output else None
    evidence = (
        render_validation_json(result) if validation_output else None
    )
    if combined_output and validation_output:
        transactional_write(
            (
                (combined_output, combined, ""),
                (validation_output, evidence, None),
            )
        )
    elif combined_output:
        atomic_write(combined_output, combined, newline="")
    elif validation_output:
        atomic_write(validation_output, evidence)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sweep_root", type=Path)
    parser.add_argument("--pr-gate", action="store_true")
    parser.add_argument("--combined-output", type=Path)
    parser.add_argument("--validation-output", type=Path)
    args = parser.parse_args()
    if args.pr_gate and args.combined_output:
        parser.error("--combined-output is only valid for a full sweep")
    try:
        result = (
            validate_pr_gate(args.sweep_root)
            if args.pr_gate
            else validate_sweep(args.sweep_root)
        )
        write_outputs(
            result,
            combined_output=args.combined_output,
            validation_output=args.validation_output,
        )
    except (OSError, KeyError, ValidationError) as error:
        parser.exit(1, f"FAIL: {error}\n")
    if args.pr_gate:
        print("PASS: PR@1us scale-20 CIRA discriminator")
    else:
        print(
            f"PASS: {result.row_count}/48 rows; canonical scale-20 "
            f"two-core checkpoint CXL evidence validated; "
            f"{result.amu_rows} AMU balances; "
            f"{result.cira_rows} CIRA balances"
        )


if __name__ == "__main__":
    main()
