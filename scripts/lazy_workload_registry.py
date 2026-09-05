#!/usr/bin/env python3
# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

"""Fail-closed dispatch for schema-2 canonical workload expanders."""

try:
    from scripts import gap_bc_lazy_trace as gap_bc
    from scripts import lazy_work_trace as lazy
    from scripts import mcf_lazy_trace as mcf
    from scripts import npb_lazy_trace as npb
    from scripts import pr_spmv_lazy_trace as pr_spmv
except ImportError:
    import gap_bc_lazy_trace as gap_bc
    import lazy_work_trace as lazy
    import mcf_lazy_trace as mcf
    import npb_lazy_trace as npb
    import pr_spmv_lazy_trace as pr_spmv


NPB_PHASE_NAMES = {
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
_MODULES = (npb, gap_bc, pr_spmv, mcf)


def module_for_kernel(kernel):
    matches = [module for module in _MODULES if kernel in module.EXPANDERS]
    if len(matches) != 1:
        raise lazy.LazyTraceError(
            f"lazy kernel registry match count for {kernel!r} is {len(matches)}"
        )
    return matches[0]


def expander(invocation):
    return module_for_kernel(invocation.kernel).EXPANDERS[invocation.kernel]


def boundary_specs(bundle, invocation):
    module = module_for_kernel(invocation.kernel)
    return module.invocation_boundary_specs(bundle, invocation)


def expand_slice(
    state, invocation, first, stop, batch_work_items=1024,
    *, include_controls=True,
):
    module = module_for_kernel(invocation.kernel)
    if module in {gap_bc, pr_spmv, mcf}:
        return module.expand_slice(
            state, invocation, first, stop, batch_work_items,
            include_controls=include_controls,
        )
    if not include_controls:
        raise lazy.LazyTraceError(
            "control-free slicing is not defined for this lazy workload"
        )
    return module.expand_slice(state, invocation, first, stop, batch_work_items)


def fixed_controls(invocation):
    module = module_for_kernel(invocation.kernel)
    function = getattr(module, "fixed_controls", None)
    if function is None:
        raise lazy.LazyTraceError(
            f"fixed controls are not defined for {invocation.kernel}"
        )
    return function(invocation)


def fast_forward(state, invocation, first=0, stop=None):
    module = module_for_kernel(invocation.kernel)
    function = getattr(module, "fast_forward", None)
    if function is None:
        raise lazy.LazyTraceError(
            f"fast-forward is not defined for {invocation.kernel}"
        )
    return function(state, invocation, first, stop)


def phase_name(bundle, phase):
    names = bundle.meta.get("phase_names")
    if isinstance(names, dict):
        value = names.get(str(phase), names.get(phase))
        if isinstance(value, str) and value:
            return value
        raise lazy.LazyTraceError(
            f"selected lazy phase {phase} has no declared name"
        )
    value = NPB_PHASE_NAMES.get(phase)
    if value is None:
        raise lazy.LazyTraceError(
            f"selected lazy phase {phase} has no canonical name"
        )
    return value
