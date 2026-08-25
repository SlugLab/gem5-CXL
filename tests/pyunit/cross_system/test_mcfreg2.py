# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

import hashlib
import dataclasses
import json
import shutil
import struct
import subprocess
import tempfile
import unittest
from pathlib import Path

try:
    from scripts import mcfreg2
    from scripts import generate_mcfreg2_state as generator
except ImportError:
    mcfreg2 = None
    generator = None


class MCFREG2Test(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.repo = Path(__file__).resolve().parents[3]

    def require_module(self):
        self.assertIsNotNone(mcfreg2, "scripts.mcfreg2 is missing")

    def fixture_package(self):
        self.require_module()
        sections = {
            name: f"{name.lower()}\n".encode("ascii")
            for name in mcfreg2.REQUIRED_SECTIONS
        }
        return mcfreg2.new_package(
            nodes=4,
            active_arcs=6,
            dummy_arcs=3,
            arena_capacity=16,
            pricing_calls=2,
            price_out_calls=1,
            event_count=9,
            sections=sections,
        )

    def write_fixture(self, name="fixture.reg2"):
        path = self.root / name
        mcfreg2.write_package(path, self.fixture_package())
        return path

    def write_semantic_fixture(self, name="semantic.reg2", fault=None):
        self.require_module()
        self.assertIsNotNone(generator)
        pricing = [
            {"kind": "BEGIN", "call": 0, "order": 0, "m": 6,
             "nr_group": 3, "group_pos": 0, "initialize": True,
             "basket_size": 0},
            {"kind": "SCAN", "call": 0, "arc_id": 0, "tail_id": 0,
             "head_id": 1, "arc_cost": 5, "tail_potential": 10,
             "head_potential": 3, "ident": 1, "reduced_cost": -2,
             "candidate": True, "basket_slot": 1, "group_pos": 0},
            {"kind": "SCAN", "call": 0, "arc_id": 3, "tail_id": 1,
             "head_id": 2, "arc_cost": 4, "tail_potential": 1,
             "head_potential": 1, "ident": 1, "reduced_cost": 4,
             "candidate": False, "basket_slot": -1, "group_pos": 0},
            {"kind": "BASKET", "call": 0, "phase": "live_out",
             "slot": 1, "arc_id": 0, "cost": -2, "abs_cost": 2},
            {"kind": "END", "call": 0, "selected_arc_id": 0,
             "reduced_cost": -2, "arcs_priced": 2, "nr_group": 3,
             "group_pos": 1, "initialize": False, "basket_size": 1},
            {"kind": "BEGIN", "call": 1, "order": 1, "m": 6,
             "nr_group": 3, "group_pos": 1, "initialize": False,
             "basket_size": 0},
            {"kind": "SCAN", "call": 1, "arc_id": 1, "tail_id": 1,
             "head_id": 2, "arc_cost": 8, "tail_potential": 2,
             "head_potential": 0, "ident": 1, "reduced_cost": 6,
             "candidate": False, "basket_slot": -1, "group_pos": 1},
            {"kind": "SCAN", "call": 1, "arc_id": 4, "tail_id": 2,
             "head_id": 3, "arc_cost": 1, "tail_potential": 5,
             "head_potential": 1, "ident": 1, "reduced_cost": -3,
             "candidate": True, "basket_slot": 1, "group_pos": 1},
            {"kind": "BASKET", "call": 1, "phase": "live_out",
             "slot": 1, "arc_id": 4, "cost": -3, "abs_cost": 3},
            {"kind": "END", "call": 1, "selected_arc_id": 4,
             "reduced_cost": -3, "arcs_priced": 2, "nr_group": 3,
             "group_pos": 2, "initialize": False, "basket_size": 1},
        ]
        if fault == "selected-arc":
            pricing[4]["selected_arc_id"] = 3
        price_out = []

        def add_price_out(call, order, live_in, reduced, decision,
                          new_arcs, generation=0, remap=False):
            price_out.append({
                "kind": "BEGIN", "call": call, "order": order,
                "live_in_m": live_in, "capacity": 16 if not remap else 16,
                "generation": generation,
            })
            event_generation = generation
            capacity = 16
            if remap:
                price_out.append({
                    "kind": "ARENA_REMAP", "call": call,
                    "old_generation": generation,
                    "new_generation": generation + 1,
                    "mapped_elements": live_in,
                    "old_capacity": 16, "new_capacity": 32,
                })
                event_generation += 1
                capacity = 32
            tail_potential = 20 if reduced >= 0 else 30 - reduced
            price_out.append({
                "kind": "CANDIDATE", "call": call, "candidate": 0,
                "tail_id": 0, "head_id": 1, "arc_cost": 30,
                "tail_potential": tail_potential, "head_potential": 0,
                "reduced_cost": reduced,
            })
            if decision != "NO_CHANGE":
                price_out.append({
                    "kind": "ARC_STATE", "call": call, "candidate": 0,
                    "reference": {"kind": "arc",
                                  "generation": event_generation,
                                  "index": live_in},
                    "tail_id": 0, "head_id": 1, "cost": 30,
                    "org_cost": 30, "flow": reduced, "ident": 0,
                })
                reference = {"kind": "arc", "generation": event_generation,
                             "index": live_in}
            else:
                reference = {}
            price_out.append({
                "kind": "DECISION", "call": call, "candidate": 0,
                "decision": decision, "reference": reference,
            })
            price_out.append({
                "kind": "END", "call": call, "new_arcs": new_arcs,
                "live_out_m": live_in + new_arcs, "candidates": 1,
                "capacity": capacity, "generation": event_generation,
                "m_impl": new_arcs, "max_residual_new_m": 10 - new_arcs,
            })

        add_price_out(0, 2, 6, 10, "NO_CHANGE", 0)
        add_price_out(1, 3, 6, -5, "INSERT", 1)
        add_price_out(2, 4, 7, -7, "REPLACE", 0)
        add_price_out(3, 5, 7, -9, "REPLACE", 0, remap=True)
        frames = generator._ordered_frames(pricing, price_out)
        events, calls, boundaries, basket, deltas = (
            generator._frame_sections(frames)
        )
        network_words = [3, 2, 16, 6] + [0] * 18
        null_reference = mcfreg2.StableRef.null().pack()
        node_record = bytearray(generator.STATE_NODE_BYTES)
        for offset in range(16, 144, 16):
            node_record[offset:offset + 16] = null_reference
        nodes = bytes(node_record) * 4
        arc_records = []
        for index in range(9):
            arc_record = bytearray(generator.STATE_ARC_BYTES)
            arc_record[8:24] = mcfreg2.StableRef(
                mcfreg2.OBJECT_NODE, 0, index % 4
            ).pack()
            arc_record[24:40] = mcfreg2.StableRef(
                mcfreg2.OBJECT_NODE, 0, (index + 1) % 4
            ).pack()
            arc_record[48:64] = null_reference
            arc_record[64:80] = null_reference
            arc_records.append(bytes(arc_record))
        arcs = b"".join(arc_records)
        sections = {
            "PROVENANCE": generator._canonical_json({"schema": 1}),
            "NETWORK": struct.pack("<22Q", *network_words),
            "NODES": nodes,
            "ARCS": arcs,
            "BASKET": generator._canonical_json(
                {"schema": 1, "rows": basket}
            ),
            "CALL_INDEX": generator._canonical_json(
                {"schema": 1, "rows": calls}
            ),
            "EVENTS": b"".join(
                generator._canonical_json(row) for row in events
            ),
            "DELTAS": generator._canonical_json(
                {"schema": 1, "rows": deltas}
            ),
            "BOUNDARIES": generator._canonical_json(
                {"schema": 1, "rows": boundaries}
            ),
            "FINAL": generator._canonical_json({"schema": 1}),
        }
        package = mcfreg2.new_package(
            nodes=4, active_arcs=6, dummy_arcs=3, arena_capacity=16,
            pricing_calls=2, price_out_calls=4, event_count=len(events),
            sections=sections,
            section_layouts={
                "NETWORK": (22, 8), "NODES": (4, 176),
                "ARCS": (9, 96), "EVENTS": (len(events), 0),
            },
        )
        path = self.root / name
        mcfreg2.write_package(path, package)
        return path

    def directory_offset(self, index):
        return mcfreg2.HEADER.size + index * mcfreg2.DIRECTORY.size

    def patch_directory(self, path, index, **changes):
        payload = bytearray(path.read_bytes())
        offset = self.directory_offset(index)
        values = list(mcfreg2.DIRECTORY.unpack_from(payload, offset))
        fields = {
            "section_type": 0,
            "schema": 1,
            "flags": 2,
            "offset": 3,
            "stored_bytes": 4,
            "element_count": 5,
            "element_size": 6,
            "sha256": 7,
        }
        for name, value in changes.items():
            values[fields[name]] = value
        mcfreg2.DIRECTORY.pack_into(payload, offset, *values)
        path.write_bytes(payload)

    def patch_header(self, path, **changes):
        payload = bytearray(path.read_bytes())
        values = list(mcfreg2.HEADER.unpack_from(payload))
        fields = {
            "magic": 0,
            "schema": 1,
            "endian_tag": 2,
            "header_bytes": 3,
            "flags": 4,
            "section_count": 5,
            "directory_offset": 6,
            "nodes": 7,
            "active_arcs": 8,
            "dummy_arcs": 9,
            "arena_capacity": 10,
            "pricing_calls": 11,
            "price_out_calls": 12,
            "event_count": 13,
            "reserved": 14,
        }
        for name, value in changes.items():
            values[fields[name]] = value
        mcfreg2.HEADER.pack_into(payload, 0, *values)
        path.write_bytes(payload)

    def test_minimal_package_round_trips(self):
        self.require_module()
        path = self.root / "minimal.reg2"
        expected = self.fixture_package()
        digest = mcfreg2.write_package(path, expected)
        actual = mcfreg2.read_package(path)

        self.assertEqual(actual.header.magic, b"MCFREG2\0")
        self.assertEqual(actual.header.schema, 3)
        self.assertEqual(actual.header.nodes, 4)
        self.assertEqual(actual.header.active_arcs, 6)
        self.assertEqual(actual.header.arena_capacity, 16)
        self.assertEqual(actual.section_names(), mcfreg2.REQUIRED_SECTIONS)
        self.assertEqual(mcfreg2.sha256_file(path), digest)
        for name in mcfreg2.REQUIRED_SECTIONS:
            self.assertEqual(actual.section(name), expected.section(name))

    def strict_semantic_fixture(self, mutation=None):
        pricing_live_in = mcfreg2.PricingLiveIn(
            ordinal=0,
            m=6,
            nr_group=3,
            group_pos=0,
            initialize=True,
            basket=(mcfreg2.BasketState(
                slot=1,
                arc=mcfreg2.StableRef(mcfreg2.OBJECT_ARC, 0, 2),
                cost=-4,
                abs_cost=4,
            ),),
            scans=(mcfreg2.PricingScanLiveIn(
                scan_position=0,
                arc=mcfreg2.StableRef(mcfreg2.OBJECT_ARC, 0, 0),
                tail=mcfreg2.StableRef(mcfreg2.OBJECT_NODE, 0, 0),
                head=mcfreg2.StableRef(mcfreg2.OBJECT_NODE, 0, 1),
                cost=5,
                ident=1,
                tail_potential=10,
                head_potential=3,
            ),),
        )
        pricing_out = mcfreg2.PricingDerivedOut(
            ordinal=0,
            candidates=(mcfreg2.PricingCandidate(
                scan_position=0,
                reduced_cost=-2,
                candidate=True,
                basket_slot=2,
            ),),
            basket=(mcfreg2.BasketState(
                slot=1,
                arc=mcfreg2.StableRef(mcfreg2.OBJECT_ARC, 0, 0),
                cost=-2,
                abs_cost=2,
            ),),
            selected_arc=mcfreg2.StableRef(
                mcfreg2.OBJECT_ARC, 0, 0
            ),
            selected_reduced_cost=-2,
            arcs_priced=1,
            nr_group=3,
            group_pos=1,
            initialize=False,
        )
        price_out_live_in = mcfreg2.PriceOutLiveIn(
            ordinal=0,
            network_words=(6, 16),
            objects=(),
            arena_generation=0,
            arena_capacity=16,
            heap=(),
        )
        price_out_observed = mcfreg2.PriceOutDerivedOut(
            ordinal=0,
            network_words=(6, 16),
            objects=(),
            arena_generation=0,
            arena_capacity=16,
            heap=(),
            candidates=(mcfreg2.PriceOutCandidate(
                candidate=0,
                tail=mcfreg2.StableRef(mcfreg2.OBJECT_NODE, 0, 0),
                head=mcfreg2.StableRef(mcfreg2.OBJECT_NODE, 0, 1),
                cost=30,
                reduced_cost=10,
            ),),
            decisions=(mcfreg2.PriceOutDecision(
                candidate=0,
                decision="NO_CHANGE",
                reference=mcfreg2.StableRef.null(),
            ),),
        )
        events = [
            {"kind": "CALL_BEGIN", "role": "live_in", "call": 0,
             "order": 0, "ordinal": 0, "phase": "pricing", "m": 6,
             "nr_group": 3, "group_pos": 0, "initialize": True},
            {"kind": "BASKET_LIVE_IN", "role": "live_in", "call": 0,
             "slot": 1, "arc": {"kind": "arc", "generation": 0,
             "index": 2}, "cost": -4, "abs_cost": 4},
            {"kind": "PRICING_SCAN_LIVE_IN", "role": "live_in",
             "call": 0, "scan_position": 0, "group_pos": 0,
             "arc": {"kind": "arc", "generation": 0, "index": 0},
             "tail": {"kind": "node", "generation": 0, "index": 0},
             "head": {"kind": "node", "generation": 0, "index": 1},
             "cost": 5, "ident": 1, "tail_potential": 10,
             "head_potential": 3},
            {"kind": "PRICING_CANDIDATE_OBSERVED",
             "role": "observed_result", "call": 0, "scan_position": 0,
             "reduced_cost": -2, "candidate": True, "basket_slot": 2},
            {"kind": "BASKET_LIVE_OUT_OBSERVED",
             "role": "observed_result", "call": 0, "slot": 1,
             "arc": {"kind": "arc", "generation": 0, "index": 0},
             "cost": -2, "abs_cost": 2},
            {"kind": "PRICING_END_OBSERVED",
             "role": "observed_result", "call": 0,
             "selected_arc": {"kind": "arc", "generation": 0,
             "index": 0}, "selected_reduced_cost": -2,
             "arcs_priced": 1, "nr_group": 3, "group_pos": 1,
             "initialize": False},
            {"kind": "CALL_END", "role": "observed_result", "call": 0,
             "order": 0, "ordinal": 0, "phase": "pricing"},
        ]
        if mutation == "result-in-live-in":
            events[2]["selected_arc_id"] = 4
        pricing_event_count = len(events)
        null_ref = {
            "kind": "null", "generation": 0,
            "index": mcfreg2.UINT64_MAX,
        }
        events.extend([
            {"kind": "CALL_BEGIN", "role": "live_in", "call": 0,
             "order": 1, "ordinal": 0, "phase": "price_out"},
            {"kind": "PRICE_OUT_STATE_LIVE_IN", "role": "live_in",
             "call": 0, "network_words": [6, 16], "objects": [],
             "arena_generation": 0, "arena_capacity": 16, "heap": []},
            {"kind": "PRICE_OUT_CANDIDATE_OBSERVED",
             "role": "observed_result", "call": 0, "candidate": 0,
             "tail": {"kind": "node", "generation": 0, "index": 0},
             "head": {"kind": "node", "generation": 0, "index": 1},
             "cost": 30, "reduced_cost": 10},
            {"kind": "PRICE_OUT_DECISION_OBSERVED",
             "role": "observed_result", "call": 0, "candidate": 0,
             "decision": "NO_CHANGE", "reference": null_ref},
            {"kind": "PRICE_OUT_END_OBSERVED",
             "role": "observed_result", "call": 0,
             "network_words": [6, 16], "objects": [],
             "arena_generation": 0, "arena_capacity": 16, "heap": []},
            {"kind": "CALL_END", "role": "observed_result", "call": 0,
             "order": 1, "ordinal": 0, "phase": "price_out"},
        ])
        boundary = {
            "schema": 3,
            "rows": [
                {"call": 0, "order": 0, "phase": "pricing",
                 "pre_sha256": mcfreg2.digest_call_state(pricing_live_in),
                 "post_sha256": mcfreg2.digest_call_state(pricing_out)},
                {"call": 0, "order": 1, "phase": "price_out",
                 "pre_sha256": mcfreg2.digest_call_state(price_out_live_in),
                 "post_sha256": mcfreg2.digest_call_state(
                     price_out_observed
                 )},
            ],
        }
        call_index = {"schema": 3, "rows": [
            {"call": 0, "order": 0, "ordinal": 0, "phase": "pricing",
             "event_begin": 0, "event_count": pricing_event_count},
            {"call": 0, "order": 1, "ordinal": 0, "phase": "price_out",
             "event_begin": pricing_event_count,
             "event_count": len(events) - pricing_event_count},
        ]}
        sections = {
            name: f"{name.lower()}\n".encode("ascii")
            for name in mcfreg2.REQUIRED_SECTIONS
        }
        sections["EVENTS"] = b"".join(
            (json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n")
            .encode("ascii") for row in events
        )
        sections["CALL_INDEX"] = (
            json.dumps(call_index, sort_keys=True, separators=(",", ":"))
            + "\n"
        ).encode("ascii")
        sections["BOUNDARIES"] = (
            json.dumps(boundary, sort_keys=True, separators=(",", ":"))
            + "\n"
        ).encode("ascii")
        package = mcfreg2.new_package(
            nodes=4, active_arcs=6, dummy_arcs=3, arena_capacity=16,
            pricing_calls=1, price_out_calls=1, event_count=len(events),
            sections=sections,
            section_schemas={
                "EVENTS": 3, "CALL_INDEX": 3, "BOUNDARIES": 3,
            },
            section_layouts={"EVENTS": (len(events), 0)},
        )
        return package

    def test_formal_schema_three_separates_inputs_and_observed_results(self):
        self.require_module()
        frames = mcfreg2.validate_semantic_roles(
            self.strict_semantic_fixture()
        )
        self.assertEqual(
            frames[0].live_in_roles, {"pricing_scan", "basket"}
        )
        self.assertEqual(
            frames[0].result_roles, {"candidate", "selection"}
        )

    def test_result_field_in_live_in_record_fails_closed(self):
        self.require_module()
        with self.assertRaisesRegex(mcfreg2.FormatError, "record role"):
            mcfreg2.validate_semantic_roles(
                self.strict_semantic_fixture("result-in-live-in")
            )

    def replace_section(self, package, name, payload):
        section_type = mcfreg2.SECTION_TYPES[name]
        return dataclasses.replace(
            package,
            sections=tuple(
                dataclasses.replace(section, data=payload)
                if section.section_type == section_type else section
                for section in package.sections
            ),
        )

    def test_formal_semantics_reject_schema_two(self):
        package = self.strict_semantic_fixture()
        package = dataclasses.replace(
            package,
            header=dataclasses.replace(package.header, schema=2),
        )
        with self.assertRaisesRegex(mcfreg2.FormatError, "schema 3"):
            mcfreg2.validate_semantic_roles(package)

    def test_duplicate_semantic_role_fails_closed(self):
        package = self.strict_semantic_fixture()
        events = package.section("EVENTS").splitlines(keepends=True)
        events.insert(3, events[2])
        package = self.replace_section(package, "EVENTS", b"".join(events))
        with self.assertRaisesRegex(mcfreg2.FormatError, "duplicated"):
            mcfreg2.validate_semantic_roles(package)

    def test_missing_call_exit_fails_closed(self):
        package = self.strict_semantic_fixture()
        events = package.section("EVENTS").splitlines(keepends=True)
        package = self.replace_section(package, "EVENTS", b"".join(events[:-1]))
        with self.assertRaisesRegex(mcfreg2.FormatError, "call exit"):
            mcfreg2.validate_semantic_roles(package)

    def test_json_row_boundary_digest_is_not_a_canonical_state_digest(self):
        package = self.strict_semantic_fixture()
        boundaries = json.loads(package.section("BOUNDARIES"))
        events = package.section("EVENTS").splitlines()
        boundaries["rows"][0]["pre_sha256"] = hashlib.sha256(
            b"".join(events[:3])
        ).hexdigest()
        payload = (
            json.dumps(boundaries, sort_keys=True, separators=(",", ":"))
            + "\n"
        ).encode("ascii")
        package = self.replace_section(package, "BOUNDARIES", payload)
        with self.assertRaisesRegex(mcfreg2.FormatError, "canonical boundary"):
            mcfreg2.validate_semantic_roles(package)

    def test_python_and_cpp_canonical_call_state_digests_match(self):
        compiler = shutil.which("g++")
        if compiler is None:
            self.skipTest("g++ is unavailable")
        state = mcfreg2.PricingLiveIn(
            ordinal=0, m=6, nr_group=3, group_pos=0, initialize=True,
            basket=(mcfreg2.BasketState(
                1, mcfreg2.StableRef(mcfreg2.OBJECT_ARC, 0, 2), -4, 4
            ),),
            scans=(mcfreg2.PricingScanLiveIn(
                0,
                mcfreg2.StableRef(mcfreg2.OBJECT_ARC, 0, 0),
                mcfreg2.StableRef(mcfreg2.OBJECT_NODE, 0, 0),
                mcfreg2.StableRef(mcfreg2.OBJECT_NODE, 0, 1),
                5, 1, 10, 3,
            ),),
        )
        source = self.root / "mcfreg2_state_probe.cc"
        source.write_text(
            r'''#include "mcfreg2_state.hh"
#include <iostream>

int main()
{
    mcfreg2::PricingLiveIn state;
    state.ordinal = 0;
    state.m = 6;
    state.nrGroup = 3;
    state.groupPos = 0;
    state.initialize = true;
    state.basket.push_back({1, {MCFREG2_OBJECT_ARC, 0, 2}, -4, 4});
    state.scans.push_back({
        0,
        {MCFREG2_OBJECT_ARC, 0, 0},
        {MCFREG2_OBJECT_NODE, 0, 0},
        {MCFREG2_OBJECT_NODE, 0, 1},
        5, 1, 10, 3,
    });
    std::cout << mcfreg2::digestCallState(state) << "\n";
}
''',
            encoding="ascii",
        )
        binary = self.root / "mcfreg2-state-probe"
        subprocess.run(
            [
                compiler, "-std=c++17", "-Wall", "-Wextra", "-Werror",
                "-I", str(self.repo / "util/amu/matched_workloads"),
                str(source),
                str(self.repo / "util/amu/matched_workloads/mcfreg2_state.cc"),
                str(self.repo / "util/amu/matched_workloads/mcfreg2.cc"),
                "-o", str(binary), "-lz",
            ],
            cwd=self.repo,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=True,
        )
        completed = subprocess.run(
            [binary], text=True, stdout=subprocess.PIPE, check=True
        )
        self.assertEqual(completed.stdout.strip(), mcfreg2.digest_call_state(state))

    def test_truncated_header_is_rejected(self):
        self.require_module()
        path = self.root / "short.reg2"
        path.write_bytes(b"MCFREG2\0")
        with self.assertRaisesRegex(mcfreg2.FormatError, "header"):
            mcfreg2.read_package(path)

    def test_overlapping_sections_are_rejected(self):
        self.require_module()
        path = self.write_fixture()
        first = mcfreg2.DIRECTORY.unpack_from(
            path.read_bytes(), self.directory_offset(0)
        )
        self.patch_directory(path, 1, offset=first[3])
        with self.assertRaisesRegex(mcfreg2.FormatError, "overlap"):
            mcfreg2.read_package(path)

    def test_section_hash_drift_is_rejected(self):
        self.require_module()
        path = self.write_fixture()
        payload = bytearray(path.read_bytes())
        first = mcfreg2.DIRECTORY.unpack_from(
            payload, self.directory_offset(0)
        )
        payload[first[3]] ^= 1
        path.write_bytes(payload)
        with self.assertRaisesRegex(mcfreg2.FormatError, "SHA-256"):
            mcfreg2.read_package(path)

    def test_nonzero_reserved_header_is_rejected(self):
        self.require_module()
        path = self.write_fixture()
        self.patch_header(path, reserved=1)
        with self.assertRaisesRegex(mcfreg2.FormatError, "reserved"):
            mcfreg2.read_package(path)

    def test_duplicate_required_section_is_rejected(self):
        self.require_module()
        path = self.write_fixture()
        first_type = mcfreg2.DIRECTORY.unpack_from(
            path.read_bytes(), self.directory_offset(0)
        )[0]
        self.patch_directory(path, 1, section_type=first_type)
        with self.assertRaisesRegex(mcfreg2.FormatError, "duplicate"):
            mcfreg2.read_package(path)

    def test_unknown_mandatory_section_is_rejected(self):
        self.require_module()
        path = self.write_fixture()
        self.patch_directory(path, 0, section_type=99, flags=0)
        with self.assertRaisesRegex(mcfreg2.FormatError, "unknown mandatory"):
            mcfreg2.read_package(path)

    def test_unknown_optional_section_is_accepted(self):
        self.require_module()
        package = self.fixture_package().with_section(
            mcfreg2.Section(
                section_type=99,
                schema=1,
                flags=mcfreg2.OPTIONAL_FLAG,
                element_count=1,
                element_size=8,
                data=b"optional",
            )
        )
        path = self.root / "optional.reg2"
        mcfreg2.write_package(path, package)
        actual = mcfreg2.read_package(path)
        self.assertEqual(actual.section_by_type(99), b"optional")

    def test_trailing_bytes_are_rejected(self):
        self.require_module()
        path = self.write_fixture()
        path.write_bytes(path.read_bytes() + b"x")
        with self.assertRaisesRegex(mcfreg2.FormatError, "trailing"):
            mcfreg2.read_package(path)

    def test_stable_reference_boundaries(self):
        self.require_module()
        null = mcfreg2.StableRef.null()
        mcfreg2.validate_stable_ref(
            null, maximum_ids={mcfreg2.OBJECT_ARC: 5}, generations={0}
        )
        maximum = mcfreg2.StableRef(
            kind=mcfreg2.OBJECT_ARC, generation=0, object_id=5
        )
        mcfreg2.validate_stable_ref(
            maximum, maximum_ids={mcfreg2.OBJECT_ARC: 5}, generations={0}
        )
        out_of_range = mcfreg2.StableRef(
            kind=mcfreg2.OBJECT_ARC, generation=0, object_id=6
        )
        with self.assertRaisesRegex(mcfreg2.FormatError, "out of range"):
            mcfreg2.validate_stable_ref(
                out_of_range,
                maximum_ids={mcfreg2.OBJECT_ARC: 5},
                generations={0},
            )

    def test_writer_returns_content_sha256(self):
        self.require_module()
        path = self.root / "digest.reg2"
        actual = mcfreg2.write_package(path, self.fixture_package())
        expected = hashlib.sha256(path.read_bytes()).hexdigest()
        self.assertEqual(actual, expected)

    def test_file_backed_events_write_atomically_and_read_lazily(self):
        self.require_module()
        events = self.root / "events.jsonl.gz"
        events.write_bytes(b"\x1f\x8b" + b"x" * (1024 * 1024))
        package = self.fixture_package()
        package = dataclasses.replace(
            package,
            sections=tuple(
                dataclasses.replace(
                    section,
                    schema=2,
                    element_size=0,
                    data=events,
                )
                if section.section_type == mcfreg2.SECTION_TYPES["EVENTS"]
                else section
                for section in package.sections
            ),
        )
        path = self.root / "file-backed.reg2"
        digest = mcfreg2.write_package(path, package)
        actual = mcfreg2.read_package(
            path, lazy_section_names=("EVENTS",)
        )
        self.assertEqual(mcfreg2.sha256_file(path), digest)
        self.assertEqual(actual.section("PROVENANCE"), b"provenance\n")
        with self.assertRaisesRegex(mcfreg2.FormatError, "lazy"):
            actual.section("EVENTS")

    def compile_cpp_probe(self):
        compiler = shutil.which("g++")
        if compiler is None:
            self.skipTest("g++ is unavailable")
        implementation = (
            self.repo / "util/amu/matched_workloads/mcfreg2.cc"
        )
        self.assertTrue(implementation.is_file(), "mcfreg2.cc is missing")
        probe = self.root / "mcfreg2_probe.cc"
        probe.write_text(
            """
#include "mcfreg2.hh"

#include <cstdio>
#include <exception>
#include <iostream>
#include <string>

int main(int argc, char **argv)
{
    try {
        if (argc == 3 && std::string(argv[1]) == "--sha") {
            std::cout << mcfreg2::sha256Hex(argv[2]) << "\\n";
            return 0;
        }
        if (argc == 5 && std::string(argv[1]) == "--replay") {
            const auto package = mcfreg2::readPackage(argv[2]);
            std::FILE *trace = std::fopen(argv[3], "wb");
            if (trace == nullptr)
                return 3;
            const auto summary = mcfreg2::replay(package, trace, argv[4]);
            if (std::fclose(trace) != 0)
                return 4;
            std::cout << "{\\\"status\\\":\\\"verified\\\",\\\"pricing_calls\\\":"
                      << summary.pricingCalls << ",\\\"price_out_calls\\\":"
                      << summary.priceOutCalls << ",\\\"operations\\\":"
                      << summary.operations << ",\\\"boundary_mismatches\\\":"
                      << summary.boundaryMismatches << "}\\n";
            return 0;
        }
        if (argc != 2)
            return 2;
        const auto package = mcfreg2::readPackage(argv[1]);
        std::cout << mcfreg2::directoryJson(package) << "\\n";
        return 0;
    } catch (const std::exception &error) {
        std::cerr << error.what() << "\\n";
        return 1;
    }
}
""",
            encoding="utf-8",
        )
        output = self.root / "mcfreg2-probe"
        subprocess.run(
            [
                compiler,
                "-std=c++17",
                "-Wall",
                "-Wextra",
                "-Werror",
                "-I",
                str(self.repo / "util/amu/matched_workloads"),
                str(probe),
                str(implementation),
                "-o",
                str(output),
                "-lz",
            ],
            cwd=self.repo,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=True,
        )
        return output

    def compile_mcf_regions(self):
        compiler = shutil.which("g++")
        if compiler is None:
            self.skipTest("g++ is unavailable")
        output = self.root / "mcf-regions-formal"
        subprocess.run(
            [
                compiler,
                "-std=c++17",
                "-Wall",
                "-Wextra",
                "-Werror",
                "-I",
                str(self.repo / "util/amu/matched_workloads"),
                str(
                    self.repo
                    / "util/amu/matched_workloads/mcf_regions.cc"
                ),
                str(
                    self.repo / "util/amu/matched_workloads/mcfreg2.cc"
                ),
                "-o",
                str(output),
                "-lz",
            ],
            check=True,
        )
        return output

    def test_cpp_reader_matches_python_directory(self):
        self.require_module()
        path = self.write_fixture("parity.reg2")
        probe = self.compile_cpp_probe()
        completed = subprocess.run(
            [probe, path], text=True, stdout=subprocess.PIPE, check=True
        )
        self.assertEqual(
            json.loads(completed.stdout),
            mcfreg2.read_package(path).directory_json(),
        )

    def test_cpp_jsonl_reader_uses_a_cursor_not_per_row_compaction(self):
        implementation = (
            self.repo / "util/amu/matched_workloads/mcfreg2.cc"
        ).read_text(encoding="utf-8")
        self.assertIn("pendingOffset", implementation)
        self.assertNotIn("pending.erase(0, newline", implementation)

    def test_cpp_sha256_matches_standard_vectors(self):
        self.require_module()
        probe = self.compile_cpp_probe()
        for value in ("", "abc"):
            completed = subprocess.run(
                [probe, "--sha", value],
                text=True,
                stdout=subprocess.PIPE,
                check=True,
            )
            self.assertEqual(
                completed.stdout.strip(),
                hashlib.sha256(value.encode("ascii")).hexdigest(),
            )

    def test_cpp_reader_rejects_section_hash_drift(self):
        self.require_module()
        path = self.write_fixture("cpp-corrupt.reg2")
        payload = bytearray(path.read_bytes())
        first = mcfreg2.DIRECTORY.unpack_from(
            payload, self.directory_offset(0)
        )
        payload[first[3]] ^= 1
        path.write_bytes(payload)
        completed = subprocess.run(
            [self.compile_cpp_probe(), path],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("SHA-256", completed.stderr)

    def run_cpp_replayer(self, path, check=True):
        output_root = self.root / f"replay-{path.stem}"
        output_root.mkdir()
        return subprocess.run(
            [
                str(self.compile_cpp_probe()),
                "--replay",
                str(path),
                str(output_root / "canonical.trace"),
                str(output_root),
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=check,
        )

    def test_cpp_replayer_recomputes_all_calls(self):
        completed = self.run_cpp_replayer(
            self.write_semantic_fixture(), check=False
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(completed.stdout)
        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["pricing_calls"], 2)
        self.assertEqual(result["price_out_calls"], 4)
        self.assertGreater(result["operations"], 0)
        self.assertEqual(result["boundary_mismatches"], 0)

    def test_cpp_replayer_rejects_changed_selected_arc(self):
        completed = self.run_cpp_replayer(
            self.write_semantic_fixture(
                "selected-fault.reg2", fault="selected-arc"
            ),
            check=False,
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("selected arc differs", completed.stderr)

    def test_mcf_regions_dispatches_reg2_and_forbids_formal_reg1(self):
        binary = self.compile_mcf_regions()
        output_root = self.root / "regions-reg2"
        output_root.mkdir()
        completed = subprocess.run(
            [
                str(binary),
                "--input",
                str(self.write_semantic_fixture("dispatch.reg2")),
                "--output-root",
                str(output_root),
                "--trace",
                str(output_root / "canonical.trace"),
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn(
            "MATCHED_PHASE_INVOCATIONS=pricing_kernel:2",
            completed.stdout,
        )
        self.assertIn(
            "MATCHED_PHASE_INVOCATIONS=price_out_impl:4",
            completed.stdout,
        )
        materialized_sha256 = hashlib.sha256(
            (output_root / "canonical.trace").read_bytes()
        ).hexdigest()
        hash_root = self.root / "regions-reg2-hash-only"
        hash_root.mkdir()
        hash_only = subprocess.run(
            [
                str(binary),
                "--input",
                str(self.root / "dispatch.reg2"),
                "--output-root",
                str(hash_root),
                "--hash-only",
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(hash_only.returncode, 0, hash_only.stderr)
        self.assertFalse((hash_root / "canonical.trace").exists())
        hash_validation = json.loads(
            (hash_root / "mcfreg2-replay.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            hash_validation["trace_sha256"], materialized_sha256
        )
        legacy = self.root / "legacy.reg1"
        legacy.write_bytes(b"MCFREG1\0")
        rejected = subprocess.run(
            [
                str(binary),
                "--input",
                str(legacy),
                "--output-root",
                str(output_root),
                "--trace",
                str(output_root / "legacy.trace"),
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn("formal MCFREG1 is forbidden", rejected.stderr)


if __name__ == "__main__":
    unittest.main()
