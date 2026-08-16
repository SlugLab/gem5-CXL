#!/usr/bin/env python3
# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

"""Run the formal four-scale Vanilla/AMU/CIRA/M2NDP comparison."""

import argparse
import configparser
import csv
import dataclasses
import hashlib
import json
import re
import subprocess
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path

try:
    from scripts import cross_system_contract as contract
    from scripts import freeze_pr_scaling_inputs as scaling_inputs
    from scripts import pr_scaling_variant_build as variant_build
except ImportError:
    import cross_system_contract as contract
    import freeze_pr_scaling_inputs as scaling_inputs
    import pr_scaling_variant_build as variant_build


REPO = Path(__file__).resolve().parents[1]
PROFILE = "pr-scaling-4thread-1us"
SCALES = (4, 12, 14, 20)
PERFORMANCE_SCALES = (12, 14, 20)
SYSTEMS = ("vanilla", "amu", "cira", "m2ndp")
MIN_ACCELERATOR_SPEEDUP = Decimal("1.4")
MAX_ACCELERATOR_SPEEDUP = Decimal("1.6")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
PRE_LAZY_VARIANT_CODE_SHA256 = (
    "438735b038266173d5337d86db3fdbcf26321794336f8af52180081ef08f94d3"
)


class ScalingError(RuntimeError):
    """A scaling point violates the formal experiment contract."""


@dataclasses.dataclass(frozen=True)
class MatrixEntry:
    scale: int
    system: str
    latency: str = "1us"
    full_e2e: bool = True

    def __post_init__(self):
        if self.scale not in SCALES:
            raise ScalingError(f"unsupported graph scale: {self.scale}")
        if self.system not in SYSTEMS:
            raise ScalingError(f"unsupported system: {self.system}")
        if self.latency != "1us" or self.full_e2e is not True:
            raise ScalingError("formal scaling points must be full E2E at 1us")

    @property
    def key(self):
        return f"g{self.scale}:{self.system}"


def build_matrix():
    return tuple(
        MatrixEntry(scale, system)
        for scale in SCALES
        for system in SYSTEMS
    )


def needs_variant_build(entry):
    return entry.system in {"amu", "cira"}


def _sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _code_sha256():
    digest = hashlib.sha256()
    for path in (
        Path(__file__).resolve(),
        REPO / "scripts/compare_gapbs_cxl_amu_cira.py",
        REPO / "scripts/freeze_pr_scaling_inputs.py",
        REPO / "scripts/gapbs_pr_experiment_profiles.py",
        REPO / "scripts/pr_scaling_variant_build.py",
        REPO / "scripts/qualify_pr_scaling_g12.py",
        REPO / "scripts/build_gapbs_matched_pr_spmv_variants.py",
        REPO / "scripts/run_gapbs_matched_pr_spmv_variants.py",
        REPO / "scripts/run_m2ndp_g20_pr_spmv.py",
    ):
        relative = path.relative_to(REPO).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(8, "little"))
        digest.update(relative)
        digest.update(bytes.fromhex(_sha256_file(path)))
    return digest.hexdigest()


def _load_json(path, label):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ScalingError(f"invalid {label}: {error}") from error
    if not isinstance(value, dict):
        raise ScalingError(f"{label} must be a JSON object")
    return value


def load_inputs(path):
    try:
        return scaling_inputs.load_and_validate(path)
    except scaling_inputs.ScalingInputError as error:
        raise ScalingError(str(error)) from error


def _graph_for(entry, options):
    inputs = load_inputs(options.inputs)
    return next(row for row in inputs["graphs"] if row["scale"] == entry.scale)


def command_for(entry, options):
    graph = _graph_for(entry, options)
    common = [
        "--graph", str(Path(graph["path"]).resolve()),
        "--graph-scale", str(entry.scale),
        "--profile", PROFILE,
        "--graph-manifest", str(Path(graph["manifest"]).resolve()),
        "--cxl-link-delay", "1us",
        "--gem5", str(Path(options.gem5).resolve()),
        "--timeout", str(options.timeout),
    ]
    scale_root = Path(options.root).resolve() / "scales" / f"g{entry.scale}"
    if entry.system in {"vanilla", "m2ndp"}:
        command = [
            sys.executable,
            str(REPO / "scripts/run_m2ndp_g20_pr_spmv.py"),
            *common,
            "--cxlmemuring", str(Path(options.cxlmemuring).resolve()),
            "--m2ndp-root", str(Path(options.m2ndp_root).resolve()),
            "--m5-library", str(Path(options.m5_library).resolve()),
            "--outdir", str(scale_root / "m2ndp"),
        ]
        if entry.system == "vanilla":
            command.extend(("--stop-after", "gem5_baseline"))
            if (
                getattr(options, "resume", False)
                and (scale_root / "m2ndp/status.json").is_file()
            ):
                command.append("--resume")
        else:
            command.append("--resume")
        return command
    command = [
        sys.executable,
        str(REPO / "scripts/run_gapbs_matched_pr_spmv_variants.py"),
        *common,
        "--config", str(Path(options.config).resolve()),
        "--variants-build",
        str(Path(options.variants_build_root).resolve() / f"g{entry.scale}"),
        "--kind", entry.system,
        "--checkpoint-root", str(scale_root / "checkpoints" / entry.system),
        "--outdir", str(scale_root / entry.system),
    ]
    if entry.system == "amu":
        command.extend((
            "--asmc-profile", "paper-calibrated",
            "--asmc-calibration-manifest",
            str(Path(options.calibration).resolve()),
        ))
    return command


def validate_config(path):
    path = Path(path)
    parser = configparser.ConfigParser(
        interpolation=None, strict=True, delimiters=("=",)
    )
    parser.optionxform = str
    try:
        with path.open(encoding="utf-8") as stream:
            parser.read_file(stream)
    except (OSError, configparser.Error) as error:
        raise ScalingError(f"cannot read gem5 config: {error}") from error

    def value(section, key):
        try:
            return parser[section][key].strip()
        except KeyError as error:
            raise ScalingError(
                f"gem5 config is missing [{section}] {key}"
            ) from error

    delay = value("board.cxl_mem_link0", "delay")
    if delay != "1000000":
        raise ScalingError(
            f"CXL delay is {delay!r}, expected '1000000'"
        )
    board_range = value("board", "mem_ranges")
    link_range = value("board.cxl_mem_link0", "ranges")
    dram_range = value("board.memory.mem_ctrl.dram", "range")
    if not board_range or not (
        board_range == link_range == dram_range
    ):
        raise ScalingError(
            "all-CXL range mismatch: "
            f"board={board_range!r} link={link_range!r} "
            f"dram={dram_range!r}"
        )
    expected_topology = {
        ("board.cxl_mem_link0", "type"): "SerialLink",
        ("board.cxl_mem_link0", "cpu_side_port"): (
            "board.cache_hierarchy.membus.mem_side_ports[0]"
        ),
        ("board.cxl_mem_link0", "mem_side_port"): (
            "board.cxl_device_xbar0.cpu_side_ports[0]"
        ),
        ("board.cxl_device_xbar0", "type"): "NoncoherentXBar",
        ("board.cxl_device_xbar0", "cpu_side_ports"): (
            "board.cxl_mem_link0.mem_side_port"
        ),
        ("board.cxl_device_xbar0", "mem_side_ports"): (
            "board.memory.mem_ctrl.port"
        ),
        ("board.memory.mem_ctrl", "port"): (
            "board.cxl_device_xbar0.mem_side_ports[0]"
        ),
    }
    for (section, key), expected in expected_topology.items():
        actual = value(section, key)
        if actual != expected:
            raise ScalingError(
                f"all-CXL topology [{section}] {key}={actual!r}, "
                f"expected {expected!r}"
            )
    core_pattern = re.compile(r"board\.processor\.cores([0-9]+)\.core")
    core_sections = {
        int(match.group(1)): section
        for section in parser.sections()
        if (match := core_pattern.fullmatch(section)) is not None
    }
    if set(core_sections) != {0, 1, 2, 3}:
        raise ScalingError(
            "gem5 config does not use exactly four cores: "
            f"indices={sorted(core_sections)}"
        )
    for index, section in core_sections.items():
        cpu_type = value(section, "type")
        if cpu_type != "BaseTimingSimpleCPU":
            raise ScalingError(
                f"core {index} type is {cpu_type!r}, expected timing CPU"
            )
    return {
        "delay": int(delay),
        "cores": len(core_sections),
        "range": board_range,
        "all_memory_cxl": True,
    }


def validate_checkpoint_manifest(manifest):
    if not isinstance(manifest, dict):
        raise ScalingError("checkpoint manifest must be an object")
    if manifest.get("boundary") != "trial0_entry":
        raise ScalingError("checkpoint boundary must be trial0_entry")
    return manifest


def validate_rank_bits(reference, actual, *, expected_words):
    expected_size = expected_words * 4
    reference = Path(reference)
    actual = Path(actual)
    for label, path in (("reference", reference), ("result", actual)):
        if not path.is_file() or path.stat().st_size != expected_size:
            raise ScalingError(
                f"{label} rank image must contain {expected_words} u32 words"
            )
    word = 0
    with reference.open("rb") as left, actual.open("rb") as right:
        while True:
            a = left.read(1024 * 1024)
            b = right.read(1024 * 1024)
            if not a:
                break
            if a != b:
                for offset in range(0, len(a), 4):
                    if a[offset:offset + 4] != b[offset:offset + 4]:
                        raise ScalingError(
                            f"rank bit mismatch at word {word + offset // 4}"
                        )
            word += len(a) // 4
    return _sha256_file(reference)


def _integer(row, field):
    try:
        return int(row[field])
    except (KeyError, TypeError, ValueError) as error:
        raise ScalingError(f"{field} is not an integer") from error


def _decimal(row, field):
    try:
        value = Decimal(str(row[field]))
    except (KeyError, InvalidOperation) as error:
        raise ScalingError(f"{field} is not a decimal") from error
    if not value.is_finite():
        raise ScalingError(f"{field} must be finite")
    return value


def _per_core(row, field):
    try:
        values = tuple(int(value) for value in str(row[field]).split(";"))
    except (KeyError, ValueError) as error:
        raise ScalingError(f"{field} is not a per-core integer vector") from error
    return values


def validate_mechanism_row(system, row):
    if row.get("status") != "ok" or row.get("verification") != "pass":
        raise ScalingError(f"{system} functional verification did not pass")
    if system == "vanilla":
        return row
    if system == "amu":
        issued = _integer(row, "asmc_loads")
        completed = _integer(row, "asmc_completed")
        logical_values = _integer(row, "amu_logical_values")
        line_requests = _integer(row, "amu_line_requests")
        cache_hits = _integer(row, "amu_line_cache_hits")
        coalesced_misses = _integer(row, "amu_coalesced_misses")
        errors = sum(_integer(row, field) for field in (
            "asmc_queue_full_errors",
            "asmc_spm_full_errors",
            "asmc_translation_errors",
            "asmc_pending_errors",
            "asmc_spm_flag_errors",
        ))
        if errors:
            raise ScalingError(f"AMU error counters are nonzero: {errors}")
        if issued <= 0 or issued != completed:
            raise ScalingError("AMU issued/completed work differs")
        if line_requests != issued:
            raise ScalingError("AMU line requests differ from issued loads")
        if not 0 < line_requests < logical_values:
            raise ScalingError(
                "AMU requires fewer line requests than logical values"
            )
        if _integer(row, "scale") in PERFORMANCE_SCALES:
            if cache_hits <= 0:
                raise ScalingError("AMU cache hits must be nonzero")
            if coalesced_misses <= 0:
                raise ScalingError("AMU coalesced misses must be nonzero")
        return row
    if system == "cira":
        issued = _integer(row, "cira_prefetches")
        completed = _integer(row, "cira_completed")
        errors = sum(_integer(row, field) for field in (
            "cira_rejected_queue_full",
            "cira_rejected_csr_index_queue_full",
            "cira_dropped_csr_descriptors",
        ))
        per_core_issued = _per_core(row, "cira_issued_per_core")
        per_core_completed = _per_core(row, "cira_completed_per_core")
        if errors:
            raise ScalingError(f"CIRA rejected or dropped work: {errors}")
        if (
            issued <= 0
            or completed <= 0
            or len(per_core_issued) != 4
            or len(per_core_completed) != 4
            or any(value <= 0 for value in per_core_issued)
        ):
            raise ScalingError("CIRA requires four active cores")
        if per_core_issued != per_core_completed:
            raise ScalingError("CIRA per-core issued/completed work differs")
        return row
    if system == "m2ndp":
        if (
            _integer(row, "funcsim_compared") <= 0
            or _integer(row, "funcsim_mismatched") != 0
        ):
            raise ScalingError("M2NDP FuncSim bit-exact gate failed")
        if row.get("calibration_pass") != "pass":
            raise ScalingError("M2NDP calibration did not pass")
        if _decimal(row, "calibration_residual_ns") > _decimal(
            row, "calibration_link_period_ns"
        ):
            raise ScalingError("M2NDP calibration residual exceeds one link cycle")
        if _integer(row, "kernel_launches") <= 0:
            raise ScalingError("M2NDP has no kernel launches")
        return row
    raise ScalingError(f"unsupported system: {system}")


def load_qualification(path, options):
    value = _load_json(path, "g12 qualification")
    if (
        value.get("schema") != 1
        or value.get("status") != "passed"
        or value.get("profile") != "pr-scaling-g12-qualification"
    ):
        raise ScalingError("g12 qualification is not PASS")
    inputs = load_inputs(options.inputs)
    expected_identity = {
        "code_sha256": _code_sha256(),
        "inputs_sha256": _sha256_file(options.inputs),
        "calibration_sha256": _sha256_file(options.calibration),
        "gem5_sha256": _sha256_file(options.gem5),
        "m5_library_sha256": _sha256_file(options.m5_library),
        "config_sha256": _sha256_file(options.config),
        "g12_graph_sha256": next(
            row["sha256"] for row in inputs["graphs"]
            if row["scale"] == 12
        ),
    }
    for field, expected in expected_identity.items():
        if value.get(field) != expected:
            raise ScalingError(f"qualification identity differs: {field}")
    try:
        variant_manifest = Path(value["variant_manifest"]).resolve()
    except (KeyError, TypeError) as error:
        raise ScalingError("qualification variant manifest is missing") from error
    if (
        not variant_manifest.is_file()
        or value.get("variant_manifest_sha256")
        != _sha256_file(variant_manifest)
    ):
        raise ScalingError("qualification variant manifest hash differs")

    points = value.get("points")
    expected_points = {
        "g12:vanilla", "g12:amu", "g12:cira"
    }
    if not isinstance(points, dict) or set(points) != expected_points:
        raise ScalingError("qualification must contain exactly three g12 points")
    ranks = set()
    for key in sorted(expected_points):
        row = points[key]
        if (
            not isinstance(row, dict)
            or row.get("status") != "passed"
            or row.get("mechanism", {}).get("verification") != "pass"
        ):
            raise ScalingError(f"qualification point is not PASS: {key}")
        try:
            ranks.add(row["outputs"]["rank"])
        except (KeyError, TypeError) as error:
            raise ScalingError("qualification rank evidence is missing") from error
    if len(ranks) != 1:
        raise ScalingError("qualification rank hashes differ")

    baseline = _positive_decimal_value(
        points["g12:vanilla"].get("latency_seconds"),
        "qualification Vanilla latency",
    )
    recomputed = {}
    for system in ("amu", "cira"):
        key = f"g12:{system}"
        speedup = baseline / _positive_decimal_value(
            points[key].get("latency_seconds"),
            f"qualification {system} latency",
        )
        stored = _positive_decimal_value(
            points[key].get("speedup"),
            f"qualification {system} speedup",
        )
        if stored != speedup:
            raise ScalingError(f"qualification {system} speedup differs")
        if not MIN_ACCELERATOR_SPEEDUP <= speedup <= MAX_ACCELERATOR_SPEEDUP:
            raise ScalingError(f"qualification {system} performance is outside gate")
        recomputed[system] = format(speedup.normalize(), "f")
    if value.get("performance_gate") != {
        "status": "passed",
        "checked_points": 2,
        "speedups": recomputed,
        "offenders": [],
    }:
        raise ScalingError("qualification performance gate differs")
    return value


def new_state(options):
    inputs = load_inputs(options.inputs)
    qualification = load_qualification(options.qualification, options)
    input_hash = _sha256_file(options.inputs)
    calibration_hash = _sha256_file(options.calibration)
    return {
        "schema": 1,
        "status": "timing_in_progress",
        "profile": PROFILE,
        "code_sha256": _code_sha256(),
        "inputs_sha256": input_hash,
        "calibration_sha256": calibration_hash,
        "graph_set_sha256": inputs["graph_set_sha256"],
        "g20_graph_sha256": next(
            row["sha256"]
            for row in inputs["graphs"]
            if row["scale"] == 20
        ),
        "gem5_sha256": _sha256_file(options.gem5),
        "m5_library_sha256": _sha256_file(options.m5_library),
        "config_sha256": _sha256_file(options.config),
        "qualification_sha256": _sha256_file(options.qualification),
        "qualification_variant_manifest_sha256": qualification[
            "variant_manifest_sha256"
        ],
        "variant_builds": {
            f"g{scale}": {
                "status": "pending",
                "command": [],
                "inputs": {},
                "outputs": {},
                "log": None,
                "error": None,
            }
            for scale in SCALES
        },
        "points": {
            entry.key: {
                "scale": entry.scale,
                "system": entry.system,
                "latency": entry.latency,
                "full_e2e": entry.full_e2e,
                "status": "pending",
                "outputs": {},
                "latency_seconds": None,
                "speedup": None,
                "output_elements": 1 << entry.scale,
                "mechanism": {},
            }
            for entry in build_matrix()
        },
    }


def _variant_build_inputs(scale, options):
    root = Path(options.root).resolve()
    baseline_manifest = (
        root / f"scales/g{scale}/m2ndp/build/manifest.json"
    )
    return {
        "graph_scale": scale,
        "baseline_manifest_sha256": _sha256_file(baseline_manifest),
        "calibration_sha256": _sha256_file(options.calibration),
        "m5_library_sha256": _sha256_file(options.m5_library),
        "builder_sha256": _sha256_file(
            REPO / "scripts/build_gapbs_matched_pr_spmv_variants.py"
        ),
        "orchestrator_sha256": _sha256_file(
            REPO / "scripts/pr_scaling_variant_build.py"
        ),
    }


def ensure_variants_for_scale(scale, state, options):
    if scale not in SCALES:
        raise ScalingError(f"unsupported variant scale: {scale}")
    vanilla = state.get("points", {}).get(f"g{scale}:vanilla", {})
    if vanilla.get("status") != "passed":
        raise ScalingError(
            f"g{scale} Vanilla must pass before building variants"
        )
    root = Path(options.root).resolve()
    baseline_build = root / f"scales/g{scale}/m2ndp/build"
    final = Path(options.variants_build_root).resolve() / f"g{scale}"
    log = root / f"scales/g{scale}/variant-build.log"
    key = f"g{scale}"
    record = state["variant_builds"][key]
    inputs = _variant_build_inputs(scale, options)
    if record.get("status") == "passed":
        if record.get("inputs") != inputs:
            raise ScalingError(f"{key} variant build inputs changed")
        try:
            outputs = variant_build.validate_variant_build(
                final,
                baseline_build=baseline_build,
                calibration=options.calibration,
                graph_scale=scale,
            )
        except variant_build.VariantBuildError as error:
            raise ScalingError(str(error)) from error
        if record.get("outputs") != outputs:
            raise ScalingError(f"{key} variant build outputs changed")
        return outputs
    record.update({
        "status": "running",
        "command": [],
        "inputs": inputs,
        "outputs": {},
        "log": str(log),
        "error": None,
    })
    contract.atomic_write_json(root / "state.json", state)
    try:
        outputs = variant_build.ensure_variant_build(
            final,
            baseline_build=baseline_build,
            cxlmemuring=options.cxlmemuring,
            m5_library=options.m5_library,
            calibration=options.calibration,
            graph_scale=scale,
            log=log,
        )
    except (variant_build.VariantBuildError, OSError) as error:
        record.update(status="failed", error=str(error))
        contract.atomic_write_json(root / "state.json", state)
        raise ScalingError(str(error)) from error
    command = outputs.pop("command", [])
    record.update({
        "status": "passed",
        "command": command,
        "outputs": outputs,
        "error": None,
    })
    contract.atomic_write_json(root / "state.json", state)
    return outputs


def _positive_decimal_value(value, label):
    if isinstance(value, bool) or isinstance(value, float):
        raise ScalingError(f"{label} must be an exact decimal")
    try:
        result = Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ScalingError(f"{label} is not a decimal") from error
    if not result.is_finite() or result <= 0:
        raise ScalingError(f"{label} must be finite and positive")
    return result


def record_pass(
    state, entry, output_hashes, *, latency_seconds, output_elements,
    mechanism,
):
    if entry.key not in state.get("points", {}):
        raise ScalingError(f"unknown matrix point: {entry.key}")
    if not isinstance(output_hashes, dict) or not output_hashes:
        raise ScalingError("point output hashes are missing")
    for label, digest in output_hashes.items():
        if not isinstance(label, str) or _SHA256.fullmatch(str(digest)) is None:
            raise ScalingError(f"invalid output SHA-256 for {label}")
    seconds = _positive_decimal_value(latency_seconds, "latency seconds")
    if (
        not isinstance(output_elements, int)
        or isinstance(output_elements, bool)
        or output_elements != 1 << entry.scale
    ):
        raise ScalingError("output element count differs from graph scale")
    if (
        not isinstance(mechanism, dict)
        or mechanism.get("verification") != "pass"
    ):
        raise ScalingError("mechanism evidence is not a verified record")
    baseline = state["points"][f"g{entry.scale}:vanilla"]
    if entry.system == "vanilla":
        speedup = Decimal(1)
    else:
        if baseline.get("status") != "passed":
            raise ScalingError("Vanilla latency must pass before accelerator points")
        speedup = _positive_decimal_value(
            baseline.get("latency_seconds"), "Vanilla latency seconds"
        ) / seconds
    point = state["points"][entry.key]
    point["status"] = "passed"
    point["outputs"] = dict(sorted(output_hashes.items()))
    point["latency_seconds"] = str(seconds)
    point["speedup"] = str(speedup)
    point["output_elements"] = output_elements
    point["mechanism"] = dict(sorted(mechanism.items()))
    return state


def is_complete(state):
    points = state.get("points", {})
    return (
        set(points) == {entry.key for entry in build_matrix()}
        and all(row.get("status") == "passed" for row in points.values())
    )


def evaluate_performance_gate(state):
    if not is_complete(state):
        raise ScalingError(
            "performance gate requires 16/16 correctness-passed points"
        )
    offenders = []
    for scale in PERFORMANCE_SCALES:
        baseline = _positive_decimal_value(
            state["points"][f"g{scale}:vanilla"].get("latency_seconds"),
            f"g{scale} Vanilla latency seconds",
        )
        for system in SYSTEMS:
            if system == "vanilla":
                continue
            key = f"g{scale}:{system}"
            point = state["points"][key]
            seconds = _positive_decimal_value(
                point.get("latency_seconds"), f"{key} latency seconds"
            )
            speedup = baseline / seconds
            stored = _positive_decimal_value(
                point.get("speedup"), f"{key} stored speedup"
            )
            if stored != speedup:
                raise ScalingError(f"{key} stored speedup differs")
            if not (
                MIN_ACCELERATOR_SPEEDUP
                <= speedup
                <= MAX_ACCELERATOR_SPEEDUP
            ):
                offenders.append({
                    "point": key,
                    "scale": scale,
                    "system": system,
                    "speedup": str(speedup),
                    "minimum": str(MIN_ACCELERATOR_SPEEDUP),
                    "maximum": str(MAX_ACCELERATOR_SPEEDUP),
                })
    return {
        "status": "hold" if offenders else "passed",
        "checked_points": len(PERFORMANCE_SCALES) * 3,
        "offenders": offenders,
    }


def migrate_pre_lazy_variant_state(state, expected, options):
    """Migrate only the exact, validated one-point pre-variant state."""
    if state.get("code_sha256") != PRE_LAZY_VARIANT_CODE_SHA256:
        raise ScalingError("legacy scaling state code identity differs")
    variants_root = Path(options.variants_build_root).resolve()
    published = [
        variants_root / f"g{scale}"
        for scale in SCALES
        if (variants_root / f"g{scale}").exists()
    ]
    if published:
        raise ScalingError(
            "legacy scaling state has a published variant directory"
        )

    entry = MatrixEntry(4, "vanilla")
    candidate = json.loads(json.dumps(expected))
    candidate["code_sha256"] = PRE_LAZY_VARIANT_CODE_SHA256
    candidate.pop("variant_builds")
    record_pass(
        candidate,
        entry,
        _point_outputs(entry, options),
        **_point_measurement(entry, options),
    )
    if state != candidate:
        raise ScalingError(
            "legacy scaling state is not the exact g4 Vanilla prefix"
        )

    migrated = json.loads(json.dumps(expected))
    migrated["points"][entry.key] = json.loads(
        json.dumps(candidate["points"][entry.key])
    )
    migrated["resume_lineage"] = {
        "previous_code_sha256": PRE_LAZY_VARIANT_CODE_SHA256,
        "current_code_sha256": expected["code_sha256"],
        "retained_points": [entry.key],
    }
    return migrated


def _read_single_csv(path):
    try:
        with Path(path).open(newline="", encoding="utf-8") as stream:
            rows = list(csv.DictReader(stream))
    except OSError as error:
        raise ScalingError(f"cannot read summary {path}: {error}") from error
    if len(rows) != 1:
        raise ScalingError(f"{path} must contain exactly one data row")
    return rows[0]


def _point_outputs(entry, options):
    root = Path(options.root).resolve() / "scales" / f"g{entry.scale}"
    if entry.system == "vanilla":
        base = root / "m2ndp"
        summary = base / "gem5/run/summary.csv"
        row = _read_single_csv(summary)
        validate_mechanism_row("vanilla", row)
        config = Path(row.get("run_dir", "")) / "config.ini"
        validate_config(config)
        rank = base / "reference/scores.raw"
        validate_rank_bits(rank, rank, expected_words=1 << entry.scale)
        return {
            "summary": _sha256_file(summary),
            "config": _sha256_file(config),
            "rank": _sha256_file(rank),
        }
    if entry.system in {"amu", "cira"}:
        base = root / entry.system
        summary = base / "summary.csv"
        row = _read_single_csv(summary)
        validate_mechanism_row(entry.system, row)
        run_dir = Path(row["run_dir"])
        validate_config(run_dir / "config.ini")
        evidence = base / "evidence.json"
        evidence_value = _load_json(evidence, f"{entry.system} evidence")
        variant_manifest = (
            Path(options.variants_build_root).resolve()
            / f"g{entry.scale}/manifest.json"
        )
        variant_value = _load_json(
            variant_manifest, f"g{entry.scale} variant manifest"
        )
        vanilla_build_manifest = root / "m2ndp/build/manifest.json"
        if (
            variant_value.get("baseline_manifest_sha256")
            != _sha256_file(vanilla_build_manifest)
        ):
            raise ScalingError(
                f"g{entry.scale} variant baseline differs from Vanilla build"
            )
        variant_rank = Path(
            evidence_value["runs"][entry.system]["reference_raw"]
        )
        vanilla_rank = root / "m2ndp/reference/scores.raw"
        validate_rank_bits(
            vanilla_rank,
            variant_rank,
            expected_words=1 << entry.scale,
        )
        return {
            "summary": _sha256_file(summary),
            "evidence": _sha256_file(evidence),
            "variant_manifest": _sha256_file(variant_manifest),
            "rank": _sha256_file(variant_rank),
        }
    base = root / "m2ndp"
    summary = base / "summary.csv"
    row = _read_single_csv(summary)
    # The publisher's individual parsers already enforce strict FuncSim,
    # calibration, and NDPSim gates; preserve those proof artifacts here.
    if row.get("verification") != "pass" or row.get("funcsim_strict") != "pass":
        raise ScalingError("M2NDP publication verification did not pass")
    calibration = _load_json(
        base / "calibration/calibration.json", "M2NDP calibration"
    )
    trace_meta = _load_json(base / "trace/trace.meta.json", "M2NDP trace")
    validate_mechanism_row("m2ndp", {
        "status": "ok",
        "verification": row.get("verification"),
        "funcsim_compared": row.get("funcsim_compared"),
        "funcsim_mismatched": 0,
        "calibration_pass": (
            "pass" if calibration.get("passed") is True else "fail"
        ),
        "calibration_residual_ns": calibration.get("residual_ns"),
        "calibration_link_period_ns": calibration.get("link_period_ns"),
        "kernel_launches": trace_meta.get("ndpsim_launches"),
    })
    validate_rank_bits(
        base / "reference/scores.raw",
        base / "funcsim/scores.u32",
        expected_words=1 << entry.scale,
    )
    status = _load_json(base / "status.json", "M2NDP stage state")
    required = ("funcsim", "calibration", "ndpsim", "publish")
    if any(status.get("stages", {}).get(stage, {}).get("status") != "passed"
           for stage in required):
        raise ScalingError("M2NDP required stages did not all pass")
    return {
        "summary": _sha256_file(summary),
        "rank": _sha256_file(base / "funcsim/scores.u32"),
        "status": _sha256_file(base / "status.json"),
    }


def _point_measurement(entry, options):
    """Return publisher-ready absolute time and causal evidence."""
    root = Path(options.root).resolve() / "scales" / f"g{entry.scale}"
    if entry.system == "vanilla":
        row = _read_single_csv(root / "m2ndp/gem5/run/summary.csv")
        validate_mechanism_row("vanilla", row)
        seconds = Decimal(_integer(row, "sim_ticks")) / Decimal(10**12)
    elif entry.system in {"amu", "cira"}:
        row = _read_single_csv(root / entry.system / "summary.csv")
        validate_mechanism_row(entry.system, row)
        seconds = Decimal(_integer(row, "sim_ticks")) / Decimal(10**12)
    else:
        row = _read_single_csv(root / "m2ndp/summary.csv")
        if row.get("verification") != "pass" or row.get("funcsim_strict") != "pass":
            raise ScalingError("M2NDP publisher mechanism evidence did not pass")
        seconds = _positive_decimal_value(
            row.get("m2ndp_seconds"), "M2NDP latency seconds"
        )
    seconds = _positive_decimal_value(seconds, f"{entry.key} latency seconds")
    mechanism = {
        str(name): str(value)
        for name, value in sorted(row.items())
        if value is not None
    }
    if mechanism.get("verification") != "pass":
        raise ScalingError(f"{entry.key} verification is not pass")
    return {
        "latency_seconds": str(seconds),
        "output_elements": 1 << entry.scale,
        "mechanism": mechanism,
    }


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inputs", type=Path, required=True)
    parser.add_argument("--calibration", type=Path, required=True)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--gem5", type=Path, required=True)
    parser.add_argument("--m5-library", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--cxlmemuring", type=Path, required=True)
    parser.add_argument("--m2ndp-root", type=Path, required=True)
    parser.add_argument("--variants-build-root", type=Path, required=True)
    parser.add_argument("--qualification", type=Path, required=True)
    parser.add_argument("--timeout", type=int, default=0)
    parser.add_argument("--resume", action="store_true")
    return parser.parse_args(argv)


def main(argv=None):
    options = parse_args(argv)
    state_path = Path(options.root).resolve() / "state.json"
    complete_path = Path(options.root).resolve() / "complete.json"
    failed_path = Path(options.root).resolve() / "failed.json"
    performance_hold_path = (
        Path(options.root).resolve() / "performance-hold.json"
    )
    try:
        load_inputs(options.inputs)
        if options.timeout < 0:
            raise ScalingError("--timeout must be nonnegative")
        expected = new_state(options)
        if state_path.exists():
            if not options.resume:
                raise ScalingError(f"state exists; use --resume: {state_path}")
            state = _load_json(state_path, "scaling state")
            if (
                state.get("code_sha256")
                == PRE_LAZY_VARIANT_CODE_SHA256
                and state.get("code_sha256") != expected["code_sha256"]
            ):
                state = migrate_pre_lazy_variant_state(
                    state, expected, options
                )
                contract.atomic_write_json(state_path, state)
            for field in (
                "schema",
                "profile",
                "code_sha256",
                "inputs_sha256",
                "calibration_sha256",
                "graph_set_sha256",
                "g20_graph_sha256",
                "gem5_sha256",
                "m5_library_sha256",
                "config_sha256",
                "qualification_sha256",
                "qualification_variant_manifest_sha256",
            ):
                if state.get(field) != expected[field]:
                    raise ScalingError("resume state identity differs")
        else:
            if options.resume:
                raise ScalingError("--resume requested but scaling state is missing")
            state = expected
            contract.atomic_write_json(state_path, state)
        stale_failure_cleared = False
        for entry in build_matrix():
            if state["points"][entry.key]["status"] == "passed":
                current_outputs = _point_outputs(entry, options)
                current_measurement = _point_measurement(entry, options)
                point = state["points"][entry.key]
                if (
                    current_outputs != point["outputs"]
                    or current_measurement["latency_seconds"]
                    != point.get("latency_seconds")
                    or current_measurement["output_elements"]
                    != point.get("output_elements")
                    or current_measurement["mechanism"]
                    != point.get("mechanism")
                ):
                    raise ScalingError(
                        f"{entry.key} passed outputs changed before resume"
                    )
                continue
            if not stale_failure_cleared:
                failed_path.unlink(missing_ok=True)
                stale_failure_cleared = True
            if needs_variant_build(entry):
                ensure_variants_for_scale(entry.scale, state, options)
            command = command_for(entry, options)
            completed = subprocess.run(command, cwd=REPO, check=False)
            if completed.returncode != 0:
                raise ScalingError(
                    f"{entry.key} exited {completed.returncode}"
                )
            record_pass(
                state, entry, _point_outputs(entry, options),
                **_point_measurement(entry, options),
            )
            contract.atomic_write_json(state_path, state)
        if not is_complete(state):
            raise ScalingError("scaling state stopped before 16/16 passed")
        gate = evaluate_performance_gate(state)
        state["performance_gate"] = gate
        if gate["status"] == "hold":
            state["status"] = "performance_hold"
            contract.atomic_write_json(state_path, state)
            contract.atomic_write_json(performance_hold_path, state)
            complete_path.unlink(missing_ok=True)
            failed_path.unlink(missing_ok=True)
            print(
                "SCALING_PERFORMANCE_HOLD "
                f"offenders={len(gate['offenders'])}"
            )
            return 0
        state["status"] = "complete"
        contract.atomic_write_json(state_path, state)
        contract.atomic_write_json(complete_path, state)
        performance_hold_path.unlink(missing_ok=True)
        failed_path.unlink(missing_ok=True)
        print(f"SCALING_COMPLETE points=16 manifest={complete_path}")
        return 0
    except (ScalingError, OSError, KeyError) as error:
        complete_path.unlink(missing_ok=True)
        failure = {"schema": 1, "status": "failed", "error": str(error)}
        contract.atomic_write_json(failed_path, failure)
        print(f"SCALING_FAILED error={error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
