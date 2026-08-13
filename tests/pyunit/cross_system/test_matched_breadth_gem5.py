# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

import hashlib
import json
import shlex
import struct
import subprocess
import tempfile
import unittest
from unittest import mock
from pathlib import Path
from types import SimpleNamespace

from scripts import build_matched_breadth_workloads as builder
from scripts import canonical_work_trace as canonical
from scripts import lazy_work_trace as lazy
from scripts import npb_lazy_trace as npb
from scripts import run_matched_breadth_gem5 as replay
from scripts import stratified_timing as timing


def _digest(label):
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _meta(name, records, output_boundaries=None):
    return {
        "schema": 1,
        "workload": name,
        "input_sha256": _digest(f"{name}-input"),
        "source_sha256": _digest(f"{name}-source"),
        "binary_sha256": _digest(f"{name}-binary"),
        "config_sha256": _digest(f"{name}-config"),
        "phases": [{"id": 0, "name": name, "work_items": records}],
        "output_boundaries": output_boundaries or {},
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


def _initial_memory(operations):
    widths = {
        canonical.Opcode.LOAD_U32: 32,
        canonical.Opcode.LOAD_U64: 64,
        canonical.Opcode.LOAD_F32: 32,
        canonical.Opcode.LOAD_F64: 64,
        canonical.Opcode.STORE_U32: 32,
        canonical.Opcode.STORE_U64: 64,
        canonical.Opcode.STORE_F32: 32,
        canonical.Opcode.STORE_F64: 64,
    }
    images = {}
    covered = set()
    for operation in operations:
        word_bits = widths.get(operation.opcode)
        if word_bits is None or operation.address in covered:
            continue
        covered.add(operation.address)
        initial = operation.operand0 if operation.opcode.name.startswith("LOAD") else 0
        images[f"word-{operation.address:x}"] = {
            "logical_base": operation.address,
            "word_bits": word_bits,
            "words": (initial,),
        }
    return images


def _probes(addresses, after_sequence):
    return [
        {"address": address, "after_sequence": after_sequence}
        for address in addresses
    ]


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
                left=0x4100, right=1, result=0x4100),
            _op(canonical.Opcode.LOAD_U64, 2, address=0x4100,
                left=23, right=2, result=23),
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
                                       operations, {},
                                       initial_memory=_initial_memory(operations))
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

    def test_actual_fixture_bundles_pass_all_three_backends(self):
        manifest = builder.build_fixture_suite(self.root / "actual-build")
        bundles = builder.run_fixture_references(
            manifest, self.root / "actual-reference"
        )
        binary = replay.build_replay_binary(
            self.root / "actual-replay-build", native=True
        )
        for workload in ("mcf", "amg_gather", "lulesh_scatter"):
            for system in ("vanilla", "amu", "cira"):
                with self.subTest(workload=workload, system=system):
                    result = replay.run_native_replay(
                        binary, system=system, trace=bundles[workload],
                        outdir=self.root / f"actual-{workload}-{system}",
                    )
                    self.assertEqual(result["verification"], "pass")
                    self.assertEqual(
                        set(result["output_boundaries"]),
                        set(canonical.read_bundle(bundles[workload]).outputs),
                    )
        manifest_path = self.root / "actual-mcf-window.json"
        manifest_path.write_text('{"schema":1}\n', encoding="utf-8")
        with mock.patch.object(
            replay, "_window_coordinates",
            return_value=timing.TimingWindow(0, 1, 1, 2),
        ):
            materialized = replay.materialize_window_trace(
                bundles["mcf"], manifest=manifest_path, phase=1,
                window_index=0, outdir=self.root / "actual-mcf-window",
            )
        dynamic = canonical.read_bundle(materialized.root)
        fixed = canonical.read_bundle(materialized.fixed_root)
        self.assertFalse(any(
            operation.opcode == canonical.Opcode.COMMIT
            for operation in dynamic.operations
        ))
        self.assertTrue(any(
            operation.opcode == canonical.Opcode.COMMIT
            for operation in fixed.operations
        ))
        self.assertTrue(any(
            builder.MCF_BASES["pricing_offsets"] <= operation.address <
            builder.MCF_BASES["pricing_offsets"] + 3 * 8
            for operation in fixed.operations
        ))

    def test_functional_replay_dumps_every_declared_raw_boundary(self):
        operations = (
            _op(canonical.Opcode.STORE_U32, 0, address=0x1000,
                left=0x01234567, result=0x01234567),
            _op(canonical.Opcode.STORE_U32, 1, address=0x1004,
                left=0x89ABCDEF, result=0x89ABCDEF),
            _op(canonical.Opcode.STORE_U64, 2, address=0x2000,
                left=0x0123456789ABCDEF, result=0x0123456789ABCDEF),
            _op(canonical.Opcode.STORE_U64, 3, address=0x2008,
                left=0xFEDCBA9876543210, result=0xFEDCBA9876543210),
        )
        boundaries = {
            "state.u32": {
                "word_bits": 32,
                "count": 2,
                "probes": _probes((0x1000, 0x1004), 3),
            },
            "state.u64": {
                "word_bits": 64,
                "count": 2,
                "probes": _probes((0x2000, 0x2008), 3),
            },
        }
        outputs = {
            "state.u32": (0x01234567, 0x89ABCDEF),
            "state.u64": (
                0x0123456789ABCDEF, 0xFEDCBA9876543210,
            ),
        }
        bundle = self.root / "all-boundaries"
        canonical.write_bundle(
            bundle, _meta("all_boundaries", 1, boundaries),
            operations, outputs, initial_memory=_initial_memory(operations),
        )
        binary = replay.build_replay_binary(self.root / "build", native=True)
        result = replay.run_native_replay(
            binary, system="vanilla", trace=bundle,
            outdir=self.root / "all-boundaries-run",
        )
        self.assertEqual(result["output_boundaries"], {
            "state.u32": {
                "word_bits": 32,
                "count": 2,
                "raw_words": [0x01234567, 0x89ABCDEF],
            },
            "state.u64": {
                "word_bits": 64,
                "count": 2,
                "raw_words": [
                    0x0123456789ABCDEF, 0xFEDCBA9876543210,
                ],
            },
        })

    def test_boundary_observation_uses_runtime_store_value_not_expected_result(self):
        runtime_word = 0x01234567
        poisoned_expected_result = runtime_word ^ 1
        operations = (
            _op(canonical.Opcode.STORE_U32, 0, address=0x1000,
                left=runtime_word, result=poisoned_expected_result),
        )
        boundaries = {
            "state": {
                "word_bits": 32,
                "count": 1,
                "probes": _probes((0x1000,), 0),
            },
        }
        bundle = self.root / "runtime-boundary"
        canonical.write_bundle(
            bundle, _meta("runtime_boundary", 1, boundaries), operations,
            {"state": (runtime_word,)},
            initial_memory=_initial_memory(operations),
        )
        binary = replay.build_replay_binary(self.root / "build", native=True)
        result = replay.run_native_replay(
            binary, system="vanilla", trace=bundle,
            outdir=self.root / "runtime-boundary-run",
        )
        self.assertEqual(
            result["output_boundaries"]["state"]["raw_words"],
            [runtime_word],
        )

    def test_boundary_gate_rejects_bit_drift_missing_and_extra_data(self):
        operations = (
            _op(canonical.Opcode.STORE_U32, 0, address=0x1000,
                left=0xDEADBEEF, result=0xDEADBEEF),
        )
        boundaries = {
            "answer": {
                "word_bits": 32,
                "count": 1,
                "probes": _probes((0x1000,), 0),
            },
        }
        bundle = self.root / "boundary-gate"
        canonical.write_bundle(
            bundle, _meta("boundary_gate", 1, boundaries), operations,
            {"answer": (0xDEADBEEF,)},
            initial_memory=_initial_memory(operations),
        )
        loaded = canonical.read_bundle(bundle)
        clean = {
            "answer": {
                "word_bits": 32,
                "count": 1,
                "raw_words": [0xDEADBEEF],
            },
        }
        replay.validate_output_boundaries(loaded, clean)
        failures = (
            ({"answer": {**clean["answer"],
                         "raw_words": [0xDEADBEEE]}}, "answer\\[0\\]"),
            ({}, "boundary set"),
            ({**clean, "extra": clean["answer"]}, "boundary set"),
        )
        for observed, message in failures:
            with self.subTest(observed=observed), self.assertRaisesRegex(
                replay.ReplayError, message
            ):
                replay.validate_output_boundaries(loaded, observed)

    def test_functional_replay_rejects_unmapped_legacy_boundaries(self):
        operations = (
            _op(canonical.Opcode.STORE_U32, 0, address=0x1000,
                result=0xDEADBEEF),
        )
        bundle = self.root / "legacy-boundary"
        canonical.write_bundle(
            bundle,
            _meta("legacy_boundary", 1, {
                "answer": {"word_bits": 32, "count": 1},
            }),
            operations, {"answer": (0xDEADBEEF,)},
            initial_memory=_initial_memory(operations),
        )
        binary = replay.build_replay_binary(self.root / "build", native=True)
        with self.assertRaisesRegex(
            replay.ReplayError, "address mapping is missing"
        ):
            replay.run_native_replay(
                binary, system="vanilla", trace=bundle,
                outdir=self.root / "legacy-boundary-run",
            )

    def test_lazy_functional_replay_rejects_missing_raw_boundary_mapping(self):
        trace = self.root / "lazy-boundary"
        trace.mkdir()
        lazy.write_bundle(
            trace,
            {"schema": 2, "workload": "npb_cg",
             "source_sha256": _digest("lazy-source"),
             "binary_sha256": _digest("lazy-binary"),
             "config_sha256": _digest("lazy-config"),
             "initial_scalars": {"numerator": 0x4010000000000000,
                                 "denominator": 0x4000000000000000,
                                 "result": 0},
             "boundary_commitments": {"scalar.result": _digest("result")}},
            (),
            (lazy.Invocation(
                0, 103, "npb_cg_divide", 0, 1,
                {"numerator": "numerator", "denominator": "denominator",
                 "result": "result"},
            ),),
            {"primitive_records": 3},
        )
        binary = self.root / "trace_replay"
        gem5 = self.root / "gem5.opt"
        config = self.root / "config.py"
        calibration = self.root / "calibration.json"
        for path in (binary, gem5, config, calibration):
            path.write_bytes(path.name.encode("utf-8"))
        options = SimpleNamespace(
            mode="functional", system="vanilla", trace=trace,
            window_manifest=None, phase=None, window_index=None,
            binary=binary, gem5=gem5, config=config,
            calibration=calibration, outdir=self.root / "lazy-run", timeout=0,
        )
        with self.assertRaisesRegex(
            replay.ReplayError, "raw output boundary mapping"
        ):
            replay.run(options)

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
            bundle, _meta("four_workers", 4), tuple(operations), {},
            initial_memory=_initial_memory(operations),
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
            bundle, _meta("interleaved_window", 1), operations, {},
            initial_memory=_initial_memory(operations),
        )
        binary = replay.build_replay_binary(self.root / "build", native=True)
        result = replay.run_native_replay(
            binary, system="amu", trace=bundle,
            outdir=self.root / "interleaved-window-run",
        )
        self.assertGreaterEqual(result["max_observed_outstanding"], 2)
        self.assertEqual(result["raw_outputs"], [8])

    def test_amu_does_not_issue_dependent_pointer_loads_early(self):
        operations = _fixtures()["pointer_chain"]
        bundle = self.root / "dependent-pointer-chain"
        canonical.write_bundle(
            bundle, _meta("dependent_pointer_chain", 1), operations, {},
            initial_memory=_initial_memory(operations),
        )
        binary = replay.build_replay_binary(
            self.root / "dependent-pointer-build", native=True
        )
        result = replay.run_native_replay(
            binary, system="amu", trace=bundle,
            outdir=self.root / "dependent-pointer-run",
        )
        self.assertEqual(result["issued_loads"], 3)
        self.assertEqual(result["completed_loads"], 3)
        self.assertEqual(result["max_observed_outstanding"], 1)

    def test_cross_work_item_dependency_waits_for_global_completion(self):
        operations = (
            _op(canonical.Opcode.LOAD_U64, 0, work_item=0,
                address=0xA000, left=0xA080, result=0xA080),
            _op(canonical.Opcode.LOAD_U64, 1, work_item=1,
                address=0xA080, left=29, right=1, result=29),
            _op(canonical.Opcode.COMMIT, 2, work_item=1, left=29, result=29),
        )
        bundle = self.root / "cross-group-dependency"
        canonical.write_bundle(
            bundle, _meta("cross_group_dependency", 2), operations, {},
            initial_memory=_initial_memory(operations),
        )
        binary = replay.build_replay_binary(
            self.root / "cross-group-build", native=True
        )
        result = replay.run_native_replay(
            binary, system="amu", trace=bundle,
            outdir=self.root / "cross-group-run",
        )
        self.assertEqual(result["verification"], "pass")
        self.assertEqual(result["max_observed_outstanding"], 1)

    def test_cross_work_item_failure_cancels_dependency_waiters(self):
        operations = (
            _op(canonical.Opcode.LOAD_U64, 0, work_item=0,
                address=0xA800, left=5, result=6),
            _op(canonical.Opcode.LOAD_U64, 1, work_item=1,
                address=0xA880, left=7, right=1, result=7),
            _op(canonical.Opcode.COMMIT, 2, work_item=1, left=7, result=7),
        )
        bundle = self.root / "cancelled-dependency"
        canonical.write_bundle(
            bundle, _meta("cancelled_dependency", 2), operations, {},
            initial_memory=_initial_memory(operations),
        )
        binary = replay.build_replay_binary(
            self.root / "cancelled-dependency-build", native=True
        )
        initial = replay._write_initial_memory_map(
            canonical.read_bundle(bundle), bundle,
            self.root / "cancelled-dependency-initial.txt",
        )
        completed = subprocess.run([
            str(binary), "--system", "amu", "--trace",
            str(bundle / "trace.bin"), "--result",
            str(self.root / "cancelled-dependency-result.json"),
            "--initial-memory-map", str(initial),
        ], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
           timeout=5)
        self.assertEqual(completed.returncode, 2, completed.stderr)
        self.assertIn("bit-exact result differs", completed.stderr)

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
            bundle, _meta("same_cache_line", 1), operations, {},
            initial_memory=_initial_memory(operations),
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
        canonical.write_bundle(
            bundle, _meta("selection", 1), operations, {},
            initial_memory=_initial_memory(operations),
        )
        binary = replay.build_replay_binary(self.root / "build", native=True)
        manifest = self.root / "windows.json"
        manifest.write_text('{"schema":1}\n', encoding="utf-8")
        initial_map = replay._write_initial_memory_map(
            canonical.read_bundle(bundle), bundle,
            self.root / "selection-initial-map.txt",
        )
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
                "--initial-memory-map", str(initial_map),
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
            bundle, _meta("warmup_measure", 2), operations, {},
            initial_memory=_initial_memory(operations),
        )
        binary = replay.build_replay_binary(self.root / "build", native=True)
        manifest = self.root / "warmup-measure-windows.json"
        manifest.write_text('{"schema":1}\n', encoding="utf-8")
        result = self.root / "warmup-measure-result.json"
        initial_map = replay._write_initial_memory_map(
            canonical.read_bundle(bundle), bundle,
            self.root / "warmup-initial-map.txt",
        )
        subprocess.run([
            str(binary), "--system", "amu", "--trace",
            str(bundle / "trace.bin"), "--result", str(result),
            "--mode", "window", "--window-manifest", str(manifest),
            "--phase", "0", "--window-index", "0",
            "--measure-start-item", "1",
            "--initial-memory-map", str(initial_map),
        ], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
           text=True)
        value = json.loads(result.read_text(encoding="utf-8"))
        self.assertEqual(value["issued_loads"], 1)
        self.assertEqual(value["completed_loads"], 1)
        self.assertEqual(value["raw_outputs"], [41, 43])

    def test_eager_window_inherits_prior_duplicate_store_state(self):
        operations = (
            _op(canonical.Opcode.STORE_U64, 0, work_item=0,
                address=0x3000, left=7, result=7),
            _op(canonical.Opcode.LOAD_U64, 1, work_item=1,
                address=0x3000, left=7, result=7),
            _op(canonical.Opcode.STORE_U64, 2, work_item=1,
                address=0x3000, left=9, result=9),
            _op(canonical.Opcode.COMMIT, 3, work_item=1,
                address=0x3000, left=9, result=9),
        )
        trace = self.root / "duplicate-window"
        canonical.write_bundle(
            trace, _meta("duplicate_window", 2), operations, {},
            initial_memory={"destination": {
                "logical_base": 0x3000, "word_bits": 64, "words": (0,),
            }},
        )
        manifest = self.root / "duplicate-plan.json"
        manifest.write_text('{"schema":1}\n')
        with mock.patch.object(
            replay, "_window_coordinates",
            return_value=timing.TimingWindow(0, 1, 1, 2),
        ):
            materialized = replay.materialize_window_trace(
                trace, manifest=manifest, phase=0, window_index=0,
                outdir=self.root / "duplicate-materialized",
            )
        bundle = canonical.read_bundle(materialized.root)
        image = next(iter(bundle.meta["initial_memory"].values()))
        self.assertEqual(
            (materialized.root / image["path"]).read_bytes(),
            struct.pack("<Q", 7),
        )
        binary = replay.build_replay_binary(
            self.root / "duplicate-build", native=True
        )
        initial = replay._write_initial_memory_map(
            bundle, materialized.root, self.root / "duplicate-initial.txt"
        )
        result = self.root / "duplicate-result.json"
        subprocess.run([
            str(binary), "--system", "vanilla", "--trace",
            str(materialized.root / "trace.bin"), "--result", str(result),
            "--mode", "window", "--window-manifest", str(manifest),
            "--phase", "0", "--window-index", "0",
            "--measure-start-item", "0", "--initial-memory-map", str(initial),
        ], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.assertEqual(json.loads(result.read_text())["verification"], "pass")

    def test_eager_window_remaps_load_dependencies_after_fixed_split(self):
        operations = (
            _op(canonical.Opcode.BARRIER, 0, work_item=0),
            _op(canonical.Opcode.LOAD_U64, 1, work_item=0,
                address=0x9000, left=0x9080, result=0x9080),
            _op(canonical.Opcode.LOAD_U64, 2, work_item=0,
                address=0x9080, left=17, right=2, result=17),
            _op(canonical.Opcode.COMMIT, 3, work_item=0, left=17, result=17),
        )
        trace = self.root / "dependency-window"
        canonical.write_bundle(
            trace, _meta("dependency_window", 1), operations, {},
            initial_memory=_initial_memory(operations),
        )
        manifest = self.root / "dependency-window-plan.json"
        manifest.write_text('{"schema":1}\n', encoding="utf-8")
        with mock.patch.object(
            replay, "_window_coordinates",
            return_value=timing.TimingWindow(0, 0, 0, 1),
        ):
            materialized = replay.materialize_window_trace(
                trace, manifest=manifest, phase=0, window_index=0,
                outdir=self.root / "dependency-materialized",
            )
        dynamic = canonical.read_bundle(materialized.root)
        self.assertEqual([operation.sequence for operation in dynamic.operations],
                         [0, 1])
        self.assertEqual(dynamic.operations[1].operand1, 1)
        binary = replay.build_replay_binary(
            self.root / "dependency-window-build", native=True
        )
        result = replay.run_native_replay(
            binary, system="amu", trace=materialized.root,
            outdir=self.root / "dependency-window-run",
        )
        self.assertEqual(result["max_observed_outstanding"], 1)

    def test_eager_out_of_range_request_is_timed_as_fixed_component(self):
        operations = (
            _op(canonical.Opcode.LOAD_U64, 0, work_item=3,
                address=0xB000, left=1, result=1),
            _op(canonical.Opcode.LOAD_U64, 1, work_item=1,
                address=0xB080, left=31, right=1, result=31),
            _op(canonical.Opcode.COMMIT, 2, work_item=1, left=31, result=31),
        )
        trace = self.root / "fixed-request-window"
        canonical.write_bundle(
            trace, _meta("fixed_request_window", 3), operations, {},
            initial_memory=_initial_memory(operations),
        )
        manifest = self.root / "fixed-request-plan.json"
        manifest.write_text('{"schema":1}\n', encoding="utf-8")
        with mock.patch.object(
            replay, "_window_coordinates",
            return_value=timing.TimingWindow(0, 1, 1, 2),
        ):
            materialized = replay.materialize_window_trace(
                trace, manifest=manifest, phase=0, window_index=0,
                outdir=self.root / "fixed-request-materialized",
            )
        dynamic = canonical.read_bundle(materialized.root)
        fixed = canonical.read_bundle(materialized.fixed_root)
        self.assertEqual(dynamic.operations[0].address, 0xB080)
        self.assertEqual(dynamic.operations[0].operand1, 0)
        self.assertIn(0xB000, [operation.address for operation in fixed.operations])

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
            (), invocations, {"primitive_records": 8},
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
        self.assertEqual(materialized.fixed_event_records, 6)
        fixed = canonical.read_bundle(materialized.fixed_root)
        self.assertLessEqual(sum(
            row["byte_count"]
            for row in bundle.meta["initial_memory"].values()
        ), 8)
        self.assertLessEqual(sum(
            row["byte_count"]
            for row in fixed.meta["initial_memory"].values()
        ), 8)
        self.assertNotEqual(
            bundle.meta["initial_memory"], fixed.meta["initial_memory"]
        )
        self.assertEqual(
            [operation.opcode for operation in fixed.operations],
            [
                canonical.Opcode.BARRIER, canonical.Opcode.STORE_F64,
                canonical.Opcode.COMMIT, canonical.Opcode.BARRIER,
                canonical.Opcode.STORE_F64, canonical.Opcode.COMMIT,
            ],
        )
        self.assertEqual(
            [operation.work_item for operation in fixed.operations],
            [0, 1, 0, 1, 2, 1],
        )
        self.assertNotEqual(
            bundle.meta["trace_sha256"], fixed.meta["trace_sha256"]
        )
        self.assertEqual(bundle.meta["source_schema"], 2)
        stream = self.root / "lazy-stream.bin"
        stream_evidence = replay.write_lazy_replay_stream(trace, stream)
        self.assertEqual(stream_evidence["trace_records"], 8)
        self.assertEqual(stream_evidence["commit_order"], [3, 7])
        self.assertEqual(stream_evidence["raw_outputs"], [0, 0])
        self.assertRegex(stream_evidence["operations_sha256"], r"^[0-9a-f]{64}$")
        self.assertFalse((trace / "trace.bin").exists())
        binary = replay.build_replay_binary(
            self.root / "lazy-stream-build", native=True
        )
        result_path = self.root / "lazy-stream-result.json"
        initial_map = replay._write_lazy_initial_memory_map(
            lazy.read_bundle(trace), self.root / "lazy-stream-initial.txt"
        )
        subprocess.run([
            str(binary), "--system", "vanilla", "--trace", str(stream),
            "--result", str(result_path), "--mode", "functional",
            "--stream", "1",
            "--initial-memory-map", str(initial_map),
        ], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
           text=True)
        replayed = json.loads(result_path.read_text(encoding="utf-8"))
        self.assertEqual(replayed["trace_records"], 8)
        self.assertEqual(replayed["commit_order"], [3, 7])
        self.assertEqual(replayed["raw_outputs"], [0, 0])

    def test_native_lazy_schema2_boundaries_pass_all_backends(self):
        raw_two = 0x4000000000000000
        raw_four = 0x4010000000000000
        commitment = hashlib.sha256(struct.pack("<Q", raw_two)).hexdigest()
        binary = replay.build_replay_binary(
            self.root / "native-lazy-build", native=True
        )
        for workload in ("npb_cg",):
            trace = self.root / workload
            trace.mkdir()
            invocation = lazy.Invocation(
                0, 103, "npb_cg_divide", 7, 1,
                {"numerator": "numerator", "denominator": "denominator",
                 "result": "result"},
            )
            lazy.write_bundle(
                trace,
                {"schema": 2, "workload": workload,
                 "source_sha256": _digest(workload + "-source"),
                 "binary_sha256": _digest(workload + "-binary"),
                 "config_sha256": _digest(workload + "-config"),
                 "initial_scalars": {"numerator": raw_four,
                                     "denominator": raw_two, "result": 0},
                 "boundary_commitments": {
                     "scalar.result.divide.iter7": commitment,
                 }},
                (), (invocation,), {"primitive_records": 4},
            )
            for system in ("vanilla", "amu", "cira"):
                result = replay.run_native_lazy_replay(
                    binary, system=system, trace=trace,
                    outdir=self.root / f"{workload}-{system}",
                )
                self.assertEqual(result["verification"], "pass")
                self.assertEqual(
                    result["output_boundaries"]
                    ["scalar.result.divide.iter7"]["raw_words"],
                    [raw_two],
                )
        mg_trace = self.root / "npb_mg"
        mg_trace.mkdir()
        image = mg_trace / "images/u.f64"
        image.parent.mkdir(parents=True)
        payload = struct.pack("<d", 3.0)
        image.write_bytes(payload)
        array = lazy.ArrayImage(
            "u", "state", "f64", 1, 0x1000, "images/u.f64",
            hashlib.sha256(payload).hexdigest(),
        )
        invocation = lazy.Invocation(
            0, 201, "npb_mg_zero3", 2, 1,
            {"u": "u", "n1": 1, "n2": 1, "n3": 1,
             "boundaries": ["u"]},
        )
        zero_digest = hashlib.sha256(struct.pack("<Q", 0)).hexdigest()
        lazy.write_bundle(
            mg_trace,
            {"schema": 2, "workload": "npb_mg",
             "source_sha256": _digest("mg-source"),
             "binary_sha256": _digest("mg-binary"),
             "config_sha256": _digest("mg-config"),
             "boundary_commitments": {"u.zero3.iter2": zero_digest}},
            (array,), (invocation,), {"primitive_records": 3},
        )
        for system in ("vanilla", "amu", "cira"):
            result = replay.run_native_lazy_replay(
                binary, system=system, trace=mg_trace,
                outdir=self.root / f"npb_mg-{system}",
            )
            self.assertEqual(
                result["output_boundaries"]["u.zero3.iter2"]["raw_words"],
                [0],
            )

    def test_schema2_multiload_window_exercises_amu_and_four_cira_cores(self):
        trace_root = self.root / "schema2-cg-window"
        image_root = trace_root / "images"
        image_root.mkdir(parents=True)

        def array(name, element_type, base, values, code):
            payload = struct.pack(f"<{len(values)}{code}", *values)
            path = image_root / f"{name}.{element_type}"
            path.write_bytes(payload)
            return lazy.ArrayImage(
                name, "state", element_type, len(values), base,
                f"images/{path.name}", hashlib.sha256(payload).hexdigest(),
            )

        rows = 8
        rowstr = tuple(2 * row for row in range(rows + 1))
        colidx = tuple(index % rows for index in range(2 * rows))
        one = 0x3FF0000000000000
        zero = 0
        arrays = (
            array("rowstr", "u32", 0x1000, rowstr, "I"),
            array("colidx", "u32", 0x2000, colidx, "I"),
            array("a", "f64", 0x3000, (one,) * (2 * rows), "Q"),
            array("p", "f64", 0x4000, (one,) * rows, "Q"),
            array("q", "f64", 0x5000, (zero,) * rows, "Q"),
        )
        invocation = lazy.Invocation(
            0, 101, "npb_cg_spmv", 1, rows,
            {"rowstr": "rowstr", "colidx": "colidx", "values": "a",
             "source": "p", "destination": "q", "row_count": rows,
             "edge_base": 0, "column_base": 0,
             "destination_count": rows},
        )
        result_words = (0x4000000000000000,) * rows
        lazy.write_bundle(
            trace_root,
            {"schema": 2, "workload": "npb_cg",
             "source_sha256": _digest("window-source"),
             "binary_sha256": _digest("window-binary"),
             "config_sha256": _digest("window-config"),
             "boundary_commitments": {
                 "q.spmv.iter1": hashlib.sha256(
                     struct.pack(f"<{rows}Q", *result_words)
                 ).hexdigest(),
             }},
            arrays, (invocation,), {"primitive_records": 106},
        )
        manifest = self.root / "schema2-window-plan.json"
        manifest.write_text('{"schema":1}\n', encoding="utf-8")
        with mock.patch.object(
            replay, "_window_coordinates",
            return_value=timing.TimingWindow(0, 0, 0, rows),
        ):
            materialized = replay.materialize_window_trace(
                trace_root, manifest=manifest, phase=101, window_index=0,
                outdir=self.root / "schema2-cg-materialized",
            )
        binary = replay.build_replay_binary(
            self.root / "schema2-window-build", native=True
        )
        amu = replay.run_native_replay(
            binary, system="amu", trace=materialized.root,
            outdir=self.root / "schema2-window-amu",
        )
        cira = replay.run_native_replay(
            binary, system="cira", trace=materialized.root,
            outdir=self.root / "schema2-window-cira",
        )
        self.assertGreater(amu["max_observed_outstanding"], 1)
        self.assertEqual(amu["issued_loads"], amu["completed_loads"])
        self.assertLess(amu["drains"], amu["issued_loads"])
        self.assertTrue(all(count > 0 for count in cira["issued_per_core"]))
        self.assertEqual(cira["issued_per_core"], cira["completed_per_core"])

    def test_native_lazy_one_bit_runtime_store_drift_fails_commitment(self):
        raw_two = 0x4000000000000000
        raw_four = 0x4010000000000000
        trace = self.root / "lazy-drift"
        trace.mkdir()
        invocation = lazy.Invocation(
            0, 103, "npb_cg_divide", 1, 1,
            {"numerator": "numerator", "denominator": "denominator",
             "result": "result"},
        )
        lazy.write_bundle(
            trace,
            {"schema": 2, "workload": "npb_cg",
             "source_sha256": _digest("drift-source"),
             "binary_sha256": _digest("drift-binary"),
             "config_sha256": _digest("drift-config"),
             "initial_scalars": {"numerator": raw_four,
                                 "denominator": raw_two, "result": 0},
             "boundary_commitments": {
                 "scalar.result.divide.iter1":
                     hashlib.sha256(struct.pack("<Q", raw_two)).hexdigest(),
             }},
            (), (invocation,), {"primitive_records": 4},
        )

        binary = replay.build_replay_binary(self.root / "drift-build", native=True)
        stream = self.root / "drift.stream"
        replay.write_lazy_replay_stream(trace, stream)
        payload = bytearray(stream.read_bytes())
        cursor = 0
        changed = False
        while cursor < len(payload):
            _magic, records, flags = replay._STREAM_HEADER.unpack_from(payload, cursor)
            cursor += replay._STREAM_HEADER.size
            for _ in range(records):
                opcode = struct.unpack_from("<H", payload, cursor + 2)[0]
                if opcode == int(canonical.Opcode.STORE_F64):
                    payload[cursor + 32] ^= 1
                    changed = True
                    break
                cursor += canonical.TRACE_STRUCT.size
            if changed:
                break
            cursor += records * canonical.TRACE_STRUCT.size
        self.assertTrue(changed)
        stream.write_bytes(payload)
        bundle = lazy.read_bundle(trace)
        boundary = replay._write_lazy_boundary_map(
            bundle, self.root / "drift-boundary.txt"
        )
        initial = replay._write_lazy_initial_memory_map(
            bundle, self.root / "drift-initial.txt"
        )
        result = self.root / "drift-result.json"
        subprocess.run([
            str(binary), "--system", "vanilla", "--trace", str(stream),
            "--result", str(result), "--mode", "functional", "--stream", "1",
            "--boundary-map", str(boundary), "--initial-memory-map", str(initial),
        ], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        observed = json.loads(result.read_text())
        with self.assertRaisesRegex(replay.ReplayError, "differs"):
            row = observed["output_boundaries"]["scalar.result.divide.iter1"]
            got = hashlib.sha256(struct.pack("<Q", *row["raw_words"])).hexdigest()
            if got != bundle.meta["boundary_commitments"]["scalar.result.divide.iter1"]:
                raise replay.ReplayError("stream replay boundary differs")

    def test_lazy_boundary_map_selects_only_native_committed_boundaries(self):
        trace = self.root / "npb-cg-native-boundary-subset"
        trace.mkdir()
        raw_two = 0x4000000000000000
        raw_four = 0x4010000000000000
        invocations = (
            lazy.Invocation(
                0, 103, "npb_cg_divide", 1, 1,
                {"numerator": "numerator", "denominator": "denominator",
                 "result": "uncommitted"},
            ),
            lazy.Invocation(
                1, 103, "npb_cg_divide", 2, 1,
                {"numerator": "numerator", "denominator": "denominator",
                 "result": "kept"},
            ),
        )
        kept_name = "scalar.kept.divide.iter2"
        lazy.write_bundle(
            trace,
            {"schema": 2, "workload": "npb_cg",
             "source_sha256": _digest("subset-source"),
             "binary_sha256": _digest("subset-binary"),
             "config_sha256": _digest("subset-config"),
             "initial_scalars": {"numerator": raw_four,
                                 "denominator": raw_two,
                                 "uncommitted": 0, "kept": 0},
             "boundary_commitments": {
                 kept_name: hashlib.sha256(
                     struct.pack("<Q", raw_two)
                 ).hexdigest(),
             }},
            (), invocations, {"primitive_records": 8},
        )
        boundary_map = replay._write_lazy_boundary_map(
            lazy.read_bundle(trace), self.root / "subset-boundary-map.txt"
        )
        lines = boundary_map.read_text(encoding="ascii").splitlines()
        self.assertEqual(lines[1], "1")
        self.assertEqual(bytes.fromhex(lines[2].split()[0]).decode(), kept_name)

    def test_window_evidence_keeps_positive_fixed_roi_ticks_separate(self):
        dynamic = {"sim_ticks": 12345, "verification": "pass"}
        fixed = {"sim_ticks": 321, "verification": "pass"}
        fixed_root = self.root / "fixed-input"
        fixed_root.mkdir()
        fixed_trace = fixed_root / "trace.bin"
        fixed_trace.write_bytes(b"fixed-control-trace")
        combined = replay.combine_window_evidence(
            dynamic, fixed, fixed_trace=fixed_trace,
        )
        self.assertEqual(combined["sim_ticks"], 12345)
        self.assertEqual(combined["fixed_sim_ticks"], 321)
        self.assertGreater(combined["fixed_sim_ticks"], 0)
        self.assertEqual(
            combined["fixed_trace_sha256"], replay._sha256_file(fixed_trace)
        )
        self.assertNotEqual(
            combined["sim_ticks"], combined["fixed_sim_ticks"]
        )

    def test_window_materialization_rejects_trace_or_phase_drift(self):
        operations = (
            _op(canonical.Opcode.LOAD_U64, 0, work_item=0,
                address=0x7300, left=31, result=31),
            _op(canonical.Opcode.COMMIT, 1, work_item=0,
                address=0x7400, left=31, result=31),
        )
        trace = self.root / "eager-window"
        canonical.write_bundle(
            trace, _meta("eager_window", 1), operations, {},
            initial_memory=_initial_memory(operations),
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
        canonical.write_bundle(
            bundle, _meta("evidence", 1), operations, {},
            initial_memory=_initial_memory(operations),
        )
        run_dir = self.root / "run"
        run_dir.mkdir()
        (run_dir / "result.json").write_text(json.dumps({
            "verification": "pass", "threads": 4, "phases": 1,
            "commit_order": [1], "raw_outputs": [17],
            "output_boundaries": {},
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
        canonical.write_bundle(
            bundle, _meta("bad_commit", 1), operations, {},
            initial_memory=_initial_memory(operations),
        )
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
