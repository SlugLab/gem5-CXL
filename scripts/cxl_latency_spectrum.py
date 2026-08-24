#!/usr/bin/env python3
# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

"""Canonical CXL link-latency labels used by formal experiments."""


LABELS = ("200ns", "500ns", "1us", "2us")
TICKS = dict(zip(LABELS, (200_000, 500_000, 1_000_000, 2_000_000)))


class LatencyError(RuntimeError):
    """A latency label is outside the formal experiment contract."""


def ticks(label):
    try:
        return TICKS[label]
    except (KeyError, TypeError) as error:
        raise LatencyError(f"unsupported CXL latency: {label}") from error
