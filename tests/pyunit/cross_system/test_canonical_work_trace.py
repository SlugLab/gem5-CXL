# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

import hashlib
import json
import struct
import subprocess
import tempfile
import unittest
from pathlib import Path

from scripts import canonical_work_trace as trace


def digest(label):
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def meta():
    return {
        "schema": 1,
        "workload": "pr_spmv",
        "input_sha256": digest("input"),
        "source_sha256": digest("source"),
        "binary_sha256": digest("binary"),
        "config_sha256": digest("config"),
        "phases": [{"id": 0, "name": "pull", "work_items": 1}],
        "output_boundaries": {"rank.iter0": {"word_bits": 32, "count": 1}},
    }


def reference_ops():
    return (
        trace.Operation(
            phase=0,
            opcode=trace.Opcode.LOAD_F32,
            work_item=7,
            sequence=0,
            address=0x1000,
            operand0=0x3F800000,
            operand1=0,
            result=0x3F800000,
        ),
        trace.Operation(
            phase=0,
            opcode=trace.Opcode.COMMIT,
            work_item=7,
            sequence=1,
            address=0x2000,
            operand0=0x3F800000,
            operand1=0,
            result=0x3F800000,
        ),
    )


class CanonicalTraceTest(unittest.TestCase):
    def test_round_trip_preserves_order_raw_operands_and_outputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            trace.write_bundle(
                root, meta(), reference_ops(), {"rank.iter0": [0x3F800000]}
            )
            loaded = trace.read_bundle(root)
            self.assertEqual(loaded.operations, reference_ops())
            self.assertEqual(loaded.outputs["rank.iter0"], (0x3F800000,))
            self.assertEqual(loaded.meta["trace_record_bytes"], 56)
            self.assertEqual(loaded.meta["trace_records"], 2)

    def test_translation_may_not_reorder_commits(self):
        with self.assertRaisesRegex(trace.TraceError, "sequence"):
            trace.validate_translation(
                reference_ops(), tuple(reversed(reference_ops()))
            )

    def test_decode_rejects_truncated_record(self):
        with self.assertRaisesRegex(trace.TraceError, "multiple of 56"):
            trace.decode_operations(b"\0" * 55)

    def test_decode_rejects_unknown_opcode(self):
        payload = trace.TRACE_STRUCT.pack(0, 999, 0, 0, 0, 0, 0, 0, 0)
        with self.assertRaisesRegex(trace.TraceError, "unknown opcode"):
            trace.decode_operations(payload)

    def test_operation_rejects_address_overflow(self):
        with self.assertRaisesRegex(trace.TraceError, "address"):
            trace.Operation(
                0, trace.Opcode.LOAD_U64, 0, 0, 1 << 64, 0, 0, 0
            )

    def test_duplicate_scatter_or_reduction_tree_drift_is_rejected(self):
        expected = (
            trace.Operation(0, trace.Opcode.STORE_U64, 0, 0, 0x3000, 1, 0, 1),
            trace.Operation(0, trace.Opcode.STORE_U64, 1, 1, 0x3000, 2, 0, 2),
        )
        actual = (expected[1], expected[0])
        with self.assertRaisesRegex(trace.TraceError, "sequence"):
            trace.validate_translation(expected, actual)

    def test_one_bit_output_mismatch_fails(self):
        with self.assertRaisesRegex(
            trace.TraceError, r"rank\[1\].*00000002.*00000003"
        ):
            trace.compare_words((1, 2), (1, 3), "rank", word_bits=32)

    def test_read_rejects_tampered_output_word_width(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            trace.write_bundle(
                root, meta(), reference_ops(), {"rank.iter0": [0x3F800000]}
            )
            meta_path = root / "trace.meta.json"
            value = json.loads(meta_path.read_text(encoding="utf-8"))
            value["outputs"]["rank.iter0"]["word_bits"] = 16
            meta_path.write_text(
                json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(trace.TraceError, "word width"):
                trace.read_bundle(root)

    def test_cpp_writer_matches_python_abi(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "fixture.cc"
            binary = root / "fixture"
            output = root / "trace.bin"
            source.write_text(
                r'''
#include <cstdio>
#include "util/amu/matched_workloads/canonical_trace.hh"
int main(int argc, char **argv) {
  float one = 1.0f;
  matched_trace::TraceRecord record{};
  record.phase = 0;
  record.opcode = static_cast<uint16_t>(matched_trace::Opcode::LOAD_F32);
  record.work_item = 7;
  record.sequence = 0;
  record.address = 0x1000;
  record.operand0 = matched_trace::raw_bits(one);
  record.result = record.operand0;
  std::FILE *stream = std::fopen(argv[1], "wb");
  if (!stream) return 2;
  matched_trace::emit(stream, record);
  return std::fclose(stream) == 0 ? 0 : 3;
}
''',
                encoding="utf-8",
            )
            subprocess.run(
                ["g++", "-std=c++17", "-I.", str(source), "-o", str(binary)],
                cwd=Path(__file__).resolve().parents[3],
                check=True,
            )
            subprocess.run([str(binary), str(output)], check=True)
            operations = trace.decode_operations(output.read_bytes())
            self.assertEqual(len(operations), 1)
            self.assertEqual(operations[0].operand0, 0x3F800000)
            self.assertEqual(operations[0].address, 0x1000)


if __name__ == "__main__":
    unittest.main()
