#!/usr/bin/env python3
# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

"""Render the validated GAPBS g20 paper figure."""

import dataclasses
import math
import re


SYSTEMS = ("Vanilla CXL", "AMU", "CIRA", "M2NDP")
LATENCY_KEYS = ("200ns", "500ns", "1us", "2us")
LATENCY_NS = (200, 500, 1000, 2000)
SENSITIVITY_SERIES = ("AMU", "CIRA")


class FigureDataError(ValueError):
    """Validated table data cannot satisfy the chart contract."""


@dataclasses.dataclass(frozen=True)
class FigureData:
    systems: tuple
    formal_latencies: tuple
    formal_speedups: tuple
    latency_ns: tuple
    sensitivity: dict
    evidence_sha256: str
    panel_a_scale: str


def _positive_float(value, context):
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as error:
        raise FigureDataError(
            f"{context} must be finite and positive"
        ) from error
    if not math.isfinite(number) or number <= 0:
        raise FigureDataError(
            f"{context} must be finite and positive"
        )
    return number


def choose_latency_scale(values):
    numbers = tuple(
        _positive_float(value, "formal latencies") for value in values
    )
    if not numbers:
        raise FigureDataError(
            "formal latencies must be finite and positive"
        )
    return "log" if max(numbers) / min(numbers) >= 10.0 else "linear"


def prepare_figure_data(rows, sensitivity, *, evidence_sha256):
    if not re.fullmatch(r"[0-9a-f]{64}", evidence_sha256):
        raise FigureDataError("evidence SHA-256 is invalid")

    rows = tuple(rows)
    if tuple(getattr(row, "system", None) for row in rows) != SYSTEMS:
        raise FigureDataError("formal systems are absent or out of order")

    formal_latencies = tuple(
        _positive_float(row.latency_seconds, f"{row.system} latency")
        for row in rows
    )
    formal_speedups = tuple(
        _positive_float(row.speedup, f"{row.system} speedup")
        for row in rows
    )

    if not isinstance(sensitivity, dict) or set(sensitivity) != set(
        LATENCY_KEYS
    ):
        raise FigureDataError("sensitivity latency keys are incomplete")

    series_values = {}
    for series in SENSITIVITY_SERIES:
        values = []
        for latency in LATENCY_KEYS:
            context = f"{latency}/Geo./{series}"
            try:
                value = sensitivity[latency]["Geo."][series]
            except (KeyError, TypeError) as error:
                raise FigureDataError(f"missing {context}") from error
            values.append(_positive_float(value, context))
        series_values[series] = tuple(values)

    return FigureData(
        systems=SYSTEMS,
        formal_latencies=formal_latencies,
        formal_speedups=formal_speedups,
        latency_ns=LATENCY_NS,
        sensitivity=series_values,
        evidence_sha256=evidence_sha256,
        panel_a_scale=choose_latency_scale(formal_latencies),
    )
