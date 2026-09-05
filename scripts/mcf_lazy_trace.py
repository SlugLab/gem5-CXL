#!/usr/bin/env python3
# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

"""Lossless lazy expansion for the formal MCF pricing-window trace."""

try:
    from scripts import canonical_work_trace as canonical
    from scripts import lazy_work_trace as lazy
except ImportError:
    import canonical_work_trace as canonical
    import lazy_work_trace as lazy


PHASE_PRICING = 401
KERNEL = "mcf_pricing_window"
BASES = {
    "costs": 0x100000000,
    "tail_potentials": 0x200000000,
    "head_potentials": 0x300000000,
    "idents": 0x400000000,
    "candidate_count": 0x500000000,
    "best_violation": 0x500000008,
}
MASK64 = (1 << 64) - 1
I64_MAX = (1 << 63) - 1


class McfTraceError(lazy.LazyTraceError):
    """An MCF pricing descriptor or expansion violates its contract."""


def signed(raw):
    return raw - (1 << 64) if raw & (1 << 63) else raw


def bits(value):
    return value & MASK64


def _parameters(invocation):
    parameters = invocation.parameters
    if (
        invocation.kernel != KERNEL
        or invocation.phase != PHASE_PRICING
        or not isinstance(invocation.work_items, int)
        or invocation.work_items <= 0
    ):
        raise McfTraceError("MCF pricing invocation identity differs")
    expected_records = 7 * invocation.work_items + 4
    if parameters.get("record_count") != expected_records:
        raise McfTraceError("MCF pricing primitive count differs")
    candidate_count = parameters.get("candidate_count")
    best_violation = parameters.get("best_violation")
    if (
        not isinstance(candidate_count, int)
        or isinstance(candidate_count, bool)
        or candidate_count < 0
        or candidate_count > invocation.work_items
        or not isinstance(best_violation, int)
        or isinstance(best_violation, bool)
        or best_violation < -(1 << 63)
        or best_violation > I64_MAX
    ):
        raise McfTraceError("MCF pricing result parameters differ")
    return candidate_count, best_violation


def _slice(invocation, first, stop):
    for value, label in ((first, "first"), (stop, "stop")):
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise McfTraceError(f"MCF pricing slice {label} is invalid")
    if first >= stop or stop > invocation.work_items:
        raise McfTraceError("MCF pricing slice is empty or invalid")


def _load(invocation, work_item, address, raw):
    return canonical.Operation(
        invocation.phase, canonical.Opcode.LOAD_U64, work_item, 0,
        address, raw, 0, raw,
    )


def _add(invocation, work_item, left, right, result):
    return canonical.Operation(
        invocation.phase, canonical.Opcode.I64_ADD, work_item, 0,
        0, left, right, result,
    )


def _minimum(invocation, work_item, left, right, result):
    return canonical.Operation(
        invocation.phase, canonical.Opcode.I64_MIN, work_item, 0,
        0, left, right, result,
    )


def _store(invocation, work_item, address, raw):
    return canonical.Operation(
        invocation.phase, canonical.Opcode.STORE_U64, work_item, 0,
        address, raw, 0, raw,
    )


def _control(invocation, opcode):
    return canonical.Operation(
        invocation.phase, opcode, invocation.work_items, 0,
        0, 0, invocation.work_items, 0,
    )


def _running_state(state):
    count = state.load_raw("candidate_count", 0)[1]
    best_raw = state.load_raw("best_violation", 0)[1]
    return count, signed(best_raw)


def _scan_values(state, first, stop, count, best):
    for work_item in range(first, stop):
        cost = state.load_raw("costs", work_item)[1]
        tail = state.load_raw("tail_potentials", work_item)[1]
        head = state.load_raw("head_potentials", work_item)[1]
        ident = state.load_raw("idents", work_item)[1]
        partial = (cost - tail) & MASK64
        reduced = (partial + head) & MASK64
        reduced_signed = signed(reduced)
        if ((ident == 1 and reduced_signed < 0)
                or (ident == 2 and reduced_signed > 0)):
            count += 1
        if reduced_signed < best:
            best = reduced_signed
    state.store_raw("candidate_count", 0, count)
    state.store_raw("best_violation", 0, bits(best))
    return count, best


def fast_forward(state, invocation, first=0, stop=None):
    """Advance aggregate state without emitting primitive operations."""

    expected_count, expected_best = _parameters(invocation)
    if stop is None:
        stop = invocation.work_items
    _slice(invocation, first, stop)
    count, best = _running_state(state)
    count, best = _scan_values(state, first, stop, count, best)
    if stop == invocation.work_items:
        if count != expected_count or best != expected_best:
            raise McfTraceError("MCF pricing derived result differs")
    return count, best


def expand_slice(
    state, invocation, first, stop, batch_work_items=1024,
    *, include_controls=True,
):
    """Expand a contiguous scan slice while preserving prefix reductions."""

    expected_count, expected_best = _parameters(invocation)
    lazy._validate_batch_work_items(batch_work_items)
    _slice(invocation, first, stop)
    if include_controls and (first != 0 or stop != invocation.work_items):
        raise McfTraceError(
            "MCF pricing controls require the complete invocation"
        )
    count, best = _running_state(state)
    for work_item in range(first, stop):
        cost_address, cost = state.load_raw("costs", work_item)
        tail_address, tail = state.load_raw("tail_potentials", work_item)
        head_address, head = state.load_raw("head_potentials", work_item)
        ident_address, ident = state.load_raw("idents", work_item)
        yield _load(invocation, work_item, cost_address, cost)
        yield _load(invocation, work_item, tail_address, tail)
        yield _load(invocation, work_item, head_address, head)
        yield _load(invocation, work_item, ident_address, ident)
        partial = (cost - tail) & MASK64
        reduced = (partial + head) & MASK64
        yield _add(invocation, work_item, cost, (-tail) & MASK64, partial)
        yield _add(invocation, work_item, partial, head, reduced)
        prior_best = best
        reduced_signed = signed(reduced)
        if reduced_signed < best:
            best = reduced_signed
        yield _minimum(
            invocation, work_item, bits(prior_best), reduced, bits(best)
        )
        if ((ident == 1 and reduced_signed < 0)
                or (ident == 2 and reduced_signed > 0)):
            count += 1
    state.store_raw("candidate_count", 0, count)
    state.store_raw("best_violation", 0, bits(best))
    if stop == invocation.work_items:
        if count != expected_count or best != expected_best:
            raise McfTraceError("MCF pricing derived result differs")
    if include_controls:
        yield from fixed_controls(invocation)


def expand_pricing(state, invocation, batch_work_items):
    yield from expand_slice(
        state, invocation, 0, invocation.work_items, batch_work_items,
        include_controls=True,
    )


def fixed_controls(invocation):
    candidate_count, best_violation = _parameters(invocation)
    return (
        _store(
            invocation, invocation.work_items,
            BASES["candidate_count"], candidate_count,
        ),
        _store(
            invocation, invocation.work_items,
            BASES["best_violation"], bits(best_violation),
        ),
        _control(invocation, canonical.Opcode.BARRIER),
        _control(invocation, canonical.Opcode.COMMIT),
    )


def invocation_boundary_specs(bundle, invocation):
    _parameters(invocation)
    specifications = []
    for name in ("candidate_count", "best_violation"):
        try:
            image = next(array for array in bundle.arrays if array.name == name)
        except StopIteration as error:
            raise McfTraceError(f"MCF {name} boundary is missing") from error
        if (
            image.element_type != "u64" or image.count != 1
            or image.logical_base != BASES[name]
        ):
            raise McfTraceError(f"MCF {name} boundary shape differs")
        specifications.append((name, 64, 1, image.logical_base))
    return tuple(specifications)


EXPANDERS = {KERNEL: expand_pricing}
