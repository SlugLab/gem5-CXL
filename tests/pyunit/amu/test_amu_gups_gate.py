# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

import tempfile
import unittest
from pathlib import Path

from scripts import amu_cira_calibration as calibration
from scripts import run_amu_paper_calibration as runner


REPO = Path(__file__).resolve().parents[3]
HEADER = (REPO / "src/mem/asmc.hh").read_text(encoding="utf-8")
SOURCE = (REPO / "src/mem/asmc.cc").read_text(encoding="utf-8")


def write_stats(path, *, amu, average=200, peak=256, requests=65536,
                far_flag=0, missing_flag=0, io_write_hits=0,
                pending_queue_full=0,
                far_reads=None, far_writes=None,
                spm_reads=None, spm_writes=None):
    far_reads = requests if far_reads is None else far_reads
    far_writes = requests if far_writes is None else far_writes
    spm_reads = 2 * requests if spm_reads is None else spm_reads
    spm_writes = requests if spm_writes is None else spm_writes
    lines = [
        "---------- Begin Simulation Statistics ----------",
        "simTicks 1000000",
    ]
    if amu:
        lines.extend(
            [
                f"board.asmc.issuedLoads {requests}",
                f"board.asmc.issuedStores {requests}",
                f"board.asmc.completedLoads {requests}",
                f"board.asmc.completedStores {requests}",
                "board.asmc.rejectedQueueFull 0",
                "board.asmc.rejectedSpmFull 0",
                "board.asmc.translationFaults 0",
                f"board.asmc.pendingQueueFull {pending_queue_full}",
                f"board.asmc.farReadPackets {far_reads}",
                f"board.asmc.farWritePackets {far_writes}",
                f"board.asmc.spmReadPackets {spm_reads}",
                f"board.asmc.spmWritePackets {spm_writes}",
                f"board.asmc_io_cache.ReadReq.mshrUncacheable::asmc {requests}",
                f"board.asmc_io_cache.WriteReq.hits::asmc {io_write_hits}",
                f"board.asmc_io_cache.WriteReq.misses::asmc {requests}",
                f"board.asmc_io_cache.WriteReq.accesses::asmc {requests}",
                f"board.asmc.farSpmFlagPackets {far_flag}",
                f"board.asmc.spmMissingFlagPackets {missing_flag}",
                f"board.asmc.outstandingIntegral {average * 1000}",
                "board.asmc.occupancyTicks 1000",
                f"board.asmc.maxObservedOutstanding {peak}",
                f"board.asmc.avgOutstanding {average}",
            ]
        )
    lines.append("---------- End Simulation Statistics   ----------")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_config(path, binary, *, kind, delay=5000000, extra_core=False,
                 workload="gups", amu_token="--amu", connected=True):
    sections = [
        "[board.cxl_mem_link0]",
        f"delay={delay}",
    ]
    if kind == "amu":
        sections.extend(
            [
                "cpu_side_port=board.cache_hierarchy.membus.mem_side_ports[0]",
                "mem_side_port=board.cxl_device_xbar0.cpu_side_ports[0]",
            ]
        )
    sections.extend([
        "[board.processor.cores.core]",
        "type=BaseO3CPU",
        "[board.processor.cores.core.workload]",
        f"executable={binary}",
        f"cmd={binary} --workload {workload} --iterations 1 --raw-output out"
        + (f" {amu_token}" if kind == "amu" else ""),
    ])
    if extra_core:
        sections.extend(
            ["[board.processor.cores1.core]", "type=BaseO3CPU"]
        )
    if kind == "amu":
        cache_mem_side = (
            "board.cache_hierarchy.membus.cpu_side_ports[0]"
            if connected else "board.disconnected.cpu_side_ports[0]"
        )
        sections.extend(
            [
                "[board.asmc]",
                "mem_side_port=board.asmc_io_cache.cpu_side",
                "calibration_profile=paper-calibration-base",
                "calibration_manifest_sha256=",
                "spm_size=65536",
                "pending_queue_entries=32",
                "id_batch_entries=32",
                "metadata_latency=0",
                "id_refill_latency=0",
                "completion_publish_latency=0",
                "[board.asmc_io_cache]",
                "cpu_side=board.asmc.mem_side_port",
                f"mem_side={cache_mem_side}",
                "[board.cache_hierarchy.membus]",
                "cpu_side_ports=board.asmc_io_cache.mem_side board.system_port",
                "mem_side_ports=board.cxl_mem_link0.cpu_side_port",
                "[board.cxl_device_xbar0]",
                "cpu_side_ports=board.cxl_mem_link0.mem_side_port",
                "mem_side_ports=board.memory.mem_ctrl.port",
            ]
        )
    path.write_text("\n".join(sections) + "\n", encoding="utf-8")


def make_record(root, binary, kind, **stats_options):
    run_dir = root / kind
    run_dir.mkdir(parents=True)
    raw = run_dir / "checksum.u64"
    record = {
        "workload": "gups",
        "latency": "5us",
        "kind": kind,
        "run_dir": str(run_dir),
        "raw": str(raw),
    }
    kind_tag = "0x1" if kind == "amu" else "0"
    (run_dir / "gem5.log").write_text(
        "1: global: pseudo_inst::m5sum(0x3e862325, 0xec583e48, "
        f"0x414d5531, 0x1, {kind_tag}, 0)\n"
        "GAPBS_VERIFICATION_EXIT_CAUSE "
        "cause=m5_exit instruction encountered\n"
        "Verification: PASS\n",
        encoding="utf-8",
    )
    write_config(run_dir / "config.ini", binary, kind=kind)
    write_stats(run_dir / "stats.txt", amu=kind == "amu", **stats_options)
    (run_dir / "command.txt").write_text(
        f"gem5 --outdir={run_dir} config.py\n", encoding="utf-8"
    )
    runner._materialize_register_checksum(record)
    return record


def make_execution_inputs(root):
    paths = []
    for name in ("gem5.opt", "x86-gapbs-amu-se.py", "libm5.a"):
        path = root / name
        path.write_bytes(name.encode("ascii"))
        paths.append(path)
    return runner._execution_input_manifest(*paths)


class AmuGupsGateTest(unittest.TestCase):
    def test_route_violation_counters_are_explicit_and_fail_fast(self):
        for token in ("farSpmFlagPackets", "spmMissingFlagPackets"):
            self.assertIn(token, HEADER)
            self.assertIn(token, SOURCE)
        self.assertIn("panic(\"ASMC far-memory packet carried", SOURCE)
        self.assertIn("panic(\"ASMC SPM packet lost", SOURCE)

    def test_valid_gate_recomputes_mlp_and_hashes_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            binary = root / "proxy"
            binary.write_bytes(b"same proxy")
            baseline = make_record(root, binary.resolve(), "baseline")
            amu = make_record(root, binary.resolve(), "amu")
            inputs = make_execution_inputs(root)
            proof = runner.validate_gups_gate(
                baseline, amu, binary, execution_inputs=inputs
            )
            self.assertEqual(proof["status"], "PASS")
            self.assertEqual(proof["average_outstanding"], 200.0)
            self.assertEqual(proof["peak_outstanding"], 256)
            self.assertEqual(proof["checksum"], "ec583e483e862325")
            self.assertEqual(set(proof["evidence"]), {"baseline", "amu"})
            self.assertEqual(
                set(proof["execution_inputs"]),
                {"gem5", "config", "m5_library"},
            )
            for evidence in proof["evidence"].values():
                self.assertIn("command_sha256", evidence)

    def test_gate_rejects_every_hard_boundary(self):
        mutations = (
            "missing_stats",
            "delay",
            "checksum",
            "average",
            "peak",
            "pending_queue_full",
            "mixed_binary",
            "cores",
            "far_flag",
            "missing_flag",
            "io_hit",
            "far_read_count",
            "far_write_count",
            "spm_read_count",
            "spm_write_count",
            "operation_count",
            "workload_exact",
            "amu_exact",
            "topology",
            "profile",
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                binary = root / "proxy"
                binary.write_bytes(b"same proxy")
                baseline = make_record(root, binary.resolve(), "baseline")
                stats_options = {}
                if mutation == "average":
                    stats_options["average"] = 130
                elif mutation == "peak":
                    stats_options["peak"] = 257
                elif mutation == "pending_queue_full":
                    stats_options["pending_queue_full"] = 1
                elif mutation == "far_flag":
                    stats_options["far_flag"] = 1
                elif mutation == "missing_flag":
                    stats_options["missing_flag"] = 1
                elif mutation == "io_hit":
                    stats_options["io_write_hits"] = 1
                elif mutation == "far_read_count":
                    stats_options["far_reads"] = 1
                elif mutation == "far_write_count":
                    stats_options["far_writes"] = 1
                elif mutation == "spm_read_count":
                    stats_options["spm_reads"] = 1
                elif mutation == "spm_write_count":
                    stats_options["spm_writes"] = 1
                elif mutation == "operation_count":
                    stats_options["requests"] = 65535
                amu = make_record(root, binary.resolve(), "amu", **stats_options)
                inputs = make_execution_inputs(root)

                if mutation == "missing_stats":
                    Path(amu["run_dir"], "stats.txt").unlink()
                elif mutation == "delay":
                    write_config(
                        Path(amu["run_dir"], "config.ini"),
                        binary.resolve(), kind="amu", delay=4999999,
                    )
                elif mutation == "checksum":
                    Path(amu["raw"]).write_bytes(b"different")
                elif mutation == "mixed_binary":
                    other = root / "other-proxy"
                    other.write_bytes(b"other")
                    write_config(
                        Path(amu["run_dir"], "config.ini"),
                        other.resolve(), kind="amu",
                    )
                elif mutation == "cores":
                    write_config(
                        Path(amu["run_dir"], "config.ini"),
                        binary.resolve(), kind="amu", extra_core=True,
                    )
                elif mutation == "workload_exact":
                    write_config(
                        Path(amu["run_dir"], "config.ini"),
                        binary.resolve(), kind="amu", workload="gups-extra",
                    )
                elif mutation == "amu_exact":
                    write_config(
                        Path(amu["run_dir"], "config.ini"),
                        binary.resolve(), kind="amu", amu_token="--amu-invalid",
                    )
                elif mutation == "topology":
                    write_config(
                        Path(amu["run_dir"], "config.ini"),
                        binary.resolve(), kind="amu", connected=False,
                    )
                elif mutation == "profile":
                    config = Path(amu["run_dir"], "config.ini")
                    config.write_text(
                        config.read_text(encoding="utf-8").replace(
                            "calibration_profile=paper-calibration-base",
                            "calibration_profile=legacy",
                        ),
                        encoding="utf-8",
                    )

                with self.assertRaises(calibration.CalibrationError):
                    runner.validate_gups_gate(
                        baseline, amu, binary, execution_inputs=inputs
                    )

    def test_gate_rejects_execution_input_changed_after_hashing(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            binary = root / "proxy"
            binary.write_bytes(b"same proxy")
            baseline = make_record(root, binary.resolve(), "baseline")
            amu = make_record(root, binary.resolve(), "amu")
            inputs = make_execution_inputs(root)
            Path(inputs["gem5"]["path"]).write_bytes(b"changed")
            with self.assertRaises(calibration.CalibrationError):
                runner.validate_gups_gate(
                    baseline, amu, binary, execution_inputs=inputs
                )


if __name__ == "__main__":
    unittest.main()
