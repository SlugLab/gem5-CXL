import unittest
from pathlib import Path


class G1224CellSystemdTest(unittest.TestCase):
    def test_service_is_boot_persistent_and_stage_resumable(self):
        text = Path(
            "util/systemd/gem5-g12-24cell-20260906.service"
        ).read_text(encoding="utf-8")
        self.assertIn("RequiresMountsFor=/mnt/disk0", text)
        self.assertIn("Restart=on-abnormal", text)
        self.assertIn("WantedBy=multi-user.target", text)
        self.assertIn("scripts/run_g12_24cell_timing_evidence.py", text)
        self.assertIn(
            "--root /mnt/disk0/gem5-CXL-eval/"
            "g12-timing-24cell-20260906-qualification-r1",
            text,
        )
        self.assertIn("--jobs 1", text)
        self.assertNotIn("g12-timing-24cell-20260906-formal-r1", text)
        self.assertIn("state.json", text)
        self.assertIn("--resume", text)
        self.assertNotIn("checkpoint", text.lower())


if __name__ == "__main__":
    unittest.main()
