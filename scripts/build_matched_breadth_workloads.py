#!/usr/bin/env python3
# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

"""Build exact MCF and Spatter canonical-region adapters."""

import argparse
import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import sys
from pathlib import Path

try:
    from scripts import canonical_work_trace as canonical
    from scripts import cross_system_contract as contract
    from scripts import lazy_work_trace as lazy
    from scripts import npb_lazy_trace as npb
except ImportError:
    import canonical_work_trace as canonical
    import cross_system_contract as contract
    import lazy_work_trace as lazy
    import npb_lazy_trace as npb


REPO = Path(__file__).resolve().parents[1]
BUILDER_SOURCE = Path(__file__).resolve()
SOURCE_ROOT = REPO / "util/amu/matched_workloads"
SOURCES = {
    "mcf": SOURCE_ROOT / "mcf_regions.cc",
    "spatter": SOURCE_ROOT / "spatter_regions.cc",
}
TRACE_ABI = SOURCE_ROOT / "canonical_trace.hh"
NPB_TRACE_HOOKS = SOURCE_ROOT / "npb_trace_hooks.h"
NPB_TRACE_IMPLEMENTATION = SOURCE_ROOT / "npb_trace_hooks.cc"
NPB_PATCHES = {
    "cg": SOURCE_ROOT / "npb-cg-trace.patch",
    "mg": SOURCE_ROOT / "npb-mg-trace.patch",
}
CANONICAL_TRACE_SOURCE = REPO / "scripts/canonical_work_trace.py"
LAZY_TRACE_SOURCE = REPO / "scripts/lazy_work_trace.py"
NPB_EXPANDER_SOURCE = REPO / "scripts/npb_lazy_trace.py"
BACKENDS = ("reference", "vanilla", "amu", "cira")
STRICT_FLAGS = ("-O3", "-fopenmp", "-ffp-contract=off", "-fno-fast-math")
COMMAND_FLAGS = ("-std=c++17", *STRICT_FLAGS, "-Wall", "-Wextra", "-Werror")
BACKEND_IDS = {name: index for index, name in enumerate(BACKENDS)}
FUNCTIONAL_SYSTEMS = ("vanilla", "amu", "cira", "m2ndp-funcsim")
TIMING_SYSTEMS = ("vanilla", "amu", "cira", "m2ndp")
MCF_OUTPUTS = (
    "objective", "flow", "cost", "potential", "predecessor", "depth",
    "orientation", "tree",
)
MCF_BASES = {
    "arc": 0x100000000,
    "potential": 0x200000000,
    "tree": 0x300000000,
    "predecessor": 0x310000000,
    "depth": 0x320000000,
    "orientation": 0x330000000,
    "objective": 0x400000000,
    "pricing_offsets": 0x500000000,
    "pricing_index": 0x600000000,
    "price_out_index": 0x700000000,
}


def latency_action_layout(workload, phases):
    """Return the latency-sharing layout used by a prepared formal suite.

    Functional evidence is a content-addressed shared object.  Timing action
    fragments are latency-local and retain the canonical delay placeholder;
    the formal builder resolves the remaining tool/input paths only after all
    six frozen inputs are available.
    """
    if not isinstance(workload, str) or not workload:
        raise BuildError("action-layout workload is invalid")
    phases = tuple(phases)
    if not phases or any(
        not isinstance(phase, str) or not phase for phase in phases
    ):
        raise BuildError("action-layout phases are invalid")
    functional = {
        system: {
            "command": [],
            "evidence": f"shared/functional/{workload}/{system}/evidence.json",
        }
        for system in FUNCTIONAL_SYSTEMS
    }
    windows = {}
    for phase in phases:
        windows[phase] = {}
        for system in TIMING_SYSTEMS:
            windows[phase][system] = {
                "runner": (
                    "scripts/run_matched_breadth_gem5.py"
                    if system != "m2ndp"
                    else "scripts/m2ndp_workload_trace.py:run_ndpsim_package"
                ),
                "command": [
                    "--cxl-link-delay", "{{cxl_link_delay}}",
                ],
                "evidence": (
                    "timing/{{cxl_link_delay}}/"
                    f"{workload}/{phase}/{system}/{{{{window_index}}}}.json"
                ),
            }
    return {"functional": functional, "window": windows}


SPATTER_BASES = {
    "index": 0x100000000,
    "values": 0x200000000,
    "destination": 0x300000000,
}


class BuildError(RuntimeError):
    """A matched breadth build or reference execution failed closed."""


def _replace_exact(text, old, new, label, *, occurrences=1):
    actual = text.count(old)
    if actual != occurrences:
        raise BuildError(
            f"NPB exact-anchor patch rejected {label}: "
            f"found {actual}, expected {occurrences}"
        )
    return text.replace(old, new)


def _fixed_sum_block(accumulators, loop_text):
    """Return a four-lane OpenMP loop with an explicit binary merge tree."""
    private = ",".join(f"matched_part{index + 1}" for index in range(len(accumulators)))
    initializers = "\n".join(
        f"      matched_part{index + 1} = 0.0d0"
        for index in range(len(accumulators))
    )
    body = loop_text
    for index, accumulator in enumerate(accumulators):
        body = re.sub(
            rf"(?m)^(\s*){re.escape(accumulator)}(\s*=\s*){re.escape(accumulator)}(\s*\+)",
            rf"\1matched_part{index + 1}\2matched_part{index + 1}\3",
            body,
        )
    stores = "\n".join(
        f"      matched_lane{index + 1}(matched_tid) = matched_part{index + 1}"
        for index in range(len(accumulators))
    )
    merges = "\n".join(
        f"         {accumulator} = matched_reduce_sum4(matched_lane{index + 1})"
        for index, accumulator in enumerate(accumulators)
    )
    return (
        "c MATCHED_TRACE_BEGIN fixed four-lane sum\n"
        "!$omp parallel default(shared)\n"
        f"!$omp& private(j,matched_tid,{private})\n"
        "      matched_tid = omp_get_thread_num()\n"
        "!$omp master\n"
        "      matched_threads = omp_get_num_threads()\n"
        "      call matched_require_four_threads(matched_threads)\n"
        "!$omp end master\n"
        "!$omp barrier\n"
        f"{initializers}\n"
        "!$omp do schedule(static)\n"
        f"{body}\n"
        "!$omp end do\n"
        f"{stores}\n"
        "!$omp barrier\n"
        "!$omp master\n"
        f"{merges}\n"
        "!$omp end master\n"
        "!$omp barrier\n"
        "!$omp end parallel\n"
        "c MATCHED_TRACE_END fixed four-lane sum"
    )


def _strip_eager_primitive_calls(text):
    """Remove the obsolete per-load/arithmetic trace calls as whole records."""
    lines = text.splitlines(keepends=True)
    result = []
    cursor = 0
    while cursor < len(lines):
        if re.match(
            r"\s*matched_(?:work|ord|addr|opc|l|r|res)\s*=",
            lines[cursor],
        ):
            cursor += 1
            continue
        if re.match(r"\s*call matched_trace_", lines[cursor]):
            cursor += 1
            while cursor < len(lines) and re.match(r"\s*>", lines[cursor]):
                cursor += 1
            continue
        result.append(lines[cursor])
        cursor += 1
    transformed = "".join(result)
    if "call matched_trace_" in transformed:
        raise BuildError("NPB patch retained an eager primitive trace call")
    return transformed


def _upgrade_boundary_hooks(text):
    """Use the width-explicit SHA boundary ABI in fixed-form Fortran."""
    lines = text.splitlines()
    result = []
    cursor = 0
    inline = re.compile(
        r"^(\s*)call matched_dump_f64\(([^,]+),([^,]+),([^,]+),([^)]+)\)$"
    )
    while cursor < len(lines):
        line = lines[cursor]
        match = inline.match(line)
        if match:
            indent, boundary, iteration, data, count = match.groups()
            result.append(
                f"{indent}call matched_boundary_sha256({boundary},"
                f"{iteration},"
            )
            result.append(f"     >     {data},64_8,{count})")
            cursor += 1
            continue
        if "call matched_dump_f64(" in line:
            if cursor + 1 >= len(lines):
                raise BuildError("NPB boundary call continuation is missing")
            continuation = lines[cursor + 1]
            continuation_match = re.match(
                r"^(\s*>\s*)(.+),([^,]+)\)$", continuation
            )
            if continuation_match is None:
                raise BuildError("NPB boundary call continuation differs")
            prefix, data, count = continuation_match.groups()
            result.append(line.replace(
                "call matched_dump_f64(",
                "call matched_boundary_sha256(",
            ))
            result.append(f"{prefix}{data},64_8,{count})")
            cursor += 2
            continue
        result.append(line)
        cursor += 1
    transformed = "\n".join(result) + ("\n" if text.endswith("\n") else "")
    if "matched_dump_f64" in transformed:
        raise BuildError("NPB patch retained a raw boundary dump")
    return transformed


def _transform_cg(text):
    main_decl = "      double precision   norm_temp1,norm_temp2,norm_temp3"
    main_decl_new = """      double precision   norm_temp1,norm_temp2,norm_temp3
c MATCHED_TRACE_BEGIN declarations
      double precision matched_lane1(0:3),matched_lane2(0:3)
      double precision matched_part1,matched_part2
      double precision matched_reduce_sum4
      integer matched_tid,omp_get_thread_num,omp_get_num_threads
      integer*8 matched_id,matched_iter,matched_count,matched_threads
      integer*8 matched_allocated_bytes,matched_workload
      integer*8 matched_bits,matched_base,matched_nnz
      integer*8 matched_ordinal,matched_kernel,matched_phase
      integer*8 matched_params(11),matched_param_count
      external matched_reduce_sum4
      external omp_get_thread_num,omp_get_num_threads
c MATCHED_TRACE_END declarations"""
    text = _replace_exact(text, main_decl, main_decl_new, "CG main declarations")
    text = _replace_exact(
        text,
        " 1003 format(' Number of available threads: ', i5)\n\n      naa = na",
        """ 1003 format(' Number of available threads: ', i5)
c MATCHED_TRACE_BEGIN runtime thread gate
      matched_threads=omp_get_max_threads()
      call matched_require_four_threads(matched_threads)
c MATCHED_TRACE_END runtime thread gate

      naa = na""",
        "CG runtime thread gate",
    )
    text = _replace_exact(
        text,
        "      call alloc_space\n\nc---------------------------------------------------------------------\nc  Inialize random number generator",
        """      call alloc_space
c MATCHED_TRACE_BEGIN allocation probe
      matched_allocated_bytes=0
      matched_allocated_bytes=matched_allocated_bytes
     >     +4_8*(size(colidx,kind=8)+size(rowstr,kind=8)
     >     +size(iv,kind=8)+size(arow,kind=8)+size(acol,kind=8))
      matched_allocated_bytes=matched_allocated_bytes
     >     +8_8*(size(v,kind=8)+size(aelt,kind=8)+size(a,kind=8)
     >     +size(x,kind=8)+size(z,kind=8)+size(p,kind=8)
     >     +size(q,kind=8)+size(r,kind=8))
      matched_workload=1
      call matched_allocation_probe(matched_workload,
     >     matched_allocated_bytes)
c MATCHED_TRACE_END allocation probe

c---------------------------------------------------------------------
c  Inialize random number generator""",
        "CG allocation probe",
    )

    original_norm = """!$omp parallel default(shared) private(j,norm_temp3)
!$omp do reduction(+:norm_temp1,norm_temp2)
         do j=1, lastcol-firstcol+1
            norm_temp1 = norm_temp1 + x(j)*z(j)
            norm_temp2 = norm_temp2 + z(j)*z(j)
         enddo
!$omp end do"""
    norm_loop = """         do j=1, lastcol-firstcol+1
            norm_temp1 = norm_temp1 + x(j)*z(j)
            norm_temp2 = norm_temp2 + z(j)*z(j)
         enddo"""
    fixed_norm = _fixed_sum_block(("norm_temp1", "norm_temp2"), norm_loop)
    text = _replace_exact(
        text, original_norm, fixed_norm, "CG inverse-power dot reductions",
        occurrences=2,
    )
    # The replacement owns the parallel region; retain the normalization loop
    # in a separate region in both warmup and measured iterations.
    text = text.replace(
        "         norm_temp3 = 1.0d0 / sqrt( norm_temp2 )\n\n\n",
        "         norm_temp3 = 1.0d0 / sqrt( norm_temp2 )\n"
        "!$omp parallel default(shared) private(j)\n\n",
        2,
    )
    text = _replace_exact(
        text,
        "c MATCHED_TRACE_BEGIN fixed four-lane sum\n!$omp parallel",
        """c MATCHED_TRACE_BEGIN fixed four-lane sum
      matched_id=103
      matched_iter=it
      matched_count=lastcol-firstcol+1
      call matched_phase_begin(matched_id,matched_iter,matched_count)
!$omp parallel""",
        "CG outer dot phase begins", occurrences=2,
    )
    text = _replace_exact(
        text,
        "!$omp end parallel\nc MATCHED_TRACE_END fixed four-lane sum",
        """!$omp end parallel
      call matched_phase_end(matched_id,matched_iter)
c MATCHED_TRACE_END fixed four-lane sum""",
        "CG outer dot phase ends", occurrences=2,
    )

    conj_decl = "      double precision   d, sum, rho, rho0, alpha, beta, rnorm, suml"
    conj_decl_new = """      double precision   d, sum, rho, rho0, alpha, beta, rnorm, suml
c MATCHED_TRACE_BEGIN declarations
      double precision matched_lane1(0:3),matched_part1
      double precision matched_reduce_sum4
      integer matched_tid,omp_get_thread_num,omp_get_num_threads
      integer*8 matched_id,matched_iter,matched_count,matched_threads
      integer*8 matched_call,matched_boundary_iter
      integer*8 matched_work,matched_ord,matched_addr,matched_opc
      double precision matched_l,matched_r,matched_res
      save matched_call
      data matched_call /0/
      external matched_reduce_sum4
      external omp_get_thread_num,omp_get_num_threads
c MATCHED_PHASE cg_spmv
c MATCHED_PHASE cg_vector_update
c MATCHED_PHASE cg_dot
c MATCHED_PHASE cg_conj_grad
c MATCHED_THREADS 4
c MATCHED_FIXED_REDUCTION_TREE
c MATCHED_TRACE_END declarations"""
    text = _replace_exact(text, conj_decl, conj_decl_new, "CG conj_grad declarations")
    text = _replace_exact(
        text,
        "!$omp parallel default(shared) private(j,k,cgit,suml,alpha,beta)\n!$omp&  shared(d,rho0,rho,sum)",
        """c MATCHED_TRACE_BEGIN conj_grad begin
      matched_id=104
      matched_iter=matched_call
      matched_call=matched_call+1
      matched_count=lastcol-firstcol+1
      call matched_phase_begin(matched_id,matched_iter,matched_count)
c MATCHED_TRACE_END conj_grad begin
!$omp parallel default(shared)
!$omp& private(j,k,cgit,suml,alpha,beta,matched_tid,matched_part1)
!$omp& private(matched_work,matched_ord,matched_addr,matched_opc)
!$omp& private(matched_l,matched_r,matched_res)
!$omp& shared(d,rho0,rho,sum,matched_lane1,matched_threads)
!$omp& shared(matched_boundary_iter)
      matched_tid=omp_get_thread_num()
!$omp master
      matched_threads=omp_get_num_threads()
      call matched_require_four_threads(matched_threads)
!$omp end master
!$omp barrier""",
        "CG parallel region",
    )

    def reduction(old_directive, accumulator, loop, label, end_directive="!$omp end do"):
        nonlocal text
        old = f"{old_directive}\n{loop}\n{end_directive}"
        body = loop.replace(
            f"{accumulator} = {accumulator} +",
            "matched_part1 = matched_part1 +",
        ).replace(
            f"{accumulator}  = {accumulator} +",
            "matched_part1  = matched_part1 +",
        )
        if label == "CG initial rho":
            body = body.replace(
                "         matched_part1 = matched_part1 + r(j)*r(j)",
                """         matched_work=j
         matched_ord=0
         matched_addr=7000000000000_8+8_8*(j-1)
         call matched_trace_load_f64(matched_work,matched_ord,
     >        matched_addr,r(j))
         matched_ord=matched_ord+1
         matched_l=matched_part1
         matched_r=r(j)*r(j)
         matched_opc=18
         call matched_trace_binary_f64(matched_opc,matched_work,
     >        matched_ord,r(j),r(j),matched_r)
         matched_ord=matched_ord+1
         matched_part1 = matched_part1 + r(j)*r(j)
         matched_opc=12
         call matched_trace_binary_f64(matched_opc,matched_work,
     >        matched_ord,matched_l,matched_r,matched_part1)""",
            )
        elif label == "CG p dot q":
            body = body.replace(
                "            matched_part1 = matched_part1 + p(j)*q(j)",
                """            matched_work=j
            matched_ord=0
            matched_addr=4000000000000_8+8_8*(j-1)
            call matched_trace_load_f64(matched_work,matched_ord,
     >           matched_addr,p(j))
            matched_ord=matched_ord+1
            matched_addr=5000000000000_8+8_8*(j-1)
            call matched_trace_load_f64(matched_work,matched_ord,
     >           matched_addr,q(j))
            matched_ord=matched_ord+1
            matched_l=matched_part1
            matched_r=p(j)*q(j)
            matched_opc=18
            call matched_trace_binary_f64(matched_opc,matched_work,
     >           matched_ord,p(j),q(j),matched_r)
            matched_ord=matched_ord+1
            matched_part1 = matched_part1 + p(j)*q(j)
            matched_opc=12
            call matched_trace_binary_f64(matched_opc,matched_work,
     >           matched_ord,matched_l,matched_r,matched_part1)""",
            )
        elif label == "CG updated rho":
            body = body.replace(
                "            z(j) = z(j) + alpha*p(j)",
                """            matched_work=j
            matched_ord=0
            matched_addr=6000000000000_8+8_8*(j-1)
            call matched_trace_load_f64(matched_work,matched_ord,
     >           matched_addr,z(j))
            matched_ord=matched_ord+1
            matched_addr=4000000000000_8+8_8*(j-1)
            call matched_trace_load_f64(matched_work,matched_ord,
     >           matched_addr,p(j))
            matched_ord=matched_ord+1
            matched_l=z(j)
            matched_r=alpha*p(j)
            matched_opc=18
            call matched_trace_binary_f64(matched_opc,matched_work,
     >           matched_ord,alpha,p(j),matched_r)
            matched_ord=matched_ord+1
            z(j) = z(j) + alpha*p(j)
            matched_opc=12
            call matched_trace_binary_f64(matched_opc,matched_work,
     >           matched_ord,matched_l,matched_r,z(j))
            matched_ord=matched_ord+1
            matched_addr=6000000000000_8+8_8*(j-1)
            call matched_trace_store_f64(matched_work,matched_ord,
     >           matched_addr,z(j))
            matched_ord=matched_ord+1""",
            ).replace(
                "            r(j) = r(j) - alpha*q(j)",
                """            matched_addr=7000000000000_8+8_8*(j-1)
            call matched_trace_load_f64(matched_work,matched_ord,
     >           matched_addr,r(j))
            matched_ord=matched_ord+1
            matched_addr=5000000000000_8+8_8*(j-1)
            call matched_trace_load_f64(matched_work,matched_ord,
     >           matched_addr,q(j))
            matched_ord=matched_ord+1
            matched_l=r(j)
            matched_r=alpha*q(j)
            matched_opc=18
            call matched_trace_binary_f64(matched_opc,matched_work,
     >           matched_ord,alpha,q(j),matched_r)
            matched_ord=matched_ord+1
            r(j) = r(j) - alpha*q(j)
            matched_opc=19
            call matched_trace_binary_f64(matched_opc,matched_work,
     >           matched_ord,matched_l,matched_r,r(j))
            matched_ord=matched_ord+1
            call matched_trace_store_f64(matched_work,matched_ord,
     >           matched_addr,r(j))
            matched_ord=matched_ord+1""",
            ).replace(
                "            matched_part1 = matched_part1 + r(j)*r(j)",
                """            matched_l=matched_part1
            matched_r=r(j)*r(j)
            matched_opc=18
            call matched_trace_binary_f64(matched_opc,matched_work,
     >           matched_ord,r(j),r(j),matched_r)
            matched_ord=matched_ord+1
            matched_part1 = matched_part1 + r(j)*r(j)
            matched_opc=12
            call matched_trace_binary_f64(matched_opc,matched_work,
     >           matched_ord,matched_l,matched_r,matched_part1)""",
            )
        elif label == "CG residual norm":
            body = body.replace(
                "         suml = x(j) - r(j)",
                """         matched_work=j
         matched_ord=0
         matched_addr=8000000000000_8+8_8*(j-1)
         call matched_trace_load_f64(matched_work,matched_ord,
     >        matched_addr,x(j))
         matched_ord=matched_ord+1
         matched_addr=7000000000000_8+8_8*(j-1)
         call matched_trace_load_f64(matched_work,matched_ord,
     >        matched_addr,r(j))
         matched_ord=matched_ord+1
         suml = x(j) - r(j)
         matched_opc=19
         call matched_trace_binary_f64(matched_opc,matched_work,
     >        matched_ord,x(j),r(j),suml)
         matched_ord=matched_ord+1""",
            ).replace(
                "         matched_part1  = matched_part1 + suml*suml",
                """         matched_l=matched_part1
         matched_r=suml*suml
         matched_opc=18
         call matched_trace_binary_f64(matched_opc,matched_work,
     >        matched_ord,suml,suml,matched_r)
         matched_ord=matched_ord+1
         matched_part1  = matched_part1 + suml*suml
         matched_opc=12
         call matched_trace_binary_f64(matched_opc,matched_work,
     >        matched_ord,matched_l,matched_r,matched_part1)""",
            )
        new = f"""c MATCHED_TRACE_BEGIN {label}
      matched_part1=0.0d0
!$omp do schedule(static)
{body}
!$omp end do
      matched_lane1(matched_tid)=matched_part1
!$omp barrier
!$omp master
         {accumulator}=matched_reduce_sum4(matched_lane1)
!$omp end master
!$omp barrier
c MATCHED_TRACE_END {label}"""
        text = _replace_exact(text, old, new, label)

    reduction(
        "!$omp do reduction(+:rho)", "rho",
        """      do j=1, lastcol-firstcol+1
         rho = rho + r(j)*r(j)
      enddo""", "CG initial rho",
    )
    reduction(
        "!$omp do reduction(+:d)", "d",
        """         do j=1, lastcol-firstcol+1
            d = d + p(j)*q(j)
         enddo""", "CG p dot q",
    )
    reduction(
        "!$omp do reduction(+:rho)", "rho",
        ("""         do j=1, lastcol-firstcol+1
            z(j) = z(j) + alpha*p(j)
            r(j) = r(j) - alpha*q(j)
c         enddo
""" + "            \n" + """c---------------------------------------------------------------------
c  rho = r.r
c  Now, obtain the norm of r: First, sum squares of r elements locally...
c---------------------------------------------------------------------
c         do j=1, lastcol-firstcol+1
            rho = rho + r(j)*r(j)
         enddo"""), "CG updated rho",
    )
    reduction(
        "!$omp do reduction(+:sum)", "sum",
        ("""      do j=1, lastcol-firstcol+1
""" + "         suml = x(j) - r(j)         \n" + """         sum  = sum + suml*suml
      enddo"""), "CG residual norm", end_directive="!$omp end do nowait",
    )
    # Emit actual phase windows around the sparse matvec, dot/reduction, and
    # vector-update regions. Calls execute only on the OpenMP master thread.
    text = _replace_exact(
        text,
        "!$omp do\n         do j=1,lastrow-firstrow+1\n            suml = 0.d0",
        """!$omp master
         matched_id=101
         call matched_phase_begin(matched_id,matched_iter,matched_count)
!$omp end master
!$omp barrier
!$omp do
         do j=1,lastrow-firstrow+1
            suml = 0.d0""",
        "CG q spmv begin",
    )
    text = _replace_exact(
        text,
        "!$omp do\n      do j=1,lastrow-firstrow+1\n         suml = 0.d0",
        """!$omp master
      matched_id=101
      call matched_phase_begin(matched_id,matched_iter,matched_count)
!$omp end master
!$omp barrier
!$omp do
      do j=1,lastrow-firstrow+1
         suml = 0.d0""",
        "CG residual spmv begin",
    )
    text = _replace_exact(
        text,
        "            q(j) = suml\n         enddo\n!$omp end do",
        """            q(j) = suml
         enddo
!$omp end do
!$omp master
         call matched_phase_end(matched_id,matched_iter)
!$omp end master
!$omp barrier""",
        "CG q spmv end",
    )
    text = _replace_exact(
        text,
        """         do j=1,lastrow-firstrow+1
            suml = 0.d0
            do k=rowstr(j),rowstr(j+1)-1
               suml = suml + a(k)*p(colidx(k))
            enddo
            q(j) = suml
         enddo""",
        """         do j=1,lastrow-firstrow+1
            matched_work=j
            matched_ord=0
            matched_addr=1000000000000_8+4_8*(j-1)
            call matched_trace_load_u32(matched_work,matched_ord,
     >           matched_addr,rowstr(j))
            matched_ord=matched_ord+1
            matched_addr=1000000000000_8+4_8*j
            call matched_trace_load_u32(matched_work,matched_ord,
     >           matched_addr,rowstr(j+1))
            matched_ord=matched_ord+1
            suml = 0.d0
            do k=rowstr(j),rowstr(j+1)-1
               matched_addr=2000000000000_8+4_8*(k-1)
               call matched_trace_load_u32(matched_work,matched_ord,
     >              matched_addr,colidx(k))
               matched_ord=matched_ord+1
               matched_addr=3000000000000_8+8_8*(k-1)
               call matched_trace_load_f64(matched_work,matched_ord,
     >              matched_addr,a(k))
               matched_ord=matched_ord+1
               matched_addr=4000000000000_8+8_8*(colidx(k)-1)
               call matched_trace_load_f64(matched_work,matched_ord,
     >              matched_addr,p(colidx(k)))
               matched_ord=matched_ord+1
               matched_l=suml
               suml = suml + a(k)*p(colidx(k))
               matched_r=a(k)*p(colidx(k))
               matched_opc=18
               call matched_trace_binary_f64(matched_opc,matched_work,
     >              matched_ord,a(k),p(colidx(k)),matched_r)
               matched_ord=matched_ord+1
               matched_opc=12
               call matched_trace_binary_f64(matched_opc,matched_work,
     >              matched_ord,matched_l,matched_r,suml)
               matched_ord=matched_ord+1
            enddo
            q(j) = suml
            matched_addr=5000000000000_8+8_8*(j-1)
            call matched_trace_store_f64(matched_work,matched_ord,
     >           matched_addr,q(j))
         enddo""",
        "CG q primitive trace",
    )
    text = _replace_exact(
        text,
        "         r(j) = suml\n      enddo\n!$omp end do",
        """         r(j) = suml
      enddo
!$omp end do
!$omp master
      call matched_phase_end(matched_id,matched_iter)
!$omp end master
!$omp barrier""",
        "CG residual spmv end",
    )
    text = _replace_exact(
        text,
        """      do j=1,lastrow-firstrow+1
         suml = 0.d0
         do k=rowstr(j),rowstr(j+1)-1
            suml = suml + a(k)*z(colidx(k))
         enddo
         r(j) = suml
      enddo""",
        """      do j=1,lastrow-firstrow+1
         matched_work=j
         matched_ord=0
         matched_addr=1000000000000_8+4_8*(j-1)
         call matched_trace_load_u32(matched_work,matched_ord,
     >        matched_addr,rowstr(j))
         matched_ord=matched_ord+1
         matched_addr=1000000000000_8+4_8*j
         call matched_trace_load_u32(matched_work,matched_ord,
     >        matched_addr,rowstr(j+1))
         matched_ord=matched_ord+1
         suml = 0.d0
         do k=rowstr(j),rowstr(j+1)-1
            matched_addr=2000000000000_8+4_8*(k-1)
            call matched_trace_load_u32(matched_work,matched_ord,
     >           matched_addr,colidx(k))
            matched_ord=matched_ord+1
            matched_addr=3000000000000_8+8_8*(k-1)
            call matched_trace_load_f64(matched_work,matched_ord,
     >           matched_addr,a(k))
            matched_ord=matched_ord+1
            matched_addr=6000000000000_8+8_8*(colidx(k)-1)
            call matched_trace_load_f64(matched_work,matched_ord,
     >           matched_addr,z(colidx(k)))
            matched_ord=matched_ord+1
            matched_l=suml
            suml = suml + a(k)*z(colidx(k))
            matched_r=a(k)*z(colidx(k))
            matched_opc=18
            call matched_trace_binary_f64(matched_opc,matched_work,
     >           matched_ord,a(k),z(colidx(k)),matched_r)
            matched_ord=matched_ord+1
            matched_opc=12
            call matched_trace_binary_f64(matched_opc,matched_work,
     >           matched_ord,matched_l,matched_r,suml)
            matched_ord=matched_ord+1
         enddo
         r(j) = suml
         matched_addr=7000000000000_8+8_8*(j-1)
         call matched_trace_store_f64(matched_work,matched_ord,
     >        matched_addr,r(j))
      enddo""",
        "CG residual spmv primitive trace",
    )
    # The explicit reductions are dot phases; the z/r and p loops are vector
    # update phases. These markers are ordered by master/barrier boundaries.
    text = text.replace(
        "c MATCHED_TRACE_BEGIN CG p dot q\n      matched_part1=0.0d0",
        """c MATCHED_TRACE_BEGIN CG p dot q
!$omp master
         matched_id=103
         call matched_phase_begin(matched_id,matched_iter,matched_count)
!$omp end master
!$omp barrier
      matched_part1=0.0d0""",
        1,
    )
    text = text.replace(
        "c MATCHED_TRACE_BEGIN CG initial rho\n      matched_part1=0.0d0",
        """c MATCHED_TRACE_BEGIN CG initial rho
!$omp master
      matched_id=103
      call matched_phase_begin(matched_id,matched_iter,matched_count)
!$omp end master
!$omp barrier
      matched_part1=0.0d0""",
        1,
    )
    text = text.replace(
        "c MATCHED_TRACE_END CG initial rho",
        """!$omp master
      call matched_phase_end(matched_id,matched_iter)
!$omp end master
!$omp barrier
c MATCHED_TRACE_END CG initial rho""",
        1,
    )
    text = text.replace(
        "c MATCHED_TRACE_END CG p dot q",
        """!$omp master
         call matched_phase_end(matched_id,matched_iter)
!$omp end master
!$omp barrier
c MATCHED_TRACE_END CG p dot q""",
        1,
    )
    text = text.replace(
        "c MATCHED_TRACE_BEGIN CG updated rho\n      matched_part1=0.0d0",
        """c MATCHED_TRACE_BEGIN CG updated rho
!$omp master
         matched_id=102
         call matched_phase_begin(matched_id,matched_iter,matched_count)
!$omp end master
!$omp barrier
      matched_part1=0.0d0""",
        1,
    )
    text = text.replace(
        "c MATCHED_TRACE_END CG updated rho",
        """!$omp master
         call matched_phase_end(matched_id,matched_iter)
!$omp end master
!$omp barrier
c MATCHED_TRACE_END CG updated rho""",
        1,
    )
    text = text.replace(
        "c MATCHED_TRACE_BEGIN CG residual norm\n      matched_part1=0.0d0",
        """c MATCHED_TRACE_BEGIN CG residual norm
!$omp master
      matched_id=103
      call matched_phase_begin(matched_id,matched_iter,matched_count)
!$omp end master
!$omp barrier
      matched_part1=0.0d0""",
        1,
    )
    text = text.replace(
        "c MATCHED_TRACE_END CG residual norm",
        """!$omp master
      call matched_phase_end(matched_id,matched_iter)
!$omp end master
!$omp barrier
c MATCHED_TRACE_END CG residual norm""",
        1,
    )
    text = _replace_exact(
        text,
        """c---------------------------------------------------------------------
c  p = r + beta*p
c---------------------------------------------------------------------
!$omp do
         do j=1, lastcol-firstcol+1
            p(j) = r(j) + beta*p(j)
         enddo
!$omp end do""",
        """c---------------------------------------------------------------------
c  p = r + beta*p
c---------------------------------------------------------------------
!$omp master
         matched_id=102
         call matched_phase_begin(matched_id,matched_iter,matched_count)
!$omp end master
!$omp barrier
!$omp do
         do j=1, lastcol-firstcol+1
            matched_work=j
            matched_ord=0
            matched_addr=7000000000000_8+8_8*(j-1)
            call matched_trace_load_f64(matched_work,matched_ord,
     >           matched_addr,r(j))
            matched_ord=matched_ord+1
            matched_addr=4000000000000_8+8_8*(j-1)
            call matched_trace_load_f64(matched_work,matched_ord,
     >           matched_addr,p(j))
            matched_ord=matched_ord+1
            matched_r=beta*p(j)
            matched_opc=18
            call matched_trace_binary_f64(matched_opc,matched_work,
     >           matched_ord,beta,p(j),matched_r)
            matched_ord=matched_ord+1
            p(j) = r(j) + beta*p(j)
            matched_opc=12
            call matched_trace_binary_f64(matched_opc,matched_work,
     >           matched_ord,r(j),matched_r,p(j))
            matched_ord=matched_ord+1
            call matched_trace_store_f64(matched_work,matched_ord,
     >           matched_addr,p(j))
         enddo
!$omp end do
!$omp master
         matched_boundary_iter=matched_iter*100+cgit
         matched_id=111
         call matched_dump_f64(matched_id,matched_boundary_iter,
     >        z(1),matched_count)
         matched_id=112
         call matched_dump_f64(matched_id,matched_boundary_iter,
     >        p(1),matched_count)
         matched_id=113
         call matched_dump_f64(matched_id,matched_boundary_iter,
     >        q(1),matched_count)
         matched_id=114
         call matched_dump_f64(matched_id,matched_boundary_iter,
     >        r(1),matched_count)
         matched_id=102
         call matched_phase_end(matched_id,matched_iter)
!$omp end master
!$omp barrier""",
        "CG per-cgit vector boundaries",
    )
    text = _replace_exact(
        text,
        "!$omp end parallel\n\n      rnorm = sqrt( sum )",
        """!$omp end parallel

c MATCHED_TRACE_BEGIN conj_grad dumps
      matched_id=113
      matched_count=lastcol-firstcol+1
      call matched_dump_f64(matched_id,matched_iter,q(1),matched_count)
      matched_id=111
      call matched_dump_f64(matched_id,matched_iter,z(1),matched_count)
      matched_id=112
      call matched_dump_f64(matched_id,matched_iter,p(1),matched_count)
      matched_id=114
      call matched_dump_f64(matched_id,matched_iter,r(1),matched_count)
      matched_id=104
      call matched_phase_end(matched_id,matched_iter)
c MATCHED_TRACE_END conj_grad dumps

      rnorm = sqrt( sum )""",
        "CG final boundaries",
    )
    text = _replace_exact(
        text,
        "!$omp end parallel\n\n\n      enddo                              ! end of main iter inv pow meth",
        """!$omp end parallel

c MATCHED_TRACE_BEGIN measured outer boundaries
         matched_iter=it
         matched_count=lastcol-firstcol+1
         matched_id=110
         call matched_dump_f64(matched_id,matched_iter,
     >        x(1),matched_count)
         matched_count=1
         matched_id=115
         call matched_dump_f64(matched_id,matched_iter,
     >        rnorm,matched_count)
         matched_id=116
         call matched_dump_f64(matched_id,matched_iter,
     >        zeta,matched_count)
c MATCHED_TRACE_END measured outer boundaries

      enddo                              ! end of main iter inv pow meth""",
        "CG measured outer boundaries",
    )
    text = _replace_exact(
        text,
        "!$omp end parallel do\n\n      zeta  = 0.0d0\n\n      call timer_stop( T_init )",
        """!$omp end parallel do

      zeta  = 0.0d0

c MATCHED_TRACE_BEGIN bounded CG descriptor root
      matched_bits=32
      matched_count=lastrow-firstrow+2
      matched_base=1000000000000_8
      matched_id=1
      call matched_array_image_u32(matched_id,matched_bits,matched_base,
     >     rowstr(1),matched_count)
      matched_nnz=rowstr(lastrow-firstrow+2)-1
      matched_count=matched_nnz
      matched_base=2000000000000_8
      matched_id=2
      call matched_array_image_u32(matched_id,matched_bits,matched_base,
     >     colidx(1),matched_count)
      matched_bits=64
      matched_base=3000000000000_8
      matched_id=3
      call matched_array_image_f64(matched_id,matched_bits,matched_base,
     >     a(1),matched_count)
      matched_count=naa+1
      matched_base=4000000000000_8
      matched_id=4
      call matched_array_image_f64(matched_id,matched_bits,matched_base,
     >     p(1),matched_count)
      matched_base=5000000000000_8
      matched_id=5
      call matched_array_image_f64(matched_id,matched_bits,matched_base,
     >     q(1),matched_count)
      matched_base=6000000000000_8
      matched_id=6
      call matched_array_image_f64(matched_id,matched_bits,matched_base,
     >     z(1),matched_count)
      matched_base=7000000000000_8
      matched_id=7
      call matched_array_image_f64(matched_id,matched_bits,matched_base,
     >     r(1),matched_count)
      matched_base=8000000000000_8
      matched_id=8
      call matched_array_image_f64(matched_id,matched_bits,matched_base,
     >     x(1),matched_count)
      matched_params(1)=lastrow-firstrow+1
      matched_params(2)=lastcol-firstcol+1
      matched_params(3)=matched_nnz
      matched_params(4)=niter
      matched_params(5)=25
      matched_params(6)=firstrow
      matched_params(7)=lastrow
      matched_params(8)=firstcol
      matched_params(9)=lastcol
      matched_params(10)=transfer(shift,matched_params(10))
      matched_params(11)=naa+1
      matched_ordinal=0
      matched_phase=100
      matched_kernel=1000
      matched_iter=0
      matched_count=niter
      matched_param_count=11
      call matched_invocation(matched_ordinal,matched_phase,
     >     matched_kernel,matched_iter,matched_count,matched_params,
     >     matched_param_count)
c MATCHED_TRACE_END bounded CG descriptor root

      call timer_stop( T_init )""",
        "CG bounded descriptor root",
    )
    if any(token in text for token in (
        "reduction(+:rho)", "reduction(+:d)", "reduction(+:sum)",
    )):
        raise BuildError("CG patch left a forbidden floating reduction")
    return _upgrade_boundary_hooks(_strip_eager_primitive_calls(text))


def _transform_mg(text):
    text = _replace_exact(
        text,
        "      double precision tmax\n!$    integer  omp_get_max_threads",
        """      double precision tmax
      integer*8 matched_threads,matched_allocated_bytes,matched_workload
      integer*8 matched_bits,matched_base,matched_count,matched_id
      integer*8 matched_ordinal,matched_kernel,matched_phase
      integer*8 matched_iter
      integer*8 matched_params(16+4*maxlevel),matched_param_count
!$    integer  omp_get_max_threads""",
        "MG main thread declaration",
    )
    text = _replace_exact(
        text,
        " 1003 format(' Number of available threads: ', i5)\n\n\n      call resid",
        """ 1003 format(' Number of available threads: ', i5)
c MATCHED_TRACE_BEGIN runtime thread gate
      matched_threads=omp_get_max_threads()
      call matched_require_four_threads(matched_threads)
c MATCHED_TRACE_END runtime thread gate


      call resid""",
        "MG runtime thread gate",
    )
    text = _replace_exact(
        text,
        "      call alloc_space\n\n      call setup(n1,n2,n3,k)",
        """      call alloc_space
c MATCHED_TRACE_BEGIN allocation probe
      matched_allocated_bytes=8_8*(size(u,kind=8)+size(v,kind=8)
     >     +size(r,kind=8))
      matched_workload=2
      call matched_allocation_probe(matched_workload,
     >     matched_allocated_bytes)
c MATCHED_TRACE_END allocation probe

      call setup(n1,n2,n3,k)""",
        "MG allocation probe",
    )
    phases = {
        "psinv": (201, "mg_psinv", "u", "n1*n2*n3"),
        "resid": (202, "mg_resid", "r", "n1*n2*n3"),
        "rprj3": (203, "mg_rprj3", "s", "m1j*m2j*m3j"),
        "interp": (204, "mg_interp", "u", "n1*n2*n3"),
    }
    for routine, (phase_id, phase_name, array, count) in phases.items():
        declaration_anchor = {
            "psinv": "      integer i3, i2, i1",
            "resid": "      integer i3, i2, i1",
            "rprj3": "      integer j3, j2, j1, i3, i2, i1, d1, d2, d3, j",
            "interp": "      integer i3, i2, i1, d1, d2, d3, t1, t2, t3",
        }[routine]
        declaration = declaration_anchor + f"""
c MATCHED_TRACE_BEGIN {routine} declarations
      integer*8 matched_id,matched_iter,matched_count,matched_call
      save matched_call
      data matched_call /0/
c MATCHED_PHASE {phase_name}
c MATCHED_TRACE_END {routine} declarations"""
        # resid and psinv share the same declaration anchor, so restrict the
        # edit to the named subroutine body.
        start = text.index(f"      subroutine {routine}")
        end = text.index("\n      end\n", start) + len("\n      end")
        section = text[start:end]
        section = _replace_exact(
            section, declaration_anchor, declaration,
            f"MG {routine} declarations",
        )
        timer_anchor = f"      if (timeron) call timer_start(T_{routine})"
        begin = f"""{timer_anchor}
c MATCHED_TRACE_BEGIN {routine} begin
      matched_id={phase_id}
      matched_iter=matched_call*100+k
      matched_call=matched_call+1
      matched_count={count}
      call matched_phase_begin(matched_id,matched_iter,matched_count)
c MATCHED_TRACE_END {routine} begin"""
        section = _replace_exact(
            section, timer_anchor, begin, f"MG {routine} begin"
        )
        return_anchor = (
            "      return \n      end" if routine == "interp"
            else "      return\n      end"
        )
        finish = f"""c MATCHED_TRACE_BEGIN {routine} boundary
      call matched_dump_f64(matched_id,matched_iter,
     >     {array}(1,1,1),matched_count)
      call matched_phase_end(matched_id,matched_iter)
c MATCHED_TRACE_END {routine} boundary
      return
      end"""
        section = _replace_exact(
            section, return_anchor, finish, f"MG {routine} boundary"
        )
        text = text[:start] + section + text[end:]

    norm_decl = """      double precision s, a
      integer i3, i2, i1"""
    norm_decl_new = """      double precision s, a
      integer i3, i2, i1
c MATCHED_TRACE_BEGIN norm2u3 declarations
      double precision matched_sum(0:3),matched_max(0:3)
      double precision matched_s,matched_a,matched_reduce_sum4
      double precision matched_reduce_max4
      integer matched_tid,omp_get_thread_num,omp_get_num_threads
      integer*8 matched_id,matched_iter,matched_count,matched_threads
      integer*8 matched_call
      save matched_call
      data matched_call /0/
      external matched_reduce_sum4,matched_reduce_max4
      external omp_get_thread_num,omp_get_num_threads
c MATCHED_PHASE mg_norm2u3
c MATCHED_THREADS 4
c MATCHED_FIXED_REDUCTION_TREE
c MATCHED_TRACE_END norm2u3 declarations"""
    text = _replace_exact(text, norm_decl, norm_decl_new, "MG norm declarations")
    old_norm = """      s=0.0D0
      rnmu = 0.0D0
!$omp parallel do default(shared) private(i1,i2,i3,a) collapse(2)
!$omp& reduction(+:s) reduction(max:rnmu)
      do  i3=2,n3-1
         do  i2=2,n2-1
            do  i1=2,n1-1
               s=s+r(i1,i2,i3)**2
               a=abs(r(i1,i2,i3))
               rnmu=dmax1(rnmu,a)
            enddo
         enddo
      enddo"""
    new_norm = """c MATCHED_TRACE_BEGIN norm2u3 fixed four-lane reduction
      matched_id=205
      matched_iter=matched_call
      matched_call=matched_call+1
      matched_count=n1*n2*n3
      call matched_phase_begin(matched_id,matched_iter,matched_count)
!$omp parallel default(shared)
!$omp& private(i1,i2,i3,a,matched_a,matched_s,matched_tid)
      matched_tid=omp_get_thread_num()
!$omp master
      matched_threads=omp_get_num_threads()
      call matched_require_four_threads(matched_threads)
!$omp end master
!$omp barrier
      matched_s=0.0d0
      matched_a=0.0d0
!$omp do schedule(static) collapse(2)
      do  i3=2,n3-1
         do  i2=2,n2-1
            do  i1=2,n1-1
               matched_s=matched_s+r(i1,i2,i3)**2
               a=abs(r(i1,i2,i3))
               matched_a=dmax1(matched_a,a)
            enddo
         enddo
      enddo
!$omp end do
      matched_sum(matched_tid)=matched_s
      matched_max(matched_tid)=matched_a
!$omp barrier
!$omp master
      s=matched_reduce_sum4(matched_sum)
      rnmu=matched_reduce_max4(matched_max)
!$omp end master
!$omp end parallel
c MATCHED_TRACE_END norm2u3 fixed four-lane reduction"""
    text = _replace_exact(text, old_norm, new_norm, "MG norm reduction")
    text = _replace_exact(
        text,
        "      rnm2=sqrt( s / dn )",
        """      rnm2=sqrt( s / dn )
c MATCHED_TRACE_BEGIN norm2u3 boundary
      matched_count=1
      call matched_dump_f64(matched_id,matched_iter,rnm2,matched_count)
      matched_id=206
      call matched_dump_f64(matched_id,matched_iter,rnmu,matched_count)
      matched_id=205
      call matched_phase_end(matched_id,matched_iter)
c MATCHED_TRACE_END norm2u3 boundary""",
        "MG norm boundary",
    )
    text = _replace_exact(
        text,
        "      call zran3(v,n1,n2,n3,nx(lt),ny(lt),k)\n\n      call timer_stop(T_init)",
        """      call zran3(v,n1,n2,n3,nx(lt),ny(lt),k)

c MATCHED_TRACE_BEGIN bounded MG descriptor root
      matched_bits=64
      matched_base=10000000000000_8
      matched_count=size(u,kind=8)
      matched_id=21
      call matched_array_image_f64(matched_id,matched_bits,matched_base,
     >     u(1),matched_count)
      matched_base=20000000000000_8
      matched_count=size(v,kind=8)
      matched_id=22
      call matched_array_image_f64(matched_id,matched_bits,matched_base,
     >     v(1),matched_count)
      matched_base=30000000000000_8
      matched_count=size(r,kind=8)
      matched_id=23
      call matched_array_image_f64(matched_id,matched_bits,matched_base,
     >     r(1),matched_count)
      matched_params(1)=lt
      matched_params(2)=nit
      matched_params(3)=n1
      matched_params(4)=n2
      matched_params(5)=n3
      matched_params(6)=size(u,kind=8)
      matched_params(7)=size(v,kind=8)
      matched_params(8)=size(r,kind=8)
      do i=0,3
         matched_params(9+i)=transfer(a(i),matched_params(9+i))
         matched_params(13+i)=transfer(c(i),matched_params(13+i))
      enddo
      do i=1,lt
         matched_params(16+4*(i-1)+1)=m1(i)
         matched_params(16+4*(i-1)+2)=m2(i)
         matched_params(16+4*(i-1)+3)=m3(i)
         matched_params(16+4*(i-1)+4)=ir(i)
      enddo
      matched_ordinal=0
      matched_phase=200
      matched_kernel=2000
      matched_iter=0
      matched_count=nit
      matched_param_count=16+4*lt
      call matched_invocation(matched_ordinal,matched_phase,
     >     matched_kernel,matched_iter,matched_count,matched_params,
     >     matched_param_count)
c MATCHED_TRACE_END bounded MG descriptor root

      call timer_stop(T_init)""",
        "MG bounded descriptor root",
    )
    if "reduction(+:s)" in text or "reduction(max:rnmu)" in text:
        raise BuildError("MG patch left a forbidden floating reduction")
    return _upgrade_boundary_hooks(text)


def arithmetic_fingerprint(text):
    """Hash every NPB arithmetic line after exact lane-name normalization."""
    normalized = []
    skip_allocation_probe = False
    skip_descriptor_root = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped == "c MATCHED_TRACE_BEGIN allocation probe":
            skip_allocation_probe = True
            continue
        if stripped == "c MATCHED_TRACE_END allocation probe":
            skip_allocation_probe = False
            continue
        if skip_allocation_probe:
            continue
        if stripped.startswith("c MATCHED_TRACE_BEGIN bounded "):
            skip_descriptor_root = True
            continue
        if stripped.startswith("c MATCHED_TRACE_END bounded "):
            skip_descriptor_root = False
            continue
        if skip_descriptor_root:
            continue
        if not stripped or stripped.lower().startswith(("c", "!$omp")):
            continue
        if stripped.startswith("external omp_get_thread_num"):
            continue
        if "matched_" in stripped:
            if "=" not in stripped or "matched_reduce_" in stripped:
                continue
            if re.match(
                r"matched_(work|ord|addr|opc|l|r|res)\s*=", stripped
            ):
                # These assignments only construct primitive-trace metadata
                # or preserve already-computed operands for raw-bit logging.
                continue
            if re.fullmatch(
                r"matched_part[12]\s*=\s*0\.0d0", stripped
            ):
                continue
            mapped = stripped
            if "matched_part1" in mapped:
                original = None
                for token, candidate in (
                    ("x(j)*z(j)", "norm_temp1"),
                    ("p(j)*q(j)", "d"),
                    ("suml*suml", "sum"),
                    ("r(j)*r(j)", "rho"),
                ):
                    if token in mapped:
                        original = candidate
                        break
                if original is None:
                    # Per-lane zeroing and merge storage are instrumentation;
                    # the original scalar initialization remains elsewhere.
                    continue
                mapped = mapped.replace("matched_part1", original)
            mapped = mapped.replace("matched_part2", "norm_temp2")
            mapped = mapped.replace("matched_s", "s")
            mapped = mapped.replace("matched_a", "rnmu")
            if "matched_" in mapped:
                continue
            stripped = mapped
        normalized.append(re.sub(r"\s+", "", stripped).lower())
    return hashlib.sha256("\n".join(normalized).encode("utf-8")).hexdigest()


def inspect_npb_patch(text, workload):
    expected = {
        "cg": {"cg_spmv", "cg_vector_update", "cg_dot", "cg_conj_grad"},
        "mg": {"mg_resid", "mg_rprj3", "mg_interp", "mg_psinv", "mg_norm2u3"},
    }
    phases = set(re.findall(r"(?m)^c MATCHED_PHASE ([a-z0-9_]+)$", text))
    if workload not in expected or phases != expected[workload]:
        raise BuildError(f"NPB {workload} phase markers are incomplete")
    return {
        "threads": 4 if "c MATCHED_THREADS 4" in text else None,
        "runtime_thread_guard": "call matched_require_four_threads" in text,
        "fixed_reduction_tree": "c MATCHED_FIXED_REDUCTION_TREE" in text,
        "phases": phases,
    }


def _transform_npb_source(source_root, workload, destination_root):
    del source_root
    command = [
        "patch", "--batch", "--forward", "--fuzz=0", "-p1",
        "-i", str(NPB_PATCHES[workload]),
    ]
    completed = subprocess.run(
        command, cwd=destination_root, text=True, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, check=False,
    )
    if completed.returncode != 0:
        raise BuildError(
            f"NPB {workload} zero-fuzz patch failed:\n{completed.stdout}"
        )
    source = destination_root / workload.upper() / f"{workload}.f"
    transformed = source.read_text(encoding="utf-8")
    inspect_npb_patch(transformed, workload)
    return source


def apply_npb_patch(source_root, workload, destination_root):
    """Copy and exact-anchor patch the pinned NPB source with no fallback."""
    source_root = Path(source_root).resolve()
    destination_root = Path(destination_root).resolve()
    if workload not in NPB_PATCHES:
        raise BuildError(f"unknown NPB workload: {workload}")
    if destination_root.exists():
        raise BuildError(f"fresh NPB patch root required: {destination_root}")
    if not NPB_PATCHES[workload].is_file():
        raise BuildError(f"NPB patch manifest is missing: {workload}")
    shutil.copytree(source_root, destination_root)
    _transform_npb_source(source_root, workload, destination_root)
    return destination_root


def _git_root(path):
    candidate = Path(path).resolve()
    while candidate != candidate.parent:
        if (candidate / ".git").exists():
            return candidate
        candidate = candidate.parent
    raise BuildError(f"NPB source has no enclosing git repository: {path}")


def _git_read(repo, *arguments):
    completed = subprocess.run(
        ["git", "-c", f"safe.directory={repo}", "-C", str(repo), *arguments],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        check=False,
    )
    if completed.returncode != 0:
        raise BuildError(f"NPB git inspection failed: {completed.stdout.strip()}")
    return completed.stdout


def validate_npb_formal_source_identity(
    source_root, *, expected_commit, parameter_files,
    expected_parameter_hashes, allocated_bytes,
):
    """Bind clean source, parameters, and declared paper allocation."""
    source_root = Path(source_root).resolve()
    repo = _git_root(source_root)
    status = _git_read(repo, "status", "--porcelain", "--untracked-files=all")
    if status.strip():
        raise BuildError("formal NPB source tree is dirty")
    commit = _git_read(repo, "rev-parse", "HEAD").strip()
    if commit != expected_commit:
        raise BuildError(f"formal NPB commit {commit} != {expected_commit}")
    for workload in ("cg", "mg"):
        parameter = Path(parameter_files[workload]).resolve()
        if not parameter.is_file():
            raise BuildError(f"formal NPB {workload} parameter file is missing")
        try:
            parameter.relative_to(source_root)
        except ValueError as error:
            raise BuildError(
                f"formal NPB {workload} parameter file is outside source root"
            ) from error
        expected_hash = expected_parameter_hashes.get(workload)
        if _sha256_file(parameter) != expected_hash:
            raise BuildError(f"formal NPB {workload} parameter hash differs")
        value = allocated_bytes.get(workload)
        if (
            not isinstance(value, int) or isinstance(value, bool)
            or value < 12_800_000_000
        ):
            raise BuildError(
                f"formal NPB {workload} allocation is below 12.8 GB"
            )
    return True


def validate_npb_formal_source(
    source_root, *, expected_commit, parameter_files,
    expected_parameter_hashes, allocated_bytes, measured_allocated_bytes=None,
):
    validate_npb_formal_source_identity(
        source_root,
        expected_commit=expected_commit,
        parameter_files=parameter_files,
        expected_parameter_hashes=expected_parameter_hashes,
        allocated_bytes=allocated_bytes,
    )
    for workload in ("cg", "mg"):
        value = allocated_bytes[workload]
        if measured_allocated_bytes is None or workload not in measured_allocated_bytes:
            raise BuildError(
                f"formal NPB {workload} allocation probe is missing"
            )
        measured = measured_allocated_bytes[workload]
        if measured != value:
            raise BuildError(
                f"formal NPB {workload} allocation probe {measured} "
                f"!= inputs.json {value}"
            )
    return True


def compare_npb_raw_boundaries(reference, actual):
    expected = _read_words(reference, 64)
    observed = _read_words(actual, 64)
    canonical.compare_words(expected, observed, "NPB boundary word", word_bits=64)
    return True


def _npb_boundary_count(path):
    return len(_npb_boundary_records(path))


def _npb_boundary_records(path):
    words = _read_words(path, 64)
    records = []
    cursor = 0
    while cursor < len(words):
        if len(words) - cursor < 4:
            raise BuildError("NPB boundary header is truncated")
        magic, _boundary, _iteration, length = words[cursor:cursor + 4]
        if magic != 0x4e5042424e443031:
            raise BuildError(f"NPB boundary {len(records)} has invalid magic")
        cursor += 4
        if length > len(words) - cursor:
            raise BuildError(
                f"NPB boundary {len(records)} payload is truncated"
            )
        records.append({
            "boundary": _boundary,
            "iteration": _iteration,
            "count": length,
        })
        cursor += length
    return records


def _npb_allocation_probe(path, expected_workload):
    words = _read_words(path, 64)
    if len(words) != 3:
        raise BuildError("NPB allocation probe record count differs")
    magic, workload, allocated_bytes = words
    if magic != 0x4e5042414c4c3031:
        raise BuildError("NPB allocation probe magic differs")
    expected_id = {"cg": 1, "mg": 2}[expected_workload]
    if workload != expected_id or allocated_bytes == 0:
        raise BuildError("NPB allocation probe workload or bytes differ")
    return allocated_bytes


_NPB_ARRAY_MAGIC = 0x4e50424152593032
_NPB_INVOCATION_MAGIC = 0x4e5042494e563032
_NPB_BOUNDARY_MAGIC = 0x4e50425348413032


def _parse_npb_capture(path):
    """Parse and authenticate the bounded native NPB capture stream."""
    path = Path(path).resolve()
    try:
        payload = path.read_bytes()
    except OSError as error:
        raise BuildError(f"cannot read NPB capture: {error}") from error
    cursor = 0
    arrays = {}
    invocations = []
    boundaries = []

    def take(size, label):
        nonlocal cursor
        if not isinstance(size, int) or size < 0 or size > len(payload) - cursor:
            raise BuildError(f"NPB capture {label} is truncated")
        result = payload[cursor:cursor + size]
        cursor += size
        return result

    def word(label):
        return struct.unpack("<Q", take(8, label))[0]

    while cursor < len(payload):
        magic = word("record magic")
        if magic == _NPB_ARRAY_MAGIC:
            array_id = word("array id")
            element_bits = word("array element bits")
            logical_base = word("array logical base")
            count = word("array count")
            if (
                array_id == 0 or array_id in arrays
                or element_bits not in (32, 64) or logical_base == 0
                or count == 0
            ):
                raise BuildError("NPB capture array metadata is invalid")
            digest = take(32, "array SHA-256").hex()
            if count > (1 << 63) or count * (element_bits // 8) > len(payload):
                raise BuildError("NPB capture array byte count is invalid")
            data = take(count * (element_bits // 8), "array payload")
            if hashlib.sha256(data).hexdigest() != digest:
                raise BuildError("NPB capture array SHA-256 differs")
            arrays[array_id] = {
                "id": array_id,
                "element_bits": element_bits,
                "logical_base": logical_base,
                "count": count,
                "sha256": digest,
                "payload": data,
            }
        elif magic == _NPB_INVOCATION_MAGIC:
            ordinal = word("invocation ordinal")
            phase = word("invocation phase")
            kernel = word("invocation kernel")
            iteration = word("invocation iteration")
            work_items = word("invocation work items")
            parameter_count = word("invocation parameter count")
            if (
                ordinal != len(invocations) or phase > 0xffff or kernel == 0
                or parameter_count > 4096
            ):
                raise BuildError("NPB capture invocation metadata is invalid")
            parameters = tuple(
                struct.unpack(f"<{parameter_count}q", take(
                    parameter_count * 8, "invocation parameters"
                ))
            ) if parameter_count else ()
            invocations.append({
                "ordinal": ordinal, "phase": phase, "kernel": kernel,
                "iteration": iteration, "work_items": work_items,
                "parameters": parameters,
            })
        elif magic == _NPB_BOUNDARY_MAGIC:
            boundary = word("boundary id")
            iteration = word("boundary iteration")
            element_bits = word("boundary element bits")
            count = word("boundary count")
            if element_bits not in (32, 64) or count == 0:
                raise BuildError("NPB capture boundary metadata is invalid")
            boundaries.append({
                "boundary": boundary, "iteration": iteration,
                "element_bits": element_bits, "count": count,
                "sha256": take(32, "boundary SHA-256").hex(),
            })
        else:
            raise BuildError(f"NPB capture record has unknown magic 0x{magic:x}")
    if not arrays or not invocations or not boundaries:
        raise BuildError("NPB capture record classes are incomplete")
    return {
        "arrays": arrays,
        "invocations": tuple(invocations),
        "boundaries": tuple(boundaries),
        "capture_sha256": hashlib.sha256(payload).hexdigest(),
    }


def _canonical_lanes(count):
    return [[count * lane // 4, count * (lane + 1) // 4]
            for lane in range(4)]


def _cg_invocation_descriptors(capture):
    records = capture["invocations"]
    if not records or records[0]["kernel"] != 1000:
        raise BuildError("NPB CG capture has no configuration root")
    config = records[0]["parameters"]
    if len(config) != 11:
        raise BuildError("NPB CG configuration parameter count differs")
    rows, columns, nonzeros, niter, cgitmax = config[:5]
    firstrow, lastrow, firstcol, lastcol, shift_signed = config[5:10]
    init_count = config[10]
    if (
        rows <= 0 or columns <= 0 or nonzeros <= 0 or niter <= 0
        or cgitmax <= 0 or rows != lastrow - firstrow + 1
        or columns != lastcol - firstcol + 1 or init_count != rows + 1
    ):
        raise BuildError("NPB CG configuration values are inconsistent")
    arrays = capture["arrays"]
    expected_arrays = {
        1: (rows + 1, 32), 2: (nonzeros, 32), 3: (nonzeros, 64),
        4: (init_count, 64), 5: (init_count, 64),
        6: (init_count, 64), 7: (init_count, 64), 8: (init_count, 64),
    }
    if set(arrays) != set(expected_arrays):
        raise BuildError("NPB CG captured array ids differ")
    for array_id, expected in expected_arrays.items():
        observed = arrays[array_id]
        if (observed["count"], observed["element_bits"]) != expected:
            raise BuildError(f"NPB CG array {array_id} shape differs")

    expected_events = []
    for outer in range(1, niter + 1):
        phases = [104, 103]
        phases.extend([101, 103, 102, 102] * cgitmax)
        phases.extend([101, 103, 103])
        expected_events.extend((phase, outer, columns) for phase in phases)
    observed_events = [
        (record["phase"], record["iteration"], record["work_items"])
        for record in records[1:]
    ]
    if observed_events != expected_events or any(
        record["kernel"] != record["phase"] or record["parameters"]
        for record in records[1:]
    ):
        raise BuildError("NPB CG captured invocation sequence differs")

    invocations = []
    primitive_records = 0

    def add(phase, kernel, iteration, work_items, parameters, count):
        nonlocal primitive_records
        invocations.append(lazy.Invocation(
            len(invocations), phase, kernel, iteration,
            work_items, parameters,
        ))
        primitive_records += count

    lanes = _canonical_lanes(columns)
    spmv_count = 3 * rows + 5 * nonzeros + 2
    for outer in range(1, niter + 1):
        add(104, "npb_cg_init", outer * 100, init_count, {
            "x": "x", "q": "q", "z": "z", "r": "r", "p": "p",
            "boundaries": [],
        }, 6 * init_count + 2)
        add(103, "npb_cg_dot", outer * 100, columns, {
            "left": "r", "right": "r", "result": "rho",
            "lanes": lanes,
        }, 4 * columns + 6)
        for cgit in range(1, cgitmax + 1):
            iteration = outer * 100 + cgit
            add(104, "npb_cg_prepare_iteration", iteration, 1, {
                "source": "rho", "snapshot": "rho0",
                "zero": ["d", "rho"], "results": ["rho0", "d", "rho"],
            }, 8)
            add(101, "npb_cg_spmv", iteration, rows, {
                "rowstr": "rowstr", "colidx": "colidx", "values": "a",
                "source": "p", "destination": "q", "row_count": rows,
                "edge_base": 1, "column_base": 1,
                "destination_count": rows,
            }, spmv_count)
            add(103, "npb_cg_dot", iteration, columns, {
                "left": "p", "right": "q", "result": "d",
                "lanes": lanes,
            }, 4 * columns + 6)
            add(103, "npb_cg_divide", iteration * 10 + 1, 1, {
                "numerator": "rho0", "denominator": "d",
                "result": "alpha",
            }, 4)
            add(102, "npb_cg_update_zr", iteration, columns, {
                "z": "z", "p": "p", "r": "r", "q": "q",
                "alpha": "alpha", "result": "rho",
                "boundaries": ["z", "r"],
                "boundary_counts": {"z": columns, "r": columns},
                "lanes": lanes,
            }, 12 * columns + 6)
            add(102, "npb_cg_divide", iteration * 10 + 2, 1, {
                "numerator": "rho", "denominator": "rho0",
                "result": "beta",
            }, 4)
            add(102, "npb_cg_update_p", iteration, columns, {
                "r": "r", "p": "p", "beta": "beta",
                "boundaries": ["p", "q"],
                "boundary_counts": {"p": columns, "q": columns},
            }, 5 * columns + 2)
        tail = outer * 100 + cgitmax + 1
        add(101, "npb_cg_spmv", tail, rows, {
            "rowstr": "rowstr", "colidx": "colidx", "values": "a",
            "source": "z", "destination": "r", "row_count": rows,
            "edge_base": 1, "column_base": 1,
            "destination_count": rows,
        }, spmv_count)
        add(103, "npb_cg_residual_norm", tail, columns, {
            "x": "x", "r": "r", "result": "rnorm", "lanes": lanes,
        }, 5 * columns + 7)
        add(103, "npb_cg_outer_dots", tail + 1, columns, {
            "x": "x", "z": "z", "result_xz": "norm1",
            "result_zz": "norm2", "results": ["norm1", "norm2"],
            "lanes": lanes,
        }, 8 * columns + 10)
        add(102, "npb_cg_normalize", outer, columns, {
            "z": "z", "x": "x", "norm1": "norm1", "norm2": "norm2",
            "norm3": "norm3", "shift": "shift", "zeta": "zeta",
            "write_zeta": 1, "boundaries": ["x"],
            "boundary_counts": {"x": columns},
            "results": ["norm3", "zeta", "rnorm"],
        }, 3 * columns + 8)
    return tuple(invocations), primitive_records, {
        "shift": shift_signed & ((1 << 64) - 1),
    }


def _mg_comm3_records(dimensions):
    n1, n2, n3 = dimensions
    copies = (
        2 * (n3 - 2) * (n2 - 2)
        + 2 * (n3 - 2) * n1
        + 2 * n2 * n1
    )
    return 2 * copies


def _mg_resid_records(dimensions):
    n1, n2, n3 = dimensions
    rows = (n2 - 2) * (n3 - 2)
    interior = (n1 - 2) * rows
    return 2 + 14 * n1 * rows + 12 * interior + _mg_comm3_records(dimensions)


def _mg_psinv_records(dimensions):
    n1, n2, n3 = dimensions
    rows = (n2 - 2) * (n3 - 2)
    interior = (n1 - 2) * rows
    return 2 + 14 * n1 * rows + 15 * interior + _mg_comm3_records(dimensions)


def _mg_rprj_records(fine_dimensions, coarse_dimensions):
    del fine_dimensions
    m1, m2, m3 = coarse_dimensions
    rows = (m2 - 2) * (m3 - 2)
    return (
        2 + rows * (14 * (m1 - 1) + 30 * (m1 - 2))
        + _mg_comm3_records(coarse_dimensions)
    )


def _mg_interp_degenerate_records(coarse_dimensions, fine_dimensions):
    mm1, mm2, mm3 = coarse_dimensions
    n1, n2, n3 = fine_dimensions
    d1, t1 = (2, 1) if n1 == 3 else (1, 0)
    d2, t2 = (2, 1) if n2 == 3 else (1, 0)
    d3, t3 = (2, 1) if n3 == 3 else (1, 0)
    del t1, t2, t3

    def emitted(outer_a, outer_b, inner_a, inner_b):
        # One-source updates cost 4 records; two-source updates cost 7.
        return outer_a * outer_b * (4 * inner_a + 7 * inner_b)

    count = emitted(mm3 - d3, mm2 - d2, mm1 - d1, mm1 - 1)
    # The remaining three source blocks use 2/4, 2/4, and 4/8 sources.
    count += (mm3 - d3) * (mm2 - 1) * (
        7 * (mm1 - d1) + 11 * (mm1 - 1)
    )
    count += (mm3 - 1) * (mm2 - d2) * (
        7 * (mm1 - d1) + 11 * (mm1 - 1)
    )
    count += (mm3 - 1) * (mm2 - 1) * (
        11 * (mm1 - d1) + 19 * (mm1 - 1)
    )
    return count + 2


def _mg_interp_records(coarse_dimensions, fine_dimensions):
    if 3 in fine_dimensions:
        return _mg_interp_degenerate_records(
            coarse_dimensions, fine_dimensions
        )
    mm1, mm2, mm3 = coarse_dimensions
    rows = (mm2 - 1) * (mm3 - 1)
    return 2 + rows * (10 * mm1 + 38 * (mm1 - 1))


def _mg_invocation_descriptors(capture):
    records = capture["invocations"]
    if not records or records[0]["kernel"] != 2000:
        raise BuildError("NPB MG capture has no configuration root")
    config = records[0]["parameters"]
    if len(config) < 20:
        raise BuildError("NPB MG configuration is truncated")
    lt, nit, n1, n2, n3, u_count, v_count, r_count = config[:8]
    if lt <= 0 or nit <= 0 or len(config) != 16 + 4 * lt:
        raise BuildError("NPB MG configuration parameter count differs")
    arrays = capture["arrays"]
    expected_arrays = {21: u_count, 22: v_count, 23: r_count}
    if set(arrays) != set(expected_arrays):
        raise BuildError("NPB MG captured array ids differ")
    for array_id, count in expected_arrays.items():
        record = arrays[array_id]
        if record["count"] != count or record["element_bits"] != 64:
            raise BuildError(f"NPB MG array {array_id} shape differs")
    a_raw = [value & ((1 << 64) - 1) for value in config[8:12]]
    c_raw = [value & ((1 << 64) - 1) for value in config[12:16]]
    levels = {}
    for level in range(1, lt + 1):
        start = 16 + 4 * (level - 1)
        m1, m2, m3, ir = config[start:start + 4]
        if min(m1, m2, m3, ir) <= 0:
            raise BuildError("NPB MG level metadata is invalid")
        levels[level] = ((m1, m2, m3), ir - 1)
    if levels[lt][0] != (n1, n2, n3):
        raise BuildError("NPB MG finest dimensions differ")
    for array_id, count in ((21, u_count), (23, r_count)):
        intervals = []
        for dimensions, offset in levels.values():
            end = offset + dimensions[0] * dimensions[1] * dimensions[2]
            if end > count:
                raise BuildError(f"NPB MG array {array_id} level exceeds image")
            intervals.append((offset, end))
        intervals.sort()
        if any(left[1] > right[0] for left, right in zip(intervals, intervals[1:])):
            raise BuildError("NPB MG level images overlap")

    semantic_events = [(202, lt), (205, lt)]
    for _outer in range(1, nit + 1):
        semantic_events.extend((203, level) for level in range(lt, 1, -1))
        semantic_events.append((201, 1))
        for level in range(2, lt):
            semantic_events.extend(((204, level), (202, level), (201, level)))
        semantic_events.extend(((204, lt), (202, lt), (201, lt)))
        semantic_events.append((202, lt))
    semantic_events.append((205, lt))
    if len(records) - 1 != len(semantic_events):
        raise BuildError("NPB MG captured invocation count differs")

    previous_calls = {}
    invocations = []
    primitive_records = 0

    def add(record, kernel, work_items, parameters, count):
        nonlocal primitive_records
        invocations.append(lazy.Invocation(
            len(invocations), record["phase"], kernel,
            record["iteration"], work_items, parameters,
        ))
        primitive_records += count

    for record, (expected_phase, level) in zip(records[1:], semantic_events):
        if (
            record["phase"] != expected_phase
            or record["kernel"] != expected_phase
            or record["parameters"]
        ):
            raise BuildError("NPB MG captured invocation sequence differs")
        if expected_phase == 205:
            call = record["iteration"]
        else:
            call, observed_level = divmod(record["iteration"], 100)
            if observed_level != level:
                raise BuildError("NPB MG captured invocation level differs")
        if expected_phase in previous_calls and call != previous_calls[expected_phase] + 1:
            raise BuildError("NPB MG routine call counter is not contiguous")
        previous_calls[expected_phase] = call
        dimensions = levels[level][0]
        full_items = dimensions[0] * dimensions[1] * dimensions[2]
        captured_items = full_items
        if expected_phase == 203:
            coarse_capture = levels[level - 1][0]
            captured_items = (
                coarse_capture[0] * coarse_capture[1] * coarse_capture[2]
            )
        if record["work_items"] != captured_items:
            raise BuildError("NPB MG captured routine work count differs")
        interior = ((dimensions[0] - 2) * (dimensions[1] - 2)
                    * (dimensions[2] - 2))
        if (
            (expected_phase == 201 and level == 1)
            or (expected_phase == 204 and level < lt)
        ):
            add(record, "npb_mg_zero3", full_items, {
                "u": f"u.l{level}", "n1": dimensions[0],
                "n2": dimensions[1], "n3": dimensions[2],
                "boundaries": [],
            }, full_items + 2)
        if expected_phase == 202:
            v_name = "v" if level == lt else f"r.l{level}"
            add(record, "npb_mg_resid", interior, {
                "u": f"u.l{level}", "v": v_name, "r": f"r.l{level}",
                "n1": dimensions[0], "n2": dimensions[1],
                "n3": dimensions[2], "a_raw": a_raw,
                "boundaries": [f"r.l{level}"],
            }, _mg_resid_records(dimensions))
        elif expected_phase == 201:
            add(record, "npb_mg_psinv", interior, {
                "r": f"r.l{level}", "u": f"u.l{level}",
                "n1": dimensions[0], "n2": dimensions[1],
                "n3": dimensions[2], "c_raw": c_raw,
                "boundaries": [f"u.l{level}"],
            }, _mg_psinv_records(dimensions))
        elif expected_phase == 203:
            coarse = levels[level - 1][0]
            coarse_items = ((coarse[0] - 2) * (coarse[1] - 2)
                            * (coarse[2] - 2))
            add(record, "npb_mg_rprj3", coarse_items, {
                "r": f"r.l{level}", "s": f"r.l{level - 1}",
                "m1k": dimensions[0], "m2k": dimensions[1],
                "m3k": dimensions[2], "m1j": coarse[0],
                "m2j": coarse[1], "m3j": coarse[2],
                "boundaries": [f"r.l{level - 1}"],
            }, _mg_rprj_records(dimensions, coarse))
        elif expected_phase == 204:
            coarse = levels[level - 1][0]
            add(record, "npb_mg_interp", full_items, {
                "z": f"u.l{level - 1}", "u": f"u.l{level}",
                "mm1": coarse[0], "mm2": coarse[1], "mm3": coarse[2],
                "n1": dimensions[0], "n2": dimensions[1],
                "n3": dimensions[2], "boundaries": [f"u.l{level}"],
            }, _mg_interp_records(coarse, dimensions))
        else:
            add(record, "npb_mg_norm2u3", interior, {
                "r": f"r.l{level}", "n1": dimensions[0],
                "n2": dimensions[1], "n3": dimensions[2],
                "dn_raw": npb.raw_f64(float(
                    (n1 - 2) * (n2 - 2) * (n3 - 2)
                )),
                "rnm2": "rnm2", "rnmu": "rnmu",
                "results": ["rnm2", "rnmu"],
                "lanes": _canonical_lanes(interior),
            }, 6 * interior + 12)
    return tuple(invocations), primitive_records, levels


def _write_npb_lazy_bundle(capture, workload, root, *, source_sha256,
                           binary_sha256, config_sha256):
    root = Path(root).resolve()
    if root.exists():
        raise BuildError(f"fresh NPB lazy bundle root required: {root}")
    images = root / "images"
    images.mkdir(parents=True)
    arrays = []
    initial_scalars = {}
    if workload == "cg":
        names = {
            1: ("rowstr", "input", "u32"),
            2: ("colidx", "input", "u32"),
            3: ("a", "input", "f64"),
            4: ("p", "state", "f64"),
            5: ("q", "state", "f64"),
            6: ("z", "state", "f64"),
            7: ("r", "state", "f64"),
            8: ("x", "state", "f64"),
        }
        for array_id in sorted(capture["arrays"]):
            record = capture["arrays"][array_id]
            name, role, element_type = names[array_id]
            relative = f"images/{name}.{element_type}"
            (root / relative).write_bytes(record["payload"])
            arrays.append(lazy.ArrayImage(
                name, role, element_type, record["count"],
                record["logical_base"], relative, record["sha256"],
            ))
        invocations, primitive_records, initial_scalars = (
            _cg_invocation_descriptors(capture)
        )
    elif workload == "mg":
        invocations, primitive_records, levels = (
            _mg_invocation_descriptors(capture)
        )
        for array_id, prefix in ((21, "u"), (23, "r")):
            record = capture["arrays"][array_id]
            for level in sorted(levels):
                dimensions, offset = levels[level]
                count = dimensions[0] * dimensions[1] * dimensions[2]
                payload = record["payload"][offset * 8:(offset + count) * 8]
                name = f"{prefix}.l{level}"
                relative = f"images/{name}.f64"
                (root / relative).write_bytes(payload)
                arrays.append(lazy.ArrayImage(
                    name, "state", "f64", count,
                    record["logical_base"] + offset * 8, relative,
                    hashlib.sha256(payload).hexdigest(),
                ))
        record = capture["arrays"][22]
        finest_count = levels[max(levels)][0]
        count = finest_count[0] * finest_count[1] * finest_count[2]
        payload = record["payload"][:count * 8]
        relative = "images/v.f64"
        (root / relative).write_bytes(payload)
        arrays.append(lazy.ArrayImage(
            "v", "input", "f64", count, record["logical_base"],
            relative, hashlib.sha256(payload).hexdigest(),
        ))
    else:
        raise BuildError(f"unknown NPB lazy workload: {workload}")
    boundary_crosswalk, boundary_expectations = _npb_boundary_expectations(
        capture, workload
    )
    meta = {
        "schema": 2, "workload": f"npb_{workload}",
        "source_sha256": source_sha256,
        "binary_sha256": binary_sha256,
        "config_sha256": config_sha256,
        "initial_scalars": initial_scalars,
        "boundary_commitments": boundary_expectations,
        "native_boundary_crosswalk": boundary_crosswalk,
    }
    lazy.write_bundle(
        root, meta, arrays, invocations,
        {"primitive_records": primitive_records},
    )
    return lazy.read_bundle(root)


def _run_checked(command, *, cwd, label, env=None):
    completed = subprocess.run(
        [str(item) for item in command], cwd=cwd, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        env=env, check=False,
    )
    if completed.returncode != 0:
        raise BuildError(
            f"{label} exited {completed.returncode}:\n{completed.stdout}"
        )
    return completed.stdout


def _compile_npb_hook(output):
    compiler = shutil.which("g++")
    if compiler is None:
        raise BuildError("g++ is required for NPB trace hooks")
    command = [
        compiler, "-std=c++17", "-O2", "-Wall", "-Wextra", "-Werror",
        "-ffp-contract=off", "-fno-fast-math", "-I", SOURCE_ROOT,
        "-c", NPB_TRACE_IMPLEMENTATION, "-o", output,
    ]
    _run_checked(command, cwd=REPO, label="NPB hook compilation")
    return command


def _build_npb(source, workload, hook_object, npb_class):
    if not isinstance(npb_class, str) or not re.fullmatch(r"[SWABCDEF]", npb_class):
        raise BuildError(f"NPB {workload} class is invalid: {npb_class!r}")
    flags = "-O3 -fopenmp -g -fallow-invalid-boz -ffp-contract=off -fno-fast-math"
    directory = source / workload.upper()
    command = [
        "make", f"CLASS={npb_class}", f"FFLAGS={flags}",
        f"FLINKFLAGS={flags}",
        f"F_LIB={hook_object} -lstdc++ -lcrypto",
    ]
    _run_checked(
        command, cwd=directory,
        label=f"NPB {workload} Class {npb_class} build",
    )
    binary = source / "bin" / f"{workload}.{npb_class}.x"
    if not binary.is_file():
        raise BuildError(
            f"NPB {workload} Class {npb_class} binary is missing"
        )
    return binary.resolve(), command


def _run_npb_binary(binary, workload, root, run_name):
    capture = root / f"{workload}-{run_name}.capture.bin"
    allocation = root / f"{workload}-{run_name}.allocation.u64"
    environment = {
        **os.environ,
        "OMP_NUM_THREADS": "4",
        "OMP_DYNAMIC": "FALSE",
        "OMP_PROC_BIND": "TRUE",
        "MATCHED_NPB_CAPTURE_FILE": str(capture),
        "MATCHED_NPB_ALLOCATION_FILE": str(allocation),
    }
    output = _run_checked(
        [binary], cwd=binary.parent.parent / workload.upper(),
        label=f"NPB {workload} {run_name}", env=environment,
    )
    if "Verification    =               SUCCESSFUL" not in output:
        # Preserve the original output in the error; do not accept the less
        # strict zeta/norm status lines alone.
        raise BuildError(f"NPB {workload} official verifier failed:\n{output}")
    thread_rows = re.findall(
        r"Number of available threads:\s+(\d+)", output
    )
    if not thread_rows or set(thread_rows) != {"4"}:
        raise BuildError(
            f"NPB {workload} did not prove exactly four runtime threads"
        )
    if not capture.is_file() or not allocation.is_file():
        raise BuildError(f"NPB {workload} trace hooks produced no evidence")
    run_identity = {
        "argv": [str(binary)],
        "cwd": str((binary.parent.parent / workload.upper()).resolve()),
        "environment": {
            name: environment[name]
            for name in ("OMP_NUM_THREADS", "OMP_DYNAMIC", "OMP_PROC_BIND")
        },
    }
    return (
        capture, output, _npb_allocation_probe(allocation, workload),
        run_identity,
    )


def _capture_boundary_map(capture):
    result = {}
    for record in capture["boundaries"]:
        key = f"{record['boundary']}.iter{record['iteration']}"
        if key in result:
            raise BuildError(f"duplicate NPB boundary commitment {key}")
        result[key] = {
            "element_bits": record["element_bits"],
            "count": record["count"],
            "sha256": record["sha256"],
        }
    encoded = json.dumps(
        result, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return result, hashlib.sha256(encoded).hexdigest()


def _validate_npb_native_boundary_sequence(capture, workload):
    expected = []
    if workload == "cg":
        _invocations, _records, _scalars = _cg_invocation_descriptors(capture)
        config = capture["invocations"][0]["parameters"]
        columns, niter, cgitmax = config[1], config[3], config[4]
        for outer in range(1, niter + 1):
            for cgit in range(1, cgitmax + 1):
                iteration = outer * 100 + cgit
                expected.extend(
                    (boundary, iteration, columns, 64)
                    for boundary in (111, 112, 113, 114)
                )
            expected.extend(
                (boundary, outer, columns, 64)
                for boundary in (113, 111, 112, 114, 110)
            )
            expected.extend(((115, outer, 1, 64), (116, outer, 1, 64)))
    elif workload == "mg":
        _invocations, _records, _levels = _mg_invocation_descriptors(capture)
        boundary_ids = {201: (201,), 202: (202,), 203: (203,), 204: (204,)}
        for record in capture["invocations"][1:]:
            phase = record["phase"]
            if phase == 205:
                expected.extend((boundary, record["iteration"], 1, 64)
                                for boundary in (205, 206))
            else:
                try:
                    ids = boundary_ids[phase]
                except KeyError as error:
                    raise BuildError(
                        f"NPB MG invocation phase {phase} has no boundary"
                    ) from error
                expected.extend(
                    (boundary, record["iteration"], record["work_items"], 64)
                    for boundary in ids
                )
    else:
        raise BuildError(f"unknown NPB boundary workload {workload}")
    observed = [
        (
            record["boundary"], record["iteration"], record["count"],
            record["element_bits"],
        )
        for record in capture["boundaries"]
    ]
    if observed != expected:
        raise BuildError(f"NPB {workload} native boundary sequence differs")
    return True


def _npb_boundary_expectations(capture, workload):
    """Crosswalk native boundary ids to exact lazy state commitments."""
    _validate_npb_native_boundary_sequence(capture, workload)
    crosswalk = {}
    if workload == "cg":
        config = capture["invocations"][0]["parameters"]
        niter, cgitmax = config[3], config[4]
        vector_names = {111: "z", 112: "p", 113: "q", 114: "r"}
        program_points = {
            110: "normalize", 111: "update_zr",
            112: "update_p", 113: "spmv",
            114: "update_zr", 115: "residual_norm",
            116: "normalize",
        }
        for record in capture["boundaries"]:
            boundary = record["boundary"]
            iteration = record["iteration"]
            if boundary in vector_names:
                final_dump = 1 <= iteration <= niter
                if boundary == 114 and final_dump:
                    lazy_iteration = iteration * 100 + cgitmax + 1
                    program_point = "spmv"
                else:
                    lazy_iteration = (
                        iteration * 100 + cgitmax
                        if final_dump else iteration
                    )
                    program_point = program_points[boundary]
                lazy_key = (
                    f"{vector_names[boundary]}.{program_point}"
                    f".iter{lazy_iteration}"
                )
            elif boundary == 110:
                lazy_key = f"x.{program_points[boundary]}.iter{iteration}"
            elif boundary == 115:
                lazy_key = (
                    f"scalar.rnorm.{program_points[boundary]}"
                    f".iter{iteration * 100 + cgitmax + 1}"
                )
            elif boundary == 116:
                lazy_key = (
                    f"scalar.zeta.{program_points[boundary]}.iter{iteration}"
                )
            else:
                raise BuildError(f"unknown native CG boundary {boundary}")
            native_key = f"{boundary}.iter{iteration}"
            crosswalk[native_key] = lazy_key
    elif workload == "mg":
        names = {
            201: "u", 202: "r", 203: "r", 204: "u",
            205: "scalar.rnm2", 206: "scalar.rnmu",
        }
        program_points = {
            201: "psinv", 202: "resid", 203: "rprj3", 204: "interp",
        }
        for record in capture["boundaries"]:
            boundary = record["boundary"]
            iteration = record["iteration"]
            try:
                name = names[boundary]
            except KeyError as error:
                raise BuildError(
                    f"unknown native MG boundary {boundary}"
                ) from error
            if boundary in (205, 206):
                lazy_key = f"{name}.norm2u3.iter{iteration}"
            else:
                level = iteration % 100
                if boundary == 203:
                    level -= 1
                lazy_key = (
                    f"{name}.l{level}.{program_points[boundary]}"
                    f".iter{iteration}"
                )
            native_key = f"{boundary}.iter{iteration}"
            crosswalk[native_key] = lazy_key
    else:
        raise BuildError(f"unknown NPB boundary workload {workload}")
    if len(crosswalk) != len(capture["boundaries"]):
        raise BuildError(f"NPB {workload} native boundary keys overlap")
    expectations = {}
    for record in capture["boundaries"]:
        native_key = f"{record['boundary']}.iter{record['iteration']}"
        lazy_key = crosswalk[native_key]
        if record["element_bits"] != 64:
            raise BuildError(
                f"NPB {workload} boundary {native_key} is not binary64"
            )
        previous = expectations.get(lazy_key)
        if previous is not None and previous != record["sha256"]:
            raise BuildError(
                f"NPB {workload} native boundaries disagree for {lazy_key}"
            )
        expectations[lazy_key] = record["sha256"]
    return crosswalk, expectations


def _validate_npb_boundary_commitments(capture, workload, lazy_boundaries):
    crosswalk, expectations = _npb_boundary_expectations(capture, workload)
    for lazy_key, expected in expectations.items():
        if lazy_boundaries.get(lazy_key) != expected:
            raise BuildError(
                f"NPB {workload} lazy boundary {lazy_key} differs"
            )
    return crosswalk


def _npb_semantic_identity():
    return {
        "builder_source_sha256": _sha256_file(BUILDER_SOURCE),
        "canonical_trace_source_sha256": _sha256_file(
            CANONICAL_TRACE_SOURCE
        ),
        "expander_sha256": _sha256_file(NPB_EXPANDER_SOURCE),
        "hook_header_sha256": _sha256_file(NPB_TRACE_HOOKS),
        "hook_implementation_sha256": _sha256_file(
            NPB_TRACE_IMPLEMENTATION
        ),
        "lazy_runtime_sha256": _sha256_file(LAZY_TRACE_SOURCE),
        "trace_abi_sha256": _sha256_file(TRACE_ABI),
        "cg_patch_sha256": _sha256_file(NPB_PATCHES["cg"]),
        "mg_patch_sha256": _sha256_file(NPB_PATCHES["mg"]),
    }


def _json_sha256(value):
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _npb_bundle_identity(bundle):
    images = [
        {"name": array.name, "sha256": array.sha256}
        for array in bundle.arrays
    ]
    invocations = [
        {
            "ordinal": invocation.ordinal,
            "phase": invocation.phase,
            "kernel": invocation.kernel,
            "iteration": invocation.iteration,
            "work_items": invocation.work_items,
            "parameters": invocation.parameters,
        }
        for invocation in bundle.invocations
    ]
    return {
        "ordered_image_sha256": images,
        "invocation_table_sha256": _json_sha256(invocations),
        "dynamic_work_sha256": _json_sha256(bundle.dynamic_work),
    }


def _validate_npb_semantic_identity(expected):
    current = _npb_semantic_identity()
    if not isinstance(expected, dict) or set(expected) != set(current):
        raise BuildError("formal NPB semantic identity fields differ")
    for name, observed in current.items():
        if expected.get(name) != observed:
            raise BuildError(f"formal NPB semantic identity {name} differs")
    return True


def build_and_run_npb_fixture(source_root, outdir, *, workloads=("cg", "mg"),
                              expand=True):
    """Build patched Class S CG/MG and prove deterministic raw boundaries."""
    outdir = Path(outdir).resolve()
    selected = tuple(workloads)
    if not selected or any(workload not in NPB_PATCHES for workload in selected):
        raise BuildError("NPB fixture workload selection is invalid")
    if len(set(selected)) != len(selected):
        raise BuildError("NPB fixture workload selection contains duplicates")
    if outdir.exists():
        raise BuildError(f"fresh NPB fixture root required: {outdir}")
    outdir.mkdir(parents=True)
    source = outdir / "source"
    source_root = Path(source_root).resolve()
    source_repo = _git_root(source_root)
    source_commit = _git_read(source_repo, "rev-parse", "HEAD").strip()
    source_status = _git_read(
        source_repo, "status", "--porcelain", "--untracked-files=all"
    )
    shutil.copytree(source_root, source)
    # Imported trees can contain stale Class E artifacts. They are never
    # evidence and must not influence the Class S fixture build.
    _run_checked(["make", "clean"], cwd=source, label="NPB fixture clean")
    for workload in selected:
        _transform_npb_source(source_root, workload, source)
    hook_object = outdir / "npb_trace_hooks.o"
    hook_command = _compile_npb_hook(hook_object)
    result = {
        "schema": 1,
        "status": "verified" if expand else "diagnostic",
        "publishable": False,
        "paper_evidence": False,
        "evidence_scope": "class_s_validation",
        "validation_complete": bool(expand),
        "class": "S",
        "threads": 4,
        "source_root": str(source_root),
        "source_commit": source_commit,
        "source_tree_clean": not bool(source_status.strip()),
        "formal_evidence": False,
        "formal_rejection_reason": (
            "fixture Class S is not the frozen 12.8 GB paper input"
        ),
        "hook_command": [str(item) for item in hook_command],
        "semantic_identity": _npb_semantic_identity(),
        "workloads": {},
    }
    for workload in selected:
        binary, command = _build_npb(source, workload, hook_object, "S")
        reference_path, _, allocated_bytes, reference_run = _run_npb_binary(
            binary, workload, outdir, "reference"
        )
        repeated_path, _, repeated_bytes, repeated_run = _run_npb_binary(
            binary, workload, outdir, "repeat"
        )
        if allocated_bytes != repeated_bytes:
            raise BuildError(f"NPB {workload} allocation probe is unstable")
        reference = _parse_npb_capture(reference_path)
        repeated = _parse_npb_capture(repeated_path)
        if reference_path.read_bytes() != repeated_path.read_bytes():
            raise BuildError(f"NPB {workload} bounded capture is unstable")
        boundary_map, boundary_map_sha256 = _capture_boundary_map(reference)
        repeated_boundary_map, repeated_boundary_sha256 = (
            _capture_boundary_map(repeated)
        )
        if (
            boundary_map != repeated_boundary_map
            or boundary_map_sha256 != repeated_boundary_sha256
        ):
            raise BuildError(f"NPB {workload} boundary commitments differ")
        source_sha256 = _sha256_file(
            source / workload.upper() / f"{workload}.f"
        )
        binary_sha256 = _sha256_file(binary)
        parameter_sha256 = _sha256_file(
            source / workload.upper() / "npbparams.h"
        )
        reference_bundle = _write_npb_lazy_bundle(
            reference, workload, outdir / f"{workload}-reference",
            source_sha256=source_sha256,
            binary_sha256=binary_sha256,
            config_sha256=parameter_sha256,
        )
        repeated_bundle = _write_npb_lazy_bundle(
            repeated, workload, outdir / f"{workload}-repeat",
            source_sha256=source_sha256,
            binary_sha256=binary_sha256,
            config_sha256=parameter_sha256,
        )
        descriptor = reference_bundle.root / "trace.v2.json"
        repeated_descriptor = repeated_bundle.root / "trace.v2.json"
        if descriptor.read_bytes() != repeated_descriptor.read_bytes():
            raise BuildError(f"NPB {workload} descriptor is unstable")
        if expand:
            boundary_crosswalk, boundary_expectations = (
                _npb_boundary_expectations(reference, workload)
            )
            expanded_sha256, expanded_records, replay_boundaries = (
                npb.expanded_evidence(
                    reference_bundle,
                    boundary_expectations=boundary_expectations,
                )
            )
            repeated_crosswalk, repeated_expectations = (
                _npb_boundary_expectations(repeated, workload)
            )
            repeated_evidence = npb.expanded_evidence(
                repeated_bundle,
                boundary_expectations=repeated_expectations,
            )
            if (
                repeated_crosswalk != boundary_crosswalk
                or repeated_evidence != (
                    expanded_sha256, expanded_records, replay_boundaries
                )
            ):
                raise BuildError(
                    f"NPB {workload} reference/repeat expanded evidence differs"
                )
            repeated_expanded_sha256 = repeated_evidence[0]
            repeated_expanded_records = repeated_evidence[1]
            lazy_boundary_map_sha256 = _json_sha256(replay_boundaries)
            boundary_crosswalk_sha256 = _json_sha256(boundary_crosswalk)
        else:
            expanded_sha256 = "pending"
            expanded_records = reference_bundle.dynamic_work[
                "primitive_records"
            ]
            boundary_crosswalk = {}
            replay_boundaries = {}
            repeated_expanded_sha256 = "pending"
            repeated_expanded_records = expanded_records
            lazy_boundary_map_sha256 = "pending"
            boundary_crosswalk_sha256 = "pending"
        bundle_identity = _npb_bundle_identity(reference_bundle)
        result["workloads"][workload] = {
            "class": "S",
            "official_verification": "pass",
            "raw_verification": "pass",
            "runtime_threads": 4,
            "measured_allocated_bytes": allocated_bytes,
            "boundary_count": len(boundary_map),
            "boundary_ids": sorted({
                record["boundary"] for record in reference["boundaries"]
            }),
            "boundary_map": boundary_map,
            "boundary_map_sha256": boundary_map_sha256,
            "capture_file": str(reference_path),
            "capture_sha256": reference["capture_sha256"],
            "repeated_capture_sha256": repeated["capture_sha256"],
            "descriptor_file": str(descriptor),
            "descriptor_sha256": _sha256_file(descriptor),
            "repeated_descriptor_sha256": _sha256_file(
                repeated_descriptor
            ),
            **bundle_identity,
            "expanded_sha256": expanded_sha256,
            "expanded_records": expanded_records,
            "repeated_expanded_sha256": repeated_expanded_sha256,
            "repeated_expanded_records": repeated_expanded_records,
            "boundary_crosswalk": boundary_crosswalk,
            "boundary_crosswalk_sha256": boundary_crosswalk_sha256,
            "lazy_boundary_map": replay_boundaries,
            "lazy_boundary_map_sha256": lazy_boundary_map_sha256,
            "binary_sha256": binary_sha256,
            "binary_file": str(binary),
            "parameter_sha256": parameter_sha256,
            "config_sha256": parameter_sha256,
            "source_sha256": source_sha256,
            "patch_sha256": _sha256_file(NPB_PATCHES[workload]),
            "hook_sha256": _sha256_file(NPB_TRACE_IMPLEMENTATION),
            "trace_abi_sha256": _sha256_file(TRACE_ABI),
            "build_command": [str(item) for item in command],
            "reference_run": reference_run,
            "repeat_run": repeated_run,
        }
    output_name = "manifest.json" if expand else "diagnostic.json"
    _validate_npb_semantic_identity(result["semantic_identity"])
    contract.atomic_write_json(outdir / output_name, result)
    return result


def load_frozen_npb_inputs(path):
    path = Path(path).resolve()
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise BuildError(f"invalid frozen NPB inputs: {error}") from error
    if value.get("schema") != 1 or value.get("status") != "accepted":
        raise BuildError("frozen NPB inputs are not accepted schema 1")
    _validate_npb_semantic_identity(value.get("semantic_identity"))
    workloads = value.get("workloads", {})
    result = {}
    for short, name in (("cg", "npb_cg"), ("mg", "npb_mg")):
        row = workloads.get(name)
        required = {
            "source_root", "source_commit", "parameter_file",
            "parameter_sha256", "allocated_bytes", "class",
        }
        if not isinstance(row, dict) or not required.issubset(row):
            raise BuildError(f"frozen {name} fields are incomplete")
        result[short] = dict(row)
    return result, _sha256_file(path)


def build_and_run_npb_formal(inputs_path, outdir):
    """Run the frozen paper NPB inputs or fail before publishing evidence."""
    rows, inputs_sha256 = load_frozen_npb_inputs(inputs_path)
    roots = {Path(row["source_root"]).resolve() for row in rows.values()}
    commits = {row["source_commit"] for row in rows.values()}
    if len(roots) != 1 or len(commits) != 1:
        raise BuildError("formal NPB CG/MG source identity differs")
    source_root = roots.pop()
    outdir = Path(outdir).resolve()
    if outdir.exists():
        raise BuildError(f"fresh formal NPB root required: {outdir}")
    validate_npb_formal_source_identity(
        source_root,
        expected_commit=commits.pop(),
        parameter_files={
            workload: row["parameter_file"] for workload, row in rows.items()
        },
        expected_parameter_hashes={
            workload: row["parameter_sha256"] for workload, row in rows.items()
        },
        allocated_bytes={
            workload: row["allocated_bytes"] for workload, row in rows.items()
        },
    )
    outdir.mkdir(parents=True)
    source = outdir / "source"
    shutil.copytree(source_root, source)
    _run_checked(["make", "clean"], cwd=source, label="formal NPB clean")
    for workload in rows:
        _transform_npb_source(source_root, workload, source)
    hook_object = outdir / "npb_trace_hooks.o"
    hook_command = _compile_npb_hook(hook_object)
    result = {
        "schema": 1,
        "mode": "formal",
        "status": "verified",
        "publishable": True,
        "paper_evidence": True,
        "evidence_scope": "formal_paper_input",
        "threads": 4,
        "source_root": str(source_root),
        "source_commit": next(iter({row["source_commit"] for row in rows.values()})),
        "inputs_sha256": inputs_sha256,
        "hook_command": [str(item) for item in hook_command],
        "semantic_identity": _npb_semantic_identity(),
        "workloads": {},
    }
    measured = {}
    for workload, row in rows.items():
        binary, command = _build_npb(
            source, workload, hook_object, row["class"]
        )
        capture_path, _, allocated, run_identity = _run_npb_binary(
            binary, workload, outdir, "formal"
        )
        if allocated != row["allocated_bytes"]:
            raise BuildError(
                f"formal NPB {workload} allocation probe {allocated} "
                f"!= inputs.json {row['allocated_bytes']}"
            )
        capture = _parse_npb_capture(capture_path)
        boundary_map, boundary_map_sha256 = _capture_boundary_map(capture)
        source_sha256 = _sha256_file(
            source / workload.upper() / f"{workload}.f"
        )
        binary_sha256 = _sha256_file(binary)
        bundle = _write_npb_lazy_bundle(
            capture, workload, outdir / f"{workload}-formal",
            source_sha256=source_sha256,
            binary_sha256=binary_sha256,
            config_sha256=row["parameter_sha256"],
        )
        boundary_crosswalk, boundary_expectations = (
            _npb_boundary_expectations(capture, workload)
        )
        expanded_sha256, expanded_records, replay_boundaries = (
            npb.expanded_evidence(
                bundle, boundary_expectations=boundary_expectations
            )
        )
        bundle_identity = _npb_bundle_identity(bundle)
        measured[workload] = allocated
        descriptor = bundle.root / "trace.v2.json"
        result["workloads"][workload] = {
            "class": row["class"],
            "official_verification": "pass",
            "raw_verification": "pass",
            "runtime_threads": 4,
            "capture_file": str(capture_path),
            "capture_sha256": capture["capture_sha256"],
            "boundary_map": boundary_map,
            "boundary_map_sha256": boundary_map_sha256,
            "descriptor_file": str(descriptor),
            "descriptor_sha256": _sha256_file(descriptor),
            **bundle_identity,
            "expanded_sha256": expanded_sha256,
            "expanded_records": expanded_records,
            "boundary_crosswalk": boundary_crosswalk,
            "boundary_crosswalk_sha256": _json_sha256(
                boundary_crosswalk
            ),
            "lazy_boundary_map": replay_boundaries,
            "lazy_boundary_map_sha256": _json_sha256(
                replay_boundaries
            ),
            "measured_allocated_bytes": allocated,
            "parameter_sha256": row["parameter_sha256"],
            "config_sha256": row["parameter_sha256"],
            "binary_sha256": binary_sha256,
            "binary_file": str(binary),
            "source_sha256": source_sha256,
            "patch_sha256": _sha256_file(NPB_PATCHES[workload]),
            "hook_header_sha256": _sha256_file(NPB_TRACE_HOOKS),
            "hook_implementation_sha256": _sha256_file(
                NPB_TRACE_IMPLEMENTATION
            ),
            "expander_sha256": _sha256_file(NPB_EXPANDER_SOURCE),
            "lazy_runtime_sha256": _sha256_file(LAZY_TRACE_SOURCE),
            "canonical_trace_source_sha256": _sha256_file(
                CANONICAL_TRACE_SOURCE
            ),
            "trace_abi_sha256": _sha256_file(TRACE_ABI),
            "build_command": [str(item) for item in command],
            "formal_run": run_identity,
        }
    validate_npb_formal_source(
        source_root,
        expected_commit=result["source_commit"],
        parameter_files={
            workload: row["parameter_file"] for workload, row in rows.items()
        },
        expected_parameter_hashes={
            workload: row["parameter_sha256"] for workload, row in rows.items()
        },
        allocated_bytes={
            workload: row["allocated_bytes"] for workload, row in rows.items()
        },
        measured_allocated_bytes=measured,
    )
    _validate_npb_semantic_identity(result["semantic_identity"])
    contract.atomic_write_json(outdir / "manifest.json", result)
    return result


def _sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_bytes(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return path.resolve()


def _float_bits(value):
    return struct.unpack("<I", struct.pack("<f", value))[0]


def fixture_gather_values():
    return tuple(float(index) + 0.25 for index in range(24))


def fixture_gather_index():
    return tuple((index * 7 + 3) % 24 for index in range(24))


def fixture_gather_expected_bits():
    values = fixture_gather_values()
    return tuple(_float_bits(values[index]) for index in fixture_gather_index())


def fixture_scatter_values():
    return tuple(float(index) + 0.5 for index in range(24))


def fixture_scatter_index():
    # Positions 4 and 19 deliberately target the same element.  Canonical
    # program order therefore makes position 19 the last writer.
    result = list(range(24))
    result[19] = result[4]
    return tuple(result)


def fixture_scatter_expected_bits():
    values = fixture_scatter_values()
    index = fixture_scatter_index()
    destination = [0] * (max(index) + 1)
    for position, target in enumerate(index):
        destination[target] = _float_bits(values[position])
    return tuple(destination)


def _pack_f32(values):
    return struct.pack(f"<{len(values)}f", *values)


def _pack_u64(values):
    return struct.pack(f"<{len(values)}Q", *values)


def _fixture_mcf_payload():
    nodes = 4
    arcs = (
        (0, 1, -5, 0),
        (1, 2, 2, 1),
        (2, 3, -3, 0),
        (0, 3, 1, 0),
    )
    pricing_offsets = (0, 3, 6)
    pricing_index = (0, 1, 3, 2, 3, 1)
    price_out_index = (0, 2, 3)
    payload = bytearray(struct.pack(
        "<8sQQQQQ", b"MCFREG1\0", nodes, len(arcs), 2,
        len(pricing_index), len(price_out_index),
    ))
    for arc in arcs:
        payload.extend(struct.pack("<QQqq", *arc))
    for values in (
        (0, 1, -1, 2),
        (-1, -1, -1, -1),
        (0, 0, 0, 0),
        (0, 0, 0, 0),
        (-1, -1, -1, -1),
    ):
        payload.extend(struct.pack(f"<{nodes}q", *values))
    payload.extend(_pack_u64(pricing_offsets))
    payload.extend(_pack_u64(pricing_index))
    payload.extend(_pack_u64(price_out_index))
    return bytes(payload)


def _make_fixture_inputs(root):
    root = Path(root)
    files = {
        "mcf": _write_bytes(root / "mcf.regions", _fixture_mcf_payload()),
        "amg_values": _write_bytes(
            root / "amg.values.f32", _pack_f32(fixture_gather_values())
        ),
        "amg_index": _write_bytes(
            root / "amg.index.u64", _pack_u64(fixture_gather_index())
        ),
        "lulesh_values": _write_bytes(
            root / "lulesh.values.f32", _pack_f32(fixture_scatter_values())
        ),
        "lulesh_index": _write_bytes(
            root / "lulesh.index.u64", _pack_u64(fixture_scatter_index())
        ),
    }
    return {
        name: {"path": str(path), "sha256": _sha256_file(path)}
        for name, path in files.items()
    }


def validate_mode(*, formal, fixture, synthetic):
    if formal and fixture:
        raise BuildError("formal mode rejects fixture inputs")
    if formal and synthetic:
        raise BuildError("formal mode rejects synthetic inputs")
    if formal is fixture:
        raise BuildError("select exactly one of formal or fixture mode")
    return True


def _compiler_version(cxx):
    try:
        completed = subprocess.run(
            [cxx, "--version"], text=True, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, check=True,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        raise BuildError(f"cannot identify compiler {cxx}: {error}") from error
    return completed.stdout.splitlines()[0]


def _compiler_identity(cxx):
    resolved = shutil.which(cxx)
    if resolved is None:
        raise BuildError(f"compiler is missing: {cxx}")
    path = Path(resolved).resolve()
    return {
        "path": str(path),
        "sha256": _sha256_file(path),
        "version": _compiler_version(str(path)),
    }


def _compile(cxx, workload, backend, output, *, fixture):
    source = SOURCES[workload]
    command = [
        cxx,
        *COMMAND_FLAGS,
        f"-DMATCHED_BACKEND={BACKEND_IDS[backend]}",
    ]
    if fixture:
        command.append("-DMATCHED_FIXTURE=1")
    command.extend(("-I", str(SOURCE_ROOT), str(source)))
    if workload == "mcf":
        command.append(str(SOURCE_ROOT / "mcfreg2.cc"))
    command.extend(("-o", str(output)))
    completed = subprocess.run(
        command, cwd=REPO, text=True, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, check=False,
    )
    if completed.returncode != 0:
        raise BuildError(
            f"{workload}:{backend} compilation failed:\n{completed.stdout}"
        )
    return command


def _formal_file(path_value, digest_value, label):
    path = Path(path_value or "")
    if not path.is_absolute() or path.resolve() != path or not path.is_file():
        raise BuildError(f"formal {label} input is missing")
    if _sha256_file(path) != digest_value:
        raise BuildError(f"formal {label} input SHA-256 differs")
    return {"path": str(path), "sha256": digest_value}


def load_formal_inputs(path):
    path = Path(path)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise BuildError(f"invalid frozen inputs manifest: {error}") from error
    if value.get("schema") != 1 or value.get("status") != "accepted":
        raise BuildError("formal inputs manifest is not accepted schema 1")
    workloads = value.get("workloads", {})
    for workload, minimum in (
        ("mcf", 345_000_000),
        ("amg_gather", 1 << 30),
        ("lulesh_scatter", 1 << 30),
    ):
        allocated = workloads.get(workload, {}).get("allocated_bytes")
        if (
            not isinstance(allocated, int)
            or isinstance(allocated, bool)
            or allocated < minimum
        ):
            raise BuildError(
                f"formal {workload} allocated_bytes is below paper input"
            )
    if workloads.get("mcf", {}).get("synthetic") is not False:
        raise BuildError("formal mcf synthetic input is forbidden")
    records = {
        "mcf": (workloads.get("mcf", {}).get("input"),
                workloads.get("mcf", {}).get("input_sha256")),
        "amg_values": (workloads.get("amg_gather", {}).get("input"),
                       workloads.get("amg_gather", {}).get("input_sha256")),
        "amg_index": (workloads.get("amg_gather", {}).get("index"),
                      workloads.get("amg_gather", {}).get("index_sha256")),
        "lulesh_values": (workloads.get("lulesh_scatter", {}).get("input"),
                          workloads.get("lulesh_scatter", {}).get("input_sha256")),
        "lulesh_index": (workloads.get("lulesh_scatter", {}).get("index"),
                         workloads.get("lulesh_scatter", {}).get("index_sha256")),
        "mcf_source": (workloads.get("mcf", {}).get("source"),
                       workloads.get("mcf", {}).get("source_sha256")),
    }
    result = {
        name: _formal_file(path_value, expected, name)
        for name, (path_value, expected) in records.items()
    }
    for prefix, workload in (
        ("amg", "amg_gather"),
        ("lulesh", "lulesh_scatter"),
    ):
        values = Path(result[f"{prefix}_values"]["path"])
        index = Path(result[f"{prefix}_index"]["path"])
        if values.stat().st_size == 0 or values.stat().st_size % 4:
            raise BuildError(f"formal {workload} values are not nonempty f32")
        if index.stat().st_size == 0 or index.stat().st_size % 8:
            raise BuildError(f"formal {workload} index is not nonempty u64")
        count = index.stat().st_size // 8
        value_count = values.stat().st_size // 4
        if workload == "lulesh_scatter" and value_count != count:
            raise BuildError("formal lulesh_scatter value/index counts differ")
        result[f"{prefix}_values"]["element_count"] = value_count
        result[f"{prefix}_index"]["element_count"] = count
        result[f"{prefix}_values"]["allocated_bytes"] = workloads[
            workload
        ]["allocated_bytes"]
    return result, _sha256_file(path)


def build_suite(
    outdir, *, inputs, cxx="g++", fixture=False,
    input_manifest_sha256=None,
):
    outdir = Path(outdir).resolve()
    if outdir.exists():
        raise BuildError(f"fresh build root required: {outdir}")
    (outdir / "bin").mkdir(parents=True)
    binaries = {}
    commands = {}
    for workload in SOURCES:
        for backend in BACKENDS:
            output = outdir / "bin" / f"{workload}-{backend}"
            command = _compile(
                cxx, workload, backend, output, fixture=fixture
            )
            key = f"{workload}:{backend}"
            commands[key] = command
            binaries[key] = {
                "path": str(output.resolve()),
                "sha256": _sha256_file(output),
                "source_sha256": _sha256_file(SOURCES[workload]),
                "trace_abi_sha256": _sha256_file(TRACE_ABI),
            }
    manifest = {
        "schema": 1,
        "mode": "fixture" if fixture else "formal",
        "root": str(outdir),
        "threads": 4,
        "compiler": _compiler_identity(cxx),
        "flags": list(STRICT_FLAGS),
        "command_flags": list(COMMAND_FLAGS),
        "inputs": inputs,
        "input_manifest_sha256": (
            input_manifest_sha256
            or hashlib.sha256(contract.canonical_json(inputs)).hexdigest()
        ),
        "binaries": binaries,
        "commands": commands,
        "shared_objects": {
            "inputs": inputs,
            "binaries": binaries,
        },
        "latency_action_layouts": {
            "mcf": latency_action_layout(
                "mcf", ("pricing_kernel", "price_out_impl")
            ),
            "amg_gather": latency_action_layout(
                "amg_gather", ("amg_gather",)
            ),
            "lulesh_scatter": latency_action_layout(
                "lulesh_scatter", ("lulesh_scatter",)
            ),
        },
    }
    contract.atomic_write_json(outdir / "manifest.json", manifest)
    return manifest


def build_fixture_suite(outdir, cxx="g++"):
    outdir = Path(outdir).resolve()
    inputs = _make_fixture_inputs(outdir.parent / f".{outdir.name}.inputs")
    return build_suite(outdir, inputs=inputs, cxx=cxx, fixture=True)


def _read_words(path, word_bits):
    payload = Path(path).read_bytes()
    width = word_bits // 8
    if len(payload) % width:
        raise BuildError(f"raw output width differs: {path}")
    code = "I" if word_bits == 32 else "Q"
    return tuple(struct.unpack(f"<{len(payload) // width}{code}", payload))


def _run(command, label):
    completed = subprocess.run(
        [str(item) for item in command], cwd=REPO, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False,
        env={**os.environ, "OMP_NUM_THREADS": "4"},
    )
    if completed.returncode != 0:
        raise BuildError(f"{label} exited {completed.returncode}:\n{completed.stdout}")
    return completed.stdout


def _markers(output):
    work = {}
    invocations = {}
    duplicate_policy = None
    state_shape = None
    for line in output.splitlines():
        if line.startswith("MATCHED_PHASE_WORK="):
            phase, value = line.split("=", 1)[1].rsplit(":", 1)
            work[phase] = int(value)
        elif line.startswith("MATCHED_PHASE_INVOCATIONS="):
            phase, value = line.split("=", 1)[1].rsplit(":", 1)
            invocations[phase] = int(value)
        elif line.startswith("MATCHED_DUPLICATE_POLICY="):
            duplicate_policy = line.split("=", 1)[1]
        elif line.startswith("MATCHED_STATE_SHAPE="):
            state_shape = {
                name: int(value)
                for name, value in (
                    item.split(":", 1)
                    for item in line.split("=", 1)[1].split(",")
                )
            }
    if not work or set(work) != set(invocations):
        raise BuildError("reference phase markers are incomplete")
    return work, invocations, duplicate_policy, state_shape


def _combined_input_sha256(inputs, names):
    digest = hashlib.sha256()
    for name in names:
        digest.update(name.encode("utf-8") + b"\0")
        digest.update(bytes.fromhex(inputs[name]["sha256"]))
    return digest.hexdigest()


def _mcf_initial_memory(path):
    payload = Path(path).read_bytes()
    header = struct.Struct("<8sQQQQQ")
    if len(payload) < header.size:
        raise BuildError("MCF input is shorter than its header")
    magic, nodes, arcs, pricing_calls, pricing_items, price_out_calls = (
        header.unpack_from(payload)
    )
    if magic != b"MCFREG1\0":
        raise BuildError("MCF input magic differs")
    offset = header.size

    def image(name, base, count):
        nonlocal offset
        byte_count = count * 8
        end = offset + byte_count
        if end > len(payload):
            raise BuildError(f"MCF input {name} image is truncated")
        words = struct.unpack_from(f"<{count}Q", payload, offset)
        offset = end
        return {
            "logical_base": base, "word_bits": 64, "words": words,
        }

    result = {"arcs": image("arcs", MCF_BASES["arc"], arcs * 4)}
    for name in ("potential", "predecessor", "depth", "orientation", "tree"):
        result[name] = image(name, MCF_BASES[name], nodes)
    result["objective"] = {
        "logical_base": MCF_BASES["objective"],
        "word_bits": 64,
        "words": (0,),
    }
    result["pricing_offsets"] = image(
        "pricing offsets", MCF_BASES["pricing_offsets"], pricing_calls + 1
    )
    result["pricing_index"] = image(
        "pricing index", MCF_BASES["pricing_index"], pricing_items
    )
    result["price_out_index"] = image(
        "price-out index", MCF_BASES["price_out_index"], price_out_calls
    )
    if offset != len(payload):
        raise BuildError("MCF input has trailing state bytes")
    return result


def _spatter_initial_memory(*, values_path, index_path, output_count):
    values = _read_words(values_path, 32)
    index = _read_words(index_path, 64)
    return {
        "values": {
            "logical_base": SPATTER_BASES["values"],
            "word_bits": 32,
            "words": values,
        },
        "index": {
            "logical_base": SPATTER_BASES["index"],
            "word_bits": 64,
            "words": index,
        },
        "destination": {
            "logical_base": SPATTER_BASES["destination"],
            "word_bits": 32,
            "words": (0,) * output_count,
        },
    }


def _boundary_probes(workload, operations, outputs, state_shape):
    if workload != "mcf":
        after = operations[-1].sequence if operations else 0
        return {
            "destination": [
                {"address": SPATTER_BASES["destination"] + 4 * index,
                 "after_sequence": after}
                for index in range(len(outputs["destination"]))
            ]
        }
    if not isinstance(state_shape, dict):
        raise BuildError("MCF state shape is missing")
    commits = [
        operation.sequence for operation in operations
        if operation.phase == 2 and operation.opcode == canonical.Opcode.COMMIT
    ]
    if len(commits) != len(outputs["objective"]):
        raise BuildError("MCF objective/COMMIT boundary count differs")
    arcs = state_shape["arcs"]
    nodes = state_shape["nodes"]
    result = {name: [] for name in MCF_OUTPUTS}
    for after in commits:
        result["objective"].append({
            "address": MCF_BASES["objective"], "after_sequence": after,
        })
        for index in range(arcs):
            result["flow"].append({
                "address": MCF_BASES["arc"] + index * 32 + 24,
                "after_sequence": after,
            })
            result["cost"].append({
                "address": MCF_BASES["arc"] + index * 32 + 16,
                "after_sequence": after,
            })
        for name in ("potential", "predecessor", "depth", "orientation", "tree"):
            result[name].extend(
                {"address": MCF_BASES[name] + index * 8,
                 "after_sequence": after}
                for index in range(nodes)
            )
    return result


def _write_reference_bundle(
    *, bundle_root, workload, phases, input_sha256, binary_row,
    manifest_sha256, trace_path, output_paths, stdout,
    initial_memory=None,
):
    operations = canonical.decode_operations(Path(trace_path).read_bytes())
    work, invocations, duplicate_policy, state_shape = _markers(stdout)
    outputs = {
        name: _read_words(path, bits)
        for name, (path, bits) in output_paths.items()
    }
    probes = _boundary_probes(workload, operations, outputs, state_shape)
    meta = {
        "schema": 1,
        "workload": workload,
        "input_sha256": input_sha256,
        "source_sha256": binary_row["source_sha256"],
        "binary_sha256": binary_row["sha256"],
        "config_sha256": manifest_sha256,
        "phases": list(phases),
        "phase_work": work,
        "phase_invocations": invocations,
        "output_boundaries": {
            name: {
                "word_bits": bits,
                "count": len(outputs[name]),
                "probes": probes[name],
            }
            for name, (_, bits) in output_paths.items()
        },
    }
    if duplicate_policy is not None:
        meta["duplicate_policy"] = duplicate_policy
    if state_shape is not None:
        meta["state_shape"] = state_shape
    canonical.write_bundle(
        bundle_root, meta, operations, outputs,
        initial_memory=initial_memory,
    )
    return Path(bundle_root).resolve()


def _run_mcf_reference(manifest, root):
    root.mkdir(parents=True)
    raw = root / "raw"
    raw.mkdir()
    trace_path = raw / "trace.bin"
    row = manifest["binaries"]["mcf:reference"]
    stdout = _run([
        row["path"], "--input", manifest["inputs"]["mcf"]["path"],
        "--output-root", raw, "--trace", trace_path,
    ], "MCF reference")
    return _write_reference_bundle(
        bundle_root=root / "bundle", workload="mcf",
        phases=("pricing_kernel", "price_out_impl"),
        input_sha256=manifest["inputs"]["mcf"]["sha256"],
        binary_row=row,
        manifest_sha256=_sha256_file(Path(manifest["root"]) / "manifest.json"),
        trace_path=trace_path,
        output_paths={name: (raw / f"{name}.u64", 64) for name in MCF_OUTPUTS},
        initial_memory=_mcf_initial_memory(
            manifest["inputs"]["mcf"]["path"]
        ),
        stdout=stdout,
    )


def _run_spatter_reference(manifest, root, *, kind, faulty=False):
    root.mkdir(parents=True)
    raw = root / "raw"
    raw.mkdir()
    prefix = "amg" if kind == "gather" else "lulesh"
    workload = "amg_gather" if kind == "gather" else "lulesh_scatter"
    phase = workload
    row = manifest["binaries"]["spatter:reference"]
    command = [
        row["path"], "--kind", kind,
        "--values", manifest["inputs"][f"{prefix}_values"]["path"],
        "--index", manifest["inputs"][f"{prefix}_index"]["path"],
        "--destination", raw / "destination.u32",
        "--trace", raw / "trace.bin",
    ]
    if faulty:
        command.append("--reverse-duplicate-stores")
    stdout = _run(command, f"{workload} reference")
    return _write_reference_bundle(
        bundle_root=root / "bundle", workload=workload, phases=(phase,),
        input_sha256=_combined_input_sha256(
            manifest["inputs"], (f"{prefix}_values", f"{prefix}_index")
        ),
        binary_row=row,
        manifest_sha256=_sha256_file(Path(manifest["root"]) / "manifest.json"),
        trace_path=raw / "trace.bin",
        output_paths={"destination": (raw / "destination.u32", 32)},
        initial_memory=_spatter_initial_memory(
            values_path=manifest["inputs"][f"{prefix}_values"]["path"],
            index_path=manifest["inputs"][f"{prefix}_index"]["path"],
            output_count=len(_read_words(raw / "destination.u32", 32)),
        ),
        stdout=stdout,
    )


def run_fixture_references(manifest, outdir):
    if manifest.get("mode") != "fixture":
        raise BuildError("fixture reference execution requires a fixture build")
    outdir = Path(outdir).resolve()
    if outdir.exists():
        raise BuildError(f"fresh reference root required: {outdir}")
    outdir.mkdir(parents=True)
    return {
        "mcf": _run_mcf_reference(manifest, outdir / "mcf"),
        "amg_gather": _run_spatter_reference(
            manifest, outdir / "amg_gather", kind="gather"
        ),
        "lulesh_scatter": _run_spatter_reference(
            manifest, outdir / "lulesh_scatter", kind="scatter"
        ),
    }


def run_faulty_scatter_reversed_duplicates(manifest, outdir):
    if manifest.get("mode") != "fixture":
        raise BuildError("fault injection requires a fixture build")
    outdir = Path(outdir).resolve()
    if outdir.exists():
        raise BuildError(f"fresh faulty root required: {outdir}")
    outdir.mkdir(parents=True)
    return _run_spatter_reference(
        manifest, outdir / "lulesh_scatter", kind="scatter", faulty=True
    )


def verify_reference_bundle(reference, actual):
    expected = canonical.read_bundle(reference)
    observed = canonical.read_bundle(actual)
    if expected.meta["workload"] != observed.meta["workload"]:
        raise canonical.TraceError("workload identity differs")
    for field in (
        "input_sha256",
        "source_sha256",
        "binary_sha256",
        "config_sha256",
        "phases",
        "phase_work",
        "phase_invocations",
        "duplicate_policy",
        "state_shape",
    ):
        if expected.meta.get(field) != observed.meta.get(field):
            raise canonical.TraceError(f"{field} identity differs")
    if set(expected.outputs) != set(observed.outputs):
        raise canonical.TraceError("output boundary set differs")
    for name in expected.outputs:
        bits = expected.meta["output_boundaries"][name]["word_bits"]
        canonical.compare_words(
            expected.outputs[name], observed.outputs[name], name,
            word_bits=bits,
        )
    canonical.validate_translation(expected.operations, observed.operations)
    return True


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--fixture", action="store_true")
    mode.add_argument("--formal", action="store_true")
    mode.add_argument("--formal-npb", action="store_true")
    parser.add_argument("--synthetic", action="store_true")
    parser.add_argument("--inputs", type=Path)
    parser.add_argument("--outdir", type=Path, required=True)
    parser.add_argument("--cxx", default="g++")
    return parser.parse_args(argv)


def main(argv=None):
    options = parse_args(argv)
    try:
        if options.formal_npb:
            if options.synthetic:
                raise BuildError("formal NPB mode rejects synthetic inputs")
            if options.inputs is None:
                raise BuildError("formal NPB mode requires --inputs")
            manifest = build_and_run_npb_formal(options.inputs, options.outdir)
            print(
                f"MATCHED_NPB_FORMAL_PASS manifest={manifest['source_commit']}"
            )
            return 0
        validate_mode(
            formal=options.formal,
            fixture=options.fixture,
            synthetic=options.synthetic,
        )
        if options.formal:
            if options.inputs is None:
                raise BuildError("formal mode requires --inputs")
            load_formal_inputs(options.inputs)
            raise BuildError(
                "formal MCF is failed_input: the frozen SPEC MCF source and "
                "345 MB input are not available to the exact instrumentation path"
            )
        else:
            if options.inputs is not None:
                raise BuildError("fixture mode rejects --inputs")
            manifest = build_fixture_suite(options.outdir, cxx=options.cxx)
        print(f"MATCHED_BREADTH_BUILD_PASS manifest={manifest['root']}/manifest.json")
        return 0
    except (BuildError, OSError) as error:
        print(f"MATCHED_BREADTH_BUILD_FAILED error={error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
