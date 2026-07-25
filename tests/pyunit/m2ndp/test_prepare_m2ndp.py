# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import m2ndp_artifacts as artifacts
from scripts import prepare_m2ndp as prepare


class PrepareM2NDPTest(unittest.TestCase):
    def test_patch_deduplicates_repeated_timing_kernel_registration(self):
        patch = prepare.PATCH.read_text()
        self.assertIn("src/m2ndp.cc", prepare.PATCHED_PATHS)
        self.assertIn(
            "registered->kernel_id == ndp_kernel->kernel_id", patch
        )
        self.assertIn("delete ndp_kernel;", patch)
        self.assertIn("src/m2ndp_config.cc", prepare.PATCHED_PATHS)
        self.assertIn("m_core_cycle++;", patch)

    def test_git_output_preserves_porcelain_status_prefix(self):
        with mock.patch.object(
            prepare.subprocess,
            "check_output",
            return_value=" M CMakeLists.txt\n",
        ):
            output = prepare.git_output(Path("/checkout"), "status")
        self.assertEqual(output, " M CMakeLists.txt")

    def test_rejects_wrong_commit(self):
        with mock.patch.object(
            prepare, "git_output", return_value="deadbeef"
        ):
            with self.assertRaisesRegex(
                prepare.PrepareError, "expected M2NDP commit"
            ):
                prepare.validate_upstream(Path("/checkout"))

    def test_rejects_unrelated_dirty_checkout(self):
        responses = iter([
            artifacts.EXPECTED_M2NDP_COMMIT,
            " M unrelated.cc\n",
        ])
        with mock.patch.object(
            prepare, "git_output", side_effect=lambda *args: next(responses)
        ):
            with self.assertRaisesRegex(
                prepare.PrepareError, "unrelated local changes"
            ):
                prepare.validate_upstream(Path("/checkout"))

    def test_state_records_commit_patch_and_tool_hashes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            patch = root / "strict.patch"
            patch.write_bytes(b"patch")
            funcsim = root / "FuncSim"
            ndpsim = root / "NDPSim"
            cxl_probe = root / "M2NDPCXLProbe"
            for executable in (funcsim, ndpsim, cxl_probe):
                executable.write_bytes(b"binary")
                executable.chmod(0o755)
            state = prepare.build_state(
                root=root,
                commit=artifacts.EXPECTED_M2NDP_COMMIT,
                patch=patch,
                funcsim=funcsim,
                ndpsim=ndpsim,
                cxl_probe=cxl_probe,
                build_commands=[["build"]],
            )
        self.assertEqual(
            state["upstream_commit"], artifacts.EXPECTED_M2NDP_COMMIT
        )
        self.assertEqual(len(state["patch_sha256"]), 64)
        self.assertEqual(
            set(state["tool_sha256"]),
            {"FuncSim", "NDPSim", "M2NDPCXLProbe"},
        )

    def test_require_executable_rejects_nonexecutable_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tool"
            path.write_bytes(b"tool")
            with self.assertRaisesRegex(
                prepare.PrepareError, "not executable"
            ):
                prepare.require_executable(path)

    def test_build_tools_prefetches_missing_conan_packages(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tools = root / "tools"
            with (
                mock.patch.object(prepare.subprocess, "run") as run,
                mock.patch.object(prepare, "_copy_tool"),
            ):
                prepare.build_tools(
                    root,
                    tools,
                    conan="/toolchain/bin/conan",
                    cmake="/toolchain/bin/cmake",
                    cc="/usr/bin/gcc-13",
                    cxx="/usr/bin/g++-13",
                )
        first = run.call_args_list[0]
        self.assertEqual(
            first.args[0],
            [
                "/toolchain/bin/conan",
                "install",
                ".",
                "--install-folder",
                "build",
                "--build=missing",
            ],
        )
        self.assertEqual(
            first.kwargs["env"]["CC"], "/usr/bin/gcc-13"
        )
        self.assertEqual(
            first.kwargs["env"]["CXX"], "/usr/bin/g++-13"
        )
        self.assertTrue(
            first.kwargs["env"]["PATH"].startswith("/toolchain/bin:")
        )


if __name__ == "__main__":
    unittest.main()
