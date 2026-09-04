# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from scripts import timing_evidence_24cell as evidence


def _sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class TimingEvidence24CellTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def _write_calibration(
        self, label="1us", *, selected=7799, target="2012.652",
        measured="2012.625000000", residual="0.027000000",
    ):
        root = self.root / f"calibration-{label}"
        config = root / "config"
        config.mkdir(parents=True)
        m2ndp = config / "m2ndp.config"
        link = config / "cxl_link.icnt"
        m2ndp.write_text("core = 2GHz\n", encoding="utf-8")
        link.write_text(f"link_latency = {selected}\n", encoding="utf-8")
        path = root / "calibration.json"
        path.write_text(json.dumps({
            "schema": 1, "passed": True, "cxl_delay": label,
            "cxl_link_delay": label, "target_ns": target,
            "measured_ns": measured, "residual_ns": residual,
            "selected_link_latency": selected,
            "core_period_ns": "0.5", "link_period_ns": "0.125",
            "derived_m2ndp_config_sha256": _sha256(m2ndp),
            "derived_cxl_link_config_sha256": _sha256(link),
        }, sort_keys=True) + "\n", encoding="utf-8")
        return path

    def _write_m2ndp(self, calibration, *, workload="npb_cg", latency="1us"):
        run = self.root / "m2ndp"
        run.mkdir()
        ndpsim = run / "NDPSim"
        funcsim = run / "FuncSim"
        timing_config = run / "m2ndp.config"
        functional_config = run / "functional.config"
        output = run / "ndpsim.out"
        stdout = run / "ndpsim.stdout.log"
        stderr = run / "ndpsim.stderr.log"
        fstdout = run / "funcsim.stdout.log"
        fstderr = run / "funcsim.stderr.log"
        for path, payload in (
            (ndpsim, b"ndpsim"), (funcsim, b"funcsim"),
            (timing_config, b"timing"), (functional_config, b"functional"),
            (output, b"output"),
            (stdout, b"Gantt info: host 0 finished NDP kernel\n"
                     b"EXPR FINISHED 102531389\nMEMROY MATCH SUCCESS\n"),
            (stderr, b""), (fstdout, b"functional pass\n"),
            (fstderr, b""),
        ):
            path.write_bytes(payload)
        calibration_record = json.loads(Path(calibration).read_text())
        record = {
            "schema": 1, "status": "pass", "returncode": 0,
            "workload": workload, "cxl_link_delay": latency,
            "cycles": 102531389, "verification": "pass",
            "numeric_verification": "pass", "bit_exact": True,
            "memory_match": "pass", "expected_launches": 1,
            "completed_launches": 1, "calibration": calibration_record,
            "calibration_evidence_path": str(calibration),
            "calibration_evidence_sha256": _sha256(calibration),
            "ndpsim_sha256": _sha256(ndpsim),
            "config_sha256": _sha256(timing_config),
            "package_sha256": "1" * 64, "trace_sha256": "2" * 64,
            "input_sha256": "3" * 64, "patch_sha256": "4" * 64,
            "command": [str(ndpsim), "--config", str(timing_config)],
            "stdout_path": str(stdout), "stdout_sha256": _sha256(stdout),
            "stderr_path": str(stderr), "stderr_sha256": _sha256(stderr),
            "output_path": str(output), "output_sha256": _sha256(output),
            "functional": {
                "schema": 1, "status": "pass", "returncode": 0,
                "verification": "pass", "numeric_verification": "pass",
                "bit_exact": True, "completed_launches": 1,
                "expected_launches": 1, "funcsim_sha256": _sha256(funcsim),
                "config_sha256": _sha256(functional_config),
                "command": [str(funcsim), "--config", str(functional_config)],
                "stdout_path": str(fstdout), "stdout_sha256": _sha256(fstdout),
                "stderr_path": str(fstderr), "stderr_sha256": _sha256(fstderr),
            },
            "execution_origin": "fresh",
        }
        path = run / "evidence.json"
        path.write_text(json.dumps(record, sort_keys=True) + "\n", encoding="utf-8")
        return path

    def test_matrix_is_exactly_six_by_four(self):
        self.assertEqual(len(evidence.WORKLOADS), 6)
        self.assertEqual(evidence.LATENCIES, ("200ns", "500ns", "1us", "2us"))
        self.assertEqual(len(evidence.COORDINATES), 24)
        self.assertEqual(len(set(evidence.COORDINATES)), 24)

    def test_cycles_to_ns_is_exact(self):
        self.assertEqual(
            evidence.cycles_to_ns(102_531_389, "0.5"), "51265694.5"
        )
        with self.assertRaisesRegex(evidence.EvidenceError, "nonnegative integer"):
            evidence.cycles_to_ns(True, "0.5")
        with self.assertRaisesRegex(evidence.EvidenceError, "decimal string"):
            evidence.cycles_to_ns(1, 0.5)

    def test_calibration_rows_match_microprobe_records(self):
        cases = {
            "200ns": (1397, "412.254", "412.250", "0.004", "4"),
            "500ns": (3798, "1012.32", "1012.375", "0.055", "55"),
            "1us": (7799, "2012.652", "2012.625", "0.027", "27"),
            "2us": (15801, "4012.65", "4012.624999998", "0.025000002", "25.000002"),
        }
        for label, (cycles, target, measured, residual, residual_ps) in cases.items():
            with self.subTest(label=label):
                path = self._write_calibration(
                    label, selected=cycles, target=target,
                    measured=measured, residual=residual,
                )
                row = evidence.load_calibration(path)
                self.assertEqual(row.latency, label)
                self.assertEqual(row.selected_link_latency, cycles)
                self.assertEqual(row.residual_ps, residual_ps)
                self.assertEqual(row.gem5_round_trip_ns, target)
                self.assertEqual(row.m2ndp_round_trip_ns, measured)

    def test_load_m2ndp_cell_hash_binds_logs_binary_configs_and_calibration(self):
        calibration = self._write_calibration()
        path = self._write_m2ndp(calibration)
        row = evidence.load_m2ndp_cell(path, "npb_cg", "1us")
        self.assertEqual(row["cycles"], 102531389)
        self.assertEqual(row["core_period_ns"], "0.5")
        self.assertEqual(row["kernel_time_ns"], "51265694.5")
        self.assertEqual(row["execution_origin"], "fresh")
        self.assertEqual(row["calibration"]["selected_link_latency"], 7799)

    def test_m2ndp_rejects_wrong_identity_hash_marker_and_functional_gate(self):
        calibration = self._write_calibration()
        source = self._write_m2ndp(calibration)
        pristine = json.loads(source.read_text())
        cases = []
        wrong_latency = copy.deepcopy(pristine)
        wrong_latency["cxl_link_delay"] = "500ns"
        cases.append((wrong_latency, "latency differs"))
        wrong_hash = copy.deepcopy(pristine)
        wrong_hash["output_sha256"] = "0" * 64
        cases.append((wrong_hash, "output SHA-256 differs"))
        two_markers = copy.deepcopy(pristine)
        stdout = Path(two_markers["stdout_path"])
        stdout.write_text(
            "EXPR FINISHED 102531389\nEXPR FINISHED 102531389\n",
            encoding="utf-8",
        )
        two_markers["stdout_sha256"] = _sha256(stdout)
        cases.append((two_markers, "exactly one EXPR FINISHED"))
        for index, (record, message) in enumerate(cases):
            with self.subTest(message=message):
                if index == 2:
                    Path(pristine["stdout_path"]).write_text(
                        "EXPR FINISHED 102531389\n"
                        "EXPR FINISHED 102531389\n",
                        encoding="utf-8",
                    )
                else:
                    Path(pristine["stdout_path"]).write_text(
                        "Gantt info: host 0 finished NDP kernel\n"
                        "EXPR FINISHED 102531389\nMEMROY MATCH SUCCESS\n",
                        encoding="utf-8",
                    )
                candidate = self.root / f"candidate-{index}.json"
                candidate.write_text(json.dumps(record) + "\n", encoding="utf-8")
                with self.assertRaisesRegex(evidence.EvidenceError, message):
                    evidence.load_m2ndp_cell(candidate, "npb_cg", "1us")

        missing = copy.deepcopy(pristine)
        missing.pop("functional")
        candidate = self.root / "missing-functional.json"
        candidate.write_text(json.dumps(missing) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(evidence.EvidenceError, "functional evidence"):
            evidence.load_m2ndp_cell(candidate, "npb_cg", "1us")

        Path(pristine["stdout_path"]).write_text(
            "Gantt info: host 0 finished NDP kernel\n"
            "EXPR FINISHED 102531389\nMEMROY MATCH SUCCESS\n",
            encoding="utf-8",
        )
        floating = copy.deepcopy(pristine)
        floating["calibration"]["core_period_ns"] = 0.5
        candidate = self.root / "floating-period.json"
        candidate.write_text(json.dumps(floating) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(evidence.EvidenceError, "decimal string"):
            evidence.load_m2ndp_cell(candidate, "npb_cg", "1us")

    def test_calibration_rejects_derived_config_hash_drift(self):
        path = self._write_calibration()
        (path.parent / "config" / "m2ndp.config").write_text(
            "changed\n", encoding="utf-8"
        )
        with self.assertRaisesRegex(evidence.EvidenceError, "config SHA-256 differs"):
            evidence.load_calibration(path)


if __name__ == "__main__":
    unittest.main()
