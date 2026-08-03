"""Local diagnostic endpoints.

These could not be tested before: the routing lived in main.py, which imports
MicroPython-only modules and cannot be loaded on the host. Extracting it was
mostly for this.
"""
import importlib.util
import struct
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PICO = ROOT / "pico"
added = str(PICO) not in sys.path
if added:
    sys.path.insert(0, str(PICO))

import binary_api as api  # noqa: E402

SPEC = importlib.util.spec_from_file_location("binary_api_link", PICO / "bmcu_link.py")
link = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(link)
if added:
    sys.path.remove(str(PICO))


class FakeCapture:
    def __init__(self, records):
        self._records = records

    def records(self):
        return iter(self._records)


class FakeMonitor:
    def __init__(self, link_index=0, **overrides):
        self.link_index = link_index
        self.link_state = "online"
        self.channels = [None, None, None, None]
        self.snapshot = None
        self.events = []
        self.bmcu_boot_session = 0
        self.tick_hz = None
        self.sequence_gap_count = 0
        self.capture = None
        for name, value in overrides.items():
            setattr(self, name, value)


class FakeRing:
    def __init__(self, records=()):
        self._records = list(records)

    def iter_records(self):
        return iter(self._records)


class FakeOutbox:
    def __init__(self, current=(), durable=()):
        self._current = list(current)
        self.durable = FakeRing(durable)

    def iter_current(self):
        return iter(self._current)


class FakeLog:
    def __init__(self, messages=()):
        self._messages = list(messages)
        self.calls = []

    def iter_messages(self, after, limit):
        self.calls.append((after, limit))
        return iter(self._messages)


def build(monitors=None, current=(), durable=(), messages=(), diagnostic=b"D"):
    outbox = FakeOutbox(current, durable)
    log = FakeLog(messages)
    return api.BinaryAPI(
        monitors if monitors is not None else [FakeMonitor()],
        outbox, log, lambda: diagnostic), outbox, log


def parse_snapshot(blobs):
    out = []
    for blob in blobs:
        assert blob[:4] == api.SNAPSHOT_MAGIC
        tick, _reserved, length = struct.unpack_from(">IHH", blob, 8)
        out.append({
            "version": blob[4], "link": blob[5], "record_type": blob[6],
            "record_index": blob[7], "hw_tick32": tick,
            "data": blob[api.SNAPSHOT_HEADER:api.SNAPSHOT_HEADER + length],
        })
        assert len(blob) == api.SNAPSHOT_HEADER + length
    return out


class RoutingTests(unittest.TestCase):
    def test_unknown_paths_are_not_served(self):
        route, _outbox, _log = build()
        for path in ("/api/devices", "/api/status", "/", "/api/snapshot",
                     "/api/../secrets.py"):
            self.assertIsNone(route(path), path)

    def test_query_string_does_not_change_the_route(self):
        route, _outbox, _log = build(current=[b"one"])
        self.assertEqual(route("/api/current.bin?after=5"), [b"one"])

    def test_history_alias_serves_current(self):
        route, _outbox, _log = build(current=[b"one"])
        self.assertEqual(route("/api/history/status.bin"), [b"one"])

    def test_log_cursor_and_limit_are_bounded(self):
        route, _outbox, log = build(messages=[b"m"])
        route("/api/logs.bin")
        route("/api/logs.bin?after=9&limit=999")
        route("/api/logs.bin?after=abc&limit=-4")
        self.assertEqual(log.calls, [(0, 32), (9, 64), (0, 0)])

    def test_events_are_filtered_by_cursor_and_byte_budget(self):
        durable = [(index, b"x" * 100, False, None) for index in range(1, 40)]
        route, _outbox, _log = build(durable=durable)
        self.assertEqual(len(route("/api/events.bin?after=0&limit=5")), 5)
        self.assertEqual(len(route("/api/events.bin?after=37")), 2)


class SnapshotTests(unittest.TestCase):
    def test_a_link_record_is_emitted_even_with_no_snapshot(self):
        monitor = FakeMonitor(link_state="stale", sequence_gap_count=7)
        route, _outbox, _log = build([monitor])

        records = parse_snapshot(route("/api/snapshot.bin"))

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["record_type"], api.RECORD_LINK)
        body = records[0]["data"]
        self.assertEqual(body[0], api.LINK_STATE_CODES["stale"])
        self.assertEqual(body[1], 0)
        self.assertEqual(struct.unpack_from(">III", body, 4), (0, 0, 7))

    def test_link_record_reports_resyncing_distinctly_from_stale(self):
        # The old UI collapsed every state to Receiving/Waiting, which hid the
        # difference that mattered while debugging the link.
        codes = set()
        for state in ("online", "stale", "resyncing", "incompatible"):
            route, _outbox, _log = build([FakeMonitor(link_state=state)])
            codes.add(parse_snapshot(route("/api/snapshot.bin"))[0]["data"][0])
        self.assertEqual(len(codes), 4)

    def test_full_status_payloads_are_passed_through_verbatim(self):
        parts = [
            {"record_type": 2, "hw_tick32": 111, "record_data": bytes(range(16))},
            {"record_type": 7, "hw_tick32": 222, "record_data": bytes(range(16, 32))},
        ]
        monitor = FakeMonitor(snapshot=parts, channels=[{}, {}, None, None])
        route, _outbox, _log = build([monitor])

        records = parse_snapshot(route("/api/snapshot.bin"))

        self.assertEqual(records[0]["data"][1], 2, "two channels present")
        self.assertEqual([r["record_type"] for r in records[1:]], [2, 7])
        self.assertEqual([r["hw_tick32"] for r in records[1:]], [111, 222])
        self.assertEqual(records[1]["data"], bytes(range(16)))
        self.assertEqual(records[2]["data"], bytes(range(16, 32)))

    def test_a_part_without_payload_is_skipped(self):
        monitor = FakeMonitor(snapshot=[{"record_type": 2, "record_data": b""}])
        route, _outbox, _log = build([monitor])
        self.assertEqual(len(route("/api/snapshot.bin")), 1)

    def test_events_are_served_as_the_bytes_that_arrived(self):
        raw = bytes(range(100, 116))
        monitor = FakeMonitor(events=[{"raw": raw, "hw_tick32": 5}])
        route, _outbox, _log = build([monitor])

        records = parse_snapshot(route("/api/snapshot.bin"))
        event = records[-1]
        self.assertEqual(event["record_type"], api.RECORD_EVENT)
        self.assertEqual(event["data"], raw)

    def test_records_carry_their_own_link_index(self):
        first = FakeMonitor(0, snapshot=[{"record_type": 2, "record_data": bytes(16)}])
        second = FakeMonitor(1, events=[{"raw": bytes(16), "hw_tick32": 1}])
        route, _outbox, _log = build([first, second])

        records = parse_snapshot(route("/api/snapshot.bin"))

        self.assertEqual({r["link"] for r in records}, {0, 1})
        self.assertEqual([r["link"] for r in records if r["record_type"] == 2], [0])
        self.assertEqual(
            [r["link"] for r in records if r["record_type"] == api.RECORD_EVENT], [1])

    def test_the_real_decoder_round_trips_a_served_event(self):
        # Guards the pass-through claim: what comes out still decodes.
        payload = bytes((7, 0, 0, 0, 4, 2, 1, 6)) + bytes((1, 2, 3, 0, 4, 0, 0, 0))
        decoded = link.BMCUMonitor._decode_event(payload)
        monitor = FakeMonitor(events=[decoded])
        route, _outbox, _log = build([monitor])

        served = parse_snapshot(route("/api/snapshot.bin"))[-1]["data"]

        self.assertEqual(served, payload)
        self.assertEqual(link.BMCUMonitor._decode_event(served)["event_name"],
                         "state_change")


class CaptureTests(unittest.TestCase):
    def test_capture_records_carry_reason_and_ordinal(self):
        monitor = FakeMonitor(capture=FakeCapture([(3, link.REJECT_CRC, 4242, b"abcd")]))
        route, _outbox, _log = build([monitor])

        blob = route("/api/capture.bin")[0]

        self.assertEqual(blob[:4], api.CAPTURE_MAGIC)
        self.assertEqual(blob[6], link.REJECT_CRC)
        ordinal, timestamp, length = struct.unpack_from(">IHH", blob, 8)
        self.assertEqual((ordinal, timestamp, length), (3, 4242, 4))
        self.assertEqual(blob[api.CAPTURE_HEADER:], b"abcd")

    def test_a_monitor_without_capture_is_skipped(self):
        route, _outbox, _log = build([FakeMonitor(capture=None)])
        self.assertEqual(route("/api/capture.bin"), [])


if __name__ == "__main__":
    unittest.main()
