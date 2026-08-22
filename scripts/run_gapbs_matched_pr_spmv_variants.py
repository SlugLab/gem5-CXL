#!/usr/bin/env python3
# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

"""Run fixed-20 AMU/CIRA variants against a formal experiment profile."""

import argparse
import json
import os
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

try:
    from scripts import amu_cira_calibration as calibration
    from scripts import compare_gapbs_cxl_amu_cira as comparison
    from scripts import gapbs_pr_experiment_profiles as profiles
    from scripts import m2ndp_artifacts as artifacts
except ImportError:
    import amu_cira_calibration as calibration
    import compare_gapbs_cxl_amu_cira as comparison
    import gapbs_pr_experiment_profiles as profiles
    import m2ndp_artifacts as artifacts


class VariantRunError(RuntimeError):
    pass


def validate_pr_calibration(manifest):
    try:
        if manifest.get("schema") != 2:
            raise VariantRunError(
                "formal PR offload requires calibration schema 2"
            )
        if manifest["sources"]["amu_pdf"]["sha256"] != calibration.AMU_PDF_SHA256:
            raise VariantRunError("AMU source hash differs")
        if manifest["sources"]["cira_csv"]["sha256"] != calibration.CIRA_CSV_SHA256:
            raise VariantRunError("CIRA source hash differs")
        near_data = manifest["near_data_pr"]
        if near_data.get("formal_speedup_is_fit_target") is not False:
            raise VariantRunError("formal speedup cannot be a calibration target")
        for owner in ("amu", "cira"):
            parameters = near_data[owner]["parameters"]
            if not isinstance(parameters, dict) or not parameters:
                raise VariantRunError(f"{owner} PR parameters are missing")
            for name, value in parameters.items():
                if "speedup" in name.lower():
                    raise VariantRunError(
                        "formal speedup cannot be stored as a model parameter"
                    )
                if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                    raise VariantRunError(
                        f"{owner} PR parameter {name} must be a positive integer"
                    )
        if near_data["cira"]["selected_source_row"] not in {"A", "B", "C"}:
            raise VariantRunError("CIRA selected source row is invalid")
        candidates = near_data["cira"].get("candidates")
        if candidates is not None:
            for name in ("A", "B", "C"):
                ppm = candidates[name]["relative_cost_ppm"]
                if not isinstance(ppm, int) or isinstance(ppm, bool) or ppm <= 0:
                    raise VariantRunError(
                        f"CIRA policy {name} relative cost is invalid"
                    )
    except (KeyError, TypeError) as error:
        raise VariantRunError("formal PR calibration is incomplete") from error
    return manifest


def load_pr_calibration(path):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise VariantRunError("cannot read formal PR calibration") from error
    return validate_pr_calibration(value)


def pr_calibration_fixture_for_test():
    return {
        "schema": 2,
        "sources": {
            "amu_pdf": {"sha256": calibration.AMU_PDF_SHA256},
            "cira_csv": {"sha256": calibration.CIRA_CSV_SHA256},
        },
        "near_data_pr": {
            "formal_speedup_is_fit_target": False,
            "amu": {
                "parameters": dict(calibration.NEAR_DATA_PR_AMU_ASSUMPTIONS)
            },
            "cira": {
                "parameters": dict(calibration.NEAR_DATA_PR_CIRA_ASSUMPTIONS),
                "selected_source_row": "B",
            },
        },
    }


def resolve_profile(options):
    name = getattr(options, "profile", "g20-2thread-1us")
    manifest = getattr(options, "graph_manifest", None)
    if name == profiles.FORMAL_PROFILE_NAME:
        if manifest is None:
            raise VariantRunError(
                f"profile {name} requires --graph-manifest"
            )
        return profiles.validate_formal_offload_profile(
            profiles.load_formal_offload_profile(manifest)
        )
    if name == profiles.SCALING_PROFILE_NAME:
        if manifest is None:
            raise VariantRunError(
                f"profile {name} requires --graph-manifest"
            )
        return profiles.validate_scaling_profile(
            profiles.load_scaling_profile(manifest)
        )
    if name in profiles.FROZEN_PROFILE_CONTRACTS:
        if manifest is None:
            raise VariantRunError(
                f"profile {name} requires --graph-manifest"
            )
        return profiles.load_frozen_profile(name, manifest)
    if manifest is not None:
        raise VariantRunError(
            "--graph-manifest is only valid for a frozen profile"
        )
    return profiles.get_profile(name)


def make_compare_args(options):
    profile = resolve_profile(options)
    cxl_link_delay = getattr(options, "cxl_link_delay", "1us")
    profiles.require_latency(profile, cxl_link_delay)
    smoke_test = getattr(options, "smoke_test", False)
    graph_scale = options.graph_scale if smoke_test else profile.graph_scale
    asmc_profile = getattr(options, "asmc_profile", "legacy")
    asmc_calibration_manifest = getattr(
        options, "asmc_calibration_manifest", None
    )
    pr_parameters = pr_calibration_fixture_for_test()["near_data_pr"]
    if asmc_calibration_manifest is not None:
        pr_parameters = load_pr_calibration(
            asmc_calibration_manifest
        )["near_data_pr"]
    amu_pr = pr_parameters["amu"]["parameters"]
    cira_pr = pr_parameters["cira"]["parameters"]
    candidate_costs = pr_parameters["cira"].get("candidates", {})
    policy_cost_ppm = {
        name: candidate_costs.get(name, {}).get("relative_cost_ppm", default)
        for name, default in (("A", 1003978), ("B", 1000000),
                              ("C", 1038586))
    }
    return SimpleNamespace(
        gem5=Path(options.gem5).resolve(),
        config=Path(options.config).resolve(),
        scale=graph_scale,
        iterations=profile.trials,
        cpu="timing",
        fast_forward_cpu=None,
        measure_trial=profile.measured_trial,
        cores=profile.cores,
        mem_size="4GiB",
        graph=Path(options.graph).resolve(),
        graph_scale=graph_scale,
        checkpoint_root=Path(options.checkpoint_root).resolve(),
        checkpoint_boundary="trial0_entry",
        warmup_execution="full_cxl_trial0",
        reuse_checkpoints=True,
        smoke_test=smoke_test,
        cxl_link_delay=cxl_link_delay,
        disable_hw_prefetchers=False,
        l1_mshrs=None,
        l1_tgts_per_mshr=None,
        l2_mshrs=None,
        l2_tgts_per_mshr=None,
        asmc_profile=asmc_profile,
        asmc_calibration_manifest=(
            Path(asmc_calibration_manifest).resolve()
            if asmc_calibration_manifest is not None
            else None
        ),
        asmc_spm_size=(
            "64KiB" if asmc_profile == "paper-calibrated" else "256KiB"
        ),
        asmc_granularity=8,
        asmc_max_outstanding=256,
        asmc_max_send_queue=512,
        asmc_issue_latency="1ns",
        asmc_completion_latency="0ns",
        asmc_latency="0ns",
        asmc_pr_descriptor_entries=amu_pr["descriptor_entries"],
        asmc_pr_read_entries=amu_pr["read_entries"],
        asmc_pr_fp_add_cycles=amu_pr["fp_add_cycles"],
        asmc_pr_fp_mul_cycles=amu_pr["fp_mul_cycles"],
        asmc_pr_fp_div_cycles=amu_pr["fp_div_cycles"],
        cira_max_outstanding=256,
        cira_max_send_queue=1024,
        cira_max_csr_walk_queue=4096,
        cira_max_csr_index_reads=1024,
        cira_csr_lines_per_turn=64,
        cira_max_completed_lines=65536,
        cira_issue_latency="1ns",
        cira_completion_latency="0ns",
        cira_pr_descriptor_entries=cira_pr["descriptor_entries"],
        cira_pr_csr_read_entries=cira_pr["csr_read_entries"],
        cira_pr_coherent_entries=cira_pr["coherent_entries"],
        cira_pr_fp_add_cycles=cira_pr["fp_add_cycles"],
        cira_pr_fp_mul_cycles=cira_pr["fp_mul_cycles"],
        cira_pr_fp_div_cycles=cira_pr["fp_div_cycles"],
        cira_pr_reconfiguration_latency=(
            f"{cira_pr['reconfiguration_latency_ns']}ns"
        ),
        cira_pr_policy_base_cycles=cira_pr["policy_base_cycles"],
        cira_pr_policy_a_cost_ppm=policy_cost_ppm["A"],
        cira_pr_policy_b_cost_ppm=policy_cost_ppm["B"],
        cira_pr_policy_c_cost_ppm=policy_cost_ppm["C"],
        roi_work_events=True,
        verify=True,
        env=[f"OMP_NUM_THREADS={profile.threads}"],
        allow_zero_cira=False,
        timeout=options.timeout,
        dry_run=False,
        outdir=Path(options.outdir).resolve(),
    )


def _integer(row, field):
    try:
        return int(row.get(field, 0))
    except (TypeError, ValueError) as error:
        raise VariantRunError(
            f"{field} is not an integer: {row.get(field)!r}"
        ) from error


def validate_row(
    row,
    kind,
    *,
    smoke_test,
    profile_name="g20-2thread-1us",
    latency="1us",
    profile=None,
):
    profile = profile or profiles.get_profile(profile_name)
    profiles.require_latency(profile, latency)
    expected = {
        "benchmark": "pr_spmv",
        "kind": kind,
        "status": "ok",
        "verification": "pass",
        "iterations": profile.trials,
        "measured_trial": profile.measured_trial,
        "roi_cpu": "timing",
        "cores": profile.cores,
        "cxl_link_delay": latency,
        "all_memory_cxl": True,
        "checkpoint_restores": 1,
    }
    if profile.name == profiles.SCALING_PROFILE_NAME:
        expected.update(
            checkpoint_boundary="trial0_entry",
            warmup_execution="full_cxl_trial0",
        )
    for field, value in expected.items():
        if row.get(field) != value:
            raise VariantRunError(
                f"{kind} {field}={row.get(field)!r}, expected {value!r}"
            )
    if not smoke_test:
        if row.get("scale") != profile.graph_scale:
            raise VariantRunError(
                f"{kind} scale={row.get('scale')!r}, "
                f"expected {profile.graph_scale!r}"
            )
        if row.get("graph_sha256") != profile.graph_sha256:
            raise VariantRunError(
                f"{kind} graph SHA-256 does not match {profile.name}"
            )
    if _integer(row, "sim_ticks") <= 0:
        raise VariantRunError(f"{kind} sim_ticks must be positive")
    if kind == "amu":
        issued = _integer(row, "asmc_loads")
        completed = _integer(row, "asmc_completed")
        logical_values = _integer(row, "amu_logical_values")
        line_requests = _integer(row, "amu_line_requests")
        cache_hits = _integer(row, "amu_line_cache_hits")
        coalesced_misses = _integer(row, "amu_coalesced_misses")
        errors = sum(_integer(row, field) for field in (
            "asmc_queue_full_errors",
            "asmc_spm_full_errors",
            "asmc_translation_errors",
            "asmc_pending_errors",
            "asmc_spm_flag_errors",
        ))
        if errors:
            raise VariantRunError(
                f"AMU error counters are nonzero: {errors}"
            )
        if issued <= 0 or issued != completed:
            raise VariantRunError(
                f"AMU issued/completed load mismatch: {issued}/{completed}"
            )
        if line_requests != issued:
            raise VariantRunError(
                "AMU line requests differ from issued loads"
            )
        if not 0 < line_requests < logical_values:
            raise VariantRunError(
                "AMU requires fewer line requests than logical values"
            )
        if row.get("scale") in (12, 14, 20):
            if cache_hits <= 0:
                raise VariantRunError("AMU cache hits must be nonzero")
            if coalesced_misses <= 0:
                raise VariantRunError("AMU coalesced misses must be nonzero")
    elif kind == "cira":
        descriptors = (
            _integer(row, "cira_prefetches")
            + _integer(row, "cira_indexed_prefetches")
            + _integer(row, "cira_csr_prefetches")
        )
        completed = _integer(row, "cira_completed")
        if descriptors <= 0 or completed <= 0:
            raise VariantRunError("no CIRA events completed")
    else:
        raise VariantRunError(f"unsupported variant kind: {kind}")
    return row


def validate_config_delay(path, latency="1us"):
    try:
        expected_ticks = profiles.LATENCY_TICKS[latency]
    except KeyError as error:
        raise VariantRunError(f"unsupported CXL latency: {latency}") from error
    values = [
        line.split("=", 1)[1].strip()
        for line in Path(path).read_text(
            encoding="utf-8", errors="replace"
        ).splitlines()
        if line.startswith("delay=")
    ]
    expected = str(expected_ticks)
    if values != [expected]:
        raise VariantRunError(
            f"{path}: CXL delay values are {values!r}, expected {[expected]!r}"
        )
    return expected_ticks


def load_manifest(path):
    try:
        manifest = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise VariantRunError(f"invalid variant manifest: {error}") from error
    if (
        manifest.get("benchmark") != "pr_spmv"
        or manifest.get("page_rank_iterations") != 20
        or manifest.get("fixed_iterations") is not True
        or manifest.get("fp_contract") is not False
        or manifest.get("fast_math") is not False
    ):
        raise VariantRunError("variant manifest violates fixed-20 FP contract")
    rows = manifest.get("variants")
    if not isinstance(rows, list):
        raise VariantRunError("variant manifest has no variant rows")
    by_kind = {row.get("kind"): row for row in rows}
    if set(by_kind) != {"amu", "cira"}:
        raise VariantRunError("variant manifest must contain AMU and CIRA")
    baseline_manifest = (
        Path(manifest.get("baseline_build", "")) / "manifest.json"
    )
    if not baseline_manifest.is_file():
        raise VariantRunError(
            f"baseline manifest is missing: {baseline_manifest}"
        )
    if (
        artifacts.sha256_file(baseline_manifest)
        != manifest.get("baseline_manifest_sha256")
    ):
        raise VariantRunError("baseline manifest hash changed")
    return manifest, by_kind


def validate_cira_policy_binding(manifest, mode, source_row):
    if mode not in {"static", "pgo-selected", "few-shot-online"}:
        raise VariantRunError("requested CIRA mode is invalid")
    if source_row not in {"A", "B", "C"}:
        raise VariantRunError("requested CIRA source row is invalid")
    if manifest.get("cira_mode") != mode:
        raise VariantRunError("variant manifest CIRA mode differs")
    policy = manifest.get("cira_policy")
    if not isinstance(policy, dict) or policy.get("source_row") != source_row:
        raise VariantRunError("variant manifest CIRA source row differs")
    return policy


def write_summary_atomic(path, rows):
    path = Path(path)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.unlink(missing_ok=True)
    try:
        comparison.write_summary(temporary, rows)
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _json_safe(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    return value


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gem5", type=Path, required=True)
    parser.add_argument(
        "--config", type=Path, default=comparison.DEFAULT_CONFIG
    )
    parser.add_argument("--graph", type=Path, required=True)
    parser.add_argument("--graph-scale", type=int, default=20)
    parser.add_argument(
        "--profile",
        choices=tuple(
            sorted(set(profiles.PROFILES) | set(profiles.FROZEN_PROFILE_CONTRACTS))
            + [profiles.SCALING_PROFILE_NAME, profiles.FORMAL_PROFILE_NAME]
        ),
        default=profiles.FORMAL_PROFILE_NAME,
    )
    parser.add_argument("--graph-manifest", type=Path)
    parser.add_argument("--cxl-link-delay", default="1us")
    parser.add_argument("--variants-build", type=Path, required=True)
    parser.add_argument(
        "--kind",
        action="append",
        choices=("amu", "cira"),
        required=True,
    )
    parser.add_argument("--checkpoint-root", type=Path, required=True)
    parser.add_argument("--outdir", type=Path, required=True)
    parser.add_argument("--timeout", type=int, default=0)
    parser.add_argument(
        "--asmc-profile",
        choices=("legacy", "paper-calibrated", "paper-sensitivity-256k"),
        default="legacy",
    )
    parser.add_argument("--asmc-calibration-manifest", type=Path)
    parser.add_argument(
        "--cira-mode",
        choices=("static", "pgo-selected", "few-shot-online"),
    )
    parser.add_argument("--cira-source-row", choices=("A", "B", "C"))
    parser.add_argument("--smoke-test", action="store_true")
    return parser.parse_args(argv)


def main(argv=None):
    options = parse_args(argv)
    options.outdir.mkdir(parents=True, exist_ok=True)
    summary_path = options.outdir / "summary.csv"
    evidence_path = options.outdir / "evidence.json"
    summary_path.unlink(missing_ok=True)
    evidence_path.unlink(missing_ok=True)
    try:
        profile = resolve_profile(options)
        profiles.require_latency(profile, options.cxl_link_delay)
        if options.timeout < 0:
            raise VariantRunError("--timeout must be nonnegative")
        if (
            options.graph_scale != profile.graph_scale
            and not options.smoke_test
        ):
            raise VariantRunError(
                "graph scale does not match experiment profile"
            )
        for label, path in (
            ("gem5", options.gem5),
            ("gem5 config", options.config),
            ("graph", options.graph),
        ):
            if not Path(path).is_file():
                raise VariantRunError(f"{label} is missing: {path}")
        if not options.smoke_test:
            profiles.validate_graph(profile, options.graph)

        manifest_path = options.variants_build / "manifest.json"
        manifest, variants = load_manifest(manifest_path)
        if "cira" in options.kind and (
            profile.name == profiles.FORMAL_PROFILE_NAME
            or options.cira_mode is not None
            or options.cira_source_row is not None
        ):
            if options.cira_mode is None or options.cira_source_row is None:
                raise VariantRunError(
                    "formal CIRA run requires mode and source row"
                )
            validate_cira_policy_binding(
                manifest, options.cira_mode, options.cira_source_row
            )
        compare_args = make_compare_args(options)
        result_rows = []
        run_evidence = {}
        for kind in options.kind:
            variant = variants[kind]
            binary = Path(variant["binary"])
            if not binary.is_file():
                raise VariantRunError(
                    f"{kind} binary is missing: {binary}"
                )
            if artifacts.sha256_file(binary) != variant["binary_sha256"]:
                raise VariantRunError(f"{kind} binary hash changed")
            row = comparison.run_one(
                compare_args,
                "pr_spmv",
                f"{kind}_matched",
                binary.parent,
                kind,
            )
            validate_row(
                row,
                kind,
                smoke_test=options.smoke_test,
                profile_name=profile.name,
                latency=options.cxl_link_delay,
                profile=profile,
            )
            run_dir = Path(row["run_dir"])
            delay_ticks = validate_config_delay(
                run_dir / "config.ini", options.cxl_link_delay
            )
            reference = Path(variant["reference_raw"])
            expected_words = (
                profile.num_nodes
                if not options.smoke_test
                else reference.stat().st_size // 4
            )
            if (
                not reference.is_file()
                or reference.stat().st_size != expected_words * 4
            ):
                raise VariantRunError(
                    f"{kind} raw reference has invalid size"
                )
            result_rows.append(row)
            run_evidence[kind] = {
                "row": _json_safe(row),
                "config_delay_ticks": delay_ticks,
                "binary_sha256": variant["binary_sha256"],
                "reference_raw": str(reference.resolve()),
                "reference_raw_sha256": artifacts.sha256_file(reference),
            }

        write_summary_atomic(summary_path, result_rows)
        artifacts.atomic_write_json(
            evidence_path,
            {
                "schema": 1,
                "profile": profile.name,
                "cxl_link_delay": options.cxl_link_delay,
                "graph_sha256": artifacts.sha256_file(options.graph),
                "variant_manifest": str(manifest_path.resolve()),
                "variant_manifest_sha256": artifacts.sha256_file(
                    manifest_path
                ),
                "fixed_source_sha256": manifest["fixed_source_sha256"],
                "runs": run_evidence,
            },
        )
        print(f"Wrote {summary_path}")
        print(f"Wrote {evidence_path}")
        return 0
    except (
        VariantRunError,
        artifacts.EvidenceError,
        profiles.ProfileError,
        OSError,
        KeyError,
    ) as error:
        summary_path.unlink(missing_ok=True)
        evidence_path.unlink(missing_ok=True)
        print(f"MATCHED_VARIANT_RUN_FAILED error={error}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
