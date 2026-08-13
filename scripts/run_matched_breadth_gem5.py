#!/usr/bin/env python3
# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

"""Build and run the common Vanilla/AMU/CIRA canonical-trace replay."""

import hashlib
import argparse
import configparser
import dataclasses
import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path

try:
    from scripts import canonical_work_trace as canonical
    from scripts import compare_gapbs_cxl_amu_cira as comparison
    from scripts import cross_system_contract as contract
    from scripts import lazy_work_trace as lazy
    from scripts import npb_lazy_trace as npb
    from scripts import stratified_timing as timing
except ImportError:
    import canonical_work_trace as canonical
    import compare_gapbs_cxl_amu_cira as comparison
    import cross_system_contract as contract
    import lazy_work_trace as lazy
    import npb_lazy_trace as npb
    import stratified_timing as timing


REPO = Path(__file__).resolve().parents[1]
SOURCE = REPO / "util/amu/matched_workloads/trace_replay.cc"
M5_LIBRARY = REPO / "util/m5/build/x86/out/libm5.a"
SYSTEMS = ("vanilla", "amu", "cira")
_CORE_SECTION = re.compile(r"^board\.processor\.cores([0-9]+)\.core$")
_ALLOCATION = re.compile(
    r"^TRACE_REPLAY_ALLOCATION logical_bytes=([0-9]+) "
    r"allocated_bytes=([0-9]+) all_memory_cxl=(true|false)$"
)
_NPB_PHASE_NAMES = {
    101: "cg_spmv",
    102: "cg_vector_update",
    103: "cg_dot",
    104: "cg_conj_grad",
    201: "mg_psinv",
    202: "mg_resid",
    203: "mg_rprj3",
    204: "mg_interp",
    205: "mg_norm2u3",
}


class ReplayError(RuntimeError):
    """A replay command or its causal mechanism evidence is invalid."""


@dataclasses.dataclass(frozen=True)
class MaterializedTrace:
    root: Path
    source_schema: int
    source_trace_sha256: str
    phase: int
    phase_name: str
    warmup_items: int
    measured_items: int
    measure_start_item: int
    fixed_event_records: int


def _sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def trace_identity_sha256(trace):
    """Return the frozen payload identity used to derive timing windows."""
    trace = Path(trace).resolve()
    eager = trace / "trace.meta.json"
    descriptor = trace / "trace.v2.json"
    if eager.is_file() and not descriptor.exists():
        return canonical.read_bundle(trace).meta["trace_sha256"]
    if descriptor.is_file() and not eager.exists():
        lazy.read_bundle(trace)
        return _sha256_file(descriptor)
    raise ReplayError("trace root must contain exactly one canonical schema")


def _eager_phase_identity(bundle, phase):
    phases = bundle.meta.get("phases")
    if not isinstance(phases, list) or not phases:
        raise ReplayError("eager trace phase metadata is invalid")
    if all(isinstance(row, dict) for row in phases):
        matches = [row for row in phases if row.get("id") == phase]
        if len(matches) != 1:
            raise ReplayError("selected eager phase is absent")
        row = matches[0]
        name = row.get("name")
        count = row.get("work_items")
    elif all(isinstance(row, str) and row for row in phases):
        phase_ids = sorted({operation.phase for operation in bundle.operations})
        if phase not in phase_ids or len(phase_ids) != len(phases):
            raise ReplayError("eager phase names do not match phase IDs")
        name = phases[phase_ids.index(phase)]
        count = bundle.meta.get("phase_work", {}).get(name)
    else:
        raise ReplayError("eager trace phase metadata is invalid")
    if not isinstance(name, str) or not name:
        raise ReplayError("eager trace phase name is invalid")
    if isinstance(count, bool) or not isinstance(count, int) or count <= 0:
        raise ReplayError("eager trace phase work count is invalid")
    return name, count


def _lazy_phase_identity(bundle, phase):
    name = _NPB_PHASE_NAMES.get(phase)
    if name is None:
        raise ReplayError("selected lazy phase has no canonical name")
    count = sum(
        invocation.work_items
        for invocation in bundle.invocations
        if invocation.phase == phase
    )
    if count <= 0:
        raise ReplayError("selected lazy phase is absent")
    return name, count


def _window_coordinates(manifest, *, trace_sha256, phase_name, work_items,
                        window_index):
    try:
        plan = timing.read_plan(manifest)
    except timing.TimingError as error:
        raise ReplayError(f"invalid timing-window manifest: {error}") from error
    if plan.trace_sha256 != trace_sha256:
        raise ReplayError("timing-window trace SHA-256 differs")
    if plan.phase != phase_name:
        raise ReplayError("timing-window phase identity differs")
    if plan.work_items != work_items:
        raise ReplayError("timing-window phase work count differs")
    if (
        isinstance(window_index, bool)
        or not isinstance(window_index, int)
        or window_index < 0
        or window_index >= len(plan.windows)
    ):
        raise ReplayError("timing-window index is outside the canonical plan")
    return plan.windows[window_index]


def _write_segment_payload(root, operations):
    root = Path(root).resolve()
    if root.exists():
        raise ReplayError(f"fresh materialized trace root required: {root}")
    root.mkdir(parents=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".trace.bin.", dir=root
    )
    digest = hashlib.sha256()
    count = 0
    try:
        with os.fdopen(descriptor, "wb") as stream:
            for operation in operations:
                if not isinstance(operation, canonical.Operation):
                    raise ReplayError("materializer emitted a non-operation")
                sequenced = dataclasses.replace(operation, sequence=count)
                payload = canonical.TRACE_STRUCT.pack(
                    sequenced.phase, int(sequenced.opcode), 0,
                    sequenced.work_item, sequenced.sequence,
                    sequenced.address, sequenced.operand0,
                    sequenced.operand1, sequenced.result,
                )
                stream.write(payload)
                digest.update(payload)
                count += 1
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, root / "trace.bin")
    except BaseException:
        Path(temporary_name).unlink(missing_ok=True)
        raise
    if count == 0:
        raise ReplayError("selected timing window has no canonical operations")
    return digest.hexdigest(), count


def materialize_window_trace(trace, *, manifest, phase, window_index, outdir):
    """Stream one canonical warmup+measure window into a bounded schema-1 trace."""
    trace = Path(trace).resolve()
    source_sha256 = trace_identity_sha256(trace)
    state = {"fixed": 0, "expanded": 0, "phase_items": 0}

    if (trace / "trace.meta.json").is_file():
        source_schema = 1
        source = canonical.read_bundle(trace)
        phase_name, phase_items = _eager_phase_identity(source, phase)
        window = _window_coordinates(
            manifest, trace_sha256=source_sha256, phase_name=phase_name,
            work_items=phase_items, window_index=window_index,
        )

        def selected_operations():
            for operation in source.operations:
                if operation.phase != phase:
                    continue
                if operation.opcode in {
                    canonical.Opcode.BARRIER, canonical.Opcode.COMMIT,
                }:
                    state["fixed"] += 1
                    continue
                if window.warmup_start <= operation.work_item < window.measure_stop:
                    yield dataclasses.replace(
                        operation,
                        work_item=operation.work_item - window.warmup_start,
                    )

        source_meta = source.meta
        operations = selected_operations()
    else:
        source_schema = 2
        source = lazy.read_bundle(trace)
        phase_name, phase_items = _lazy_phase_identity(source, phase)
        window = _window_coordinates(
            manifest, trace_sha256=source_sha256, phase_name=phase_name,
            work_items=phase_items, window_index=window_index,
        )

        def selected_operations():
            phase_base = 0
            expanded = 0
            with lazy.MappedState(source) as mapped:
                for invocation in source.invocations:
                    try:
                        expander = npb.EXPANDERS[invocation.kernel]
                    except KeyError as error:
                        raise ReplayError(
                            f"unknown lazy replay kernel {invocation.kernel}"
                        ) from error
                    for operation in expander(mapped, invocation, 1024):
                        lazy._validate_expanded_operation(
                            source, invocation, operation
                        )
                        expanded += 1
                        if invocation.phase != phase:
                            continue
                        if operation.opcode in {
                            canonical.Opcode.BARRIER,
                            canonical.Opcode.COMMIT,
                        }:
                            state["fixed"] += 1
                            continue
                        if operation.work_item >= invocation.work_items:
                            state["fixed"] += 1
                            continue
                        global_item = phase_base + operation.work_item
                        if window.warmup_start <= global_item < window.measure_stop:
                            yield dataclasses.replace(
                                operation,
                                work_item=global_item - window.warmup_start,
                            )
                    if invocation.phase == phase:
                        phase_base += invocation.work_items
            state["expanded"] = expanded
            state["phase_items"] = phase_base
            if expanded != source.dynamic_work["primitive_records"]:
                raise ReplayError("lazy dynamic primitive count differs")
            if phase_base != phase_items:
                raise ReplayError("lazy phase work count changed during expansion")

        source_meta = source.meta
        operations = selected_operations()

    trace_sha256, trace_records = _write_segment_payload(outdir, operations)
    warmup_items = window.measure_start - window.warmup_start
    measured_items = window.measure_stop - window.measure_start
    meta = {
        "schema": 1,
        "workload": source_meta["workload"],
        "input_sha256": source_meta.get("input_sha256", source_sha256),
        "source_sha256": source_meta["source_sha256"],
        "binary_sha256": source_meta["binary_sha256"],
        "config_sha256": source_meta["config_sha256"],
        "phases": [{"id": phase, "name": phase_name,
                    "work_items": warmup_items + measured_items}],
        "output_boundaries": {},
        "source_schema": source_schema,
        "source_trace_sha256": source_sha256,
        "source_phase_work_items": phase_items,
        "window_index": window_index,
        "warmup_start": window.warmup_start,
        "measure_start": window.measure_start,
        "measure_stop": window.measure_stop,
        "measure_start_item": warmup_items,
        "fixed_event_records": state["fixed"],
        "trace_path": "trace.bin",
        "trace_sha256": trace_sha256,
        "trace_record_bytes": canonical.TRACE_STRUCT.size,
        "trace_records": trace_records,
        "outputs": {},
    }
    contract.atomic_write_json(Path(outdir) / "trace.meta.json", meta)
    canonical.read_bundle(outdir)
    return MaterializedTrace(
        Path(outdir).resolve(), source_schema, source_sha256, phase,
        phase_name, warmup_items, measured_items, warmup_items, state["fixed"],
    )


def build_replay_binary(outdir, *, native=False, cxx="g++"):
    outdir = Path(outdir).resolve()
    if outdir.exists():
        raise ReplayError(f"replay build root already exists: {outdir}")
    if shutil.which(cxx) is None:
        raise ReplayError(f"C++ compiler is unavailable: {cxx}")
    outdir.mkdir(parents=True)
    binary = outdir / "trace_replay"
    command = [
        cxx,
        "-std=c++17",
        "-O3",
        "-fopenmp",
        "-ffp-contract=off",
        "-fno-fast-math",
        "-Wall",
        "-Wextra",
        "-Werror",
        "-I",
        str(REPO),
        "-I",
        str(REPO / "include"),
    ]
    if native:
        command.append("-DTRACE_REPLAY_NATIVE=1")
    else:
        command.extend(("-static", "-no-pie"))
    command.append(str(SOURCE))
    if not native:
        if not M5_LIBRARY.is_file():
            raise ReplayError(f"checked-in m5 ABI library is missing: {M5_LIBRARY}")
        command.append(str(M5_LIBRARY))
    command.extend(("-o", str(binary)))
    try:
        subprocess.run(
            command,
            cwd=REPO,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except subprocess.CalledProcessError as error:
        raise ReplayError(
            f"trace replay build failed: {error.stderr.strip()}"
        ) from error
    manifest = {
        "schema": 1,
        "native": bool(native),
        "source": str(SOURCE),
        "source_sha256": _sha256_file(SOURCE),
        "binary": str(binary),
        "binary_sha256": _sha256_file(binary),
        "command": command,
    }
    if not native:
        manifest["m5_library"] = str(M5_LIBRARY)
        manifest["m5_library_sha256"] = _sha256_file(M5_LIBRARY)
    (outdir / "build.json").write_text(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    return binary


def run_native_replay(binary, *, system, trace, outdir):
    if system not in SYSTEMS:
        raise ReplayError(f"unsupported replay system: {system}")
    binary = Path(binary).resolve()
    trace = Path(trace).resolve()
    outdir = Path(outdir).resolve()
    if outdir.exists():
        raise ReplayError(f"replay output root already exists: {outdir}")
    bundle = canonical.read_bundle(trace)
    outdir.mkdir(parents=True)
    result = outdir / "result.json"
    command = [
        str(binary),
        "--system",
        system,
        "--trace",
        str(trace / "trace.bin"),
        "--result",
        str(result),
    ]
    try:
        subprocess.run(
            command,
            cwd=REPO,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env={"OMP_NUM_THREADS": "4"},
        )
    except subprocess.CalledProcessError as error:
        raise ReplayError(
            f"{system} native replay failed: {error.stderr.strip()}"
        ) from error
    try:
        value = json.loads(result.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ReplayError(f"invalid native replay result: {error}") from error
    if value.get("trace_records") != len(bundle.operations):
        raise ReplayError("native replay record count differs from trace")
    if value.get("verification") != "pass":
        raise ReplayError("native replay bit-exact verification failed")
    return value


def _integer(row, field):
    value = row.get(field)
    if isinstance(value, bool):
        raise ReplayError(f"{field} must be an integer")
    try:
        result = int(value)
    except (TypeError, ValueError) as error:
        raise ReplayError(f"{field} must be an integer") from error
    if result != value or result < 0:
        raise ReplayError(f"{field} must be a nonnegative integer")
    return result


def validate_mechanism(system, row):
    """Fail closed on topology, correctness, or mechanism-counter drift."""
    if system not in SYSTEMS:
        raise ReplayError(f"unsupported replay system: {system}")
    if row.get("verification") != "pass":
        raise ReplayError(f"{system} bit-exact verification did not pass")
    if _integer(row, "threads") != 4:
        raise ReplayError("matched replay requires four threads")
    if row.get("all_memory_cxl") is not True:
        raise ReplayError("matched replay requires all-CXL memory")
    if row.get("allocated_on_cxl") is not True:
        raise ReplayError("matched replay allocation is not on CXL")
    if _integer(row, "cxl_link_delay_ticks") != 1_000_000:
        raise ReplayError("matched replay requires a 1 us CXL link")
    if _integer(row, "queue_errors"):
        raise ReplayError(f"{system} queue errors are nonzero")
    if _integer(row, "descriptor_errors"):
        raise ReplayError(f"{system} descriptor errors are nonzero")

    if system == "amu":
        issued = _integer(row, "issued_loads")
        completed = _integer(row, "completed_loads")
        if issued == 0 or issued != completed:
            raise ReplayError("AMU issued/completed loads differ")
        drains = _integer(row, "drains")
        phases = _integer(row, "phases")
        if drains > phases * _integer(row, "threads"):
            raise ReplayError("AMU per-request drain is forbidden")
    elif system == "cira":
        issued = _integer(row, "issued_prefetches")
        completed = _integer(row, "completed_prefetches")
        if issued == 0 or issued != completed:
            raise ReplayError("CIRA issued/completed prefetches differ")
        issued_per_core = row.get("issued_per_core")
        completed_per_core = row.get("completed_per_core")
        if (
            not isinstance(issued_per_core, list)
            or len(issued_per_core) != 4
            or any(_integer({"value": value}, "value") == 0
                   for value in issued_per_core)
        ):
            raise ReplayError("CIRA requires four active cores")
        if issued_per_core != completed_per_core:
            raise ReplayError("CIRA per-core issued/completed work differs")
    return row


def validate_config_ini(path):
    """Prove the generated gem5 topology is four-core, timing, and all CXL."""
    path = Path(path)
    parser = configparser.ConfigParser(interpolation=None, strict=True)
    parser.optionxform = str
    try:
        with path.open("r", encoding="utf-8") as stream:
            parser.read_file(stream)
    except (OSError, UnicodeDecodeError, configparser.Error) as error:
        raise ReplayError(f"cannot parse gem5 config.ini: {error}") from error
    if not parser.has_section("board"):
        raise ReplayError("gem5 config has no board section")
    if parser.get("board", "mem_mode", fallback="") != "timing":
        raise ReplayError("matched replay requires timing memory mode")

    cores = []
    for section in parser.sections():
        match = _CORE_SECTION.fullmatch(section)
        if match is not None:
            cores.append(int(match.group(1)))
    if sorted(cores) != [0, 1, 2, 3]:
        raise ReplayError("gem5 config does not contain exactly four cores")
    for core in cores:
        cpu_type = parser.get(
            f"board.processor.cores{core}.core", "type", fallback=""
        )
        if "Timing" not in cpu_type and "O3" not in cpu_type:
            raise ReplayError("gem5 config core is not a timing CPU")

    board_ranges = parser.get("board", "mem_ranges", fallback="").split()
    links = []
    for section in parser.sections():
        if parser.get(section, "type", fallback="") != "SerialLink":
            continue
        ranges = parser.get(section, "ranges", fallback="").split()
        if ranges and set(ranges).intersection(board_ranges):
            links.append(section)
    if not links:
        raise ReplayError("gem5 config has no CXL SerialLink memory route")
    delays = {
        int(parser.get(section, "delay", fallback="-1")) for section in links
    }
    if delays != {1_000_000}:
        raise ReplayError("matched replay requires a 1 us CXL link")
    covered = set()
    link_ports = set()

    def reaches_memory(port, visited):
        if ".memory." in port and "mem_ctrl" in port:
            return True
        section = port.split(".cpu_side", 1)[0].split(".mem_side", 1)[0]
        if section in visited or not parser.has_section(section):
            return False
        visited.add(section)
        if parser.get(section, "type", fallback="") not in {
            "NoncoherentXBar", "CoherentXBar",
        }:
            return False
        return any(
            reaches_memory(destination, visited)
            for destination in parser.get(
                section, "mem_side_ports", fallback=""
            ).split()
        )

    for section in links:
        covered.update(parser.get(section, "ranges", fallback="").split())
        link_ports.add(f"{section}.cpu_side_port")
        destination = parser.get(section, "mem_side_port", fallback="")
        if not reaches_memory(destination, set()):
            raise ReplayError("CXL link does not terminate at memory")
    if set(board_ranges) - covered:
        raise ReplayError("CXL links do not cover every board memory range")

    membus = "board.cache_hierarchy.membus"
    if not parser.has_section(membus):
        raise ReplayError("gem5 config has no coherent memory bus")
    destinations = parser.get(membus, "mem_side_ports", fallback="").split()
    if not link_ports.issubset(destinations):
        raise ReplayError("CXL memory links are not attached to the memory bus")
    if any(".memory." in port or "mem_ctrl" in port for port in destinations):
        raise ReplayError("a memory-controller path bypasses CXL")
    return {
        "threads": 4,
        "all_memory_cxl": True,
        "cxl_link_delay_ticks": 1_000_000,
        "cxl_links": links,
        "memory_ranges": board_ranges,
    }


def parse_allocation_log(path, *, required_bytes):
    if isinstance(required_bytes, bool) or not isinstance(required_bytes, int):
        raise ReplayError("required allocation bytes must be an integer")
    if required_bytes <= 0:
        raise ReplayError("required allocation bytes must be positive")
    try:
        lines = Path(path).read_text(
            encoding="utf-8", errors="strict"
        ).splitlines()
    except (OSError, UnicodeDecodeError) as error:
        raise ReplayError(f"cannot read replay allocation log: {error}") from error
    matches = [match for line in lines if (match := _ALLOCATION.fullmatch(line))]
    if len(matches) != 1:
        raise ReplayError("replay allocation log must contain one marker")
    logical_bytes = int(matches[0].group(1))
    allocated_bytes = int(matches[0].group(2))
    on_cxl = matches[0].group(3) == "true"
    if logical_bytes < required_bytes or allocated_bytes < logical_bytes:
        raise ReplayError("replay allocation does not cover canonical state")
    if not on_cxl:
        raise ReplayError("replay allocation is not marked all-CXL")
    return {
        "logical_bytes": logical_bytes,
        "allocated_bytes": allocated_bytes,
        "allocated_on_cxl": True,
    }


def command_for(options):
    if options.system not in SYSTEMS:
        raise ReplayError(f"unsupported replay system: {options.system}")
    if options.mode not in {"functional", "window"}:
        raise ReplayError(f"unsupported replay mode: {options.mode}")
    trace = Path(options.trace).resolve()
    trace_file = Path(
        getattr(options, "replay_trace", trace / "trace.bin")
    ).resolve()
    binary_args = [
        "--system", options.system,
        "--trace", str(trace_file),
        "--result", str((Path(options.outdir).resolve() / "result.json")),
        "--mode", options.mode,
    ]
    if options.mode == "functional":
        if any(
            value is not None
            for value in (
                options.window_manifest, options.phase, options.window_index
            )
        ):
            raise ReplayError("functional replay may not select a timing window")
    else:
        if (
            options.window_manifest is None
            or options.phase is None
            or options.window_index is None
        ):
            raise ReplayError("window replay requires manifest, phase, and index")
        binary_args.extend((
            "--window-manifest", str(Path(options.window_manifest).resolve()),
            "--phase", str(options.phase),
            "--window-index", str(options.window_index),
            "--measure-start-item",
            str(getattr(options, "measure_start_item", 0)),
        ))

    command = [
        str(Path(options.gem5).resolve()),
        "--redirect-stdout",
        "--redirect-stderr",
        "--stdout-file=simout",
        "--stderr-file=simerr",
        "-d", str(Path(options.outdir).resolve()),
        str(Path(options.config).resolve()),
        "--binary", str(Path(options.binary).resolve()),
        "--arguments", shlex.join(binary_args),
        "--cores", "4",
        "--cpu", "timing",
        "--cxl-memory",
        "--cxl-link-delay", "1us",
        "--require-m5-verification-exit",
    ]
    if options.system == "amu":
        command.extend((
            "--asmc-profile", "paper-calibrated",
            "--asmc-calibration-manifest",
            str(Path(options.calibration).resolve()),
            "--asmc-spm-size", "64KiB",
        ))
    else:
        command.append("--no-asmc")
    if options.system == "cira":
        command.extend(("--cira", "--cira-to-l2"))
    if options.mode == "window":
        command.extend(("--roi-work-events", "--continue-after-roi"))
    return command


def _load_json(path, label):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ReplayError(f"cannot read {label}: {error}") from error
    if not isinstance(value, dict):
        raise ReplayError(f"{label} must be a JSON object")
    return value


def _stat_integer(stats, name):
    if name not in stats:
        raise ReplayError(f"missing required gem5 statistic: {name}")
    value = stats[name]
    integer = int(value)
    if value != integer or integer < 0:
        raise ReplayError(f"gem5 statistic is not a nonnegative integer: {name}")
    return integer


def _expected_commits(bundle):
    commits = [
        operation for operation in bundle.operations
        if operation.opcode == canonical.Opcode.COMMIT
    ]
    return (
        [operation.sequence for operation in commits],
        [operation.result for operation in commits],
    )


def _required_shadow_bytes(bundle):
    memory_opcodes = {
        canonical.Opcode.LOAD_U32, canonical.Opcode.LOAD_U64,
        canonical.Opcode.LOAD_F32, canonical.Opcode.LOAD_F64,
        canonical.Opcode.STORE_U32, canonical.Opcode.STORE_U64,
        canonical.Opcode.STORE_F32, canonical.Opcode.STORE_F64,
    }
    return 64 * len({
        operation.address & ~63 for operation in bundle.operations
        if operation.opcode in memory_opcodes
    })


def collect_run_evidence(run_dir, *, system, trace, config):
    """Join bit-exact program output with gem5-owned causal statistics."""
    if system not in SYSTEMS:
        raise ReplayError(f"unsupported replay system: {system}")
    run_dir = Path(run_dir).resolve()
    bundle = canonical.read_bundle(Path(trace).resolve())
    result = _load_json(run_dir / "result.json", "replay result")
    expected_order, expected_raw = _expected_commits(bundle)
    if result.get("commit_order") != expected_order:
        raise ReplayError("replay commit order differs from canonical trace")
    if result.get("raw_outputs") != expected_raw:
        raise ReplayError("replay raw output differs from canonical trace")
    if result.get("verification") != "pass":
        raise ReplayError("replay program verification did not pass")

    topology = validate_config_ini(config)
    required_bytes = _required_shadow_bytes(bundle)
    allocation = (
        parse_allocation_log(run_dir / "simout", required_bytes=required_bytes)
        if required_bytes
        else {"logical_bytes": 0, "allocated_bytes": 0,
              "allocated_on_cxl": True}
    )
    try:
        stats = comparison.parse_stats(run_dir / "stats.txt")
    except comparison.StatsError as error:
        raise ReplayError(str(error)) from error
    sim_ticks = _stat_integer(stats, "simTicks")
    row = {
        "verification": "pass",
        "threads": _integer(result, "threads"),
        "phases": _integer(result, "phases"),
        "all_memory_cxl": topology["all_memory_cxl"],
        "cxl_link_delay_ticks": topology["cxl_link_delay_ticks"],
        "allocated_on_cxl": allocation["allocated_on_cxl"],
        "allocated_bytes": allocation["allocated_bytes"],
        "logical_bytes": allocation["logical_bytes"],
        "raw_outputs": result["raw_outputs"],
        "commit_order": result["commit_order"],
        "sim_ticks": sim_ticks,
        "queue_errors": 0,
        "descriptor_errors": 0,
    }
    if system == "amu":
        row.update({
            "issued_loads": _stat_integer(
                stats, "board.asmc.issuedLoads"
            ),
            "completed_loads": _stat_integer(
                stats, "board.asmc.completedLoads"
            ),
            "drains": _integer(result, "drains"),
        })
        row["queue_errors"] = sum(
            _stat_integer(stats, name)
            for name in (
                "board.asmc.rejectedQueueFull",
                "board.asmc.rejectedSpmFull",
                "board.asmc.translationFaults",
                "board.asmc.pendingQueueFull",
                "board.asmc.farSpmFlagPackets",
                "board.asmc.spmMissingFlagPackets",
            )
        )
    elif system == "cira":
        row.update({
            "issued_prefetches": _stat_integer(
                stats, "board.cira.issuedPrefetches"
            ),
            "completed_prefetches": _stat_integer(
                stats, "board.cira.completedPrefetches"
            ),
            "issued_per_core": [
                _stat_integer(stats, f"board.cira.issuedPrefetchesPerCore::{core}")
                for core in range(4)
            ],
            "completed_per_core": [
                _stat_integer(
                    stats, f"board.cira.completedPrefetchesPerCore::{core}"
                )
                for core in range(4)
            ],
        })
        row["queue_errors"] = _stat_integer(
            stats, "board.cira.rejectedQueueFull"
        ) + _stat_integer(stats, "board.cira.rejectedCsrIndexQueueFull")
        row["descriptor_errors"] = _stat_integer(
            stats, "board.cira.droppedCsrDescriptors"
        )
    validate_mechanism(system, row)
    return row


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Run one matched Vanilla/AMU/CIRA canonical replay."
    )
    parser.add_argument("--mode", choices=("functional", "window"), required=True)
    parser.add_argument("--system", choices=SYSTEMS, required=True)
    parser.add_argument("--trace", type=Path, required=True)
    parser.add_argument("--window-manifest", type=Path)
    parser.add_argument("--phase", type=int)
    parser.add_argument("--window-index", type=int)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--gem5", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--calibration", type=Path, required=True)
    parser.add_argument("--outdir", type=Path, required=True)
    parser.add_argument("--timeout", type=int, default=0)
    options = parser.parse_args(argv)
    selected = (
        options.window_manifest, options.phase, options.window_index
    )
    if options.mode == "functional" and any(value is not None for value in selected):
        parser.error("functional replay may not select a timing window")
    if options.mode == "window" and any(value is None for value in selected):
        parser.error("window replay requires manifest, phase, and index")
    if options.phase is not None and options.phase < 0:
        parser.error("--phase must be nonnegative")
    if options.window_index is not None and options.window_index < 0:
        parser.error("--window-index must be nonnegative")
    if options.timeout < 0:
        parser.error("--timeout must be nonnegative")
    return options


def run(options):
    outdir = Path(options.outdir).resolve()
    if outdir.exists():
        raise ReplayError(f"fresh replay output root required: {outdir}")
    for label in ("binary", "gem5", "config"):
        path = Path(getattr(options, label)).resolve()
        if not path.is_file():
            raise ReplayError(f"replay {label} is missing: {path}")
    trace = Path(options.trace).resolve()
    materialized = None
    replay_trace = trace
    if options.mode == "window":
        materialized = materialize_window_trace(
            trace,
            manifest=Path(options.window_manifest).resolve(),
            phase=options.phase,
            window_index=options.window_index,
            outdir=outdir.with_name(outdir.name + ".input"),
        )
        replay_trace = materialized.root
    elif (trace / "trace.v2.json").is_file():
        raise ReplayError(
            "schema-2 functional replay requires the bounded streaming engine"
        )
    else:
        try:
            canonical.read_bundle(trace)
        except canonical.TraceError as error:
            raise ReplayError(
                f"invalid canonical replay trace: {error}"
            ) from error
    if options.mode == "window":
        manifest = Path(options.window_manifest).resolve()
        if not manifest.is_file():
            raise ReplayError(f"window manifest is missing: {manifest}")
    run_options = argparse.Namespace(**vars(options))
    run_options.replay_trace = replay_trace / "trace.bin"
    if materialized is not None:
        run_options.measure_start_item = materialized.measure_start_item
    command = command_for(run_options)
    outdir.parent.mkdir(parents=True, exist_ok=True)
    driver_log = outdir.with_suffix(".driver.log")
    try:
        with driver_log.open("x", encoding="utf-8") as stream:
            completed = subprocess.run(
                command,
                cwd=REPO,
                stdout=stream,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=None if options.timeout == 0 else options.timeout,
            )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ReplayError(f"gem5 replay launch failed: {error}") from error
    if completed.returncode != 0:
        raise ReplayError(
            f"gem5 replay exited {completed.returncode}; see {driver_log}"
        )
    row = collect_run_evidence(
        outdir, system=options.system, trace=replay_trace,
        config=outdir / "config.ini",
    )
    source_descriptor = (
        trace / "trace.v2.json"
        if (trace / "trace.v2.json").is_file()
        else trace / "trace.meta.json"
    )
    evidence = {
        "schema": 1,
        "status": "pass",
        "mode": options.mode,
        "system": options.system,
        "trace": str(trace),
        "trace_meta_sha256": _sha256_file(source_descriptor),
        "trace_identity_sha256": trace_identity_sha256(trace),
        "binary_sha256": _sha256_file(options.binary),
        "gem5_sha256": _sha256_file(options.gem5),
        "config_sha256": _sha256_file(outdir / "config.ini"),
        "command": command,
        "row": row,
    }
    if materialized is not None:
        evidence["materialized_window"] = {
            **dataclasses.asdict(materialized),
            "root": str(materialized.root),
            "trace_sha256": _sha256_file(replay_trace / "trace.bin"),
        }
    (outdir / "evidence.json").write_text(
        json.dumps(evidence, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    return evidence


def main(argv=None):
    try:
        run(parse_args(argv))
    except ReplayError as error:
        print(f"MATCHED_REPLAY_FAILED {error}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
