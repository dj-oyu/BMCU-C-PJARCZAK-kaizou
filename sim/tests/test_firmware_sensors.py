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
# Filament pushed past the outer switch but not to the gear, then pulled out
# just after the firmware gives up (the auto-load times out 5 s after ks=2).
SHORT_INSERT = {"seconds": 7.5, "events": [{"t": 1.0, "do": "insert", "ch": 0, "tip_mm": -5, "push_s": 0},
                                           {"t": 6.2, "do": "remove", "ch": 0}]}
# A printer unload of a loaded channel, then a reload. On the world's path the
# outer lever stays pressed while the strand lies over it (outer -> gear ->
# inner), as on AMS-B slot 3: once the tip leaves the inner switch the key reads
# outer only, never none. Seen live on 2026-11-26: the pull back kept going
# until the tip was behind the gear, and the reload could not catch it.
# Motion frames are [ams, statu_flag, channel, motion_flag].
PULL_BACK_RELOAD = {"seconds": 11, "events": [
    {"t": 0.5, "do": "insert", "ch": 0, "tip_mm": 60, "push_s": 0},
    {"t": 1.0, "do": "printer_repeat", "type": 3, "payload": "00 07 00 7f", "every": 0.25, "until": 1.5},  # on_use
    {"t": 1.5, "do": "printer_repeat", "type": 3, "payload": "00 09 00 3f", "every": 0.25, "until": 2.0},  # before_pull_back
    {"t": 2.0, "do": "printer_repeat", "type": 3, "payload": "00 03 ff 00", "every": 0.25, "until": 8.0},  # pull_back
    {"t": 8.0, "do": "printer_repeat", "type": 3, "payload": "00 03 00 00", "every": 0.25, "until": 11.0},  # send_out
]}
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
        cls.pull = run(cls.image, 0, scenario=PULL_BACK_RELOAD, nvm=cls.nvm)

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


    def test_short_insertion_matches_the_recording(self):
        # Pico recording, AMS-A, 2026-11-25 10:03:56-10:04:20, twice: online,
        # auto-load gives up (state 2, cause 3, ks 2, 5000 ms), filament gone
        # (state 3, cause 4, ks 0), offline.
        lines = RECORDING.read_text().splitlines()
        recorded = [json.loads(line).get("event", {}) for line in lines]
        recorded = [e for e in recorded if e.get("event_name") in ("dm_teardown", "state_change")]
        got = [msg["data"] for msg in self.short[1].messages
               if msg.get("type") == "event" and msg["data"].get("event_name") in ("dm_teardown", "state_change")
               and msg["sim_ms"] > 500]
        fields = ("event_name", "slot", "field", "previous_value", "value", "dm_auto_state", "cause", "ks")

        def shape(events):
            return [{k: e[k] for k in fields if k in e} for e in events]
        # The recording starts with online 0->1.  The simulated firmware puts
        # that change in the STATUS reply to the GET_STATUS the monitor sent
        # 15 ms after insertion (GET_STATUS consumes pending changes without
        # an event), so it is checked through STATUS instead.
        self.assertEqual(recorded[0]["field"], 3)
        self.assertTrue(any(msg["data"]["online_mask"] & 1 for msg in self.short[1].messages
                            if msg.get("type") == "status" and 1000 <= msg["sim_ms"] < 6000))
        self.assertEqual(shape(got), shape(recorded[1:4]))
        self.assertAlmostEqual(got[0]["held_ms"], recorded[1]["held_ms"], delta=50)
        # Not compared: held_ms of the second teardown.  The device reports 0
        # (the filament had left the outer switch within a millisecond of the
        # timeout, both times); here the hand pulls it 0.1 s later.

    def test_pull_back_stops_in_front_of_the_gear_and_reloads(self):
        from sim.world import Params
        m, pico = self.pull[0], self.pull[1]
        self.assertIsNone(m.fault)
        gear = Params().gear_mm
        tips = [(t / HCLK_HZ, s[0]["tip_mm"]) for t, s in m.world.snapshots]
        # The unload ends once the tip has left the inner switch, with the
        # strand still in the gear, and stays there until the printer reloads.
        motions = [(msg["sim_ms"], msg["data"]["motion"][0]) for msg in pico.messages
                   if msg.get("type") == "status"]
        self.assertTrue(any(2500 < ms < 6000 and mo == 0 for ms, mo in motions),
                        "pull back never returned to idle")
        held = [tip for t, tip in tips if 6.0 < t < 8.0]
        self.assertTrue(held)
        self.assertTrue(all(tip is not None and tip >= gear for tip in held), held[:5])
        # The reload catches it.
        self.assertGreater(tips[-1][1], 30.0)


# The spool end holds at the core while printing (AMS-A ch3, 2026-11-27,
# recording sim/recordings/2026-11-27_ams_a_ch3_anchored_spool_jam.log): the
# extruder stretches the buffer, LOW+JAM latch, the printer stops the channel
# and unloads it. The user then frees the spool end, and the printer reloads.
# Live, the latch outlived the printer's unload: it cut the pull back to 100 mm
# and vetoed every later send_out, so the reload failed again and again until
# the buffer was pinched past 85 % by hand during a send_out.
ANCHORED_JAM = {"seconds": 30, "events": [
    {"t": 0.0, "do": "insert", "ch": 3, "tip_mm": 800, "push_s": 0},
    {"t": 0.3, "do": "extruder", "mm_s": 1.5},
    {"t": 1.0, "do": "printer_repeat", "type": 3, "payload": "00 09 03 7f", "every": 0.25, "until": 1.5},   # before_on_use
    {"t": 1.5, "do": "printer_repeat", "type": 3, "payload": "00 07 03 7f", "every": 0.25, "until": 14.0},  # on_use
    {"t": 3.0, "do": "set", "ch": 3, "anchored": True},
    {"t": 14.0, "do": "extruder", "mm_s": 0.0},
    {"t": 14.0, "do": "printer_repeat", "type": 3, "payload": "00 07 03 00", "every": 0.25, "until": 16.0},  # stop_on_use
    {"t": 16.0, "do": "printer_repeat", "type": 3, "payload": "00 09 03 3f", "every": 0.25, "until": 17.0},  # before_pull_back
    {"t": 17.0, "do": "printer_repeat", "type": 3, "payload": "00 03 ff 00", "every": 0.25, "until": 22.0},  # pull_back
    {"t": 22.0, "do": "printer_repeat", "type": 3, "payload": "00 00 ff 00", "every": 0.25, "until": 24.0},  # slot 255
    {"t": 23.0, "do": "set", "ch": 3, "anchored": False},                                                     # spool end freed
    {"t": 24.0, "do": "printer_repeat", "type": 3, "payload": "00 03 03 00", "every": 0.25, "until": 30.0},  # send_out ch3
]}


@unittest.skipUnless(IMAGE, "BMCU_SIM_IMAGE not set")
class AnchoredSpoolJam(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from sim.run import run
        with open(IMAGE, "rb") as f:
            image = f.read()
        nvm = bytes(run(image, 0, scenario=CALIBRATE)[0].flash.raw)
        cls.m, cls.pico = run(image, 0, scenario=ANCHORED_JAM, nvm=nvm)[:2]
        cls.status = [(msg["sim_ms"] / 1000, msg["data"]) for msg in cls.pico.messages
                      if msg.get("type") == "status"]
        cls.world = [(t / HCLK_HZ, s[3]) for t, s in cls.m.world.snapshots]

    def _jam(self, t0, t1):
        return [d["channel_flags"][3]["jam_latch"] for t, d in self.status if t0 < t < t1]

    def test_the_held_strand_latches_a_jam(self):
        self.assertIsNone(self.m.fault)
        self.assertTrue(any(self._jam(3.0, 14.0)), "the anchored spool never latched JAM")

    def test_the_printer_unload_clears_the_jam(self):
        self.assertTrue(self._jam(22.0, 30.0))
        self.assertFalse(any(self._jam(22.0, 30.0)), "JAM outlived the printer's unload")

    def test_the_unload_is_not_cut_to_100_mm(self):
        tips = [c["tip_mm"] for t, c in self.world if 16.0 < t < 24.0 and c["tip_mm"] is not None]
        self.assertGreater(max(tips) - min(tips), 150.0)

    def test_the_reload_drives_once_the_spool_end_is_free(self):
        drive = [c["drive"] for t, c in self.world if 24.5 < t < 30.0]
        self.assertTrue(any(d > 0.1 for d in drive), "send_out never drove the motor")


# Tube off the filament hub mid-print (docs/FEED_RESISTANCE_DESIGN.md §2).
# The BMCU keeps feeding and the push goes out of the gap; the guard has to
# stop it, wait while the extruder takes up the spilled slack, and resume once
# the tube is drawn back on and the extruder pulls on the strand again --
# without the resume reading as a jam, and without telling the printer: it
# recovers by itself. Before the detach it is a normal print at 4 mm/s, which
# must not trip.
OPEN_OUTLET = {"seconds": 36, "events": [
    {"t": 0.0, "do": "insert", "ch": 3, "tip_mm": 800, "push_s": 0},
    {"t": 0.3, "do": "extruder", "mm_s": 4.0},
    {"t": 1.0, "do": "printer_repeat", "type": 3, "payload": "00 09 03 7f", "every": 0.25, "until": 1.5},   # before_on_use
    {"t": 1.5, "do": "printer_repeat", "type": 3, "payload": "00 07 03 7f", "every": 0.25, "until": 36.0},  # on_use
    {"t": 8.0, "do": "set", "ch": 3, "outlet_open": True},
]}


@unittest.skipUnless(IMAGE, "BMCU_SIM_IMAGE not set")
class OpenOutlet(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from sim.run import run
        with open(IMAGE, "rb") as f:
            image = f.read()
        nvm = bytes(run(image, 0, scenario=CALIBRATE)[0].flash.raw)
        cls.m, cls.pico = run(image, 0, scenario=OPEN_OUTLET, nvm=nvm)[:2]
        cls.status = [(msg["sim_ms"] / 1000, msg["data"]) for msg in cls.pico.messages
                      if msg.get("type") == "status"]
        cls.world = [(t / HCLK_HZ, s[3]) for t, s in cls.m.world.snapshots]
        cls.reseat_t = next((t for t, c in cls.world if t > 8.0 and not c["outlet_open"]), None)

    def test_printing_feeds_without_tripping(self):
        self.assertIsNone(self.m.fault)
        drive = [c["drive"] for t, c in self.world if 4.0 < t < 8.0]
        self.assertTrue(any(d > 0.1 for d in drive), "the print was not fed before the detach")
        bufs = [c["buffer_mm"] for t, c in self.world if 4.0 < t < 8.0]
        self.assertGreater(min(bufs), -6.0, "the buffer was stretched: feeding stopped")

    def test_the_spill_is_bounded(self):
        spill = max(c["spill_mm"] for t, c in self.world)
        self.assertGreater(spill, 5.0, "the scenario never spilled")
        self.assertLess(spill, 80.0, "the guard let the open outlet pour")

    def test_no_push_while_the_outlet_is_open(self):
        peak_t = max(self.world, key=lambda tc: tc[1]["spill_mm"])[0]
        end = self.reseat_t if self.reseat_t is not None else 36.0
        drive = [c["drive"] for t, c in self.world if peak_t + 0.5 < t < end]
        self.assertTrue(drive)
        self.assertFalse(any(d > 0.05 for d in drive), "pushed into the open outlet")

    def test_resumes_once_the_tube_is_back(self):
        self.assertIsNotNone(self.reseat_t, "the extruder never took up the slack")
        drive = [c["drive"] for t, c in self.world if self.reseat_t < t < 36.0]
        self.assertTrue(any(d > 0.1 for d in drive), "feeding never resumed")
        bufs = [c["buffer_mm"] for t, c in self.world if 33.0 < t < 36.0]
        self.assertGreater(min(bufs), -6.0, "the resumed print is starved")

    def test_the_resume_is_not_a_jam(self):
        flags = [d["channel_flags"][3] for t, d in self.status if t > 1.5]
        self.assertFalse(any(f["jam_latch"] or f["low_latch"] for f in flags),
                         "the guard's stop or its resume latched LOW/JAM")


# The drive gear slips on a held strand mid-print (2026-11-27: grinding dust
# in the BMCU). The AS5600 reads the gear turning, the strand stays put and the
# extruder stretches the buffer. Printing speed (1 mm/s, AMS-B ch3 measured
# 1.1) and a buffer slower to read the stretch than the default model: with
# the default 12 mm stroke the jam detector fires within a second whatever
# the guard does, and the test would not tell the builds apart. Here the
# deployed build (64b5c7c) grinds ~166 mm, the open-outlet guard without the
# slip verdict ~617 mm and never stops (its resume on tension disarmed the jam
# detector), and the slip verdict ~108 mm (an open-outlet stretch, the
# resume on the held strand's tension, then the slip).
GEAR_SLIP = {"seconds": 20, "params": {"buffer_travel_mm": 30.0}, "events": [
    {"t": 0.0, "do": "insert", "ch": 3, "tip_mm": 800, "push_s": 0},
    {"t": 0.3, "do": "extruder", "mm_s": 1.0},
    {"t": 1.0, "do": "printer_repeat", "type": 3, "payload": "00 09 03 7f", "every": 0.25, "until": 1.5},   # before_on_use
    {"t": 1.5, "do": "printer_repeat", "type": 3, "payload": "00 07 03 7f", "every": 0.25, "until": 20.0},  # on_use
    {"t": 8.0, "do": "set", "ch": 3, "slip": True},
]}


@unittest.skipUnless(IMAGE, "BMCU_SIM_IMAGE not set")
class GearSlip(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from sim.run import run
        with open(IMAGE, "rb") as f:
            image = f.read()
        nvm = bytes(run(image, 0, scenario=CALIBRATE)[0].flash.raw)
        cls.m, cls.pico = run(image, 0, scenario=GEAR_SLIP, nvm=nvm)[:2]
        cls.status = [(msg["sim_ms"] / 1000, msg["data"]) for msg in cls.pico.messages
                      if msg.get("type") == "status"]
        cls.world = [(t / HCLK_HZ, s[3]) for t, s in cls.m.world.snapshots]

    def test_the_grinding_is_bounded(self):
        self.assertIsNone(self.m.fault)
        ground = max(c["slipped_mm"] for t, c in self.world)
        self.assertGreater(ground, 1.0, "the scenario never slipped")
        self.assertLess(ground, 130.0, "the gear kept grinding")

    def test_the_motor_stays_stopped(self):
        stop_t = next(t for t, c in self.world if c["slipped_mm"] >= max(
            cc["slipped_mm"] for tt, cc in self.world))
        drive = [c["drive"] for t, c in self.world if stop_t + 0.5 < t < 20.0]
        self.assertTrue(drive)
        self.assertFalse(any(abs(d) > 0.05 for d in drive), "the motor ran after the slip")

    def test_the_printer_is_told(self):
        flags = [d["channel_flags"][3] for t, d in self.status if t > 8.0]
        self.assertTrue(any(f["jam_latch"] for f in flags), "no jam was raised for the slip")


if __name__ == "__main__":
    unittest.main()
