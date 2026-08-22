# Copyright (c) 2026
# SPDX-License-Identifier: BSD-3-Clause

import importlib.util
import subprocess
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[3]
ABI_HEADER = REPO / "util/pr_offload/pr_row_offload.h"
PYTHON_CONTRACT = REPO / "scripts/pr_offload_contract.py"


def load_contract():
    spec = importlib.util.spec_from_file_location(
        "pr_offload_contract_under_test", PYTHON_CONTRACT
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PrRowOffloadAbiTest(unittest.TestCase):
    def test_descriptor_contract_files_exist(self):
        self.assertTrue(ABI_HEADER.is_file(), str(ABI_HEADER))
        self.assertTrue(PYTHON_CONTRACT.is_file(), str(PYTHON_CONTRACT))

    def test_python_contract_has_fixed_shape_and_partition(self):
        contract = load_contract()
        self.assertEqual(getattr(contract, "PR_ROW_DESC_BYTES", None), 104)
        partition = getattr(contract, "static_partition", None)
        self.assertTrue(callable(partition))
        self.assertEqual(
            [partition(11, 4, worker) for worker in range(4)],
            [(0, 3), (3, 6), (6, 9), (9, 11)],
        )

    def test_descriptor_abi_compiles_as_c_and_cxx(self):
        source = r'''
#include "util/pr_offload/pr_row_offload.h"

int main(void) {
    struct pr_row_offload_desc desc = {0};
    desc.in_offsets_addr = 1;
    desc.in_neighbors_addr = 2;
    desc.out_degree_addr = 3;
    desc.scores_in_addr = 4;
    desc.contributions_addr = 5;
    desc.scores_out_addr = 6;
    desc.row_begin = 7;
    desc.row_count = 8;
    desc.node_count = 9;
    desc.iteration = 10;
    desc.phase = PR_ROW_PULL;
    desc.row_window = 64;
    desc.lead_blocks = 1;
    desc.flags = PR_ROW_FLAG_SAMPLE;
    desc.damping_bits = 0x3f59999aU;
    desc.base_score_bits = 0x3a99999aU;
    return sizeof(desc) != 104 || desc.phase != PR_ROW_PULL;
}
'''
        for compiler, language in (("cc", "c"), ("c++", "c++")):
            with self.subTest(language=language):
                completed = subprocess.run(
                    [compiler, "-std=c11" if language == "c" else "-std=c++11",
                     "-I", str(REPO), "-x", language, "-fsyntax-only", "-"],
                    input=source,
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.assertEqual(completed.returncode, 0, completed.stderr)


if __name__ == "__main__":
    unittest.main()
