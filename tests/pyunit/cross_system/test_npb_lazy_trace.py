# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

import dataclasses
import hashlib
import math
import struct
import tempfile
import unittest
from pathlib import Path

from scripts import canonical_work_trace as canonical
from scripts import lazy_work_trace as lazy
from scripts import npb_lazy_trace as npb


U32 = struct.Struct("<I")
U64 = struct.Struct("<Q")
F64 = struct.Struct("<d")


def raw_f64(value):
    return U64.unpack(F64.pack(value))[0]


def digest(payload):
    return hashlib.sha256(payload).hexdigest()


def with_sequences(rows):
    return tuple(
        dataclasses.replace(row, sequence=index)
        for index, row in enumerate(rows)
    )


def without_scalar_stores(rows):
    return tuple(
        dataclasses.replace(row, sequence=index)
        for index, row in enumerate(
            row for row in rows
            if not (
                row.opcode == canonical.Opcode.STORE_F64
                and row.address >= 0x7000000000000000
            )
        )
    )


def control(phase, opcode, iteration, work_items=0):
    return canonical.Operation(
        phase, opcode, iteration, 0, 0, 0, work_items, 0,
    )


def lanes(count):
    return [[count * lane // 4, count * (lane + 1) // 4]
            for lane in range(4)]


def load(phase, opcode, work_item, address, raw):
    return canonical.Operation(
        phase, opcode, work_item, 0, address, raw, 0, raw,
    )


def binary(phase, opcode, work_item, left, right, result):
    return canonical.Operation(
        phase, opcode, work_item, 0, 0,
        raw_f64(left), raw_f64(right), raw_f64(result),
    )


def store(phase, work_item, address, value):
    raw = raw_f64(value)
    return canonical.Operation(
        phase, canonical.Opcode.STORE_F64, work_item, 0,
        address, raw, 0, raw,
    )


def unary(phase, opcode, work_item, operand, result):
    return canonical.Operation(
        phase, opcode, work_item, 0, 0,
        raw_f64(operand), 0, raw_f64(result),
    )


class NpbCgLazyTraceTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def image(self, name, element_type, values, base, role="input"):
        formats = {"u32": "I", "f64": "d"}
        payload = struct.pack(f"<{len(values)}{formats[element_type]}", *values)
        path = self.root / f"images/{name}.{element_type}"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        return lazy.ArrayImage(
            name, role, element_type, len(values), base,
            path.relative_to(self.root).as_posix(), digest(payload),
        )

    def spmv_bundle(self):
        arrays = (
            self.image("rowstr", "u32", (0, 2, 3, 5), 0x1000),
            self.image("colidx", "u32", (0, 2, 1, 0, 2), 0x2000),
            self.image("a", "f64", (2.0, 1.0, 3.0, -1.0, 4.0), 0x3000),
            self.image("p", "f64", (1.0, 2.0, 3.0), 0x4000),
            self.image("q", "f64", (0.0, 0.0, 0.0), 0x5000, "state"),
        )
        invocation = lazy.Invocation(
            ordinal=0, phase=101, kernel="npb_cg_spmv", iteration=9,
            work_items=3,
            parameters={
                "rowstr": "rowstr", "colidx": "colidx", "values": "a",
                "source": "p", "destination": "q", "row_count": 3,
                "edge_base": 0, "column_base": 0,
                "destination_count": 3,
            },
        )
        lazy.write_bundle(
            self.root,
            {
                "schema": 2, "workload": "npb_cg",
                "source_sha256": "1" * 64,
                "binary_sha256": "2" * 64,
                "config_sha256": "3" * 64,
            },
            arrays, (invocation,), {"primitive_records": 36},
        )
        return lazy.read_bundle(self.root)

    def expected_spmv(self):
        phase = 101
        rowstr = (0, 2, 3, 5)
        colidx = (0, 2, 1, 0, 2)
        values = (2.0, 1.0, 3.0, -1.0, 4.0)
        source = (1.0, 2.0, 3.0)
        rows = [control(phase, canonical.Opcode.BARRIER, 9, 3)]
        for row in range(3):
            rows.append(load(
                phase, canonical.Opcode.LOAD_U32, row,
                0x1000 + row * 4, rowstr[row],
            ))
            rows.append(load(
                phase, canonical.Opcode.LOAD_U32, row,
                0x1000 + (row + 1) * 4, rowstr[row + 1],
            ))
            total = 0.0
            for edge in range(rowstr[row], rowstr[row + 1]):
                column = colidx[edge]
                rows.append(load(
                    phase, canonical.Opcode.LOAD_U32, row,
                    0x2000 + edge * 4, column,
                ))
                rows.append(load(
                    phase, canonical.Opcode.LOAD_F64, row,
                    0x3000 + edge * 8, raw_f64(values[edge]),
                ))
                rows.append(load(
                    phase, canonical.Opcode.LOAD_F64, row,
                    0x4000 + column * 8, raw_f64(source[column]),
                ))
                product = values[edge] * source[column]
                rows.append(binary(
                    phase, canonical.Opcode.F64_MUL, row,
                    values[edge], source[column], product,
                ))
                updated = total + product
                rows.append(binary(
                    phase, canonical.Opcode.F64_ADD, row,
                    total, product, updated,
                ))
                total = updated
            rows.append(store(phase, row, 0x5000 + row * 8, total))
        rows.append(control(phase, canonical.Opcode.COMMIT, 9))
        return with_sequences(rows)

    def dot_bundle(self):
        arrays = (
            self.image("p", "f64", (1.0, 2.0, 3.0, 4.0, 5.0), 0x4000),
            self.image("q", "f64", (6.0, 7.0, 8.0, 9.0, 10.0), 0x5000),
        )
        invocation = lazy.Invocation(
            ordinal=0, phase=103, kernel="npb_cg_dot", iteration=4,
            work_items=5,
            parameters={
                "left": "p", "right": "q", "result": "d",
                "lanes": lanes(5),
            },
        )
        lazy.write_bundle(
            self.root,
            {
                "schema": 2, "workload": "npb_cg",
                "source_sha256": "1" * 64,
                "binary_sha256": "2" * 64,
                "config_sha256": "3" * 64,
            },
            arrays, (invocation,), {"primitive_records": 26},
        )
        return lazy.read_bundle(self.root)

    def divide_bundle(self):
        invocation = lazy.Invocation(
            ordinal=0, phase=103, kernel="npb_cg_divide", iteration=5,
            work_items=1,
            parameters={
                "numerator": "rho0", "denominator": "d",
                "result": "alpha",
            },
        )
        lazy.write_bundle(
            self.root,
            {
                "schema": 2, "workload": "npb_cg",
                "source_sha256": "1" * 64,
                "binary_sha256": "2" * 64,
                "config_sha256": "3" * 64,
                "initial_scalars": {
                    "rho0": raw_f64(10.0), "d": raw_f64(4.0),
                },
            },
            (), (invocation,), {"primitive_records": 4},
        )
        return lazy.read_bundle(self.root)

    def update_zr_bundle(self):
        arrays = (
            self.image("z", "f64", (0.0, 1.0, 2.0, 3.0, 4.0),
                       0x6000, "state"),
            self.image("p", "f64", (1.0, 1.0, 1.0, 1.0, 1.0), 0x4000),
            self.image("r", "f64", (5.0, 4.0, 3.0, 2.0, 1.0),
                       0x7000, "state"),
            self.image("q", "f64", (2.0, 2.0, 2.0, 2.0, 2.0), 0x5000),
        )
        invocation = lazy.Invocation(
            ordinal=0, phase=102, kernel="npb_cg_update_zr", iteration=6,
            work_items=5,
            parameters={
                "z": "z", "p": "p", "r": "r", "q": "q",
                "alpha": "alpha", "result": "rho",
                "boundaries": ["z", "r"], "boundary_counts": {},
                "lanes": lanes(5),
            },
        )
        lazy.write_bundle(
            self.root,
            {
                "schema": 2, "workload": "npb_cg",
                "source_sha256": "1" * 64,
                "binary_sha256": "2" * 64,
                "config_sha256": "3" * 64,
                "initial_scalars": {"alpha": raw_f64(0.5)},
            },
            arrays, (invocation,), {"primitive_records": 66},
        )
        return lazy.read_bundle(self.root)

    def update_p_bundle(self):
        arrays = (
            self.image("r", "f64", (1.0, 2.0, 3.0), 0x7000),
            self.image("p", "f64", (4.0, 5.0, 6.0), 0x4000, "state"),
        )
        invocation = lazy.Invocation(
            ordinal=0, phase=102, kernel="npb_cg_update_p", iteration=7,
            work_items=3,
            parameters={
                "r": "r", "p": "p", "beta": "beta",
                "boundaries": ["p"], "boundary_counts": {},
            },
        )
        lazy.write_bundle(
            self.root,
            {
                "schema": 2, "workload": "npb_cg",
                "source_sha256": "1" * 64,
                "binary_sha256": "2" * 64,
                "config_sha256": "3" * 64,
                "initial_scalars": {"beta": raw_f64(0.5)},
            },
            arrays, (invocation,), {"primitive_records": 17},
        )
        return lazy.read_bundle(self.root)

    def residual_norm_bundle(self):
        arrays = (
            self.image("x", "f64", (4.0, 5.0, 6.0), 0x8000),
            self.image("r", "f64", (1.0, 1.0, 2.0), 0x7000),
        )
        invocation = lazy.Invocation(
            ordinal=0, phase=103, kernel="npb_cg_residual_norm",
            iteration=8, work_items=3,
            parameters={
                "x": "x", "r": "r", "result": "rnorm",
                "lanes": lanes(3),
            },
        )
        lazy.write_bundle(
            self.root,
            {
                "schema": 2, "workload": "npb_cg",
                "source_sha256": "1" * 64,
                "binary_sha256": "2" * 64,
                "config_sha256": "3" * 64,
            },
            arrays, (invocation,), {"primitive_records": 22},
        )
        return lazy.read_bundle(self.root)

    def init_bundle(self):
        arrays = (
            self.image("x", "f64", (1.0, 2.0, 3.0), 0x8000),
            self.image("q", "f64", (9.0, 9.0, 9.0), 0x5000, "state"),
            self.image("z", "f64", (9.0, 9.0, 9.0), 0x6000, "state"),
            self.image("r", "f64", (9.0, 9.0, 9.0), 0x7000, "state"),
            self.image("p", "f64", (9.0, 9.0, 9.0), 0x4000, "state"),
        )
        invocation = lazy.Invocation(
            ordinal=0, phase=102, kernel="npb_cg_init", iteration=0,
            work_items=3,
            parameters={
                "x": "x", "q": "q", "z": "z", "r": "r", "p": "p",
                "boundaries": ["q", "z", "r", "p"],
            },
        )
        lazy.write_bundle(
            self.root,
            {
                "schema": 2, "workload": "npb_cg",
                "source_sha256": "1" * 64,
                "binary_sha256": "2" * 64,
                "config_sha256": "3" * 64,
            },
            arrays, (invocation,), {"primitive_records": 20},
        )
        return lazy.read_bundle(self.root)

    def outer_dots_bundle(self):
        arrays = (
            self.image("x", "f64", (1.0, 2.0, 4.0), 0x8000),
            self.image("z", "f64", (3.0, 5.0, 7.0), 0x6000),
        )
        invocation = lazy.Invocation(
            ordinal=0, phase=103, kernel="npb_cg_outer_dots", iteration=10,
            work_items=3,
            parameters={
                "x": "x", "z": "z",
                "result_xz": "norm1", "result_zz": "norm2",
                "results": ["norm1", "norm2"], "lanes": lanes(3),
            },
        )
        lazy.write_bundle(
            self.root,
            {
                "schema": 2, "workload": "npb_cg",
                "source_sha256": "1" * 64,
                "binary_sha256": "2" * 64,
                "config_sha256": "3" * 64,
            },
            arrays, (invocation,), {"primitive_records": 34},
        )
        return lazy.read_bundle(self.root)

    def normalize_bundle(self):
        arrays = (
            self.image("z", "f64", (3.0, 5.0, 7.0), 0x6000),
            self.image("x", "f64", (0.0, 0.0, 0.0), 0x8000, "state"),
        )
        invocation = lazy.Invocation(
            ordinal=0, phase=102, kernel="npb_cg_normalize", iteration=11,
            work_items=3,
            parameters={
                "z": "z", "x": "x", "norm1": "norm1",
                "norm2": "norm2", "norm3": "norm3", "shift": "shift",
                "zeta": "zeta", "write_zeta": 1,
                "boundaries": ["x"], "boundary_counts": {},
                "results": ["norm3", "zeta"],
            },
        )
        lazy.write_bundle(
            self.root,
            {
                "schema": 2, "workload": "npb_cg",
                "source_sha256": "1" * 64,
                "binary_sha256": "2" * 64,
                "config_sha256": "3" * 64,
                "initial_scalars": {
                    "norm1": raw_f64(41.0), "norm2": raw_f64(83.0),
                    "shift": raw_f64(10.0),
                },
            },
            arrays, (invocation,), {"primitive_records": 17},
        )
        return lazy.read_bundle(self.root)

    def full_cg_bundle(self):
        arrays = (
            self.image("rowstr", "u32", (0, 2, 3, 5), 0x1000),
            self.image("colidx", "u32", (0, 2, 1, 0, 2), 0x2000),
            self.image("a", "f64", (2.0, 1.0, 3.0, -1.0, 4.0), 0x3000),
            self.image("p", "f64", (0.0, 0.0, 0.0), 0x4000, "state"),
            self.image("q", "f64", (0.0, 0.0, 0.0), 0x5000, "state"),
            self.image("z", "f64", (0.0, 0.0, 0.0), 0x6000, "state"),
            self.image("r", "f64", (0.0, 0.0, 0.0), 0x7000, "state"),
            self.image("x", "f64", (1.0, 2.0, 3.0), 0x8000, "state"),
        )
        invocations = []

        def add(phase, kernel, iteration, work_items, parameters):
            invocations.append(lazy.Invocation(
                len(invocations), phase, kernel, iteration,
                work_items, parameters,
            ))

        add(102, "npb_cg_init", 0, 3, {
            "x": "x", "q": "q", "z": "z", "r": "r", "p": "p",
            "boundaries": ["q", "z", "r", "p"],
        })
        add(103, "npb_cg_dot", 0, 3, {
            "left": "r", "right": "r", "result": "rho",
            "lanes": lanes(3),
        })
        for cgit in (1, 2):
            add(103, "npb_cg_prepare_iteration", cgit, 1, {
                "source": "rho", "snapshot": "rho0",
                "zero": ["d", "rho"], "results": ["rho0", "d", "rho"],
            })
            add(101, "npb_cg_spmv", cgit, 3, {
                "rowstr": "rowstr", "colidx": "colidx", "values": "a",
                "source": "p", "destination": "q", "row_count": 3,
                "edge_base": 0, "column_base": 0,
                "destination_count": 3,
            })
            add(103, "npb_cg_dot", cgit, 3, {
                "left": "p", "right": "q", "result": "d",
                "lanes": lanes(3),
            })
            add(103, "npb_cg_divide", cgit * 10 + 1, 1, {
                "numerator": "rho0", "denominator": "d",
                "result": "alpha",
            })
            add(102, "npb_cg_update_zr", cgit, 3, {
                "z": "z", "p": "p", "r": "r", "q": "q",
                "alpha": "alpha", "result": "rho",
                "boundaries": ["z", "r"], "boundary_counts": {},
                "lanes": lanes(3),
            })
            add(102, "npb_cg_divide", cgit * 10 + 2, 1, {
                "numerator": "rho", "denominator": "rho0",
                "result": "beta",
            })
            add(102, "npb_cg_update_p", cgit, 3, {
                "r": "r", "p": "p", "beta": "beta",
                "boundaries": ["p"], "boundary_counts": {},
            })
        add(101, "npb_cg_spmv", 90, 3, {
            "rowstr": "rowstr", "colidx": "colidx", "values": "a",
            "source": "z", "destination": "r", "row_count": 3,
            "edge_base": 0, "column_base": 0,
            "destination_count": 3,
        })
        add(103, "npb_cg_residual_norm", 90, 3, {
            "x": "x", "r": "r", "result": "rnorm",
            "lanes": lanes(3),
        })
        add(103, "npb_cg_outer_dots", 91, 3, {
            "x": "x", "z": "z", "result_xz": "norm1",
            "result_zz": "norm2", "results": ["norm1", "norm2"],
            "lanes": lanes(3),
        })
        add(102, "npb_cg_normalize", 92, 3, {
            "z": "z", "x": "x", "norm1": "norm1", "norm2": "norm2",
            "norm3": "norm3", "shift": "shift", "zeta": "zeta",
            "write_zeta": 1, "boundaries": ["x"],
            "boundary_counts": {},
            "results": ["norm3", "zeta"],
        })
        lazy.write_bundle(
            self.root,
            {
                "schema": 2, "workload": "npb_cg",
                "source_sha256": "1" * 64,
                "binary_sha256": "2" * 64,
                "config_sha256": "3" * 64,
                "initial_scalars": {"shift": raw_f64(10.0)},
            },
            arrays, tuple(invocations), {"primitive_records": 405},
        )
        return lazy.read_bundle(self.root)

    def expected_update_zr(self):
        phase = 102
        z = (0.0, 1.0, 2.0, 3.0, 4.0)
        p = (1.0,) * 5
        r = (5.0, 4.0, 3.0, 2.0, 1.0)
        q = (2.0,) * 5
        alpha = 0.5
        rows = [control(phase, canonical.Opcode.BARRIER, 6, 5)]
        lanes = []
        new_z = list(z)
        new_r = list(r)
        for lane in range(4):
            first = len(z) * lane // 4
            last = len(z) * (lane + 1) // 4
            total = 0.0
            for index in range(first, last):
                rows.extend((
                    load(phase, canonical.Opcode.LOAD_F64, index,
                         0x6000 + index * 8, raw_f64(z[index])),
                    load(phase, canonical.Opcode.LOAD_F64, index,
                         0x4000 + index * 8, raw_f64(p[index])),
                ))
                zp = alpha * p[index]
                rows.append(binary(
                    phase, canonical.Opcode.F64_MUL,
                    index, alpha, p[index], zp,
                ))
                new_z[index] = z[index] + zp
                rows.append(binary(
                    phase, canonical.Opcode.F64_ADD,
                    index, z[index], zp, new_z[index],
                ))
                rows.append(store(
                    phase, index, 0x6000 + index * 8, new_z[index],
                ))
                rows.extend((
                    load(phase, canonical.Opcode.LOAD_F64, index,
                         0x7000 + index * 8, raw_f64(r[index])),
                    load(phase, canonical.Opcode.LOAD_F64, index,
                         0x5000 + index * 8, raw_f64(q[index])),
                ))
                rq = alpha * q[index]
                rows.append(binary(
                    phase, canonical.Opcode.F64_MUL,
                    index, alpha, q[index], rq,
                ))
                new_r[index] = r[index] - rq
                rows.append(binary(
                    phase, canonical.Opcode.F64_SUB,
                    index, r[index], rq, new_r[index],
                ))
                rows.append(store(
                    phase, index, 0x7000 + index * 8, new_r[index],
                ))
                square = new_r[index] * new_r[index]
                rows.append(binary(
                    phase, canonical.Opcode.F64_MUL,
                    index, new_r[index], new_r[index], square,
                ))
                updated = total + square
                rows.append(binary(
                    phase, canonical.Opcode.F64_ADD,
                    index, total, square, updated,
                ))
                total = updated
            lanes.append(total)
        left = lanes[0] + lanes[1]
        right = lanes[2] + lanes[3]
        rho = left + right
        rows.extend((
            binary(phase, canonical.Opcode.F64_ADD, 5,
                   lanes[0], lanes[1], left),
            binary(phase, canonical.Opcode.F64_ADD, 6,
                   lanes[2], lanes[3], right),
            binary(phase, canonical.Opcode.F64_ADD, 7, left, right, rho),
            control(phase, canonical.Opcode.COMMIT, 6),
        ))
        return with_sequences(rows), tuple(new_z), tuple(new_r), rho

    def expected_dot(self):
        phase = 103
        left = (1.0, 2.0, 3.0, 4.0, 5.0)
        right = (6.0, 7.0, 8.0, 9.0, 10.0)
        rows = [control(phase, canonical.Opcode.BARRIER, 4, 5)]
        lanes = []
        for lane in range(4):
            first = len(left) * lane // 4
            last = len(left) * (lane + 1) // 4
            total = 0.0
            for index in range(first, last):
                rows.append(load(
                    phase, canonical.Opcode.LOAD_F64, index,
                    0x4000 + index * 8, raw_f64(left[index]),
                ))
                rows.append(load(
                    phase, canonical.Opcode.LOAD_F64, index,
                    0x5000 + index * 8, raw_f64(right[index]),
                ))
                product = left[index] * right[index]
                rows.append(binary(
                    phase, canonical.Opcode.F64_MUL,
                    index, left[index], right[index], product,
                ))
                updated = total + product
                rows.append(binary(
                    phase, canonical.Opcode.F64_ADD,
                    index, total, product, updated,
                ))
                total = updated
            lanes.append(total)
        merge_left = lanes[0] + lanes[1]
        merge_right = lanes[2] + lanes[3]
        result = merge_left + merge_right
        rows.extend((
            binary(phase, canonical.Opcode.F64_ADD, 5,
                   lanes[0], lanes[1], merge_left),
            binary(phase, canonical.Opcode.F64_ADD, 6,
                   lanes[2], lanes[3], merge_right),
            binary(phase, canonical.Opcode.F64_ADD, 7,
                   merge_left, merge_right, result),
            control(phase, canonical.Opcode.COMMIT, 4),
        ))
        return with_sequences(rows), result

    def test_tiny_cg_spmv_lazy_stream_equals_hand_eager_stream(self):
        bundle = self.spmv_bundle()
        observed = tuple(lazy.iter_operations(bundle, npb.EXPANDERS))
        self.assertEqual(observed, self.expected_spmv())
        self.assertEqual(
            npb.replay_boundaries(bundle),
            {"q.spmv.iter9": digest(F64.pack(5.0) + F64.pack(6.0) + F64.pack(11.0))},
        )

    def test_combined_evidence_rejects_an_expander_phase_change(self):
        bundle = self.spmv_bundle()
        original = npb.EXPANDERS["npb_cg_spmv"]

        def changed_phase(_state, invocation, _batch_work_items):
            yield canonical.Operation(
                invocation.phase + 1, canonical.Opcode.BARRIER,
                invocation.iteration, 0, 0, 0, invocation.work_items, 0,
            )

        npb.EXPANDERS["npb_cg_spmv"] = changed_phase
        try:
            with self.assertRaisesRegex(
                lazy.LazyTraceError, "changed invocation phase",
            ):
                npb.expanded_evidence(bundle)
        finally:
            npb.EXPANDERS["npb_cg_spmv"] = original

    def test_combined_evidence_rejects_zero_batch_size(self):
        with self.assertRaisesRegex(
            lazy.LazyTraceError, "batch work items is zero",
        ):
            npb.expanded_evidence(self.spmv_bundle(), batch_work_items=0)

    def test_tiny_cg_dot_uses_explicit_four_lane_tree(self):
        bundle = self.dot_bundle()
        expected, result = self.expected_dot()
        self.assertEqual(
            without_scalar_stores(lazy.iter_operations(bundle, npb.EXPANDERS)), expected,
        )
        self.assertEqual(
            npb.replay_boundaries(bundle),
            {"scalar.d.dot.iter4": digest(U64.pack(raw_f64(result)))},
        )

    def test_canonical_abi_exposes_f64_division(self):
        self.assertEqual(canonical.Opcode.F64_DIV.value, 20)

    def test_canonical_abi_exposes_f64_square_root(self):
        self.assertEqual(canonical.Opcode.F64_SQRT.value, 21)

    def test_canonical_abi_exposes_f64_scalar_move(self):
        self.assertEqual(canonical.Opcode.F64_MOV.value, 22)

    def test_cg_prepare_iteration_snapshots_and_zeros_scalars(self):
        invocation = lazy.Invocation(
            ordinal=0, phase=103, kernel="npb_cg_prepare_iteration",
            iteration=12, work_items=1,
            parameters={
                "source": "rho", "snapshot": "rho0",
                "zero": ["d", "rho"], "results": ["rho0", "d", "rho"],
            },
        )
        lazy.write_bundle(
            self.root,
            {
                "schema": 2, "workload": "npb_cg",
                "source_sha256": "1" * 64,
                "binary_sha256": "2" * 64,
                "config_sha256": "3" * 64,
                "initial_scalars": {"rho": raw_f64(17.0)},
            },
            (), (invocation,), {"primitive_records": 8},
        )
        bundle = lazy.read_bundle(self.root)
        rows = (
            control(103, canonical.Opcode.BARRIER, 12, 1),
            unary(103, canonical.Opcode.F64_MOV, 0, 17.0, 17.0),
            unary(103, canonical.Opcode.F64_MOV, 1, 0.0, 0.0),
            unary(103, canonical.Opcode.F64_MOV, 2, 0.0, 0.0),
            control(103, canonical.Opcode.COMMIT, 12),
        )
        self.assertEqual(
            without_scalar_stores(lazy.iter_operations(bundle, npb.EXPANDERS)),
            with_sequences(rows),
        )
        self.assertEqual(npb.replay_boundaries(bundle), {
            "scalar.rho0.prepare_iteration.iter12": digest(U64.pack(raw_f64(17.0))),
            "scalar.d.prepare_iteration.iter12": digest(U64.pack(raw_f64(0.0))),
            "scalar.rho.prepare_iteration.iter12": digest(U64.pack(raw_f64(0.0))),
        })

    def test_cg_scalar_division_is_explicit_and_bit_exact(self):
        bundle = self.divide_bundle()
        expected = with_sequences((
            control(103, canonical.Opcode.BARRIER, 5, 1),
            binary(103, canonical.Opcode.F64_DIV, 0,
                   10.0, 4.0, 2.5),
            control(103, canonical.Opcode.COMMIT, 5),
        ))
        self.assertEqual(
            without_scalar_stores(lazy.iter_operations(bundle, npb.EXPANDERS)), expected,
        )
        self.assertEqual(
            npb.replay_boundaries(bundle),
            {"scalar.alpha.divide.iter5": digest(U64.pack(raw_f64(2.5)))},
        )

    def test_cg_update_zr_and_rho_preserves_expression_and_lane_order(self):
        bundle = self.update_zr_bundle()
        expected, z, r, rho = self.expected_update_zr()
        self.assertEqual(
            without_scalar_stores(lazy.iter_operations(bundle, npb.EXPANDERS)), expected,
        )
        self.assertEqual(
            npb.replay_boundaries(bundle),
            {
                "z.update_zr.iter6": digest(b"".join(F64.pack(value) for value in z)),
                "r.update_zr.iter6": digest(b"".join(F64.pack(value) for value in r)),
                "scalar.rho.update_zr.iter6": digest(U64.pack(raw_f64(rho))),
            },
        )

    def test_cg_update_p_preserves_separate_multiply_and_add(self):
        bundle = self.update_p_bundle()
        rows = [control(102, canonical.Opcode.BARRIER, 7, 3)]
        expected_values = []
        for index, (r, p) in enumerate(zip((1.0, 2.0, 3.0),
                                            (4.0, 5.0, 6.0))):
            product = 0.5 * p
            result = r + product
            expected_values.append(result)
            rows.extend((
                load(102, canonical.Opcode.LOAD_F64, index,
                     0x7000 + index * 8, raw_f64(r)),
                load(102, canonical.Opcode.LOAD_F64, index,
                     0x4000 + index * 8, raw_f64(p)),
                binary(102, canonical.Opcode.F64_MUL, index,
                       0.5, p, product),
                binary(102, canonical.Opcode.F64_ADD, index,
                       r, product, result),
                store(102, index, 0x4000 + index * 8, result),
            ))
        rows.append(control(102, canonical.Opcode.COMMIT, 7))
        self.assertEqual(
            without_scalar_stores(lazy.iter_operations(bundle, npb.EXPANDERS)),
            with_sequences(rows),
        )
        self.assertEqual(
            npb.replay_boundaries(bundle),
            {"p.update_p.iter7": digest(b"".join(
                F64.pack(value) for value in expected_values
            ))},
        )

    def test_cg_residual_norm_emits_sub_square_tree_and_sqrt(self):
        bundle = self.residual_norm_bundle()
        x = (4.0, 5.0, 6.0)
        r = (1.0, 1.0, 2.0)
        rows = [control(103, canonical.Opcode.BARRIER, 8, 3)]
        lanes = []
        for lane in range(4):
            first = len(x) * lane // 4
            last = len(x) * (lane + 1) // 4
            total = 0.0
            for index in range(first, last):
                rows.extend((
                    load(103, canonical.Opcode.LOAD_F64, index,
                         0x8000 + index * 8, raw_f64(x[index])),
                    load(103, canonical.Opcode.LOAD_F64, index,
                         0x7000 + index * 8, raw_f64(r[index])),
                ))
                difference = x[index] - r[index]
                square = difference * difference
                updated = total + square
                rows.extend((
                    binary(103, canonical.Opcode.F64_SUB, index,
                           x[index], r[index], difference),
                    binary(103, canonical.Opcode.F64_MUL, index,
                           difference, difference, square),
                    binary(103, canonical.Opcode.F64_ADD, index,
                           total, square, updated),
                ))
                total = updated
            lanes.append(total)
        left = lanes[0] + lanes[1]
        right = lanes[2] + lanes[3]
        total = left + right
        result = math.sqrt(total)
        rows.extend((
            binary(103, canonical.Opcode.F64_ADD, 3,
                   lanes[0], lanes[1], left),
            binary(103, canonical.Opcode.F64_ADD, 4,
                   lanes[2], lanes[3], right),
            binary(103, canonical.Opcode.F64_ADD, 5,
                   left, right, total),
            unary(103, canonical.Opcode.F64_SQRT, 6, total, result),
            control(103, canonical.Opcode.COMMIT, 8),
        ))
        self.assertEqual(
            without_scalar_stores(lazy.iter_operations(bundle, npb.EXPANDERS)),
            with_sequences(rows),
        )
        self.assertEqual(
            npb.replay_boundaries(bundle),
            {"scalar.rnorm.residual_norm.iter8": digest(U64.pack(raw_f64(result)))},
        )

    def test_cg_initialization_records_source_loads_and_every_store(self):
        bundle = self.init_bundle()
        values = (1.0, 2.0, 3.0)
        rows = [control(102, canonical.Opcode.BARRIER, 0, 3)]
        for index, value in enumerate(values):
            rows.extend((
                store(102, index, 0x5000 + index * 8, 0.0),
                store(102, index, 0x6000 + index * 8, 0.0),
                load(102, canonical.Opcode.LOAD_F64, index,
                     0x8000 + index * 8, raw_f64(value)),
                store(102, index, 0x7000 + index * 8, value),
                load(102, canonical.Opcode.LOAD_F64, index,
                     0x7000 + index * 8, raw_f64(value)),
                store(102, index, 0x4000 + index * 8, value),
            ))
        rows.append(control(102, canonical.Opcode.COMMIT, 0))
        self.assertEqual(
            without_scalar_stores(lazy.iter_operations(bundle, npb.EXPANDERS)),
            with_sequences(rows),
        )
        expected = b"".join(F64.pack(value) for value in values)
        zeros = F64.pack(0.0) * 3
        self.assertEqual(npb.replay_boundaries(bundle), {
            "q.init.iter0": digest(zeros), "z.init.iter0": digest(zeros),
            "r.init.iter0": digest(expected), "p.init.iter0": digest(expected),
        })

    def test_cg_outer_dots_preserve_interleaved_source_order_and_two_trees(self):
        bundle = self.outer_dots_bundle()
        x = (1.0, 2.0, 4.0)
        z = (3.0, 5.0, 7.0)
        rows = [control(103, canonical.Opcode.BARRIER, 10, 3)]
        lanes_xz = []
        lanes_zz = []
        for lane in range(4):
            first = len(x) * lane // 4
            last = len(x) * (lane + 1) // 4
            total_xz = 0.0
            total_zz = 0.0
            for index in range(first, last):
                rows.extend((
                    load(103, canonical.Opcode.LOAD_F64, index,
                         0x8000 + index * 8, raw_f64(x[index])),
                    load(103, canonical.Opcode.LOAD_F64, index,
                         0x6000 + index * 8, raw_f64(z[index])),
                ))
                product_xz = x[index] * z[index]
                next_xz = total_xz + product_xz
                rows.extend((
                    binary(103, canonical.Opcode.F64_MUL, index,
                           x[index], z[index], product_xz),
                    binary(103, canonical.Opcode.F64_ADD, index,
                           total_xz, product_xz, next_xz),
                    load(103, canonical.Opcode.LOAD_F64, index,
                         0x6000 + index * 8, raw_f64(z[index])),
                    load(103, canonical.Opcode.LOAD_F64, index,
                         0x6000 + index * 8, raw_f64(z[index])),
                ))
                product_zz = z[index] * z[index]
                next_zz = total_zz + product_zz
                rows.extend((
                    binary(103, canonical.Opcode.F64_MUL, index,
                           z[index], z[index], product_zz),
                    binary(103, canonical.Opcode.F64_ADD, index,
                           total_zz, product_zz, next_zz),
                ))
                total_xz = next_xz
                total_zz = next_zz
            lanes_xz.append(total_xz)
            lanes_zz.append(total_zz)
        results = []
        for offset, lanes in ((0, lanes_xz), (3, lanes_zz)):
            left = lanes[0] + lanes[1]
            right = lanes[2] + lanes[3]
            result = left + right
            rows.extend((
                binary(103, canonical.Opcode.F64_ADD, 3 + offset,
                       lanes[0], lanes[1], left),
                binary(103, canonical.Opcode.F64_ADD, 4 + offset,
                       lanes[2], lanes[3], right),
                binary(103, canonical.Opcode.F64_ADD, 5 + offset,
                       left, right, result),
            ))
            results.append(result)
        rows.append(control(103, canonical.Opcode.COMMIT, 10))
        self.assertEqual(
            without_scalar_stores(lazy.iter_operations(bundle, npb.EXPANDERS)),
            with_sequences(rows),
        )
        self.assertEqual(npb.replay_boundaries(bundle), {
            "scalar.norm1.outer_dots.iter10": digest(U64.pack(raw_f64(results[0]))),
            "scalar.norm2.outer_dots.iter10": digest(U64.pack(raw_f64(results[1]))),
        })

    def test_cg_normalization_emits_sqrt_div_zeta_and_vector_stores(self):
        bundle = self.normalize_bundle()
        square_root = math.sqrt(83.0)
        norm3 = 1.0 / square_root
        reciprocal = 1.0 / 41.0
        zeta = 10.0 + reciprocal
        rows = [
            control(102, canonical.Opcode.BARRIER, 11, 3),
            unary(102, canonical.Opcode.F64_SQRT, 0, 83.0, square_root),
            binary(102, canonical.Opcode.F64_DIV, 1,
                   1.0, square_root, norm3),
            binary(102, canonical.Opcode.F64_DIV, 2,
                   1.0, 41.0, reciprocal),
            binary(102, canonical.Opcode.F64_ADD, 3,
                   10.0, reciprocal, zeta),
        ]
        values = []
        for index, z in enumerate((3.0, 5.0, 7.0)):
            value = norm3 * z
            values.append(value)
            rows.extend((
                load(102, canonical.Opcode.LOAD_F64, index,
                     0x6000 + index * 8, raw_f64(z)),
                binary(102, canonical.Opcode.F64_MUL, index,
                       norm3, z, value),
                store(102, index, 0x8000 + index * 8, value),
            ))
        rows.append(control(102, canonical.Opcode.COMMIT, 11))
        self.assertEqual(
            without_scalar_stores(lazy.iter_operations(bundle, npb.EXPANDERS)),
            with_sequences(rows),
        )
        self.assertEqual(npb.replay_boundaries(bundle), {
            "x.normalize.iter11": digest(b"".join(F64.pack(value) for value in values)),
            "scalar.norm3.normalize.iter11": digest(U64.pack(raw_f64(norm3))),
            "scalar.zeta.normalize.iter11": digest(U64.pack(raw_f64(zeta))),
        })

    def test_cg_reduction_rejects_one_changed_lane_endpoint(self):
        bundle = self.dot_bundle()
        parameters = dict(bundle.invocations[0].parameters)
        parameters["lanes"] = [[0, 1], [1, 2], [2, 4], [4, 5]]
        corrupted = dataclasses.replace(
            bundle,
            invocations=(dataclasses.replace(
                bundle.invocations[0], parameters=parameters,
            ),),
        )
        with self.assertRaisesRegex(lazy.LazyTraceError, "lane ranges"):
            tuple(lazy.iter_operations(corrupted, npb.EXPANDERS))

    def test_two_step_cg_is_deterministic_and_batch_invariant(self):
        bundle = self.full_cg_bundle()
        fingerprints = {
            lazy.expanded_fingerprint(
                bundle, npb.EXPANDERS, batch_work_items=batch,
            )
            for batch in (1, 2, 17)
        }
        self.assertEqual(len(fingerprints), 1)
        digest_value, count = fingerprints.pop()
        self.assertRegex(digest_value, r"^[0-9a-f]{64}$")
        self.assertEqual(count, 405)
        evidence_hash, evidence_count, boundaries = npb.expanded_evidence(bundle)
        self.assertEqual((evidence_hash, evidence_count), (digest_value, count))
        self.assertEqual({key: boundaries[key] for key in (
            "p.update_p.iter2", "q.spmv.iter2", "z.update_zr.iter2",
            "r.spmv.iter90", "scalar.rnorm.residual_norm.iter90",
            "scalar.zeta.normalize.iter92", "x.normalize.iter92",
        )}, {
            "p.update_p.iter2": "a3b53e2c123ab65b6b3be7bc6cd2310bda1ef47acfdd81cfe4bcee2a96d40056",
            "q.spmv.iter2": "914cf00c1437bb483104959d7029ca9a11ec5ca03e35167aa40c76f49adb7f0f",
            "z.update_zr.iter2": "c8f7c5eda6963c0c2499525363c38ef15bd512e667e62dd943a3ce92d555e8fd",
            "r.spmv.iter90": "d2509a6be14ce92a9a1a3cc0ed43aba5bcd51e8a05d746367e61ff4e846d729a",
            "scalar.rnorm.residual_norm.iter90": "5746cfde1201660e43b77c6200cc5f75f4f48fbafda5b469671f42b6aaf9a183",
            "scalar.zeta.normalize.iter92": "1fb50b16377943c1a719035202ac5aeb31fd194380c2ff2df935e02786fd3a2c",
            "x.normalize.iter92": "f1c1d4650afbddea40a1f16f628db30047f9b9b023935f0c4bd7bc46db385066",
        })

    def test_cg_out_of_range_column_fails_before_commitment(self):
        bundle = self.spmv_bundle()
        image = self.root / "images/colidx.u32"
        payload = bytearray(image.read_bytes())
        U32.pack_into(payload, 0, 99)
        image.write_bytes(payload)
        with self.assertRaisesRegex(lazy.LazyTraceError, "outside image"):
            tuple(lazy.iter_operations(bundle, npb.EXPANDERS))


class NpbMgLazyTraceTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def image(self, name, values, base, role="input"):
        payload = b"".join(F64.pack(value) for value in values)
        path = self.root / f"images/{name}.f64"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        return lazy.ArrayImage(
            name, role, "f64", len(values), base,
            path.relative_to(self.root).as_posix(), digest(payload),
        )

    @staticmethod
    def index(i1, i2, i3, n1, n2, n3):
        del n3
        return i1 + n1 * (i2 + n2 * i3)

    def resid_reference(self, u, v, n1, n2, n3, coefficients):
        result = [0.0] * len(u)
        index = self.index
        a0, _a1, a2, a3 = coefficients
        for i3 in range(1, n3 - 1):
            for i2 in range(1, n2 - 1):
                u1 = []
                u2 = []
                for i1 in range(n1):
                    u1.append(
                        ((u[index(i1, i2 - 1, i3, n1, n2, n3)] +
                          u[index(i1, i2 + 1, i3, n1, n2, n3)]) +
                         u[index(i1, i2, i3 - 1, n1, n2, n3)]) +
                        u[index(i1, i2, i3 + 1, n1, n2, n3)]
                    )
                    u2.append(
                        ((u[index(i1, i2 - 1, i3 - 1, n1, n2, n3)] +
                          u[index(i1, i2 + 1, i3 - 1, n1, n2, n3)]) +
                         u[index(i1, i2 - 1, i3 + 1, n1, n2, n3)]) +
                        u[index(i1, i2 + 1, i3 + 1, n1, n2, n3)]
                    )
                for i1 in range(1, n1 - 1):
                    at = index(i1, i2, i3, n1, n2, n3)
                    value = v[at] - a0 * u[at]
                    value = value - a2 * ((u2[i1] + u1[i1 - 1]) + u1[i1 + 1])
                    value = value - a3 * (u2[i1 - 1] + u2[i1 + 1])
                    result[at] = value
        for i3 in range(1, n3 - 1):
            for i2 in range(1, n2 - 1):
                result[index(0, i2, i3, n1, n2, n3)] = result[index(n1 - 2, i2, i3, n1, n2, n3)]
                result[index(n1 - 1, i2, i3, n1, n2, n3)] = result[index(1, i2, i3, n1, n2, n3)]
            for i1 in range(n1):
                result[index(i1, 0, i3, n1, n2, n3)] = result[index(i1, n2 - 2, i3, n1, n2, n3)]
                result[index(i1, n2 - 1, i3, n1, n2, n3)] = result[index(i1, 1, i3, n1, n2, n3)]
        for i2 in range(n2):
            for i1 in range(n1):
                result[index(i1, i2, 0, n1, n2, n3)] = result[index(i1, i2, n3 - 2, n1, n2, n3)]
                result[index(i1, i2, n3 - 1, n1, n2, n3)] = result[index(i1, i2, 1, n1, n2, n3)]
        return tuple(result)

    def test_mg_zero3_stores_every_padded_grid_element(self):
        values = tuple((index + 1) / 8.0 for index in range(64))
        arrays = (self.image("zero_u", values, 0xF000, "state"),)
        invocation = lazy.Invocation(
            0, 200, "npb_mg_zero3", 0, 64,
            {
                "u": "zero_u", "n1": 4, "n2": 4, "n3": 4,
                "boundaries": ["zero_u"],
            },
        )
        lazy.write_bundle(
            self.root,
            {
                "schema": 2, "workload": "npb_mg",
                "source_sha256": "1" * 64,
                "binary_sha256": "2" * 64,
                "config_sha256": "3" * 64,
            }, arrays, (invocation,), {"primitive_records": 66},
        )
        bundle = lazy.read_bundle(self.root)
        operations = tuple(lazy.iter_operations(bundle, npb.EXPANDERS))
        self.assertEqual(len(operations), 66)
        self.assertEqual(
            sum(row.opcode == canonical.Opcode.STORE_F64 for row in operations),
            64,
        )
        self.assertEqual(npb.replay_boundaries(bundle), {
            "zero_u.zero3.iter0": digest(F64.pack(0.0) * 64)
        })

    def test_mg_boundary_identity_includes_kernel_program_point(self):
        values = tuple((index + 1) / 8.0 for index in range(64))
        arrays = (
            self.image("shared_u", values, 0xF000, "state"),
            self.image("shared_r", values, 0x10000),
        )
        invocations = (
            lazy.Invocation(0, 200, "npb_mg_zero3", 7, 64, {
                "u": "shared_u", "n1": 4, "n2": 4, "n3": 4,
                "boundaries": ["shared_u"],
            }),
            lazy.Invocation(1, 201, "npb_mg_psinv", 7, 8, {
                "r": "shared_r", "u": "shared_u",
                "n1": 4, "n2": 4, "n3": 4,
                "c_raw": [raw_f64(value)
                          for value in (0.75, -0.25, 0.125, 0.0)],
                "boundaries": ["shared_u"],
            }),
        )
        lazy.write_bundle(
            self.root,
            {
                "schema": 2, "workload": "npb_mg",
                "source_sha256": "1" * 64,
                "binary_sha256": "2" * 64,
                "config_sha256": "3" * 64,
            }, arrays, invocations, {"primitive_records": 524},
        )
        boundaries = npb.replay_boundaries(lazy.read_bundle(self.root))
        self.assertEqual(set(boundaries), {
            "shared_u.zero3.iter7", "shared_u.psinv.iter7",
        })
        self.assertNotEqual(
            boundaries["shared_u.zero3.iter7"],
            boundaries["shared_u.psinv.iter7"],
        )

    def test_mg_duplicate_boundary_identity_is_rejected(self):
        arrays = (self.image(
            "duplicate_u", (1.0,) * 64, 0xF000, "state",
        ),)
        parameters = {
            "u": "duplicate_u", "n1": 4, "n2": 4, "n3": 4,
            "boundaries": ["duplicate_u"],
        }
        invocations = tuple(
            lazy.Invocation(ordinal, 200, "npb_mg_zero3", 7, 64, parameters)
            for ordinal in range(2)
        )
        lazy.write_bundle(
            self.root,
            {
                "schema": 2, "workload": "npb_mg",
                "source_sha256": "1" * 64,
                "binary_sha256": "2" * 64,
                "config_sha256": "3" * 64,
            }, arrays, invocations, {"primitive_records": 132},
        )
        with self.assertRaisesRegex(
            lazy.LazyTraceError, "duplicate lazy boundary",
        ):
            npb.replay_boundaries(lazy.read_bundle(self.root))

    def resid_bundle(self):
        n1 = n2 = n3 = 4
        count = n1 * n2 * n3
        u = tuple((index + 1) / 16.0 for index in range(count))
        v = tuple(((-1.0) ** index) * (index + 3) / 32.0
                  for index in range(count))
        r = (0.0,) * count
        coefficients = (1.25, 0.0, -0.5, 0.25)
        arrays = (
            self.image("mg_u", u, 0x10000),
            self.image("mg_v", v, 0x20000),
            self.image("mg_r", r, 0x30000, "state"),
        )
        invocation = lazy.Invocation(
            0, 201, "npb_mg_resid", 1, 8,
            {
                "u": "mg_u", "v": "mg_v", "r": "mg_r",
                "n1": n1, "n2": n2, "n3": n3,
                "a_raw": [raw_f64(value) for value in coefficients],
                "boundaries": ["mg_r"],
            },
        )
        lazy.write_bundle(
            self.root,
            {
                "schema": 2, "workload": "npb_mg",
                "source_sha256": "1" * 64,
                "binary_sha256": "2" * 64,
                "config_sha256": "3" * 64,
            },
            arrays, (invocation,), {"primitive_records": 434},
        )
        return lazy.read_bundle(self.root), u, v, coefficients

    def test_mg_resid_preserves_stencil_grouping_and_comm3_boundaries(self):
        bundle, u, v, coefficients = self.resid_bundle()
        operations = tuple(lazy.iter_operations(bundle, npb.EXPANDERS))
        self.assertEqual(len(operations), 434)
        self.assertEqual(operations[0].opcode, canonical.Opcode.BARRIER)
        self.assertEqual(operations[-1].opcode, canonical.Opcode.COMMIT)
        expected = self.resid_reference(u, v, 4, 4, 4, coefficients)
        self.assertEqual(npb.replay_boundaries(bundle), {
            "mg_r.resid.iter1": digest(b"".join(F64.pack(value) for value in expected))
        })

    def norm_bundle(self):
        n1 = n2 = n3 = 4
        values = tuple((index - 31) / 8.0 for index in range(64))
        arrays = (self.image("mg_norm_r", values, 0x40000),)
        invocation = lazy.Invocation(
            0, 205, "npb_mg_norm2u3", 2, 8,
            {
                "r": "mg_norm_r", "n1": n1, "n2": n2, "n3": n3,
                "dn_raw": raw_f64(8.0), "rnm2": "rnm2", "rnmu": "rnmu",
                "results": ["rnm2", "rnmu"], "lanes": lanes(8),
            },
        )
        lazy.write_bundle(
            self.root,
            {
                "schema": 2, "workload": "npb_mg",
                "source_sha256": "1" * 64,
                "binary_sha256": "2" * 64,
                "config_sha256": "3" * 64,
            },
            arrays, (invocation,), {"primitive_records": 60},
        )
        return lazy.read_bundle(self.root), values

    def test_mg_norm_uses_sum_max_trees_division_and_sqrt(self):
        bundle, values = self.norm_bundle()
        operations = tuple(lazy.iter_operations(bundle, npb.EXPANDERS))
        self.assertEqual(len(operations), 60)
        self.assertEqual(
            sum(row.opcode == canonical.Opcode.F64_MAX for row in operations),
            11,
        )
        self.assertEqual(
            sum(row.opcode == canonical.Opcode.F64_ABS for row in operations),
            8,
        )
        interior = [values[self.index(i1, i2, i3, 4, 4, 4)]
                    for i3 in range(1, 3)
                    for i2 in range(1, 3)
                    for i1 in range(1, 3)]
        lane_sums = []
        lane_maxes = []
        for lane in range(4):
            first, last = lanes(8)[lane]
            total = 0.0
            maximum = 0.0
            for value in interior[first:last]:
                total = total + value * value
                maximum = max(maximum, abs(value))
            lane_sums.append(total)
            lane_maxes.append(maximum)
        total = (lane_sums[0] + lane_sums[1]) + (lane_sums[2] + lane_sums[3])
        maximum = max(max(lane_maxes[0], lane_maxes[1]),
                      max(lane_maxes[2], lane_maxes[3]))
        rnm2 = math.sqrt(total / 8.0)
        self.assertEqual(npb.replay_boundaries(bundle), {
            "scalar.rnm2.norm2u3.iter2": digest(U64.pack(raw_f64(rnm2))),
            "scalar.rnmu.norm2u3.iter2": digest(U64.pack(raw_f64(maximum))),
        })

    def test_canonical_abi_exposes_f64_absolute_value(self):
        self.assertEqual(canonical.Opcode.F64_ABS.value, 23)

    def psinv_reference(self, r, u, n1, n2, n3, coefficients):
        result = list(u)
        index = self.index
        c0, c1, c2, _c3 = coefficients
        for i3 in range(1, n3 - 1):
            for i2 in range(1, n2 - 1):
                r1 = []
                r2 = []
                for i1 in range(n1):
                    r1.append(
                        ((r[index(i1, i2 - 1, i3, n1, n2, n3)] +
                          r[index(i1, i2 + 1, i3, n1, n2, n3)]) +
                         r[index(i1, i2, i3 - 1, n1, n2, n3)]) +
                        r[index(i1, i2, i3 + 1, n1, n2, n3)]
                    )
                    r2.append(
                        ((r[index(i1, i2 - 1, i3 - 1, n1, n2, n3)] +
                          r[index(i1, i2 + 1, i3 - 1, n1, n2, n3)]) +
                         r[index(i1, i2 - 1, i3 + 1, n1, n2, n3)]) +
                        r[index(i1, i2 + 1, i3 + 1, n1, n2, n3)]
                    )
                for i1 in range(1, n1 - 1):
                    at = index(i1, i2, i3, n1, n2, n3)
                    value = result[at] + c0 * r[at]
                    pair = (r[index(i1 + 1, i2, i3, n1, n2, n3)] +
                            r[index(i1 - 1, i2, i3, n1, n2, n3)])
                    value = value + c1 * (r1[i1] + pair)
                    pair = r1[i1 - 1] + r2[i1]
                    value = value + c2 * (r1[i1 + 1] + pair)
                    result[at] = value
        # The routine calls comm3(u).
        for i3 in range(1, n3 - 1):
            for i2 in range(1, n2 - 1):
                result[index(0, i2, i3, n1, n2, n3)] = result[index(n1 - 2, i2, i3, n1, n2, n3)]
                result[index(n1 - 1, i2, i3, n1, n2, n3)] = result[index(1, i2, i3, n1, n2, n3)]
            for i1 in range(n1):
                result[index(i1, 0, i3, n1, n2, n3)] = result[index(i1, n2 - 2, i3, n1, n2, n3)]
                result[index(i1, n2 - 1, i3, n1, n2, n3)] = result[index(i1, 1, i3, n1, n2, n3)]
        for i2 in range(n2):
            for i1 in range(n1):
                result[index(i1, i2, 0, n1, n2, n3)] = result[index(i1, i2, n3 - 2, n1, n2, n3)]
                result[index(i1, i2, n3 - 1, n1, n2, n3)] = result[index(i1, i2, 1, n1, n2, n3)]
        return tuple(result)

    def test_mg_psinv_preserves_expression_grouping_and_comm3(self):
        # Expected operands follow the exact-flag gfortran optimized tree,
        # which is also checked against every native Class S boundary.
        n1 = n2 = n3 = 4
        count = 64
        r = tuple((index - 17) / 16.0 for index in range(count))
        u = tuple((index + 5) / 32.0 for index in range(count))
        coefficients = (0.75, -0.25, 0.125, 0.0)
        arrays = (
            self.image("ps_r", r, 0x50000),
            self.image("ps_u", u, 0x60000, "state"),
        )
        invocation = lazy.Invocation(
            0, 204, "npb_mg_psinv", 3, 8,
            {
                "r": "ps_r", "u": "ps_u",
                "n1": n1, "n2": n2, "n3": n3,
                "c_raw": [raw_f64(value) for value in coefficients],
                "boundaries": ["ps_u"],
            },
        )
        lazy.write_bundle(
            self.root,
            {
                "schema": 2, "workload": "npb_mg",
                "source_sha256": "1" * 64,
                "binary_sha256": "2" * 64,
                "config_sha256": "3" * 64,
            }, arrays, (invocation,), {"primitive_records": 458},
        )
        bundle = lazy.read_bundle(self.root)
        self.assertEqual(
            len(tuple(lazy.iter_operations(bundle, npb.EXPANDERS))), 458
        )
        expected = self.psinv_reference(r, u, n1, n2, n3, coefficients)
        self.assertEqual(npb.replay_boundaries(bundle), {
            "ps_u.psinv.iter3": digest(b"".join(F64.pack(value) for value in expected))
        })

    def rprj_reference(self, fine, fine_dims, coarse_dims):
        m1k, m2k, m3k = fine_dims
        m1j, m2j, m3j = coarse_dims
        coarse = [0.0] * (m1j * m2j * m3j)
        d1 = 2 if m1k == 3 else 1
        d2 = 2 if m2k == 3 else 1
        d3 = 2 if m3k == 3 else 1

        def r(i1, i2, i3):
            return fine[self.index(i1 - 1, i2 - 1, i3 - 1,
                                   m1k, m2k, m3k)]

        for j3 in range(2, m3j):
            for j2 in range(2, m2j):
                i3 = 2 * j3 - d3
                i2 = 2 * j2 - d2
                x1 = {}
                y1 = {}
                for j1 in range(2, m1j + 1):
                    i1 = 2 * j1 - d1
                    x1[i1 - 1] = ((r(i1 - 1, i2 - 1, i3) +
                                  r(i1 - 1, i2 + 1, i3)) +
                                 r(i1 - 1, i2, i3 - 1)) + r(i1 - 1, i2, i3 + 1)
                    y1[i1 - 1] = ((r(i1 - 1, i2 - 1, i3 - 1) +
                                  r(i1 - 1, i2 - 1, i3 + 1)) +
                                 r(i1 - 1, i2 + 1, i3 - 1)) + r(i1 - 1, i2 + 1, i3 + 1)
                for j1 in range(2, m1j):
                    i1 = 2 * j1 - d1
                    y2 = ((r(i1, i2 - 1, i3 - 1) + r(i1, i2 - 1, i3 + 1)) +
                          r(i1, i2 + 1, i3 - 1)) + r(i1, i2 + 1, i3 + 1)
                    x2 = ((r(i1, i2 - 1, i3) + r(i1, i2 + 1, i3)) +
                          r(i1, i2, i3 - 1)) + r(i1, i2, i3 + 1)
                    value = 0.5 * r(i1, i2, i3)
                    value = value + 0.25 * ((r(i1 - 1, i2, i3) +
                                             r(i1 + 1, i2, i3)) + x2)
                    value = value + 0.125 * ((x1[i1 - 1] + x1[i1 + 1]) + y2)
                    value = value + 0.0625 * (y1[i1 - 1] + y1[i1 + 1])
                    coarse[self.index(j1 - 1, j2 - 1, j3 - 1,
                                      m1j, m2j, m3j)] = value
        # comm3 on the coarse result.
        for i3 in range(1, m3j - 1):
            for i2 in range(1, m2j - 1):
                coarse[self.index(0, i2, i3, m1j, m2j, m3j)] = coarse[self.index(m1j - 2, i2, i3, m1j, m2j, m3j)]
                coarse[self.index(m1j - 1, i2, i3, m1j, m2j, m3j)] = coarse[self.index(1, i2, i3, m1j, m2j, m3j)]
            for i1 in range(m1j):
                coarse[self.index(i1, 0, i3, m1j, m2j, m3j)] = coarse[self.index(i1, m2j - 2, i3, m1j, m2j, m3j)]
                coarse[self.index(i1, m2j - 1, i3, m1j, m2j, m3j)] = coarse[self.index(i1, 1, i3, m1j, m2j, m3j)]
        for i2 in range(m2j):
            for i1 in range(m1j):
                coarse[self.index(i1, i2, 0, m1j, m2j, m3j)] = coarse[self.index(i1, i2, m3j - 2, m1j, m2j, m3j)]
                coarse[self.index(i1, i2, m3j - 1, m1j, m2j, m3j)] = coarse[self.index(i1, i2, 1, m1j, m2j, m3j)]
        return tuple(coarse)

    def test_mg_rprj3_preserves_projection_grouping_and_coarse_comm3(self):
        fine_dims = (6, 6, 6)
        coarse_dims = (4, 4, 4)
        fine = tuple((index - 71) / 64.0 for index in range(216))
        coarse = (0.0,) * 64
        arrays = (
            self.image("rp_r", fine, 0x70000),
            self.image("rp_s", coarse, 0x80000, "state"),
        )
        invocation = lazy.Invocation(
            0, 202, "npb_mg_rprj3", 4, 8,
            {
                "r": "rp_r", "s": "rp_s",
                "m1k": 6, "m2k": 6, "m3k": 6,
                "m1j": 4, "m2j": 4, "m3j": 4,
                "boundaries": ["rp_s"],
            },
        )
        lazy.write_bundle(
            self.root,
            {
                "schema": 2, "workload": "npb_mg",
                "source_sha256": "1" * 64,
                "binary_sha256": "2" * 64,
                "config_sha256": "3" * 64,
            }, arrays, (invocation,), {"primitive_records": 522},
        )
        bundle = lazy.read_bundle(self.root)
        self.assertEqual(
            len(tuple(lazy.iter_operations(bundle, npb.EXPANDERS))), 522
        )
        expected = self.rprj_reference(fine, fine_dims, coarse_dims)
        self.assertEqual(npb.replay_boundaries(bundle), {
            "rp_s.rprj3.iter4": digest(b"".join(F64.pack(value) for value in expected))
        })

    def test_mg_rprj3_three_point_level_uses_degenerate_offsets(self):
        fine_dims = coarse_dims = (3, 3, 3)
        fine = tuple((index - 11) / 32.0 for index in range(27))
        arrays = (
            self.image("rd_r", fine, 0x88000),
            self.image("rd_s", (0.0,) * 27, 0x8C000, "state"),
        )
        invocation = lazy.Invocation(
            0, 202, "npb_mg_rprj3", 41, 1,
            {
                "r": "rd_r", "s": "rd_s",
                "m1k": 3, "m2k": 3, "m3k": 3,
                "m1j": 3, "m2j": 3, "m3j": 3,
                "boundaries": ["rd_s"],
            },
        )
        lazy.write_bundle(
            self.root,
            {
                "schema": 2, "workload": "npb_mg",
                "source_sha256": "1" * 64,
                "binary_sha256": "2" * 64,
                "config_sha256": "3" * 64,
            }, arrays, (invocation,), {"primitive_records": 112},
        )
        bundle = lazy.read_bundle(self.root)
        self.assertEqual(
            len(tuple(lazy.iter_operations(bundle, npb.EXPANDERS))), 112
        )
        expected = self.rprj_reference(fine, fine_dims, coarse_dims)
        self.assertEqual(npb.replay_boundaries(bundle), {
            "rd_s.rprj3.iter41": digest(
                b"".join(F64.pack(value) for value in expected)
            )
        })

    def interp_reference(self, coarse, fine, coarse_dims, fine_dims):
        mm1, mm2, mm3 = coarse_dims
        n1, n2, n3 = fine_dims
        result = list(fine)

        def z(i1, i2, i3):
            return coarse[self.index(i1 - 1, i2 - 1, i3 - 1,
                                     mm1, mm2, mm3)]

        def add(i1, i2, i3, value):
            at = self.index(i1 - 1, i2 - 1, i3 - 1, n1, n2, n3)
            result[at] = result[at] + value

        for i3 in range(1, mm3):
            for i2 in range(1, mm2):
                z1 = {}
                z2 = {}
                z3 = {}
                for i1 in range(1, mm1 + 1):
                    z1[i1] = z(i1, i2 + 1, i3) + z(i1, i2, i3)
                    z2[i1] = z(i1, i2, i3 + 1) + z(i1, i2, i3)
                    z3[i1] = (z(i1, i2 + 1, i3 + 1) + z(i1, i2, i3 + 1)) + z1[i1]
                for i1 in range(1, mm1):
                    add(2 * i1 - 1, 2 * i2 - 1, 2 * i3 - 1,
                        z(i1, i2, i3))
                    add(2 * i1, 2 * i2 - 1, 2 * i3 - 1,
                        0.5 * (z(i1 + 1, i2, i3) + z(i1, i2, i3)))
                for i1 in range(1, mm1):
                    add(2 * i1 - 1, 2 * i2, 2 * i3 - 1, 0.5 * z1[i1])
                    add(2 * i1, 2 * i2, 2 * i3 - 1,
                        0.25 * (z1[i1] + z1[i1 + 1]))
                for i1 in range(1, mm1):
                    add(2 * i1 - 1, 2 * i2 - 1, 2 * i3, 0.5 * z2[i1])
                    add(2 * i1, 2 * i2 - 1, 2 * i3,
                        0.25 * (z2[i1] + z2[i1 + 1]))
                for i1 in range(1, mm1):
                    add(2 * i1 - 1, 2 * i2, 2 * i3, 0.25 * z3[i1])
                    add(2 * i1, 2 * i2, 2 * i3,
                        0.125 * (z3[i1] + z3[i1 + 1]))
        return tuple(result)

    def test_mg_interp_normal_path_preserves_all_eight_weighted_updates(self):
        coarse_dims = (4, 4, 4)
        fine_dims = (6, 6, 6)
        coarse = tuple((index - 13) / 32.0 for index in range(64))
        fine = tuple((index + 7) / 128.0 for index in range(216))
        arrays = (
            self.image("ip_z", coarse, 0x90000),
            self.image("ip_u", fine, 0xA0000, "state"),
        )
        invocation = lazy.Invocation(
            0, 203, "npb_mg_interp", 5, 216,
            {
                "z": "ip_z", "u": "ip_u",
                "mm1": 4, "mm2": 4, "mm3": 4,
                "n1": 6, "n2": 6, "n3": 6,
                "boundaries": ["ip_u"],
            },
        )
        lazy.write_bundle(
            self.root,
            {
                "schema": 2, "workload": "npb_mg",
                "source_sha256": "1" * 64,
                "binary_sha256": "2" * 64,
                "config_sha256": "3" * 64,
            }, arrays, (invocation,), {"primitive_records": 1388},
        )
        bundle = lazy.read_bundle(self.root)
        operations = tuple(lazy.iter_operations(bundle, npb.EXPANDERS))
        self.assertEqual(len(operations), 1388)
        self.assertEqual(
            sum(row.opcode == canonical.Opcode.STORE_F64 for row in operations),
            216,
        )
        expected = self.interp_reference(coarse, fine, coarse_dims, fine_dims)
        self.assertEqual(npb.replay_boundaries(bundle), {
            "ip_u.interp.iter5": digest(b"".join(F64.pack(value) for value in expected))
        })

    def interp_degenerate_reference(self, coarse, fine):
        result = list(fine)

        def z(i1, i2, i3):
            return coarse[self.index(i1 - 1, i2 - 1, i3 - 1, 3, 3, 3)]

        def add(i1, i2, i3, value):
            at = self.index(i1 - 1, i2 - 1, i3 - 1, 3, 3, 3)
            result[at] = result[at] + value

        d1 = d2 = d3 = 2
        t1 = t2 = t3 = 1
        for i3 in range(d3, 3):
            for i2 in range(d2, 3):
                for i1 in range(d1, 3):
                    add(2*i1-d1, 2*i2-d2, 2*i3-d3, z(i1, i2, i3))
                for i1 in range(1, 3):
                    add(2*i1-t1, 2*i2-d2, 2*i3-d3,
                        0.5 * (z(i1+1, i2, i3) + z(i1, i2, i3)))
        for i3 in range(d3, 3):
            for i2 in range(1, 3):
                for i1 in range(d1, 3):
                    add(2*i1-d1, 2*i2-t2, 2*i3-d3,
                        0.5 * (z(i1, i2+1, i3) + z(i1, i2, i3)))
                for i1 in range(1, 3):
                    add(2*i1-t1, 2*i2-t2, 2*i3-d3,
                        0.25 * (((z(i1+1, i2+1, i3) + z(i1+1, i2, i3)) +
                                 z(i1, i2+1, i3)) + z(i1, i2, i3)))
        for i3 in range(1, 3):
            for i2 in range(d2, 3):
                for i1 in range(d1, 3):
                    add(2*i1-d1, 2*i2-d2, 2*i3-t3,
                        0.5 * (z(i1, i2, i3+1) + z(i1, i2, i3)))
                for i1 in range(1, 3):
                    add(2*i1-t1, 2*i2-d2, 2*i3-t3,
                        0.25 * (((z(i1+1, i2, i3+1) + z(i1, i2, i3+1)) +
                                 z(i1+1, i2, i3)) + z(i1, i2, i3)))
        for i3 in range(1, 3):
            for i2 in range(1, 3):
                for i1 in range(d1, 3):
                    add(2*i1-d1, 2*i2-t2, 2*i3-t3,
                        0.25 * (((z(i1, i2+1, i3+1) + z(i1, i2, i3+1)) +
                                 z(i1, i2+1, i3)) + z(i1, i2, i3)))
                for i1 in range(1, 3):
                    add(2*i1-t1, 2*i2-t2, 2*i3-t3,
                        0.125 * (((((((z(i1+1, i2+1, i3+1) +
                                      z(i1+1, i2, i3+1)) +
                                     z(i1, i2+1, i3+1)) +
                                    z(i1, i2, i3+1)) +
                                   z(i1+1, i2+1, i3)) +
                                  z(i1+1, i2, i3)) +
                                 z(i1, i2+1, i3)) + z(i1, i2, i3)))
        return tuple(result)

    def test_mg_interp_degenerate_three_point_path_covers_every_cell(self):
        coarse = tuple((index - 9) / 16.0 for index in range(27))
        fine = tuple((index + 1) / 64.0 for index in range(27))
        arrays = (
            self.image("id_z", coarse, 0xB0000),
            self.image("id_u", fine, 0xC0000, "state"),
        )
        invocation = lazy.Invocation(
            0, 203, "npb_mg_interp", 6, 27,
            {
                "z": "id_z", "u": "id_u",
                "mm1": 3, "mm2": 3, "mm3": 3,
                "n1": 3, "n2": 3, "n3": 3,
                "boundaries": ["id_u"],
            },
        )
        lazy.write_bundle(
            self.root,
            {
                "schema": 2, "workload": "npb_mg",
                "source_sha256": "1" * 64,
                "binary_sha256": "2" * 64,
                "config_sha256": "3" * 64,
            }, arrays, (invocation,), {"primitive_records": 332},
        )
        bundle = lazy.read_bundle(self.root)
        operations = tuple(lazy.iter_operations(bundle, npb.EXPANDERS))
        self.assertEqual(len(operations), 332)
        self.assertEqual(
            sum(row.opcode == canonical.Opcode.STORE_F64 for row in operations),
            27,
        )
        expected = self.interp_degenerate_reference(coarse, fine)
        self.assertEqual(npb.replay_boundaries(bundle), {
            "id_u.interp.iter6": digest(b"".join(F64.pack(value) for value in expected))
        })

    def full_vcycle_bundle(self):
        fine_r = tuple((index - 71) / 64.0 for index in range(216))
        fine_v = tuple((index + 11) / 128.0 for index in range(216))
        arrays = (
            self.image("vc_fine_r", fine_r, 0xD0000),
            self.image("vc_coarse_r", (0.0,) * 64, 0xE0000, "state"),
            self.image("vc_coarse_u", (0.0,) * 64, 0xF0000, "state"),
            self.image("vc_fine_u", (0.0,) * 216, 0x100000, "state"),
            self.image("vc_fine_v", fine_v, 0x110000),
            self.image("vc_resid", (0.0,) * 216, 0x120000, "state"),
        )
        invocations = (
            lazy.Invocation(0, 202, "npb_mg_rprj3", 10, 8, {
                "r": "vc_fine_r", "s": "vc_coarse_r",
                "m1k": 6, "m2k": 6, "m3k": 6,
                "m1j": 4, "m2j": 4, "m3j": 4,
                "boundaries": ["vc_coarse_r"],
            }),
            lazy.Invocation(1, 204, "npb_mg_psinv", 11, 8, {
                "r": "vc_coarse_r", "u": "vc_coarse_u",
                "n1": 4, "n2": 4, "n3": 4,
                "c_raw": [raw_f64(value)
                          for value in (0.75, -0.25, 0.125, 0.0)],
                "boundaries": ["vc_coarse_u"],
            }),
            lazy.Invocation(2, 203, "npb_mg_interp", 12, 216, {
                "z": "vc_coarse_u", "u": "vc_fine_u",
                "mm1": 4, "mm2": 4, "mm3": 4,
                "n1": 6, "n2": 6, "n3": 6,
                "boundaries": ["vc_fine_u"],
            }),
            lazy.Invocation(3, 201, "npb_mg_resid", 13, 64, {
                "u": "vc_fine_u", "v": "vc_fine_v", "r": "vc_resid",
                "n1": 6, "n2": 6, "n3": 6,
                "a_raw": [raw_f64(value)
                          for value in (1.25, 0.0, -0.5, 0.25)],
                "boundaries": ["vc_resid"],
            }),
            lazy.Invocation(4, 205, "npb_mg_norm2u3", 14, 64, {
                "r": "vc_resid", "n1": 6, "n2": 6, "n3": 6,
                "dn_raw": raw_f64(64.0), "rnm2": "rnm2", "rnmu": "rnmu",
                "results": ["rnm2", "rnmu"], "lanes": lanes(64),
            }),
        )
        lazy.write_bundle(
            self.root,
            {
                "schema": 2, "workload": "npb_mg",
                "source_sha256": "1" * 64,
                "binary_sha256": "2" * 64,
                "config_sha256": "3" * 64,
            }, arrays, invocations, {"primitive_records": 5182},
        )
        return lazy.read_bundle(self.root)

    def test_mg_mini_vcycle_is_batch_invariant_and_matches_fixed_boundaries(self):
        bundle = self.full_vcycle_bundle()
        fingerprints = {
            lazy.expanded_fingerprint(
                bundle, npb.EXPANDERS, batch_work_items=batch,
            )
            for batch in (1, 2, 17)
        }
        self.assertEqual(len(fingerprints), 1)
        stream_sha256, count = fingerprints.pop()
        self.assertRegex(stream_sha256, r"^[0-9a-f]{64}$")
        self.assertEqual(count, 5182)
        boundaries = npb.replay_boundaries(bundle)
        self.assertEqual({key: boundaries[key] for key in (
            "vc_coarse_r.rprj3.iter10", "vc_coarse_u.psinv.iter11",
            "vc_fine_u.interp.iter12", "vc_resid.resid.iter13",
            "scalar.rnm2.norm2u3.iter14", "scalar.rnmu.norm2u3.iter14",
        )}, {
            "vc_coarse_r.rprj3.iter10": "2b797866313918128011f1ad4e8377e9d7e9fd9e9db0069e43cd7e96ddbad522",
            "vc_coarse_u.psinv.iter11": "2c3a800c768cd3bda4050fdaf304748beef12ba3841ede03d50be02b765d9aa2",
            "vc_fine_u.interp.iter12": "2e2bb8418e0cee991b5fa7db960d4234c5df9ba35542392deca9a382ad7072b9",
            "vc_resid.resid.iter13": "8831b8b04d1c9f0160a69ca3919eb211fe29a19eed3a5e9f639c291e4ace821d",
            "scalar.rnm2.norm2u3.iter14": "2d81149e7f1152e790cdb27de6e10cc3a58930c7d81efa6a71ea49d9534d237b",
            "scalar.rnmu.norm2u3.iter14": "6cbf669f8ded3fca4f4358cc94e2408405b03ba3db916b3d3ef633c3995dc119",
        })

    def test_mg_changed_coarse_level_dimension_fails_before_boundary(self):
        bundle = self.full_vcycle_bundle()
        first = bundle.invocations[0]
        parameters = dict(first.parameters)
        parameters["m1j"] = 5
        corrupted = dataclasses.replace(
            bundle,
            invocations=(dataclasses.replace(
                first, work_items=12, parameters=parameters,
            ),) + bundle.invocations[1:],
            dynamic_work={"primitive_records": 1},
        )
        with self.assertRaisesRegex(lazy.LazyTraceError, "outside image"):
            tuple(lazy.iter_operations(corrupted, npb.EXPANDERS))


if __name__ == "__main__":
    unittest.main()
