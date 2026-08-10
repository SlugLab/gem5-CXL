#!/usr/bin/env python3
# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

"""Independently reparse and validate a staged g14 publication."""

import argparse
import csv
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

try:
    from scripts import generate_gapbs_g14_4thread_latency_results as results
    from scripts import m2ndp_artifacts as artifacts
except ImportError:
    import generate_gapbs_g14_4thread_latency_results as results
    import m2ndp_artifacts as artifacts


class ValidationError(RuntimeError):
    pass


def _read_publication(root):
    paths = results.publication_paths(root)
    try:
        with paths.csv.open(newline="", encoding="utf-8") as stream:
            rows = list(csv.DictReader(stream))
        evidence = json.loads(paths.evidence.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, csv.Error, json.JSONDecodeError) as error:
        raise ValidationError(f"cannot read publication: {error}") from error
    if not isinstance(evidence, dict):
        raise ValidationError("evidence must be a JSON object")
    return paths, rows, evidence


def _same_rows(left, right):
    return [dict(row) for row in left] == [dict(row) for row in right]


def validate_directory(publication_root, sweep_root=None, *, expected_rows=None):
    paths, rows, evidence = _read_publication(publication_root)
    if evidence.get("csv_sha256") != artifacts.sha256_file(paths.csv):
        raise ValidationError("CSV/evidence hash mismatch")
    if evidence.get("row_count") != 16 or not _same_rows(evidence.get("rows", []), rows):
        raise ValidationError("CSV/evidence row mismatch")
    try:
        validated = results.validate_matrix(
            rows, graph_sha256=evidence.get("graph_sha256"),
            profile_manifest_sha256=evidence.get("profile_manifest_sha256"),
            require_sensitivity=sweep_root is not None,
        )
    except results.PublicationError as error:
        raise ValidationError(str(error)) from error
    if sweep_root is not None:
        try:
            reparsed, _ = results.collect_rows(sweep_root)
        except Exception as error:
            raise ValidationError(f"cannot reparse raw summaries: {error}") from error
        if not _same_rows(reparsed, validated):
            raise ValidationError("published rows differ from raw summaries")
    elif expected_rows is not None and not _same_rows(expected_rows, validated):
        raise ValidationError("published rows differ from raw summaries")
    if paths.tex.read_text(encoding="utf-8") != results.render_tex(validated):
        raise ValidationError("TeX does not match validated rows")
    from scripts import generate_gapbs_g14_4thread_latency_figure as figure
    expected_pdf, expected_svg = figure.render_figure(
        validated, evidence_sha256=artifacts.sha256_file(paths.evidence)
    )
    if paths.pdf.read_bytes() != expected_pdf:
        raise ValidationError("PDF does not match validated rows")
    if paths.svg.read_bytes() != expected_svg:
        raise ValidationError("SVG does not match validated rows")
    hashes = {path.name: artifacts.sha256_file(path) for path in paths.files[:-1]}
    return {"schema": 1, "status": "pass", "profile": results.PROFILE,
            "row_count": 16, "graph_sha256": evidence["graph_sha256"],
            "profile_manifest_sha256": evidence["profile_manifest_sha256"],
            "reparsed_raw_summaries": sweep_root is not None,
            "artifact_sha256": hashes}


def install_files(publication_root, destinations, *, replace=os.replace):
    """Install a complete publication with a rollback journal."""
    source = results.publication_paths(publication_root)
    source_by_name = {path.name: path for path in source.files}
    if set(destinations) != set(source_by_name):
        raise ValidationError("installation destination set is incomplete")
    parent = Path(next(iter(destinations.values()))).resolve().parent
    staging = Path(tempfile.mkdtemp(prefix=".g14-install-", dir=parent))
    backups = {}
    installed = []
    try:
        for name, destination in destinations.items():
            destination = Path(destination).resolve()
            staged = staging / name
            shutil.copyfile(source_by_name[name], staged)
            if artifacts.sha256_file(staged) != artifacts.sha256_file(source_by_name[name]):
                raise ValidationError(f"staged installation hash mismatch: {name}")
            if destination.exists():
                backup = staging / (name + ".backup")
                shutil.copyfile(destination, backup)
                backups[destination] = backup
        for name, destination in destinations.items():
            destination = Path(destination).resolve()
            destination.parent.mkdir(parents=True, exist_ok=True)
            replace(staging / name, destination)
            installed.append(destination)
    except BaseException:
        for destination in reversed(installed):
            if destination in backups:
                os.replace(backups[destination], destination)
            else:
                destination.unlink(missing_ok=True)
        raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--publication", type=Path, required=True)
    parser.add_argument("--sweep-root", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        report = validate_directory(args.publication, args.sweep_root)
        if args.output:
            artifacts.atomic_write_json(args.output, report)
        print("G14_PUBLICATION_VALIDATION_PASS")
        return 0
    except (ValidationError, OSError) as error:
        print(f"G14_PUBLICATION_VALIDATION_FAILED error={error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
