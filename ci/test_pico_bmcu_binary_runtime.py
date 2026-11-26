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
        wire = link.encode_frame(link.STATUS, 3, bytes(range(31)))
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
        wire = bytearray(link.encode_frame(link.STATUS, 3, bytes(range(31))))
        wire[-1] ^= 1
        accepted = []
        monitor = link.BMCUMonitor(
            FakeUART(wire), on_valid_frame=lambda *args: accepted.append(args))
        monitor.poll_uart(25)
        self.assertEqual(accepted, [])
        self.assertEqual(monitor.decoder.crc_errors, 1)

    def test_round_robin_is_bounded_and_fair(self):
        one = FakeUART(link.encode_frame(link.STATUS, 1, bytes(31)) * 8)
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
        wire1 = link.encode_frame(link.STATUS, 1, bytes(31))
        wire2 = link.encode_frame(link.STATUS, 2, bytes(range(31)))
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

    def test_drop_marker_waits_for_a_quarter_of_the_ring(self):
        outbox = BMB1Outbox(99, durable_slots=16)
        event = link.encode_frame(link.EVENT, 1, bytes(16))
        # One slot is held back for the marker, so 15 records fit.
        for n in range(15):
            self.assertEqual(
                outbox.enqueue_raw(0, 1000 + n, event, link.EVENT), n + 1)
        self.assertEqual(outbox.enqueue_raw(0, 2000, event, link.EVENT), 0)

        # Steady overload: each ACK frees one slot and a record arrives.
        # A marker written at once would take every freed slot in turn.
        # The ring starts with its reserved slot free, so two ACKs leave 3.
        for acked in (1, 2):
            outbox.acknowledge(99, acked)
            outbox.peek()
            self.assertEqual(outbox.enqueue_raw(
                0, 2000 + acked, event, link.EVENT), 0)
        self.assertEqual(outbox.forced_drop_count, 3)

        # Four free slots (16 // 4): one marker covers the whole run.
        outbox.acknowledge(99, 3)
        outbox.peek()
        markers = []
        for sequence, message, _, _ in outbox.durable.iter_records():
            parser = binary.StreamParser(bytearray(C.MAX_MESSAGE_SIZE))
            parser.feed(message)
            decoded = parser.next_message()
            if decoded.message_type == C.TRANSPORT_DROP:
                markers.append(binary.parse_transport_drop(decoded)[1:4])
        self.assertEqual(markers, [(16, 18, 3)])
        self.assertEqual(outbox.forced_drop_count, 0)

        # The slots behind the marker go to records again.
        self.assertEqual(outbox.enqueue_raw(0, 3000, event, link.EVENT), 19)

    def test_drop_marker_has_a_reserved_slot_during_replay_saturation(self):
        outbox = BMB1Outbox(99, durable_slots=2)
        event = link.encode_frame(link.EVENT, 1, bytes(16))
        self.assertEqual(outbox.enqueue_raw(0, 1000, event, link.EVENT), 1)
        self.assertEqual(outbox.enqueue_raw(0, 1001, event, link.EVENT), 0)

        self.assertEqual(outbox.queue_depth, 1)
        outbox.peek()
        self.assertEqual(outbox.queue_depth, 2)
        self.assertEqual(outbox.forced_drop_count, 0)

        outbox.acknowledge(99, 1)
        sequence, message, protected, _ = outbox.peek()
        self.assertEqual(sequence, 2)
        self.assertTrue(protected)
        parser = binary.StreamParser(bytearray(C.MAX_MESSAGE_SIZE))
        parser.feed(message)
        drop = parser.next_message()
        self.assertEqual(drop.message_type, C.TRANSPORT_DROP)
        self.assertEqual(binary.parse_transport_drop(drop)[1:4], (2, 2, 1))

    def test_recovered_boot_ranges_are_reported_current_first(self):
        outbox = BMB1Outbox(99, durable_slots=4)
        payload = b"x"
        outbox.restore(7, C.PICO_LOG, C.FLAG_JOURNALED,
                       C.GLOBAL_SCOPE, 3, 1, payload)
        self.assertEqual(outbox.available_boot_ranges(),
                         ((99, 0, 0), (7, 3, 3)))

    def test_historical_replay_cannot_jump_a_live_ack_gap(self):
        outbox = BMB1Outbox(99, durable_slots=4, large_slots=4)
        self.assertTrue(outbox.restore(
            7, C.PICO_LOG, C.FLAG_JOURNALED,
            C.GLOBAL_SCOPE, 500, 1, b"old"))
        first = outbox.enqueue_payload(
            C.PICO_DIAGNOSTIC, 0, C.GLOBAL_SCOPE, 2, b"x" * 200)
        second = outbox.enqueue_payload(
            C.LINK_STATE, 0, 0, 3, b"small")
        third = outbox.enqueue_payload(
            C.PICO_DIAGNOSTIC, 0, C.GLOBAL_SCOPE, 4, b"y" * 200)
        self.assertEqual((first, second, third), (1, 2, 3))

        seen = []
        for boot, watermark in ((99, 1), (99, 2), (99, 3), (7, 500)):
            sequence, message, _, _ = outbox.peek()
            seen.append((
                int.from_bytes(bytes(message[20:28]), "big"), sequence))
            self.assertEqual(outbox.acknowledge(boot, watermark), 1)
        self.assertEqual(seen, [(99, 1), (99, 2), (99, 3), (7, 500)])
        self.assertIsNone(outbox.peek())


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
            # Watermarks reach flash on commit, not on every record written.
            managed.commit()
            managed.file.close()
            recovered = []
            self.assertEqual(journal.recover_directory(
                directory, lambda *args: recovered.append(args)), 1)
            self.assertEqual(recovered[0][4], 2)

    def test_records_are_written_without_committing_each_one(self):
        # The write costs ~1.6 ms on the device, the commit ~45 ms with
        # interrupts disabled. Committing per record is what starved the UART.
        commits = []
        with tempfile.TemporaryDirectory() as directory:
            managed = journal.BMJ1Journal(directory, 99, 1000, staging_slots=8)
            managed.file.flush = lambda: commits.append(1)
            for sequence in range(1, 6):
                managed.stage(C.BMCU_FRAME, 0, 0, sequence, sequence, b"x")
                managed.flush_one()
            self.assertEqual(commits, [], "no commit until one is asked for")
            self.assertGreater(managed.uncommitted_bytes, 0)

            self.assertGreater(managed.commit(), 0)
            self.assertEqual(len(commits), 1, "one commit covers every record")
            self.assertEqual(managed.uncommitted_bytes, 0)
            self.assertEqual(managed.commit_count, 1)
            managed.file.close()

    def test_commit_is_free_when_nothing_is_pending(self):
        commits = []
        with tempfile.TemporaryDirectory() as directory:
            managed = journal.BMJ1Journal(directory, 99, 1000)
            managed.file.flush = lambda: commits.append(1)
            self.assertEqual(managed.commit(), 0)
            self.assertEqual(commits, [])
            self.assertEqual(managed.commit_count, 0)
            managed.file.close()

    def test_an_unbounded_backlog_forces_a_commit(self):
        # A power cut can only take commit_bytes of history with it.
        with tempfile.TemporaryDirectory() as directory:
            managed = journal.BMJ1Journal(
                directory, 99, 1000, staging_slots=8, commit_bytes=64)
            for sequence in range(1, 9):
                managed.stage(C.BMCU_FRAME, 0, 0, sequence, sequence, b"y" * 8)
                managed.flush_one()
            self.assertGreater(managed.commit_count, 0)
            self.assertLess(managed.uncommitted_bytes, 64)
            managed.file.close()

    def test_rotation_counts_as_a_commit(self):
        with tempfile.TemporaryDirectory() as directory:
            managed = journal.BMJ1Journal(
                directory, 99, 1000, staging_slots=1, segment_size=128,
                commit_bytes=1 << 30)
            for sequence in range(1, 12):
                managed.stage(C.BMCU_FRAME, 0, 0, sequence, sequence, b"z" * 8)
                managed.flush_one(sequence)
            # Closing the old segment persisted it, so nothing from before the
            # rotation is still waiting.
            self.assertLess(managed.uncommitted_bytes, 128)
            managed.file.close()

    def test_rotation_prunes_oldest_segments(self):
        # Unbounded segments filled littlefs, after which every flush_one
        # raised ENOSPC instead of dropping the oldest history.
        with tempfile.TemporaryDirectory() as directory:
            managed = journal.BMJ1Journal(
                directory, 99, 1000, staging_slots=1, segment_size=128,
                max_segments=3)
            for sequence in range(1, 40):
                managed.stage(C.BMCU_FRAME, 0, 0, sequence, sequence, b"x" * 8)
                managed.flush_one(sequence)
            managed.file.close()
            names = sorted(name for name in os.listdir(directory)
                           if name.endswith(".bmj"))
            self.assertEqual(len(names), 3)
            self.assertEqual(names[-1], "%08d.bmj" % managed.segment_sequence)
            self.assertGreater(managed.segments_removed, 0)

    def test_restart_prunes_history_beyond_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            for _ in range(5):
                managed = journal.BMJ1Journal(
                    directory, 99, 1000, staging_slots=1, max_segments=2)
                managed.stage(C.BMCU_FRAME, 0, 0, 1, 5, b"x")
                managed.flush_one()
                managed.file.close()
            names = [name for name in os.listdir(directory)
                     if name.endswith(".bmj")]
            self.assertEqual(len(names), 2)

    def test_zero_max_segments_keeps_every_segment(self):
        with tempfile.TemporaryDirectory() as directory:
            for _ in range(4):
                managed = journal.BMJ1Journal(
                    directory, 99, 1000, staging_slots=1, max_segments=0)
                managed.stage(C.BMCU_FRAME, 0, 0, 1, 5, b"x")
                managed.flush_one()
                managed.file.close()
            names = [name for name in os.listdir(directory)
                     if name.endswith(".bmj")]
            self.assertEqual(len(names), 4)

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

    def test_cursor_synthesizes_drop_for_sparse_journal_sequences(self):
        with tempfile.TemporaryDirectory() as directory:
            managed = journal.BMJ1Journal(
                directory, 99, 1000, staging_slots=1)
            for sequence in (1, 4):
                managed.stage(
                    C.BMCU_FRAME, C.FLAG_CRITICAL, 0, sequence,
                    sequence, b"x")
                managed.flush_one()
            managed.file.close()
            cursor = journal.JournalReplayCursor(directory)
            outbox = BMB1Outbox(100, durable_slots=4)
            outbox.replay_pager = lambda: cursor.page_into(outbox, 4)
            outbox.replay_pager()

            seen = []
            drop_range = None
            while True:
                current = outbox.peek()
                if current is None:
                    break
                sequence, message, _, _ = current
                parser = binary.StreamParser(bytearray(C.MAX_MESSAGE_SIZE))
                parser.feed(message)
                decoded = parser.next_message()
                seen.append((sequence, decoded.message_type))
                if decoded.message_type == C.TRANSPORT_DROP:
                    drop_range = binary.parse_transport_drop(decoded)[1:4]
                outbox.acknowledge(decoded.pico_boot_id, sequence)
            self.assertEqual(seen, [
                (1, C.BMCU_FRAME),
                (2, C.TRANSPORT_DROP),
                (4, C.BMCU_FRAME),
            ])
            self.assertEqual(drop_range, (2, 3, 2))


if __name__ == "__main__":
    unittest.main()
