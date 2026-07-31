"""Bounded BMB1 queues with contiguous global sequence semantics."""

try:
    import ustruct as struct
except ImportError:
    import struct

import bmcu_binary as binary
import bmcu_binary_constants as C
from byte_ring import ByteRing, RingFull, RecordTooLarge

BMCU_STATUS_KIND = 0x02
BMCU_EVENT_KIND = 0x03
DELIVERY_SLOT_SIZE = 128
LARGE_SLOT_SIZE = 1024
STATUS_SLOT_SIZE = 80


class BMB1Outbox:
    def __init__(self, pico_boot_id, link_count=1, durable_slots=128,
                 journal=None, large_slots=16):
        self.pico_boot_id = pico_boot_id
        self.next_sequence = 1
        self.durable = ByteRing(
            bytearray(DELIVERY_SLOT_SIZE * durable_slots), DELIVERY_SLOT_SIZE)
        self.large = ByteRing(
            bytearray(LARGE_SLOT_SIZE * large_slots), LARGE_SLOT_SIZE)
        self.latest_status = ByteRing(
            bytearray(STATUS_SLOT_SIZE * link_count), STATUS_SLOT_SIZE)
        self.current_status = ByteRing(
            bytearray(DELIVERY_SLOT_SIZE * link_count), DELIVERY_SLOT_SIZE)
        self.encode_buffer = bytearray(C.MAX_MESSAGE_SIZE)
        self.journal = journal
        self.replay_pager = None
        self.historical_ranges = ()
        self.forced_drop_first = 0
        self.forced_drop_last = 0
        self.forced_drop_count = 0
        self.forced_drop_observed_at_us = 0
        self.status_replacements = 0
        self.last_ack_watermark = 0
        self.journal_failure_count = 0

    def _allocate_sequence(self):
        if self.forced_drop_count:
            return 0
        result = self.next_sequence
        self.next_sequence += 1
        return result

    def _reserve_drop(self, observed_at_us=0):
        if not self.forced_drop_count:
            # The marker itself owns this otherwise missing sequence. No later
            # sequence is allocated until the marker is stored.
            self.forced_drop_first = self.next_sequence
            self.forced_drop_last = self.next_sequence
            self.next_sequence += 1
        else:
            # Every additional lost record owns the next global sequence so
            # the marker advertises one exact contiguous range.
            self.forced_drop_last = self.next_sequence
            self.next_sequence += 1
        self.forced_drop_count += 1
        self.forced_drop_observed_at_us = observed_at_us

    def _ring_for_size(self, size):
        if size <= DELIVERY_SLOT_SIZE:
            return self.durable
        if size <= LARGE_SLOT_SIZE:
            return self.large
        return None

    def _append_message(self, sequence, size, protected,
                        use_drop_reserve=False):
        target = self._ring_for_size(size)
        if target is None:
            return False
        limit = target.capacity
        if (target is self.durable and target.capacity > 1 and
                not use_drop_reserve):
            # A loss marker is the only way the peer can advance its durable
            # ACK across records rejected while this ring is saturated.
            # Keep one slot available so replay traffic cannot deadlock that
            # marker behind a permanently full queue.
            limit -= 1
        if len(target) >= limit:
            return False
        try:
            target.append(sequence, memoryview(self.encode_buffer)[:size],
                          protected=protected)
            return True
        except (RingFull, RecordTooLarge):
            return False

    def enqueue_raw(self, link_index, received_at_us, wire, metadata):
        kind = (metadata["kind"] if isinstance(metadata, dict)
                else int(metadata))
        if kind == BMCU_STATUS_KIND:
            payload_size = 10 + len(wire)
            if payload_size > STATUS_SLOT_SIZE:
                self._reserve_drop(received_at_us)
                return 0
            struct.pack_into(">QH", self.encode_buffer, 0, received_at_us,
                             len(wire))
            memoryview(self.encode_buffer)[10:payload_size] = wire
            replaced = self.latest_status.append(
                0, memoryview(self.encode_buffer)[:payload_size],
                replace_key=link_index)
            if replaced:
                self.status_replacements += 1
            current_size = binary.write_bmcu_frame(
                self.encode_buffer, 0, 0, 0, self.pico_boot_id, link_index,
                received_at_us, wire)
            self.current_status.append(
                0, memoryview(self.encode_buffer)[:current_size],
                replace_key=link_index)
            return 0
        if self.forced_drop_count:
            self._reserve_drop(received_at_us)
            return 0
        sequence = self._allocate_sequence()
        size = binary.write_bmcu_frame(
            self.encode_buffer, 0,
            C.FLAG_CRITICAL if kind == BMCU_EVENT_KIND else 0,
            sequence, self.pico_boot_id, link_index, received_at_us, wire)
        if not self._append_message(
                sequence, size, kind == BMCU_EVENT_KIND):
            # Reuse the just-allocated sequence as the future DROP marker.
            self.forced_drop_first = sequence
            self.forced_drop_last = sequence
            self.forced_drop_count = 1
            self.forced_drop_observed_at_us = received_at_us
            return 0
        if self.journal is not None:
            try:
                self.journal.stage(
                    C.BMCU_FRAME,
                    C.FLAG_CRITICAL if kind == BMCU_EVENT_KIND else 0,
                    link_index, sequence, received_at_us,
                    memoryview(self.encode_buffer)[C.HEADER_SIZE:size])
            except (RingFull, RecordTooLarge):
                self.journal_failure_count += 1
        return sequence

    def materialize_drop(self):
        if not self.forced_drop_count or len(self.durable) == \
                self.durable.capacity:
            return False
        sequence = self.forced_drop_first
        size = binary.write_transport_drop(
            self.encode_buffer, 0, C.FLAG_CRITICAL, sequence,
            self.pico_boot_id, self.forced_drop_observed_at_us,
            self.forced_drop_first, self.forced_drop_last,
            self.forced_drop_count,
            C.DROP_RAM_QUEUE_FULL)
        if not self._append_message(
                sequence, size, True, use_drop_reserve=True):
            return False
        if self.journal is not None:
            try:
                self.journal.stage(
                    C.TRANSPORT_DROP, C.FLAG_CRITICAL, C.GLOBAL_SCOPE,
                    sequence, self.forced_drop_observed_at_us,
                    memoryview(self.encode_buffer)[C.HEADER_SIZE:size])
            except (RingFull, RecordTooLarge):
                self.journal_failure_count += 1
        self.forced_drop_first = 0
        self.forced_drop_last = 0
        self.forced_drop_count = 0
        self.forced_drop_observed_at_us = 0
        return True

    def materialize_status(self):
        if (self.forced_drop_count or len(self.durable) or len(self.large) or
                not len(self.latest_status)):
            return False
        _, payload, _, link_index = self.latest_status.peek()
        sequence = self._allocate_sequence()
        size = binary.write_message(
            self.encode_buffer, 0, C.BMCU_FRAME, 0, sequence,
            self.pico_boot_id, link_index, payload)
        if not self._append_message(sequence, size, False):
            self.forced_drop_first = sequence
            self.forced_drop_last = sequence
            self.forced_drop_count = 1
            return False
        self.latest_status.release_through(0)
        return True

    @staticmethod
    def _head(ring):
        return ring.peek()

    def _next_head(self):
        left, right = self._head(self.durable), self._head(self.large)
        if left is None:
            return self.large, right
        if right is None or left[0] <= right[0]:
            return self.durable, left
        return self.large, right

    def _page_replay(self):
        if self.replay_pager is not None:
            self.replay_pager()

    def peek(self):
        self.materialize_drop()
        self._page_replay()
        self.materialize_status()
        return self._next_head()[1]

    def acknowledge(self, pico_boot_id, watermark):
        released = 0
        while True:
            ring, current = self._next_head()
            if current is None:
                break
            sequence, message, _, _ = current
            header_boot = struct.unpack_from(">Q", message, 20)[0]
            if header_boot != pico_boot_id or sequence > watermark:
                break
            ring.release_one()
            released += 1
        if self.journal is not None:
            self.journal.record_ack(pico_boot_id, watermark)
        if pico_boot_id == self.pico_boot_id:
            self.last_ack_watermark = max(
                self.last_ack_watermark, watermark)
        self._page_replay()
        return released

    def restore(self, pico_boot_id, record_type, flags, link_index, sequence,
                received_at_us, payload):
        try:
            size = binary.write_message(
                self.encode_buffer, 0, record_type, flags | C.FLAG_REPLAY,
                sequence, pico_boot_id, link_index, payload)
        except binary.CodecError:
            return False
        return self._append_message(sequence, size, True)

    def enqueue_link_state(self, link_index, observed_at_us, state, reason):
        if self.forced_drop_count:
            self._reserve_drop(observed_at_us)
            return 0
        sequence = self._allocate_sequence()
        flags = C.FLAG_CRITICAL if state in (
            C.LINK_OFFLINE, C.LINK_INCOMPATIBLE) else 0
        size = binary.write_link_state(
            self.encode_buffer, 0, flags, sequence, self.pico_boot_id,
            link_index, observed_at_us, state, reason)
        if not self._append_message(sequence, size, True):
            self.forced_drop_first = sequence
            self.forced_drop_last = sequence
            self.forced_drop_count = 1
            self.forced_drop_observed_at_us = observed_at_us
            return 0
        if self.journal is not None:
            try:
                self.journal.stage(
                    C.LINK_STATE, flags, link_index, sequence, observed_at_us,
                    memoryview(self.encode_buffer)[C.HEADER_SIZE:size])
            except (RingFull, RecordTooLarge):
                self.journal_failure_count += 1
        return sequence

    def enqueue_payload(self, record_type, flags, link_index, received_at_us,
                        payload, journal_record=False):
        if self.forced_drop_count:
            self._reserve_drop(received_at_us)
            return 0
        sequence = self._allocate_sequence()
        try:
            size = binary.write_message(
                self.encode_buffer, 0, record_type, flags, sequence,
                self.pico_boot_id, link_index, payload)
        except binary.CodecError:
            self.forced_drop_first = sequence
            self.forced_drop_last = sequence
            self.forced_drop_count = 1
            self.forced_drop_observed_at_us = received_at_us
            return 0
        if not self._append_message(
                sequence, size, bool(flags & C.FLAG_CRITICAL)):
            self.forced_drop_first = sequence
            self.forced_drop_last = sequence
            self.forced_drop_count = 1
            self.forced_drop_observed_at_us = received_at_us
            return 0
        if journal_record and self.journal is not None:
            try:
                self.journal.stage(
                    record_type, flags, link_index, sequence, received_at_us,
                    payload)
            except (RingFull, RecordTooLarge):
                self.journal_failure_count += 1
        return sequence

    @property
    def queue_depth(self):
        return len(self.durable) + len(self.large) + len(self.latest_status)

    def available_boot_ranges(self):
        ranges = {}
        for boot_id, oldest, newest in self.historical_ranges:
            ranges[boot_id] = [oldest, newest]
        for ring in (self.durable, self.large):
            for sequence, message, _, _ in ring.iter_records():
                boot_id = struct.unpack_from(">Q", message, 20)[0]
                current = ranges.get(boot_id)
                if current is None:
                    ranges[boot_id] = [sequence, sequence]
                else:
                    current[0] = min(current[0], sequence)
                    current[1] = max(current[1], sequence)
        if self.pico_boot_id not in ranges:
            ranges[self.pico_boot_id] = [0, 0]
        current = ranges.pop(self.pico_boot_id)
        result = [(self.pico_boot_id, current[0], current[1])]
        for boot_id in sorted(ranges):
            values = ranges[boot_id]
            result.append((boot_id, values[0], values[1]))
        return tuple(result[:C.MAX_REPLAY_BOOT_RANGES])

    def iter_current(self):
        for _, message, _, _ in self.current_status.iter_records():
            yield message
