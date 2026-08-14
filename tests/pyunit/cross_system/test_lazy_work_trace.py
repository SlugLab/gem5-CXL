# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

import hashlib
import dataclasses
import itertools
import json
import struct
import tempfile
import tracemalloc
import unittest
from pathlib import Path

from scripts import canonical_work_trace as canonical
from scripts import lazy_work_trace as lazy


F64 = struct.Struct("<d")
U64 = struct.Struct("<Q")


def digest(payload):
    if isinstance(payload, str):
        payload = payload.encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def raw_f64(value):
    return U64.unpack(F64.pack(value))[0]


def operation(phase, opcode, work_item, address, left, right, result):
    return canonical.Operation(
        phase=phase,
        opcode=opcode,
        work_item=work_item,
        sequence=0,
        address=address,
        operand0=raw_f64(left),
        operand1=raw_f64(right) if right is not None else 0,
        result=raw_f64(result),
    )


def expand_fixture_add(state, invocation, batch_work_items):
    yield canonical.Operation(
        invocation.phase, canonical.Opcode.BARRIER,
        invocation.iteration, 0, 0, 0, invocation.work_items, 0,
    )
    addend = F64.unpack(U64.pack(invocation.parameters["addend_raw"]))[0]
    for first in range(0, invocation.work_items, batch_work_items):
        last = min(first + batch_work_items, invocation.work_items)
        for index in range(first, last):
            address, value = state.load_float("x", index)
            result = value + addend
            yield operation(
                invocation.phase, canonical.Opcode.LOAD_F64,
                index, address, value, None, value,
            )
            yield operation(
                invocation.phase, canonical.Opcode.F64_ADD,
                index, 0, value, addend, result,
            )
            state.store_float("x", index, result)
            yield operation(
                invocation.phase, canonical.Opcode.STORE_F64,
                index, address, result, None, result,
            )
    yield canonical.Operation(
        invocation.phase, canonical.Opcode.COMMIT,
        invocation.iteration, 0, 0, 0, 0, 0,
    )


EXPANDERS = {"fixture_add": expand_fixture_add}


class LazyTraceTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def make_bundle(self):
        image = self.root / "images/x.f64"
        image.parent.mkdir(parents=True, exist_ok=True)
        payload = F64.pack(1.0) + F64.pack(2.0)
        image.write_bytes(payload)
        array = lazy.ArrayImage(
            name="x", role="state", element_type="f64", count=2,
            logical_base=0x1000, path="images/x.f64", sha256=digest(payload),
        )
        invocation = lazy.Invocation(
            ordinal=0, phase=7, kernel="fixture_add", iteration=3,
            work_items=2, parameters={"addend_raw": raw_f64(0.5)},
        )
        lazy.write_bundle(
            self.root,
            {
                "schema": 2,
                "workload": "fixture_add",
                "source_sha256": digest("source"),
                "binary_sha256": digest("binary"),
                "config_sha256": digest("config"),
            },
            (array,), (invocation,), {"primitive_records": 8},
        )
        return lazy.read_bundle(self.root)

    def expected_operations(self):
        rows = [
            canonical.Operation(
                7, canonical.Opcode.BARRIER, 3, 0, 0, 0, 2, 0,
            )
        ]
        for index, value in enumerate((1.0, 2.0)):
            result = value + 0.5
            rows.extend((
                operation(7, canonical.Opcode.LOAD_F64, index,
                          0x1000 + index * 8, value, None, value),
                operation(7, canonical.Opcode.F64_ADD, index,
                          0, value, 0.5, result),
                operation(7, canonical.Opcode.STORE_F64, index,
                          0x1000 + index * 8, result, None, result),
            ))
        rows.append(canonical.Operation(
            7, canonical.Opcode.COMMIT, 3, 0, 0, 0, 0, 0,
        ))
        return tuple(
            dataclasses.replace(row, sequence=index)
            for index, row in enumerate(rows)
        )

    def test_schema_two_round_trip_preserves_descriptors(self):
        bundle = self.make_bundle()
        self.assertEqual(bundle.meta["schema"], 2)
        self.assertRegex(bundle.meta["input_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(bundle.arrays[0].name, "x")
        self.assertEqual(bundle.invocations[0].kernel, "fixture_add")
        self.assertEqual(bundle.dynamic_work["primitive_records"], 8)

    def test_claimed_input_hash_drift_is_rejected(self):
        self.make_bundle()
        descriptor = self.root / "trace.v2.json"
        value = json.loads(descriptor.read_text())
        value["meta"]["input_sha256"] = digest("caller-controlled")
        descriptor.write_text(json.dumps(value, sort_keys=True) + "\n")
        with self.assertRaisesRegex(lazy.LazyTraceError, "input SHA-256"):
            lazy.read_bundle(self.root)

    def test_lazy_expansion_matches_eager_and_is_batch_invariant(self):
        bundle = self.make_bundle()
        eager = self.expected_operations()
        for batch in (1, 7):
            observed = tuple(lazy.iter_operations(
                bundle, EXPANDERS, batch_work_items=batch,
            ))
            self.assertEqual(observed, eager)
        self.assertEqual(
            lazy.expanded_fingerprint(bundle, EXPANDERS),
            (canonical.operations_sha256(eager), len(eager)),
        )

    def test_mapped_state_exposes_raw_words_and_boundary_digest(self):
        bundle = self.make_bundle()
        with lazy.MappedState(bundle) as state:
            address, raw = state.load_raw("x", 1)
            self.assertEqual((address, raw), (0x1008, raw_f64(2.0)))
            state.store_raw("x", 1, raw_f64(3.0))
            self.assertEqual(
                state.boundary_sha256("x"),
                digest(F64.pack(1.0) + F64.pack(3.0)),
            )
            self.assertEqual(
                state.boundary_sha256("x", 1), digest(F64.pack(1.0))
            )
            with self.assertRaisesRegex(lazy.LazyTraceError, "count"):
                state.boundary_sha256("x", 3)

    def test_memory_operation_outside_declared_images_is_rejected(self):
        bundle = self.make_bundle()

        def bad_address(_state, invocation, _batch):
            yield canonical.Operation(
                invocation.phase, canonical.Opcode.LOAD_F64,
                0, 0, 0xDEAD, raw_f64(1.0), 0, raw_f64(1.0),
            )

        with self.assertRaisesRegex(lazy.LazyTraceError, "declared image"):
            tuple(lazy.iter_operations(
                dataclasses.replace(
                    bundle,
                    dynamic_work={"primitive_records": 1},
                ),
                {"fixture_add": bad_address},
            ))

    def test_one_bit_image_change_fails_before_iteration(self):
        self.make_bundle()
        image = self.root / "images/x.f64"
        payload = bytearray(image.read_bytes())
        payload[0] ^= 1
        image.write_bytes(payload)
        with self.assertRaisesRegex(lazy.LazyTraceError, "SHA-256"):
            lazy.read_bundle(self.root)

    def test_overlapping_logical_images_are_rejected(self):
        root = self.root / "overlap"
        images = root / "images"
        images.mkdir(parents=True)
        arrays = []
        for name, base in (("x", 0x1000), ("y", 0x1008)):
            payload = F64.pack(1.0) + F64.pack(2.0)
            path = images / f"{name}.f64"
            path.write_bytes(payload)
            arrays.append(lazy.ArrayImage(
                name, "state", "f64", 2, base,
                f"images/{name}.f64", digest(payload),
            ))
        with self.assertRaisesRegex(lazy.LazyTraceError, "overlap"):
            lazy.write_bundle(
                root,
                {
                    "schema": 2, "workload": "fixture_add",
                    "source_sha256": digest("source"),
                    "binary_sha256": digest("binary"),
                    "config_sha256": digest("config"),
                },
                arrays, (), {"primitive_records": 0},
            )

    def test_unknown_kernel_is_rejected(self):
        bundle = self.make_bundle()
        with self.assertRaisesRegex(lazy.LazyTraceError, "unknown kernel"):
            tuple(lazy.iter_operations(bundle, {}))

    def test_descriptor_rejects_decimal_floating_parameters(self):
        bundle = self.make_bundle()
        root = self.root / "decimal"
        image = root / "images/x.f64"
        image.parent.mkdir(parents=True)
        source = self.root / bundle.arrays[0].path
        image.write_bytes(source.read_bytes())
        with self.assertRaisesRegex(lazy.LazyTraceError, "raw integer"):
            bad = dataclasses.replace(
                bundle.invocations[0], parameters={"addend": 0.5},
            )
            lazy.write_bundle(
                root, bundle.meta,
                (dataclasses.replace(bundle.arrays[0], path="images/x.f64"),),
                (bad,), bundle.dynamic_work,
            )

    def test_streaming_memory_is_independent_of_declared_work_count(self):
        bundle = self.make_bundle()
        invocation = dataclasses.replace(
            bundle.invocations[0], work_items=10_000_000,
        )
        large = dataclasses.replace(
            bundle,
            invocations=(invocation,),
            dynamic_work={"primitive_records": 10_000_000},
        )

        def arithmetic_only(_state, row, batch):
            for first in range(0, row.work_items, batch):
                for index in range(first, min(first + batch, row.work_items)):
                    yield canonical.Operation(
                        row.phase, canonical.Opcode.F64_ADD,
                        index, 0, 0, raw_f64(1.0), raw_f64(2.0), raw_f64(3.0),
                    )

        stream = lazy.iter_operations(
            large, {"fixture_add": arithmetic_only}, batch_work_items=257,
        )
        tracemalloc.start()
        try:
            consumed = sum(1 for _ in itertools.islice(stream, 100_000))
            _current, peak = tracemalloc.get_traced_memory()
        finally:
            stream.close()
            tracemalloc.stop()
        self.assertEqual(consumed, 100_000)
        self.assertLess(peak, 16 * 1024 * 1024)


if __name__ == "__main__":
    unittest.main()
