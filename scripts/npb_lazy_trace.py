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
