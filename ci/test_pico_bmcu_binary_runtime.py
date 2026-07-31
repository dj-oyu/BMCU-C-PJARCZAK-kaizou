import importlib.util
import io
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PICO = os.path.join(ROOT, "pico")
added = PICO not in sys.path
if added:
    sys.path.insert(0, PICO)

import bambuddy_binary_tcp as tcp
import bmcu_binary as binary
import bmcu_binary_constants as C
import bmcu_journal as journal
from bmcu_binary_outbox import BMB1Outbox
from byte_ring import RingFull

spec = importlib.util.spec_from_file_location(
    "binary_runtime_link", os.path.join(PICO, "bmcu_link.py"))
link = importlib.util.module_from_spec(spec)
spec.loader.exec_module(link)
if added:
    sys.path.remove(PICO)


class FakeUART:
    def __init__(self, data=b""):
        self.rx = bytearray(data)
        self.writes = []

    def any(self):
        return len(self.rx)

    def read(self, count):
        result = bytes(self.rx[:count])
        del self.rx[:count]
        return result

    def write(self, data):
        self.writes.append(bytes(data))


class RawAndSchedulingTests(unittest.TestCase):
    def test_validated_callback_preserves_exact_wire(self):
        wire = link.encode_frame(link.STATUS, 3, bytes(range(27)))
        accepted = []
        monitor = link.BMCUMonitor(
            FakeUART(wire), link_id="a", link_index=1,
            on_valid_frame=lambda *args: accepted.append(args))
        monitor.poll_uart(25)
        self.assertEqual(len(accepted), 1)
        self.assertEqual(accepted[0][0:2], (1, 25000))
        self.assertEqual(bytes(accepted[0][2]), wire)
        self.assertEqual(accepted[0][3], link.STATUS)

    def test_invalid_crc_never_reaches_callback(self):
        wire = bytearray(link.encode_frame(link.STATUS, 3, bytes(range(27))))
        wire[-1] ^= 1
        accepted = []
        monitor = link.BMCUMonitor(
            FakeUART(wire), on_valid_frame=lambda *args: accepted.append(args))
        monitor.poll_uart(25)
        self.assertEqual(accepted, [])
        self.assertEqual(monitor.decoder.crc_errors, 1)

    def test_round_robin_is_bounded_and_fair(self):
        one = FakeUART(link.encode_frame(link.STATUS, 1, bytes(27)) * 8)
        two = FakeUART(link.encode_frame(link.EVENT, 2, bytes(16)) * 8)
        accepted = [0, 0]
        monitors = [
            link.BMCUMonitor(one, link_index=0,
                             on_valid_frame=lambda *args: accepted.__setitem__(
                                 0, accepted[0] + 1)),
            link.BMCUMonitor(two, link_index=1,
                             on_valid_frame=lambda *args: accepted.__setitem__(
                                 1, accepted[1] + 1)),
        ]
        next_index, drained = link.drain_monitors(
            monitors, 100, byte_budget=128, chunk_size=32)
        self.assertEqual(drained, 128)
        self.assertEqual(next_index, 0)
        self.assertGreater(accepted[0], 0)
        self.assertGreater(accepted[1], 0)
        self.assertTrue(one.any())
        self.assertTrue(two.any())

    def test_outbox_coalesces_status_before_allocating_sequence(self):
        outbox = BMB1Outbox(99, link_count=2, durable_slots=4)
        wire1 = link.encode_frame(link.STATUS, 1, bytes(27))
        wire2 = link.encode_frame(link.STATUS, 2, bytes(range(27)))
        meta = {"kind": link.STATUS, "sequence": 1}
        outbox.enqueue_raw(0, 1000, wire1, meta)
        outbox.enqueue_raw(0, 2000, wire2, meta)
        self.assertEqual(outbox.next_sequence, 1)
        self.assertEqual(outbox.status_replacements, 1)
        sequence, message, _, _ = outbox.peek()
        self.assertEqual(sequence, 1)
        parser = binary.StreamParser(bytearray(C.MAX_MESSAGE_SIZE))
        parser.feed(message)
        received, wire = binary.parse_bmcu_frame(parser.next_message())
        self.assertEqual(received, 2000)
        self.assertEqual(bytes(wire), wire2)

    def test_event_is_protected_and_sequenced_immediately(self):
        outbox = BMB1Outbox(99, durable_slots=1)
        event = link.encode_frame(link.EVENT, 1, bytes(16))
        sequence = outbox.enqueue_raw(
            0, 1000, event, {"kind": link.EVENT, "sequence": 1})
        self.assertEqual(sequence, 1)
        self.assertTrue(outbox.peek()[2])
        self.assertEqual(outbox.enqueue_raw(
            0, 1001, event, {"kind": link.EVENT, "sequence": 2}), 0)
        self.assertEqual(outbox.forced_drop_count, 1)
        self.assertEqual(outbox.acknowledge(99, 1), 1)
        sequence, message, protected, _ = outbox.peek()
        self.assertEqual(sequence, 2)
        self.assertTrue(protected)
        parser = binary.StreamParser(bytearray(C.MAX_MESSAGE_SIZE))
        parser.feed(message)
        drop = parser.next_message()
        self.assertEqual(drop.message_type, C.TRANSPORT_DROP)
        self.assertEqual(binary.parse_transport_drop(drop)[1:4], (2, 2, 1))

    def test_consecutive_drops_form_exact_range_and_ack_can_advance(self):
        outbox = BMB1Outbox(99, durable_slots=1)
        event = link.encode_frame(link.EVENT, 1, bytes(16))
        self.assertEqual(outbox.enqueue_raw(0, 1000, event, link.EVENT), 1)
        self.assertEqual(outbox.enqueue_raw(0, 1001, event, link.EVENT), 0)
        self.assertEqual(outbox.enqueue_raw(0, 1002, event, link.EVENT), 0)
        self.assertEqual(outbox.next_sequence, 4)
        self.assertEqual(outbox.acknowledge(99, 1), 1)

        sequence, message, _, _ = outbox.peek()
        self.assertEqual(sequence, 2)
        parser = binary.StreamParser(bytearray(C.MAX_MESSAGE_SIZE))
        parser.feed(message)
        drop = parser.next_message()
        self.assertEqual(binary.parse_transport_drop(drop)[1:4], (2, 3, 2))

        self.assertEqual(outbox.acknowledge(99, 3), 1)
        self.assertEqual(outbox.enqueue_raw(0, 1003, event, link.EVENT), 4)
        self.assertEqual(outbox.acknowledge(99, 4), 1)
        self.assertIsNone(outbox.peek())

    def test_recovered_boot_ranges_are_reported_current_first(self):
        outbox = BMB1Outbox(99, durable_slots=4)
        payload = b"x"
        outbox.restore(7, C.PICO_LOG, C.FLAG_JOURNALED,
                       C.GLOBAL_SCOPE, 3, 1, payload)
        self.assertEqual(outbox.available_boot_ranges(),
                         ((99, 0, 0), (7, 3, 3)))


class JournalTests(unittest.TestCase):
    def record(self):
        storage = bytearray(journal.RECORD_MAX_SIZE)
        size = journal.write_record(
            storage, C.BMCU_FRAME, C.FLAG_CRITICAL, 1, 7, 1234, b"payload")
        return bytes(storage[:size])

    def test_segment_and_record_roundtrip(self):
        header = bytearray(journal.SEGMENT_HEADER_SIZE)
        journal.write_segment_header(header, 99, 2, 1000)
        self.assertEqual(journal.parse_segment_header(header), (99, 2, 1000))
        parsed = journal.parse_record(self.record())
        self.assertEqual(parsed[0:6], (
            len(self.record()), C.BMCU_FRAME, C.FLAG_CRITICAL, 1, 7, 1234))
        self.assertEqual(bytes(parsed[6]), b"payload")

    def test_recovery_stops_at_every_torn_tail(self):
        header = bytearray(journal.SEGMENT_HEADER_SIZE)
        journal.write_segment_header(header, 99, 2, 1000)
        record = self.record()
        for cut in range(len(record)):
            stream = io.BytesIO(bytes(header) + record + record[:cut])
            recovered = list(journal.recover_records(
                stream, bytearray(journal.RECORD_MAX_SIZE)))
            self.assertEqual(len(recovered), 1)

    def test_corrupt_tail_preserves_prior_record(self):
        header = bytearray(journal.SEGMENT_HEADER_SIZE)
        journal.write_segment_header(header, 99, 2, 1000)
        damaged = bytearray(self.record())
        damaged[-1] ^= 1
        stream = io.BytesIO(bytes(header) + self.record() + damaged)
        recovered = list(journal.recover_records(
            stream, bytearray(journal.RECORD_MAX_SIZE)))
        self.assertEqual(len(recovered), 1)

    def test_stager_does_not_write_until_flush(self):
        stager = journal.JournalStager(
            bytearray(journal.RECORD_MAX_SIZE * 2))
        target = io.BytesIO()
        stager.stage(C.BMCU_FRAME, 0, 0, 1, 5, b"x")
        self.assertEqual(target.getvalue(), b"")
        self.assertGreater(stager.flush_one(target), 0)
        self.assertTrue(target.getvalue())

    def test_stager_reserves_capacity_for_critical_event(self):
        stager = journal.JournalStager(
            bytearray(journal.RECORD_MAX_SIZE * 2))
        stager.stage(C.PICO_LOG, 0, C.GLOBAL_SCOPE, 1, 5, b"log")
        with self.assertRaises(journal.RingFull):
            stager.stage(C.PICO_LOG, 0, C.GLOBAL_SCOPE, 2, 6, b"log")
        stager.stage(
            C.BMCU_FRAME, C.FLAG_CRITICAL, 0, 2, 6, b"event")

    def test_directory_restart_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            managed = journal.BMJ1Journal(directory, 99, 1000)
            managed.stage(C.BMCU_FRAME, 0, 0, 1, 5, b"x")
            managed.flush_one()
            managed.file.close()
            recovered = []
            self.assertEqual(journal.recover_directory(
                directory, lambda *args: recovered.append(args)), 1)
            self.assertEqual((recovered[0][0], recovered[0][4]),
                             (99, 1))

    def test_checkpoint_filters_acknowledged_restart_records(self):
        with tempfile.TemporaryDirectory() as directory:
            managed = journal.BMJ1Journal(directory, 99, 1000)
            managed.stage(C.BMCU_FRAME, 0, 0, 1, 5, b"one")
            managed.stage(C.BMCU_FRAME, 0, 0, 2, 6, b"two")
            managed.flush_one()
            managed.flush_one()
            managed.record_ack(99, 1)
            managed.flush_one()
            managed.file.close()
            recovered = []
            self.assertEqual(journal.recover_directory(
                directory, lambda *args: recovered.append(args)), 1)
            self.assertEqual(recovered[0][4], 2)

    def test_corrupt_checkpoint_is_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "ack.bma")
            with open(path, "wb") as target:
                target.write(b"BMA1\x01\x01\0\0broken")
            self.assertEqual(journal.load_watermarks(directory), {})

    def test_cursor_pages_more_than_ram_capacity_until_all_acked(self):
        with tempfile.TemporaryDirectory() as directory:
            managed = journal.BMJ1Journal(
                directory, 99, 1000, staging_slots=1)
            for sequence in range(1, 201):
                managed.stage(
                    C.BMCU_FRAME, C.FLAG_CRITICAL, 0, sequence,
                    sequence, b"x")
                managed.flush_one()
            managed.file.close()
            cursor = journal.JournalReplayCursor(directory)
            outbox = BMB1Outbox(
                100, durable_slots=4, large_slots=1)
            outbox.historical_ranges = cursor.available_ranges
            outbox.replay_pager = lambda: cursor.page_into(outbox, 4)
            seen = []
            while True:
                current = outbox.peek()
                if current is None:
                    break
                sequence, message, _, _ = current
                boot = int.from_bytes(bytes(message[20:28]), "big")
                seen.append(sequence)
                outbox.acknowledge(boot, sequence)
            self.assertEqual(seen, list(range(1, 201)))
            self.assertIn((99, 1, 200), outbox.historical_ranges)


class FakeSocket:
    def __init__(self, receive=b"", send_limit=13):
        self.receive = bytearray(receive)
        self.sent = bytearray()
        self.send_limit = send_limit

    def recv_into(self, target):
        if not self.receive:
            raise OSError(11)
        count = min(len(target), len(self.receive))
        target[:count] = self.receive[:count]
        del self.receive[:count]
        return count

    def send(self, data):
        count = min(len(data), self.send_limit)
        self.sent.extend(data[:count])
        return count

    def close(self):
        pass


class TCPClientTests(unittest.TestCase):
    def frame(self, message_type, payload, boot=0, link_index=C.GLOBAL_SCOPE):
        out = bytearray(C.MAX_MESSAGE_SIZE)
        size = binary.write_message(
            out, 0, message_type, 0, 0, boot, link_index, payload)
        return bytes(out[:size])

    def test_auth_partial_send_online_delivery_and_ack(self):
        boot = 0x0102030405060708
        challenge = self.frame(C.SERVER_CHALLENGE, bytes(range(32)), 0, 0)
        accepted = self.frame(
            C.HELLO_ACCEPTED,
            (0).to_bytes(8, "big") + (5000).to_bytes(4, "big") +
            (15000).to_bytes(4, "big"), boot)
        fake = FakeSocket(challenge, send_limit=7)
        outbox = BMB1Outbox(boot, durable_slots=4)
        client = tcp.BMB1TCPClient(
            outbox, "host", 1234, b"device", bytes(range(32)), b"1.0",
            ((0, b"a"),))
        client.attach_connected_socket(fake)
        for now in range(100):
            client.poll(now)
            if client.state == tcp.ACCEPT_WAIT and not fake.receive:
                fake.receive.extend(accepted)
            if client.state == tcp.ONLINE:
                break
        self.assertEqual(client.state, tcp.ONLINE)
        event = link.encode_frame(link.EVENT, 1, bytes(16))
        outbox.enqueue_raw(0, 1000, event,
                           {"kind": link.EVENT, "sequence": 1})
        before = len(fake.sent)
        for now in range(100, 130):
            client.poll(now)
        self.assertGreater(len(fake.sent), before)
        ack_payload = (
            boot.to_bytes(8, "big") + bytes((C.GLOBAL_SCOPE, 0)) + b"\0\0" +
            (1).to_bytes(8, "big"))
        fake.receive.extend(self.frame(C.ACK, ack_payload, boot))
        client.poll(131)
        self.assertEqual(outbox.queue_depth, 0)

    def test_control_uses_session_relative_ttl(self):
        now = [20]
        boot = 99
        outbox = BMB1Outbox(boot, durable_slots=4)
        client = tcp.BMB1TCPClient(
            outbox, "host", 1, b"d", b"k" * 32, b"1", ((0, b"a"),),
            clock_ms=lambda: now[0], control_handler=lambda *_: b"ok")
        client.state = tcp.ONLINE
        client.session_epoch_ms = 0
        client.session_key = b"s" * 32
        unsigned = (
            (1).to_bytes(8, "big") + (1000).to_bytes(8, "big") +
            (5).to_bytes(4, "big") + bytes((C.CONTROL_SOFT_RESET, 0))
        )
        header = bytearray(C.HEADER_SIZE)
        binary.write_header(header, 0, C.CONTROL, 0,
                            len(unsigned) + 32, 0, boot, 0)
        mac = tcp.hmac_sha256(client.session_key, header, unsigned)
        raw = self.frame(C.CONTROL, unsigned + mac, boot, 0)
        parser = binary.StreamParser(bytearray(C.MAX_MESSAGE_SIZE))
        parser.feed(raw)
        client._handle_message(parser.next_message())
        result_parser = binary.StreamParser(bytearray(C.MAX_MESSAGE_SIZE))
        result_parser.feed(memoryview(
            client.control_buffer)[:client.control_pending_length])
        result = binary.parse_control_result(result_parser.next_message())
        self.assertEqual(result[1], C.RESULT_EXPIRED)
        self.assertEqual(outbox.queue_depth, 0)

    def test_delivery_waits_for_ack_before_retransmitting(self):
        now = [0]
        boot = 99
        fake = FakeSocket(send_limit=C.MAX_MESSAGE_SIZE)
        outbox = BMB1Outbox(boot, durable_slots=4)
        outbox.enqueue_link_state(0, 1, C.LINK_ONLINE, 0)
        client = tcp.BMB1TCPClient(
            outbox, "host", 1, b"d", b"k" * 32, b"1", ((0, b"a"),),
            clock_ms=lambda: now[0])
        client.sock = fake
        client.state = tcp.ONLINE
        client.ack_timeout_ms = 10
        client.poll(0)
        first_length = len(fake.sent)
        self.assertGreater(first_length, 0)
        for value in range(1, 10):
            now[0] = value
            client.poll(value)
        self.assertEqual(len(fake.sent), first_length)
        now[0] = 10
        client.poll(10)
        self.assertEqual(len(fake.sent), first_length * 2)

    def test_handshake_timeout_is_wrap_safe(self):
        modulus = 16384

        def ticks_add(value, delta):
            return (value + delta) % modulus

        def ticks_diff(left, right):
            return ((left - right + modulus // 2) % modulus) - modulus // 2

        now = [16000]
        client = tcp.BMB1TCPClient(
            BMB1Outbox(99), "host", 1, b"d", b"k" * 32, b"1",
            ((0, b"a"),), clock_ms=lambda: now[0],
            ticks_add=ticks_add, ticks_diff=ticks_diff)
        client.attach_connected_socket(FakeSocket())
        now[0] = ticks_add(16000, 4999)
        client.poll(now[0])
        self.assertEqual(client.state, tcp.CHALLENGE_WAIT)
        now[0] = ticks_add(16000, 5000)
        client.poll(now[0])
        self.assertEqual(client.state, tcp.BACKOFF)

    def test_device_key_update_reconnects_without_reboot(self):
        client = tcp.BMB1TCPClient(
            BMB1Outbox(99), "host", 1, b"d", b"k" * 32, b"1",
            ((0, b"a"),))
        client.sock = FakeSocket()
        client.state = tcp.ONLINE

        client.set_device_key(b"n" * 32, 123)

        self.assertEqual(client.device_key, b"n" * 32)
        self.assertEqual(client.state, tcp.BACKOFF)
        self.assertEqual(client.last_error, "device key updated")
        with self.assertRaises(ValueError):
            client.set_device_key(b"short", 124)

    def test_disconnect_discards_session_bound_control_result(self):
        client = tcp.BMB1TCPClient(
            BMB1Outbox(99), "host", 1, b"d", b"k" * 32, b"1",
            ((0, b"a"),))
        client.sock = FakeSocket()
        client.state = tcp.ONLINE
        client.control_pending_length = 42
        client._close(1, "test")
        self.assertEqual(client.control_pending_length, 0)
        self.assertEqual(client.state, tcp.BACKOFF)


if __name__ == "__main__":
    unittest.main()
