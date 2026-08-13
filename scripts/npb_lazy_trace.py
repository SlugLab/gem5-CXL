#!/usr/bin/env python3
# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

"""Bit-exact lazy operation expansion for canonical NPB CG and MG phases."""

import math
import struct

try:
    from scripts import canonical_work_trace as canonical
    from scripts import lazy_work_trace as lazy
except ImportError:
    import canonical_work_trace as canonical
    import lazy_work_trace as lazy


_F64 = struct.Struct("<d")
_U64 = struct.Struct("<Q")


def raw_f64(value):
    return _U64.unpack(_F64.pack(value))[0]


def f64_from_raw(value):
    return _F64.unpack(_U64.pack(value))[0]


def f64(value):
    return f64_from_raw(raw_f64(value))


def _control(invocation, opcode, work_items=0):
    return canonical.Operation(
        invocation.phase, opcode, invocation.iteration,
        0, 0, 0, work_items, 0,
    )


def _load(invocation, opcode, work_item, address, raw):
    return canonical.Operation(
        invocation.phase, opcode, work_item, 0,
        address, raw, 0, raw,
    )


def _binary(invocation, opcode, work_item, left, right, result):
    return canonical.Operation(
        invocation.phase, opcode, work_item, 0, 0,
        raw_f64(left), raw_f64(right), raw_f64(result),
    )


def _store(invocation, work_item, address, value):
    raw = raw_f64(value)
    return canonical.Operation(
        invocation.phase, canonical.Opcode.STORE_F64,
        work_item, 0, address, raw, 0, raw,
    )


def _unary(invocation, opcode, work_item, operand, result):
    return canonical.Operation(
        invocation.phase, opcode, work_item, 0, 0,
        raw_f64(operand), 0, raw_f64(result),
    )


def _require_parameters(invocation, names):
    expected = set(names)
    observed = set(invocation.parameters)
    if observed != expected:
        raise lazy.LazyTraceError(
            f"{invocation.kernel} parameters differ: "
            f"missing={sorted(expected - observed)} "
            f"extra={sorted(observed - expected)}"
        )


def expand_cg_spmv(state, invocation, batch_work_items):
    _require_parameters(invocation, (
        "rowstr", "colidx", "values", "source", "destination",
        "row_count",
    ))
    parameters = invocation.parameters
    row_count = parameters["row_count"]
    if row_count != invocation.work_items:
        raise lazy.LazyTraceError("CG SpMV row count differs from work items")
    yield _control(
        invocation, canonical.Opcode.BARRIER, invocation.work_items
    )
    for first in range(0, row_count, batch_work_items):
        last = min(first + batch_work_items, row_count)
        for row in range(first, last):
            start_address, start = state.load_raw(parameters["rowstr"], row)
            end_address, end = state.load_raw(parameters["rowstr"], row + 1)
            if end < start:
                raise lazy.LazyTraceError("CG row offsets decrease")
            yield _load(
                invocation, canonical.Opcode.LOAD_U32,
                row, start_address, start,
            )
            yield _load(
                invocation, canonical.Opcode.LOAD_U32,
                row, end_address, end,
            )
            total = 0.0
            for edge in range(start, end):
                column_address, column = state.load_raw(
                    parameters["colidx"], edge
                )
                value_address, value = state.load_float(
                    parameters["values"], edge
                )
                source_address, source = state.load_float(
                    parameters["source"], column
                )
                yield _load(
                    invocation, canonical.Opcode.LOAD_U32,
                    row, column_address, column,
                )
                yield _load(
                    invocation, canonical.Opcode.LOAD_F64,
                    row, value_address, raw_f64(value),
                )
                yield _load(
                    invocation, canonical.Opcode.LOAD_F64,
                    row, source_address, raw_f64(source),
                )
                product = f64(value * source)
                yield _binary(
                    invocation, canonical.Opcode.F64_MUL,
                    row, value, source, product,
                )
                updated = f64(total + product)
                yield _binary(
                    invocation, canonical.Opcode.F64_ADD,
                    row, total, product, updated,
                )
                total = updated
            destination_address, _old = state.load_float(
                parameters["destination"], row
            )
            state.store_float(parameters["destination"], row, total)
            yield _store(invocation, row, destination_address, total)
    yield _control(invocation, canonical.Opcode.COMMIT)


def lane_range(count, lane):
    if lane < 0 or lane >= 4:
        raise lazy.LazyTraceError("canonical reduction lane is invalid")
    return count * lane // 4, count * (lane + 1) // 4


def _validate_lanes(invocation):
    expected = [list(lane_range(invocation.work_items, lane))
                for lane in range(4)]
    if invocation.parameters["lanes"] != expected:
        raise lazy.LazyTraceError("canonical four-lane ranges differ")
    return expected


def expand_cg_dot(state, invocation, _batch_work_items):
    _require_parameters(invocation, ("left", "right", "result", "lanes"))
    ranges = _validate_lanes(invocation)
    parameters = invocation.parameters
    yield _control(
        invocation, canonical.Opcode.BARRIER, invocation.work_items
    )
    lanes = []
    for lane in range(4):
        first, last = ranges[lane]
        total = 0.0
        for index in range(first, last):
            left_address, left = state.load_float(parameters["left"], index)
            right_address, right = state.load_float(parameters["right"], index)
            yield _load(
                invocation, canonical.Opcode.LOAD_F64,
                index, left_address, raw_f64(left),
            )
            yield _load(
                invocation, canonical.Opcode.LOAD_F64,
                index, right_address, raw_f64(right),
            )
            product = f64(left * right)
            yield _binary(
                invocation, canonical.Opcode.F64_MUL,
                index, left, right, product,
            )
            updated = f64(total + product)
            yield _binary(
                invocation, canonical.Opcode.F64_ADD,
                index, total, product, updated,
            )
            total = updated
        lanes.append(total)
    merge_left = f64(lanes[0] + lanes[1])
    merge_right = f64(lanes[2] + lanes[3])
    result = f64(merge_left + merge_right)
    yield _binary(
        invocation, canonical.Opcode.F64_ADD,
        invocation.work_items, lanes[0], lanes[1], merge_left,
    )
    yield _binary(
        invocation, canonical.Opcode.F64_ADD,
        invocation.work_items + 1, lanes[2], lanes[3], merge_right,
    )
    yield _binary(
        invocation, canonical.Opcode.F64_ADD,
        invocation.work_items + 2, merge_left, merge_right, result,
    )
    state.store_scalar(parameters["result"], raw_f64(result))
    yield _control(invocation, canonical.Opcode.COMMIT)


def expand_cg_divide(state, invocation, _batch_work_items):
    _require_parameters(
        invocation, ("numerator", "denominator", "result")
    )
    if invocation.work_items != 1:
        raise lazy.LazyTraceError("CG scalar division work items must be one")
    parameters = invocation.parameters
    numerator = f64_from_raw(state.load_scalar(parameters["numerator"]))
    denominator = f64_from_raw(state.load_scalar(parameters["denominator"]))
    if denominator == 0.0:
        raise lazy.LazyTraceError("CG scalar division denominator is zero")
    result = f64(numerator / denominator)
    yield _control(invocation, canonical.Opcode.BARRIER, 1)
    yield _binary(
        invocation, canonical.Opcode.F64_DIV,
        0, numerator, denominator, result,
    )
    state.store_scalar(parameters["result"], raw_f64(result))
    yield _control(invocation, canonical.Opcode.COMMIT)


def _merge_sum4(invocation, lanes, work_item):
    left = f64(lanes[0] + lanes[1])
    right = f64(lanes[2] + lanes[3])
    result = f64(left + right)
    yield _binary(
        invocation, canonical.Opcode.F64_ADD,
        work_item, lanes[0], lanes[1], left,
    )
    yield _binary(
        invocation, canonical.Opcode.F64_ADD,
        work_item + 1, lanes[2], lanes[3], right,
    )
    yield _binary(
        invocation, canonical.Opcode.F64_ADD,
        work_item + 2, left, right, result,
    )
    return result


def expand_cg_update_zr(state, invocation, _batch_work_items):
    _require_parameters(invocation, (
        "z", "p", "r", "q", "alpha", "result", "boundaries", "lanes",
    ))
    ranges = _validate_lanes(invocation)
    parameters = invocation.parameters
    alpha = f64_from_raw(state.load_scalar(parameters["alpha"]))
    yield _control(
        invocation, canonical.Opcode.BARRIER, invocation.work_items
    )
    lanes = []
    for lane in range(4):
        first, last = ranges[lane]
        total = 0.0
        for index in range(first, last):
            z_address, z = state.load_float(parameters["z"], index)
            p_address, p = state.load_float(parameters["p"], index)
            yield _load(
                invocation, canonical.Opcode.LOAD_F64,
                index, z_address, raw_f64(z),
            )
            yield _load(
                invocation, canonical.Opcode.LOAD_F64,
                index, p_address, raw_f64(p),
            )
            alpha_p = f64(alpha * p)
            yield _binary(
                invocation, canonical.Opcode.F64_MUL,
                index, alpha, p, alpha_p,
            )
            new_z = f64(z + alpha_p)
            yield _binary(
                invocation, canonical.Opcode.F64_ADD,
                index, z, alpha_p, new_z,
            )
            state.store_float(parameters["z"], index, new_z)
            yield _store(invocation, index, z_address, new_z)
            r_address, r = state.load_float(parameters["r"], index)
            q_address, q = state.load_float(parameters["q"], index)
            yield _load(
                invocation, canonical.Opcode.LOAD_F64,
                index, r_address, raw_f64(r),
            )
            yield _load(
                invocation, canonical.Opcode.LOAD_F64,
                index, q_address, raw_f64(q),
            )
            alpha_q = f64(alpha * q)
            yield _binary(
                invocation, canonical.Opcode.F64_MUL,
                index, alpha, q, alpha_q,
            )
            new_r = f64(r - alpha_q)
            yield _binary(
                invocation, canonical.Opcode.F64_SUB,
                index, r, alpha_q, new_r,
            )
            state.store_float(parameters["r"], index, new_r)
            yield _store(invocation, index, r_address, new_r)
            square = f64(new_r * new_r)
            yield _binary(
                invocation, canonical.Opcode.F64_MUL,
                index, new_r, new_r, square,
            )
            updated = f64(total + square)
            yield _binary(
                invocation, canonical.Opcode.F64_ADD,
                index, total, square, updated,
            )
            total = updated
        lanes.append(total)
    merge = _merge_sum4(invocation, lanes, invocation.work_items)
    while True:
        try:
            yield next(merge)
        except StopIteration as stop:
            rho = stop.value
            break
    state.store_scalar(parameters["result"], raw_f64(rho))
    yield _control(invocation, canonical.Opcode.COMMIT)


def expand_cg_update_p(state, invocation, batch_work_items):
    _require_parameters(invocation, ("r", "p", "beta", "boundaries"))
    parameters = invocation.parameters
    beta = f64_from_raw(state.load_scalar(parameters["beta"]))
    yield _control(
        invocation, canonical.Opcode.BARRIER, invocation.work_items
    )
    for first in range(0, invocation.work_items, batch_work_items):
        last = min(first + batch_work_items, invocation.work_items)
        for index in range(first, last):
            r_address, r = state.load_float(parameters["r"], index)
            p_address, p = state.load_float(parameters["p"], index)
            yield _load(
                invocation, canonical.Opcode.LOAD_F64,
                index, r_address, raw_f64(r),
            )
            yield _load(
                invocation, canonical.Opcode.LOAD_F64,
                index, p_address, raw_f64(p),
            )
            product = f64(beta * p)
            yield _binary(
                invocation, canonical.Opcode.F64_MUL,
                index, beta, p, product,
            )
            result = f64(r + product)
            yield _binary(
                invocation, canonical.Opcode.F64_ADD,
                index, r, product, result,
            )
            state.store_float(parameters["p"], index, result)
            yield _store(invocation, index, p_address, result)
    yield _control(invocation, canonical.Opcode.COMMIT)


def expand_cg_residual_norm(state, invocation, _batch_work_items):
    _require_parameters(invocation, ("x", "r", "result", "lanes"))
    ranges = _validate_lanes(invocation)
    parameters = invocation.parameters
    yield _control(
        invocation, canonical.Opcode.BARRIER, invocation.work_items
    )
    lanes = []
    for lane in range(4):
        first, last = ranges[lane]
        total = 0.0
        for index in range(first, last):
            x_address, x = state.load_float(parameters["x"], index)
            r_address, r = state.load_float(parameters["r"], index)
            yield _load(
                invocation, canonical.Opcode.LOAD_F64,
                index, x_address, raw_f64(x),
            )
            yield _load(
                invocation, canonical.Opcode.LOAD_F64,
                index, r_address, raw_f64(r),
            )
            difference = f64(x - r)
            yield _binary(
                invocation, canonical.Opcode.F64_SUB,
                index, x, r, difference,
            )
            square = f64(difference * difference)
            yield _binary(
                invocation, canonical.Opcode.F64_MUL,
                index, difference, difference, square,
            )
            updated = f64(total + square)
            yield _binary(
                invocation, canonical.Opcode.F64_ADD,
                index, total, square, updated,
            )
            total = updated
        lanes.append(total)
    merge = _merge_sum4(invocation, lanes, invocation.work_items)
    while True:
        try:
            yield next(merge)
        except StopIteration as stop:
            total = stop.value
            break
    result = f64(math.sqrt(total))
    yield _unary(
        invocation, canonical.Opcode.F64_SQRT,
        invocation.work_items + 3, total, result,
    )
    state.store_scalar(parameters["result"], raw_f64(result))
    yield _control(invocation, canonical.Opcode.COMMIT)


def expand_cg_init(state, invocation, batch_work_items):
    _require_parameters(
        invocation, ("x", "q", "z", "r", "p", "boundaries")
    )
    parameters = invocation.parameters
    yield _control(
        invocation, canonical.Opcode.BARRIER, invocation.work_items
    )
    for first in range(0, invocation.work_items, batch_work_items):
        last = min(first + batch_work_items, invocation.work_items)
        for index in range(first, last):
            q_address, _q = state.load_float(parameters["q"], index)
            z_address, _z = state.load_float(parameters["z"], index)
            state.store_float(parameters["q"], index, 0.0)
            yield _store(invocation, index, q_address, 0.0)
            state.store_float(parameters["z"], index, 0.0)
            yield _store(invocation, index, z_address, 0.0)
            x_address, x = state.load_float(parameters["x"], index)
            yield _load(
                invocation, canonical.Opcode.LOAD_F64,
                index, x_address, raw_f64(x),
            )
            r_address, _r = state.load_float(parameters["r"], index)
            state.store_float(parameters["r"], index, x)
            yield _store(invocation, index, r_address, x)
            r_address, r = state.load_float(parameters["r"], index)
            yield _load(
                invocation, canonical.Opcode.LOAD_F64,
                index, r_address, raw_f64(r),
            )
            p_address, _p = state.load_float(parameters["p"], index)
            state.store_float(parameters["p"], index, r)
            yield _store(invocation, index, p_address, r)
    yield _control(invocation, canonical.Opcode.COMMIT)


def _finish_merge(generator):
    while True:
        try:
            yield next(generator)
        except StopIteration as stop:
            return stop.value


def expand_cg_outer_dots(state, invocation, _batch_work_items):
    _require_parameters(invocation, (
        "x", "z", "result_xz", "result_zz", "results", "lanes",
    ))
    ranges = _validate_lanes(invocation)
    parameters = invocation.parameters
    yield _control(
        invocation, canonical.Opcode.BARRIER, invocation.work_items
    )
    lanes_xz = []
    lanes_zz = []
    for lane in range(4):
        first, last = ranges[lane]
        total_xz = 0.0
        total_zz = 0.0
        for index in range(first, last):
            x_address, x = state.load_float(parameters["x"], index)
            z_address, z = state.load_float(parameters["z"], index)
            yield _load(
                invocation, canonical.Opcode.LOAD_F64,
                index, x_address, raw_f64(x),
            )
            yield _load(
                invocation, canonical.Opcode.LOAD_F64,
                index, z_address, raw_f64(z),
            )
            product_xz = f64(x * z)
            yield _binary(
                invocation, canonical.Opcode.F64_MUL,
                index, x, z, product_xz,
            )
            next_xz = f64(total_xz + product_xz)
            yield _binary(
                invocation, canonical.Opcode.F64_ADD,
                index, total_xz, product_xz, next_xz,
            )
            z_address, z_left = state.load_float(parameters["z"], index)
            yield _load(
                invocation, canonical.Opcode.LOAD_F64,
                index, z_address, raw_f64(z_left),
            )
            z_address, z_right = state.load_float(parameters["z"], index)
            yield _load(
                invocation, canonical.Opcode.LOAD_F64,
                index, z_address, raw_f64(z_right),
            )
            product_zz = f64(z_left * z_right)
            yield _binary(
                invocation, canonical.Opcode.F64_MUL,
                index, z_left, z_right, product_zz,
            )
            next_zz = f64(total_zz + product_zz)
            yield _binary(
                invocation, canonical.Opcode.F64_ADD,
                index, total_zz, product_zz, next_zz,
            )
            total_xz = next_xz
            total_zz = next_zz
        lanes_xz.append(total_xz)
        lanes_zz.append(total_zz)
    merge_xz = _finish_merge(_merge_sum4(
        invocation, lanes_xz, invocation.work_items
    ))
    while True:
        try:
            yield next(merge_xz)
        except StopIteration as stop:
            result_xz = stop.value
            break
    merge_zz = _finish_merge(_merge_sum4(
        invocation, lanes_zz, invocation.work_items + 3
    ))
    while True:
        try:
            yield next(merge_zz)
        except StopIteration as stop:
            result_zz = stop.value
            break
    state.store_scalar(parameters["result_xz"], raw_f64(result_xz))
    state.store_scalar(parameters["result_zz"], raw_f64(result_zz))
    yield _control(invocation, canonical.Opcode.COMMIT)


def expand_cg_normalize(state, invocation, batch_work_items):
    _require_parameters(invocation, (
        "z", "x", "norm1", "norm2", "norm3", "shift", "zeta",
        "write_zeta", "boundaries", "results",
    ))
    parameters = invocation.parameters
    if parameters["write_zeta"] not in (0, 1):
        raise lazy.LazyTraceError("CG write_zeta flag is invalid")
    norm1 = f64_from_raw(state.load_scalar(parameters["norm1"]))
    norm2 = f64_from_raw(state.load_scalar(parameters["norm2"]))
    if norm2 < 0.0:
        raise lazy.LazyTraceError("CG normalization norm is negative")
    yield _control(
        invocation, canonical.Opcode.BARRIER, invocation.work_items
    )
    square_root = f64(math.sqrt(norm2))
    yield _unary(
        invocation, canonical.Opcode.F64_SQRT, 0, norm2, square_root,
    )
    if square_root == 0.0:
        raise lazy.LazyTraceError("CG normalization norm is zero")
    norm3 = f64(1.0 / square_root)
    yield _binary(
        invocation, canonical.Opcode.F64_DIV,
        1, 1.0, square_root, norm3,
    )
    state.store_scalar(parameters["norm3"], raw_f64(norm3))
    if parameters["write_zeta"]:
        if norm1 == 0.0:
            raise lazy.LazyTraceError("CG zeta dot product is zero")
        reciprocal = f64(1.0 / norm1)
        yield _binary(
            invocation, canonical.Opcode.F64_DIV,
            2, 1.0, norm1, reciprocal,
        )
        shift = f64_from_raw(state.load_scalar(parameters["shift"]))
        zeta = f64(shift + reciprocal)
        yield _binary(
            invocation, canonical.Opcode.F64_ADD,
            3, shift, reciprocal, zeta,
        )
        state.store_scalar(parameters["zeta"], raw_f64(zeta))
    for first in range(0, invocation.work_items, batch_work_items):
        last = min(first + batch_work_items, invocation.work_items)
        for index in range(first, last):
            z_address, z = state.load_float(parameters["z"], index)
            yield _load(
                invocation, canonical.Opcode.LOAD_F64,
                index, z_address, raw_f64(z),
            )
            value = f64(norm3 * z)
            yield _binary(
                invocation, canonical.Opcode.F64_MUL,
                index, norm3, z, value,
            )
            x_address, _x = state.load_float(parameters["x"], index)
            state.store_float(parameters["x"], index, value)
            yield _store(invocation, index, x_address, value)
    yield _control(invocation, canonical.Opcode.COMMIT)


def expand_cg_prepare_iteration(state, invocation, _batch_work_items):
    _require_parameters(
        invocation, ("source", "snapshot", "zero", "results")
    )
    if invocation.work_items != 1 or len(invocation.parameters["zero"]) != 2:
        raise lazy.LazyTraceError("CG iteration preparation shape differs")
    parameters = invocation.parameters
    source_raw = state.load_scalar(parameters["source"])
    source = f64_from_raw(source_raw)
    yield _control(invocation, canonical.Opcode.BARRIER, 1)
    yield _unary(
        invocation, canonical.Opcode.F64_MOV, 0, source, source,
    )
    state.store_scalar(parameters["snapshot"], source_raw)
    for work_item, name in enumerate(parameters["zero"], start=1):
        yield _unary(
            invocation, canonical.Opcode.F64_MOV,
            work_item, 0.0, 0.0,
        )
        state.store_scalar(name, raw_f64(0.0))
    yield _control(invocation, canonical.Opcode.COMMIT)


def f_index(i1, i2, i3, n1, n2, n3):
    if not (
        0 <= i1 < n1 and 0 <= i2 < n2 and 0 <= i3 < n3
    ):
        raise lazy.LazyTraceError("MG grid index is outside image")
    return i1 + n1 * (i2 + n2 * i3)


def _grid_load(state, invocation, name, i1, i2, i3, dimensions, work_item):
    index = f_index(i1, i2, i3, *dimensions)
    address, value = state.load_float(name, index)
    return value, _load(
        invocation, canonical.Opcode.LOAD_F64,
        work_item, address, raw_f64(value),
    )


def _grid_store(state, invocation, name, i1, i2, i3, dimensions,
                work_item, value):
    index = f_index(i1, i2, i3, *dimensions)
    address, _old = state.load_float(name, index)
    state.store_float(name, index, value)
    return _store(invocation, work_item, address, value)


def _fold_add(invocation, work_item, values):
    if not values:
        raise lazy.LazyTraceError("MG addition has no operands")
    result = values[0]
    for value in values[1:]:
        updated = f64(result + value)
        yield _binary(
            invocation, canonical.Opcode.F64_ADD,
            work_item, result, value, updated,
        )
        result = updated
    return result


def _run_fold(generator):
    while True:
        try:
            yield next(generator)
        except StopIteration as stop:
            return stop.value


def _comm3(state, invocation, name, dimensions):
    n1, n2, n3 = dimensions
    work_item = invocation.work_items

    def copy(destination, source):
        nonlocal work_item
        value, load_operation = _grid_load(
            state, invocation, name, *source, dimensions, work_item
        )
        yield load_operation
        yield _grid_store(
            state, invocation, name, *destination,
            dimensions, work_item, value,
        )
        work_item += 1

    for i3 in range(1, n3 - 1):
        for i2 in range(1, n2 - 1):
            yield from copy((0, i2, i3), (n1 - 2, i2, i3))
            yield from copy((n1 - 1, i2, i3), (1, i2, i3))
        for i1 in range(n1):
            yield from copy((i1, 0, i3), (i1, n2 - 2, i3))
            yield from copy((i1, n2 - 1, i3), (i1, 1, i3))
    for i2 in range(n2):
        for i1 in range(n1):
            yield from copy((i1, i2, 0), (i1, i2, n3 - 2))
            yield from copy((i1, i2, n3 - 1), (i1, i2, 1))


def expand_mg_resid(state, invocation, _batch_work_items):
    _require_parameters(invocation, (
        "u", "v", "r", "n1", "n2", "n3", "a_raw", "boundaries",
    ))
    parameters = invocation.parameters
    dimensions = (
        parameters["n1"], parameters["n2"], parameters["n3"]
    )
    n1, n2, n3 = dimensions
    expected_work = (n1 - 2) * (n2 - 2) * (n3 - 2)
    if min(dimensions) < 3 or invocation.work_items != expected_work:
        raise lazy.LazyTraceError("MG resid dimensions or work items differ")
    if len(parameters["a_raw"]) != 4:
        raise lazy.LazyTraceError("MG resid coefficient count differs")
    coefficients = [f64_from_raw(value) for value in parameters["a_raw"]]
    yield _control(
        invocation, canonical.Opcode.BARRIER, invocation.work_items
    )
    for i3 in range(1, n3 - 1):
        for i2 in range(1, n2 - 1):
            row_work = (i3 - 1) * (n2 - 2) + (i2 - 1)
            u1 = []
            u2 = []
            for i1 in range(n1):
                values1 = []
                for coordinate in (
                    (i1, i2 - 1, i3), (i1, i2 + 1, i3),
                    (i1, i2, i3 - 1), (i1, i2, i3 + 1),
                ):
                    value, operation = _grid_load(
                        state, invocation, parameters["u"], *coordinate,
                        dimensions, row_work,
                    )
                    yield operation
                    values1.append(value)
                fold = _run_fold(_fold_add(invocation, row_work, values1))
                while True:
                    try:
                        yield next(fold)
                    except StopIteration as stop:
                        u1.append(stop.value)
                        break
                values2 = []
                for coordinate in (
                    (i1, i2 - 1, i3 - 1), (i1, i2 + 1, i3 - 1),
                    (i1, i2 - 1, i3 + 1), (i1, i2 + 1, i3 + 1),
                ):
                    value, operation = _grid_load(
                        state, invocation, parameters["u"], *coordinate,
                        dimensions, row_work,
                    )
                    yield operation
                    values2.append(value)
                fold = _run_fold(_fold_add(invocation, row_work, values2))
                while True:
                    try:
                        yield next(fold)
                    except StopIteration as stop:
                        u2.append(stop.value)
                        break
            for i1 in range(1, n1 - 1):
                work_item = f_index(i1, i2, i3, *dimensions)
                v, operation = _grid_load(
                    state, invocation, parameters["v"], i1, i2, i3,
                    dimensions, work_item,
                )
                yield operation
                u, operation = _grid_load(
                    state, invocation, parameters["u"], i1, i2, i3,
                    dimensions, work_item,
                )
                yield operation
                product = f64(coefficients[0] * u)
                yield _binary(
                    invocation, canonical.Opcode.F64_MUL,
                    work_item, coefficients[0], u, product,
                )
                result = f64(v - product)
                yield _binary(
                    invocation, canonical.Opcode.F64_SUB,
                    work_item, v, product, result,
                )
                fold = _run_fold(_fold_add(
                    invocation, work_item,
                    [u2[i1], u1[i1 - 1], u1[i1 + 1]],
                ))
                while True:
                    try:
                        yield next(fold)
                    except StopIteration as stop:
                        neighbors = stop.value
                        break
                product = f64(coefficients[2] * neighbors)
                yield _binary(
                    invocation, canonical.Opcode.F64_MUL,
                    work_item, coefficients[2], neighbors, product,
                )
                updated = f64(result - product)
                yield _binary(
                    invocation, canonical.Opcode.F64_SUB,
                    work_item, result, product, updated,
                )
                fold = _run_fold(_fold_add(
                    invocation, work_item, [u2[i1 - 1], u2[i1 + 1]],
                ))
                while True:
                    try:
                        yield next(fold)
                    except StopIteration as stop:
                        corners = stop.value
                        break
                product = f64(coefficients[3] * corners)
                yield _binary(
                    invocation, canonical.Opcode.F64_MUL,
                    work_item, coefficients[3], corners, product,
                )
                result = f64(updated - product)
                yield _binary(
                    invocation, canonical.Opcode.F64_SUB,
                    work_item, updated, product, result,
                )
                yield _grid_store(
                    state, invocation, parameters["r"], i1, i2, i3,
                    dimensions, work_item, result,
                )
    yield from _comm3(state, invocation, parameters["r"], dimensions)
    yield _control(invocation, canonical.Opcode.COMMIT)


def _merge_max4(invocation, lanes, work_item):
    left = max(lanes[0], lanes[1])
    right = max(lanes[2], lanes[3])
    result = max(left, right)
    yield _binary(
        invocation, canonical.Opcode.F64_MAX,
        work_item, lanes[0], lanes[1], left,
    )
    yield _binary(
        invocation, canonical.Opcode.F64_MAX,
        work_item + 1, lanes[2], lanes[3], right,
    )
    yield _binary(
        invocation, canonical.Opcode.F64_MAX,
        work_item + 2, left, right, result,
    )
    return result


def expand_mg_norm2u3(state, invocation, _batch_work_items):
    _require_parameters(invocation, (
        "r", "n1", "n2", "n3", "dn_raw", "rnm2", "rnmu",
        "results", "lanes",
    ))
    parameters = invocation.parameters
    dimensions = (
        parameters["n1"], parameters["n2"], parameters["n3"]
    )
    n1, n2, n3 = dimensions
    expected_work = (n1 - 2) * (n2 - 2) * (n3 - 2)
    if min(dimensions) < 3 or invocation.work_items != expected_work:
        raise lazy.LazyTraceError("MG norm dimensions or work items differ")
    ranges = _validate_lanes(invocation)
    coordinates = [
        (i1, i2, i3)
        for i3 in range(1, n3 - 1)
        for i2 in range(1, n2 - 1)
        for i1 in range(1, n1 - 1)
    ]
    yield _control(
        invocation, canonical.Opcode.BARRIER, invocation.work_items
    )
    sum_lanes = []
    max_lanes = []
    for lane in range(4):
        first, last = ranges[lane]
        total = 0.0
        maximum = 0.0
        for work_item in range(first, last):
            coordinate = coordinates[work_item]
            value, operation = _grid_load(
                state, invocation, parameters["r"], *coordinate,
                dimensions, work_item,
            )
            yield operation
            square = f64(value * value)
            yield _binary(
                invocation, canonical.Opcode.F64_MUL,
                work_item, value, value, square,
            )
            updated = f64(total + square)
            yield _binary(
                invocation, canonical.Opcode.F64_ADD,
                work_item, total, square, updated,
            )
            total = updated
            value_again, operation = _grid_load(
                state, invocation, parameters["r"], *coordinate,
                dimensions, work_item,
            )
            yield operation
            absolute = f64(abs(value_again))
            yield _unary(
                invocation, canonical.Opcode.F64_ABS,
                work_item, value_again, absolute,
            )
            updated_max = max(maximum, absolute)
            yield _binary(
                invocation, canonical.Opcode.F64_MAX,
                work_item, maximum, absolute, updated_max,
            )
            maximum = updated_max
        sum_lanes.append(total)
        max_lanes.append(maximum)
    merge_sum = _finish_merge(_merge_sum4(
        invocation, sum_lanes, invocation.work_items
    ))
    while True:
        try:
            yield next(merge_sum)
        except StopIteration as stop:
            total = stop.value
            break
    merge_max = _finish_merge(_merge_max4(
        invocation, max_lanes, invocation.work_items + 3
    ))
    while True:
        try:
            yield next(merge_max)
        except StopIteration as stop:
            maximum = stop.value
            break
    dn = f64_from_raw(parameters["dn_raw"])
    if dn <= 0.0:
        raise lazy.LazyTraceError("MG norm denominator is not positive")
    quotient = f64(total / dn)
    yield _binary(
        invocation, canonical.Opcode.F64_DIV,
        invocation.work_items + 6, total, dn, quotient,
    )
    rnm2 = f64(math.sqrt(quotient))
    yield _unary(
        invocation, canonical.Opcode.F64_SQRT,
        invocation.work_items + 7, quotient, rnm2,
    )
    state.store_scalar(parameters["rnm2"], raw_f64(rnm2))
    state.store_scalar(parameters["rnmu"], raw_f64(maximum))
    yield _control(invocation, canonical.Opcode.COMMIT)


def expand_mg_psinv(state, invocation, _batch_work_items):
    _require_parameters(invocation, (
        "r", "u", "n1", "n2", "n3", "c_raw", "boundaries",
    ))
    parameters = invocation.parameters
    dimensions = (
        parameters["n1"], parameters["n2"], parameters["n3"]
    )
    n1, n2, n3 = dimensions
    expected_work = (n1 - 2) * (n2 - 2) * (n3 - 2)
    if min(dimensions) < 3 or invocation.work_items != expected_work:
        raise lazy.LazyTraceError("MG psinv dimensions or work items differ")
    if len(parameters["c_raw"]) != 4:
        raise lazy.LazyTraceError("MG psinv coefficient count differs")
    coefficients = [f64_from_raw(value) for value in parameters["c_raw"]]
    yield _control(
        invocation, canonical.Opcode.BARRIER, invocation.work_items
    )
    for i3 in range(1, n3 - 1):
        for i2 in range(1, n2 - 1):
            row_work = (i3 - 1) * (n2 - 2) + (i2 - 1)
            r1 = []
            r2 = []
            for i1 in range(n1):
                values1 = []
                for coordinate in (
                    (i1, i2 - 1, i3), (i1, i2 + 1, i3),
                    (i1, i2, i3 - 1), (i1, i2, i3 + 1),
                ):
                    value, operation = _grid_load(
                        state, invocation, parameters["r"], *coordinate,
                        dimensions, row_work,
                    )
                    yield operation
                    values1.append(value)
                fold = _run_fold(_fold_add(invocation, row_work, values1))
                while True:
                    try:
                        yield next(fold)
                    except StopIteration as stop:
                        r1.append(stop.value)
                        break
                values2 = []
                for coordinate in (
                    (i1, i2 - 1, i3 - 1), (i1, i2 + 1, i3 - 1),
                    (i1, i2 - 1, i3 + 1), (i1, i2 + 1, i3 + 1),
                ):
                    value, operation = _grid_load(
                        state, invocation, parameters["r"], *coordinate,
                        dimensions, row_work,
                    )
                    yield operation
                    values2.append(value)
                fold = _run_fold(_fold_add(invocation, row_work, values2))
                while True:
                    try:
                        yield next(fold)
                    except StopIteration as stop:
                        r2.append(stop.value)
                        break
            for i1 in range(1, n1 - 1):
                work_item = f_index(i1, i2, i3, *dimensions)
                u, operation = _grid_load(
                    state, invocation, parameters["u"], i1, i2, i3,
                    dimensions, work_item,
                )
                yield operation
                r, operation = _grid_load(
                    state, invocation, parameters["r"], i1, i2, i3,
                    dimensions, work_item,
                )
                yield operation
                product = f64(coefficients[0] * r)
                yield _binary(
                    invocation, canonical.Opcode.F64_MUL,
                    work_item, coefficients[0], r, product,
                )
                result = f64(u + product)
                yield _binary(
                    invocation, canonical.Opcode.F64_ADD,
                    work_item, u, product, result,
                )
                r_left, operation = _grid_load(
                    state, invocation, parameters["r"], i1 - 1, i2, i3,
                    dimensions, work_item,
                )
                yield operation
                r_right, operation = _grid_load(
                    state, invocation, parameters["r"], i1 + 1, i2, i3,
                    dimensions, work_item,
                )
                yield operation
                fold = _run_fold(_fold_add(
                    invocation, work_item, [r_left, r_right, r1[i1]],
                ))
                while True:
                    try:
                        yield next(fold)
                    except StopIteration as stop:
                        neighbors = stop.value
                        break
                product = f64(coefficients[1] * neighbors)
                yield _binary(
                    invocation, canonical.Opcode.F64_MUL,
                    work_item, coefficients[1], neighbors, product,
                )
                updated = f64(result + product)
                yield _binary(
                    invocation, canonical.Opcode.F64_ADD,
                    work_item, result, product, updated,
                )
                fold = _run_fold(_fold_add(
                    invocation, work_item,
                    [r2[i1], r1[i1 - 1], r1[i1 + 1]],
                ))
                while True:
                    try:
                        yield next(fold)
                    except StopIteration as stop:
                        diagonals = stop.value
                        break
                product = f64(coefficients[2] * diagonals)
                yield _binary(
                    invocation, canonical.Opcode.F64_MUL,
                    work_item, coefficients[2], diagonals, product,
                )
                result = f64(updated + product)
                yield _binary(
                    invocation, canonical.Opcode.F64_ADD,
                    work_item, updated, product, result,
                )
                yield _grid_store(
                    state, invocation, parameters["u"], i1, i2, i3,
                    dimensions, work_item, result,
                )
    yield from _comm3(state, invocation, parameters["u"], dimensions)
    yield _control(invocation, canonical.Opcode.COMMIT)


def expand_mg_rprj3(state, invocation, _batch_work_items):
    _require_parameters(invocation, (
        "r", "s", "m1k", "m2k", "m3k", "m1j", "m2j", "m3j",
        "boundaries",
    ))
    parameters = invocation.parameters
    fine_dims = (parameters["m1k"], parameters["m2k"], parameters["m3k"])
    coarse_dims = (parameters["m1j"], parameters["m2j"], parameters["m3j"])
    m1k, m2k, m3k = fine_dims
    m1j, m2j, m3j = coarse_dims
    if min(fine_dims + coarse_dims) < 3:
        raise lazy.LazyTraceError("MG rprj3 dimensions are too small")
    expected_work = (m1j - 2) * (m2j - 2) * (m3j - 2)
    if invocation.work_items != expected_work:
        raise lazy.LazyTraceError("MG rprj3 work items differ")
    d1 = 2 if m1k == 3 else 1
    d2 = 2 if m2k == 3 else 1
    d3 = 2 if m3k == 3 else 1

    def load_r(i1, i2, i3, work_item):
        return _grid_load(
            state, invocation, parameters["r"], i1 - 1, i2 - 1, i3 - 1,
            fine_dims, work_item,
        )

    yield _control(
        invocation, canonical.Opcode.BARRIER, invocation.work_items
    )
    for j3 in range(2, m3j):
        for j2 in range(2, m2j):
            i3 = 2 * j3 - d3
            i2 = 2 * j2 - d2
            row_work = (j3 - 2) * (m2j - 2) + (j2 - 2)
            x1 = {}
            y1 = {}
            for j1 in range(2, m1j + 1):
                i1 = 2 * j1 - d1
                values = []
                for coordinate in (
                    (i1 - 1, i2 - 1, i3), (i1 - 1, i2 + 1, i3),
                    (i1 - 1, i2, i3 - 1), (i1 - 1, i2, i3 + 1),
                ):
                    value, operation = load_r(*coordinate, row_work)
                    yield operation
                    values.append(value)
                fold = _run_fold(_fold_add(invocation, row_work, values))
                while True:
                    try:
                        yield next(fold)
                    except StopIteration as stop:
                        x1[i1 - 1] = stop.value
                        break
                values = []
                for coordinate in (
                    (i1 - 1, i2 - 1, i3 - 1),
                    (i1 - 1, i2 - 1, i3 + 1),
                    (i1 - 1, i2 + 1, i3 - 1),
                    (i1 - 1, i2 + 1, i3 + 1),
                ):
                    value, operation = load_r(*coordinate, row_work)
                    yield operation
                    values.append(value)
                fold = _run_fold(_fold_add(invocation, row_work, values))
                while True:
                    try:
                        yield next(fold)
                    except StopIteration as stop:
                        y1[i1 - 1] = stop.value
                        break
            for j1 in range(2, m1j):
                i1 = 2 * j1 - d1
                work_item = f_index(
                    j1 - 1, j2 - 1, j3 - 1, *coarse_dims
                )
                values = []
                for coordinate in (
                    (i1, i2 - 1, i3 - 1), (i1, i2 - 1, i3 + 1),
                    (i1, i2 + 1, i3 - 1), (i1, i2 + 1, i3 + 1),
                ):
                    value, operation = load_r(*coordinate, work_item)
                    yield operation
                    values.append(value)
                fold = _run_fold(_fold_add(invocation, work_item, values))
                while True:
                    try:
                        yield next(fold)
                    except StopIteration as stop:
                        y2 = stop.value
                        break
                values = []
                for coordinate in (
                    (i1, i2 - 1, i3), (i1, i2 + 1, i3),
                    (i1, i2, i3 - 1), (i1, i2, i3 + 1),
                ):
                    value, operation = load_r(*coordinate, work_item)
                    yield operation
                    values.append(value)
                fold = _run_fold(_fold_add(invocation, work_item, values))
                while True:
                    try:
                        yield next(fold)
                    except StopIteration as stop:
                        x2 = stop.value
                        break
                center, operation = load_r(i1, i2, i3, work_item)
                yield operation
                result = f64(0.5 * center)
                yield _binary(
                    invocation, canonical.Opcode.F64_MUL,
                    work_item, 0.5, center, result,
                )
                left, operation = load_r(i1 - 1, i2, i3, work_item)
                yield operation
                right, operation = load_r(i1 + 1, i2, i3, work_item)
                yield operation
                fold = _run_fold(_fold_add(
                    invocation, work_item, [left, right, x2]
                ))
                while True:
                    try:
                        yield next(fold)
                    except StopIteration as stop:
                        neighbors = stop.value
                        break
                product = f64(0.25 * neighbors)
                yield _binary(
                    invocation, canonical.Opcode.F64_MUL,
                    work_item, 0.25, neighbors, product,
                )
                updated = f64(result + product)
                yield _binary(
                    invocation, canonical.Opcode.F64_ADD,
                    work_item, result, product, updated,
                )
                fold = _run_fold(_fold_add(
                    invocation, work_item,
                    [x1[i1 - 1], x1[i1 + 1], y2],
                ))
                while True:
                    try:
                        yield next(fold)
                    except StopIteration as stop:
                        diagonals = stop.value
                        break
                product = f64(0.125 * diagonals)
                yield _binary(
                    invocation, canonical.Opcode.F64_MUL,
                    work_item, 0.125, diagonals, product,
                )
                result = f64(updated + product)
                yield _binary(
                    invocation, canonical.Opcode.F64_ADD,
                    work_item, updated, product, result,
                )
                fold = _run_fold(_fold_add(
                    invocation, work_item, [y1[i1 - 1], y1[i1 + 1]]
                ))
                while True:
                    try:
                        yield next(fold)
                    except StopIteration as stop:
                        corners = stop.value
                        break
                product = f64(0.0625 * corners)
                yield _binary(
                    invocation, canonical.Opcode.F64_MUL,
                    work_item, 0.0625, corners, product,
                )
                before_corners = result
                result = f64(before_corners + product)
                yield _binary(
                    invocation, canonical.Opcode.F64_ADD,
                    work_item, before_corners, product, result,
                )
                yield _grid_store(
                    state, invocation, parameters["s"],
                    j1 - 1, j2 - 1, j3 - 1, coarse_dims,
                    work_item, result,
                )
    yield from _comm3(state, invocation, parameters["s"], coarse_dims)
    yield _control(invocation, canonical.Opcode.COMMIT)


def _mg_weighted_update(state, invocation, name, coordinate, dimensions,
                        work_item, values, coefficient):
    fold = _run_fold(_fold_add(invocation, work_item, values))
    while True:
        try:
            yield next(fold)
        except StopIteration as stop:
            source = stop.value
            break
    if coefficient != 1.0:
        weighted = f64(coefficient * source)
        yield _binary(
            invocation, canonical.Opcode.F64_MUL,
            work_item, coefficient, source, weighted,
        )
    else:
        weighted = source
    old, operation = _grid_load(
        state, invocation, name, *coordinate, dimensions, work_item
    )
    yield operation
    result = f64(old + weighted)
    yield _binary(
        invocation, canonical.Opcode.F64_ADD,
        work_item, old, weighted, result,
    )
    yield _grid_store(
        state, invocation, name, *coordinate,
        dimensions, work_item, result,
    )


def _expand_mg_interp_degenerate(state, invocation, parameters,
                                 coarse_dims, fine_dims):
    mm1, mm2, mm3 = coarse_dims
    n1, n2, n3 = fine_dims
    d1, t1 = (2, 1) if n1 == 3 else (1, 0)
    d2, t2 = (2, 1) if n2 == 3 else (1, 0)
    d3, t3 = (2, 1) if n3 == 3 else (1, 0)

    def emit(destination, sources, coefficient):
        coordinate = tuple(value - 1 for value in destination)
        work_item = f_index(*coordinate, *fine_dims)
        values = []
        for source in sources:
            source_coordinate = tuple(value - 1 for value in source)
            value, operation = _grid_load(
                state, invocation, parameters["z"], *source_coordinate,
                coarse_dims, work_item,
            )
            yield operation
            values.append(value)
        yield from _mg_weighted_update(
            state, invocation, parameters["u"], coordinate, fine_dims,
            work_item, values, coefficient,
        )

    yield _control(
        invocation, canonical.Opcode.BARRIER, invocation.work_items
    )
    for i3 in range(d3, mm3):
        for i2 in range(d2, mm2):
            for i1 in range(d1, mm1):
                yield from emit(
                    (2*i1-d1, 2*i2-d2, 2*i3-d3),
                    ((i1, i2, i3),), 1.0,
                )
            for i1 in range(1, mm1):
                yield from emit(
                    (2*i1-t1, 2*i2-d2, 2*i3-d3),
                    ((i1+1, i2, i3), (i1, i2, i3)), 0.5,
                )
    for i3 in range(d3, mm3):
        for i2 in range(1, mm2):
            for i1 in range(d1, mm1):
                yield from emit(
                    (2*i1-d1, 2*i2-t2, 2*i3-d3),
                    ((i1, i2+1, i3), (i1, i2, i3)), 0.5,
                )
            for i1 in range(1, mm1):
                yield from emit(
                    (2*i1-t1, 2*i2-t2, 2*i3-d3),
                    ((i1+1, i2+1, i3), (i1+1, i2, i3),
                     (i1, i2+1, i3), (i1, i2, i3)), 0.25,
                )
    for i3 in range(1, mm3):
        for i2 in range(d2, mm2):
            for i1 in range(d1, mm1):
                yield from emit(
                    (2*i1-d1, 2*i2-d2, 2*i3-t3),
                    ((i1, i2, i3+1), (i1, i2, i3)), 0.5,
                )
            for i1 in range(1, mm1):
                yield from emit(
                    (2*i1-t1, 2*i2-d2, 2*i3-t3),
                    ((i1+1, i2, i3+1), (i1, i2, i3+1),
                     (i1+1, i2, i3), (i1, i2, i3)), 0.25,
                )
    for i3 in range(1, mm3):
        for i2 in range(1, mm2):
            for i1 in range(d1, mm1):
                yield from emit(
                    (2*i1-d1, 2*i2-t2, 2*i3-t3),
                    ((i1, i2+1, i3+1), (i1, i2, i3+1),
                     (i1, i2+1, i3), (i1, i2, i3)), 0.25,
                )
            for i1 in range(1, mm1):
                yield from emit(
                    (2*i1-t1, 2*i2-t2, 2*i3-t3),
                    ((i1+1, i2+1, i3+1), (i1+1, i2, i3+1),
                     (i1, i2+1, i3+1), (i1, i2, i3+1),
                     (i1+1, i2+1, i3), (i1+1, i2, i3),
                     (i1, i2+1, i3), (i1, i2, i3)), 0.125,
                )
    yield _control(invocation, canonical.Opcode.COMMIT)


def expand_mg_interp(state, invocation, _batch_work_items):
    _require_parameters(invocation, (
        "z", "u", "mm1", "mm2", "mm3", "n1", "n2", "n3",
        "boundaries",
    ))
    parameters = invocation.parameters
    coarse_dims = (
        parameters["mm1"], parameters["mm2"], parameters["mm3"]
    )
    fine_dims = (parameters["n1"], parameters["n2"], parameters["n3"])
    mm1, mm2, mm3 = coarse_dims
    n1, n2, n3 = fine_dims
    if min(coarse_dims + fine_dims) < 3:
        raise lazy.LazyTraceError("MG interp dimensions are too small")
    if invocation.work_items != n1 * n2 * n3:
        raise lazy.LazyTraceError("MG interp work items differ")
    if n1 == 3 or n2 == 3 or n3 == 3:
        yield from _expand_mg_interp_degenerate(
            state, invocation, parameters, coarse_dims, fine_dims
        )
        return
    if fine_dims != (2 * mm1 - 2, 2 * mm2 - 2, 2 * mm3 - 2):
        raise lazy.LazyTraceError("MG interp fine/coarse dimensions differ")

    def load_z(i1, i2, i3, work_item):
        return _grid_load(
            state, invocation, parameters["z"],
            i1 - 1, i2 - 1, i3 - 1, coarse_dims, work_item,
        )

    def loaded_values(coordinates, work_item):
        values = []
        operations = []
        for coordinate in coordinates:
            value, operation = load_z(*coordinate, work_item)
            values.append(value)
            operations.append(operation)
        return values, operations

    yield _control(
        invocation, canonical.Opcode.BARRIER, invocation.work_items
    )
    for i3 in range(1, mm3):
        for i2 in range(1, mm2):
            row_work = (i3 - 1) * (mm2 - 1) + (i2 - 1)
            z1 = {}
            z2 = {}
            z3 = {}
            for i1 in range(1, mm1 + 1):
                values, operations = loaded_values(
                    ((i1, i2 + 1, i3), (i1, i2, i3)), row_work
                )
                yield from operations
                fold = _run_fold(_fold_add(invocation, row_work, values))
                while True:
                    try:
                        yield next(fold)
                    except StopIteration as stop:
                        z1[i1] = stop.value
                        break
                values, operations = loaded_values(
                    ((i1, i2, i3 + 1), (i1, i2, i3)), row_work
                )
                yield from operations
                fold = _run_fold(_fold_add(invocation, row_work, values))
                while True:
                    try:
                        yield next(fold)
                    except StopIteration as stop:
                        z2[i1] = stop.value
                        break
                values, operations = loaded_values(
                    ((i1, i2 + 1, i3 + 1), (i1, i2, i3 + 1)), row_work
                )
                yield from operations
                values.append(z1[i1])
                fold = _run_fold(_fold_add(invocation, row_work, values))
                while True:
                    try:
                        yield next(fold)
                    except StopIteration as stop:
                        z3[i1] = stop.value
                        break

            for i1 in range(1, mm1):
                odd = (2 * i1 - 2, 2 * i2 - 2, 2 * i3 - 2)
                even = (2 * i1 - 1, 2 * i2 - 2, 2 * i3 - 2)
                work_item = f_index(*odd, *fine_dims)
                values, operations = loaded_values(
                    ((i1, i2, i3),), work_item
                )
                yield from operations
                yield from _mg_weighted_update(
                    state, invocation, parameters["u"], odd, fine_dims,
                    work_item, values, 1.0,
                )
                work_item = f_index(*even, *fine_dims)
                values, operations = loaded_values(
                    ((i1 + 1, i2, i3), (i1, i2, i3)), work_item
                )
                yield from operations
                yield from _mg_weighted_update(
                    state, invocation, parameters["u"], even, fine_dims,
                    work_item, values, 0.5,
                )
            for i1 in range(1, mm1):
                odd = (2 * i1 - 2, 2 * i2 - 1, 2 * i3 - 2)
                even = (2 * i1 - 1, 2 * i2 - 1, 2 * i3 - 2)
                work_item = f_index(*odd, *fine_dims)
                yield from _mg_weighted_update(
                    state, invocation, parameters["u"], odd, fine_dims,
                    work_item, [z1[i1]], 0.5,
                )
                work_item = f_index(*even, *fine_dims)
                yield from _mg_weighted_update(
                    state, invocation, parameters["u"], even, fine_dims,
                    work_item, [z1[i1], z1[i1 + 1]], 0.25,
                )
            for i1 in range(1, mm1):
                odd = (2 * i1 - 2, 2 * i2 - 2, 2 * i3 - 1)
                even = (2 * i1 - 1, 2 * i2 - 2, 2 * i3 - 1)
                work_item = f_index(*odd, *fine_dims)
                yield from _mg_weighted_update(
                    state, invocation, parameters["u"], odd, fine_dims,
                    work_item, [z2[i1]], 0.5,
                )
                work_item = f_index(*even, *fine_dims)
                yield from _mg_weighted_update(
                    state, invocation, parameters["u"], even, fine_dims,
                    work_item, [z2[i1], z2[i1 + 1]], 0.25,
                )
            for i1 in range(1, mm1):
                odd = (2 * i1 - 2, 2 * i2 - 1, 2 * i3 - 1)
                even = (2 * i1 - 1, 2 * i2 - 1, 2 * i3 - 1)
                work_item = f_index(*odd, *fine_dims)
                yield from _mg_weighted_update(
                    state, invocation, parameters["u"], odd, fine_dims,
                    work_item, [z3[i1]], 0.25,
                )
                work_item = f_index(*even, *fine_dims)
                yield from _mg_weighted_update(
                    state, invocation, parameters["u"], even, fine_dims,
                    work_item, [z3[i1], z3[i1 + 1]], 0.125,
                )
    yield _control(invocation, canonical.Opcode.COMMIT)


EXPANDERS = {
    "npb_cg_spmv": expand_cg_spmv,
    "npb_cg_dot": expand_cg_dot,
    "npb_cg_divide": expand_cg_divide,
    "npb_cg_update_zr": expand_cg_update_zr,
    "npb_cg_update_p": expand_cg_update_p,
    "npb_cg_residual_norm": expand_cg_residual_norm,
    "npb_cg_init": expand_cg_init,
    "npb_cg_outer_dots": expand_cg_outer_dots,
    "npb_cg_normalize": expand_cg_normalize,
    "npb_cg_prepare_iteration": expand_cg_prepare_iteration,
    "npb_mg_resid": expand_mg_resid,
    "npb_mg_norm2u3": expand_mg_norm2u3,
    "npb_mg_psinv": expand_mg_psinv,
    "npb_mg_rprj3": expand_mg_rprj3,
    "npb_mg_interp": expand_mg_interp,
}


def replay_boundaries(bundle, *, batch_work_items=1):
    boundaries = {}
    count = 0
    with lazy.MappedState(bundle) as state:
        for invocation in bundle.invocations:
            try:
                expander = EXPANDERS[invocation.kernel]
            except KeyError as error:
                raise lazy.LazyTraceError(
                    f"unknown NPB kernel {invocation.kernel}"
                ) from error
            for operation in expander(state, invocation, batch_work_items):
                lazy._validate_memory_address(bundle, operation)
                count += 1
            boundary_arrays = []
            destination = invocation.parameters.get("destination")
            if destination is not None:
                boundary_arrays.append(destination)
            boundary_arrays.extend(invocation.parameters.get("boundaries", []))
            for boundary in boundary_arrays:
                boundaries[
                    f"{boundary}.iter{invocation.iteration}"
                ] = state.boundary_sha256(boundary)
            result = invocation.parameters.get("result")
            if result is not None:
                boundaries[
                    f"scalar.{result}.iter{invocation.iteration}"
                ] = state.scalar_sha256(result)
            for result in invocation.parameters.get("results", []):
                boundaries[
                    f"scalar.{result}.iter{invocation.iteration}"
                ] = state.scalar_sha256(result)
    if count != bundle.dynamic_work["primitive_records"]:
        raise lazy.LazyTraceError(
            f"dynamic primitive count {count} != "
            f"{bundle.dynamic_work['primitive_records']}"
        )
    return boundaries
