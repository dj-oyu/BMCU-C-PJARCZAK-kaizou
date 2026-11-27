"""Printer long frames the firmware does not answer, seen from the monitor link.

Set BMCU_SIM_IMAGE as for test_firmware_boot. PRINTER_LONG_TRANSACTION carries
only a hash of such a frame's payload; RECORD_PRINTER_LONG_PAYLOAD carries the
bytes, once per distinct (type, hash).
"""
from __future__ import annotations

import os
import unittest

IMAGE = os.environ.get("BMCU_SIM_IMAGE")

# 0x0411 at the rate a printer sends it, with two payloads alternating as on the
# bus (one per AMS address), and one 10-byte 0x040D that needs three chunks.
# The firmware starts serving the printer bus about 5.6 s after reset, which is
# where sim/run.py's default scenario starts its queries too.
UNANSWERED = {"seconds": 9.0, "connected": [False] * 4, "events": [
    {"t": 6.0, "do": "printer_repeat", "long_type": 0x0411, "payload": "00 01 02 03",
     "every": 0.2, "until": 8.0},
    {"t": 6.1, "do": "printer_repeat", "long_type": 0x0411, "payload": "01 01 02 03",
     "every": 0.2, "until": 8.0},
    {"t": 7.05, "do": "printer", "long_type": 0x040D,
     "payload": "10 11 12 13 14 15 16 17 18 19"},
]}


@unittest.skipUnless(IMAGE, "BMCU_SIM_IMAGE not set")
class UnansweredLongFrames(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from sim.run import run
        with open(IMAGE, "rb") as f:
            cls.m, cls.pico, cls.printer, cls.wall = run(f.read(), 0, scenario=UNANSWERED)
        cls.chunks = [msg["data"] for msg in cls.pico.messages
                      if msg.get("type") == "event"
                      and msg["data"].get("event_name") == "printer_long_payload"]

    def _payloads(self, frame_type):
        out = {}
        current = None
        for c in self.chunks:
            if c["frame_type"] != frame_type:
                continue
            if c["offset"] == 0:
                current = bytearray()
                out.setdefault(c["total_length"], []).append(current)
            self.assertIsNotNone(current, "a chunk arrived before its offset 0")
            self.assertEqual(len(current), c["offset"])
            current += bytes.fromhex(c["data_hex"])
        return out

    def test_runs_without_cpu_fault(self):
        self.assertIsNone(self.m.fault)

    def test_each_distinct_payload_is_captured_once(self):
        got = self._payloads(0x0411)
        self.assertEqual(sorted(bytes(p) for p in got[4]),
                         [bytes.fromhex("00010203"), bytes.fromhex("01010203")])

    def test_a_longer_payload_arrives_in_chunks(self):
        got = self._payloads(0x040D)
        self.assertEqual([bytes(p) for p in got[10]], [bytes(range(0x10, 0x1A))])

    def test_the_hash_record_still_goes_out(self):
        kinds = [msg["data"].get("frame_type") for msg in self.pico.messages
                 if msg.get("type") == "event"
                 and msg["data"].get("event_name") == "printer_long_transaction"]
        self.assertIn(0x0411, kinds)
        self.assertIn(0x040D, kinds)


if __name__ == "__main__":
    unittest.main()
