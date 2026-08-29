# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

import hashlib
import inspect
import atexit
import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from scripts import cross_system_contract as contract
from scripts import run_cira_amu_m2ndp_breadth as breadth


_BOUNDARY_TEMPORARY = tempfile.TemporaryDirectory()
atexit.register(_BOUNDARY_TEMPORARY.cleanup)
_BOUNDARY_PATH = Path(_BOUNDARY_TEMPORARY.name) / "objective.u64"
_BOUNDARY_PATH.write_bytes((7).to_bytes(8, "little"))
_BOUNDARY_SHA256 = hashlib.sha256(_BOUNDARY_PATH.read_bytes()).hexdigest()


def sha(label):
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def identity(code="code"):
    return contract.ExperimentIdentity(
        code_sha256=sha(code),
        input_manifest_sha256=sha("inputs"),
        calibration_manifest_sha256=sha("calibration"),
        trace_sha256=sha("traces"),
        config_sha256=sha("config"),
    )


def specs():
    return {
        "mcf": {
            "trace_sha256": sha("mcf-trace"),
            "phases": {
                "pricing": 819200,
                "price_out": 204800,
            },
        }
    }


def boundaries():
    return {
        "objective": {
            "path": str(_BOUNDARY_PATH.resolve()),
            "sha256": _BOUNDARY_SHA256,
            "word_bits": 64,
            "count": 1,
        }
    }


def mechanism(system, *, bad=False, timing=False):
    if system == "vanilla":
        return {"error_counters": {}}
    if system == "amu":
        return {
            "issued_loads": 8, "completed_loads": 8, "drains": 1,
            "phases": 1,
            "error_counters": {
                "queue_full": 1 if bad else 0, "spm_full": 0,
                "translation": 0, "pending": 0, "far_spm_flag": 0,
                "spm_missing_flag": 0,
            },
        }
    if system == "cira":
        return {
            "issued_prefetches": 8, "completed_prefetches": 8,
            "issued_per_core": [2, 2, 2, 2],
            "completed_per_core": [2, 2, 2, 2],
            "error_counters": {
                "queue_full": 0, "csr_index_queue_full": 0,
                "dropped_descriptors": 0,
            },
        }
    row = {
        "expected_operations": 12, "compared_operations": 12,
        "expected_launches": 2, "completed_launches": 2,
        "funcsim_status": "pass", "error_counters": {},
    }
    if timing:
        row.update({
            "memory_match": "pass", "calibration_pass": True,
            "calibration_residual_ns": "0.01",
            "calibration_link_period_ns": "0.125",
        })
    return row


def functional_state(*, cxl_link_delay="1us"):
    state = breadth.new_state(
        identity(), specs(), g20_graph_sha256=sha("g20"),
        cxl_link_delay=cxl_link_delay,
    )
    breadth.record_reference(state, "mcf", boundaries())
    for system in breadth.FUNCTIONAL_SYSTEMS:
        breadth.record_functional(
            state,
            "mcf",
            system,
            {
                "status": "pass",
                "bit_exact": True,
                "compared_words": 1,
                "mismatched_words": 0,
                "boundaries": boundaries(),
                "outputs": {"objective": sha(system)},
                **mechanism(system),
            },
        )
    breadth.begin_timing(state)
    return state


class BreadthRunnerTest(unittest.TestCase):
    def test_native_verified_accepts_numerically_valid_boundary_hash_drift(self):
        with tempfile.TemporaryDirectory() as temporary:
            relaxed_path = Path(temporary) / "objective.u64"
            relaxed_path.write_bytes((8).to_bytes(8, "little"))
            relaxed_boundaries = {
                "objective": {
                    "path": str(relaxed_path.resolve()),
                    "sha256": hashlib.sha256(
                        relaxed_path.read_bytes()
                    ).hexdigest(),
                    "word_bits": 64,
                    "count": 1,
                }
            }
            state = breadth.new_state(
                identity(), specs(), g20_graph_sha256=sha("g20"),
                correctness_policy="native-verified",
            )
            breadth.record_reference(state, "mcf", boundaries())
            breadth.record_functional(
                state,
                "mcf",
                "cira",
                {
                    "status": "pass",
                    "verification": "pass",
                    "numeric_verification": "pass",
                    "bit_exact": False,
                    "compared_words": 1,
                    "mismatched_words": 1,
                    "nonfinite_words": 0,
                    "boundaries": relaxed_boundaries,
                    "outputs": {"objective": sha("relaxed-objective")},
                    **mechanism("cira"),
                },
            )
            self.assertEqual(state["correctness_policy"], "native-verified")
            self.assertEqual(
                state["workloads"]["mcf"]["functional"]["cira"][
                    "status"
                ],
                "pass",
            )

    def test_bit_exact_policy_rejects_same_boundary_hash_drift(self):
        with tempfile.TemporaryDirectory() as temporary:
            relaxed_path = Path(temporary) / "objective.u64"
            relaxed_path.write_bytes((8).to_bytes(8, "little"))
            relaxed_boundaries = {
                "objective": {
                    "path": str(relaxed_path.resolve()),
                    "sha256": hashlib.sha256(
                        relaxed_path.read_bytes()
                    ).hexdigest(),
                    "word_bits": 64,
                    "count": 1,
                }
            }
            state = breadth.new_state(
                identity(), specs(), g20_graph_sha256=sha("g20"),
                correctness_policy="bit-exact",
            )
            breadth.record_reference(state, "mcf", boundaries())
            with self.assertRaisesRegex(
                breadth.BreadthError, "raw output boundary hashes differ"
            ):
                breadth.record_functional(
                    state,
                    "mcf",
                    "cira",
                    {
                        "status": "pass",
                        "verification": "pass",
                        "numeric_verification": "pass",
                        "bit_exact": False,
                        "compared_words": 1,
                        "mismatched_words": 1,
                        "nonfinite_words": 0,
                        "boundaries": relaxed_boundaries,
                        "outputs": {"objective": sha("relaxed-objective")},
                        **mechanism("cira"),
                    },
                )

    def test_native_verified_timing_accepts_numerically_valid_drift(self):
        state = breadth.new_state(
            identity(), specs(), g20_graph_sha256=sha("g20"),
            correctness_policy="native-verified",
        )
        evidence = {
            "verification": "pass",
            "numeric_verification": "pass",
            "bit_exact": False,
            "compared_words": 1,
            "mismatched_words": 1,
            "nonfinite_words": 0,
            "threads": 4,
            "all_memory_cxl": True,
            "allocated_on_cxl": True,
            "cxl_link_delay": "1us",
            "cxl_link_delay_ticks": 1_000_000,
            "error_counters": {},
            "boundaries": boundaries(),
            "fixed_seconds": "0.01",
            "seconds_per_item": "0.000001",
        }
        breadth._validate_window_evidence("vanilla", evidence, state)

    def test_native_verified_rejects_failed_numeric_verification(self):
        state = breadth.new_state(
            identity(), specs(), g20_graph_sha256=sha("g20"),
            correctness_policy="native-verified",
        )
        evidence = {
            "verification": "pass",
            "numeric_verification": "failed",
            "bit_exact": False,
            "compared_words": 1,
            "mismatched_words": 1,
            "nonfinite_words": 0,
            "threads": 4,
            "all_memory_cxl": True,
            "allocated_on_cxl": True,
            "cxl_link_delay": "1us",
            "cxl_link_delay_ticks": 1_000_000,
            "error_counters": {},
            "boundaries": boundaries(),
            "fixed_seconds": "0.01",
            "seconds_per_item": "0.000001",
        }
        with self.assertRaisesRegex(
            breadth.BreadthError, "correctness or 4-thread all-CXL gate"
        ):
            breadth._validate_window_evidence("vanilla", evidence, state)

    def test_functional_adapter_preserves_native_verification_fields(self):
        record = breadth._functional_record({
            "outputs": {"objective": {
                "path": str(_BOUNDARY_PATH.resolve()),
                "sha256": _BOUNDARY_SHA256,
            }},
            "bit_exact": False,
            "verification": "pass",
            "numeric_verification": "pass",
            "compared_words": 1,
            "mismatched_words": 1,
            "nonfinite_words": 0,
            "error_counters": {},
            "boundaries": boundaries(),
        })
        self.assertEqual(record["verification"], "pass")
        self.assertEqual(record["numeric_verification"], "pass")
        self.assertEqual(record["nonfinite_words"], 0)

    def test_cli_accepts_native_verified_correctness_policy(self):
        options = breadth.parse_args([
            "--inputs", "inputs.json",
            "--calibration", "calibration.json",
            "--root", "evidence",
            "--correctness-policy", "native-verified",
        ])
        self.assertEqual(options.correctness_policy, "native-verified")

    def test_breadth_state_records_canonical_cxl_latency(self):
        state = breadth.new_state(
            identity(), specs(), g20_graph_sha256=sha("g20"),
            cxl_link_delay="500ns",
        )
        self.assertEqual(state["cxl_link_delay"], "500ns")
        self.assertEqual(state["cxl_link_delay_ticks"], 500_000)

    def test_action_rendering_includes_canonical_cxl_latency(self):
        action = breadth.Action(
            "window", "mcf", system="vanilla", phase="pricing",
            cxl_link_delay="500ns", cxl_link_delay_ticks=500_000,
        )
        self.assertEqual(
            breadth._render(
                "timing/{{cxl_link_delay}}/{{cxl_link_delay_ticks}}", action
            ),
            "timing/500ns/500000",
        )

    def test_500ns_executor_rejects_fixed_1us_timing_action(self):
        manifest = {
            "workloads": {"mcf": {"actions": {"window": {
                "pricing": {"vanilla": {
                    "command": [
                        "python3", "scripts/run_matched_breadth_gem5.py",
                        "--cxl-link-delay", "1us",
                    ],
                    "evidence": "timing/1us/mcf/vanilla.json",
                }}
            }}}}
        }
        executor = breadth.ManifestExecutor(
            manifest, root=".", cxl_link_delay="500ns"
        )
        with self.assertRaisesRegex(
            breadth.BreadthError, "prepared timing action CXL latency differs"
        ):
            executor(breadth.Action(
                "window", "mcf", system="vanilla", phase="pricing",
                window_index=0, level=8, stratum=0,
                warmup_start=0, measure_start=1, measure_stop=2,
            ))

    def test_500ns_window_rejects_1us_evidence(self):
        state = functional_state(cxl_link_delay="500ns")
        evidence = {
            "verification": "pass", "bit_exact": True,
            "mismatched_words": 0, "threads": 4,
            "all_memory_cxl": True, "allocated_on_cxl": True,
            "cxl_link_delay_ticks": 1_000_000,
        }
        with self.assertRaisesRegex(
            breadth.BreadthError, "timing evidence CXL latency differs"
        ):
            breadth._validate_window_evidence("vanilla", evidence, state)

    def test_500ns_resume_rejects_1us_checkpoint(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "boundary.raw"
            output.write_bytes(b"proof")
            state = functional_state(cxl_link_delay="1us")
            breadth.write_checkpoint(
                root, state, boundary="functional",
                outputs={"boundary": {
                    "path": str(output),
                    "sha256": hashlib.sha256(b"proof").hexdigest(),
                }},
            )
            selected, rejected = breadth.select_resume(
                root, identity().digest(), cxl_link_delay="500ns"
            )
            self.assertIsNone(selected)
            self.assertEqual(len(rejected), 1)

    def test_breadth_state_records_g20_graph_identity(self):
        state = breadth.new_state(
            identity(), specs(), g20_graph_sha256=sha("g20")
        )
        self.assertEqual(state["g20_graph_sha256"], sha("g20"))

    def test_breadth_input_g20_graph_matches_pr_workload(self):
        inputs = {
            "graphs": [{"scale": 20, "sha256": sha("g20")}],
            "workloads": {
                "pr_spmv": {"input_sha256": sha("g20")}
            },
        }
        self.assertEqual(breadth._g20_graph_sha256(inputs), sha("g20"))
        inputs["workloads"]["pr_spmv"]["input_sha256"] = sha("other")
        with self.assertRaisesRegex(breadth.BreadthError, "g20 graph"):
            breadth._g20_graph_sha256(inputs)

    def test_functional_pass_precedes_timing(self):
        state = breadth.new_state(
            identity(), specs(), g20_graph_sha256=sha("g20")
        )
        self.assertEqual(breadth.next_action(state).stage, "reference")
        breadth.record_reference(state, "mcf", boundaries())
        self.assertEqual(breadth.next_action(state).stage, "functional")
        self.assertNotEqual(breadth.next_action(state).stage, "window")

    def test_functional_gate_requires_exact_four_system_set(self):
        records = {
            system: {
                "status": "pass",
                "bit_exact": True,
                "compared_words": 1,
                "mismatched_words": 0,
                "error_counters": {},
                "boundary_hashes": {"objective": sha("reference")},
            }
            for system in breadth.FUNCTIONAL_SYSTEMS[:-1]
        }
        self.assertFalse(breadth.functional_complete(records))
        records[breadth.FUNCTIONAL_SYSTEMS[-1]] = {
            "status": "pass",
            "bit_exact": True,
            "compared_words": 1,
            "mismatched_words": 0,
            "error_counters": {},
            "boundary_hashes": {"objective": sha("reference")},
        }
        self.assertTrue(breadth.functional_complete(records))

    def test_identical_coordinates_across_four_systems(self):
        state = functional_state()
        for system in breadth.TIMING_SYSTEMS:
            breadth.record_fixed(state, "mcf", system, "0.01")
        actions = breadth.pending_window_actions(state, "mcf", "pricing")
        grouped = {}
        for action in actions:
            grouped.setdefault(action.window_index, []).append(action)
        self.assertEqual(len(grouped), 8)
        for actions_at_coordinate in grouped.values():
            self.assertEqual(
                {action.system for action in actions_at_coordinate},
                set(breadth.TIMING_SYSTEMS),
            )
            coordinates = {
                (
                    action.stratum,
                    action.warmup_start,
                    action.measure_start,
                    action.measure_stop,
                )
                for action in actions_at_coordinate
            }
            self.assertEqual(len(coordinates), 1)

    def test_warmup_is_phase_local_and_immediately_precedes_measurement(self):
        state = functional_state()
        action = breadth.pending_window_actions(
            state, "mcf", "pricing"
        )[0]
        self.assertEqual(action.phase, "pricing")
        self.assertLessEqual(action.warmup_start, action.measure_start)
        self.assertEqual(
            action.measure_stop - action.measure_start,
            state["workloads"]["mcf"]["phases"]["pricing"]["plan"][
                "length"
            ],
        )

    def test_reconstruction_requires_all_fixed_costs(self):
        state = functional_state()
        for system in breadth.TIMING_SYSTEMS[:-1]:
            breadth.record_fixed(state, "mcf", system, "0.01")
        with self.assertRaisesRegex(breadth.BreadthError, "fixed costs"):
            breadth.reconstruct_system(state, "mcf", "vanilla", level=8)

    def test_mcf_dynamic_phase_weighting(self):
        state = functional_state()
        for system in breadth.TIMING_SYSTEMS:
            breadth.record_fixed(state, "mcf", system, "1")
            for phase, seconds_per_item in (
                ("pricing", "0.002"),
                ("price_out", "0.005"),
            ):
                for action in breadth.pending_window_actions(
                    state, "mcf", phase, system=system
                ):
                    breadth.record_window(
                        state,
                        "mcf",
                        phase,
                        action.window_index,
                        system,
                        seconds_per_item,
                    )
        expected = Decimal(1) + Decimal(819200) * Decimal(
            "0.002"
        ) + Decimal(204800) * Decimal("0.005")
        self.assertEqual(
            breadth.reconstruct_system(
                state, "mcf", "vanilla", level=8
            ),
            expected,
        )

    def test_error_counter_propagation_fails_closed(self):
        state = breadth.new_state(
            identity(), specs(), g20_graph_sha256=sha("g20")
        )
        breadth.record_reference(state, "mcf", boundaries())
        with self.assertRaisesRegex(breadth.BreadthError, "error counters"):
            breadth.record_functional(
                state,
                "mcf",
                "amu",
                {
                    "status": "pass",
                    "bit_exact": True,
                    "compared_words": 1,
                    "mismatched_words": 0,
                    "boundaries": boundaries(),
                    "outputs": {"objective": sha("bad")},
                    **mechanism("amu", bad=True),
                },
            )
        self.assertEqual(state["status"], "failed")

    def test_missing_mechanism_activity_is_rejected(self):
        state = breadth.new_state(
            identity(), specs(), g20_graph_sha256=sha("g20")
        )
        breadth.record_reference(state, "mcf", boundaries())
        with self.assertRaisesRegex(breadth.BreadthError, "issued_loads"):
            breadth.record_functional(
                state, "mcf", "amu", {
                    "status": "pass", "bit_exact": True,
                    "compared_words": 1, "mismatched_words": 0,
                    "boundaries": boundaries(),
                    "outputs": {"artifact": sha("empty-amu")},
                    "error_counters": {},
                },
            )

    def test_amu_per_request_drain_is_rejected(self):
        state = breadth.new_state(
            identity(), specs(), g20_graph_sha256=sha("g20")
        )
        breadth.record_reference(state, "mcf", boundaries())
        row = mechanism("amu")
        row["drains"] = 8
        with self.assertRaisesRegex(breadth.BreadthError, "per-request drain"):
            breadth.record_functional(
                state, "mcf", "amu", {
                    "status": "pass", "bit_exact": True,
                    "compared_words": 1, "mismatched_words": 0,
                    "boundaries": boundaries(),
                    "outputs": {"artifact": sha("draining-amu")},
                    **row,
                },
            )

    def test_64_windows_without_ci_gate_is_inconclusive(self):
        result = breadth.finish_timing(
            {"publishable": False, "relative_half_width": "0.071"},
            level=64,
        )
        self.assertEqual(result["status"], "inconclusive")
        self.assertEqual(result["relative_half_width"], "0.071")

    def test_nonfinal_ci_miss_expands_all_systems(self):
        result = breadth.finish_timing(
            {"publishable": False, "relative_half_width": "0.071"},
            level=8,
        )
        self.assertEqual(result["status"], "expand")
        self.assertEqual(result["next_level"], 16)

    def test_code_hash_change_requires_new_root(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            original = identity()
            breadth.bind_or_resume(root, original, resume=False)
            with self.assertRaisesRegex(
                breadth.BreadthError, "fresh evidence root"
            ):
                breadth.bind_or_resume(
                    root, identity(code="changed"), resume=True
                )

    def test_newest_valid_checkpoint_is_selected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "boundary.raw"
            output.write_bytes(b"proof")
            digest = hashlib.sha256(b"proof").hexdigest()
            state = functional_state()
            first = breadth.write_checkpoint(
                root,
                state,
                boundary="functional",
                outputs={"boundary": {"path": str(output), "sha256": digest}},
            )
            second = breadth.write_checkpoint(
                root,
                state,
                boundary="functional",
                outputs={"boundary": {"path": str(output), "sha256": digest}},
            )
            selected, rejected = breadth.select_resume(root, identity().digest())
            self.assertEqual(selected["sequence"], second["sequence"])
            self.assertGreater(second["sequence"], first["sequence"])
            self.assertEqual(rejected, ())

    def test_checkpoint_state_tampering_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "boundary.raw"
            output.write_bytes(b"proof")
            digest = hashlib.sha256(b"proof").hexdigest()
            breadth.write_checkpoint(
                root, functional_state(), boundary="functional",
                outputs={"boundary": {"path": str(output), "sha256": digest}},
            )
            path = next((root / "checkpoints").glob("*.json"))
            value = json.loads(path.read_text())
            value["state"]["reason"] = "tampered while keeping outer artifacts"
            path.write_text(json.dumps(value) + "\n", encoding="utf-8")
            selected, rejected = breadth.select_resume(root, identity().digest())
            self.assertIsNone(selected)
            self.assertEqual(len(rejected), 1)

    def test_checkpoint_rejects_nonboundary_events(self):
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(breadth.BreadthError, "boundary"):
                breadth.write_checkpoint(
                    Path(temporary), functional_state(),
                    boundary="periodic", outputs={},
                )

    def test_source_has_no_periodic_timer_or_signal_dump(self):
        source = inspect.getsource(breadth)
        self.assertNotIn("threading.Timer", source)
        self.assertNotIn("signal.signal", source)
        self.assertNotIn("SIGALRM", source)

    def test_bootstrap_resamples_n_paired_windows_per_phase(self):
        state = functional_state()
        for system in breadth.TIMING_SYSTEMS:
            breadth.record_fixed(state, "mcf", system, "1")
            for phase in ("pricing", "price_out"):
                for action in breadth.pending_window_actions(
                    state, "mcf", phase, system=system
                ):
                    breadth.record_window(
                        state, "mcf", phase, action.window_index, system,
                        "0.000002" if system == "vanilla" else "0.000001",
                    )
        calls = 0
        original = __import__("random").Random.randrange

        def counted(instance, *args, **kwargs):
            nonlocal calls
            calls += 1
            return original(instance, *args, **kwargs)

        from unittest import mock
        with mock.patch("random.Random.randrange", new=counted):
            breadth.bootstrap_workload(state, "mcf", level=8, resamples=5)
        self.assertEqual(calls, 3 * 5 * 2 * 8)

    def test_crash_artifacts_do_not_block_action_retry(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            evidence = root / "run/reference.json"
            evidence.parent.mkdir(parents=True)
            evidence.write_text("partial", encoding="utf-8")
            default_log = evidence.with_suffix(".json.driver.log")
            default_log.write_text("old crash", encoding="utf-8")
            manifest = {
                "workloads": {"mcf": {"actions": {"reference": {
                    "command": ["/bin/false"],
                    "evidence": str(evidence),
                }}}}
            }
            executor = breadth.ManifestExecutor(manifest, root=root)
            with self.assertRaisesRegex(breadth.BreadthError, "exited 1"):
                executor(breadth.Action("reference", "mcf"))
            self.assertTrue(any(evidence.parent.glob("reference.json.invalid.*")))
            self.assertTrue(any(evidence.parent.glob("reference.json.driver.log.retry.*")))

    def test_hash_bound_shared_functional_evidence_may_be_read_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "latency/500ns"
            artifact = base / "shared/output.raw"
            artifact.parent.mkdir(parents=True)
            artifact.write_bytes(b"shared exact output")
            evidence = base / "shared/functional.json"
            record = {
                "status": "pass",
                "command": [],
                "boundaries": boundaries(),
                "outputs": {"output": {
                    "path": str(artifact.resolve()),
                    "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
                }},
            }
            evidence.write_text(
                json.dumps(record, sort_keys=True) + "\n", encoding="utf-8"
            )
            manifest = {"workloads": {"mcf": {"actions": {
                "functional": {"vanilla": {
                    "command": [], "evidence": str(evidence.resolve()),
                }}
            }}}}
            executor = breadth.ManifestExecutor(
                manifest, root=root, cxl_link_delay="500ns"
            )
            observed = executor(breadth.Action(
                "functional", "mcf", system="vanilla"
            ))
            self.assertEqual(observed["status"], "pass")

    def test_timing_evidence_may_not_escape_latency_root(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "latency/500ns"
            evidence = base / "shared/{{cxl_link_delay}}/window.json"
            manifest = {"workloads": {"mcf": {"actions": {"window": {
                "pricing": {"vanilla": {
                    "command": [
                        "/bin/true", "--cxl-link-delay",
                        "{{cxl_link_delay}}",
                    ],
                    "evidence": str(evidence),
                }}
            }}}}}
            executor = breadth.ManifestExecutor(
                manifest, root=root, cxl_link_delay="500ns"
            )
            with self.assertRaisesRegex(
                breadth.BreadthError, "escapes the evidence root"
            ):
                executor(breadth.Action(
                    "window", "mcf", system="vanilla", phase="pricing",
                    window_index=0, level=8, stratum=0,
                    warmup_start=0, measure_start=1, measure_stop=2,
                ))

    def test_documented_cli_records_failed_input_without_formal_preparation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            inputs = root / "inputs.json"
            calibration = root / "calibration.json"
            inputs.write_text(
                json.dumps({"schema": 1, "status": "accepted"}) + "\n",
                encoding="utf-8",
            )
            calibration.write_text(
                json.dumps({"schema": 1, "passed": True}) + "\n",
                encoding="utf-8",
            )
            evidence = root / "evidence"
            self.assertEqual(breadth.main([
                "--inputs", str(inputs), "--calibration", str(calibration),
                "--root", str(evidence),
            ]), 1)
            failure = json.loads((evidence / "failed_input.json").read_text())
            self.assertEqual(failure["status"], "failed_input")
            self.assertIn("prepared formal breadth manifest", failure["error"])

    def test_action_driver_reaches_complete_only_after_bit_exact_timing(self):
        state = breadth.new_state(
            identity(), specs(), g20_graph_sha256=sha("g20")
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sequence = 0

            def execute(action):
                nonlocal sequence
                artifact = root / "artifacts" / f"{sequence:04d}.raw"
                artifact.parent.mkdir(parents=True, exist_ok=True)
                artifact.write_bytes(str(action).encode())
                digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
                evidence_file = root / "evidence" / f"{sequence:04d}.json"
                evidence_file.parent.mkdir(parents=True, exist_ok=True)
                sequence += 1
                common = {
                    "status": "pass",
                    "boundaries": boundaries(),
                    "outputs": {
                        "artifact": {"path": str(artifact), "sha256": digest}
                    },
                }
                if action.stage == "reference":
                    response = common
                elif action.stage == "functional":
                    response = {
                        **common, "bit_exact": True, "compared_words": 1,
                        "mismatched_words": 0, **mechanism(action.system),
                    }
                else:
                    response = {
                        **common,
                        "verification": "pass",
                        "bit_exact": True,
                        "compared_words": 1,
                        "mismatched_words": 0,
                        "threads": 4,
                        "all_memory_cxl": True,
                        "allocated_on_cxl": True,
                        "cxl_link_delay": state["cxl_link_delay"],
                        "cxl_link_delay_ticks": 1_000_000,
                        "fixed_seconds": "1",
                        "seconds_per_item": (
                            "0.000002" if action.system == "vanilla"
                            else "0.000001"
                        ),
                        **mechanism(action.system, timing=True),
                    }
                payload = json.dumps(response, sort_keys=True) + "\n"
                evidence_file.write_text(payload, encoding="utf-8")
                return {
                    **response,
                    "evidence_output": {
                        "path": str(evidence_file),
                        "sha256": hashlib.sha256(payload.encode()).hexdigest(),
                    },
                }

            result = breadth.collect(state, root=root, executor=execute)
            self.assertEqual(result["status"], "complete")
            self.assertTrue((root / "complete.json").is_file())
            checkpoints = breadth._checkpoint_records(root)
            self.assertIn("functional", {row["boundary"] for row in checkpoints})
            self.assertIn("window", {row["boundary"] for row in checkpoints})
            self.assertIn("phase", {row["boundary"] for row in checkpoints})
            self.assertEqual(len(result["evidence_files"]), 69)
            selected, rejected = breadth.select_resume(root, identity().digest())
            self.assertIsNotNone(selected)
            self.assertEqual(rejected, ())
            (root / "evidence/0005.json").unlink()
            selected, rejected = breadth.select_resume(root, identity().digest())
            self.assertEqual(selected["boundary"], "functional")
            self.assertTrue(rejected)


if __name__ == "__main__":
    unittest.main()
