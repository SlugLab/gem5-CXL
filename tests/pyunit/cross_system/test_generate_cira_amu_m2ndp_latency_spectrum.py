# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

import dataclasses
import hashlib
import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from scripts import cross_system_contract as contract
from scripts import cxl_latency_spectrum as latency
from scripts import generate_cira_amu_m2ndp_latency_spectrum as publisher
from scripts import run_cira_amu_m2ndp_breadth as breadth
from scripts import run_cira_amu_m2ndp_latency_spectrum as spectrum
from scripts import run_pr_asymmetric_offload as offload


def digest(value):
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def qualification(calibration_sha256):
    vanilla_ticks = 1_600_000
    points = {
        "g12:vanilla": {
            "scale": 12, "system": "vanilla", "sim_ticks": vanilla_ticks,
        },
        "g12:amu": {
            "scale": 12, "system": "amu",
            "sim_ticks": int(Decimal(vanilla_ticks) / Decimal("1.42")),
        },
        "g12:cira-few-shot": {
            "scale": 12, "system": "cira-few-shot",
            "sim_ticks": int(Decimal(vanilla_ticks) / Decimal("1.45")),
            "selected_candidate": "B",
        },
        "g12:m2ndp": {
            "scale": 12, "system": "m2ndp",
            "ndpsim_cycles": int(Decimal(vanilla_ticks) / Decimal("2.67")),
            "ndpsim_core_period_seconds": "1e-12",
        },
    }
    common = {
        "profile": "pr-offload-4thread-1us",
        "cxl_link_delay": "1us",
        "workers": 4,
        "iterations": 20,
        "all_memory_cxl": True,
        "verification": "pass",
        "raw_sha256": digest("rank-bits"),
        "worker_completions": [40, 40, 40, 40],
        "pending": {"all": 0},
    }
    for point in points.values():
        point.update({key: value for key, value in common.items() if key not in point})
    cira_ticks = points["g12:cira-few-shot"]["sim_ticks"]
    points["g12:cira-few-shot"]["phases"] = {
        "formation": 1, "sampling": 1, "selection": 1,
        "jit": 1, "execution": cira_ticks - 5, "drain": 1,
    }
    points["g12:cira-few-shot"]["phase_total_ns"] = cira_ticks
    points["g12:m2ndp"]["funcsim"] = {
        "status": "pass", "compared": 1 << 12,
        "mismatched": 0, "completed_at_seq": 1,
    }
    points["g12:m2ndp"]["ndpsim_started_at_seq"] = 2
    return {
        "schema": 1,
        "status": "passed",
        "profile": "pr-offload-4thread-1us",
        "identity": {"calibration_sha256": calibration_sha256},
        "performance_gate": offload.qualification_gate(points),
        "primary": points,
        "replay": json.loads(json.dumps(points)),
    }


def functional(system):
    row = {
        "status": "pass",
        "bit_exact": True,
        "compared_words": 16,
        "mismatched_words": 0,
        "outputs": {"rank": digest(f"{system}-rank")},
    }
    if system == "vanilla":
        row["error_counters"] = {}
    elif system == "amu":
        row.update({
            "issued_loads": 16, "completed_loads": 16,
            "drains": 4, "phases": 4,
            "error_counters": {
                "queue_full": 0, "spm_full": 0, "translation": 0,
                "pending": 0, "far_spm_flag": 0, "spm_missing_flag": 0,
            },
        })
    elif system == "cira":
        row.update({
            "issued_prefetches": 16, "completed_prefetches": 16,
            "issued_per_core": [4, 4, 4, 4],
            "completed_per_core": [4, 4, 4, 4],
            "error_counters": {
                "queue_full": 0, "csr_index_queue_full": 0,
                "dropped_descriptors": 0,
            },
        })
    else:
        row.update({
            "expected_operations": 16, "compared_operations": 16,
            "expected_launches": 4, "completed_launches": 4,
            "funcsim_status": "pass", "error_counters": {},
        })
    return row


class LatencySpectrumPublisherTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.campaign = self.root / "campaign"
        self.shared = {}
        for name in ("inputs", "calibration", "prepared"):
            path = self.root / "shared" / f"{name}.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(name + "\n", encoding="utf-8")
            self.shared[name] = {
                "path": str(path.resolve()),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        qualification_path = self.root / "shared/qualification.json"
        contract.atomic_write_json(
            qualification_path,
            qualification(self.shared["calibration"]["sha256"]),
        )
        qualification_record = spectrum.validate_qualification(
            qualification_path, self.shared["calibration"]["sha256"]
        )
        identity = spectrum._aggregate_identity(
            self.shared, qualification_record
        )
        self.aggregate = spectrum.new_state(
            self.shared, qualification_record, identity
        )
        for label in latency.LABELS:
            child = self._write_child(label)
            spectrum.record_child(
                self.aggregate, label, child, ["breadth", label]
            )
        spectrum.complete_state(self.aggregate, self.campaign)
        self.complete = self.campaign / "complete.json"

    def _write_child(self, label):
        child = self.campaign / "latency" / label
        child.mkdir(parents=True, exist_ok=True)
        evidence = child / "evidence.json"
        evidence.write_text(label + "\n", encoding="utf-8")
        child_identity = contract.ExperimentIdentity(
            code_sha256=digest("child-code"),
            input_manifest_sha256=self.shared["inputs"]["sha256"],
            calibration_manifest_sha256=self.shared["calibration"]["sha256"],
            trace_sha256=self.shared["prepared"]["sha256"],
            config_sha256=digest(f"config-{label}"),
        )
        contract.atomic_write_json(child / "identity.json", {
            "schema": 1,
            "digest": child_identity.digest(),
            "identity": dataclasses.asdict(child_identity),
        })
        workload_rows = {}
        results = {}
        for index, workload in enumerate(breadth.WORKLOADS, start=1):
            workload_rows[workload] = {
                "status": "complete",
                "functional": {
                    system: functional(system)
                    for system in breadth.FUNCTIONAL_SYSTEMS
                },
            }
            vanilla = Decimal(index) * Decimal("0.001") + Decimal(
                latency.ticks(label)
            ) * Decimal("1e-12")
            absolute = {"vanilla": vanilla}
            systems = {}
            for system, speedup in (
                ("amu", Decimal("1.42")),
                ("cira", Decimal("1.48")),
                ("m2ndp", Decimal("2.63")),
            ):
                absolute[system] = vanilla / speedup
                systems[system] = {
                    "speedup": str(speedup),
                    "ci_low": str(speedup - Decimal("0.01")),
                    "ci_high": str(speedup + Decimal("0.01")),
                    "relative_half_width": str(Decimal("0.01") / speedup),
                    "publishable": True,
                    "resamples": 10_000,
                }
            results[workload] = {
                "status": "complete",
                "level": 8,
                "absolute_seconds": {
                    system: str(seconds) for system, seconds in absolute.items()
                },
                "systems": systems,
                "publishable": True,
                "relative_half_width": "0.01",
            }
        complete = {
            "schema": 1,
            "status": "complete",
            "identity": dataclasses.asdict(child_identity),
            "identity_sha256": child_identity.digest(),
            "cxl_link_delay": label,
            "cxl_link_delay_ticks": latency.ticks(label),
            "g20_graph_sha256": digest("g20"),
            "workload_order": list(breadth.WORKLOADS),
            "workloads": workload_rows,
            "results": results,
            "evidence_files": {
                "evidence": {
                    "path": str(evidence.resolve()),
                    "sha256": hashlib.sha256(evidence.read_bytes()).hexdigest(),
                }
            },
        }
        contract.atomic_write_json(child / "complete.json", complete)
        return child

    def _rewrite_child(self, label, mutate):
        child_path = self.campaign / "latency" / label / "complete.json"
        child = json.loads(child_path.read_text(encoding="utf-8"))
        mutate(child)
        contract.atomic_write_json(child_path, child)
        aggregate = json.loads(self.complete.read_text(encoding="utf-8"))
        aggregate["latencies"][label]["complete"]["sha256"] = hashlib.sha256(
            child_path.read_bytes()
        ).hexdigest()
        contract.atomic_write_json(self.complete, aggregate)

    def test_loads_exact_96_rows_and_recomputes_same_latency_speedup(self):
        data = publisher.load_complete(self.complete)
        self.assertEqual(len(data.rows), 96)
        self.assertEqual(
            {(row.latency, row.workload, row.system) for row in data.rows},
            set(spectrum.coordinates()),
        )
        for row in data.rows:
            if row.system == "vanilla":
                self.assertEqual(row.speedup, Decimal(1))

    def test_rejects_tampered_named_evidence_before_loading_rows(self):
        evidence = self.campaign / "latency/500ns/evidence.json"
        evidence.write_text("changed\n", encoding="utf-8")
        with self.assertRaisesRegex(
            publisher.PublicationError, "evidence hashes"
        ):
            publisher.load_complete(self.complete)

    def test_rejects_stored_speedup_drift(self):
        self._rewrite_child(
            "1us",
            lambda child: child["results"]["mcf"]["systems"]["amu"].update(
                {"speedup": "1.99"}
            ),
        )
        with self.assertRaisesRegex(
            publisher.PublicationError, "stored speedup differs"
        ):
            publisher.load_complete(self.complete)

    def test_rejects_missing_latency_coordinate(self):
        aggregate = json.loads(self.complete.read_text(encoding="utf-8"))
        del aggregate["latencies"]["2us"]
        contract.atomic_write_json(self.complete, aggregate)
        with self.assertRaisesRegex(
            publisher.PublicationError, "latency matrix"
        ):
            publisher.load_complete(self.complete)

    def test_publish_writes_raw_table_two_composites_and_six_standalones(self):
        data = publisher.load_complete(self.complete)
        first = publisher.publish(data, self.root / "publication-a")
        second = publisher.publish(data, self.root / "publication-b")
        self.assertEqual(first["row_count"], 96)
        self.assertEqual(len(first["artifacts"]), 27)
        self.assertEqual(
            {name: row["sha256"] for name, row in first["artifacts"].items()},
            {name: row["sha256"] for name, row in second["artifacts"].items()},
        )
        image_names = [
            name for name in first["artifacts"]
            if name.endswith((".pdf", ".svg", ".png"))
        ]
        self.assertEqual(len(image_names), 24)
        for workload in breadth.WORKLOADS:
            for suffix in ("pdf", "svg", "png"):
                self.assertIn(
                    f"fig/standalone/{workload}-latency-spectrum.{suffix}",
                    first["artifacts"],
                )

    def test_composite_uses_one_shared_speedup_scale(self):
        data = publisher.load_complete(self.complete)
        selected = [
            row for row in data.rows if row.system in publisher.ACCELERATORS
        ]
        limits = publisher._global_speedup_limits(selected)
        _matplotlib, plt, _np = publisher._matplotlib()
        figure, axes = plt.subplots(2, 1)
        try:
            publisher._speedup_axis(axes[0], selected[:12], limits=limits)
            publisher._speedup_axis(axes[1], selected[12:24], limits=limits)
            self.assertEqual(axes[0].get_ylim(), axes[1].get_ylim())
            self.assertLessEqual(axes[0].get_ylim()[0], 1.0)
            self.assertGreaterEqual(axes[0].get_ylim()[1], 1.0)
        finally:
            plt.close(figure)

    def test_grouped_speedup_bars_start_at_zero(self):
        data = publisher.load_complete(self.complete)
        selected = [
            row for row in data.rows
            if row.latency == "1us" and row.system in publisher.ACCELERATORS
        ]
        _matplotlib, plt, _np = publisher._matplotlib()
        figure, axis = plt.subplots()
        try:
            publisher._bar_speedup_axis(axis, selected)
            self.assertEqual(axis.get_ylim()[0], 0.0)
            self.assertGreater(axis.get_ylim()[1], max(
                float(row.ci_high) for row in selected
            ))
        finally:
            plt.close(figure)


if __name__ == "__main__":
    unittest.main()
