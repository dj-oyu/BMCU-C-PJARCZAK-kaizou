"""Host conversation test for src/bambu_bus_ams.cpp.

Building this needs one thing the other native tests do not: three headers
under src/ have to be replaced by host stand-ins, and `-I` cannot do it.
`bambu_bus_ams.cpp` reaches them with quoted includes -- `#include
"hal/irq_wch.h"` -- and a quoted include is resolved against the *including
file's own directory* before any -I path is consulted. So a host copy of
hal/irq_wch.h placed first on the search path is simply never reached; src/'s
own copy wins no matter what order the -I flags are in.

The shadowing is therefore done in the filesystem instead: src/ is copied into
a scratch tree and ci/host/overlay/ is unpacked over the top of it, replacing
exactly hal/irq_wch.h, hal/time_hw.h and app_api.h. The .cpp files under test
are copied byte-for-byte and never edited, so what compiles is the shipped
translation unit -- if a future include drags in a fourth register header, the
build breaks here rather than being silently accommodated.
"""

from pathlib import Path
import os
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
OVERLAY = ROOT / "ci" / "host" / "overlay"

# The three host stand-ins, asserted rather than assumed: a stand-in that
# stopped matching a real header's name would otherwise go quietly unused and
# the register version would be compiled instead.
SHADOWED = ("hal/irq_wch.h", "hal/time_hw.h", "app_api.h")


def stage_sources(directory: Path) -> Path:
    """Copy src/ into `directory` and overlay the host headers over it."""
    stage = directory / "src"
    shutil.copytree(ROOT / "src", stage)

    for relative in SHADOWED:
        real = stage / relative
        host = OVERLAY / relative
        assert real.is_file(), f"{relative} is not in src/ any more"
        assert host.is_file(), f"{relative} has no host stand-in"
        shutil.copyfile(host, real)

    return stage


class BambuBusConversationNativeTests(unittest.TestCase):
    def test_conversation(self):
        zig = shutil.which("zig")
        if zig is None:
            self.skipTest("zig compiler is not installed")

        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            stage = stage_sources(work)
            executable = work / "bambu_bus_conversation_test.exe"

            environment = os.environ.copy()
            environment["ZIG_GLOBAL_CACHE_DIR"] = str(ROOT / ".pio" / "zig-global")
            environment["ZIG_LOCAL_CACHE_DIR"] = str(ROOT / ".pio" / "zig-local")

            # src/crc_bus.c is C and compiles unmodified; it gets its own step
            # rather than being coerced through the C++ driver.
            crc_object = work / "crc_bus.o"
            build = subprocess.run(
                [zig, "cc", "-std=gnu11", "-c", str(stage / "crc_bus.c"),
                 f"-I{stage}", "-o", str(crc_object)],
                cwd=ROOT, env=environment, capture_output=True, text=True)
            self.assertEqual(build.returncode, 0, build.stderr)

            command = [
                # The sibling native tests compile their pure headers as
                # c++11. This translation unit cannot: the reply templates at
                # bambu_bus_ams.cpp:507 and :747 brace-initialise structs that
                # carry default member initialisers, which is an aggregate
                # only from C++14 onwards.
                zig, "c++", "-std=gnu++17",
                "-DBAMBU_BUS_AMS_NUM=0",
                f"-I{stage}",
                "-Ici/host",
                str(stage / "bambu_bus_ams.cpp"),
                "ci/host/bambu_bus_host.cpp",
                "ci/bambu_bus_conversation_test.cpp",
                str(crc_object),
                "-o", str(executable),
            ]
            build = subprocess.run(command, cwd=ROOT, env=environment,
                                   capture_output=True, text=True)
            # The overlay makes a build failure the most likely way this test
            # breaks, and a bare CalledProcessError hides which header did it.
            self.assertEqual(build.returncode, 0, build.stderr)

            # One process per vector. bambu_bus_ams.cpp keeps file-scope state
            # with no reset entry point -- package_num, count_on_use, the
            # before_on_use sniff cursor, the online-detect window -- so vectors
            # sharing a process would be order-dependent in ways none of their
            # assertions would show.
            listing = subprocess.run([str(executable), "--list"], cwd=ROOT,
                                     capture_output=True, text=True, check=True)
            vectors = listing.stdout.split()
            self.assertTrue(vectors, "the test binary listed no vectors")

            for vector in vectors:
                with self.subTest(vector=vector):
                    result = subprocess.run([str(executable), vector], cwd=ROOT,
                                            capture_output=True, text=True)
                    self.assertEqual(result.returncode, 0,
                                     f"{vector}: {result.stdout}{result.stderr}")


if __name__ == "__main__":
    unittest.main()
