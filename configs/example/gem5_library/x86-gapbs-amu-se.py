# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

"""
Run a local GAPBS binary in X86 syscall-emulation mode.

This config is intended for comparing a normal GAPBS binary against a binary
rebuilt with scripts/build_gapbs_amu_cxlmemuring.py. It avoids the stock
full-system GAPBS resource's KVM requirement.
"""

import argparse
import hashlib
import json
import shlex
import time
from pathlib import Path

import m5
from m5.objects import (
    ASMC,
    CIRA,
    Cache,
    CommMonitor,
    NoncoherentXBar,
    NULL,
    SerialLink,
    SpmPartitionManager,
    WayPartitioningPolicy,
    WayPolicyAllocation,
)

from gapbs_roi_state import (
    GapbsCheckpointState,
    GapbsRoiState,
    RoiSequenceError,
    classify_final_exit,
    resolve_workload_shape,
    validate_checkpoint_options,
)
from gem5.components.boards.simple_board import SimpleBoard
from gem5.components.cachehierarchies.classic.no_cache import NoCache
from gem5.components.cachehierarchies.classic.private_l1_private_l2_cache_hierarchy import (
    PrivateL1PrivateL2CacheHierarchy,
)
from gem5.components.memory import SingleChannelDDR4_2400
from gem5.components.processors.cpu_types import CPUTypes
from gem5.components.processors.simple_processor import SimpleProcessor
from gem5.components.processors.simple_switchable_processor import (
    SimpleSwitchableProcessor,
)
from gem5.isas import ISA
from gem5.resources.resource import BinaryResource, CheckpointResource
from gem5.simulate.exit_event import ExitEvent
from gem5.simulate.simulator import Simulator
from gem5.utils.requires import requires


requires(isa_required=ISA.X86)


AMU_PDF_SHA256 = (
    "cba178ece7593b3ede868417a031ded3efddd85d5f7c50672b0a93735187790f"
)
CIRA_CSV_SHA256 = (
    "4e0297da423cee0a742bc2e10656d022bb27776807f2d2ce4cca43e65c634184"
)


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_amu_calibration(path):
    path = Path(path).resolve()
    try:
        with path.open("r", encoding="utf-8") as stream:
            calibration = json.load(stream)
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot read AMU calibration manifest: {path}") from error
    try:
        if calibration["schema"] != 2:
            raise ValueError("AMU calibration manifest schema must be 2")
        if calibration["sources"]["amu_pdf"]["sha256"] != AMU_PDF_SHA256:
            raise ValueError("AMU calibration PDF hash is not approved")
        if calibration["sources"]["cira_csv"]["sha256"] != CIRA_CSV_SHA256:
            raise ValueError("AMU calibration CIRA CSV hash is not approved")
        if calibration["amu"]["validation"]["status"] != "PASS":
            raise ValueError("AMU calibration validation did not pass")
        profile = calibration["amu"]["formal_profile"]
        fit_parameters = calibration["amu"]["fit"]["parameters"]
        selection = calibration["amu"]["formal_profile_selection"]
        near_data = calibration["near_data_pr"]
        if near_data["formal_speedup_is_fit_target"] is not False:
            raise ValueError("formal speedup cannot be a calibration target")
        for owner in ("amu", "cira"):
            parameters = near_data[owner]["parameters"]
            if not parameters or any(
                not isinstance(value, int) or isinstance(value, bool) or value <= 0
                for value in parameters.values()
            ):
                raise ValueError(f"{owner} near-data parameters are invalid")
    except (KeyError, TypeError) as error:
        raise ValueError("AMU calibration manifest is incomplete") from error

    required = {
        "spm_bytes": 64 * 1024,
        "pending_entries_per_state_machine": 32,
        "id_batch_entries": 32,
    }
    for name, expected in required.items():
        if profile.get(name) != expected:
            raise ValueError(
                f"AMU calibration {name} must equal approved value {expected}"
            )
    fitted_names = ("metadata_cycles", "id_refill_cycles", "completion_cycles")
    for name in fitted_names:
        value = profile.get(name)
        if (
            not isinstance(value, int)
            or isinstance(value, bool)
            or value not in {0, 2, 4, 6, 8, 10}
        ):
            raise ValueError(f"AMU calibration {name} is outside the fit space")
    source = selection.get("source")
    fit_applied = selection.get("fit_parameters_applied")
    if source == "bounded_table4_fit":
        if fit_applied is not True:
            raise ValueError("bounded AMU fit must apply fit parameters")
        for name in fitted_names:
            if fit_parameters.get(name) != profile[name]:
                raise ValueError(
                    f"AMU calibration {name} differs from fitted value"
                )
    elif source == "asmc_architecture_defaults":
        if fit_applied is not False:
            raise ValueError("infeasible AMU proxy must not apply fit parameters")
        if (
            calibration["amu"]["validation"].get(
                "proxy_feasibility_status"
            )
            != "INFEASIBLE_NONNEGATIVE_COSTS"
        ):
            raise ValueError(
                "AMU architecture defaults require proven infeasible proxy"
            )
        architecture_defaults = {
            "metadata_cycles": 10,
            "id_refill_cycles": 0,
            "completion_cycles": 0,
        }
        for name, expected in architecture_defaults.items():
            if profile[name] != expected:
                raise ValueError(
                    f"AMU {name} differs from architecture default {expected}"
                )
    else:
        raise ValueError("AMU formal profile selection source is invalid")
    return profile, sha256_file(path)


def configure_amu_l2_spm(l2):
    if int(l2.assoc) != 8:
        raise RuntimeError("calibrated AMU requires an exactly 8-way private L2")
    l2.partitioning_manager = SpmPartitionManager(
        partitioning_policies=[
            WayPartitioningPolicy(
                allocations=[
                    WayPolicyAllocation(
                        partition_id=0, ways=[0, 1, 2, 3, 4, 5]
                    ),
                    WayPolicyAllocation(partition_id=1, ways=[6, 7]),
                ]
            )
        ],
        spm_partition_id=1,
    )


def validate_amu_spm_cardinality(num_cores, num_l2_buses):
    if num_l2_buses != num_cores:
        raise RuntimeError(
            "calibrated AMU requires one private L2 bus per timing core: "
            f"cores={num_cores}, l2_buses={num_l2_buses}"
        )


def connect_asmc_spm_ports(asmc, l2buses, num_cores):
    validate_amu_spm_cardinality(num_cores, len(l2buses))
    for l2bus in l2buses:
        asmc.spm_side_ports = l2bus.cpu_side_ports


class TunablePrivateL1PrivateL2CacheHierarchy(PrivateL1PrivateL2CacheHierarchy):
    def __init__(
        self,
        *args,
        disable_hw_prefetchers=False,
        l1_mshrs=None,
        l1_tgts_per_mshr=None,
        l2_mshrs=None,
        l2_tgts_per_mshr=None,
        l2_assoc=None,
        amu_l2_spm_partition=False,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self._disable_hw_prefetchers = disable_hw_prefetchers
        self._l1_mshrs = l1_mshrs
        self._l1_tgts_per_mshr = l1_tgts_per_mshr
        self._l2_mshrs = l2_mshrs
        self._l2_tgts_per_mshr = l2_tgts_per_mshr
        self._l2_assoc_override = l2_assoc
        self._amu_l2_spm_partition = amu_l2_spm_partition

    def incorporate_cache(self, board):
        super().incorporate_cache(board)

        for idx in range(board.get_processor().get_num_cores()):
            l1i = getattr(self, f"l1i-cache-{idx}")
            l1d = getattr(self, f"l1d-cache-{idx}")
            l2 = getattr(self, f"l2-cache-{idx}")
            if self._l2_assoc_override is not None:
                l2.assoc = self._l2_assoc_override
            if self._amu_l2_spm_partition:
                configure_amu_l2_spm(l2)
            for cache in (l1i, l1d):
                if self._disable_hw_prefetchers:
                    cache.prefetcher = NULL
                if self._l1_mshrs is not None:
                    cache.mshrs = self._l1_mshrs
                if self._l1_tgts_per_mshr is not None:
                    cache.tgts_per_mshr = self._l1_tgts_per_mshr
            if self._disable_hw_prefetchers:
                l2.prefetcher = NULL
            if self._l2_mshrs is not None:
                l2.mshrs = self._l2_mshrs
            if self._l2_tgts_per_mshr is not None:
                l2.tgts_per_mshr = self._l2_tgts_per_mshr


class CXLSimpleBoard(SimpleBoard):
    def __init__(
        self,
        *args,
        cxl_memory=False,
        cxl_args=None,
        cxl_latency_monitor=False,
        cira_to_l2=False,
        amu_l2_spm_partition=False,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        object.__setattr__(self, "_cxl_memory", cxl_memory)
        object.__setattr__(self, "_cxl_args", cxl_args or {})
        object.__setattr__(
            self, "_cxl_latency_monitor", cxl_latency_monitor
        )
        object.__setattr__(self, "_cxl_mem_ports", None)
        object.__setattr__(self, "_cxl_device_xbars", [])
        object.__setattr__(self, "_cira_to_l2", cira_to_l2)
        object.__setattr__(
            self, "_amu_l2_spm_partition", amu_l2_spm_partition
        )

    def get_mem_ports(self):
        if not self._cxl_memory:
            return super().get_mem_ports()

        if self._cxl_mem_ports is None:
            cxl_mem_ports = []
            for idx, (addr_range, port) in enumerate(super().get_mem_ports()):
                link = SerialLink(ranges=[addr_range], **self._cxl_args)
                device_xbar = NoncoherentXBar(
                    width=64,
                    frontend_latency=1,
                    forward_latency=1,
                    response_latency=1,
                )
                link.mem_side_port = device_xbar.cpu_side_ports
                device_xbar.mem_side_ports = port
                setattr(self, f"cxl_mem_link{idx}", link)
                setattr(self, f"cxl_device_xbar{idx}", device_xbar)
                self._cxl_device_xbars.append(device_xbar)
                upstream_port = link.cpu_side_port
                if self._cxl_latency_monitor:
                    monitor = CommMonitor(
                        disable_burst_length_hists=True,
                        disable_bandwidth_hists=True,
                        disable_itt_dists=True,
                        disable_outstanding_hists=True,
                        disable_transaction_hists=True,
                    )
                    monitor.mem_side_port = link.cpu_side_port
                    setattr(self, f"cxl_latency_monitor{idx}", monitor)
                    upstream_port = monitor.cpu_side_port
                cxl_mem_ports.append((addr_range, upstream_port))
            object.__setattr__(self, "_cxl_mem_ports", cxl_mem_ports)

        return self._cxl_mem_ports

    def _connect_things(self):
        super()._connect_things()
        asmc = getattr(self, "asmc", None)
        if asmc is not None:
            if not hasattr(self.cache_hierarchy, "l2buses"):
                raise RuntimeError(
                    "ASMC coherent SPM requires private per-core L2 buses"
                )
            connect_asmc_spm_ports(
                asmc,
                self.cache_hierarchy.l2buses,
                self.get_processor().get_num_cores(),
            )

        cira = getattr(self, "cira", None)
        if cira is None:
            return
        if len(self._cxl_device_xbars) != 1:
            raise RuntimeError(
                "timing CIRA CSR traversal requires one CXL device xbar"
            )
        cira.csr_mem_side_port = self._cxl_device_xbars[0].cpu_side_ports

        if self._cira_to_l2 and hasattr(self.cache_hierarchy, "l2buses"):
            cira.demand_probe_targets = [
                getattr(self.cache_hierarchy, f"l2-cache-{idx}")
                for idx in range(len(self.cache_hierarchy.l2buses))
            ]
            for idx, l2bus in enumerate(self.cache_hierarchy.l2buses):
                cira.mem_side_ports = l2bus.cpu_side_ports
        else:
            cira.demand_probe_targets = []
            cira.mem_side_ports = self.cache_hierarchy.get_cpu_side_port()

parser = argparse.ArgumentParser(
    description="Run local X86 GAPBS binaries, including AMU-instrumented ones."
)
parser.add_argument("--binary", required=True, help="Path to GAPBS binary.")
parser.add_argument(
    "--arguments",
    default="-g 10 -n 1",
    help="Arguments passed to the GAPBS binary.",
)
parser.add_argument("--cores", type=int, default=1)
parser.add_argument(
    "--cpu", choices=["atomic", "timing", "o3", "minor"], default="timing"
)
parser.add_argument(
    "--fast-forward-cpu",
    choices=["atomic"],
    help="CPU used before trial 0 begins; switch to --cpu at trial 0 begin.",
)
parser.add_argument("--scale", type=int)
parser.add_argument("--iterations", type=int)
parser.add_argument("--measure-trial", type=int, default=0)
parser.add_argument("--mem-size", default="4GiB")
parser.add_argument("--clk", default="3GHz")
parser.add_argument("--l1d-size", default="32KiB")
parser.add_argument("--l1i-size", default="32KiB")
parser.add_argument("--l2-size", default="256KiB")
parser.add_argument("--disable-hw-prefetchers", action="store_true")
parser.add_argument("--l1-mshrs", type=int)
parser.add_argument("--l1-tgts-per-mshr", type=int)
parser.add_argument("--l2-mshrs", type=int)
parser.add_argument("--l2-tgts-per-mshr", type=int)
parser.add_argument(
    "--env",
    action="append",
    default=[],
    help="Extra workload environment entry, e.g. KEY=VALUE.",
)
parser.add_argument("--no-asmc", action="store_true")
parser.add_argument(
    "--asmc-profile",
    choices=(
        "legacy",
        "paper-calibration-base",
        "paper-calibrated",
        "paper-sensitivity-256k",
    ),
    default="legacy",
)
parser.add_argument("--asmc-calibration-manifest", type=Path)
parser.add_argument("--asmc-spm-size", default="256KiB")
parser.add_argument("--asmc-granularity", type=int, default=8)
parser.add_argument("--asmc-max-outstanding", type=int, default=256)
parser.add_argument("--asmc-max-send-queue", type=int, default=512)
parser.add_argument("--asmc-issue-latency", default="1ns")
parser.add_argument("--asmc-completion-latency", default="0ns")
parser.add_argument("--asmc-latency", default="0ns")
parser.add_argument("--asmc-pr-descriptor-entries", type=int, default=32)
parser.add_argument("--asmc-pr-read-entries", type=int, default=1024)
parser.add_argument("--asmc-pr-fp-add-cycles", type=int, default=1)
parser.add_argument("--asmc-pr-fp-mul-cycles", type=int, default=1)
parser.add_argument("--asmc-pr-fp-div-cycles", type=int, default=4)
parser.add_argument(
    "--cxl-memory",
    action="store_true",
    help="Route normal cache/memory traffic through a CXL SerialLink.",
)
parser.add_argument("--cxl-link-delay", default="1us")
parser.add_argument("--cxl-link-speed", type=int, default=32)
parser.add_argument("--cxl-link-lanes", type=int, default=16)
parser.add_argument("--cxl-link-req-size", type=int, default=256)
parser.add_argument("--cxl-link-resp-size", type=int, default=256)
parser.add_argument(
    "--cxl-latency-monitor",
    action="store_true",
    help="Measure request-to-response latency at the CXL link boundary.",
)
parser.add_argument("--cira", action="store_true", help="Enable CIRA model.")
parser.add_argument(
    "--cira-to-l2",
    action="store_true",
    help="Send CIRA cacheline installs through the first private L2.",
)
parser.add_argument("--cira-max-outstanding", type=int, default=256)
parser.add_argument("--cira-max-send-queue", type=int, default=1024)
parser.add_argument("--cira-max-csr-walk-queue", type=int, default=4096)
parser.add_argument("--cira-max-csr-index-reads", type=int, default=1024)
parser.add_argument("--cira-csr-lines-per-turn", type=int, default=64)
parser.add_argument("--cira-max-completed-lines", type=int, default=65536)
parser.add_argument("--cira-issue-latency", default="1ns")
parser.add_argument("--cira-completion-latency", default="0ns")
parser.add_argument("--cira-pr-descriptor-entries", type=int, default=16)
parser.add_argument("--cira-pr-csr-read-entries", type=int, default=256)
parser.add_argument("--cira-pr-coherent-entries", type=int, default=256)
parser.add_argument("--cira-pr-fp-add-cycles", type=int, default=1)
parser.add_argument("--cira-pr-fp-mul-cycles", type=int, default=1)
parser.add_argument("--cira-pr-fp-div-cycles", type=int, default=4)
parser.add_argument("--cira-pr-reconfiguration-latency", default="100ns")
parser.add_argument("--cira-pr-policy-base-cycles", type=int, default=1000)
parser.add_argument("--cira-pr-policy-a-cost-ppm", type=int, default=1003978)
parser.add_argument("--cira-pr-policy-b-cost-ppm", type=int, default=1000000)
parser.add_argument("--cira-pr-policy-c-cost-ppm", type=int, default=1038586)
parser.add_argument(
    "--roi-work-events",
    action="store_true",
    help="Reset stats at m5_work_begin and stop after m5_work_end.",
)
parser.add_argument("--continue-after-roi", action="store_true")
parser.add_argument(
    "--require-m5-verification-exit",
    action="store_true",
    help="Require the workload to finish through m5_exit or m5_fail.",
)
checkpoint_group = parser.add_mutually_exclusive_group()
checkpoint_group.add_argument(
    "--checkpoint-save",
    type=Path,
    help="Save state at trial 0's work-begin event.",
)
checkpoint_group.add_argument(
    "--checkpoint-restore",
    type=Path,
    help="Restore state saved at the measured trial's work-begin event.",
)

args = parser.parse_args()

amu_manifest_sha256 = ""
amu_profile = {
    "pending_entries_per_state_machine": 32,
    "id_batch_entries": 32,
    "metadata_cycles": 10,
    "id_refill_cycles": 0,
    "completion_cycles": 0,
}
if args.asmc_profile == "legacy":
    if args.asmc_calibration_manifest is not None:
        parser.error("legacy AMU profile cannot bind a calibration manifest")
elif args.asmc_profile == "paper-calibration-base":
    if args.no_asmc:
        parser.error("paper calibration base requires ASMC to be enabled")
    if args.asmc_calibration_manifest is not None:
        parser.error("paper calibration base cannot bind a fitted manifest")
    if args.asmc_spm_size != "64KiB":
        parser.error("paper calibration base requires --asmc-spm-size 64KiB")
    amu_profile = {
        "pending_entries_per_state_machine": 32,
        "id_batch_entries": 32,
        "metadata_cycles": 0,
        "id_refill_cycles": 0,
        "completion_cycles": 0,
    }
    print("AMU_CALIBRATION_BASE profile=zero-control")
elif args.no_asmc:
    parser.error("a calibrated AMU profile requires ASMC to be enabled")
else:
    if args.asmc_calibration_manifest is None:
        parser.error("calibrated AMU profile requires --asmc-calibration-manifest")
    try:
        amu_profile, amu_manifest_sha256 = load_amu_calibration(
            args.asmc_calibration_manifest
        )
    except ValueError as error:
        parser.error(str(error))
    required_spm = (
        "64KiB" if args.asmc_profile == "paper-calibrated" else "256KiB"
    )
    if args.asmc_profile == "paper-calibrated" and args.asmc_spm_size != "64KiB":
        parser.error("paper-calibrated AMU requires --asmc-spm-size 64KiB")
    if args.asmc_profile == "paper-sensitivity-256k" and args.asmc_spm_size != required_spm:
        parser.error("paper-sensitivity-256k AMU requires --asmc-spm-size 256KiB")
    print(
        "AMU_CALIBRATION "
        f"profile={args.asmc_profile} manifest_sha256={amu_manifest_sha256}"
    )

amu_l2_spm_partition = args.asmc_profile != "legacy"

if args.fast_forward_cpu and not args.roi_work_events:
    parser.error("--fast-forward-cpu requires --roi-work-events")
if args.fast_forward_cpu and args.cpu not in ("timing", "o3"):
    parser.error("--fast-forward-cpu requires --cpu timing or o3")
if args.fast_forward_cpu and args.measure_trial != 1:
    parser.error(
        "--fast-forward-cpu requires --iterations 2 and --measure-trial 1"
    )

binary = Path(args.binary).resolve()
if not binary.exists():
    raise FileNotFoundError(binary)

workload_arguments = shlex.split(args.arguments)

try:
    args.scale, args.iterations = resolve_workload_shape(
        arguments=workload_arguments,
        configured_scale=args.scale,
        configured_iterations=args.iterations,
        fast_forward=bool(args.fast_forward_cpu),
    )
except ValueError as error:
    parser.error(str(error))
if args.iterations <= 0:
    parser.error("--iterations must be positive")
if args.measure_trial < 0 or args.measure_trial >= args.iterations:
    parser.error("--measure-trial must be less than iterations")
try:
    validate_checkpoint_options(
        checkpoint_save=args.checkpoint_save,
        checkpoint_restore=args.checkpoint_restore,
        cxl_memory=args.cxl_memory,
        cpu=args.cpu,
        roi_work_events=args.roi_work_events,
        continue_after_roi=args.continue_after_roi,
        fast_forward_cpu=args.fast_forward_cpu,
        iterations=args.iterations,
        measure_trial=args.measure_trial,
        require_m5_verification_exit=args.require_m5_verification_exit,
    )
except ValueError as error:
    parser.error(str(error))

cpu_type = {
    "atomic": CPUTypes.ATOMIC,
    "timing": CPUTypes.TIMING,
    "o3": CPUTypes.O3,
    "minor": CPUTypes.MINOR,
}[args.cpu]

if args.checkpoint_save:
    cache_hierarchy = NoCache()
else:
    cache_hierarchy = TunablePrivateL1PrivateL2CacheHierarchy(
        l1d_size=args.l1d_size,
        l1i_size=args.l1i_size,
        l2_size=args.l2_size,
        disable_hw_prefetchers=args.disable_hw_prefetchers,
        l1_mshrs=args.l1_mshrs,
        l1_tgts_per_mshr=args.l1_tgts_per_mshr,
        l2_mshrs=args.l2_mshrs,
        l2_tgts_per_mshr=args.l2_tgts_per_mshr,
        l2_assoc=8 if amu_l2_spm_partition else None,
        amu_l2_spm_partition=amu_l2_spm_partition,
    )
memory = SingleChannelDDR4_2400(size=args.mem_size)
if args.fast_forward_cpu:
    starting_cpu_type = {
        "atomic": CPUTypes.ATOMIC,
    }[args.fast_forward_cpu]
    processor = SimpleSwitchableProcessor(
        starting_core_type=starting_cpu_type,
        switch_core_type=cpu_type,
        isa=ISA.X86,
        num_cores=args.cores,
    )
else:
    processor = SimpleProcessor(
        cpu_type=cpu_type, isa=ISA.X86, num_cores=args.cores
    )

board = CXLSimpleBoard(
    clk_freq=args.clk,
    processor=processor,
    memory=memory,
    cache_hierarchy=cache_hierarchy,
    cxl_memory=args.cxl_memory,
    cxl_args={
        "delay": args.cxl_link_delay,
        "link_speed": args.cxl_link_speed,
        "num_lanes": args.cxl_link_lanes,
        "req_size": args.cxl_link_req_size,
        "resp_size": args.cxl_link_resp_size,
    },
    cxl_latency_monitor=args.cxl_latency_monitor,
    cira_to_l2=args.cira_to_l2,
    amu_l2_spm_partition=amu_l2_spm_partition,
)
board.m5ops_base = 0xFFFF0000

if not args.no_asmc:
    board.asmc = ASMC(
        calibration_profile=args.asmc_profile,
        calibration_manifest_sha256=amu_manifest_sha256,
        spm_size=args.asmc_spm_size,
        default_granularity=args.asmc_granularity,
        max_outstanding=args.asmc_max_outstanding,
        max_send_queue=args.asmc_max_send_queue,
        pending_queue_entries=amu_profile[
            "pending_entries_per_state_machine"
        ],
        id_batch_entries=amu_profile["id_batch_entries"],
        metadata_latency=amu_profile["metadata_cycles"],
        id_refill_latency=amu_profile["id_refill_cycles"],
        completion_publish_latency=amu_profile["completion_cycles"],
        issue_latency=args.asmc_issue_latency,
        completion_latency=args.asmc_completion_latency,
        asmc_latency=args.asmc_latency,
        pr_descriptor_entries=args.asmc_pr_descriptor_entries,
        pr_read_entries=args.asmc_pr_read_entries,
        pr_fp_add_cycles=args.asmc_pr_fp_add_cycles,
        pr_fp_mul_cycles=args.asmc_pr_fp_mul_cycles,
        pr_fp_div_cycles=args.asmc_pr_fp_div_cycles,
    )
    board.asmc_io_cache = Cache(
        assoc=8,
        tag_latency=1,
        data_latency=1,
        response_latency=1,
        mshrs=args.asmc_max_outstanding,
        size="1KiB",
        tgts_per_mshr=16,
        write_buffers=args.asmc_max_outstanding,
    )
    board.asmc.mem_side_port = board.asmc_io_cache.cpu_side
    if args.cxl_memory:
        board.asmc_io_cache.mem_side = cache_hierarchy.get_cpu_side_port()
    else:
        board.cxl_link = SerialLink(
            delay=args.cxl_link_delay,
            link_speed=args.cxl_link_speed,
            num_lanes=args.cxl_link_lanes,
            req_size=args.cxl_link_req_size,
            resp_size=args.cxl_link_resp_size,
        )
        board.asmc_io_cache.mem_side = board.cxl_link.cpu_side_port
        board.cxl_link.mem_side_port = cache_hierarchy.get_cpu_side_port()

if args.cira:
    board.cira = CIRA(
        max_outstanding=args.cira_max_outstanding,
        max_send_queue=args.cira_max_send_queue,
        max_csr_walk_queue=args.cira_max_csr_walk_queue,
        max_csr_index_reads=args.cira_max_csr_index_reads,
        csr_lines_per_turn=args.cira_csr_lines_per_turn,
        max_completed_lines=args.cira_max_completed_lines,
        issue_latency=args.cira_issue_latency,
        completion_latency=args.cira_completion_latency,
        pr_descriptor_entries=args.cira_pr_descriptor_entries,
        pr_csr_read_entries=args.cira_pr_csr_read_entries,
        pr_coherent_entries=args.cira_pr_coherent_entries,
        pr_fp_add_cycles=args.cira_pr_fp_add_cycles,
        pr_fp_mul_cycles=args.cira_pr_fp_mul_cycles,
        pr_fp_div_cycles=args.cira_pr_fp_div_cycles,
        pr_reconfiguration_latency=args.cira_pr_reconfiguration_latency,
        pr_policy_base_cycles=args.cira_pr_policy_base_cycles,
        pr_policy_a_cost_ppm=args.cira_pr_policy_a_cost_ppm,
        pr_policy_b_cost_ppm=args.cira_pr_policy_b_cost_ppm,
        pr_policy_c_cost_ppm=args.cira_pr_policy_c_cost_ppm,
    )

checkpoint = (
    CheckpointResource(
        local_path=str(args.checkpoint_restore.resolve())
    )
    if args.checkpoint_restore
    else None
)
board.set_se_binary_workload(
    BinaryResource(local_path=str(binary)),
    arguments=workload_arguments,
    env_list=[f"OMP_NUM_THREADS={args.cores}", *args.env],
    checkpoint=checkpoint,
)
if not args.no_asmc:
    # SimpleBoard initializes mem_ranges when its workload is installed.
    # Binding the I/O cache range earlier silently leaves it empty, causing
    # the cache to drop every coherent snoop while still accepting requests.
    board.asmc_io_cache.addr_ranges = board.mem_ranges

start_tick = None
roi_state = None
checkpoint_state = None
if args.checkpoint_save:
    checkpoint_state = GapbsCheckpointState(
        mode="save",
        iterations=args.iterations,
        measure_trial=args.measure_trial,
    )
elif args.checkpoint_restore:
    checkpoint_state = GapbsCheckpointState(
        mode="restore",
        iterations=args.iterations,
        measure_trial=args.measure_trial,
    )
elif args.roi_work_events:
    roi_state = GapbsRoiState(
        iterations=args.iterations,
        measure_trial=args.measure_trial,
        switch_at_trial_zero=bool(args.fast_forward_cpu),
    )


def handle_workbegin():
    while True:
        actions = (
            checkpoint_state.work_begin()
            if checkpoint_state is not None
            else roi_state.work_begin()
        )
        for action in actions:
            if action == "switch":
                print(
                    "Switching from fast-forward CPU to "
                    f"{args.cpu} CPU!"
                )
                processor.switch()
            elif action == "reset":
                print("Resetting stats at the start of measured ROI!")
                m5.stats.reset()
            elif action == "record_start_tick":
                global start_tick
                start_tick = m5.curTick()
            elif action == "checkpoint":
                simulator.save_checkpoint(args.checkpoint_save)
                print(
                    "GAPBS_CHECKPOINT_SAVED "
                    f"path={args.checkpoint_save.resolve()}"
                )
                checkpoint_state.checkpoint_saved()
        yield "checkpoint" in actions


def handle_workend():
    while True:
        actions = (
            checkpoint_state.work_end()
            if checkpoint_state is not None
            else roi_state.work_end()
        )
        if "dump" in actions:
            print("Dump stats at the end of the measured ROI!")
            m5.stats.dump()
        if args.checkpoint_restore:
            yield False
        elif args.fast_forward_cpu:
            yield False
        else:
            yield "dump" in actions and not args.continue_after_roi


if args.roi_work_events:
    simulator = Simulator(
        board=board,
        on_exit_event={
            ExitEvent.WORKBEGIN: handle_workbegin(),
            ExitEvent.WORKEND: handle_workend(),
        },
    )
else:
    simulator = Simulator(board=board)

start_wall = time.time()
print(f"Running {binary} {' '.join(args.arguments.split())}")
if args.checkpoint_restore:
    simulator._instantiate()
    for action in checkpoint_state.resume_actions():
        if action == "reset":
            print("Resetting stats at restored measured ROI!")
            m5.stats.reset()
        elif action == "record_start_tick":
            start_tick = m5.curTick()
    print(
        "GAPBS_CHECKPOINT_RESTORED "
        f"path={args.checkpoint_restore.resolve()}"
    )
simulator.run()
if checkpoint_state is not None:
    try:
        checkpoint_state.finish()
    except RoiSequenceError as error:
        print(f"Verification: MISSING ({error})")
        raise SystemExit(3)
elif roi_state is not None:
    try:
        roi_state.finish()
    except RoiSequenceError as error:
        print(f"Verification: MISSING ({error})")
        raise SystemExit(3)
if (
    args.fast_forward_cpu
    or args.continue_after_roi
    or args.require_m5_verification_exit
):
    if not args.checkpoint_save:
        exit_cause = simulator.get_last_exit_event_cause()
        verification, exit_code = classify_final_exit(
            exit_cause,
            require_m5_exit=args.require_m5_verification_exit,
        )
        if verification == "fail":
            print("Verification: FAIL")
            raise SystemExit(exit_code)
        if verification == "missing":
            print(f"Verification: MISSING ({exit_cause})")
            raise SystemExit(exit_code)
        if args.continue_after_roi or args.require_m5_verification_exit:
            if args.require_m5_verification_exit:
                print(
                    "GAPBS_VERIFICATION_EXIT_CAUSE "
                    f"cause={exit_cause}"
                )
            print("Verification: PASS")
if not args.roi_work_events:
    m5.stats.dump()

print("Done with the simulation")
print(f"Simulated ticks: {simulator.get_current_tick()}")
if start_tick is not None:
    print(f"Simulated ROI ticks: {m5.curTick() - start_tick}")
print(f"Wallclock seconds: {time.time() - start_wall:.2f}")
