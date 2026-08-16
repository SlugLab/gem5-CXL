#!/usr/bin/env python3
# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

"""Frozen CIRA rolling-window policy for formal PageRank experiments."""

from dataclasses import asdict

try:
    from scripts import cira_hoist_model
except ImportError:
    import cira_hoist_model


ROW_BLOCK_SIZE = 64
CANDIDATE_1US_LEADS = (1, 2, 4, 8)
SOURCE_CANDIDATES = {
    "A": {
        "name": "static-default",
        "row_window_rows": 64,
        "lead_blocks": 1,
    },
    "B": {
        "name": "row-window-2048",
        "row_window_rows": 2048,
        "lead_blocks": 32,
    },
    "C": {
        "name": "row-window-1024",
        "row_window_rows": 1024,
        "lead_blocks": 16,
    },
}
CALIBRATED_1US_LEADS = tuple(
    sorted({spec["lead_blocks"] for spec in SOURCE_CANDIDATES.values()})
)
ALL_1US_LEADS = tuple(sorted(set(CANDIDATE_1US_LEADS + CALIBRATED_1US_LEADS)))


class LeadPolicyError(ValueError):
    pass


def _source_rows(calibration):
    try:
        rows = calibration["sources"]["cira_csv"]["rows"]["pr_spmv"]
    except (KeyError, TypeError) as error:
        raise LeadPolicyError("calibration has no pr_spmv source rows") from error
    if not isinstance(rows, dict):
        raise LeadPolicyError("calibration pr_spmv rows are invalid")
    return rows


def _require_verified_row(rows, source_row):
    try:
        row = rows[source_row]
    except KeyError as error:
        raise LeadPolicyError(f"missing source row {source_row}") from error
    if row.get("verification") != "PASS" or row.get("return_code") != 0:
        raise LeadPolicyError(f"source row {source_row} is not verified")
    return row


def _hoist_candidate(source_row, *, cxl_latency_ns):
    spec = SOURCE_CANDIDATES[source_row]
    return cira_hoist_model.HoistCandidate(
        name=source_row,
        operands_dominate=True,
        guards_available=True,
        alias_safe=True,
        invalidation_safe=True,
        lifetime_safe=True,
        available_slack_ns=cxl_latency_ns + 200,
        issue_ns=5,
        index_walk_ns=80,
        queue_wait_ns=20,
        cxl_memory_ns=cxl_latency_ns,
        cache_install_ns=40,
        expected_saved_stall_ns=400,
        usefulness_probability=0.75,
        descriptor_formation_ns=20,
        runtime_guards_ns=10,
        selection_cost_ns=0,
        extra_traffic_ns=40,
        cache_pollution_ns=10,
        late_request_ns=20,
        lead_rows=spec["row_window_rows"],
    )


def resolve_mode(
    calibration, mode, *, source_row=None, cxl_latency_ns=1000
):
    """Resolve one mode from approved hardware rows and gate its hoist."""

    if mode not in {"static", "pgo-selected", "few-shot-online"}:
        raise LeadPolicyError(f"unknown CIRA mode {mode}")
    if not isinstance(cxl_latency_ns, int) or cxl_latency_ns <= 0:
        raise LeadPolicyError("CXL latency must be a positive integer")
    rows = _source_rows(calibration)
    if mode == "static":
        if source_row not in {None, "A"}:
            raise LeadPolicyError("static mode is fixed to source row A")
        selected_row = "A"
    elif mode == "pgo-selected":
        if source_row is not None:
            raise LeadPolicyError("PGO source row cannot be overridden")
        candidates = {
            name: _hoist_candidate(name, cxl_latency_ns=cxl_latency_ns)
            for name in SOURCE_CANDIDATES
        }
        try:
            selected_row = cira_hoist_model.select_pgo(candidates, rows).name
        except cira_hoist_model.PolicyError as error:
            raise LeadPolicyError(str(error)) from error
        try:
            declared = calibration["cira"]["primary"]["selected_source_mode"]
        except (KeyError, TypeError) as error:
            raise LeadPolicyError("calibration has no selected source mode") from error
        if declared != selected_row:
            raise LeadPolicyError(
                f"PGO selected source {selected_row} differs from manifest {declared}"
            )
    else:
        if source_row not in SOURCE_CANDIDATES:
            raise LeadPolicyError(
                "few-shot-online requires source row A, B, or C"
            )
        selected_row = source_row

    source_evidence = _require_verified_row(rows, selected_row)
    spec = SOURCE_CANDIDATES[selected_row]
    if spec["row_window_rows"] % ROW_BLOCK_SIZE:
        raise LeadPolicyError("source row window is not divisible by 64 rows")
    if spec["lead_blocks"] != spec["row_window_rows"] // ROW_BLOCK_SIZE:
        raise LeadPolicyError("source row lead differs from its row window")
    candidate = _hoist_candidate(selected_row, cxl_latency_ns=cxl_latency_ns)
    resources = cira_hoist_model.ResourceState(
        descriptor_queue_free=1,
        csr_walk_queue_free=1,
        outstanding_reads_free=1,
        destination_ports_free=1,
        mshrs_free=1,
        max_lead_rows=spec["row_window_rows"],
    )
    decision = cira_hoist_model.evaluate(candidate, resources)
    if not decision.emit_prefetch:
        raise LeadPolicyError(
            f"CIRA {mode}/{selected_row} hoist rejected: {decision.reason}"
        )
    return {
        "mode": mode,
        "source_row": selected_row,
        **spec,
        "hardware_mean_time_ms": source_evidence.get("mean_time_ms"),
        "hardware_speedup_mean": source_evidence.get("speedup_mean"),
        "hoist_decision": asdict(decision),
    }


def lead_blocks_for_latency(selected_1us, latency_ns):
    if selected_1us not in ALL_1US_LEADS:
        raise LeadPolicyError("1us lead is outside the qualification set")
    if latency_ns <= 0:
        raise LeadPolicyError("latency must be positive")
    return max(1, (selected_1us * latency_ns + 999) // 1000)


def select_1us_lead(candidate_rows):
    for lead in CANDIDATE_1US_LEADS:
        try:
            row = candidate_rows[lead]
            qualifies = (
                int(row["queue_rejections"]) == 0
                and int(row["dropped_descriptors"]) == 0
                and int(row["useful_prefetches"])
                > int(row["late_prefetches"])
            )
        except (KeyError, TypeError, ValueError) as error:
            raise LeadPolicyError(
                f"lead {lead} has incomplete qualification evidence"
            ) from error
        if qualifies:
            return lead
    raise LeadPolicyError("no 1us CIRA lead passed qualification")


def static_partition(total_rows, num_threads, thread_id):
    if total_rows < 0:
        raise LeadPolicyError("row count must be non-negative")
    if num_threads <= 0:
        raise LeadPolicyError("thread count must be positive")
    if thread_id < 0 or thread_id >= num_threads:
        raise LeadPolicyError("thread id is outside the team")
    rows_per_thread, extra_rows = divmod(total_rows, num_threads)
    begin = (
        thread_id * rows_per_thread + min(thread_id, extra_rows)
    )
    end = begin + rows_per_thread + (1 if thread_id < extra_rows else 0)
    return begin, end


def owner_for_row(total_rows, num_threads, row):
    if row < 0 or row >= total_rows:
        raise LeadPolicyError("row is outside the graph")
    for thread_id in range(num_threads):
        begin, end = static_partition(total_rows, num_threads, thread_id)
        if begin <= row < end:
            return thread_id
    raise LeadPolicyError("row has no static-partition owner")


def effective_lead_for_scale(
    scale, *, num_threads, calibrated_lead_blocks
):
    if scale < 0 or num_threads <= 0 or calibrated_lead_blocks <= 0:
        raise LeadPolicyError("scale-aware lead inputs must be positive")
    total_rows = 1 << scale
    spans = [
        end - begin
        for begin, end in (
            static_partition(total_rows, num_threads, thread_id)
            for thread_id in range(num_threads)
        )
    ]
    minimum = min(spans)
    calibrated_rows = calibrated_lead_blocks * ROW_BLOCK_SIZE
    if minimum < 2 * ROW_BLOCK_SIZE:
        effective_rows = 1
        effective_blocks = None
        batch_rows = 1
        fallback = True
    else:
        half_aligned = (
            minimum // 2 // ROW_BLOCK_SIZE
        ) * ROW_BLOCK_SIZE
        effective_rows = min(calibrated_rows, half_aligned)
        effective_blocks = effective_rows // ROW_BLOCK_SIZE
        batch_rows = ROW_BLOCK_SIZE
        fallback = False
    return {
        "graph_scale": scale,
        "total_rows": total_rows,
        "num_threads": num_threads,
        "minimum_thread_rows": minimum,
        "calibrated_rows": calibrated_rows,
        "calibrated_blocks": calibrated_lead_blocks,
        "effective_rows": effective_rows,
        "effective_blocks": effective_blocks,
        "batch_rows": batch_rows,
        "correctness_fallback": fallback,
    }


def future_window(
    total_rows,
    num_threads,
    thread_id,
    current,
    effective_rows,
    batch_rows,
):
    if effective_rows <= 0 or batch_rows <= 0:
        raise LeadPolicyError("lead and batch must be positive row counts")
    thread_begin, thread_end = static_partition(
        total_rows, num_threads, thread_id
    )
    if current < thread_begin or current >= thread_end:
        raise LeadPolicyError("current row is not owned by the issuing thread")
    if batch_rows != 1 and (current - thread_begin) % ROW_BLOCK_SIZE:
        return None
    first = current + effective_rows
    if first >= thread_end:
        return None
    return first, min(batch_rows, thread_end - first)


def future_block(total_rows, num_threads, thread_id, current, lead_blocks):
    if lead_blocks <= 0:
        raise LeadPolicyError("lead must be a positive whole block count")
    return future_window(
        total_rows,
        num_threads,
        thread_id,
        current,
        lead_blocks * ROW_BLOCK_SIZE,
        ROW_BLOCK_SIZE,
    )
