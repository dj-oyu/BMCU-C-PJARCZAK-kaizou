import hashlib
import json
import os
import struct
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PICO = os.path.join(ROOT, "pico")
_added_pico_path = PICO not in sys.path
if _added_pico_path:
    sys.path.insert(0, PICO)

import bmcu_binary as binary
import bmcu_binary_constants as C
from byte_ring import ByteRing, RecordTooLarge, RingFull
if _added_pico_path:
    sys.path.remove(PICO)


class BinaryCodecTests(unittest.TestCase):
    def setUp(self):
        self.out = bytearray(C.MAX_MESSAGE_SIZE)

    def parse(self, data, chunks=(1, 2, 7, 31)):
        parser = binary.StreamParser(bytearray(C.MAX_MESSAGE_SIZE * 2))
        pos = 0
        index = 0
        result = None
        while pos < len(data):
            count = min(chunks[index % len(chunks)], len(data) - pos)
            parser.feed(memoryview(data)[pos:pos + count])
            pos += count
            index += 1
            item = parser.next_message()
            if item is not None:
                result = item
        return result

    def test_header_and_partial_bmcu_frame(self):
        size = binary.write_bmcu_frame(
            self.out, 0, C.FLAG_JOURNALED, 7, 0x0102030405060708, 0,
            123456789,
            bytes.fromhex(
                "a55a830202001b000102030405060708090a0b0c0d0e0f"
                "101112131415161718191a929c"
            ))
        message = self.parse(memoryview(self.out)[:size])
        self.assertEqual(message.message_type, C.BMCU_FRAME)
        self.assertEqual(message.transport_sequence, 7)
        self.assertEqual(message.link_index, 0)
        received, wire_size = struct.unpack_from(">QH", message.payload, 0)
        self.assertEqual((received, wire_size), (123456789, 36))
        self.assertEqual(bytes(message.payload[10:2 + 8 + wire_size])[:2],
                         b"\xa5\x5a")

    def test_multiple_messages_in_one_feed(self):
        first = binary.write_ping(self.out, 0, C.PING, 3, 11)
        second = binary.write_ping(self.out, first, C.PONG, 3, 11)
        parser = binary.StreamParser(bytearray(C.MAX_MESSAGE_SIZE * 2))
        parser.feed(memoryview(self.out)[:first + second])
        self.assertEqual(parser.next_message().message_type, C.PING)
        pong = parser.next_message()
        self.assertEqual(binary.parse_pong(pong), 11)
        self.assertIsNone(parser.next_message())

    def test_ack_global_watermark(self):
        payload = struct.pack(">QBBHQ", 99, C.GLOBAL_SCOPE, 1, 0, 42)
        payload += struct.pack(">QB", 41, C.REJECT_MALFORMED)
        size = binary.write_message(self.out, 0, C.ACK, 0, 0, 99,
                                    C.GLOBAL_SCOPE, payload)
        message = self.parse(memoryview(self.out)[:size])
        boot, watermark, count, rejects = binary.parse_ack(message)
        self.assertEqual((boot, watermark, count), (99, 42, 1))
        self.assertEqual(bytes(rejects), struct.pack(">QB", 41, 1))

    def test_control_and_limits(self):
        arguments = b"\x01\x02"
        payload = struct.pack(">QQIBB", 5, 1000, 500, C.CONTROL_SOFT_RESET,
                              len(arguments)) + arguments + b"H" * 32
        size = binary.write_message(self.out, 0, C.CONTROL, 0, 0, 9,
                                    C.GLOBAL_SCOPE, payload)
        control = binary.parse_control(self.parse(memoryview(self.out)[:size]))
        self.assertEqual(control[:4], (5, 1000, 500, C.CONTROL_SOFT_RESET))
        self.assertEqual(bytes(control[4]), arguments)
        self.assertEqual(bytes(control[5]), b"H" * 32)

    def test_maximum_log(self):
        size = binary.write_log(
            self.out, 0, C.FLAG_CRITICAL, 9, 3, 17, 123456, C.LOG_CRITICAL,
            b"C" * 40, b"M" * 320, b"D" * 512)
        self.assertEqual(size, C.HEADER_SIZE + 22 + 40 + 320 + 512)
        with self.assertRaises(binary.CodecError):
            binary.write_log(self.out, 0, 0, 1, 1, 1, 1, C.LOG_ERROR,
                             b"C" * 41, b"", b"")

    def test_maximum_diagnostic_and_rejected_oversize(self):
        tlv = bytearray(C.MAX_PAYLOAD_SIZE)
        used = binary.write_tlv(tlv, 0, 1, C.VALUE_BYTES,
                                b"x" * (C.MAX_PAYLOAD_SIZE - 4))
        size = binary.write_diagnostic(self.out, 0, 0, 1, 2,
                                       memoryview(tlv)[:used])
        self.assertEqual(size, C.MAX_MESSAGE_SIZE)
        with self.assertRaises(binary.CodecError):
            binary.write_message(self.out, 0, C.PICO_DIAGNOSTIC, 0, 1, 2,
                                 C.GLOBAL_SCOPE,
                                 b"x" * (C.MAX_PAYLOAD_SIZE + 1))

    def test_parser_rejects_oversize_before_payload(self):
        binary.write_header(self.out, 0, C.PING, 0, 0, 0, 0, C.GLOBAL_SCOPE)
        struct.pack_into(">I", self.out, 8, C.MAX_PAYLOAD_SIZE + 1)
        parser = binary.StreamParser(bytearray(C.MAX_MESSAGE_SIZE))
        parser.feed(memoryview(self.out)[:C.HEADER_SIZE])
        with self.assertRaises(binary.CodecError):
            parser.next_message()

    def test_control_result_and_protocol_error_bounds(self):
        size = binary.write_control_result(
            self.out, 0, 0, 0, 4, C.GLOBAL_SCOPE, 7, 0, b"ok",
            b"H" * 32)
        self.assertEqual(size, C.HEADER_SIZE + 46)
        size = binary.write_protocol_error(self.out, 0, 4, 2, b"bad")
        self.assertEqual(size, C.HEADER_SIZE + 7)


class ByteRingTests(unittest.TestCase):
    def test_wraparound_and_watermark(self):
        ring = ByteRing(bytearray(3 * 8), 8)
        ring.append(1, b"one")
        ring.append(2, b"two")
        ring.append(3, b"three")
        self.assertEqual(ring.release_through(2), 2)
        ring.append(4, b"four")
        ring.append(5, b"five")
        self.assertEqual([item[0] for item in ring.iter_records()], [3, 4, 5])

    def test_protected_and_unacked_are_not_evicted(self):
        ring = ByteRing(bytearray(2 * 8), 8)
        ring.append(5, b"event", protected=True)
        ring.append(6, b"normal")
        with self.assertRaises(RingFull):
            ring.append_evict_acked(7, b"new", 6)
        self.assertEqual([item[0] for item in ring.iter_records()], [5, 6])

    def test_acked_normal_head_can_be_evicted(self):
        ring = ByteRing(bytearray(2 * 8), 8)
        ring.append(5, b"old")
        ring.append(6, b"live")
        ring.append_evict_acked(7, b"new", 5)
        self.assertEqual([item[0] for item in ring.iter_records()], [6, 7])

    def test_status_replacement(self):
        ring = ByteRing(bytearray(2 * 8), 8)
        self.assertFalse(ring.append(1, b"old", replace_key=0))
        self.assertTrue(ring.append(2, b"new", replace_key=0))
        self.assertEqual(len(ring), 1)
        sequence, data, _, key = ring.peek()
        self.assertEqual((sequence, bytes(data), key), (2, b"new", 0))
        self.assertEqual(ring.take_dropped_range(), (0, 0, 0))

    def test_record_size_bound(self):
        ring = ByteRing(bytearray(8), 8)
        with self.assertRaises(RecordTooLarge):
            ring.append(1, b"x" * 9)

    def test_memoryview_supports_offset_partial_send(self):
        ring = ByteRing(bytearray(16), 16)
        ring.append(1, b"0123456789")
        data = ring.peek()[1]
        self.assertEqual(bytes(data[4:]), b"456789")


class FixtureTests(unittest.TestCase):
    def test_registry_matches_wire_constants(self):
        with open(os.path.join(ROOT, "docs", "bmcu_binary_registry.json"),
                  encoding="utf-8") as handle:
            registry = json.load(handle)
        self.assertEqual(registry["message_types"]["BMCU_FRAME"], C.BMCU_FRAME)
        self.assertEqual(registry["flags"]["JOURNALED"], C.FLAG_JOURNALED)
        self.assertEqual(registry["scope"]["GLOBAL"], C.GLOBAL_SCOPE)
        self.assertEqual(registry["diagnostic_tags"]["UART1_BACKLOG"],
                         C.DIAG_UART1_BACKLOG)
        self.assertEqual(registry["control_commands"]["SOFT_RESET"],
                         C.CONTROL_SOFT_RESET)

    def test_manifest_hashes(self):
        fixture_dir = os.path.join(ROOT, "tests", "fixtures", "bmcu_binary")
        with open(os.path.join(fixture_dir, "manifest.json"),
                  encoding="utf-8") as handle:
            manifest = json.load(handle)
        self.assertEqual(len(manifest["files"]), 18)
        for name, metadata in manifest["files"].items():
            with open(os.path.join(fixture_dir, name), "rb") as handle:
                raw = handle.read()
                digest = hashlib.sha256(raw).hexdigest()
            self.assertEqual(len(raw), metadata["bytes"])
            self.assertEqual(digest, metadata["sha256"])

    def load_messages(self, name):
        fixture_dir = os.path.join(ROOT, "tests", "fixtures", "bmcu_binary")
        with open(os.path.join(fixture_dir, name), "rb") as handle:
            raw = handle.read()
        parser = binary.StreamParser(bytearray(C.MAX_MESSAGE_SIZE * 2))
        parser.feed(raw)
        messages = []
        while True:
            message = parser.next_message()
            if message is None:
                break
            messages.append(message)
        return raw, messages, parser

    def test_supported_canonical_fixtures_parse_semantically(self):
        _, messages, _ = self.load_messages("server_challenge.bin")
        self.assertEqual(bytes(binary.parse_challenge(messages[0])),
                         bytes(range(32, 64)))
        _, messages, _ = self.load_messages("ack.bin")
        self.assertEqual(binary.parse_ack(messages[0])[:3],
                         (0x0102030405060708, 42, 0))
        _, messages, _ = self.load_messages("ack_reject.bin")
        self.assertEqual(binary.parse_ack(messages[0])[:3],
                         (0x0102030405060708, 41, 1))
        _, messages, _ = self.load_messages("link_state.bin")
        self.assertEqual(binary.parse_link_state(messages[0]),
                         (123456, C.LINK_ONLINE, 0))
        _, messages, _ = self.load_messages("transport_drop.bin")
        self.assertEqual(binary.parse_transport_drop(messages[0]),
                         (123500, 3, 6, 4, C.DROP_RAM_QUEUE_FULL))
        for name in ("bmcu_status.bin", "bmcu_event.bin",
                     "bmcu_full_status.bin", "bmcu_unknown.bin"):
            _, messages, _ = self.load_messages(name)
            received, wire = binary.parse_bmcu_frame(messages[0])
            self.assertTrue(received >= 100001)
            self.assertEqual(bytes(wire[:2]), b"\xa5\x5a")
        _, messages, _ = self.load_messages("control.bin")
        self.assertEqual(binary.parse_control(messages[0])[:4],
                         (77, 500000, 5000, C.CONTROL_SOFT_RESET))
        _, messages, _ = self.load_messages("control_result.bin")
        result = binary.parse_control_result(messages[0])
        self.assertEqual((result[0], result[1], bytes(result[2])),
                         (77, C.RESULT_OK, b"accepted"))
        _, messages, _ = self.load_messages("diagnostic_unknown_tags.bin")
        self.assertEqual([item[0] for item in
                          binary.parse_tlvs(messages[0].payload)], [240, 241])
        for name in ("pico_log_utf8.bin", "pico_log_max.bin",
                     "recovered_replay.bin"):
            _, messages, _ = self.load_messages(name)
            self.assertEqual(len(binary.parse_log(messages[0])), 6)
        _, messages, _ = self.load_messages("concatenated.bin")
        self.assertEqual([item.message_type for item in messages],
                         [C.LINK_STATE, C.ACK])

    def test_hello_and_truncated_fixture(self):
        raw, messages, _ = self.load_messages("hello.bin")
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].message_type, C.HELLO)
        truncated, messages, parser = self.load_messages("truncated_header.bin")
        self.assertEqual(len(truncated), 17)
        self.assertEqual(messages, [])
        self.assertEqual(parser.buffered, 17)

    def test_every_supported_fixture_matches_expected_type(self):
        expected = {
            "ack.bin": C.ACK, "ack_reject.bin": C.ACK,
            "bmcu_event.bin": C.BMCU_FRAME,
            "bmcu_full_status.bin": C.BMCU_FRAME,
            "bmcu_status.bin": C.BMCU_FRAME,
            "bmcu_unknown.bin": C.BMCU_FRAME,
            "control.bin": C.CONTROL,
            "control_result.bin": C.CONTROL_RESULT,
            "diagnostic_unknown_tags.bin": C.PICO_DIAGNOSTIC,
            "hello.bin": C.HELLO, "link_state.bin": C.LINK_STATE,
            "pico_log_max.bin": C.PICO_LOG,
            "pico_log_utf8.bin": C.PICO_LOG,
            "recovered_replay.bin": C.PICO_LOG,
            "server_challenge.bin": C.SERVER_CHALLENGE,
            "transport_drop.bin": C.TRANSPORT_DROP,
        }
        for name, message_type in expected.items():
            _, messages, _ = self.load_messages(name)
            self.assertEqual(messages[0].message_type, message_type)


if __name__ == "__main__":
    unittest.main()
