# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

"""Strict reader and deterministic writer for the MCFREG2 container."""

from __future__ import annotations

import dataclasses
import gzip
import hashlib
import io
import json
import os
import shutil
import struct
import tempfile
from pathlib import Path
from typing import Iterable, Mapping


MAGIC = b"MCFREG2\0"
SCHEMA = 3
ENDIAN_TAG = 0x0102
HEADER = struct.Struct("<8sHHI11Q")
DIRECTORY = struct.Struct("<HHIQQQQ32s")
STABLE_REF = struct.Struct("<IIQ")
OPTIONAL_FLAG = 1
UINT64_MAX = (1 << 64) - 1

SECTION_TYPES = {
    "PROVENANCE": 1,
    "NETWORK": 2,
    "NODES": 3,
    "ARCS": 4,
    "BASKET": 5,
    "CALL_INDEX": 6,
    "EVENTS": 7,
    "DELTAS": 8,
    "BOUNDARIES": 9,
    "FINAL": 10,
}
SECTION_NAMES = {value: name for name, value in SECTION_TYPES.items()}
REQUIRED_SECTIONS = tuple(SECTION_TYPES)

OBJECT_NULL = 0
OBJECT_NODE = 1
OBJECT_ARC = 2
OBJECT_DUMMY_ARC = 3
NULL_OBJECT_ID = UINT64_MAX


class FormatError(RuntimeError):
    """An MCFREG2 input violates the binary contract."""


@dataclasses.dataclass(frozen=True)
class Header:
    magic: bytes
    schema: int
    endian_tag: int
    header_bytes: int
    flags: int
    section_count: int
    directory_offset: int
    nodes: int
    active_arcs: int
    dummy_arcs: int
    arena_capacity: int
    pricing_calls: int
    price_out_calls: int
    event_count: int
    reserved: int


@dataclasses.dataclass(frozen=True)
class DirectoryEntry:
    section_type: int
    schema: int
    flags: int
    offset: int
    stored_bytes: int
    element_count: int
    element_size: int
    sha256: bytes


@dataclasses.dataclass(frozen=True)
class SectionView:
    path: Path
    offset: int
    stored_bytes: int

    def chunks(self, chunk_bytes=1024 * 1024):
        if chunk_bytes <= 0:
            raise FormatError("MCFREG2 section chunk size is invalid")
        with self.path.open("rb") as stream:
            stream.seek(self.offset)
            remaining = self.stored_bytes
            while remaining:
                chunk = stream.read(min(remaining, chunk_bytes))
                if not chunk:
                    raise FormatError("MCFREG2 section is truncated")
                remaining -= len(chunk)
                yield chunk

    def read(self) -> bytes:
        return b"".join(self.chunks())


class _DigestingSectionReader(io.RawIOBase):
    """Bounded raw reader which authenticates the stored section bytes."""

    def __init__(self, path: Path, entry: DirectoryEntry):
        super().__init__()
        self._entry = entry
        self._remaining = entry.stored_bytes
        self._digest = hashlib.sha256()
        try:
            self._stream = Path(path).open("rb")
            self._stream.seek(entry.offset)
        except OSError as error:
            raise FormatError(
                f"cannot open MCFREG2 EVENTS section: {error}"
            ) from error

    def readable(self):
        return True

    def readinto(self, buffer):
        if self.closed:
            raise ValueError("I/O operation on closed EVENTS section")
        if self._remaining == 0:
            return 0
        size = min(len(buffer), self._remaining)
        try:
            data = self._stream.read(size)
        except OSError as error:
            raise FormatError(
                f"cannot read MCFREG2 EVENTS section: {error}"
            ) from error
        if not data:
            raise FormatError("MCFREG2 EVENTS section is truncated")
        buffer[:len(data)] = data
        self._digest.update(data)
        self._remaining -= len(data)
        return len(data)

    def finish(self, expected_sha256):
        """Drain unread stored bytes and require the directory digest."""

        scratch = bytearray(1024 * 1024)
        while self._remaining:
            self.readinto(scratch)
        if self._digest.digest() != expected_sha256:
            raise FormatError("MCFREG2 EVENTS SHA-256 differs")

    def close(self):
        stream = getattr(self, "_stream", None)
        if stream is not None:
            stream.close()
        super().close()


@dataclasses.dataclass(frozen=True)
class Section:
    section_type: int
    schema: int
    flags: int
    element_count: int
    element_size: int
    data: bytes | Path | SectionView

    def __post_init__(self):
        if isinstance(self.data, (Path, SectionView)):
            path = self.data if isinstance(self.data, Path) else self.data.path
            if not path.is_file():
                raise TypeError("MCFREG2 file-backed section is missing")
            return
        if not isinstance(self.data, bytes):
            raise TypeError(
                "MCFREG2 section data must be bytes, a Path, or a SectionView"
            )


@dataclasses.dataclass(frozen=True)
class StableRef:
    kind: int
    generation: int
    object_id: int

    @classmethod
    def null(cls) -> "StableRef":
        return cls(OBJECT_NULL, 0, NULL_OBJECT_ID)

    def pack(self) -> bytes:
        return STABLE_REF.pack(self.kind, self.generation, self.object_id)


@dataclasses.dataclass(frozen=True)
class BasketState:
    slot: int
    arc: StableRef
    cost: int
    abs_cost: int


@dataclasses.dataclass(frozen=True)
class PricingScanLiveIn:
    scan_position: int
    arc: StableRef
    tail: StableRef
    head: StableRef
    cost: int
    ident: int
    tail_potential: int
    head_potential: int


@dataclasses.dataclass(frozen=True)
class PricingCandidate:
    scan_position: int
    reduced_cost: int
    candidate: bool
    basket_slot: int


@dataclasses.dataclass(frozen=True)
class PricingLiveIn:
    ordinal: int
    m: int
    nr_group: int
    group_pos: int
    initialize: bool
    basket: tuple[BasketState, ...]
    scans: tuple[PricingScanLiveIn, ...]


@dataclasses.dataclass(frozen=True)
class PricingDerivedOut:
    ordinal: int
    candidates: tuple[PricingCandidate, ...]
    basket: tuple[BasketState, ...]
    selected_arc: StableRef
    selected_reduced_cost: int
    arcs_priced: int
    nr_group: int
    group_pos: int
    initialize: bool


@dataclasses.dataclass(frozen=True)
class ObjectState:
    reference: StableRef
    words: tuple[int, ...]
    links: tuple[StableRef, ...]


@dataclasses.dataclass(frozen=True)
class PriceOutCandidate:
    candidate: int
    tail: StableRef
    head: StableRef
    cost: int
    reduced_cost: int


@dataclasses.dataclass(frozen=True)
class PriceOutDecision:
    candidate: int
    decision: str
    reference: StableRef


@dataclasses.dataclass(frozen=True)
class PriceOutLiveIn:
    ordinal: int
    network_words: tuple[int, ...]
    objects: tuple[ObjectState, ...]
    arena_generation: int
    arena_capacity: int
    heap: tuple[StableRef, ...]


@dataclasses.dataclass(frozen=True)
class PriceOutDerivedOut:
    ordinal: int
    network_words: tuple[int, ...]
    objects: tuple[ObjectState, ...]
    arena_generation: int
    arena_capacity: int
    heap: tuple[StableRef, ...]
    candidates: tuple[PriceOutCandidate, ...]
    decisions: tuple[PriceOutDecision, ...]


CanonicalCallState = (
    PricingLiveIn | PricingDerivedOut | PriceOutLiveIn | PriceOutDerivedOut
)


def _append_u8(output, value):
    if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value < 256:
        raise FormatError("MCFREG2 canonical u8 is invalid")
    output.extend(struct.pack("<B", value))


def _append_u32(output, value):
    if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value < 1 << 32:
        raise FormatError("MCFREG2 canonical u32 is invalid")
    output.extend(struct.pack("<I", value))


def _append_u64(output, value):
    if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= UINT64_MAX:
        raise FormatError("MCFREG2 canonical u64 is invalid")
    output.extend(struct.pack("<Q", value))


def _append_i64(output, value):
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or not -(1 << 63) <= value < 1 << 63
    ):
        raise FormatError("MCFREG2 canonical i64 is invalid")
    output.extend(struct.pack("<q", value))


def _append_bool(output, value):
    if not isinstance(value, bool):
        raise FormatError("MCFREG2 canonical boolean is invalid")
    _append_u8(output, int(value))


def _append_ref(output, reference):
    if not isinstance(reference, StableRef):
        raise FormatError("MCFREG2 canonical stable reference is invalid")
    _append_u32(output, reference.kind)
    _append_u32(output, reference.generation)
    _append_u64(output, reference.object_id)


def _append_refs(output, values):
    _append_u64(output, len(values))
    for value in values:
        _append_ref(output, value)


def _append_basket(output, values):
    _append_u64(output, len(values))
    for value in values:
        _append_u64(output, value.slot)
        _append_ref(output, value.arc)
        _append_i64(output, value.cost)
        _append_i64(output, value.abs_cost)


def _ref_key(reference):
    return (reference.kind, reference.generation, reference.object_id)


def _append_objects(output, values):
    ordered = sorted(values, key=lambda value: _ref_key(value.reference))
    if len({_ref_key(value.reference) for value in ordered}) != len(ordered):
        raise FormatError("MCFREG2 canonical object reference is duplicated")
    _append_u64(output, len(ordered))
    for value in ordered:
        _append_ref(output, value.reference)
        _append_u64(output, len(value.words))
        for word in value.words:
            _append_i64(output, word)
        _append_refs(output, value.links)


def encode_call_state(state: CanonicalCallState) -> bytes:
    output = bytearray(b"MCFCS3\0\0")
    if isinstance(state, PricingLiveIn):
        _append_u8(output, 1)
        _append_u64(output, state.ordinal)
        _append_u64(output, state.m)
        _append_u64(output, state.nr_group)
        _append_u64(output, state.group_pos)
        _append_bool(output, state.initialize)
        _append_basket(output, state.basket)
        _append_u64(output, len(state.scans))
        for scan in state.scans:
            _append_u64(output, scan.scan_position)
            _append_ref(output, scan.arc)
            _append_ref(output, scan.tail)
            _append_ref(output, scan.head)
            _append_i64(output, scan.cost)
            _append_i64(output, scan.ident)
            _append_i64(output, scan.tail_potential)
            _append_i64(output, scan.head_potential)
    elif isinstance(state, PricingDerivedOut):
        _append_u8(output, 2)
        _append_u64(output, state.ordinal)
        _append_u64(output, len(state.candidates))
        for candidate in state.candidates:
            _append_u64(output, candidate.scan_position)
            _append_i64(output, candidate.reduced_cost)
            _append_bool(output, candidate.candidate)
            _append_i64(output, candidate.basket_slot)
        _append_basket(output, state.basket)
        _append_ref(output, state.selected_arc)
        _append_i64(output, state.selected_reduced_cost)
        _append_u64(output, state.arcs_priced)
        _append_u64(output, state.nr_group)
        _append_u64(output, state.group_pos)
        _append_bool(output, state.initialize)
    elif isinstance(state, (PriceOutLiveIn, PriceOutDerivedOut)):
        _append_u8(output, 3 if isinstance(state, PriceOutLiveIn) else 4)
        _append_u64(output, state.ordinal)
        _append_u64(output, len(state.network_words))
        for word in state.network_words:
            _append_i64(output, word)
        _append_objects(output, state.objects)
        _append_u32(output, state.arena_generation)
        _append_u64(output, state.arena_capacity)
        _append_refs(output, state.heap)
        if isinstance(state, PriceOutDerivedOut):
            _append_u64(output, len(state.candidates))
            for candidate in state.candidates:
                _append_u64(output, candidate.candidate)
                _append_ref(output, candidate.tail)
                _append_ref(output, candidate.head)
                _append_i64(output, candidate.cost)
                _append_i64(output, candidate.reduced_cost)
            _append_u64(output, len(state.decisions))
            decisions = {"NO_CHANGE": 0, "INSERT": 1, "REPLACE": 2}
            for decision in state.decisions:
                _append_u64(output, decision.candidate)
                if decision.decision not in decisions:
                    raise FormatError("MCFREG2 canonical decision is invalid")
                _append_u8(output, decisions[decision.decision])
                _append_ref(output, decision.reference)
    else:
        raise FormatError("MCFREG2 canonical call state type is invalid")
    return bytes(output)


def digest_call_state(state: CanonicalCallState) -> str:
    return hashlib.sha256(encode_call_state(state)).hexdigest()


@dataclasses.dataclass(frozen=True)
class SemanticFrame:
    call: int
    order: int
    ordinal: int
    phase: str
    live_in_roles: frozenset[str]
    result_roles: frozenset[str]
    live_in_state: CanonicalCallState
    observed_state: CanonicalCallState


@dataclasses.dataclass(frozen=True)
class Package:
    header: Header
    directory: tuple[DirectoryEntry, ...]
    sections: tuple[Section, ...]

    def section_names(self) -> tuple[str, ...]:
        return tuple(
            SECTION_NAMES[section.section_type]
            for section in self.sections
            if section.section_type in SECTION_NAMES
        )

    def section_by_type(self, section_type: int) -> bytes:
        for section in self.sections:
            if section.section_type == section_type:
                if isinstance(section.data, bytes):
                    return section.data
                if isinstance(section.data, SectionView):
                    return section.data.read()
                if isinstance(section.data, Path):
                    return section.data.read_bytes()
        raise FormatError(f"MCFREG2 section {section_type} is absent")

    def section(self, name: str) -> bytes:
        try:
            section_type = SECTION_TYPES[name]
        except KeyError as error:
            raise FormatError(f"unknown MCFREG2 section name: {name}") from error
        return self.section_by_type(section_type)

    def with_section(self, section: Section) -> "Package":
        if any(
            current.section_type == section.section_type
            for current in self.sections
        ):
            raise FormatError(
                f"duplicate MCFREG2 section type {section.section_type}"
            )
        return dataclasses.replace(
            self, sections=tuple((*self.sections, section))
        )

    def directory_json(self):
        return [
            {
                "section_type": entry.section_type,
                "schema": entry.schema,
                "flags": entry.flags,
                "offset": entry.offset,
                "stored_bytes": entry.stored_bytes,
                "element_count": entry.element_count,
                "element_size": entry.element_size,
                "sha256": entry.sha256.hex(),
            }
            for entry in self.directory
        ]


_EVENT_SPECS = {
    "PRICING_SCAN_LIVE_IN": (
        "live_in",
        "pricing_scan",
        {"kind", "role", "call", "scan_position", "group_pos", "arc",
         "tail", "head", "cost", "ident", "tail_potential",
         "head_potential"},
    ),
    "PRICING_CANDIDATE_OBSERVED": (
        "observed_result",
        "candidate",
        {"kind", "role", "call", "scan_position", "reduced_cost",
         "candidate", "basket_slot"},
    ),
    "BASKET_LIVE_IN": (
        "live_in",
        "basket",
        {"kind", "role", "call", "slot", "arc", "cost", "abs_cost"},
    ),
    "BASKET_LIVE_OUT_OBSERVED": (
        "observed_result",
        "selection",
        {"kind", "role", "call", "slot", "arc", "cost", "abs_cost"},
    ),
    "PRICING_END_OBSERVED": (
        "observed_result",
        "selection",
        {"kind", "role", "call", "selected_arc", "selected_reduced_cost",
         "arcs_priced", "nr_group", "group_pos", "initialize"},
    ),
    "PRICE_OUT_STATE_LIVE_IN": (
        "live_in",
        "price_out_state",
        {"kind", "role", "call", "network_words", "objects",
         "arena_generation", "arena_capacity", "heap"},
    ),
    "PRICE_OUT_CANDIDATE_OBSERVED": (
        "observed_result",
        "candidate",
        {"kind", "role", "call", "candidate", "tail", "head", "cost",
         "reduced_cost"},
    ),
    "PRICE_OUT_DECISION_OBSERVED": (
        "observed_result",
        "decision",
        {"kind", "role", "call", "candidate", "decision", "reference"},
    ),
    "ARC_FINAL_OBSERVED": (
        "observed_result",
        "final_state",
        {"kind", "role", "call", "reference", "tail", "head", "cost",
         "org_cost", "flow", "ident", "nextout", "nextin"},
    ),
    "REMAP_OBSERVED": (
        "observed_result",
        "remap",
        {"kind", "role", "call", "old_reference", "new_reference"},
    ),
    "ADJACENCY_FINAL_OBSERVED": (
        "observed_result",
        "final_state",
        {"kind", "role", "call", "reference", "firstout", "firstin"},
    ),
    "PRICE_OUT_END_OBSERVED": (
        "observed_result",
        "final_state",
        {"kind", "role", "call", "network_words", "objects",
         "arena_generation", "arena_capacity", "heap"},
    ),
}

_REFERENCE_KINDS = {
    "null": OBJECT_NULL,
    "node": OBJECT_NODE,
    "arc": OBJECT_ARC,
    "dummy_arc": OBJECT_DUMMY_ARC,
}


def _semantic_json(payload, label):
    if not isinstance(payload, bytes):
        raise FormatError(f"MCFREG2 {label} section is lazy")
    if payload.startswith(b"\x1f\x8b"):
        try:
            payload = gzip.decompress(payload)
        except (OSError, EOFError) as error:
            raise FormatError(f"MCFREG2 {label} gzip stream is invalid") from error
        rows = []
        for number, line in enumerate(payload.splitlines(), start=1):
            try:
                row = json.loads(line)
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise FormatError(
                    f"MCFREG2 {label} row {number} is invalid"
                ) from error
            if not isinstance(row, dict):
                raise FormatError(
                    f"MCFREG2 {label} row {number} is not an object"
                )
            rows.append(row)
        if not rows:
            raise FormatError(f"MCFREG2 {label} rows are invalid")
        return rows
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise FormatError(f"MCFREG2 {label} JSON is invalid") from error
    if not isinstance(value, dict) or value.get("schema") != 3:
        raise FormatError(f"MCFREG2 {label} schema differs")
    rows = value.get("rows")
    if not isinstance(rows, list):
        raise FormatError(f"MCFREG2 {label} rows are invalid")
    return rows


def _semantic_events(payload):
    if not isinstance(payload, bytes):
        raise FormatError("MCFREG2 EVENTS section is lazy")
    if payload.startswith(b"\x1f\x8b"):
        try:
            payload = gzip.decompress(payload)
        except (OSError, EOFError) as error:
            raise FormatError("MCFREG2 EVENTS gzip stream is invalid") from error
    rows = []
    for number, line in enumerate(payload.splitlines(), start=1):
        try:
            row = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise FormatError(
                f"MCFREG2 EVENTS row {number} is invalid"
            ) from error
        if not isinstance(row, dict):
            raise FormatError(f"MCFREG2 EVENTS row {number} is not an object")
        rows.append(row)
    if not rows:
        raise FormatError("MCFREG2 EVENTS section is empty")
    return rows


def _stable_ref_from_json(value, label):
    if not isinstance(value, dict) or set(value) != {
        "kind", "generation", "index"
    }:
        raise FormatError(f"MCFREG2 {label} stable reference is invalid")
    kind = _REFERENCE_KINDS.get(value["kind"])
    generation = value["generation"]
    index = value["index"]
    if kind is None or not all(
        isinstance(item, int) and not isinstance(item, bool) and item >= 0
        for item in (generation, index)
    ):
        raise FormatError(f"MCFREG2 {label} stable reference is invalid")
    if kind == OBJECT_NULL and (generation != 0 or index != UINT64_MAX):
        raise FormatError(f"MCFREG2 {label} null reference is malformed")
    return StableRef(kind, generation, index)


def _integer(value, label, *, minimum=None):
    if not isinstance(value, int) or isinstance(value, bool):
        raise FormatError(f"MCFREG2 {label} is not an integer")
    if minimum is not None and value < minimum:
        raise FormatError(f"MCFREG2 {label} is out of range")
    return value


def _object_states(value, label):
    if not isinstance(value, list):
        raise FormatError(f"MCFREG2 {label} objects are invalid")
    result = []
    for row in value:
        if not isinstance(row, dict) or set(row) != {
            "reference", "words", "links"
        }:
            raise FormatError(f"MCFREG2 {label} object is invalid")
        words = row["words"]
        links = row["links"]
        if not isinstance(words, list) or not isinstance(links, list):
            raise FormatError(f"MCFREG2 {label} object fields are invalid")
        result.append(ObjectState(
            reference=_stable_ref_from_json(row["reference"], label),
            words=tuple(_integer(word, f"{label} word") for word in words),
            links=tuple(
                _stable_ref_from_json(link, label) for link in links
            ),
        ))
    return tuple(result)


def _refs(value, label):
    if not isinstance(value, list):
        raise FormatError(f"MCFREG2 {label} references are invalid")
    return tuple(_stable_ref_from_json(item, label) for item in value)


def _pricing_states(begin, rows):
    live_basket = []
    scans = []
    candidates = []
    result_basket = []
    ending = None
    for row in rows:
        kind = row["kind"]
        if kind == "BASKET_LIVE_IN":
            live_basket.append(BasketState(
                _integer(row["slot"], "basket slot", minimum=1),
                _stable_ref_from_json(row["arc"], "basket arc"),
                _integer(row["cost"], "basket cost"),
                _integer(row["abs_cost"], "basket absolute cost"),
            ))
        elif kind == "PRICING_SCAN_LIVE_IN":
            _integer(row["group_pos"], "scan group position", minimum=0)
            scans.append(PricingScanLiveIn(
                _integer(row["scan_position"], "scan position", minimum=0),
                _stable_ref_from_json(row["arc"], "scan arc"),
                _stable_ref_from_json(row["tail"], "scan tail"),
                _stable_ref_from_json(row["head"], "scan head"),
                _integer(row["cost"], "scan cost"),
                _integer(row["ident"], "scan ident"),
                _integer(row["tail_potential"], "tail potential"),
                _integer(row["head_potential"], "head potential"),
            ))
        elif kind == "PRICING_CANDIDATE_OBSERVED":
            if not isinstance(row["candidate"], bool):
                raise FormatError("MCFREG2 observed candidate is not boolean")
            candidates.append(PricingCandidate(
                _integer(row["scan_position"], "candidate position", minimum=0),
                _integer(row["reduced_cost"], "candidate reduced cost"),
                row["candidate"],
                _integer(row["basket_slot"], "candidate basket slot"),
            ))
        elif kind == "BASKET_LIVE_OUT_OBSERVED":
            result_basket.append(BasketState(
                _integer(row["slot"], "basket slot", minimum=1),
                _stable_ref_from_json(row["arc"], "basket arc"),
                _integer(row["cost"], "basket cost"),
                _integer(row["abs_cost"], "basket absolute cost"),
            ))
        elif kind == "PRICING_END_OBSERVED":
            if ending is not None:
                raise FormatError("MCFREG2 pricing end role is duplicated")
            ending = row
    if ending is None:
        raise FormatError("MCFREG2 pricing observed result is incomplete")
    if not isinstance(begin["initialize"], bool) or not isinstance(
        ending["initialize"], bool
    ):
        raise FormatError("MCFREG2 pricing initialize is not boolean")
    live_in = PricingLiveIn(
        ordinal=begin["ordinal"],
        m=_integer(begin["m"], "pricing m", minimum=0),
        nr_group=_integer(begin["nr_group"], "pricing group count", minimum=1),
        group_pos=_integer(begin["group_pos"], "pricing group position", minimum=0),
        initialize=begin["initialize"],
        basket=tuple(live_basket),
        scans=tuple(scans),
    )
    observed = PricingDerivedOut(
        ordinal=begin["ordinal"],
        candidates=tuple(candidates),
        basket=tuple(result_basket),
        selected_arc=_stable_ref_from_json(
            ending["selected_arc"], "selected arc"
        ),
        selected_reduced_cost=_integer(
            ending["selected_reduced_cost"], "selected reduced cost"
        ),
        arcs_priced=_integer(ending["arcs_priced"], "arcs priced", minimum=0),
        nr_group=_integer(ending["nr_group"], "pricing group count", minimum=1),
        group_pos=_integer(ending["group_pos"], "pricing group position", minimum=0),
        initialize=ending["initialize"],
    )
    return live_in, observed


def _price_out_states(begin, rows):
    state = None
    ending = None
    candidates = []
    decisions = []
    for row in rows:
        kind = row["kind"]
        if kind == "PRICE_OUT_STATE_LIVE_IN":
            if state is not None:
                raise FormatError("MCFREG2 price-out live-in role is duplicated")
            state = row
        elif kind == "PRICE_OUT_CANDIDATE_OBSERVED":
            candidates.append(PriceOutCandidate(
                _integer(row["candidate"], "price-out candidate", minimum=0),
                _stable_ref_from_json(row["tail"], "candidate tail"),
                _stable_ref_from_json(row["head"], "candidate head"),
                _integer(row["cost"], "candidate cost"),
                _integer(row["reduced_cost"], "candidate reduced cost"),
            ))
        elif kind == "PRICE_OUT_DECISION_OBSERVED":
            decisions.append(PriceOutDecision(
                _integer(row["candidate"], "price-out decision", minimum=0),
                row["decision"],
                _stable_ref_from_json(row["reference"], "decision reference"),
            ))
        elif kind == "PRICE_OUT_END_OBSERVED":
            if ending is not None:
                raise FormatError("MCFREG2 price-out end role is duplicated")
            ending = row
    if state is None or ending is None:
        raise FormatError("MCFREG2 price-out state is incomplete")

    def make_common(row):
        words = row["network_words"]
        if not isinstance(words, list):
            raise FormatError("MCFREG2 price-out network words are invalid")
        return {
            "ordinal": begin["ordinal"],
            "network_words": tuple(
                _integer(word, "price-out network word") for word in words
            ),
            "objects": _object_states(row["objects"], "price-out"),
            "arena_generation": _integer(
                row["arena_generation"], "arena generation", minimum=0
            ),
            "arena_capacity": _integer(
                row["arena_capacity"], "arena capacity", minimum=0
            ),
            "heap": _refs(row["heap"], "price-out heap"),
        }

    return (
        PriceOutLiveIn(**make_common(state)),
        PriceOutDerivedOut(
            **make_common(ending),
            candidates=tuple(candidates),
            decisions=tuple(decisions),
        ),
    )


def validate_semantic_roles(package: Package) -> tuple[SemanticFrame, ...]:
    if package.header.schema != 3:
        raise FormatError("MCFREG2 formal schema 3 is required")
    section_by_name = {
        SECTION_NAMES[section.section_type]: section
        for section in package.sections
        if section.section_type in SECTION_NAMES
    }
    for name in ("EVENTS", "CALL_INDEX", "BOUNDARIES"):
        if section_by_name[name].schema != 3:
            raise FormatError(f"MCFREG2 {name} section schema differs")
    events = _semantic_events(package.section("EVENTS"))
    call_rows = _semantic_json(package.section("CALL_INDEX"), "CALL_INDEX")
    boundary_rows = _semantic_json(
        package.section("BOUNDARIES"), "BOUNDARIES"
    )
    frames = []
    active = None
    active_rows = []
    active_roles = [set(), set()]
    seen_unique = set()
    frame_ranges = []
    for event_index, row in enumerate(events):
        kind = row.get("kind")
        if kind == "CALL_BEGIN":
            if active is not None:
                raise FormatError("MCFREG2 call entry is duplicated")
            phase = row.get("phase")
            common = {"kind", "role", "call", "order", "ordinal", "phase"}
            expected = common | (
                {"m", "nr_group", "group_pos", "initialize"}
                if phase == "pricing" else set()
            )
            if phase not in ("pricing", "price_out") or set(row) != expected:
                raise FormatError("MCFREG2 CALL_BEGIN record role differs")
            if row.get("role") != "live_in":
                raise FormatError("MCFREG2 CALL_BEGIN record role differs")
            for name in ("call", "order", "ordinal"):
                _integer(row[name], f"call begin {name}", minimum=0)
            active = row
            active_rows = []
            active_roles = [set(), set()]
            seen_unique = set()
            frame_begin = event_index
            continue
        if kind == "CALL_END":
            expected = {"kind", "role", "call", "order", "ordinal", "phase"}
            if active is None or set(row) != expected:
                raise FormatError("MCFREG2 call exit record role differs")
            if row.get("role") != "observed_result" or any(
                row.get(name) != active[name]
                for name in ("call", "order", "ordinal", "phase")
            ):
                raise FormatError("MCFREG2 call exit record role differs")
            if active["phase"] == "pricing":
                live_in, observed = _pricing_states(active, active_rows)
            else:
                live_in, observed = _price_out_states(active, active_rows)
            frames.append(SemanticFrame(
                call=active["call"],
                order=active["order"],
                ordinal=active["ordinal"],
                phase=active["phase"],
                live_in_roles=frozenset(active_roles[0]),
                result_roles=frozenset(active_roles[1]),
                live_in_state=live_in,
                observed_state=observed,
            ))
            frame_ranges.append((frame_begin, event_index - frame_begin + 1))
            active = None
            continue
        if active is None:
            raise FormatError("MCFREG2 event has no call entry")
        spec = _EVENT_SPECS.get(kind)
        if spec is None:
            raise FormatError("MCFREG2 event record kind differs")
        role, logical_role, keys = spec
        if row.get("role") != role or set(row) != keys:
            raise FormatError(f"MCFREG2 {kind} record role differs")
        if row.get("call") != active["call"]:
            raise FormatError("MCFREG2 event call differs")
        if active["phase"] == "pricing" and kind.startswith("PRICE_OUT"):
            raise FormatError("MCFREG2 event phase differs")
        if active["phase"] == "price_out" and (
            kind.startswith("PRICING") or kind.startswith("BASKET")
        ):
            raise FormatError("MCFREG2 event phase differs")
        unique_value = None
        for name in ("scan_position", "slot", "candidate", "reference",
                     "old_reference"):
            if name in row:
                unique_value = json.dumps(row[name], sort_keys=True)
                break
        unique = (kind, unique_value)
        if unique in seen_unique:
            raise FormatError(f"MCFREG2 {kind} record role is duplicated")
        seen_unique.add(unique)
        active_roles[0 if role == "live_in" else 1].add(logical_role)
        active_rows.append(row)
    if active is not None:
        raise FormatError("MCFREG2 call exit is missing")
    if len(frames) != len(call_rows) or len(frames) != len(boundary_rows):
        raise FormatError("MCFREG2 call metadata count differs")
    for index, (frame, call_row, boundary, event_range) in enumerate(
        zip(frames, call_rows, boundary_rows, frame_ranges)
    ):
        expected_call = {
            "call": frame.call, "order": frame.order,
            "ordinal": frame.ordinal, "phase": frame.phase,
            "event_begin": event_range[0], "event_count": event_range[1],
        }
        if call_row != expected_call:
            raise FormatError(f"MCFREG2 call index {index} differs")
        expected_boundary = {
            "call": frame.call,
            "order": frame.order,
            "phase": frame.phase,
            "pre_sha256": digest_call_state(frame.live_in_state),
            "post_sha256": digest_call_state(frame.observed_state),
        }
        if boundary != expected_boundary:
            raise FormatError(f"MCFREG2 canonical boundary {index} differs")
    pricing = sum(frame.phase == "pricing" for frame in frames)
    price_out = sum(frame.phase == "price_out" for frame in frames)
    if pricing != package.header.pricing_calls:
        raise FormatError("MCFREG2 pricing call count differs")
    if price_out != package.header.price_out_calls:
        raise FormatError("MCFREG2 price-out call count differs")
    if len(events) != package.header.event_count:
        raise FormatError("MCFREG2 event count differs")
    return tuple(frames)


def _checked_add(left: int, right: int, label: str) -> int:
    if left < 0 or right < 0 or left > UINT64_MAX - right:
        raise FormatError(f"MCFREG2 {label} overflows u64")
    return left + right


def _checked_mul(left: int, right: int, label: str) -> int:
    if left < 0 or right < 0 or (left != 0 and right > UINT64_MAX // left):
        raise FormatError(f"MCFREG2 {label} overflows u64")
    return left * right


def sha256_file(path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_stable_ref(
    reference: StableRef,
    *,
    maximum_ids: Mapping[int, int],
    generations: Iterable[int],
) -> None:
    generation_set = set(generations)
    if reference.kind == OBJECT_NULL:
        if reference != StableRef.null():
            raise FormatError("MCFREG2 null stable reference is malformed")
        return
    if reference.kind not in maximum_ids:
        raise FormatError("MCFREG2 stable reference kind is unknown")
    if reference.generation not in generation_set:
        raise FormatError("MCFREG2 stable reference generation is unknown")
    maximum = maximum_ids[reference.kind]
    if maximum < 0 or reference.object_id > maximum:
        raise FormatError("MCFREG2 stable reference object ID is out of range")


def _default_header(
    *,
    nodes: int,
    active_arcs: int,
    dummy_arcs: int,
    arena_capacity: int,
    pricing_calls: int,
    price_out_calls: int,
    event_count: int,
    section_count: int,
) -> Header:
    return Header(
        magic=MAGIC,
        schema=SCHEMA,
        endian_tag=ENDIAN_TAG,
        header_bytes=HEADER.size,
        flags=0,
        section_count=section_count,
        directory_offset=HEADER.size,
        nodes=nodes,
        active_arcs=active_arcs,
        dummy_arcs=dummy_arcs,
        arena_capacity=arena_capacity,
        pricing_calls=pricing_calls,
        price_out_calls=price_out_calls,
        event_count=event_count,
        reserved=0,
    )


def new_package(
    *,
    nodes: int,
    active_arcs: int,
    dummy_arcs: int,
    arena_capacity: int,
    pricing_calls: int,
    price_out_calls: int,
    event_count: int,
    sections: Mapping[str, bytes | Path],
    section_layouts: Mapping[str, tuple[int, int]] | None = None,
    section_schemas: Mapping[str, int] | None = None,
) -> Package:
    unknown = set(sections) - set(REQUIRED_SECTIONS)
    missing = set(REQUIRED_SECTIONS) - set(sections)
    if unknown:
        raise FormatError(f"unknown MCFREG2 section name: {sorted(unknown)[0]}")
    if missing:
        raise FormatError(f"missing MCFREG2 section: {sorted(missing)[0]}")
    layouts = dict(section_layouts or {})
    schemas = dict(section_schemas or {})
    unknown_layouts = set(layouts) - set(REQUIRED_SECTIONS)
    if unknown_layouts:
        raise FormatError(
            f"unknown MCFREG2 section layout: {sorted(unknown_layouts)[0]}"
        )
    unknown_schemas = set(schemas) - set(REQUIRED_SECTIONS)
    if unknown_schemas:
        raise FormatError(
            f"unknown MCFREG2 section schema: {sorted(unknown_schemas)[0]}"
        )
    if any(
        not isinstance(value, int) or isinstance(value, bool) or value <= 0
        for value in schemas.values()
    ):
        raise FormatError("MCFREG2 section schema is invalid")
    values = tuple(
        Section(
            section_type=SECTION_TYPES[name],
            schema=schemas.get(name, 1),
            flags=0,
            element_count=(
                layouts[name][0] if name in layouts else 1
            ),
            element_size=(
                layouts[name][1]
                if name in layouts
                else _section_source_size(sections[name])
            ),
            data=sections[name],
        )
        for name in REQUIRED_SECTIONS
    )
    header = _default_header(
        nodes=nodes,
        active_arcs=active_arcs,
        dummy_arcs=dummy_arcs,
        arena_capacity=arena_capacity,
        pricing_calls=pricing_calls,
        price_out_calls=price_out_calls,
        event_count=event_count,
        section_count=len(values),
    )
    return Package(header=header, directory=(), sections=values)


def _section_source_size(data) -> int:
    if isinstance(data, bytes):
        return len(data)
    if isinstance(data, Path) and data.is_file():
        return data.stat().st_size
    if isinstance(data, SectionView):
        return data.stored_bytes
    raise FormatError("MCFREG2 section source is invalid")


def _validate_header(header: Header, file_size: int) -> int:
    if header.magic != MAGIC:
        raise FormatError("MCFREG2 header magic differs")
    if header.schema != SCHEMA:
        raise FormatError("MCFREG2 header schema differs")
    if header.endian_tag != ENDIAN_TAG:
        raise FormatError("MCFREG2 header endian tag differs")
    if header.header_bytes != HEADER.size:
        raise FormatError("MCFREG2 header size differs")
    if header.flags != 0:
        raise FormatError("MCFREG2 header flags are unsupported")
    if header.reserved != 0:
        raise FormatError("MCFREG2 reserved header field is nonzero")
    if header.directory_offset != HEADER.size:
        raise FormatError("MCFREG2 directory offset differs")
    if header.section_count < len(REQUIRED_SECTIONS):
        raise FormatError("MCFREG2 section count is too small")
    if header.nodes == 0 or header.active_arcs == 0:
        raise FormatError("MCFREG2 network counts must be positive")
    if header.arena_capacity < header.active_arcs:
        raise FormatError("MCFREG2 arena capacity is below active arcs")
    if (
        (header.pricing_calls == 0 and header.price_out_calls == 0)
        or header.event_count == 0
    ):
        raise FormatError("MCFREG2 call and event counts must be positive")
    directory_bytes = _checked_mul(
        header.section_count, DIRECTORY.size, "directory size"
    )
    directory_end = _checked_add(
        header.directory_offset, directory_bytes, "directory end"
    )
    if directory_end > file_size:
        raise FormatError("MCFREG2 directory is truncated")
    return directory_end


def _header_from_values(values) -> Header:
    return Header(*values)


def _entry_from_values(values) -> DirectoryEntry:
    return DirectoryEntry(*values)


def read_package(path, *, lazy_section_names=()) -> Package:
    path = Path(path).resolve()
    for name in lazy_section_names:
        if name not in SECTION_TYPES:
            raise FormatError(f"unknown lazy MCFREG2 section: {name}")
    lazy_section_types = {
        SECTION_TYPES[name] for name in lazy_section_names
    }
    try:
        file_size = path.stat().st_size
        with path.open("rb") as stream:
            raw_header = stream.read(HEADER.size)
            if len(raw_header) != HEADER.size:
                raise FormatError("MCFREG2 header is truncated")
            header = _header_from_values(HEADER.unpack(raw_header))
            directory_end = _validate_header(header, file_size)
            directory = []
            seen = set()
            for _ in range(header.section_count):
                raw_entry = stream.read(DIRECTORY.size)
                if len(raw_entry) != DIRECTORY.size:
                    raise FormatError("MCFREG2 directory is truncated")
                entry = _entry_from_values(DIRECTORY.unpack(raw_entry))
                if entry.section_type in seen:
                    raise FormatError(
                        f"duplicate MCFREG2 section type {entry.section_type}"
                    )
                seen.add(entry.section_type)
                if (
                    entry.section_type not in SECTION_NAMES
                    and not entry.flags & OPTIONAL_FLAG
                ):
                    raise FormatError(
                        "MCFREG2 unknown mandatory section "
                        f"{entry.section_type}"
                    )
                if entry.section_type in SECTION_NAMES and entry.flags != 0:
                    raise FormatError("MCFREG2 required section flags differ")
                if entry.schema == 0:
                    raise FormatError("MCFREG2 section schema is zero")
                if entry.stored_bytes == 0:
                    raise FormatError("MCFREG2 section is empty")
                if entry.element_size:
                    logical_bytes = _checked_mul(
                        entry.element_count,
                        entry.element_size,
                        "section logical size",
                    )
                    if logical_bytes != entry.stored_bytes:
                        raise FormatError(
                            "MCFREG2 section element size differs"
                        )
                directory.append(entry)
            required_types = set(SECTION_TYPES.values())
            missing = required_types - seen
            if missing:
                name = SECTION_NAMES[min(missing)]
                raise FormatError(f"missing MCFREG2 section {name}")
            if [entry.section_type for entry in directory] != sorted(seen):
                raise FormatError("MCFREG2 directory is not sorted")
            cursor = directory_end
            sections = []
            for entry in directory:
                if entry.offset < cursor:
                    raise FormatError("MCFREG2 sections overlap")
                if entry.offset > cursor:
                    raise FormatError("MCFREG2 section layout has a gap")
                end = _checked_add(
                    entry.offset, entry.stored_bytes, "section end"
                )
                if end > file_size:
                    raise FormatError("MCFREG2 section is truncated")
                if entry.section_type not in lazy_section_types:
                    stream.seek(entry.offset)
                    remaining = entry.stored_bytes
                    digest = hashlib.sha256()
                    while remaining:
                        chunk = stream.read(min(remaining, 1024 * 1024))
                        if not chunk:
                            raise FormatError("MCFREG2 section is truncated")
                        digest.update(chunk)
                        remaining -= len(chunk)
                    actual_digest = digest.digest()
                    if actual_digest != entry.sha256:
                        raise FormatError("MCFREG2 section SHA-256 differs")
                sections.append(Section(
                    section_type=entry.section_type,
                    schema=entry.schema,
                    flags=entry.flags,
                    element_count=entry.element_count,
                    element_size=entry.element_size,
                    data=SectionView(path, entry.offset, entry.stored_bytes),
                ))
                cursor = end
            if cursor != file_size:
                raise FormatError("MCFREG2 file has trailing bytes")
    except OSError as error:
        raise FormatError(f"cannot read MCFREG2 package {path}: {error}") from error
    return Package(
        header=header,
        directory=tuple(directory),
        sections=tuple(sections),
    )


@dataclasses.dataclass(frozen=True)
class StreamedEvent:
    ordinal: int
    row: dict


def _event_json(line, ordinal):
    try:
        row = json.loads(line)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise FormatError(
            f"MCFREG2 EVENTS row {ordinal + 1} is invalid"
        ) from error
    if not isinstance(row, dict):
        raise FormatError(
            f"MCFREG2 EVENTS row {ordinal + 1} is not an object"
        )
    return row


def stream_events(path):
    """Yield EVENTS rows while authenticating one bounded gzip scan.

    The stored EVENTS bytes are read exactly through their directory extent;
    the fully decompressed section is never retained in memory or written.
    Authentication completes when the iterator is exhausted.
    """

    package = read_package(path, lazy_section_names=("EVENTS",))
    entry = next(
        item
        for item in package.directory
        if item.section_type == SECTION_TYPES["EVENTS"]
    )
    reader = _DigestingSectionReader(Path(path).resolve(), entry)
    buffered = io.BufferedReader(reader, buffer_size=1024 * 1024)
    compressed = gzip.GzipFile(fileobj=buffered, mode="rb")
    count = 0
    try:
        try:
            while True:
                line = compressed.readline()
                if not line:
                    break
                yield StreamedEvent(count, _event_json(line, count))
                count += 1
        except (OSError, EOFError) as error:
            raise FormatError(
                "MCFREG2 EVENTS gzip stream is invalid"
            ) from error
        if count != package.header.event_count:
            raise FormatError(
                "MCFREG2 EVENTS event count differs: "
                f"declared={package.header.event_count}, observed={count}"
            )
        if entry.element_count != count:
            raise FormatError(
                "MCFREG2 EVENTS directory event count differs"
            )
    finally:
        try:
            reader.finish(entry.sha256)
        finally:
            try:
                compressed.close()
            finally:
                buffered.close()


def _pack_header(header: Header, section_count: int) -> bytes:
    return HEADER.pack(
        header.magic,
        header.schema,
        header.endian_tag,
        HEADER.size,
        header.flags,
        section_count,
        HEADER.size,
        header.nodes,
        header.active_arcs,
        header.dummy_arcs,
        header.arena_capacity,
        header.pricing_calls,
        header.price_out_calls,
        header.event_count,
        header.reserved,
    )


def _section_size(section: Section) -> int:
    if isinstance(section.data, bytes):
        return len(section.data)
    if isinstance(section.data, Path):
        return section.data.stat().st_size
    if isinstance(section.data, SectionView):
        return section.data.stored_bytes
    raise FormatError("cannot write a lazy MCFREG2 section")


def _section_sha256(section: Section) -> bytes:
    if isinstance(section.data, bytes):
        return hashlib.sha256(section.data).digest()
    if isinstance(section.data, Path):
        digest = hashlib.sha256()
        with section.data.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.digest()
    if isinstance(section.data, SectionView):
        digest = hashlib.sha256()
        for chunk in section.data.chunks():
            digest.update(chunk)
        return digest.digest()
    raise FormatError("cannot hash a lazy MCFREG2 section")


def _package_entries(package: Package):
    sections = tuple(sorted(package.sections, key=lambda value: value.section_type))
    seen = set()
    for section in sections:
        if section.section_type in seen:
            raise FormatError(
                f"duplicate MCFREG2 section type {section.section_type}"
            )
        seen.add(section.section_type)
    missing = set(SECTION_TYPES.values()) - seen
    if missing:
        raise FormatError(
            f"missing MCFREG2 section {SECTION_NAMES[min(missing)]}"
        )
    directory_end = _checked_add(
        HEADER.size,
        _checked_mul(len(sections), DIRECTORY.size, "directory size"),
        "directory end",
    )
    entries = []
    offset = directory_end
    for section in sections:
        if section.section_type not in SECTION_NAMES:
            if not section.flags & OPTIONAL_FLAG:
                raise FormatError(
                    f"MCFREG2 unknown mandatory section {section.section_type}"
                )
        elif section.flags != 0:
            raise FormatError("MCFREG2 required section flags differ")
        stored_bytes = _section_size(section)
        if stored_bytes == 0:
            raise FormatError("MCFREG2 section is empty")
        if section.element_size:
            logical_bytes = _checked_mul(
                section.element_count,
                section.element_size,
                "section logical size",
            )
            if logical_bytes != stored_bytes:
                raise FormatError("MCFREG2 section element size differs")
        entries.append(DirectoryEntry(
            section_type=section.section_type,
            schema=section.schema,
            flags=section.flags,
            offset=offset,
            stored_bytes=stored_bytes,
            element_count=section.element_count,
            element_size=section.element_size,
            sha256=_section_sha256(section),
        ))
        offset = _checked_add(offset, stored_bytes, "section end")
    return sections, entries


def _encoded_package(package: Package) -> bytes:
    sections, entries = _package_entries(package)
    if any(not isinstance(section.data, bytes) for section in sections):
        raise FormatError("file-backed MCFREG2 requires streaming write")
    chunks = [_pack_header(package.header, len(entries))]
    chunks.extend(
        DIRECTORY.pack(
            entry.section_type,
            entry.schema,
            entry.flags,
            entry.offset,
            entry.stored_bytes,
            entry.element_count,
            entry.element_size,
            entry.sha256,
        )
        for entry in entries
    )
    chunks.extend(section.data for section in sections)
    return b"".join(chunks)


def write_package(path, package: Package) -> str:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    sections, entries = _package_entries(package)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(_pack_header(package.header, len(entries)))
            for entry in entries:
                stream.write(DIRECTORY.pack(
                    entry.section_type,
                    entry.schema,
                    entry.flags,
                    entry.offset,
                    entry.stored_bytes,
                    entry.element_count,
                    entry.element_size,
                    entry.sha256,
                ))
            for section in sections:
                if isinstance(section.data, bytes):
                    stream.write(section.data)
                elif isinstance(section.data, Path):
                    with section.data.open("rb") as source:
                        shutil.copyfileobj(source, stream, 1024 * 1024)
                elif isinstance(section.data, SectionView):
                    for chunk in section.data.chunks():
                        stream.write(chunk)
                else:
                    raise FormatError("cannot write a lazy MCFREG2 section")
            stream.flush()
            os.fsync(stream.fileno())
        read_package(temporary)
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return sha256_file(path)
