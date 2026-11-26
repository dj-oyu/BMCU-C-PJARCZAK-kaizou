"""Quint spec checks: the safety invariants hold, the known races are still
reachable, and the firmware policies replay the spec's traces without
diverging. Skipped where quint or zig is missing (host-tests.yml installs
quint; .github/workflows/quint.yml installs both)."""

from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
MERGER = ROOT / "spec" / "bmcu_merger.qnt"
ZONE = ROOT / "spec" / "strand_zone.qnt"
sys.path.insert(0, str(ROOT / "ci"))

# Witnesses are expected to be violated. One that stops being violated means
# the model lost a behaviour the firmware still has.
WITNESSES = {
    MERGER: ("witnessNoCollision", "witnessNoParkedOrphan"),
    ZONE: ("witnessNeverSpurious", "witnessNoProofClear", "witnessNoDoubleDown"),
}


def _quint(*args):
    return subprocess.run(["quint", *args], cwd=ROOT, capture_output=True, text=True)


@unittest.skipIf(shutil.which("quint") is None, "quint is not installed")
class QuintSpecTests(unittest.TestCase):
    def test_typechecks(self):
        for spec in (MERGER, ZONE):
            with self.subTest(spec=spec.name):
                result = _quint("typecheck", str(spec))
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_safety_holds_in_simulation(self):
        for spec in (MERGER, ZONE):
            with self.subTest(spec=spec.name):
                result = _quint("run", str(spec), "--backend=typescript",
                                "--invariant=safety", "--max-samples=300",
                                "--max-steps=50", "--seed=7")
                self.assertEqual(result.returncode, 0,
                                 result.stdout[-3000:] + result.stderr)

    def test_known_behaviours_stay_reachable(self):
        for spec, names in WITNESSES.items():
            for witness in names:
                with self.subTest(spec=spec.name, witness=witness):
                    result = _quint("run", str(spec), "--backend=typescript",
                                    f"--invariant={witness}", "--max-samples=3000",
                                    "--max-steps=80", "--seed=7")
                    self.assertNotEqual(result.returncode, 0, f"{witness} was not reached")
                    self.assertIn("violation", result.stdout)

    @unittest.skipIf(shutil.which("zig") is None, "zig compiler is not installed")
    def test_firmware_policies_replay_spec_traces(self):
        import quint_replay
        self.assertEqual(quint_replay.main(["--traces", "200", "--steps", "80",
                                            "--seed", "11"]), 0)

    @unittest.skipIf(shutil.which("zig") is None, "zig compiler is not installed")
    def test_replay_catches_a_planted_bug(self):
        # The replay is only worth something if it fails on a wrong header:
        # drop the remnant a new strand behind an old tail must leave.
        import quint_replay
        header = (ROOT / "src" / "strand_zone_policy.h").read_text(encoding="utf-8")
        planted = "if (s.front_known && s.front > 0) s.remnant = 1u;"
        self.assertIn(planted, header)
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "strand_zone_policy.h").write_text(header.replace(planted, ""),
                                                             encoding="utf-8")
            rc = quint_replay.main(["--spec", "zone", "--traces", "150", "--steps", "50",
                                    "--seed", "11"], [f"-I{tmp}"])
        self.assertEqual(rc, 1)


if __name__ == "__main__":
    unittest.main()
