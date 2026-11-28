"""SET_LONG_REPLY end to end on the real image: link request -> bus answer.

Set BMCU_SIM_IMAGE as for test_firmware_boot (an AMS 0 image). The Pico asks
the BMCU to answer 0x0411 frames whose payload starts with 00; the printer
then sends 0x0411 for both AMS addresses. Only ours is answered, with the
table's bytes, and the reply is reported upstream as REPLIED.
"""
from __future__ import annotations

import os
import unittest

IMAGE = os.environ.get("BMCU_SIM_IMAGE")

OUTCOME_IGNORED = 3
OUTCOME_REPLIED = 2

SCENARIO = {"seconds": 9.0, "connected": [False] * 4, "events": [
    {"t": 5.0, "do": "long_reply", "long_type": 0x0411, "match": "00",
     "reply": "a1 a2 a3"},
    {"t": 6.0, "do": "printer_repeat", "long_type": 0x0411, "payload": "00 00 00 00",
     "every": 0.2, "until": 7.0},
    {"t": 6.1, "do": "printer_repeat", "long_type": 0x0411, "payload": "01 00 00 00",
     "every": 0.2, "until": 7.0},
    # Dropping the entry returns the frame to unanswered.
    {"t": 7.2, "do": "long_reply", "long_type": 0x0411, "clear": True},
    {"t": 7.6, "do": "printer_repeat", "long_type": 0x0411, "payload": "00 00 00 00",
     "every": 0.2, "until": 8.6},
]}


def _long_replies(printer, frame_type):
    out = []
    for frame in printer.replies:
        if frame.kind != "long" or not frame.ok or len(frame.payload) < 6:
            continue
        if frame.payload[4] | frame.payload[5] << 8 == frame_type:
            out.append(frame)
    return out


@unittest.skipUnless(IMAGE, "BMCU_SIM_IMAGE not set")
class LongReplyTable(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from sim.run import run
        with open(IMAGE, "rb") as f:
            cls.m, cls.pico, cls.printer, cls.wall = run(f.read(), 0, scenario=SCENARIO)
        cls.acks = [msg for msg in cls.pico.messages
                    if msg.get("type") == "ack" and msg.get("request_kind") == 0x19]
        cls.long = [msg for msg in cls.pico.messages
                    if msg.get("type") == "event"
                    and msg["data"].get("event_name") == "printer_long_transaction"
                    and msg["data"].get("frame_type") == 0x0411]

    def test_runs_without_cpu_fault(self):
        self.assertIsNone(self.m.fault)

    def test_both_requests_are_acked_ok(self):
        self.assertEqual([a["result"] for a in self.acks], [0, 0])
        self.assertEqual(self.pico.monitor.long_reply["state"], "applied")

    def test_our_frames_get_the_table_reply(self):
        replies = _long_replies(self.printer, 0x0411)
        self.assertGreaterEqual(len(replies), 4)
        for frame in replies:
            self.assertEqual(frame.payload[6:], bytes.fromhex("a1a2a3"))
            # To the printer (0x0600), from the AMS (0x0700).
            self.assertEqual(frame.payload[0:4], bytes.fromhex("00060007"))

    def test_only_while_the_entry_exists(self):
        replies = _long_replies(self.printer, 0x0411)
        # Five 00-frames between 6.0 s and 7.0 s; none after the clear.
        self.assertLessEqual(len(replies), 5)
        late = [m for m in self.long if m["sim_ms"] > 7700]
        self.assertTrue(late)
        self.assertTrue(all(m["data"]["outcome"] == OUTCOME_IGNORED for m in late))

    def test_replies_are_reported_upstream(self):
        outcomes = [m["data"]["outcome"] for m in self.long]
        self.assertIn(OUTCOME_REPLIED, outcomes)
        self.assertIn(OUTCOME_IGNORED, outcomes)


if __name__ == "__main__":
    unittest.main()
