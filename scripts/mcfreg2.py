# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

"""Strict reader and deterministic writer for the MCFREG2 container."""

from __future__ import annotations

import dataclasses
import hashlib
import os
import struct
import tempfile
from pathlib import Path
from typing import Iterable, Mapping


MAGIC = b"MCFREG2\0"
SCHEMA = 2
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
class Section:
    section_type: int
    schema: int
    flags: int
    element_count: int
    element_size: int
    data: bytes

    def __post_init__(self):
        if not isinstance(self.data, bytes):
            raise TypeError("MCFREG2 section data must be bytes")


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
                return section.data
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
    sections: Mapping[str, bytes],
    section_layouts: Mapping[str, tuple[int, int]] | None = None,
) -> Package:
    unknown = set(sections) - set(REQUIRED_SECTIONS)
    missing = set(REQUIRED_SECTIONS) - set(sections)
    if unknown:
        raise FormatError(f"unknown MCFREG2 section name: {sorted(unknown)[0]}")
    if missing:
        raise FormatError(f"missing MCFREG2 section: {sorted(missing)[0]}")
    layouts = dict(section_layouts or {})
    unknown_layouts = set(layouts) - set(REQUIRED_SECTIONS)
    if unknown_layouts:
        raise FormatError(
            f"unknown MCFREG2 section layout: {sorted(unknown_layouts)[0]}"
        )
    values = tuple(
        Section(
            section_type=SECTION_TYPES[name],
            schema=1,
            flags=0,
            element_count=layouts.get(name, (1, len(sections[name])))[0],
            element_size=layouts.get(name, (1, len(sections[name])))[1],
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
        header.pricing_calls == 0
        or header.price_out_calls == 0
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


def read_package(path) -> Package:
    path = Path(path)
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
                stream.seek(entry.offset)
                data = stream.read(entry.stored_bytes)
                if len(data) != entry.stored_bytes:
                    raise FormatError("MCFREG2 section is truncated")
                if hashlib.sha256(data).digest() != entry.sha256:
                    raise FormatError("MCFREG2 section SHA-256 differs")
                sections.append(Section(
                    section_type=entry.section_type,
                    schema=entry.schema,
                    flags=entry.flags,
                    element_count=entry.element_count,
                    element_size=entry.element_size,
                    data=data,
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


def _encoded_package(package: Package) -> bytes:
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
        if not section.data:
            raise FormatError("MCFREG2 section is empty")
        if section.element_size:
            logical_bytes = _checked_mul(
                section.element_count,
                section.element_size,
                "section logical size",
            )
            if logical_bytes != len(section.data):
                raise FormatError("MCFREG2 section element size differs")
        entries.append(DirectoryEntry(
            section_type=section.section_type,
            schema=section.schema,
            flags=section.flags,
            offset=offset,
            stored_bytes=len(section.data),
            element_count=section.element_count,
            element_size=section.element_size,
            sha256=hashlib.sha256(section.data).digest(),
        ))
        offset = _checked_add(offset, len(section.data), "section end")
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
    payload = _encoded_package(package)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
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
