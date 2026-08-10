#!/usr/bin/env python3
# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

"""Frozen CIRA rolling-window policy for formal PageRank experiments."""


ROW_BLOCK_SIZE = 64
CANDIDATE_1US_LEADS = (1, 2, 4, 8)


class LeadPolicyError(ValueError):
    pass


def lead_blocks_for_latency(selected_1us, latency_ns):
    if selected_1us not in CANDIDATE_1US_LEADS:
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


def future_block(total_rows, num_threads, thread_id, current, lead_blocks):
    if lead_blocks <= 0:
        raise LeadPolicyError("lead must be a positive whole block count")
    thread_begin, thread_end = static_partition(
        total_rows, num_threads, thread_id
    )
    if current < thread_begin or current >= thread_end:
        raise LeadPolicyError("current row is not owned by the issuing thread")
    if (current - thread_begin) % ROW_BLOCK_SIZE:
        return None
    first = current + lead_blocks * ROW_BLOCK_SIZE
    if first >= thread_end:
        return None
    return first, min(ROW_BLOCK_SIZE, thread_end - first)
