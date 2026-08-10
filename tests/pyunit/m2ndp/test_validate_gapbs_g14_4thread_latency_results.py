# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

import tempfile
import unittest
import os
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from scripts import generate_gapbs_g14_4thread_latency_results as publisher
from scripts import validate_gapbs_g14_4thread_latency_results as validator
try:
    from test_generate_gapbs_g14_4thread_latency_results import (
        GRAPH_SHA, MANIFEST_SHA, make_valid_rows,
        write_completed_formal_sweep,
    )
except ModuleNotFoundError:
    from m2ndp.test_generate_gapbs_g14_4thread_latency_results import (
        GRAPH_SHA, MANIFEST_SHA, make_valid_rows,
        write_completed_formal_sweep,
    )


class IndependentValidatorTest(unittest.TestCase):
    def test_validator_reparses_completed_formal_sweep(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest_sha = write_completed_formal_sweep(root)
            profile = SimpleNamespace(
                name="g14-4thread-sweep", graph_scale=14,
                graph_sha256=GRAPH_SHA, num_nodes=16384, cores=4, threads=4,
                trials=2, measured_trial=1, page_rank_iterations=20,
                latencies=publisher.LATENCIES,
            )
            with mock.patch.object(
                publisher.profiles, "load_frozen_profile", return_value=profile
            ):
                rows, evidence = publisher.collect_rows(root)
                staging = root / "stage"
                publisher.write_staged_files(
                    rows, staging, graph_sha256=GRAPH_SHA,
                    profile_manifest_sha256=manifest_sha,
                    source_evidence=evidence,
                )
                report = validator.validate_directory(staging, root)
        self.assertTrue(report["reparsed_raw_summaries"])
        self.assertEqual(report["row_count"], 24)

    def test_reparsed_source_rows_must_match_published_rows(self):
        rows = make_valid_rows()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            staging = root / "stage"
            publisher.write_staged_files(
                rows, staging, graph_sha256=GRAPH_SHA,
                profile_manifest_sha256=MANIFEST_SHA,
            )
            with mock.patch.object(
                validator.results, "collect_rows", return_value=(tuple(rows), {})
            ) as collect:
                report = validator.validate_directory(staging, root / "sweep")
            collect.assert_called_once()
            self.assertEqual(report["row_count"], 24)

            staging.joinpath(publisher.SVG_NAME).write_bytes(b"<svg/>")
            with self.assertRaisesRegex(validator.ValidationError, "SVG"):
                validator.validate_directory(
                    staging, expected_rows=rows
                )

            changed = make_valid_rows()
            changed[1]["roi_seconds"] = "9"
            publisher.write_staged_files(
                rows, root / "stage-two", graph_sha256=GRAPH_SHA,
                profile_manifest_sha256=MANIFEST_SHA,
            )
            with mock.patch.object(
                validator.results, "collect_rows", return_value=(tuple(changed), {})
            ):
                with self.assertRaisesRegex(validator.ValidationError, "raw summaries"):
                    validator.validate_directory(root / "stage-two", root / "sweep")

    def test_install_rolls_back_every_destination(self):
        rows = make_valid_rows()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            publication = publisher.publish(
                rows, root / "published", graph_sha256=GRAPH_SHA,
                profile_manifest_sha256=MANIFEST_SHA,
            )
            paper = root / "paper"
            paper.mkdir()
            destinations = {
                path.name: paper / path.name for path in publication.files
            }
            for destination in destinations.values():
                destination.write_bytes(("old-" + destination.name).encode())
            before = {path: path.read_bytes() for path in destinations.values()}
            calls = 0

            def fail_second(source, destination):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("injected install failure")
                return os.replace(source, destination)

            with self.assertRaisesRegex(OSError, "injected"):
                validator.install_files(
                    publication.root, destinations, replace=fail_second
                )
            self.assertEqual(
                {path: path.read_bytes() for path in destinations.values()}, before
            )


if __name__ == "__main__":
    unittest.main()
