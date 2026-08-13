# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

import hashlib
import json
import shlex
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from scripts import canonical_work_trace as canonical
from scripts import lazy_work_trace as lazy
from scripts import npb_lazy_trace as npb
from scripts import run_matched_breadth_gem5 as replay
from scripts import stratified_timing as timing


def _digest(label):
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _meta(name, records):
    return {
        "schema": 1,
        "workload": name,
        "input_sha256": _digest(f"{name}-input"),
        "source_sha256": _digest(f"{name}-source"),
        "binary_sha256": _digest(f"{name}-binary"),
        "config_sha256": _digest(f"{name}-config"),
        "phases": [{"id": 0, "name": name, "work_items": records}],
        "output_boundaries": {},
    }


def _op(opcode, sequence, *, work_item=0, address=0, left=0, right=0,
        result=0):
    return canonical.Operation(
        phase=0,
        opcode=opcode,
        work_item=work_item,
        sequence=sequence,
        address=address,
        operand0=left,
        operand1=right,
        result=result,
    )


def _fixtures():
    f32_one = 0x3F800000
    f32_two = 0x40000000
    f64_one = 0x3FF0000000000000
    f64_two = 0x4000000000000000
    return {
        "gather": (
            _op(canonical.Opcode.LOAD_F32, 0, address=0x1000,
                left=f32_one, result=f32_one),
            _op(canonical.Opcode.LOAD_F32, 1, address=0x1080,
                left=f32_two, result=f32_two),
            _op(canonical.Opcode.F32_ADD, 2, left=f32_one, right=f32_two,
                result=0x40400000),
            _op(canonical.Opcode.COMMIT, 3, address=0x2000,
                left=0x40400000, result=0x40400000),
        ),
        "duplicate_scatter": (
            _op(canonical.Opcode.STORE_U64, 0, work_item=0, address=0x3000,
                left=7, result=7),
            _op(canonical.Opcode.COMMIT, 1, work_item=0, address=0x3000,
                left=7, result=7),
            _op(canonical.Opcode.STORE_U64, 2, work_item=1, address=0x3000,
                left=9, result=9),
            _op(canonical.Opcode.COMMIT, 3, work_item=1, address=0x3000,
                left=9, result=9),
        ),
        "pointer_chain": (
            _op(canonical.Opcode.LOAD_U64, 0, address=0x4000,
                left=0x4080, result=0x4080),
            _op(canonical.Opcode.LOAD_U64, 1, address=0x4080,
                left=0x4100, result=0x4100),
            _op(canonical.Opcode.LOAD_U64, 2, address=0x4100,
                left=23, result=23),
            _op(canonical.Opcode.COMMIT, 3, address=0x5000,
                left=23, result=23),
        ),
        "fixed_reduction": (
            _op(canonical.Opcode.F64_ADD, 0, work_item=0,
                left=f64_one, right=f64_two, result=0x4008000000000000),
            _op(canonical.Opcode.F64_ADD, 1, work_item=1,
                left=f64_two, right=f64_one, result=0x4008000000000000),
            _op(canonical.Opcode.F64_ADD, 2, work_item=2,
                left=0x4008000000000000, right=0x4008000000000000,
                result=0x4018000000000000),
            _op(canonical.Opcode.COMMIT, 3, work_item=2, address=0x6000,
                left=0x4018000000000000, result=0x4018000000000000),
        ),
    }


class MatchedBreadthGem5Test(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_native_replay_is_bit_exact_for_all_backends_and_regions(self):
        binary = replay.build_replay_binary(self.root / "build", native=True)
        for name, operations in _fixtures().items():
            with self.subTest(region=name):
                bundle = self.root / name
                canonical.write_bundle(bundle, _meta(name, len(operations)),
                                       operations, {})
                observed = {
                    system: replay.run_native_replay(
                        binary, system=system, trace=bundle,
                        outdir=self.root / f"{name}-{system}",
                    )
                    for system in ("vanilla", "amu", "cira")
                }
                reference = observed["vanilla"]
                for system in ("amu", "cira"):
                    self.assertEqual(
                        observed[system]["raw_outputs"],
                        reference["raw_outputs"],
                    )
                    self.assertEqual(
                        observed[system]["commit_order"],
                        reference["commit_order"],
                    )
                self.assertEqual(reference["verification"], "pass")

    def test_native_engine_uses_four_workers_and_real_load_windows(self):
        operations = []
        for work_item in range(4):
            first = 2 * work_item + 1
            second = first + 1
            operations.extend((
                _op(
                    canonical.Opcode.LOAD_U64,
                    len(operations),
                    work_item=work_item,
                    address=0x7000 + work_item * 0x100,
                    left=first,
                    result=first,
                ),
                _op(
                    canonical.Opcode.LOAD_U64,
                    len(operations) + 1,
                    work_item=work_item,
                    address=0x7080 + work_item * 0x100,
                    left=second,
                    result=second,
                ),
                _op(
                    canonical.Opcode.COMMIT,
                    len(operations) + 2,
                    work_item=work_item,
                    address=0x8000 + work_item * 8,
                    left=second,
                    result=second,
                ),
            ))
        bundle = self.root / "four-workers"
        canonical.write_bundle(
            bundle, _meta("four_workers", 4), tuple(operations), {}
        )
        binary = replay.build_replay_binary(self.root / "build", native=True)
        amu = replay.run_native_replay(
            binary, system="amu", trace=bundle, outdir=self.root / "amu"
        )
        cira = replay.run_native_replay(
            binary, system="cira", trace=bundle, outdir=self.root / "cira"
        )
        self.assertGreaterEqual(amu["max_observed_outstanding"], 2)
        self.assertLess(amu["drains"], amu["issued_loads"])
        self.assertEqual(amu["issued_loads"], amu["completed_loads"])
        self.assertEqual(cira["worker_threads"], [0, 1, 2, 3])
        self.assertTrue(all(value > 0 for value in cira["issued_per_core"]))
        self.assertEqual(
            cira["issued_per_core"], cira["completed_per_core"]
        )

    def test_amu_issues_across_interleaved_compute_until_consumer_reach(self):
        operations = (
            _op(canonical.Opcode.LOAD_U64, 0, address=0x7700,
                left=5, result=5),
            _op(canonical.Opcode.I64_ADD, 1, left=5, right=1, result=6),
            _op(canonical.Opcode.LOAD_U64, 2, address=0x7780,
                left=7, result=7),
            _op(canonical.Opcode.I64_ADD, 3, left=7, right=1, result=8),
            _op(canonical.Opcode.COMMIT, 4, address=0x7800,
                left=8, result=8),
        )
        bundle = self.root / "interleaved-window"
        canonical.write_bundle(
            bundle, _meta("interleaved_window", 1), operations, {}
        )
        binary = replay.build_replay_binary(self.root / "build", native=True)
        result = replay.run_native_replay(
            binary, system="amu", trace=bundle,
            outdir=self.root / "interleaved-window-run",
        )
        self.assertGreaterEqual(result["max_observed_outstanding"], 2)
        self.assertEqual(result["raw_outputs"], [8])

    def test_native_replay_preserves_logical_cache_line_layout(self):
        operations = (
            _op(canonical.Opcode.LOAD_U64, 0, address=0x7100,
                left=11, result=11),
            _op(canonical.Opcode.LOAD_U64, 1, address=0x7108,
                left=13, result=13),
            _op(canonical.Opcode.COMMIT, 2, address=0x7200,
                left=13, result=13),
        )
        bundle = self.root / "same-cache-line"
        canonical.write_bundle(
            bundle, _meta("same_cache_line", 1), operations, {}
        )
        binary = replay.build_replay_binary(self.root / "build", native=True)
        result = replay.run_native_replay(
            binary, system="vanilla", trace=bundle,
            outdir=self.root / "same-cache-line-run",
        )
        self.assertEqual(result["allocated_bytes"], 64)
        self.assertEqual(
            replay._required_shadow_bytes(canonical.read_bundle(bundle)), 64
        )
        self.assertEqual(result["raw_outputs"], [13])

    def test_replay_binary_accepts_functional_and_window_selection(self):
        operations = (
            _op(canonical.Opcode.COMMIT, 0, address=0x9000,
                left=17, result=17),
        )
        bundle = self.root / "selection"
        canonical.write_bundle(bundle, _meta("selection", 1), operations, {})
        binary = replay.build_replay_binary(self.root / "build", native=True)
        manifest = self.root / "windows.json"
        manifest.write_text('{"schema":1}\n', encoding="utf-8")
        for label, extra in (
            ("functional", ["--mode", "functional"]),
            ("window", [
                "--mode", "window", "--window-manifest", str(manifest),
                "--phase", "0", "--window-index", "2",
                "--measure-start-item", "0",
            ]),
        ):
            result = self.root / f"{label}.json"
            command = [
                str(binary), "--system", "vanilla", "--trace",
                str(bundle / "trace.bin"), "--result", str(result), *extra,
            ]
            with self.subTest(mode=label):
                subprocess.run(command, check=True, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True)
                value = json.loads(result.read_text(encoding="utf-8"))
                self.assertEqual(value["mode"], label)

    def test_window_warmup_is_executed_but_excluded_from_measured_stats(self):
        operations = (
            _op(canonical.Opcode.LOAD_U64, 0, work_item=0,
                address=0x7500, left=41, result=41),
            _op(canonical.Opcode.COMMIT, 1, work_item=0,
                address=0x7600, left=41, result=41),
            _op(canonical.Opcode.LOAD_U64, 2, work_item=1,
                address=0x7580, left=43, result=43),
            _op(canonical.Opcode.COMMIT, 3, work_item=1,
                address=0x7608, left=43, result=43),
        )
        bundle = self.root / "warmup-measure"
        canonical.write_bundle(
            bundle, _meta("warmup_measure", 2), operations, {}
        )
        binary = replay.build_replay_binary(self.root / "build", native=True)
        manifest = self.root / "warmup-measure-windows.json"
        manifest.write_text('{"schema":1}\n', encoding="utf-8")
        result = self.root / "warmup-measure-result.json"
        subprocess.run([
            str(binary), "--system", "amu", "--trace",
            str(bundle / "trace.bin"), "--result", str(result),
            "--mode", "window", "--window-manifest", str(manifest),
            "--phase", "0", "--window-index", "0",
            "--measure-start-item", "1",
        ], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
           text=True)
        value = json.loads(result.read_text(encoding="utf-8"))
        self.assertEqual(value["issued_loads"], 1)
        self.assertEqual(value["completed_loads"], 1)
        self.assertEqual(value["raw_outputs"], [41, 43])

    def test_schema2_window_materializes_repeated_invocations_globally(self):
        trace = self.root / "lazy"
        trace.mkdir()
        raw_two = 0x4000000000000000
        raw_four = 0x4010000000000000
        invocations = tuple(
            lazy.Invocation(
                ordinal, 103, "npb_cg_divide", ordinal, 1,
                {"numerator": "numerator", "denominator": "denominator",
                 "result": "result"},
            )
            for ordinal in range(2)
        )
        lazy.write_bundle(
            trace,
            {"schema": 2, "workload": "npb_cg",
             "source_sha256": _digest("lazy-source"),
             "binary_sha256": _digest("lazy-binary"),
             "config_sha256": _digest("lazy-config"),
             "initial_scalars": {"numerator": raw_four,
                                 "denominator": raw_two,
                                 "result": 0}},
            (), invocations, {"primitive_records": 6},
        )
        trace_sha = replay.trace_identity_sha256(trace)
        plan = timing.make_plan(trace_sha, "cg_dot", 2)
        manifest = self.root / "lazy-windows.json"
        timing.write_plan(manifest, plan)
        materialized = replay.materialize_window_trace(
            trace, manifest=manifest, phase=103, window_index=0,
            outdir=self.root / "lazy-segment",
        )
        bundle = canonical.read_bundle(materialized.root)
        self.assertEqual(
            [operation.opcode for operation in bundle.operations],
            [canonical.Opcode.F64_DIV, canonical.Opcode.F64_DIV],
        )
        self.assertEqual(
            [operation.work_item for operation in bundle.operations], [0, 1]
        )
        self.assertEqual(
            [operation.sequence for operation in bundle.operations], [0, 1]
        )
        self.assertEqual(materialized.measure_start_item, 0)
        self.assertEqual(materialized.fixed_event_records, 4)
        self.assertEqual(bundle.meta["source_schema"], 2)
        stream = self.root / "lazy-stream.bin"
        stream_evidence = replay.write_lazy_replay_stream(trace, stream)
        self.assertEqual(stream_evidence["trace_records"], 6)
        self.assertEqual(stream_evidence["commit_order"], [2, 5])
        self.assertEqual(stream_evidence["raw_outputs"], [0, 0])
        self.assertRegex(stream_evidence["operations_sha256"], r"^[0-9a-f]{64}$")
        self.assertFalse((trace / "trace.bin").exists())
        binary = replay.build_replay_binary(
            self.root / "lazy-stream-build", native=True
        )
        result_path = self.root / "lazy-stream-result.json"
        subprocess.run([
            str(binary), "--system", "vanilla", "--trace", str(stream),
            "--result", str(result_path), "--mode", "functional",
            "--stream", "1",
        ], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
           text=True)
        replayed = json.loads(result_path.read_text(encoding="utf-8"))
        self.assertEqual(replayed["trace_records"], 6)
        self.assertEqual(replayed["commit_order"], [2, 5])
        self.assertEqual(replayed["raw_outputs"], [0, 0])

    def test_window_materialization_rejects_trace_or_phase_drift(self):
        operations = (
            _op(canonical.Opcode.LOAD_U64, 0, work_item=0,
                address=0x7300, left=31, result=31),
            _op(canonical.Opcode.COMMIT, 1, work_item=0,
                address=0x7400, left=31, result=31),
        )
        trace = self.root / "eager-window"
        canonical.write_bundle(
            trace, _meta("eager_window", 1), operations, {}
        )
        plan = timing.make_plan("f" * 64, "eager_window", 1)
        manifest = self.root / "bad-window.json"
        timing.write_plan(manifest, plan)
        with self.assertRaisesRegex(replay.ReplayError, "trace SHA-256"):
            replay.materialize_window_trace(
                trace, manifest=manifest, phase=0, window_index=0,
                outdir=self.root / "bad-segment",
            )

    def test_gem5_replay_binary_links_the_checked_in_m5_abi(self):
        binary = replay.build_replay_binary(self.root / "build", native=False)
        manifest = json.loads(
            (binary.parent / "build.json").read_text(encoding="utf-8")
        )
        self.assertFalse(manifest["native"])
        self.assertIn(str(replay.M5_LIBRARY), manifest["command"])
        self.assertIn("-static", manifest["command"])
        self.assertIn("-no-pie", manifest["command"])
        self.assertEqual(
            manifest["m5_library_sha256"],
            replay._sha256_file(replay.M5_LIBRARY),
        )

    def test_amu_window_is_bounded_by_far_and_spm_queue_geometry(self):
        source = replay.SOURCE.read_text(encoding="utf-8")
        self.assertIn("AMU_CFG_CACHE_LINE_BYTES", source)
        self.assertIn("AMU_CFG_FAR_SEND_QUEUE_PACKETS", source)
        self.assertIn("AMU_CFG_SPM_SEND_QUEUE_PACKETS", source)
        self.assertIn("maxFarPackets", source)
        self.assertIn("2 * spmPackets", source)
        self.assertIn("windowSlots", source)
        self.assertNotIn("active >= slots.size()", source)

    def test_replay_flushes_initialized_state_before_roi(self):
        source = replay.SOURCE.read_text(encoding="utf-8")
        self.assertIn("void flushForRoi()", source)
        flush = source.index("memory.flushForRoi();")
        work_begin = source.index("m5_work_begin(selectedPhase, windowIndex);")
        execute = source.index("stats = executeTrace(")
        self.assertLess(flush, work_begin)
        self.assertLess(flush, execute)
        self.assertIn('"clflush (%0)"', source)

    def test_amu_rejects_per_request_drain_and_bad_counters(self):
        clean = {
            "verification": "pass",
            "threads": 4,
            "all_memory_cxl": True,
            "cxl_link_delay_ticks": 1_000_000,
            "allocated_on_cxl": True,
            "issued_loads": 8,
            "completed_loads": 8,
            "drains": 1,
            "phases": 1,
            "queue_errors": 0,
            "descriptor_errors": 0,
        }
        replay.validate_mechanism("amu", clean)
        for change, message in (
            ({"drains": 8}, "per-request drain"),
            ({"completed_loads": 7}, "issued/completed"),
            ({"queue_errors": 1}, "queue"),
        ):
            with self.subTest(change=change), self.assertRaisesRegex(
                replay.ReplayError, message
            ):
                replay.validate_mechanism("amu", {**clean, **change})

    def test_cira_requires_four_active_cores_and_clean_descriptors(self):
        clean = {
            "verification": "pass",
            "threads": 4,
            "all_memory_cxl": True,
            "cxl_link_delay_ticks": 1_000_000,
            "allocated_on_cxl": True,
            "issued_prefetches": 12,
            "completed_prefetches": 12,
            "issued_per_core": [3, 3, 3, 3],
            "completed_per_core": [3, 3, 3, 3],
            "queue_errors": 0,
            "descriptor_errors": 0,
        }
        replay.validate_mechanism("cira", clean)
        for change, message in (
            ({"issued_per_core": [6, 6, 0, 0]}, "four active cores"),
            ({"completed_prefetches": 11}, "issued/completed"),
            ({"descriptor_errors": 1}, "descriptor"),
        ):
            with self.subTest(change=change), self.assertRaisesRegex(
                replay.ReplayError, message
            ):
                replay.validate_mechanism("cira", {**clean, **change})

    def test_cira_drains_each_core_before_window_boundary(self):
        source = replay.SOURCE.read_text(encoding="utf-8")
        cira = source[
            source.index("class CiraAccessor"):
            source.index("uint64_t\nevaluate")
        ]
        self.assertIn("pendingIds", cira)
        self.assertIn("cira_getfin()", cira)
        self.assertIn("void drain() override", cira)
        self.assertNotIn("void drain() override {}", cira)

    def test_all_backends_require_four_threads_all_cxl_and_one_microsecond(self):
        clean = {
            "verification": "pass",
            "threads": 4,
            "all_memory_cxl": True,
            "cxl_link_delay_ticks": 1_000_000,
            "allocated_on_cxl": True,
            "queue_errors": 0,
            "descriptor_errors": 0,
        }
        replay.validate_mechanism("vanilla", clean)
        for change, message in (
            ({"threads": 2}, "four threads"),
            ({"all_memory_cxl": False}, "all-CXL"),
            ({"allocated_on_cxl": False}, "allocation"),
            ({"cxl_link_delay_ticks": 500_000}, "1 us"),
        ):
            with self.subTest(change=change), self.assertRaisesRegex(
                replay.ReplayError, message
            ):
                replay.validate_mechanism("vanilla", {**clean, **change})

    def _write_config(self, *, delay=1_000_000, direct_memory=False):
        path = self.root / "config.ini"
        cores = "\n".join(
            f"[board.processor.cores{core}.core]\ntype=TimingSimpleCPU\n"
            for core in range(4)
        )
        memory_port = (
            " board.memory.mem_ctrl.port" if direct_memory else ""
        )
        path.write_text(
            "[board]\n"
            "type=System\n"
            "mem_mode=timing\n"
            "mem_ranges=0:4294967296\n"
            "[board.cache_hierarchy.membus]\n"
            "type=CoherentXBar\n"
            "mem_side_ports=board.cxl_mem_link0.cpu_side_port"
            f"{memory_port}\n"
            "[board.cxl_mem_link0]\n"
            "type=SerialLink\n"
            f"delay={delay}\n"
            "ranges=0:4294967296\n"
            "cpu_side_port=board.cache_hierarchy.membus.mem_side_ports[0]\n"
            "mem_side_port=board.memory.mem_ctrl.port\n"
            "[board.memory.mem_ctrl]\n"
            "type=MemCtrl\n"
            "port=board.cxl_mem_link0.mem_side_port\n"
            f"{cores}",
            encoding="utf-8",
        )
        return path

    def test_config_parser_proves_four_core_timing_all_cxl_one_microsecond(self):
        evidence = replay.validate_config_ini(self._write_config())
        self.assertEqual(evidence["threads"], 4)
        self.assertEqual(evidence["cxl_link_delay_ticks"], 1_000_000)
        self.assertTrue(evidence["all_memory_cxl"])
        with self.assertRaisesRegex(replay.ReplayError, "1 us"):
            replay.validate_config_ini(self._write_config(delay=500_000))
        with self.assertRaisesRegex(replay.ReplayError, "bypasses CXL"):
            replay.validate_config_ini(self._write_config(direct_memory=True))

    def test_allocation_log_must_cover_trace_state_and_report_cxl_routing(self):
        log = self.root / "simout"
        log.write_text(
            "TRACE_REPLAY_ALLOCATION logical_bytes=4096 "
            "allocated_bytes=8192 all_memory_cxl=true\n",
            encoding="utf-8",
        )
        row = replay.parse_allocation_log(log, required_bytes=4096)
        self.assertEqual(row["allocated_bytes"], 8192)
        self.assertTrue(row["allocated_on_cxl"])
        log.write_text(
            "TRACE_REPLAY_ALLOCATION logical_bytes=4096 "
            "allocated_bytes=2048 all_memory_cxl=true\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(replay.ReplayError, "allocation"):
            replay.parse_allocation_log(log, required_bytes=4096)

    def test_window_command_is_phase_local_and_functional_has_no_sampling(self):
        binary = self.root / "trace_replay"
        gem5 = self.root / "gem5.opt"
        config = self.root / "config.py"
        trace = self.root / "trace"
        window_manifest = self.root / "windows.json"
        for path in (binary, gem5, config):
            path.write_bytes(path.name.encode("utf-8"))
        trace.mkdir()
        (trace / "trace.bin").write_bytes(b"")
        window_manifest.write_text(json.dumps({"schema": 1}) + "\n")
        common = dict(
            system="amu", trace=trace, binary=binary, gem5=gem5,
            config=config, outdir=self.root / "out",
            calibration=self.root / "calibration.json",
        )
        functional = replay.command_for(SimpleNamespace(
            **common, mode="functional", window_manifest=None,
            phase=None, window_index=None,
        ))
        functional_args = shlex.split(
            functional[functional.index("--arguments") + 1]
        )
        self.assertNotIn("--window-index", functional_args)
        self.assertIn("--redirect-stdout", functional)
        self.assertIn("--stdout-file=simout", functional)
        self.assertIn("--mode", functional_args)
        self.assertEqual(
            functional_args[functional_args.index("--mode") + 1],
            "functional",
        )
        window = replay.command_for(SimpleNamespace(
            **common, mode="window", window_manifest=window_manifest,
            phase=3, window_index=7,
        ))
        window_args = shlex.split(window[window.index("--arguments") + 1])
        self.assertEqual(window_args[window_args.index("--phase") + 1], "3")
        self.assertEqual(
            window_args[window_args.index("--window-index") + 1], "7"
        )
        self.assertIn("--roi-work-events", window)

    def test_run_evidence_uses_gem5_owned_amu_counters_and_exact_commits(self):
        operations = (
            _op(canonical.Opcode.LOAD_U64, 0, address=0xA000,
                left=17, result=17),
            _op(canonical.Opcode.COMMIT, 1, address=0xB000,
                left=17, result=17),
        )
        bundle = self.root / "evidence-trace"
        canonical.write_bundle(bundle, _meta("evidence", 1), operations, {})
        run_dir = self.root / "run"
        run_dir.mkdir()
        (run_dir / "result.json").write_text(json.dumps({
            "verification": "pass", "threads": 4, "phases": 1,
            "commit_order": [1], "raw_outputs": [17],
            "issued_loads": 999, "completed_loads": 999,
            "drains": 4,
        }) + "\n", encoding="utf-8")
        (run_dir / "simout").write_text(
            "TRACE_REPLAY_ALLOCATION logical_bytes=64 "
            "allocated_bytes=64 all_memory_cxl=true\n",
            encoding="utf-8",
        )
        (run_dir / "stats.txt").write_text(
            "---------- Begin Simulation Statistics ----------\n"
            "simTicks 12345\n"
            "board.asmc.issuedLoads 1\n"
            "board.asmc.completedLoads 1\n"
            "board.asmc.rejectedQueueFull 0\n"
            "board.asmc.rejectedSpmFull 0\n"
            "board.asmc.translationFaults 0\n"
            "board.asmc.pendingQueueFull 0\n"
            "board.asmc.farSpmFlagPackets 0\n"
            "board.asmc.spmMissingFlagPackets 0\n"
            "---------- End Simulation Statistics ----------\n",
            encoding="utf-8",
        )
        config = self._write_config()
        row = replay.collect_run_evidence(
            run_dir, system="amu", trace=bundle, config=config
        )
        self.assertEqual(row["issued_loads"], 1)
        self.assertEqual(row["completed_loads"], 1)
        self.assertEqual(row["sim_ticks"], 12345)
        self.assertEqual(row["verification"], "pass")
        self.assertEqual(row["raw_outputs"], [17])

    def test_run_evidence_rejects_program_commit_drift(self):
        operations = (
            _op(canonical.Opcode.COMMIT, 0, address=0xC000,
                left=21, result=21),
        )
        bundle = self.root / "bad-commit-trace"
        canonical.write_bundle(bundle, _meta("bad_commit", 1), operations, {})
        run_dir = self.root / "bad-run"
        run_dir.mkdir()
        (run_dir / "result.json").write_text(json.dumps({
            "verification": "pass", "threads": 4, "phases": 1,
            "commit_order": [0], "raw_outputs": [20], "drains": 0,
        }) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(replay.ReplayError, "raw output"):
            replay.collect_run_evidence(
                run_dir, system="vanilla", trace=bundle,
                config=self._write_config(),
            )

    def test_cli_requires_complete_window_selection(self):
        common = [
            "--mode", "functional", "--system", "vanilla",
            "--trace", "trace", "--binary", "trace_replay",
            "--gem5", "gem5.opt", "--config", "config.py",
            "--calibration", "calibration.json", "--outdir", "out",
        ]
        functional = replay.parse_args(common)
        self.assertEqual(functional.mode, "functional")
        with self.assertRaises(SystemExit):
            replay.parse_args([
                *common[:1], "window", *common[2:],
                "--window-manifest", "windows.json", "--phase", "3",
            ])
        window = replay.parse_args([
            *common[:1], "window", *common[2:],
            "--window-manifest", "windows.json", "--phase", "3",
            "--window-index", "7",
        ])
        self.assertEqual(window.window_index, 7)


if __name__ == "__main__":
    unittest.main()
