"""Token-gated long-reply requests (docs/BMCU_MONITOR_STREAM_V2.md section 7.3)."""

import json
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "pico"))
sys.path.insert(0, str(ROOT / "ci"))

import long_reply  # noqa: E402
from long_reply import LongReplyAPI  # noqa: E402
from test_pico_monitor import FakeUART, frame, link  # noqa: E402

TOKEN = "correct-horse-battery"
POST = {"content-type": "application/json",
        "x-bmcu-control-action": "long-reply",
        "x-bmcu-control-token": TOKEN}
GET = {"x-bmcu-control-token": TOKEN}


class Clock:
    def __init__(self):
        self.now = 1000

    def __call__(self):
        return self.now


class LongReplyTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.uarts = [FakeUART(), FakeUART()]
        self.monitors = [link.BMCUMonitor(uart, lambda _m: None, link_id=name)
                         for uart, name in zip(self.uarts, ("bmcu-a", "bmcu-b"))]
        self.events = []
        self.api = self.make(TOKEN)

    def make(self, token):
        return LongReplyAPI(self.monitors, ("bmcu-a", "bmcu-b"), token,
                            self.clock, on_event=self.events.append)

    def post(self, value, headers=POST, api=None):
        body = value if isinstance(value, bytes) else json.dumps(value).encode()
        return (api or self.api).handle("POST", long_reply.PATH, headers, body)

    def status(self, link_index=0, headers=GET):
        return self.api.handle(
            "GET", long_reply.PATH + "?link=%d" % link_index, headers, b"")

    @staticmethod
    def body(result):
        return json.loads(result[2])

    def sent(self, link_index=0):
        return link.FrameDecoder().feed(self.uarts[link_index].writes[-1])[0]

    # -- routing and guards ----------------------------------------------

    def test_other_paths_are_not_claimed(self):
        self.assertIsNone(self.api.handle("POST", "/api/control/reset", POST, b"{}"))

    def test_without_a_token_the_endpoint_is_off(self):
        for token in (None, "", "short-token"):
            result = self.post({"link": 0, "type": 0x0411, "reply": "00"},
                               api=self.make(token))
            self.assertEqual(result[0], "503 Service Unavailable")
        self.assertEqual(self.uarts[0].writes, [])

    def test_post_needs_the_cross_site_headers(self):
        for missing in ("content-type", "x-bmcu-control-action"):
            headers = dict(POST)
            del headers[missing]
            result = self.post({"link": 0, "type": 0x0411, "reply": "00"}, headers)
            self.assertEqual(result[0], "403 Forbidden")
        self.assertEqual(self.uarts[0].writes, [])

    def test_wrong_token_is_refused_and_sends_nothing(self):
        headers = dict(POST, **{"x-bmcu-control-token": TOKEN[:-1] + "X"})
        result = self.post({"link": 0, "type": 0x0411, "reply": "00"}, headers)
        self.assertEqual(result[0], "403 Forbidden")
        self.assertEqual(self.uarts[0].writes, [])

    def test_the_token_is_never_echoed_or_logged(self):
        self.post({"link": 0, "type": 0x0411, "reply": "00"})
        bad = dict(POST, **{"x-bmcu-control-token": "x" * 20})
        for _ in range(long_reply.FAILURES_BEFORE_LOCKOUT):
            result = self.post({"link": 0, "type": 0x0411, "reply": "00"}, bad)
            self.assertNotIn(TOKEN.encode(), result[2])
        self.assertFalse(any(TOKEN in event for event in self.events))

    def test_repeated_wrong_tokens_lock_even_the_right_one_out(self):
        bad = dict(POST, **{"x-bmcu-control-token": "x" * 20})
        for _ in range(long_reply.FAILURES_BEFORE_LOCKOUT):
            self.post({"link": 0, "type": 0x0411, "reply": "00"}, bad)
        result = self.post({"link": 0, "type": 0x0411, "reply": "00"})
        self.assertEqual(result[0], "429 Too Many Requests")
        self.clock.now += long_reply.LOCKOUT_MS
        result = self.post({"link": 0, "type": 0x0411, "reply": "00"})
        self.assertEqual(result[0], "202 Accepted")

    def test_a_right_token_resets_the_failure_count(self):
        bad = dict(POST, **{"x-bmcu-control-token": "x" * 20})
        for _ in range(long_reply.FAILURES_BEFORE_LOCKOUT - 1):
            self.post({"link": 0, "type": 0x0411, "reply": "00"}, bad)
        self.post({"link": 0, "type": 0x0411, "reply": "00"})
        result = self.post({"link": 0, "type": 0x0411, "reply": "00"}, bad)
        self.assertEqual(result[0], "403 Forbidden")

    # -- request validation ----------------------------------------------

    def test_bad_bodies_are_400_and_send_nothing(self):
        for value in (b"not json", [], {"type": 0x0411, "reply": "00"},
                      {"link": 2, "type": 0x0411, "reply": "00"},
                      {"link": True, "type": 0x0411, "reply": "00"},
                      {"link": 0, "reply": "00"},
                      {"link": 0, "type": 0x10000, "reply": "00"},
                      {"link": 0, "type": 0x0411},
                      {"link": 0, "type": 0x0411, "reply": "0"},
                      {"link": 0, "type": 0x0411, "reply": "zz"},
                      {"link": 0, "type": 0x0411, "reply": "00" * 33},
                      {"link": 0, "type": 0x0411, "match": "00" * 9, "reply": "00"},
                      {"link": 0, "type": 0x0411, "clear": True, "reply": "00"}):
            result = self.post(value)
            self.assertEqual(result[0], "400 Bad Request", value)
        self.assertEqual(self.uarts[0].writes, [])

    # -- what goes on the wire -------------------------------------------

    def test_set_is_framed_as_set_long_reply(self):
        result = self.post({"link": 1, "type": "0x0411", "match": "01",
                            "reply": "01 00 00 00"})
        self.assertEqual(result[0], "202 Accepted")
        decoded = self.sent(1)
        self.assertEqual(decoded["kind"], link.SET_LONG_REPLY)
        self.assertEqual(decoded["payload"], bytes.fromhex("1104" "00" "01" "04"
                                                           "01" "01000000"))
        self.assertEqual(self.body(result)["sequence"], decoded["sequence"])
        self.assertEqual(self.uarts[0].writes, [])

    def test_clear_carries_the_flag_and_no_reply(self):
        self.post({"link": 0, "type": 1041, "clear": True})
        self.assertEqual(self.sent(0)["payload"], bytes.fromhex("1104" "01" "00" "00"))

    def test_the_largest_request_fits_one_link_frame(self):
        result = self.post({"link": 0, "type": 0x0411, "match": "aa" * 8,
                            "reply": "bb" * 32})
        self.assertEqual(result[0], "202 Accepted")
        self.assertEqual(len(self.sent(0)["payload"]), 45)

    # -- status and the BMCU's answer ------------------------------------

    def test_status_follows_the_ack(self):
        self.assertEqual(self.body(self.status())["state"], "none")
        sequence = self.body(self.post({"link": 0, "type": 0x0411,
                                        "reply": "00"}))["sequence"]
        self.assertEqual(self.body(self.status())["state"], "sent")
        # Firmware without a handler answers every unknown kind UNSUPPORTED.
        self.monitors[0]._handle_frame(
            frame(link.ACK, sequence, bytes((link.SET_LONG_REPLY, 2))), 1010)
        view = self.body(self.status())
        self.assertEqual((view["state"], view["ack_result"], view["ack"]),
                         ("rejected", 2, "unsupported"))

    def test_an_ack_for_another_sequence_is_ignored(self):
        sequence = self.body(self.post({"link": 0, "type": 0x0411,
                                        "reply": "00"}))["sequence"]
        self.monitors[0]._handle_frame(
            frame(link.ACK, (sequence + 1) & 0xFFFF,
                  bytes((link.SET_LONG_REPLY, 0))), 1010)
        self.assertEqual(self.body(self.status())["state"], "sent")

    def test_ok_ack_reads_applied(self):
        sequence = self.body(self.post({"link": 0, "type": 0x0411,
                                        "reply": "00"}))["sequence"]
        self.monitors[0]._handle_frame(
            frame(link.ACK, sequence, bytes((link.SET_LONG_REPLY, 0))), 1010)
        self.assertEqual(self.body(self.status())["state"], "applied")

    def test_silence_reads_no_ack(self):
        self.post({"link": 0, "type": 0x0411, "reply": "00"})
        self.clock.now += long_reply.ACK_TIMEOUT_MS
        self.assertEqual(self.body(self.status())["state"], "no_ack")

    def test_status_needs_the_token_and_a_link(self):
        self.assertEqual(self.status(headers={})[0], "403 Forbidden")
        result = self.api.handle("GET", long_reply.PATH, GET, b"")
        self.assertEqual(result[0], "400 Bad Request")


if __name__ == "__main__":
    unittest.main()
