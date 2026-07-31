import sys
import tempfile
import unittest
from pathlib import Path

PICO = Path(__file__).resolve().parents[1] / "pico"
sys.path.insert(0, str(PICO))

from boot_session import next_boot_id


class BootSessionTests(unittest.TestCase):
    def test_repeated_random_source_still_advances_across_soft_resets(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = (
                str(Path(directory) / "boot.a"),
                str(Path(directory) / "boot.b"),
            )
            random_bytes = (123).to_bytes(8, "big")
            self.assertEqual(next_boot_id(random_bytes, paths), 123)
            self.assertEqual(next_boot_id(random_bytes, paths), 124)
            self.assertEqual(next_boot_id(random_bytes, paths), 125)

    def test_corrupt_slot_is_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = (
                str(Path(directory) / "boot.a"),
                str(Path(directory) / "boot.b"),
            )
            self.assertEqual(next_boot_id((7).to_bytes(8, "big"), paths), 7)
            Path(paths[0]).write_bytes(b"corrupt")
            self.assertEqual(next_boot_id((7).to_bytes(8, "big"), paths), 8)


if __name__ == "__main__":
    unittest.main()
