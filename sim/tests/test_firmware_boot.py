"""Boot a real firmware image and check what the outside world observes.

Set BMCU_SIM_IMAGE to a built .bin (the CI job builds AMS-A, retract 0.60,
standard profile, autoload on, RGB off).  Every assertion is about bytes on a
wire or a pin -- what the Pico and the printer see -- never about firmware
internals.
"""
from __future__ import annotations

import os
import unittest

IMAGE = os.environ.get("BMCU_SIM_IMAGE")


@unittest.skipUnless(IMAGE, "BMCU_SIM_IMAGE not set")
class FirmwareBoot(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from sim.run import run
        with open(IMAGE, "rb") as f:
            cls.m, cls.pico, cls.printer, cls.wall = run(f.read(), 8.0)

    def test_runs_without_cpu_fault(self):
        self.assertIsNone(self.m.fault)

    def test_hello_then_status_on_the_monitor_link(self):
        types = [msg.get("type") for msg in self.pico.messages]
        self.assertEqual(types[0], "hello")
        self.assertEqual(self.pico.messages[0]["protocol"], 0x83)   # alpha.3 wire version
        self.assertGreaterEqual(types.count("status"), 10)
        self.assertEqual(self.pico.monitor.decoder.crc_errors, 0)
        self.assertEqual(self.pico.monitor.decoder.frame_errors, 0)

    def test_status_answers_every_get_status(self):
        # BMCUMonitor drops a STATUS whose sequence it did not ask for, so an
        # unanswered request shows up as one still outstanding at the end.
        self.assertLessEqual(len(self.pico.monitor._outstanding_get_status), 1)

    def test_printer_queries_get_one_valid_reply_each(self):
        sent = [f for _, f in self.printer.sent if f[4] == 0x03]
        replies = [r for r in self.printer.replies if r.kind == "short" and r.ftype == 0x03]
        self.assertGreater(len(sent), 5)
        self.assertEqual(len(replies), len(sent))
        for r in self.printer.replies:
            self.assertTrue(r.ok, f"{r.errors} {r.raw.hex()}")
            self.assertGreaterEqual(r.raw[1], 0x80)          # short-frame flag

    def test_rs485_half_duplex_discipline(self):
        self.assertEqual(self.printer.violations, [])
        self.assertTrue(self.m.usart1.tx_log, "BMCU never transmitted to the printer")


if __name__ == "__main__":
    unittest.main()
