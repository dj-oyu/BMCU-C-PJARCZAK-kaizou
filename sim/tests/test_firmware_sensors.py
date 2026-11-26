"""The firmware against the sensor world: first-boot calibration, then a load.

Set BMCU_SIM_IMAGE as for test_firmware_boot.  The checks are about what can
be seen from outside -- flash writes, the monitor link, the motor drive and
where the filament ends up -- not about firmware internals.  The world's
parameters are assumptions until fitted to Pico recordings, so the bounds are
loose on purpose: they catch "never calibrates", "does not react to
insertion", "drives the wrong motor", not millimetre differences.
"""
from __future__ import annotations

import json
import os
import unittest
from pathlib import Path

from sim.clock import HCLK_HZ

IMAGE = os.environ.get("BMCU_SIM_IMAGE")

CALIBRATE = {"seconds": 14, "events": [{"t": 3.0, "do": "calibration_gesture", "until": 14}]}
LOAD = {"seconds": 6, "events": [{"t": 1.0, "do": "insert", "ch": 0}]}
# Filament pushed only past the outer switch and let go (no push toward the gear).
SHORT_INSERT = {"seconds": 7.5, "events": [{"t": 1.0, "do": "insert", "ch": 0, "tip_mm": -35, "push_s": 0}]}
RECORDING = Path(__file__).resolve().parent.parent / "recordings" / "2026-11-25_ams_a_short_insert.jsonl"


@unittest.skipUnless(IMAGE, "BMCU_SIM_IMAGE not set")
class CalibrateThenLoad(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from sim.run import nvm_start, run
        with open(IMAGE, "rb") as f:
            cls.image = f.read()
        cls.nvm_off = nvm_start(cls.image)
        cls.cal = run(cls.image, 0, scenario=CALIBRATE)
        cls.nvm = bytes(cls.cal[0].flash.raw)
        cls.load = run(cls.image, 0, scenario=LOAD, nvm=cls.nvm)
        cls.world = cls.load[0].world
        cls.short = run(cls.image, 0, scenario=SHORT_INSERT, nvm=cls.nvm)

    def test_calibration_is_written_to_flash(self):
        m = self.cal[0]
        self.assertIsNone(m.fault)
        programs = [(t, off) for t, op, off, n in m.flashctl.log if op == "program" and off >= self.nvm_off]
        self.assertTrue(programs, "calibration never stored")
        self.assertTrue(all(3.0 < t / HCLK_HZ < 14.0 for t, _ in programs))

    def test_calibrated_boot_reports_modules_and_reacts_to_insertion(self):
        m, pico = self.load[0], self.load[1]
        self.assertIsNone(m.fault)
        status = [msg["data"] for msg in pico.messages if msg.get("type") == "status"]
        self.assertTrue(status)
        self.assertEqual(status[-1]["inserted_mask"], 0b1111)    # four modules plugged in
        self.assertTrue(status[-1]["online_mask"] & 1, "inserted channel never went online")
        self.assertNotEqual(status[-1]["channel_flags"][0]["ks"], 0)

    def test_only_the_inserted_channel_is_driven_and_the_filament_moves(self):
        w = self.world
        self.assertGreater(w.channels[0].tip_mm, 30.0)
        for ch in (1, 2, 3):
            self.assertIsNone(w.channels[ch].tip_mm)
        # The first ~0.5 s is left out: right after boot the firmware commands
        # every motor forward for a few tens of milliseconds (seen with c3f1f67,
        # not yet checked on a device), which this test is not about.
        snaps = [s for t, s in w.snapshots if t > 0.5 * HCLK_HZ]
        self.assertTrue(any(s[0]["drive"] > 0.1 for s in snaps))
        self.assertFalse(any(abs(s[ch]["drive"]) > 0.0 for s in snaps for ch in (1, 2, 3)))

    def test_every_encoder_is_polled(self):
        for bus in self.world.buses:
            self.assertGreater(bus.transactions, 0, f"ch{bus.ch}")
            self.assertEqual(bus.nacks, 0, f"ch{bus.ch}")


    def test_short_insertion_times_out_like_the_recording(self):
        # Pico recording, AMS-A, 2026-11-25 10:03:56-10:04:20: ch0 went online,
        # then the auto-load gave up with exactly this record, twice.
        recorded = [json.loads(line).get("event", {}) for line in RECORDING.read_text().splitlines()]
        recorded = [e for e in recorded if e.get("event_name") == "dm_teardown" and e["dm_auto_state"] == 2]
        self.assertEqual(len(recorded), 2)
        got = [msg["data"] for msg in self.short[1].messages
               if msg.get("type") == "event" and msg["data"].get("event_name") == "dm_teardown"]
        self.assertTrue(got, "no dm_teardown")
        for key in ("slot", "dm_auto_state", "cause", "ks"):
            self.assertEqual(got[0][key], recorded[0][key], key)
        self.assertAlmostEqual(got[0]["held_ms"], recorded[0]["held_ms"], delta=50)


if __name__ == "__main__":
    unittest.main()
