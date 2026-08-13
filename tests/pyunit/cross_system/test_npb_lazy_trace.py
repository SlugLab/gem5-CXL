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
            arrays, (invocation,), {"primitive_records": 25},
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
            (), (invocation,), {"primitive_records": 3},
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
                "boundaries": ["z", "r"], "lanes": lanes(5),
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
            arrays, (invocation,), {"primitive_records": 65},
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
                "boundaries": ["p"],
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
            arrays, (invocation,), {"primitive_records": 21},
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
            arrays, (invocation,), {"primitive_records": 32},
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
                "boundaries": ["x"], "results": ["norm3", "zeta"],
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
            arrays, (invocation,), {"primitive_records": 15},
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
                "boundaries": ["z", "r"], "lanes": lanes(3),
            })
            add(102, "npb_cg_divide", cgit * 10 + 2, 1, {
                "numerator": "rho", "denominator": "rho0",
                "result": "beta",
            })
            add(102, "npb_cg_update_p", cgit, 3, {
                "r": "r", "p": "p", "beta": "beta",
                "boundaries": ["p"],
            })
        add(101, "npb_cg_spmv", 90, 3, {
            "rowstr": "rowstr", "colidx": "colidx", "values": "a",
            "source": "z", "destination": "r", "row_count": 3,
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
            arrays, tuple(invocations), {"primitive_records": 385},
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
            {"q.iter9": digest(F64.pack(5.0) + F64.pack(6.0) + F64.pack(11.0))},
        )

    def test_tiny_cg_dot_uses_explicit_four_lane_tree(self):
        bundle = self.dot_bundle()
        expected, result = self.expected_dot()
        self.assertEqual(
            tuple(lazy.iter_operations(bundle, npb.EXPANDERS)), expected,
        )
        self.assertEqual(
            npb.replay_boundaries(bundle),
            {"scalar.d.iter4": digest(U64.pack(raw_f64(result)))},
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
            (), (invocation,), {"primitive_records": 5},
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
            tuple(lazy.iter_operations(bundle, npb.EXPANDERS)),
            with_sequences(rows),
        )
        self.assertEqual(npb.replay_boundaries(bundle), {
            "scalar.rho0.iter12": digest(U64.pack(raw_f64(17.0))),
            "scalar.d.iter12": digest(U64.pack(raw_f64(0.0))),
            "scalar.rho.iter12": digest(U64.pack(raw_f64(0.0))),
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
            tuple(lazy.iter_operations(bundle, npb.EXPANDERS)), expected,
        )
        self.assertEqual(
            npb.replay_boundaries(bundle),
            {"scalar.alpha.iter5": digest(U64.pack(raw_f64(2.5)))},
        )

    def test_cg_update_zr_and_rho_preserves_expression_and_lane_order(self):
        bundle = self.update_zr_bundle()
        expected, z, r, rho = self.expected_update_zr()
        self.assertEqual(
            tuple(lazy.iter_operations(bundle, npb.EXPANDERS)), expected,
        )
        self.assertEqual(
            npb.replay_boundaries(bundle),
            {
                "z.iter6": digest(b"".join(F64.pack(value) for value in z)),
                "r.iter6": digest(b"".join(F64.pack(value) for value in r)),
                "scalar.rho.iter6": digest(U64.pack(raw_f64(rho))),
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
            tuple(lazy.iter_operations(bundle, npb.EXPANDERS)),
            with_sequences(rows),
        )
        self.assertEqual(
            npb.replay_boundaries(bundle),
            {"p.iter7": digest(b"".join(
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
            tuple(lazy.iter_operations(bundle, npb.EXPANDERS)),
            with_sequences(rows),
        )
        self.assertEqual(
            npb.replay_boundaries(bundle),
            {"scalar.rnorm.iter8": digest(U64.pack(raw_f64(result)))},
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
            tuple(lazy.iter_operations(bundle, npb.EXPANDERS)),
            with_sequences(rows),
        )
        expected = b"".join(F64.pack(value) for value in values)
        zeros = F64.pack(0.0) * 3
        self.assertEqual(npb.replay_boundaries(bundle), {
            "q.iter0": digest(zeros), "z.iter0": digest(zeros),
            "r.iter0": digest(expected), "p.iter0": digest(expected),
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
            tuple(lazy.iter_operations(bundle, npb.EXPANDERS)),
            with_sequences(rows),
        )
        self.assertEqual(npb.replay_boundaries(bundle), {
            "scalar.norm1.iter10": digest(U64.pack(raw_f64(results[0]))),
            "scalar.norm2.iter10": digest(U64.pack(raw_f64(results[1]))),
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
            tuple(lazy.iter_operations(bundle, npb.EXPANDERS)),
            with_sequences(rows),
        )
        self.assertEqual(npb.replay_boundaries(bundle), {
            "x.iter11": digest(b"".join(F64.pack(value) for value in values)),
            "scalar.norm3.iter11": digest(U64.pack(raw_f64(norm3))),
            "scalar.zeta.iter11": digest(U64.pack(raw_f64(zeta))),
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
        self.assertEqual(count, 385)
        boundaries = npb.replay_boundaries(bundle)
        self.assertEqual({key: boundaries[key] for key in (
            "p.iter2", "q.iter2", "z.iter2", "r.iter90",
            "scalar.rnorm.iter90", "scalar.zeta.iter92", "x.iter92",
        )}, {
            "p.iter2": "a3b53e2c123ab65b6b3be7bc6cd2310bda1ef47acfdd81cfe4bcee2a96d40056",
            "q.iter2": "914cf00c1437bb483104959d7029ca9a11ec5ca03e35167aa40c76f49adb7f0f",
            "z.iter2": "c8f7c5eda6963c0c2499525363c38ef15bd512e667e62dd943a3ce92d555e8fd",
            "r.iter90": "d2509a6be14ce92a9a1a3cc0ed43aba5bcd51e8a05d746367e61ff4e846d729a",
            "scalar.rnorm.iter90": "5746cfde1201660e43b77c6200cc5f75f4f48fbafda5b469671f42b6aaf9a183",
            "scalar.zeta.iter92": "1fb50b16377943c1a719035202ac5aeb31fd194380c2ff2df935e02786fd3a2c",
            "x.iter92": "f1c1d4650afbddea40a1f16f628db30047f9b9b023935f0c4bd7bc46db385066",
        })

    def test_cg_out_of_range_column_fails_before_commitment(self):
        bundle = self.spmv_bundle()
        image = self.root / "images/colidx.u32"
        payload = bytearray(image.read_bytes())
        U32.pack_into(payload, 0, 99)
        image.write_bytes(payload)
        with self.assertRaisesRegex(lazy.LazyTraceError, "outside image"):
            tuple(lazy.iter_operations(bundle, npb.EXPANDERS))


if __name__ == "__main__":
    unittest.main()
