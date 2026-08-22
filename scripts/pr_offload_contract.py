#!/usr/bin/env python3
# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

"""Shared contracts for formal PageRank near-data offload."""


PR_ROW_DESC_BYTES = 104
FORMAL_THREADS = 4
FORMAL_ITERATIONS = 20


def static_partition(rows, workers, worker):
    """Return the half-open contiguous row range owned by one worker."""

    if (
        isinstance(rows, bool)
        or not isinstance(rows, int)
        or rows < 0
        or isinstance(workers, bool)
        or not isinstance(workers, int)
        or workers <= 0
        or isinstance(worker, bool)
        or not isinstance(worker, int)
        or worker < 0
        or worker >= workers
    ):
        raise ValueError("invalid PR static partition")
    quotient, remainder = divmod(rows, workers)
    begin = worker * quotient + min(worker, remainder)
    end = begin + quotient + (1 if worker < remainder else 0)
    return begin, end
