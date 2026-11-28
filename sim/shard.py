"""Split the simulator tests into CI shards that run in parallel.

Every firmware test class boots the real image from reset, and most of them
first run the calibration scenario, so a class is the unit of work. The heavy
classes get a shard each; ``rest`` is every test not named below, so a new
test can never fall outside all shards.

    python -m sim.shard --names         JSON list of shard names (CI matrix)
    python -m sim.shard <name>          run one shard; a skip fails it
"""
from __future__ import annotations

import json
import sys
import unittest

# Measured locally on 2026-11-28 with the AMS-A image, all shards at once:
# open_outlet 209 s, anchored_jam 182 s, calibrate_load 144 s, the two gear
# slip classes 295 s together (so one shard each), rest 75 s. In one process
# the sensor classes alone took 610 s.
SHARDS = {
    "calibrate_load": ["sim.tests.test_firmware_sensors.CalibrateThenLoad"],
    "anchored_jam": ["sim.tests.test_firmware_sensors.AnchoredSpoolJam"],
    "open_outlet": ["sim.tests.test_firmware_sensors.OpenOutlet"],
    "gear_slip": ["sim.tests.test_firmware_sensors.GearSlip"],
    "gear_slip_freed": ["sim.tests.test_firmware_sensors.GearSlipFreed"],
}
REST = "rest"


def _cases(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from _cases(item)
        else:
            yield item


def _class_id(case) -> str:
    return "%s.%s" % (type(case).__module__, type(case).__qualname__)


def select(name: str) -> unittest.TestSuite:
    named = {cls for classes in SHARDS.values() for cls in classes}
    if name != REST and name not in SHARDS:
        raise SystemExit("unknown shard %r" % name)
    wanted = set(SHARDS.get(name, ()))
    found = set()
    suite = unittest.TestSuite()
    loaded = unittest.defaultTestLoader.discover("sim/tests", top_level_dir=".")
    for case in _cases(loaded):
        if isinstance(case, unittest.loader._FailedTest):
            # An import error belongs to every shard: fail loudly everywhere.
            suite.addTest(case)
            continue
        class_id = _class_id(case)
        if name == REST:
            if class_id not in named:
                suite.addTest(case)
        elif class_id in wanted:
            found.add(class_id)
            suite.addTest(case)
    missing = wanted - found
    if missing:
        raise SystemExit("shard %s names classes that do not exist: %s"
                         % (name, ", ".join(sorted(missing))))
    return suite


def main(argv: list[str]) -> int:
    if argv == ["--names"]:
        print(json.dumps(list(SHARDS) + [REST]))
        return 0
    if len(argv) != 1:
        print(__doc__)
        return 2
    suite = select(argv[0])
    if suite.countTestCases() == 0:
        print("shard %s selected no tests" % argv[0])
        return 1
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    # A skip here would mean the image test did not run; treat it as failure,
    # the same rule host-tests.yml applies.
    if result.skipped:
        print("::error::a simulator test was skipped")
        return 1
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
