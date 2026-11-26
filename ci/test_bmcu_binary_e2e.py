"""Host-side end-to-end tests for the Pico BMS2 stream (v2).

The receiver here is written from docs/BMCU_MONITOR_STREAM_V2.md section 4 and
5.2 with struct alone, not with the Pico codec, so that a format bug cannot hide
by being wrong the same way on both ends. The socket is in memory and can be
told to refuse sends, which is how the retry and error paths are reached.
"""

import os
import struct
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PICO = os.path.join(ROOT, "pico")
sys.path.insert(0, PICO)

import bmcu_binary_constants as C
import bmcu_stream_udp as stream
from bmcu_binary_outbox import BMB1Outbox

HEADER = struct.Struct(">4sBBHIQQB3s")


class FakeUDPSocket:
    def __init__(self):
        self.datagrams = []
        self.refuse = []      # errno to raise on the next sendto calls
        self.closed = False

    def sendto(self, data, address):
        if self.refuse:
            raise OSError(self.refuse.pop(0))
        self.datagrams.append((bytes(data), address))
        return len(data)

    def close(self):
        self.closed = True


def parse_datagram(data):
    """Spec section 4, independently of bmcu_stream_udp."""
    assert len(data) <= 1200, len(data)
    magic, version, _flags, id_length = struct.unpack_from(">4sBBB", data, 0)
    assert magic == b"BMS2" and version == 1
    device_id = data[7:7 + id_length]
    count = data[7 + id_length]
    pos = 8 + id_length
    records = []
    for _ in range(count):
        (rmagic, rversion, rtype, flags, length, sequence, boot, link,
         _reserved) = HEADER.unpack_from(data, pos)
        assert rmagic == b"BMB1" and rversion == 1
        payload = data[pos + 32:pos + 32 + length]
        assert len(payload) == length
        records.append({"type": rtype, "flags": flags, "sequence": sequence,
                        "boot": boot, "link": link, "payload": payload})
        pos += 32 + length
    assert pos == len(data), "trailing bytes"
    return device_id, records


def parse_heartbeat(payload):
    """Spec section 5.2."""
    interval, uptime, next_sequence = struct.unpack_from(">IQQ", payload, 0)
    firmware_length = payload[20]
    firmware = payload[21:21 + firmware_length]
    pos = 21 + firmware_length
    links = []
    for _ in range(payload[pos]):
        index, id_length = payload[pos + 1], payload[pos + 2]
        link_id = payload[pos + 3:pos + 3 + id_length]
        state = payload[pos + 3 + id_length]
        links.append((index, link_id, state))
        pos += 3 + id_length
    assert pos + 1 == len(payload), "trailing heartbeat bytes"
    return {"interval_ms": interval, "uptime_ms": uptime,
            "next_sequence": next_sequence, "firmware": firmware,
            "links": links}


def link_state_payload(state):
    return b"\0" * 8 + bytes((state, 0, 0, 0))


class StreamEndToEndTests(unittest.TestCase):
    BOOT = 0x1020304050607080

    def make(self, outbox=None, heartbeat_ms=5000, per_loop=4, states=None):
        sock = FakeUDPSocket()
        outbox = outbox or BMB1Outbox(self.BOOT, durable_slots=16)
        client = stream.BMS2UDPClient(
            outbox, "receiver", 8799, b"pico-e2e", b"test",
            ((0, b"bmcu-a"), (1, b"bmcu-b")),
            link_states=lambda: states or (C.LINK_ONLINE, C.LINK_STALE),
            uptime_ms=lambda: 4242, socket_factory=lambda: sock,
            resolver=lambda host, port: (host, port),
            heartbeat_ms=heartbeat_ms, datagrams_per_loop=per_loop)
        return client, sock, outbox

    def records(self, sock, skip_heartbeats=True):
        result = []
        for data, _ in sock.datagrams:
            device_id, records = parse_datagram(data)
            self.assertEqual(device_id, b"pico-e2e")
            for record in records:
                if skip_heartbeats and record["type"] == C.HEARTBEAT:
                    continue
                result.append(record)
        return result

    def test_first_datagram_is_a_heartbeat_on_its_own(self):
        client, sock, outbox = self.make()
        outbox.enqueue_payload(C.LINK_STATE, C.FLAG_CRITICAL, 0, 1,
                               link_state_payload(C.LINK_ONLINE))
        client.poll(0)
        _, first = parse_datagram(sock.datagrams[0][0])
        self.assertEqual(len(first), 1)
        record = first[0]
        self.assertEqual(record["type"], C.HEARTBEAT)
        self.assertEqual(record["sequence"], 0)
        self.assertEqual(record["link"], 0xFF)
        self.assertEqual(record["boot"], self.BOOT)
        beat = parse_heartbeat(record["payload"])
        self.assertEqual(beat["interval_ms"], 5000)
        self.assertEqual(beat["uptime_ms"], 4242)
        self.assertEqual(beat["firmware"], b"test")
        self.assertEqual(beat["links"], [(0, b"bmcu-a", C.LINK_ONLINE),
                                         (1, b"bmcu-b", C.LINK_STALE)])
        # Taken before the record went out, so it names the record's sequence.
        self.assertEqual(beat["next_sequence"], 2)
        self.assertEqual(client.state, stream.ONLINE)

    def test_raw_frame_is_forwarded_byte_for_byte_and_released(self):
        client, sock, outbox = self.make()
        wire = b"\x3d\xc3\x03\x07\x10\x00" + bytes(16)
        sequence = outbox.enqueue_raw(0, 123456, wire,
                                      {"kind": 3, "sequence": 7})
        client.poll(0)
        records = self.records(sock)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["type"], C.BMCU_FRAME)
        self.assertEqual(records[0]["sequence"], sequence)
        payload = records[0]["payload"]
        self.assertEqual(struct.unpack_from(">QH", payload, 0),
                         (123456, len(wire)))
        self.assertEqual(payload[10:], wire)
        self.assertEqual(outbox.queue_depth, 0)
        self.assertEqual(client.records_sent, 1)

    def test_backlog_packs_into_bounded_datagrams_in_sequence(self):
        outbox = BMB1Outbox(self.BOOT, durable_slots=128)
        client, sock, _ = self.make(outbox, per_loop=64)
        wire = b"\x3d\xc3\x03\x07\x10\x00" + bytes(40)
        for n in range(100):
            outbox.enqueue_raw(0, n, wire, {"kind": 3, "sequence": n})
        client.poll(0)
        sizes = [len(data) for data, _ in sock.datagrams]
        self.assertTrue(all(size <= 1200 for size in sizes), sizes)
        records = self.records(sock)
        self.assertEqual([r["sequence"] for r in records],
                         list(range(1, 101)))
        self.assertGreater(len(sock.datagrams), 2)
        self.assertEqual(outbox.queue_depth, 0)

    def test_refused_send_retries_the_same_datagram_without_loss(self):
        client, sock, outbox = self.make()
        client.poll(0)                       # heartbeat out
        for n in range(3):
            outbox.enqueue_payload(C.LINK_STATE, C.FLAG_CRITICAL, 0, n,
                                   link_state_payload(C.LINK_ONLINE))
        sock.refuse = [11]                   # EAGAIN once
        client.poll(1)
        self.assertEqual(self.records(sock), [])
        # Already out of the outbox, held by the client instead.
        self.assertEqual(outbox.queue_depth, 0)
        self.assertIsNotNone(client.pending)
        client.poll(2)
        self.assertEqual([r["sequence"] for r in self.records(sock)],
                         [1, 2, 3])
        self.assertIsNone(client.pending)

    def test_hard_send_error_backs_off_and_keeps_the_datagram(self):
        client, sock, outbox = self.make()
        client.poll(0)
        outbox.enqueue_payload(C.LINK_STATE, C.FLAG_CRITICAL, 0, 1,
                               link_state_payload(C.LINK_ONLINE))
        sock.refuse = [113]                  # EHOSTUNREACH
        client.poll(10)
        self.assertEqual(client.state, stream.SEND_ERROR)
        self.assertEqual(client.send_errors, 1)
        client.poll(10 + stream.ERROR_BACKOFF_MS - 1)
        self.assertEqual(self.records(sock), [])
        client.poll(10 + stream.ERROR_BACKOFF_MS)
        self.assertEqual([r["sequence"] for r in self.records(sock)], [1])
        self.assertEqual(client.state, stream.ONLINE)

    def test_heartbeat_interval_and_wifi_return(self):
        client, sock, _ = self.make(heartbeat_ms=5000)
        client.poll(0)
        client.poll(4999)
        beats = lambda: sum(1 for data, _ in sock.datagrams
                            if parse_datagram(data)[1][0]["type"] ==
                            C.HEARTBEAT)
        self.assertEqual(beats(), 1)
        client.poll(5000)
        self.assertEqual(beats(), 2)
        client.poll(5100, wifi_online=False)
        self.assertEqual(client.state, stream.WIFI_WAIT)
        client.poll(5200, wifi_online=True)
        self.assertEqual(beats(), 3, "a heartbeat is owed when Wi-Fi returns")

    def test_outage_overflow_arrives_as_one_exact_drop_marker(self):
        outbox = BMB1Outbox(self.BOOT, durable_slots=8)
        client, sock, _ = self.make(outbox, per_loop=16)
        for n in range(20):                  # Wi-Fi down: nothing drains
            outbox.enqueue_payload(C.LINK_STATE, C.FLAG_CRITICAL, 0, n,
                                   link_state_payload(C.LINK_ONLINE))
            client.poll(n, wifi_online=False)
        client.poll(100)
        client.poll(101)
        records = self.records(sock)
        sequences = [r["sequence"] for r in records]
        self.assertEqual(sequences[:7], list(range(1, 8)))
        drops = [r for r in records if r["type"] == C.TRANSPORT_DROP]
        self.assertEqual(len(drops), 1)
        first, last, count = struct.unpack_from(">QQI", drops[0]["payload"],
                                                8)
        self.assertEqual((drops[0]["sequence"], first, last, count),
                         (8, 8, 20, 13))
        # The receiver sees no gap: 1..7, then the marker owning 8..20.
        self.assertEqual(outbox.next_sequence, 21)

    def test_no_endpoint_is_disabled_and_sends_nothing(self):
        outbox = BMB1Outbox(self.BOOT, durable_slots=4)
        sock = FakeUDPSocket()
        client = stream.BMS2UDPClient(
            outbox, "", 0, b"pico-e2e", b"test", ((0, b"bmcu-a"),),
            socket_factory=lambda: sock, resolver=lambda h, p: (h, p))
        client.poll(0)
        self.assertEqual(client.state, stream.DISABLED)
        self.assertEqual(sock.datagrams, [])

    def test_endpoint_update_resends_identity(self):
        client, sock, _ = self.make()
        client.poll(0)
        client.set_endpoint("other", 9000)
        client.poll(1)
        self.assertEqual(sock.datagrams[-1][1], ("other", 9000))
        self.assertEqual(parse_datagram(sock.datagrams[-1][0])[1][0]["type"],
                         C.HEARTBEAT)

    def test_rejection_causes_are_counted_apart(self):
        # Every rejection becomes the same RAM_QUEUE_FULL drop marker, which
        # says a record was lost and nothing about why.
        outbox = BMB1Outbox(self.BOOT, durable_slots=1)
        for sequence in range(1, 5):
            outbox.enqueue_payload(
                C.LINK_STATE, C.FLAG_CRITICAL, 0, sequence,
                link_state_payload(C.LINK_ONLINE))
        self.assertGreater(outbox.reject_durable_full_count, 0)
        self.assertEqual(outbox.reject_oversize_count, 0)
        self.assertEqual(outbox.reject_large_full_count, 0)

    def test_durable_depth_high_water_is_tracked_apart_from_queue_depth(self):
        outbox = BMB1Outbox(self.BOOT, durable_slots=4)
        for sequence in range(1, 4):
            outbox.enqueue_payload(
                C.LINK_STATE, C.FLAG_CRITICAL, 0, sequence,
                link_state_payload(C.LINK_ONLINE))
        outbox.queue_depth
        peak = outbox.durable_depth_max
        self.assertGreater(peak, 0)
        outbox.durable.release_through(outbox.next_sequence)
        outbox.queue_depth
        self.assertEqual(outbox.durable_depth_max, peak)


if __name__ == "__main__":
    unittest.main()
