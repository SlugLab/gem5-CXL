# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

import hashlib
import csv
import json
import os
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from scripts import freeze_pr_scaling_inputs as freeze
from scripts import pr_offload_contract as gate_contract
from scripts import run_cira_amu_m2ndp_scaling as scaling


def sha(label):
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


class ScalingRunnerTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        graphs = []
        frozen_rows = []
        for scale in (4, 12, 14, 20):
            graph = self.root / f"g{scale}.sg"
            manifest = self.root / f"g{scale}.manifest.json"
            generator = self.root / f"converter-{scale}"
            graph.write_bytes(f"graph-{scale}".encode())
            manifest.write_text("{}\n", encoding="utf-8")
            generator.write_bytes(f"generator-{scale}".encode())
            os.chmod(generator, 0o755)
            command = [
                str(generator.resolve()), "-g", str(scale), "-b",
                str(graph.resolve()),
            ]
            graphs.append({
                "scale": scale, "path": str(graph.resolve()),
                "sha256": sha(f"graph-{scale}"),
                "manifest": str(manifest.resolve()),
                "manifest_sha256": sha("{}\n"),
                "num_nodes": 1 << scale, "directed_edges": scale,
                "generator": str(generator.resolve()),
                "generator_sha256": sha(f"generator-{scale}"),
                "generator_command": command,
            })
            frozen_rows.append(freeze.profiles.FrozenGraphManifest(
                schema=1, scale=scale, graph=str(graph.resolve()),
                graph_sha256=sha(f"graph-{scale}"),
                generator=str(generator.resolve()),
                generator_sha256=sha(f"generator-{scale}"),
                generator_command=tuple(command), num_nodes=1 << scale,
                directed_edges=scale,
            ))
        profile_patch = mock.patch.object(
            freeze.profiles,
            "load_scaling_graphs",
            return_value=tuple(frozen_rows),
        )
        profile_patch.start()
        self.addCleanup(profile_patch.stop)
        self.inputs = self.root / "inputs.json"
        self.inputs.write_text(json.dumps({
            "schema": 1, "status": "accepted", "scope": "pr_scaling",
            "profile": "pr-scaling-4thread-1us", "graphs": graphs,
            "graph_set_sha256": freeze.graph_set_sha256(graphs),
        }) + "\n", encoding="utf-8")
        self.calibration = self.root / "calibration.json"
        self.calibration.write_text("{}\n", encoding="utf-8")
        self.gem5 = self.root / "gem5.opt"
        self.gem5.write_bytes(b"gem5")
        self.m5_library = self.root / "frozen/libm5.a"
        self.m5_library.parent.mkdir()
        self.m5_library.write_bytes(b"m5 library")
        self.config = self.root / "config.py"
        self.config.write_text("config = 1\n", encoding="utf-8")
        self.options = SimpleNamespace(
            inputs=self.inputs, calibration=self.calibration,
            root=self.root / "evidence", gem5=self.gem5,
            m5_library=self.m5_library,
            config=self.config, cxlmemuring=self.root / "CXLMemUring",
            m2ndp_root=self.root / "M2NDP", variants_build_root=self.root / "variants",
            timeout=0, resume=False,
        )
        self.qualification_variant_manifest = (
            self.root / "qualification-build/g12/manifest.json"
        )
        self.qualification_variant_manifest.parent.mkdir(parents=True)
        self.qualification_variant_manifest.write_text(
            '{"schema":1}\n', encoding="utf-8"
        )
        self.qualification = self.root / "qualification.json"
        qualification_points = {
            f"g12:{system}": {
                "status": "passed",
                "latency_seconds": "8" if system == "vanilla" else "5",
                "speedup": "1" if system == "vanilla" else "1.6",
                "outputs": {"rank": sha("g12-rank")},
                "mechanism": {"verification": "pass"},
            }
            for system in ("vanilla", "amu", "cira")
        }
        self.qualification.write_text(json.dumps({
            "schema": 1,
            "status": "passed",
            "profile": "pr-scaling-g12-qualification",
            "code_sha256": scaling._code_sha256(),
            "inputs_sha256": scaling._sha256_file(self.inputs),
            "calibration_sha256": scaling._sha256_file(self.calibration),
            "gem5_sha256": scaling._sha256_file(self.gem5),
            "m5_library_sha256": scaling._sha256_file(self.m5_library),
            "config_sha256": scaling._sha256_file(self.config),
            "g12_graph_sha256": sha("graph-12"),
            "variant_manifest": str(
                self.qualification_variant_manifest.resolve()
            ),
            "variant_manifest_sha256": scaling._sha256_file(
                self.qualification_variant_manifest
            ),
            "performance_gate": {
                "status": "passed", "checked_points": 2,
                "speedups": {"amu": "1.6", "cira": "1.6"},
                "policies": {
                    system: gate_contract.performance_policy(system)
                    for system in ("amu", "cira")
                },
                "offenders": [],
            },
            "points": qualification_points,
        }, sort_keys=True) + "\n", encoding="utf-8")
        self.options.qualification = self.qualification

    def resign_qualification(self):
        value = json.loads(self.qualification.read_text())
        value.update({
            "code_sha256": scaling._code_sha256(),
            "inputs_sha256": scaling._sha256_file(self.inputs),
            "calibration_sha256": scaling._sha256_file(self.calibration),
            "gem5_sha256": scaling._sha256_file(self.gem5),
            "m5_library_sha256": scaling._sha256_file(self.m5_library),
            "config_sha256": scaling._sha256_file(self.config),
            "variant_manifest_sha256": scaling._sha256_file(
                self.qualification_variant_manifest
            ),
        })
        self.qualification.write_text(
            json.dumps(value, sort_keys=True) + "\n", encoding="utf-8"
        )

    def write_real_config(
        self, *, delay="1000000", cores=4, link_range="0:4294967296",
        path=None, cira_device_port=False,
    ):
        config = Path(path) if path is not None else self.root / "config.ini"
        config.parent.mkdir(parents=True, exist_ok=True)
        sections = [
            "[board]",
            "type=System",
            "mem_ranges=0:4294967296",
            "[board.cxl_mem_link0]",
            "type=SerialLink",
            f"delay={delay}",
            f"ranges={link_range}",
            "cpu_side_port=board.cache_hierarchy.membus.mem_side_ports[0]",
            "mem_side_port=board.cxl_device_xbar0.cpu_side_ports[0]",
            "[board.cxl_device_xbar0]",
            "type=NoncoherentXBar",
            (
                "cpu_side_ports=board.cxl_mem_link0.mem_side_port "
                "board.cira.csr_mem_side_port"
                if cira_device_port
                else "cpu_side_ports=board.cxl_mem_link0.mem_side_port"
            ),
            "mem_side_ports=board.memory.mem_ctrl.port",
            "[board.memory.mem_ctrl]",
            "type=MemCtrl",
            "port=board.cxl_device_xbar0.mem_side_ports[0]",
            "[board.memory.mem_ctrl.dram]",
            "type=DRAMInterface",
            "range=0:4294967296",
        ]
        for index in range(cores):
            sections.extend(
                (
                    f"[board.processor.cores{index}.core]",
                    "type=BaseTimingSimpleCPU",
                )
            )
        config.write_text("\n".join(sections) + "\n", encoding="utf-8")
        return config

    def real_legacy_state_with_passed_g4_vanilla(self):
        base = self.options.root / "scales/g4/m2ndp"
        run_dir = base / "gem5/run/m5out"
        summary = base / "gem5/run/summary.csv"
        summary.parent.mkdir(parents=True, exist_ok=True)
        with summary.open("w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(
                stream,
                fieldnames=("status", "verification", "run_dir", "sim_ticks"),
            )
            writer.writeheader()
            writer.writerow({
                "status": "ok",
                "verification": "pass",
                "run_dir": str(run_dir),
                "sim_ticks": "4000000",
            })
        reference = base / "reference/scores.raw"
        reference.parent.mkdir(parents=True, exist_ok=True)
        reference.write_bytes(b"\x00" * (16 * 4))
        self.write_real_config(path=run_dir / "config.ini")
        legacy = scaling.new_state(self.options)
        legacy["code_sha256"] = scaling.PRE_LAZY_VARIANT_CODE_SHA256
        legacy.pop("variant_builds")
        entry = scaling.MatrixEntry(4, "vanilla")
        scaling.record_pass(
            legacy,
            entry,
            scaling._point_outputs(entry, self.options),
            **scaling._point_measurement(entry, self.options),
        )
        return legacy

    def complete_state_with_overrides(self, overrides=None):
        state = scaling.new_state(self.options)
        overrides = overrides or {}
        for scale in scaling.SCALES:
            desired = {
                system: Decimal(overrides.get(f"g{scale}:{system}", "1.5"))
                for system in scaling.SYSTEMS
                if system != "vanilla"
            }
            baseline = Decimal(1)
            for speedup in desired.values():
                baseline *= speedup
            for system in scaling.SYSTEMS:
                entry = scaling.MatrixEntry(scale, system)
                seconds = (
                    baseline if system == "vanilla"
                    else baseline / desired[system]
                )
                scaling.record_pass(
                    state,
                    entry,
                    {"summary": sha(entry.key)},
                    latency_seconds=str(seconds),
                    output_elements=1 << entry.scale,
                    mechanism={"verification": "pass"},
                )
        return state

    def test_matrix_is_four_scales_by_four_systems_at_1us(self):
        matrix = scaling.build_matrix()
        self.assertEqual(len(matrix), 16)
        self.assertEqual({row.scale for row in matrix}, {4, 12, 14, 20})
        self.assertEqual({row.system for row in matrix},
                         {"vanilla", "amu", "cira", "m2ndp"})
        self.assertTrue(all(row.latency == "1us" and row.full_e2e
                            for row in matrix))

    def test_formal_commands_have_no_sampling_or_smoke_flags(self):
        for system in ("vanilla", "amu", "cira", "m2ndp"):
            command = scaling.command_for(
                scaling.MatrixEntry(20, system), self.options
            )
            joined = " ".join(command)
            self.assertNotIn("--smoke-test", command)
            self.assertNotIn("--window", joined)
            self.assertIn("--profile", command)
            self.assertEqual(
                command[command.index("--profile") + 1],
                "pr-scaling-4thread-1us",
            )
            self.assertIn("--graph-manifest", command)
            if system in {"vanilla", "m2ndp"}:
                self.assertEqual(
                    command[command.index("--m5-library") + 1],
                    str(self.m5_library.resolve()),
                )

    def test_vanilla_stops_after_baseline_and_m2ndp_resumes_it(self):
        vanilla = scaling.command_for(scaling.MatrixEntry(14, "vanilla"), self.options)
        m2ndp = scaling.command_for(scaling.MatrixEntry(14, "m2ndp"), self.options)
        self.assertEqual(vanilla[vanilla.index("--stop-after") + 1], "gem5_baseline")
        self.assertIn("--resume", m2ndp)
        self.assertEqual(vanilla[vanilla.index("--outdir") + 1],
                         m2ndp[m2ndp.index("--outdir") + 1])

    def test_only_amu_and_cira_require_scale_local_variant_builds(self):
        required = {
            entry.key for entry in scaling.build_matrix()
            if scaling.needs_variant_build(entry)
        }
        self.assertEqual(
            required,
            {
                f"g{scale}:{system}"
                for scale in (4, 12, 14, 20)
                for system in ("amu", "cira")
            },
        )

    def test_amu_lazily_builds_scale_variants_after_vanilla(self):
        state_value = scaling.new_state(self.options)
        baseline = self.options.root / "scales/g4/m2ndp/build"
        baseline.mkdir(parents=True)
        (baseline / "manifest.json").write_text(
            '{"schema": 1}\n', encoding="utf-8"
        )
        scaling.record_pass(
            state_value,
            scaling.MatrixEntry(4, "vanilla"),
            {"summary": sha("vanilla")},
            latency_seconds="4",
            output_elements=16,
            mechanism={"verification": "pass"},
        )
        build_record = {
            "manifest_sha256": sha("variant"),
            "baseline_manifest_sha256": sha("baseline"),
            "calibration_sha256": sha("calibration"),
            "cira_mode": "pgo-selected",
            "cira_policy_latency_ns": 1000,
            "binary_sha256": {
                "amu": sha("amu"), "cira": sha("cira")
            },
        }
        with mock.patch.object(
            scaling.variant_build,
            "ensure_variant_build",
            return_value=build_record,
        ) as ensure:
            scaling.ensure_variants_for_scale(
                4, state_value, self.options
            )

        ensure.assert_called_once()
        call = ensure.call_args
        self.assertEqual(
            Path(call.args[0]),
            self.options.variants_build_root / "g4",
        )
        self.assertEqual(
            Path(call.kwargs["baseline_build"]),
            self.options.root / "scales/g4/m2ndp/build",
        )
        record = state_value["variant_builds"]["g4"]
        self.assertEqual(record["status"], "passed")
        self.assertEqual(record["outputs"], build_record)

    def test_new_state_has_one_pending_variant_build_per_scale(self):
        state_value = scaling.new_state(self.options)
        self.assertEqual(set(state_value["variant_builds"]), {
            "g4", "g12", "g14", "g20",
        })
        self.assertTrue(all(
            row["status"] == "pending"
            for row in state_value["variant_builds"].values()
        ))

    def test_exact_prefixed_one_point_state_migrates_with_lineage(self):
        legacy = self.real_legacy_state_with_passed_g4_vanilla()
        expected = scaling.new_state(self.options)

        migrated = scaling.migrate_pre_lazy_variant_state(
            legacy, expected, self.options
        )

        self.assertEqual(
            migrated["points"]["g4:vanilla"]["status"], "passed"
        )
        self.assertEqual(
            migrated["variant_builds"]["g4"]["status"], "pending"
        )
        self.assertEqual(migrated["resume_lineage"], {
            "previous_code_sha256": scaling.PRE_LAZY_VARIANT_CODE_SHA256,
            "current_code_sha256": expected["code_sha256"],
            "retained_points": ["g4:vanilla"],
        })

    def test_migration_rejects_any_nonexact_legacy_shape(self):
        cases = ("second-point", "wrong-code", "changed-measurement")
        for case in cases:
            with self.subTest(case=case):
                legacy = self.real_legacy_state_with_passed_g4_vanilla()
                if case == "second-point":
                    legacy["points"]["g4:amu"]["status"] = "passed"
                elif case == "wrong-code":
                    legacy["code_sha256"] = "0" * 64
                else:
                    legacy["points"]["g4:vanilla"]["latency_seconds"] = "5"
                with self.assertRaises(scaling.ScalingError):
                    scaling.migrate_pre_lazy_variant_state(
                        legacy, scaling.new_state(self.options), self.options
                    )

    def test_migration_rejects_existing_published_variant_directory(self):
        legacy = self.real_legacy_state_with_passed_g4_vanilla()
        published = self.options.variants_build_root / "g4"
        published.mkdir(parents=True)
        with self.assertRaisesRegex(scaling.ScalingError, "variant"):
            scaling.migrate_pre_lazy_variant_state(
                legacy, scaling.new_state(self.options), self.options
            )

    def test_config_must_be_four_core_all_cxl_one_microsecond(self):
        config = self.write_real_config(delay="500000")
        with self.assertRaisesRegex(scaling.ScalingError, "delay"):
            scaling.validate_config(config)

    def test_config_accepts_real_four_core_all_cxl_shape(self):
        topology = scaling.validate_config(self.write_real_config())
        self.assertEqual(topology["cores"], 4)
        self.assertEqual(topology["range"], "0:4294967296")

    def test_config_accepts_cira_local_csr_device_port_only(self):
        topology = scaling.validate_config(
            self.write_real_config(cira_device_port=True)
        )
        self.assertEqual(topology["cores"], 4)

        config = self.write_real_config()
        text = config.read_text(encoding="utf-8").replace(
            "cpu_side_ports=board.cxl_mem_link0.mem_side_port",
            "cpu_side_ports=board.cxl_mem_link0.mem_side_port "
            "board.untrusted.mem_side_port",
        )
        config.write_text(text, encoding="utf-8")
        with self.assertRaisesRegex(scaling.ScalingError, "cpu_side_ports"):
            scaling.validate_config(config)

    def test_config_rejects_missing_core_or_range_bypass(self):
        config = self.write_real_config(cores=3)
        with self.assertRaisesRegex(scaling.ScalingError, "four cores"):
            scaling.validate_config(config)

        config = self.write_real_config(link_range="4096:8192")
        with self.assertRaisesRegex(scaling.ScalingError, "range mismatch"):
            scaling.validate_config(config)

    def test_vanilla_point_validates_its_generated_config(self):
        base = self.options.root / "scales/g4/m2ndp"
        run_dir = base / "gem5/run/m5out"
        summary = base / "gem5/run/summary.csv"
        summary.parent.mkdir(parents=True, exist_ok=True)
        with summary.open("w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(
                stream,
                fieldnames=("status", "verification", "run_dir"),
            )
            writer.writeheader()
            writer.writerow({
                "status": "ok", "verification": "pass",
                "run_dir": str(run_dir),
            })
        reference = base / "reference/scores.raw"
        reference.parent.mkdir(parents=True, exist_ok=True)
        reference.write_bytes(b"\x00" * (16 * 4))
        self.write_real_config(delay="500000", path=run_dir / "config.ini")

        with self.assertRaisesRegex(scaling.ScalingError, "delay"):
            scaling._point_outputs(
                scaling.MatrixEntry(4, "vanilla"), self.options
            )

    def test_post_trial0_checkpoint_is_rejected(self):
        with self.assertRaisesRegex(scaling.ScalingError, "trial0_entry"):
            scaling.validate_checkpoint_manifest({"boundary": "trial0_end"})

    def test_one_bit_rank_mismatch_is_rejected(self):
        reference = self.root / "reference.u32"
        actual = self.root / "actual.u32"
        reference.write_bytes(b"\x00\x00\x80\x3f")
        actual.write_bytes(b"\x01\x00\x80\x3f")
        with self.assertRaisesRegex(scaling.ScalingError, "word 0"):
            scaling.validate_rank_bits(reference, actual, expected_words=1)

    def test_complete_requires_all_sixteen_passed_points(self):
        state = scaling.new_state(self.options)
        for entry in scaling.build_matrix()[:-1]:
            scaling.record_pass(
                state, entry, {"summary": sha(str(entry))},
                latency_seconds=str(entry.scale),
                output_elements=1 << entry.scale,
                mechanism={"verification": "pass"},
            )
        self.assertFalse(scaling.is_complete(state))
        last = scaling.build_matrix()[-1]
        scaling.record_pass(
            state, last, {"summary": "f" * 64},
            latency_seconds=str(last.scale),
            output_elements=1 << last.scale,
            mechanism={"verification": "pass"},
        )
        self.assertTrue(scaling.is_complete(state))
        self.assertEqual(state["points"]["g20:m2ndp"]["speedup"], "1")

    def test_performance_gate_accepts_exact_boundaries(self):
        overrides = {}
        for index, entry in enumerate(
            row for row in scaling.build_matrix()
            if (
                row.scale in scaling.PERFORMANCE_SCALES
                and row.system != "vanilla"
            )
        ):
            overrides[entry.key] = "1.4" if index % 2 == 0 else "1.6"
        state = self.complete_state_with_overrides(overrides)
        self.assertEqual(
            scaling.evaluate_performance_gate(state),
            {
                "status": "passed", "checked_points": 9,
                "policies": {
                    system: gate_contract.performance_policy(system)
                    for system in ("amu", "cira", "m2ndp")
                },
                "offenders": [],
            },
        )

    def test_performance_gate_checks_exactly_nine_points(self):
        self.assertEqual(scaling.SCALES, (4, 12, 14, 20))
        self.assertEqual(scaling.PERFORMANCE_SCALES, (12, 14, 20))
        state = self.complete_state_with_overrides({
            "g4:amu": "0.01",
            "g4:cira": "99",
            "g4:m2ndp": "0.5",
        })
        self.assertEqual(
            scaling.evaluate_performance_gate(state),
            {
                "status": "passed", "checked_points": 9,
                "policies": {
                    system: gate_contract.performance_policy(system)
                    for system in ("amu", "cira", "m2ndp")
                },
                "offenders": [],
            },
        )

    def test_performance_gate_accepts_m2ndp_above_old_upper_bound(self):
        state = self.complete_state_with_overrides({
            "g12:m2ndp": "2.634272138228941520602758013",
            "g14:m2ndp": "2.1",
            "g20:m2ndp": "1.600001",
        })
        result = scaling.evaluate_performance_gate(state)
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["offenders"], [])
        self.assertIsNone(result["policies"]["m2ndp"]["maximum"])

    def test_performance_gate_reports_system_specific_offenders(self):
        state = self.complete_state_with_overrides({
            "g4:amu": "0.01",
            "g12:amu": "1.600001",
            "g14:cira": "1.399999",
            "g20:m2ndp": "1.399999",
        })
        result = scaling.evaluate_performance_gate(state)
        self.assertEqual(result["status"], "hold")
        self.assertEqual(result["checked_points"], 9)
        self.assertEqual(
            {row["point"] for row in result["offenders"]},
            {"g12:amu", "g14:cira", "g20:m2ndp"},
        )
        m2ndp = next(
            row for row in result["offenders"]
            if row["system"] == "m2ndp"
        )
        self.assertIsNone(m2ndp["maximum"])

    def test_performance_hold_is_successful_terminal_not_complete(self):
        state = self.complete_state_with_overrides({"g12:amu": "1.39"})
        state_path = self.options.root / "state.json"
        state_path.parent.mkdir(parents=True)
        state_path.write_text(json.dumps(state) + "\n", encoding="utf-8")
        failed = self.options.root / "failed.json"
        failed.write_text('{"status":"failed"}\n', encoding="utf-8")
        self.options.resume = True

        def outputs(entry, _options):
            return state["points"][entry.key]["outputs"]

        def measurement(entry, _options):
            point = state["points"][entry.key]
            return {
                "latency_seconds": point["latency_seconds"],
                "output_elements": point["output_elements"],
                "mechanism": point["mechanism"],
            }

        with (
            mock.patch.object(scaling, "parse_args", return_value=self.options),
            mock.patch.object(scaling, "_point_outputs", side_effect=outputs),
            mock.patch.object(
                scaling, "_point_measurement", side_effect=measurement
            ),
        ):
            self.assertEqual(scaling.main([]), 0)

        held = json.loads(
            (self.options.root / "performance-hold.json").read_text()
        )
        self.assertEqual(held["status"], "performance_hold")
        self.assertEqual(len(held["points"]), 16)
        self.assertFalse((self.options.root / "complete.json").exists())
        self.assertFalse(failed.exists())

    def test_record_pass_recomputes_speedup_from_absolute_seconds(self):
        state = scaling.new_state(self.options)
        vanilla = scaling.MatrixEntry(4, "vanilla")
        amu = scaling.MatrixEntry(4, "amu")
        common = {
            "output_elements": 16,
            "mechanism": {"verification": "pass"},
        }
        scaling.record_pass(
            state, vanilla, {"summary": sha("vanilla")},
            latency_seconds="4", **common,
        )
        scaling.record_pass(
            state, amu, {"summary": sha("amu")},
            latency_seconds="2", **common,
        )
        self.assertEqual(state["points"][amu.key]["latency_seconds"], "2")
        self.assertEqual(state["points"][amu.key]["speedup"], "2")

    def test_record_pass_rejects_nonpositive_time_or_missing_mechanism(self):
        state = scaling.new_state(self.options)
        entry = scaling.MatrixEntry(4, "vanilla")
        with self.assertRaisesRegex(scaling.ScalingError, "latency"):
            scaling.record_pass(
                state, entry, {"summary": sha("x")},
                latency_seconds="0", output_elements=16,
                mechanism={"verification": "pass"},
            )
        with self.assertRaisesRegex(scaling.ScalingError, "mechanism"):
            scaling.record_pass(
                state, entry, {"summary": sha("x")},
                latency_seconds="1", output_elements=16,
                mechanism={},
            )

    def test_state_identity_changes_when_gem5_m5_library_or_config_changes(self):
        original = scaling.new_state(self.options)
        self.config.write_text("config = 2\n", encoding="utf-8")
        with self.assertRaisesRegex(scaling.ScalingError, "qualification"):
            scaling.new_state(self.options)
        self.resign_qualification()
        changed_config = scaling.new_state(self.options)
        self.assertNotEqual(
            original["config_sha256"], changed_config["config_sha256"]
        )
        self.gem5.write_bytes(b"different gem5")
        with self.assertRaisesRegex(scaling.ScalingError, "qualification"):
            scaling.new_state(self.options)
        self.resign_qualification()
        changed_gem5 = scaling.new_state(self.options)
        self.assertNotEqual(
            changed_config["gem5_sha256"], changed_gem5["gem5_sha256"]
        )
        self.m5_library.write_bytes(b"different m5 library")
        with self.assertRaisesRegex(scaling.ScalingError, "qualification"):
            scaling.new_state(self.options)
        self.resign_qualification()
        changed_m5 = scaling.new_state(self.options)
        self.assertNotEqual(
            changed_gem5["m5_library_sha256"],
            changed_m5["m5_library_sha256"],
        )

    def test_runner_rejects_general_breadth_manifest(self):
        value = json.loads(self.inputs.read_text())
        value["scope"] = "scaling_and_breadth"
        self.inputs.write_text(json.dumps(value) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(scaling.ScalingError, "scope"):
            scaling.load_inputs(self.inputs)

    def test_state_records_graph_set_and_g20_identity(self):
        inputs = scaling.load_inputs(self.inputs)
        state = scaling.new_state(self.options)
        self.assertEqual(
            state["graph_set_sha256"], inputs["graph_set_sha256"]
        )
        self.assertEqual(
            state["g20_graph_sha256"],
            next(
                row for row in inputs["graphs"] if row["scale"] == 20
            )["sha256"],
        )
        self.assertEqual(
            state["qualification_sha256"],
            scaling._sha256_file(self.qualification),
        )

    def test_qualification_must_be_fresh_pass_and_identity_bound(self):
        accepted = scaling.load_qualification(
            self.qualification, self.options
        )
        self.assertEqual(accepted["status"], "passed")
        original = json.loads(self.qualification.read_text())
        cases = (
            ("status", "performance_hold", "PASS"),
            ("code_sha256", "0" * 64, "identity"),
            ("g12_graph_sha256", "0" * 64, "identity"),
            ("variant_manifest_sha256", "0" * 64, "variant"),
        )
        for field, value, message in cases:
            with self.subTest(field=field):
                changed = json.loads(json.dumps(original))
                changed[field] = value
                self.qualification.write_text(json.dumps(changed))
                with self.assertRaisesRegex(scaling.ScalingError, message):
                    scaling.load_qualification(
                        self.qualification, self.options
                    )
        self.qualification.write_text(json.dumps(original) + "\n")

    def test_missing_qualification_cannot_create_formal_state(self):
        self.options.qualification = self.root / "missing-qualification.json"
        with mock.patch.object(
            scaling, "parse_args", return_value=self.options
        ):
            self.assertEqual(scaling.main([]), 1)
        self.assertFalse((self.options.root / "state.json").exists())

    def test_code_identity_includes_variant_builder_and_orchestrator(self):
        paths = []

        def record(path):
            paths.append(Path(path).resolve())
            return sha(str(Path(path).resolve()))

        with mock.patch.object(scaling, "_sha256_file", side_effect=record):
            scaling._code_sha256()

        self.assertIn(
            scaling.REPO / "scripts/pr_scaling_variant_build.py", paths
        )
        self.assertIn(
            scaling.REPO / "scripts/build_gapbs_matched_pr_spmv_variants.py",
            paths,
        )

    def test_amu_queue_error_and_cira_inactive_core_fail_mechanism_gate(self):
        amu = {
            "status": "ok", "verification": "pass", "asmc_loads": 8,
            "asmc_completed": 8, "asmc_queue_full_errors": 1,
            "asmc_spm_full_errors": 0, "asmc_translation_errors": 0,
            "asmc_pending_errors": 0, "asmc_spm_flag_errors": 0,
            "amu_logical_values": 24, "amu_line_requests": 8,
            "amu_line_cache_hits": 4, "amu_coalesced_misses": 12,
            "scale": 12,
        }
        with self.assertRaisesRegex(scaling.ScalingError, "AMU error"):
            scaling.validate_mechanism_row("amu", amu)
        cira = {
            "status": "ok", "verification": "pass",
            "cira_prefetches": 4, "cira_completed": 4,
            "cira_issued_per_core": "2;2;0;0",
            "cira_completed_per_core": "2;2;0;0",
            "cira_rejected_queue_full": 0,
            "cira_rejected_csr_index_queue_full": 0,
            "cira_dropped_csr_descriptors": 0,
        }
        with self.assertRaisesRegex(scaling.ScalingError, "four active cores"):
            scaling.validate_mechanism_row("cira", cira)

    def test_formal_amu_gate_requires_line_compression_evidence(self):
        base = {
            "status": "ok", "verification": "pass", "scale": 12,
            "asmc_loads": 8, "asmc_completed": 8,
            "asmc_queue_full_errors": 0, "asmc_spm_full_errors": 0,
            "asmc_translation_errors": 0, "asmc_pending_errors": 0,
            "asmc_spm_flag_errors": 0, "amu_logical_values": 24,
            "amu_line_requests": 8, "amu_line_cache_hits": 4,
            "amu_coalesced_misses": 12,
        }
        self.assertIs(scaling.validate_mechanism_row("amu", base), base)
        for field, value, message in (
            ("amu_line_requests", 7, "line requests differ"),
            ("amu_logical_values", 8, "fewer line requests"),
            ("amu_line_cache_hits", 0, "cache hits"),
            ("amu_coalesced_misses", 0, "coalesced misses"),
        ):
            with self.subTest(field=field):
                changed = dict(base)
                changed[field] = value
                with self.assertRaisesRegex(scaling.ScalingError, message):
                    scaling.validate_mechanism_row("amu", changed)

    def test_m2ndp_requires_strict_funcsim_and_link_cycle_calibration(self):
        row = {
            "status": "ok", "verification": "pass",
            "funcsim_compared": 16, "funcsim_mismatched": 1,
            "calibration_pass": "pass", "calibration_residual_ns": "0.1",
            "calibration_link_period_ns": "0.5", "kernel_launches": 42,
        }
        with self.assertRaisesRegex(scaling.ScalingError, "FuncSim"):
            scaling.validate_mechanism_row("m2ndp", row)


if __name__ == "__main__":
    unittest.main()
