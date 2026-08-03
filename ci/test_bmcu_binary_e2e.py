"""Host-side end-to-end tests for the Pico BMB1 transport.

The peer deliberately behaves like the Bambuddy session boundary: it
authenticates HELLO, commits durable messages before ACKing them, advances a
single per-boot watermark (including exact DROP markers), and signs CONTROL
with the negotiated session key.  Socket reads and writes are fragmented so
these tests exercise the production incremental parsers and send state.
"""

import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PICO = os.path.join(ROOT, "pico")
sys.path.insert(0, PICO)

import bambuddy_binary_tcp as tcp
import bmcu_binary as binary
import bmcu_binary_constants as C
import bmcu_journal as journal
from bmcu_binary_outbox import BMB1Outbox


class FragmentedSocket:
    """Nonblocking in-memory socket with independently bounded I/O."""

    def __init__(self, receive_limit=5, send_limit=7):
        self.to_client = bytearray()
        self.from_client = bytearray()
        self.receive_limit = receive_limit
        self.send_limit = send_limit
        self.closed = False

    def recv_into(self, target):
        if not self.to_client:
            raise OSError(11)
        count = min(len(target), len(self.to_client), self.receive_limit)
        target[:count] = self.to_client[:count]
        del self.to_client[:count]
        return count

    def send(self, data):
        count = min(len(data), self.send_limit)
        self.from_client.extend(data[:count])
        return count

    def close(self):
        self.closed = True


class BambuddyPeer:
    """Small protocol oracle at the commit/ACK boundary."""

    def __init__(self, sock, key, boot, challenge=bytes(range(32))):
        self.sock = sock
        self.key = key
        self.boot = boot
        self.challenge = challenge
        self.parser = binary.StreamParser(bytearray(C.MAX_MESSAGE_SIZE * 4))
        self.session_key = None
        self.hello = None
        self.committed = {}
        self.watermark = {}
        self.losses = {}
        self.control_results = []
        self.ack_enabled = True
        self.received_counts = {}
        self._queue(C.SERVER_CHALLENGE, challenge, boot=0)

    def _frame(self, typ, payload, sequence=0, boot=None,
               link=C.GLOBAL_SCOPE):
        storage = bytearray(C.MAX_MESSAGE_SIZE)
        size = binary.write_message(
            storage, 0, typ, 0, sequence,
            self.boot if boot is None else boot, link, payload)
        return bytes(storage[:size])

    def _queue(self, typ, payload, sequence=0, boot=None,
               link=C.GLOBAL_SCOPE):
        self.sock.to_client.extend(
            self._frame(typ, payload, sequence, boot, link))

    def pump(self):
        if self.sock.from_client:
            data = bytes(self.sock.from_client)
            self.sock.from_client.clear()
            self.parser.feed(data)
        handled = 0
        while True:
            message = self.parser.next_message()
            if message is None:
                return handled
            handled += 1
            self._handle(message)

    def _handle(self, message):
        if message.message_type == C.HELLO:
            device, firmware, links, ranges, supplied = \
                binary.parse_hello(message)
            transcript = bytearray(512)
            size = tcp.hello_transcript(
                bytes(device), bytes(firmware),
                tuple((index, bytes(link)) for index, link in links),
                ranges, transcript)
            expected = tcp.hmac_sha256(
                self.key, b"BMB1-AUTH", self.challenge,
                self.boot.to_bytes(8, "big"), memoryview(transcript)[:size])
            if not tcp.BMB1TCPClient._same(expected, supplied):
                raise AssertionError("HELLO HMAC mismatch")
            self.hello = (bytes(device), bytes(firmware), links, ranges)
            self.session_key = tcp.hmac_sha256(
                self.key, b"BMB1-SESSION", self.challenge,
                self.boot.to_bytes(8, "big"))
            accepted = (
                self.watermark.get(self.boot, 0).to_bytes(8, "big") +
                (1000).to_bytes(4, "big") + (15000).to_bytes(4, "big"))
            self._queue(C.HELLO_ACCEPTED, accepted)
            return
        if message.message_type == C.CONTROL_RESULT:
            result = binary.parse_control_result(message)
            unsigned_length = len(message.payload) - 32
            expected = tcp.hmac_sha256(
                self.session_key, message.header,
                message.payload[:unsigned_length])
            if message.transport_sequence != 0 or not tcp.BMB1TCPClient._same(
                    expected, result[3]):
                raise AssertionError("invalid CONTROL_RESULT")
            self.control_results.append(result)
            return
        if not message.transport_sequence:
            return
        key = (message.pico_boot_id, message.transport_sequence)
        self.received_counts[key] = self.received_counts.get(key, 0) + 1
        # Idempotent DB commit equivalent.
        self.committed.setdefault(key, bytes(message.payload))
        if message.message_type == C.TRANSPORT_DROP:
            _, first, last, _, _ = binary.parse_transport_drop(message)
            self.losses.setdefault(message.pico_boot_id, []).append((first, last))
        self._advance(message.pico_boot_id)
        if self.ack_enabled:
            self.ack(message.pico_boot_id)

    def _advance(self, boot):
        watermark = self.watermark.get(boot, 0)
        while True:
            candidate = watermark + 1
            if (boot, candidate) in self.committed:
                watermark = candidate
                continue
            covering = next((
                last for first, last in self.losses.get(boot, ())
                if first <= candidate <= last
            ), None)
            if covering is None:
                break
            watermark = covering
        self.watermark[boot] = watermark

    def ack(self, boot):
        payload = (
            boot.to_bytes(8, "big") + bytes((C.GLOBAL_SCOPE, 0)) + b"\0\0" +
            self.watermark.get(boot, 0).to_bytes(8, "big"))
        self._queue(C.ACK, payload, boot=boot)

    def send_soft_reset(self, command_sequence=1, issued_at_us=0,
                        ttl_ms=5000, link=0):
        unsigned = (
            command_sequence.to_bytes(8, "big") +
            issued_at_us.to_bytes(8, "big") +
            ttl_ms.to_bytes(4, "big") +
            bytes((C.CONTROL_SOFT_RESET, 0)))
        header = bytearray(C.HEADER_SIZE)
        binary.write_header(
            header, 0, C.CONTROL, 0, len(unsigned) + 32, 0, self.boot, link)
        mac = tcp.hmac_sha256(self.session_key, header, unsigned)
        self.sock.to_client.extend(
            self._frame(C.CONTROL, unsigned + mac, boot=self.boot, link=link))


def drive(client, peer, start=0, limit=10000, done=None):
    """Run both cooperative endpoints until ``done`` or a fixed bound."""
    for now in range(start, start + limit):
        if hasattr(client, "_e2e_now"):
            client._e2e_now[0] = now
        client.poll(now)
        peer.pump()
        if done is not None and done():
            return now
    raise AssertionError("transport did not converge")


class BinaryTransportEndToEndTests(unittest.TestCase):
    KEY = bytes(range(32))
    BOOT = 0x1020304050607080

    def make_pair(self, outbox=None, reset_handler=None):
        sock = FragmentedSocket()
        outbox = outbox or BMB1Outbox(self.BOOT, durable_slots=8)
        now = [0]
        client = tcp.BMB1TCPClient(
            outbox, "host", 1234, b"pico-e2e", self.KEY, b"test",
            ((0, b"bmcu-a"),), clock_ms=lambda: now[0],
            control_handler=reset_handler)
        client._e2e_now = now
        peer = BambuddyPeer(sock, self.KEY, self.BOOT)
        client.attach_connected_socket(sock)
        return client, peer, outbox, now

    def test_authenticated_raw_delivery_commit_then_ack_with_partial_io(self):
        client, peer, outbox, now = self.make_pair()
        drive(client, peer, done=lambda: client.state == tcp.ONLINE)
        self.assertEqual(peer.hello[0], b"pico-e2e")
        # A valid alpha3 event frame, retained byte-for-byte in BMB1.
        wire = b"\x3d\xc3\x03\x07\x10\x00" + bytes(16)
        crc = binary._crc16(wire) if hasattr(binary, "_crc16") else None
        # The transport treats raw data as opaque; CRC validation occurred at
        # the BMCU monitor boundary before this callback.
        del crc
        sequence = outbox.enqueue_raw(
            0, 123456, wire, {"kind": 3, "sequence": 7})
        peer.ack_enabled = False
        end = drive(
            client, peer, start=100,
            done=lambda: (self.BOOT, sequence) in peer.committed)
        self.assertEqual(outbox.queue_depth, 1)
        # Commit alone is not delivery: only the subsequent ACK releases RAM.
        peer.ack_enabled = True
        peer.ack(self.BOOT)
        now[0] = end + 1
        drive(client, peer, start=end + 1,
              done=lambda: outbox.queue_depth == 0)
        payload = peer.committed[(self.BOOT, sequence)]
        self.assertEqual(bytes(payload[10:]), wire)

    def test_delayed_ack_keeps_exactly_one_inflight(self):
        client, peer, outbox, _ = self.make_pair()
        drive(client, peer, done=lambda: client.state == tcp.ONLINE)
        outbox.enqueue_link_state(0, 1, C.LINK_ONLINE, 0)
        outbox.enqueue_link_state(0, 2, C.LINK_OFFLINE, 1)
        peer.ack_enabled = False
        drive(client, peer, start=100, limit=20,
              done=lambda: (self.BOOT, 1) in peer.committed)
        deadline = client.inflight_sent_ms + client.ack_timeout_ms
        for now in range(120, deadline):
            client.poll(now)
            peer.pump()
        self.assertEqual(peer.received_counts[(self.BOOT, 1)], 1)
        self.assertNotIn((self.BOOT, 2), peer.committed)
        # At timeout only the same head is retransmitted, never a growing
        # in-flight window.
        for now in range(deadline, deadline + 20):
            client.poll(now)
            peer.pump()
            if peer.received_counts[(self.BOOT, 1)] == 2:
                break
        self.assertEqual(peer.received_counts[(self.BOOT, 1)], 2)
        self.assertNotIn((self.BOOT, 2), peer.committed)

    def test_restart_replays_more_than_128_journal_records(self):
        old_boot = 77
        with tempfile.TemporaryDirectory() as directory:
            managed = journal.BMJ1Journal(
                directory, old_boot, 1000, staging_slots=2)
            payload = (1).to_bytes(8, "big") + (1).to_bytes(2, "big") + b"x"
            for sequence in range(1, 161):
                managed.stage(
                    C.BMCU_FRAME, C.FLAG_CRITICAL, 0, sequence,
                    sequence, payload)
                managed.flush_one()
            managed.file.close()

            cursor = journal.JournalReplayCursor(directory)
            outbox = BMB1Outbox(
                self.BOOT, durable_slots=4, large_slots=1)
            outbox.historical_ranges = cursor.available_ranges
            outbox.replay_pager = lambda: cursor.page_into(outbox, 4)
            client, peer, _, _ = self.make_pair(outbox)
            drive(client, peer, done=lambda: client.state == tcp.ONLINE)
            drive(
                client, peer, start=100, limit=20000,
                done=lambda: peer.watermark.get(old_boot) == 160)
            self.assertEqual(
                sorted(sequence for boot, sequence in peer.committed
                       if boot == old_boot),
                list(range(1, 161)))
            self.assertLessEqual(outbox.queue_depth, 4)

    def test_rejection_causes_are_counted_apart(self):
        # Every rejection becomes the same RAM_QUEUE_FULL drop marker, which
        # says a record was lost and nothing about why. A saturated durable
        # ring and a record too large for any size class are different
        # problems with different fixes.
        outbox = BMB1Outbox(self.BOOT, durable_slots=1)
        self.assertEqual(outbox.reject_durable_full_count, 0)
        for sequence in range(1, 5):
            outbox.enqueue_payload(
                C.LINK_STATE, C.FLAG_CRITICAL, 0, sequence,
                b"\0" * 8 + bytes((C.LINK_ONLINE, 0, 0, 0)))
        self.assertGreater(outbox.reject_durable_full_count, 0)
        self.assertEqual(outbox.reject_oversize_count, 0)
        self.assertEqual(outbox.reject_large_full_count, 0)

    def test_durable_depth_high_water_is_tracked_apart_from_queue_depth(self):
        # queue_depth sums four rings of different sizes, so it cannot say
        # which one filled -- and durable is the only one whose saturation
        # drops records.
        outbox = BMB1Outbox(self.BOOT, durable_slots=4)
        for sequence in range(1, 4):
            outbox.enqueue_payload(
                C.LINK_STATE, C.FLAG_CRITICAL, 0, sequence,
                b"\0" * 8 + bytes((C.LINK_ONLINE, 0, 0, 0)))
        outbox.queue_depth
        peak = outbox.durable_depth_max
        self.assertGreater(peak, 0)
        outbox.durable.release_through(outbox.next_sequence)
        outbox.queue_depth
        self.assertEqual(outbox.durable_depth_max, peak)

    def test_exact_drop_marker_advances_watermark(self):
        client, peer, outbox, _ = self.make_pair()
        drive(client, peer, done=lambda: client.state == tcp.ONLINE)
        # One slot forces the second event to become the exact sequence-2
        # replacement marker after sequence 1 is ACKed.
        constrained = BMB1Outbox(self.BOOT, durable_slots=1)
        client.outbox = constrained
        constrained.enqueue_payload(
            C.LINK_STATE, C.FLAG_CRITICAL, 0, 1,
            b"\0" * 8 + bytes((C.LINK_ONLINE, 0, 0, 0)))
        constrained.enqueue_payload(
            C.LINK_STATE, C.FLAG_CRITICAL, 0, 2,
            b"\0" * 8 + bytes((C.LINK_OFFLINE, 1, 0, 0)))
        drive(client, peer, start=100, limit=5000,
              done=lambda: (
                  peer.watermark.get(self.BOOT) == 2 and
                  constrained.queue_depth == 0))
        drop_key = (self.BOOT, 2)
        self.assertIn(drop_key, peer.committed)
        self.assertEqual(peer.losses[self.BOOT], [(2, 2)])
        self.assertEqual(constrained.queue_depth, 0)

    def test_soft_reset_result_is_session_signed_and_sequence_zero(self):
        called = []

        def reset(link, command, arguments):
            called.append((link, command, bytes(arguments)))
            return b"reset accepted"

        client, peer, _, _ = self.make_pair(reset_handler=reset)
        drive(client, peer, done=lambda: client.state == tcp.ONLINE)
        peer.send_soft_reset()
        drive(client, peer, start=100,
              done=lambda: bool(peer.control_results))
        self.assertEqual(called, [(0, C.CONTROL_SOFT_RESET, b"")])
        self.assertEqual(peer.control_results[0][0:3],
                         (1, C.RESULT_OK, b"reset accepted"))


if __name__ == "__main__":
    unittest.main()
