from pathlib import Path
import os
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class PrinterRxFramerNativeTests(unittest.TestCase):
    def test_portable_framer_vectors(self):
        zig = shutil.which("zig")
        if zig is None:
            self.skipTest("zig compiler is not installed")

        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "printer_rx_framer_test.exe"
            command = [
                zig, "c++", "-x", "c++", "-std=c++11", "-Isrc",
                "ci/printer_rx_framer_test.cpp",
                "src/printer_rx_framer.cpp", "src/crc_bus.c",
                "-o", str(executable),
            ]
            environment = os.environ.copy()
            environment["ZIG_GLOBAL_CACHE_DIR"] = str(ROOT / ".pio" / "zig-global")
            environment["ZIG_LOCAL_CACHE_DIR"] = str(ROOT / ".pio" / "zig-local")
            subprocess.run(command, cwd=ROOT, env=environment, check=True,
                           capture_output=True, text=True)
            subprocess.run([str(executable)], cwd=ROOT, check=True)


if __name__ == "__main__":
    unittest.main()
