"""Local soft reset with an OLED code (docs/BMCU_MONITOR_STREAM_V2.md section 7)."""

import json
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "pico"))

import local_reset  # noqa: E402
from local_reset import LocalReset  # noqa: E402
from oled_ticker import OledTicker  # noqa: E402

sys.path.insert(0, str(ROOT / "ci"))
from test_pico_oled_ticker import Display, Monitor, status  # noqa: E402

REQUEST = {"content-type": "application/json",
           "x-bmcu-control-action": "request"}
RESET = {"content-type": "application/json",
         "x-bmcu-control-action": "reset"}


class Clock:
    def __init__(self):
        self.now = 1000

    def __call__(self):
        return self.now


class LocalResetTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.panel = [True]
        self.calls = []
        self.events = []
        self.fail_with = None
        # A fixed urandom: 0x1234 = 4660 -> "4660".
        self.reset = LocalReset(
            self.control, ("bmcu-a", "bmcu-b"), lambda: self.panel[0],
            self.clock, urandom=lambda n: b"\x12\x34",
            on_event=self.events.append)

    def control(self, link):
        if self.fail_with:
            raise ValueError(self.fail_with)
        self.calls.append(link)
        return b"accepted"

    def post(self, path, headers, value):
        return self.reset.handle("POST", path, headers,
                                 json.dumps(value).encode())

    def request(self, link=1):
        return self.post(local_reset.REQUEST_PATH, REQUEST, {"link": link})

    def submit(self, code, link=1):
        return self.post(local_reset.RESET_PATH, RESET,
                         {"link": link, "code": code})

    def test_the_code_reaches_the_panel_and_nothing_else(self):
        status_line, _, body = self.request()
        self.assertEqual(status_line, "202 Accepted")
        self.assertNotIn(b"4660", body)
        self.assertFalse(any("4660" in event for event in self.events))
        lines = self.reset.overlay_lines(self.clock.now)
        self.assertEqual(lines[0], "RESET bmcu-b")
        self.assertIn("4 6 6 0", lines[2])
        self.assertTrue(all(len(line) <= 16 for line in lines), lines)

    def test_the_right_code_resets_that_link_once(self):
        self.request()
        status_line, _, body = self.submit("4660")
        self.assertEqual(status_line, "200 OK")
        self.assertEqual(json.loads(body), {"result": "accepted"})
        self.assertEqual(self.calls, [1])
        # Single use.
        self.assertEqual(self.submit("4660")[0], "409 Conflict")
        self.assertEqual(self.calls, [1])
        self.assertEqual(self.reset.overlay_lines(self.clock.now)[2], "OK")

    def test_three_wrong_codes_invalidate_it(self):
        self.request()
        self.assertEqual(json.loads(self.submit("0000")[2]),
                         {"attempts_left": 2})
        self.assertEqual(self.submit("0001")[0], "403 Forbidden")
        self.assertEqual(self.submit("0002")[0], "410 Gone")
        self.assertEqual(self.submit("4660")[0], "409 Conflict")
        self.assertEqual(self.calls, [])

    def test_the_wrong_link_is_a_wrong_attempt(self):
        self.request(link=1)
        self.assertEqual(self.submit("4660", link=0)[0], "403 Forbidden")
        self.assertEqual(self.calls, [])

    def test_the_code_expires_after_sixty_seconds(self):
        self.request()
        self.clock.now += local_reset.CODE_TTL_MS
        self.assertEqual(self.submit("4660")[0], "410 Gone")
        self.assertEqual(self.calls, [])

    def test_no_panel_no_code(self):
        self.panel[0] = False
        self.assertEqual(self.request()[0], "503 Service Unavailable")
        self.assertIsNone(self.reset.overlay_lines(self.clock.now))

    def test_requests_are_rate_limited(self):
        self.request()
        self.clock.now += 1000
        self.assertEqual(self.request()[0], "429 Too Many Requests")
        self.clock.now += local_reset.REQUEST_GAP_MS
        self.assertEqual(self.request()[0], "202 Accepted")

    def test_a_guard_refusal_still_consumes_the_code(self):
        self.request()
        self.fail_with = "motor running"
        status_line, _, body = self.submit("4660")
        self.assertEqual(status_line, "409 Conflict")
        self.assertIn("motor running", json.loads(body)["error"])
        self.assertEqual(self.submit("4660")[0], "409 Conflict")

    def test_cross_site_posts_are_refused(self):
        form = {"content-type": "application/x-www-form-urlencoded"}
        self.assertEqual(
            self.post(local_reset.REQUEST_PATH, form, {"link": 1})[0],
            "403 Forbidden")
        # The action header must name this endpoint, not the other one.
        self.assertEqual(
            self.post(local_reset.REQUEST_PATH, RESET, {"link": 1})[0],
            "403 Forbidden")

    def test_get_and_unknown_paths(self):
        self.assertEqual(self.reset.handle("GET", local_reset.RESET_PATH,
                                           {}, b"")[0],
                         "405 Method Not Allowed")
        self.assertIsNone(self.reset.handle("POST", "/api/other", REQUEST,
                                            b"{}"))
        self.assertEqual(self.post(local_reset.REQUEST_PATH, REQUEST,
                                   {"link": 7})[0], "400 Bad Request")

    def test_codes_are_uniform_over_four_digits(self):
        values = iter([b"\xff\xff", b"\xea\x60", b"\x00\x07"])
        # 65535 and 60000 are rejected; 7 -> "0007".
        self.assertEqual(local_reset.draw_code(lambda n: next(values)),
                         "0007")


class OverlayTests(unittest.TestCase):
    def test_the_overlay_owns_the_panel_until_it_ends(self):
        lines = [("RESET bmcu-b", "", "  CODE 4 6 6 0")]
        display = Display()
        ticker = OledTicker(display, [Monitor(status=status())],
                            overlay_provider=lambda now: lines[0])
        for step in range(8):
            ticker.service(step * 250)
        self.assertEqual(display.frame.lines[16][0], "  CODE 4 6 6 0")
        self.assertEqual(display.frame.lines[0][0], "RESET bmcu-b")
        lines[0] = None
        for step in range(8, 40):
            ticker.service(step * 250)
        self.assertNotIn("CODE", display.frame.lines.get(16, ("",))[0])

    def test_panel_ready_follows_write_failures(self):
        display = Display(fail=True)
        ticker = OledTicker(display, [Monitor(status=status())])
        self.assertTrue(ticker.panel_ready())
        with self.assertRaises(OSError):
            for step in range(16):
                ticker.service(step)
        self.assertFalse(ticker.panel_ready())


if __name__ == "__main__":
    unittest.main()
