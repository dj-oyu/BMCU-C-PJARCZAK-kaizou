"""Raw-frame BMB1 queueing without production dictionary envelopes."""

try:
    import ustruct as struct
except ImportError:
    import struct

import bmcu_binary as binary
import bmcu_binary_constants as C
from byte_ring import ByteRing, RingFull

BMCU_STATUS_KIND = 0x02
BMCU_EVENT_KIND = 0x03
DELIVERY_SLOT_SIZE = 128
STATUS_SLOT_SIZE = 80


class BMB1Outbox:
    """Separates unsequenced replaceable STATUS from durable records."""

    def __init__(self, pico_boot_id, link_count=1, durable_slots=128,
                 journal=None):
        self.pico_boot_id = pico_boot_id
        self.next_sequence = 1
        self.durable = ByteRing(
            bytearray(DELIVERY_SLOT_SIZE * durable_slots), DELIVERY_SLOT_SIZE)
        self.latest_status = ByteRing(
            bytearray(STATUS_SLOT_SIZE * link_count), STATUS_SLOT_SIZE)
        self.current_status = ByteRing(
            bytearray(DELIVERY_SLOT_SIZE * link_count), DELIVERY_SLOT_SIZE)
        self.encode_buffer = bytearray(C.MAX_MESSAGE_SIZE)
        self.journal = journal
        self.forced_drop_first = 0
        self.forced_drop_last = 0
        self.forced_drop_count = 0
        self.forced_drop_observed_at_us = 0
        self.status_replacements = 0
        self.last_ack_watermark = 0

    def _allocate_sequence(self):
        result = self.next_sequence
        self.next_sequence += 1
        return result

    def _remember_drop(self, sequence, observed_at_us=0):
        if not self.forced_drop_count:
            self.forced_drop_first = sequence
        self.forced_drop_last = sequence
        self.forced_drop_count += 1
        self.forced_drop_observed_at_us = observed_at_us

    def enqueue_raw(self, link_index, received_at_us, wire, metadata):
        kind = metadata["kind"]
        if kind == BMCU_STATUS_KIND:
            payload_size = 10 + len(wire)
            if payload_size > STATUS_SLOT_SIZE:
                raise ValueError("STATUS exceeds latest-value slot")
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
        sequence = self._allocate_sequence()
        size = binary.write_bmcu_frame(
            self.encode_buffer, 0,
            C.FLAG_CRITICAL if kind == BMCU_EVENT_KIND else 0,
            sequence, self.pico_boot_id, link_index, received_at_us, wire)
        try:
            self.durable.append(
                sequence, memoryview(self.encode_buffer)[:size],
                protected=kind == BMCU_EVENT_KIND)
        except RingFull:
            self._remember_drop(sequence, received_at_us)
            return 0
        if self.journal is not None:
            message = _decode_header(self.encode_buffer)
            try:
                self.journal.stage(
                    C.BMCU_FRAME, message[1], link_index, sequence,
                    received_at_us,
                    memoryview(self.encode_buffer)[C.HEADER_SIZE:size])
            except RingFull:
                # Delivery remains live. A later diagnostic/drop record makes
                # journal staging loss explicit without blocking UART.
                self._remember_drop(sequence, received_at_us)
        return sequence

    def materialize_drop(self):
        if not self.forced_drop_count or len(self.durable) == \
                self.durable.capacity:
            return False
        sequence = self._allocate_sequence()
        size = binary.write_transport_drop(
            self.encode_buffer, 0, C.FLAG_CRITICAL, sequence,
            self.pico_boot_id, self.forced_drop_observed_at_us,
            self.forced_drop_first, self.forced_drop_last,
            self.forced_drop_count, C.DROP_RAM_QUEUE_FULL)
        self.durable.append(
            sequence, memoryview(self.encode_buffer)[:size], protected=True)
        if self.journal is not None:
            try:
                self.journal.stage(
                    C.TRANSPORT_DROP, C.FLAG_CRITICAL, C.GLOBAL_SCOPE,
                    sequence, self.forced_drop_observed_at_us,
                    memoryview(self.encode_buffer)[C.HEADER_SIZE:size])
            except RingFull:
                pass
        self.forced_drop_first = 0
        self.forced_drop_last = 0
        self.forced_drop_count = 0
        self.forced_drop_observed_at_us = 0
        return True

    def materialize_status(self):
        if len(self.durable) or not len(self.latest_status):
            return False
        _, payload, _, link_index = self.latest_status.peek()
        sequence = self._allocate_sequence()
        size = binary.write_message(
            self.encode_buffer, 0, C.BMCU_FRAME, 0, sequence,
            self.pico_boot_id, link_index, payload)
        self.durable.append(
            sequence, memoryview(self.encode_buffer)[:size], protected=False)
        self.latest_status.release_through(0)
        return True

    def peek(self):
        self.materialize_drop()
        self.materialize_status()
        return self.durable.peek()

    def acknowledge(self, pico_boot_id, watermark):
        released = 0
        while True:
            current = self.durable.peek()
            if current is None:
                break
            sequence, message, _, _ = current
            header_boot = struct.unpack_from(">Q", message, 20)[0]
            if header_boot != pico_boot_id or sequence > watermark:
                break
            self.durable.release_one()
            released += 1
        if self.journal is not None:
            self.journal.record_ack(pico_boot_id, watermark)
        if pico_boot_id == self.pico_boot_id:
            self.last_ack_watermark = max(
                self.last_ack_watermark, watermark)
        return released

    def restore(self, pico_boot_id, record_type, flags, link_index, sequence,
                received_at_us, payload):
        size = binary.write_message(
            self.encode_buffer, 0, record_type, flags | C.FLAG_REPLAY,
            sequence, pico_boot_id, link_index, payload)
        try:
            self.durable.append(
                sequence, memoryview(self.encode_buffer)[:size],
                protected=True)
            return True
        except RingFull:
            return False

    def enqueue_link_state(self, link_index, observed_at_us, state, reason):
        sequence = self._allocate_sequence()
        flags = C.FLAG_CRITICAL if state in (
            C.LINK_OFFLINE, C.LINK_INCOMPATIBLE) else 0
        size = binary.write_link_state(
            self.encode_buffer, 0, flags, sequence, self.pico_boot_id,
            link_index, observed_at_us, state, reason)
        try:
            self.durable.append(
                sequence, memoryview(self.encode_buffer)[:size],
                protected=True)
        except RingFull:
            self._remember_drop(sequence, observed_at_us)
            return 0
        if self.journal is not None:
            try:
                self.journal.stage(
                    C.LINK_STATE, flags, link_index, sequence, observed_at_us,
                    memoryview(self.encode_buffer)[C.HEADER_SIZE:size])
            except RingFull:
                self._remember_drop(sequence, observed_at_us)
        return sequence

    def enqueue_payload(self, record_type, flags, link_index, received_at_us,
                        payload, journal_record=False):
        sequence = self._allocate_sequence()
        size = binary.write_message(
            self.encode_buffer, 0, record_type, flags, sequence,
            self.pico_boot_id, link_index, payload)
        try:
            self.durable.append(
                sequence, memoryview(self.encode_buffer)[:size],
                protected=bool(flags & C.FLAG_CRITICAL))
        except RingFull:
            self._remember_drop(sequence, received_at_us)
            return 0
        if journal_record and self.journal is not None:
            try:
                self.journal.stage(
                    record_type, flags, link_index, sequence, received_at_us,
                    payload)
            except RingFull:
                self._remember_drop(sequence, received_at_us)
        return sequence

    @property
    def queue_depth(self):
        return len(self.durable) + len(self.latest_status)

    def available_boot_ranges(self):
        ranges = {}
        for sequence, message, _, _ in self.durable.iter_records():
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


def _decode_header(message):
    _, _, _, flags, _, sequence, _, link, _ = struct.unpack_from(
        ">4sBBHIQQB3s", message, 0)
    return sequence, flags, link
