"""Quint spec checks: the safety invariants hold, the known races are still
reachable, and the firmware policies replay the spec's traces without
diverging. Skipped where quint or zig is missing (host-tests.yml has neither;
.github/workflows/quint.yml installs both)."""

from pathlib import Path
import shutil
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / "spec" / "bmcu_merger.qnt"
sys.path.insert(0, str(ROOT / "ci"))


def _quint(*args):
    return subprocess.run(["quint", *args], cwd=ROOT, capture_output=True, text=True)


@unittest.skipIf(shutil.which("quint") is None, "quint is not installed")
class QuintSpecTests(unittest.TestCase):
    def test_typechecks(self):
        result = _quint("typecheck", str(SPEC))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_safety_holds_in_simulation(self):
        result = _quint("run", str(SPEC), "--backend=typescript", "--invariant=safety",
                        "--max-samples=300", "--max-steps=40", "--seed=7")
        self.assertEqual(result.returncode, 0, result.stdout[-3000:] + result.stderr)

    def test_known_races_stay_reachable(self):
        # Witnesses are expected to be violated. One that stops being violated
        # means the model lost a behaviour the firmware still has.
        for witness in ("witnessNoCollision", "witnessNoParkedOrphan"):
            with self.subTest(witness=witness):
                result = _quint("run", str(SPEC), "--backend=typescript",
                                f"--invariant={witness}", "--max-samples=3000",
                                "--max-steps=30", "--seed=7")
                self.assertNotEqual(result.returncode, 0,
                                    f"{witness} was not reached")
                self.assertIn("violation", result.stdout)

    @unittest.skipIf(shutil.which("zig") is None, "zig compiler is not installed")
    def test_firmware_policies_replay_spec_traces(self):
        import quint_replay
        self.assertEqual(quint_replay.main(["--traces", "150", "--steps", "40",
                                            "--seed", "11"]), 0)


if __name__ == "__main__":
    unittest.main()
