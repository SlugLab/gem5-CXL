# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from scripts import run_gapbs_g12_qualification as qualification
from scripts import run_gapbs_g14_4thread_latency_sweep as sweep


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")


class CalibratedG12G14ContractTest(unittest.TestCase):
    def calibration(self):
        return {
            "schema": 1,
            "sources": {
                "amu_pdf": {"sha256": "a" * 64},
                "cira_csv": {"sha256": "b" * 64},
            },
            "amu": {
                "fit": {
                    "parameters": {
                        "metadata_cycles": 2,
                        "id_refill_cycles": 4,
                        "completion_cycles": 6,
                    },
                    "holdout_residuals": {"stream@2": {"relative_error": 0.1}},
                },
                "formal_profile": {
                    "spm_bytes": 65536,
                    "pending_entries_per_state_machine": 32,
                    "id_batch_entries": 32,
                    "metadata_cycles": 2,
                    "id_refill_cycles": 4,
                    "completion_cycles": 6,
                },
                "validation": {"status": "PASS"},
            },
            "cira": {"mode_mapping": {"A": {}, "B": {}, "C": {}}},
        }

    def make_files(self, root):
        calibration = root / "calibration.json"
        gem5 = root / "gem5.opt"
        config = root / "config.py"
        raw = root / "scores.u32"
        for path, data in ((gem5, b"gem5"), (config, b"config"), (raw, b"raw")):
            path.write_bytes(data)
        write_json(calibration, self.calibration())
        return calibration, gem5, config, raw

    def test_g12_provenance_freezes_sources_fit_simulator_modes_and_raw(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            calibration, gem5, config, raw = self.make_files(root)
            options = SimpleNamespace(
                calibration_manifest=calibration, gem5=gem5, config=config
            )
            modes = {
                "static": {"cira_mode": "static", "source_row": "A"},
                "pgo_selected": {
                    "cira_mode": "pgo-selected", "source_row": "B"
                },
                "few_shot_online": {
                    "cira_mode": "few-shot-online", "source_row": "B"
                },
            }
            record = qualification.calibration_provenance(
                options, modes, raw
            )
        self.assertEqual(record["status"], "PASS")
        self.assertEqual(record["amu_profile"], "paper-calibrated")
        self.assertEqual(record["source_hashes"]["amu_pdf"], "a" * 64)
        self.assertEqual(record["fit_parameters"]["metadata_cycles"], 2)
        self.assertEqual(record["mode_definitions"]["pgo_selected"]["source_row"], "B")
        self.assertEqual(len(record["simulator_hashes"]["gem5"]), 64)
        self.assertEqual(len(record["raw_vector_sha256"]), 64)

    def test_resume_rejects_provenance_and_checkpoint_drift(self):
        provenance = {
            "calibration_manifest_sha256": "a" * 64,
            "amu_profile": "paper-calibrated",
            "cira_mode": "pgo-selected",
            "policy_manifest_sha256": "b" * 64,
        }
        record = qualification.passed_record(
            command=("run",), input_hashes={"gem5": "c" * 64},
            output_hashes={"checkpoint_manifest": "d" * 64}, result={},
        )
        record["provenance"] = provenance
        qualification.validate_resumed_record(
            record, command=("run",), input_hashes={"gem5": "c" * 64},
            output_hashes={"checkpoint_manifest": "d" * 64},
            provenance=provenance,
        )
        with self.assertRaisesRegex(qualification.QualificationError, "provenance"):
            qualification.validate_resumed_record(
                record, command=("run",), input_hashes={"gem5": "c" * 64},
                output_hashes={"checkpoint_manifest": "d" * 64},
                provenance={**provenance, "cira_mode": "static"},
            )
        with self.assertRaisesRegex(qualification.QualificationError, "output hash"):
            qualification.validate_resumed_record(
                record, command=("run",), input_hashes={"gem5": "c" * 64},
                output_hashes={"checkpoint_manifest": "0" * 64},
                provenance=provenance,
            )

    def test_g14_requires_passed_hash_bound_g12_and_never_refits(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            calibration, gem5, config, raw = self.make_files(root)
            options = SimpleNamespace(
                calibration_manifest=calibration, gem5=gem5, config=config
            )
            provenance = qualification.calibration_provenance(
                options,
                {
                    "static": {"cira_mode": "static", "source_row": "A"},
                    "pgo_selected": {
                        "cira_mode": "pgo-selected", "source_row": "B"
                    },
                    "few_shot_online": {
                        "cira_mode": "few-shot-online", "source_row": "B"
                    },
                },
                raw,
            )
            qpath = root / "qualification.json"
            write_json(qpath, {
                "schema": 2, "status": "PASS", "raw_bit_exact": True,
                "calibration": provenance,
            })
            loaded = sweep.load_calibrated_qualification(
                qpath, calibration_manifest=calibration,
                gem5=gem5, config=config,
            )
            self.assertEqual(loaded["calibration"]["status"], "PASS")

            changed = self.calibration()
            changed["amu"]["fit"]["parameters"]["metadata_cycles"] = 8
            write_json(calibration, changed)
            with self.assertRaisesRegex(sweep.SweepError, "calibration"):
                sweep.load_calibrated_qualification(
                    qpath, calibration_manifest=calibration,
                    gem5=gem5, config=config,
                )

        parser_source = Path(sweep.__file__).read_text(encoding="utf-8")
        self.assertNotIn("--refit", parser_source)
        self.assertNotIn("--holdout-workload", parser_source)


if __name__ == "__main__":
    unittest.main()
