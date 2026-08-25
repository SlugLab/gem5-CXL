# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

import hashlib
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

try:
    from scripts import mcfreg2
except ImportError:
    mcfreg2 = None


class MCFREG2Test(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.repo = Path(__file__).resolve().parents[3]

    def require_module(self):
        self.assertIsNotNone(mcfreg2, "scripts.mcfreg2 is missing")

    def fixture_package(self):
        self.require_module()
        sections = {
            name: f"{name.lower()}\n".encode("ascii")
            for name in mcfreg2.REQUIRED_SECTIONS
        }
        return mcfreg2.new_package(
            nodes=4,
            active_arcs=6,
            dummy_arcs=3,
            arena_capacity=16,
            pricing_calls=2,
            price_out_calls=1,
            event_count=9,
            sections=sections,
        )

    def write_fixture(self, name="fixture.reg2"):
        path = self.root / name
        mcfreg2.write_package(path, self.fixture_package())
        return path

    def directory_offset(self, index):
        return mcfreg2.HEADER.size + index * mcfreg2.DIRECTORY.size

    def patch_directory(self, path, index, **changes):
        payload = bytearray(path.read_bytes())
        offset = self.directory_offset(index)
        values = list(mcfreg2.DIRECTORY.unpack_from(payload, offset))
        fields = {
            "section_type": 0,
            "schema": 1,
            "flags": 2,
            "offset": 3,
            "stored_bytes": 4,
            "element_count": 5,
            "element_size": 6,
            "sha256": 7,
        }
        for name, value in changes.items():
            values[fields[name]] = value
        mcfreg2.DIRECTORY.pack_into(payload, offset, *values)
        path.write_bytes(payload)

    def patch_header(self, path, **changes):
        payload = bytearray(path.read_bytes())
        values = list(mcfreg2.HEADER.unpack_from(payload))
        fields = {
            "magic": 0,
            "schema": 1,
            "endian_tag": 2,
            "header_bytes": 3,
            "flags": 4,
            "section_count": 5,
            "directory_offset": 6,
            "nodes": 7,
            "active_arcs": 8,
            "dummy_arcs": 9,
            "arena_capacity": 10,
            "pricing_calls": 11,
            "price_out_calls": 12,
            "event_count": 13,
            "reserved": 14,
        }
        for name, value in changes.items():
            values[fields[name]] = value
        mcfreg2.HEADER.pack_into(payload, 0, *values)
        path.write_bytes(payload)

    def test_minimal_package_round_trips(self):
        self.require_module()
        path = self.root / "minimal.reg2"
        expected = self.fixture_package()
        digest = mcfreg2.write_package(path, expected)
        actual = mcfreg2.read_package(path)

        self.assertEqual(actual.header.magic, b"MCFREG2\0")
        self.assertEqual(actual.header.schema, 2)
        self.assertEqual(actual.header.nodes, 4)
        self.assertEqual(actual.header.active_arcs, 6)
        self.assertEqual(actual.header.arena_capacity, 16)
        self.assertEqual(actual.section_names(), mcfreg2.REQUIRED_SECTIONS)
        self.assertEqual(mcfreg2.sha256_file(path), digest)
        for name in mcfreg2.REQUIRED_SECTIONS:
            self.assertEqual(actual.section(name), expected.section(name))

    def test_truncated_header_is_rejected(self):
        self.require_module()
        path = self.root / "short.reg2"
        path.write_bytes(b"MCFREG2\0")
        with self.assertRaisesRegex(mcfreg2.FormatError, "header"):
            mcfreg2.read_package(path)

    def test_overlapping_sections_are_rejected(self):
        self.require_module()
        path = self.write_fixture()
        first = mcfreg2.DIRECTORY.unpack_from(
            path.read_bytes(), self.directory_offset(0)
        )
        self.patch_directory(path, 1, offset=first[3])
        with self.assertRaisesRegex(mcfreg2.FormatError, "overlap"):
            mcfreg2.read_package(path)

    def test_section_hash_drift_is_rejected(self):
        self.require_module()
        path = self.write_fixture()
        payload = bytearray(path.read_bytes())
        first = mcfreg2.DIRECTORY.unpack_from(
            payload, self.directory_offset(0)
        )
        payload[first[3]] ^= 1
        path.write_bytes(payload)
        with self.assertRaisesRegex(mcfreg2.FormatError, "SHA-256"):
            mcfreg2.read_package(path)

    def test_nonzero_reserved_header_is_rejected(self):
        self.require_module()
        path = self.write_fixture()
        self.patch_header(path, reserved=1)
        with self.assertRaisesRegex(mcfreg2.FormatError, "reserved"):
            mcfreg2.read_package(path)

    def test_duplicate_required_section_is_rejected(self):
        self.require_module()
        path = self.write_fixture()
        first_type = mcfreg2.DIRECTORY.unpack_from(
            path.read_bytes(), self.directory_offset(0)
        )[0]
        self.patch_directory(path, 1, section_type=first_type)
        with self.assertRaisesRegex(mcfreg2.FormatError, "duplicate"):
            mcfreg2.read_package(path)

    def test_unknown_mandatory_section_is_rejected(self):
        self.require_module()
        path = self.write_fixture()
        self.patch_directory(path, 0, section_type=99, flags=0)
        with self.assertRaisesRegex(mcfreg2.FormatError, "unknown mandatory"):
            mcfreg2.read_package(path)

    def test_unknown_optional_section_is_accepted(self):
        self.require_module()
        package = self.fixture_package().with_section(
            mcfreg2.Section(
                section_type=99,
                schema=1,
                flags=mcfreg2.OPTIONAL_FLAG,
                element_count=1,
                element_size=8,
                data=b"optional",
            )
        )
        path = self.root / "optional.reg2"
        mcfreg2.write_package(path, package)
        actual = mcfreg2.read_package(path)
        self.assertEqual(actual.section_by_type(99), b"optional")

    def test_trailing_bytes_are_rejected(self):
        self.require_module()
        path = self.write_fixture()
        path.write_bytes(path.read_bytes() + b"x")
        with self.assertRaisesRegex(mcfreg2.FormatError, "trailing"):
            mcfreg2.read_package(path)

    def test_stable_reference_boundaries(self):
        self.require_module()
        null = mcfreg2.StableRef.null()
        mcfreg2.validate_stable_ref(
            null, maximum_ids={mcfreg2.OBJECT_ARC: 5}, generations={0}
        )
        maximum = mcfreg2.StableRef(
            kind=mcfreg2.OBJECT_ARC, generation=0, object_id=5
        )
        mcfreg2.validate_stable_ref(
            maximum, maximum_ids={mcfreg2.OBJECT_ARC: 5}, generations={0}
        )
        out_of_range = mcfreg2.StableRef(
            kind=mcfreg2.OBJECT_ARC, generation=0, object_id=6
        )
        with self.assertRaisesRegex(mcfreg2.FormatError, "out of range"):
            mcfreg2.validate_stable_ref(
                out_of_range,
                maximum_ids={mcfreg2.OBJECT_ARC: 5},
                generations={0},
            )

    def test_writer_returns_content_sha256(self):
        self.require_module()
        path = self.root / "digest.reg2"
        actual = mcfreg2.write_package(path, self.fixture_package())
        expected = hashlib.sha256(path.read_bytes()).hexdigest()
        self.assertEqual(actual, expected)

    def compile_cpp_probe(self):
        compiler = shutil.which("g++")
        if compiler is None:
            self.skipTest("g++ is unavailable")
        implementation = (
            self.repo / "util/amu/matched_workloads/mcfreg2.cc"
        )
        self.assertTrue(implementation.is_file(), "mcfreg2.cc is missing")
        probe = self.root / "mcfreg2_probe.cc"
        probe.write_text(
            """
#include "mcfreg2.hh"

#include <exception>
#include <iostream>
#include <string>

int main(int argc, char **argv)
{
    try {
        if (argc == 3 && std::string(argv[1]) == "--sha") {
            std::cout << mcfreg2::sha256Hex(argv[2]) << "\\n";
            return 0;
        }
        if (argc != 2)
            return 2;
        const auto package = mcfreg2::readPackage(argv[1]);
        std::cout << mcfreg2::directoryJson(package) << "\\n";
        return 0;
    } catch (const std::exception &error) {
        std::cerr << error.what() << "\\n";
        return 1;
    }
}
""",
            encoding="utf-8",
        )
        output = self.root / "mcfreg2-probe"
        subprocess.run(
            [
                compiler,
                "-std=c++17",
                "-Wall",
                "-Wextra",
                "-Werror",
                "-I",
                str(self.repo / "util/amu/matched_workloads"),
                str(probe),
                str(implementation),
                "-o",
                str(output),
            ],
            cwd=self.repo,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=True,
        )
        return output

    def test_cpp_reader_matches_python_directory(self):
        self.require_module()
        path = self.write_fixture("parity.reg2")
        probe = self.compile_cpp_probe()
        completed = subprocess.run(
            [probe, path], text=True, stdout=subprocess.PIPE, check=True
        )
        self.assertEqual(
            json.loads(completed.stdout),
            mcfreg2.read_package(path).directory_json(),
        )

    def test_cpp_sha256_matches_standard_vectors(self):
        self.require_module()
        probe = self.compile_cpp_probe()
        for value in ("", "abc"):
            completed = subprocess.run(
                [probe, "--sha", value],
                text=True,
                stdout=subprocess.PIPE,
                check=True,
            )
            self.assertEqual(
                completed.stdout.strip(),
                hashlib.sha256(value.encode("ascii")).hexdigest(),
            )

    def test_cpp_reader_rejects_section_hash_drift(self):
        self.require_module()
        path = self.write_fixture("cpp-corrupt.reg2")
        payload = bytearray(path.read_bytes())
        first = mcfreg2.DIRECTORY.unpack_from(
            payload, self.directory_offset(0)
        )
        payload[first[3]] ^= 1
        path.write_bytes(payload)
        completed = subprocess.run(
            [self.compile_cpp_probe(), path],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("SHA-256", completed.stderr)


if __name__ == "__main__":
    unittest.main()
