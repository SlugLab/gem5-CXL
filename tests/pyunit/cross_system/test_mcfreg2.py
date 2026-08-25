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

    def strict_semantic_fixture(self, mutation=None, *, pricing_only=False):
        pricing_live_in = mcfreg2.PricingLiveIn(
            ordinal=0,
            m=1,
            nr_group=1,
            group_pos=0,
            initialize=False,
            basket=(mcfreg2.BasketState(
                slot=1,
                arc=mcfreg2.StableRef(mcfreg2.OBJECT_ARC, 0, 0),
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
            basket=(
                mcfreg2.BasketState(
                    slot=1,
                    arc=mcfreg2.StableRef(mcfreg2.OBJECT_ARC, 0, 0),
                    cost=-4,
                    abs_cost=4,
                ),
                mcfreg2.BasketState(
                    slot=2,
                    arc=mcfreg2.StableRef(mcfreg2.OBJECT_ARC, 0, 0),
                    cost=-2,
                    abs_cost=2,
                ),
            ),
            selected_arc=mcfreg2.StableRef(
                mcfreg2.OBJECT_ARC, 0, 0
            ),
            selected_reduced_cost=-4,
            arcs_priced=1,
            nr_group=1,
            group_pos=0,
            initialize=False,
        )
        null = mcfreg2.StableRef.null()
        resize_price_out = mutation in {
            "price-out-resize",
            "price-out-resize-missing-remap",
            "price-out-resize-stale-generation",
        }
        arc_count = 6 if resize_price_out else 3
        nodes = tuple(
            mcfreg2.ObjectState(
                mcfreg2.StableRef(mcfreg2.OBJECT_NODE, 0, index),
                (0,) * 6,
                (null,) * 8,
            )
            for index in range(3)
        )
        arcs = tuple(
            mcfreg2.ObjectState(
                mcfreg2.StableRef(mcfreg2.OBJECT_ARC, 0, index),
                (
                    10 + index,
                    -1 if mutation == "price-out-sparse-prefix" and
                    index == 1 else 0,
                    0,
                    10 + index,
                ),
                (
                    mcfreg2.StableRef(
                        mcfreg2.OBJECT_NODE, 0, index % 2
                    ),
                    mcfreg2.StableRef(
                        mcfreg2.OBJECT_NODE, 0, (index + 1) % 2
                    ),
                    null,
                    null,
                ),
            )
            for index in range(arc_count)
        )
        dummy_arcs = tuple(
            mcfreg2.ObjectState(
                mcfreg2.StableRef(mcfreg2.OBJECT_DUMMY_ARC, 0, index),
                (0,) * 4,
                (
                    mcfreg2.StableRef(mcfreg2.OBJECT_NODE, 0, index),
                    mcfreg2.StableRef(mcfreg2.OBJECT_NODE, 0, 2),
                    null,
                    null,
                ),
            )
            for index in range(2)
        )
        price_out_objects = nodes + arcs + dummy_arcs
        price_out_words = (
            2,
            2 if resize_price_out else 1,
            6 if resize_price_out else 4,
            arc_count,
            arc_count,
            0,
            2 if resize_price_out else 1,
            2 if resize_price_out else 1,
            0, 0, 0, 0, 0, 0, 0, 0, 100, 0, 0, 0, 0, 0,
            arc_count,
        )
        price_out_live_in = mcfreg2.PriceOutLiveIn(
            ordinal=0,
            network_words=price_out_words,
            objects=price_out_objects,
            arena_generation=0,
            arena_capacity=6 if resize_price_out else 4,
            heap=(),
        )
        observed_words = list(price_out_words)
        observed_objects = price_out_objects
        observed_generation = 0
        observed_capacity = 4
        if resize_price_out:
            observed_words[2] = 8
            observed_words[6] = 4
            observed_generation = 1
            observed_capacity = 8
            resized_nodes = []
            for index, node in enumerate(nodes):
                links = list(node.links)
                if index == 0:
                    links[5] = mcfreg2.StableRef(
                        mcfreg2.OBJECT_ARC, 1, 4
                    )
                    links[6] = mcfreg2.StableRef(
                        mcfreg2.OBJECT_ARC, 1, 5
                    )
                elif index == 1:
                    links[5] = mcfreg2.StableRef(
                        mcfreg2.OBJECT_ARC, 1, 5
                    )
                    links[6] = mcfreg2.StableRef(
                        mcfreg2.OBJECT_ARC, 1, 4
                    )
                resized_nodes.append(dataclasses.replace(
                    node, links=tuple(links)
                ))
            resized_arcs = []
            for index, arc in enumerate(arcs):
                links = list(arc.links)
                previous = (
                    mcfreg2.StableRef(
                        mcfreg2.OBJECT_ARC, 1, index - 2
                    ) if index >= 2 else null
                )
                links[2] = previous
                links[3] = previous
                resized_arcs.append(mcfreg2.ObjectState(
                    mcfreg2.StableRef(mcfreg2.OBJECT_ARC, 1, index),
                    arc.words,
                    tuple(links),
                ))
            observed_objects = (
                tuple(resized_nodes) + tuple(resized_arcs) + dummy_arcs
            )
            if mutation == "price-out-resize-stale-generation":
                stale = dataclasses.replace(
                    observed_objects[3],
                    reference=mcfreg2.StableRef(
                        mcfreg2.OBJECT_ARC, 0, 0
                    ),
                )
                observed_objects = (
                    observed_objects[:3] + (stale,) +
                    observed_objects[4:]
                )
        price_out_observed = mcfreg2.PriceOutDerivedOut(
            ordinal=0,
            network_words=tuple(observed_words),
            objects=observed_objects,
            arena_generation=observed_generation,
            arena_capacity=observed_capacity,
            heap=(),
            candidates=(),
            decisions=(),
        )
        events = [
            {"kind": "CALL_BEGIN", "role": "live_in", "call": 0,
             "order": 0, "ordinal": 0, "phase": "pricing", "m": 1,
             "nr_group": 1, "group_pos": 0, "initialize": False},
            {"kind": "BASKET_LIVE_IN", "role": "live_in", "call": 0,
             "slot": 1, "arc": {"kind": "arc", "generation": 0,
             "index": 0}, "cost": -4, "abs_cost": 4},
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
             "cost": -4, "abs_cost": 4},
            {"kind": "BASKET_LIVE_OUT_OBSERVED",
             "role": "observed_result", "call": 0, "slot": 2,
             "arc": {"kind": "arc", "generation": 0, "index": 0},
             "cost": -2, "abs_cost": 2},
            {"kind": "PRICING_END_OBSERVED",
             "role": "observed_result", "call": 0,
             "selected_arc": {"kind": "arc", "generation": 0,
             "index": 0}, "selected_reduced_cost": -4,
             "arcs_priced": 1, "nr_group": 1, "group_pos": 0,
             "initialize": False},
            {"kind": "CALL_END", "role": "observed_result", "call": 0,
             "order": 0, "ordinal": 0, "phase": "pricing"},
        ]
        if mutation == "result-in-live-in":
            events[2]["selected_arc_id"] = 4
        elif mutation == "coupled-pricing-output":
            events[3]["reduced_cost"] = -1
            events[5]["cost"] = -1
            events[5]["abs_cost"] = 1
            pricing_out = dataclasses.replace(
                pricing_out,
                candidates=(dataclasses.replace(
                    pricing_out.candidates[0], reduced_cost=-1
                ),),
                basket=(
                    pricing_out.basket[0],
                    dataclasses.replace(
                        pricing_out.basket[1], cost=-1, abs_cost=1
                    ),
                ),
            )
        elif mutation == "tail-potential-results-only":
            events[2]["tail_potential"] = 11
            events[3]["reduced_cost"] = -3
            events[5]["cost"] = -3
            events[5]["abs_cost"] = 3
            pricing_out = dataclasses.replace(
                pricing_out,
                candidates=(dataclasses.replace(
                    pricing_out.candidates[0], reduced_cost=-3
                ),),
                basket=(
                    pricing_out.basket[0],
                    dataclasses.replace(
                        pricing_out.basket[1], cost=-3, abs_cost=3
                    ),
                ),
            )
        pricing_event_count = len(events)
        null_ref = {
            "kind": "null", "generation": 0,
            "index": mcfreg2.UINT64_MAX,
        }
        def ref_record(reference):
            names = {
                mcfreg2.OBJECT_NULL: "null",
                mcfreg2.OBJECT_NODE: "node",
                mcfreg2.OBJECT_ARC: "arc",
                mcfreg2.OBJECT_DUMMY_ARC: "dummy_arc",
            }
            return {
                "kind": names[reference.kind],
                "generation": reference.generation,
                "index": reference.object_id,
            }

        def object_record(obj):
            return {
                "reference": ref_record(obj.reference),
                "words": list(obj.words),
                "links": [ref_record(link) for link in obj.links],
            }

        price_out_live_state_record = {
            "network_words": list(price_out_words),
            "objects": [object_record(obj) for obj in price_out_objects],
            "arena_generation": 0,
            "arena_capacity": 6 if resize_price_out else 4,
            "heap": [],
        }
        price_out_end_state_record = {
            "network_words": list(observed_words),
            "objects": [object_record(obj) for obj in observed_objects],
            "arena_generation": observed_generation,
            "arena_capacity": observed_capacity,
            "heap": [],
        }
        remap_events = []
        arc_final_events = []
        if resize_price_out:
            remap_events = [
                {"kind": "REMAP_OBSERVED",
                 "role": "observed_result", "call": 0,
                 "old_reference": ref_record(arc.reference),
                 "new_reference": ref_record(
                     mcfreg2.StableRef(
                         mcfreg2.OBJECT_ARC, 1, arc.reference.object_id
                     )
                 )}
                for arc in arcs
            ]
            arc_final_events = [
                {"kind": "ARC_FINAL_OBSERVED",
                 "role": "observed_result", "call": 0,
                 "reference": ref_record(arc.reference),
                 "tail": ref_record(arc.links[0]),
                 "head": ref_record(arc.links[1]),
                 "cost": arc.words[0], "org_cost": arc.words[3],
                 "flow": arc.words[2], "ident": arc.words[1],
                 "nextout": ref_record(arc.links[2]),
                 "nextin": ref_record(arc.links[3])}
                for arc in observed_objects[3:3 + arc_count]
            ]
        price_out_events = [
            {"kind": "CALL_BEGIN", "role": "live_in", "call": 0,
             "order": 1, "ordinal": 0, "phase": "price_out"},
            {"kind": "PRICE_OUT_STATE_LIVE_IN", "role": "live_in",
             "call": 0, **price_out_live_state_record},
            *remap_events,
            *arc_final_events,
            *(
                {"kind": "ADJACENCY_FINAL_OBSERVED",
                 "role": "observed_result", "call": 0,
                 "reference": ref_record(node.reference),
                 "firstout": ref_record(node.links[5]),
                 "firstin": ref_record(node.links[6])}
                for node in observed_objects[:3]
            ),
            {"kind": "PRICE_OUT_END_OBSERVED",
             "role": "observed_result", "call": 0,
             **price_out_end_state_record},
            {"kind": "CALL_END", "role": "observed_result", "call": 0,
             "order": 1, "ordinal": 0, "phase": "price_out"},
        ]
        if mutation == "price-out-coupled-candidate":
            candidate = mcfreg2.PriceOutCandidate(
                0,
                mcfreg2.StableRef(mcfreg2.OBJECT_NODE, 0, 0),
                mcfreg2.StableRef(mcfreg2.OBJECT_NODE, 0, 1),
                30,
                -1,
            )
            decision = mcfreg2.PriceOutDecision(
                0, "NO_CHANGE", mcfreg2.StableRef.null()
            )
            price_out_observed = dataclasses.replace(
                price_out_observed,
                candidates=(candidate,),
                decisions=(decision,),
            )
            price_out_events[2:2] = [
                {"kind": "PRICE_OUT_CANDIDATE_OBSERVED",
                 "role": "observed_result", "call": 0,
                 "candidate": 0,
                 "tail": ref_record(candidate.tail),
                 "head": ref_record(candidate.head),
                 "cost": 30, "reduced_cost": -1},
                {"kind": "PRICE_OUT_DECISION_OBSERVED",
                 "role": "observed_result", "call": 0,
                 "candidate": 0, "decision": "NO_CHANGE",
                 "reference": null_ref},
            ]
        elif mutation == "price-out-wrong-counter":
            changed_words = list(price_out_words)
            changed_words[5] = 1
            price_out_observed = dataclasses.replace(
                price_out_observed, network_words=tuple(changed_words)
            )
            next(
                row for row in price_out_events
                if row["kind"] == "PRICE_OUT_END_OBSERVED"
            )["network_words"] = changed_words
        elif mutation == "price-out-missing-adjacency":
            del price_out_events[2]
        elif mutation == "price-out-resize-missing-remap":
            missing = next(
                index for index, row in enumerate(price_out_events)
                if row["kind"] == "REMAP_OBSERVED"
            )
            del price_out_events[missing]
        if not pricing_only:
            events.extend(price_out_events)
        boundary = {
            "schema": 3,
            "rows": [
                {"call": 0, "order": 0, "phase": "pricing",
                 "pre_sha256": mcfreg2.digest_call_state(pricing_live_in),
                 "post_sha256": mcfreg2.digest_call_state(pricing_out)},
                *([] if pricing_only else [{
                    "call": 0, "order": 1, "phase": "price_out",
                    "pre_sha256": mcfreg2.digest_call_state(
                        price_out_live_in
                    ),
                    "post_sha256": mcfreg2.digest_call_state(
                        price_out_observed
                    ),
                }]),
            ],
        }
        call_index = {"schema": 3, "rows": [
            {"call": 0, "order": 0, "ordinal": 0, "phase": "pricing",
             "event_begin": 0, "event_count": pricing_event_count},
            *([] if pricing_only else [{
                "call": 0, "order": 1, "ordinal": 0,
                "phase": "price_out", "event_begin": pricing_event_count,
                "event_count": len(events) - pricing_event_count,
            }]),
        ]}
        sections = {
            name: f"{name.lower()}\n".encode("ascii")
            for name in mcfreg2.REQUIRED_SECTIONS
        }
        network_words = [3, 2, 16, 6] + [0] * 18
        null_reference = mcfreg2.StableRef.null().pack()
        node_record = bytearray(generator.STATE_NODE_BYTES)
        for offset in range(16, 144, 16):
            node_record[offset:offset + 16] = null_reference
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
        sections["NETWORK"] = struct.pack("<22Q", *network_words)
        sections["NODES"] = bytes(node_record) * 4
        sections["ARCS"] = b"".join(arc_records)
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
            pricing_calls=1, price_out_calls=0 if pricing_only else 1,
            event_count=len(events),
            sections=sections,
            section_schemas={
                "EVENTS": 3, "CALL_INDEX": 3, "BOUNDARIES": 3,
            },
            section_layouts={
                "NETWORK": (22, 8), "NODES": (4, 176),
                "ARCS": (9, 96), "EVENTS": (len(events), 0),
            },
        )
        return package

    def write_strict_semantic_fixture(
        self, name="strict-semantic.reg2", mutation=None, *,
        pricing_only=True
    ):
        path = self.root / name
        mcfreg2.write_package(
            path,
            self.strict_semantic_fixture(
                mutation, pricing_only=pricing_only
            ),
        )
        return path

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
                str(self.repo / "util/amu/matched_workloads/mcfreg2_kernels.cc"),
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
        event_section = next(
            section for section in actual.sections
            if section.section_type == mcfreg2.SECTION_TYPES["EVENTS"]
        )
        self.assertIsInstance(event_section.data, mcfreg2.SectionView)
        self.assertEqual(actual.section("EVENTS"), events.read_bytes())

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
                str(
                    self.repo
                    / "util/amu/matched_workloads/mcfreg2_state.cc"
                ),
                str(
                    self.repo
                    / "util/amu/matched_workloads/mcfreg2_kernels.cc"
                ),
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
                str(
                    self.repo
                    / "util/amu/matched_workloads/mcfreg2_state.cc"
                ),
                str(
                    self.repo
                    / "util/amu/matched_workloads/mcfreg2_kernels.cc"
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

    def test_cpp_reader_rss_is_not_proportional_to_section_bytes(self):
        timer = Path("/usr/bin/time")
        if not timer.is_file():
            self.skipTest("/usr/bin/time is unavailable")
        probe = self.compile_cpp_probe()

        def package_with_payload(name, size):
            payload = self.root / f"{name}.payload"
            with payload.open("wb") as stream:
                stream.seek(size - 1)
                stream.write(b"\0")
            package = self.fixture_package()
            package = dataclasses.replace(
                package,
                sections=tuple(
                    dataclasses.replace(
                        section,
                        data=payload,
                        element_count=1,
                        element_size=size,
                    )
                    if section.section_type ==
                    mcfreg2.SECTION_TYPES["PROVENANCE"] else section
                    for section in package.sections
                ),
            )
            path = self.root / f"{name}.reg2"
            mcfreg2.write_package(path, package)
            return path

        def maximum_rss(path, name):
            output = self.root / f"{name}.rss"
            subprocess.run(
                [timer, "-f", "%M", "-o", output, probe, path],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                check=True,
            )
            return int(output.read_text(encoding="ascii").strip())

        small = maximum_rss(
            package_with_payload("small", 1 * 1024 * 1024), "small"
        )
        large = maximum_rss(
            package_with_payload("large", 96 * 1024 * 1024), "large"
        )
        self.assertLessEqual(large - small, 64 * 1024)

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

    def test_pricing_rejects_coupled_result_forgery(self):
        completed = self.run_cpp_replayer(
            self.write_strict_semantic_fixture(
                "coupled-pricing.reg2", "coupled-pricing-output"
            ),
            check=False,
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("derived pricing result differs", completed.stderr)

    def test_pricing_replays_from_live_ins(self):
        completed = self.run_cpp_replayer(
            self.write_strict_semantic_fixture(), check=False
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(completed.stdout)
        self.assertEqual(result["pricing_calls"], 1)
        self.assertEqual(result["price_out_calls"], 0)
        self.assertGreater(result["operations"], 0)
        self.assertEqual(result["boundary_mismatches"], 0)

    def test_price_out_replays_no_candidate_state_from_live_ins(self):
        completed = self.run_cpp_replayer(
            self.write_strict_semantic_fixture(
                "price-out-no-candidate.reg2", pricing_only=False
            ),
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(completed.stdout)
        self.assertEqual(result["pricing_calls"], 1)
        self.assertEqual(result["price_out_calls"], 1)
        self.assertGreater(result["operations"], 0)
        self.assertEqual(result["boundary_mismatches"], 0)

    def test_price_out_rejects_coupled_candidate_and_decision(self):
        completed = self.run_cpp_replayer(
            self.write_strict_semantic_fixture(
                "price-out-coupled-candidate.reg2",
                "price-out-coupled-candidate",
                pricing_only=False,
            ),
            check=False,
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("derived price-out result differs", completed.stderr)

    def test_price_out_rejects_wrong_derived_counter(self):
        completed = self.run_cpp_replayer(
            self.write_strict_semantic_fixture(
                "price-out-wrong-counter.reg2",
                "price-out-wrong-counter",
                pricing_only=False,
            ),
            check=False,
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("derived price-out result differs", completed.stderr)

    def test_price_out_rejects_missing_adjacency_delta(self):
        completed = self.run_cpp_replayer(
            self.write_strict_semantic_fixture(
                "price-out-missing-adjacency.reg2",
                "price-out-missing-adjacency",
                pricing_only=False,
            ),
            check=False,
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("adjacency deltas differ", completed.stderr)

    def test_price_out_rejects_native_undefined_sparse_prefix(self):
        completed = self.run_cpp_replayer(
            self.write_strict_semantic_fixture(
                "price-out-sparse-prefix.reg2",
                "price-out-sparse-prefix",
                pricing_only=False,
            ),
            check=False,
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("undefined native first_of_sparse_list", completed.stderr)

    def test_price_out_replays_resize_and_complete_remap(self):
        completed = self.run_cpp_replayer(
            self.write_strict_semantic_fixture(
                "price-out-resize.reg2",
                "price-out-resize",
                pricing_only=False,
            ),
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_price_out_rejects_omitted_resize_remap(self):
        completed = self.run_cpp_replayer(
            self.write_strict_semantic_fixture(
                "price-out-resize-missing-remap.reg2",
                "price-out-resize-missing-remap",
                pricing_only=False,
            ),
            check=False,
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("remap deltas differ", completed.stderr)

    def test_price_out_rejects_stale_generation_after_resize(self):
        completed = self.run_cpp_replayer(
            self.write_strict_semantic_fixture(
                "price-out-resize-stale-generation.reg2",
                "price-out-resize-stale-generation",
                pricing_only=False,
            ),
            check=False,
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("derived price-out result differs", completed.stderr)

    def test_pricing_live_in_change_requires_pre_boundary(self):
        completed = self.run_cpp_replayer(
            self.write_strict_semantic_fixture(
                "pricing-live-in-fault.reg2",
                "tail-potential-results-only",
            ),
            check=False,
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("pricing pre-boundary differs", completed.stderr)

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
