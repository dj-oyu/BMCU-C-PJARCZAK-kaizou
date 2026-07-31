"""Allocation-bounded BMB1 codec for MicroPython and CPython.

Encoders write into caller-owned mutable buffers and return the byte count.
Views returned by ``StreamParser.next_message`` remain valid only until the
next parser mutation.
"""

try:
    import ustruct as struct
except ImportError:
    import struct

import bmcu_binary_constants as C


class CodecError(ValueError):
    pass


class NeedMoreData(Exception):
    pass


def _check_space(out, offset, size):
    if offset < 0 or size < 0 or offset + size > len(out):
        raise CodecError("output buffer too small")


def write_header(out, offset, message_type, flags, payload_length,
                 transport_sequence, pico_boot_id, link_index):
    if payload_length < 0 or payload_length > C.MAX_PAYLOAD_SIZE:
        raise CodecError("payload length out of range")
    if flags & ~C.KNOWN_FLAGS:
        raise CodecError("reserved flag set")
    if link_index < 0 or link_index > 0xFF:
        raise CodecError("link index out of range")
    _check_space(out, offset, C.HEADER_SIZE)
    struct.pack_into(">4sBBHIQQB3s", out, offset, C.MAGIC, C.VERSION,
                     message_type, flags, payload_length,
                     transport_sequence, pico_boot_id, link_index, b"\0\0\0")
    return C.HEADER_SIZE


def write_message(out, offset, message_type, flags, transport_sequence,
                  pico_boot_id, link_index, payload):
    payload_length = len(payload)
    _check_space(out, offset, C.HEADER_SIZE + payload_length)
    write_header(out, offset, message_type, flags, payload_length,
                 transport_sequence, pico_boot_id, link_index)
    memoryview(out)[offset + C.HEADER_SIZE:
                    offset + C.HEADER_SIZE + payload_length] = payload
    return C.HEADER_SIZE + payload_length


def write_bmcu_frame(out, offset, flags, transport_sequence, pico_boot_id,
                     link_index, received_at_us, wire_frame):
    wire_length = len(wire_frame)
    payload_length = 10 + wire_length
    _check_space(out, offset, C.HEADER_SIZE + payload_length)
    write_header(out, offset, C.BMCU_FRAME, flags, payload_length,
                 transport_sequence, pico_boot_id, link_index)
    pos = offset + C.HEADER_SIZE
    struct.pack_into(">QH", out, pos, received_at_us, wire_length)
    memoryview(out)[pos + 10:pos + 10 + wire_length] = wire_frame
    return C.HEADER_SIZE + payload_length


def write_link_state(out, offset, flags, transport_sequence, pico_boot_id,
                     link_index, observed_at_us, state, reason):
    _check_space(out, offset, C.HEADER_SIZE + 12)
    write_header(out, offset, C.LINK_STATE, flags, 12, transport_sequence,
                 pico_boot_id, link_index)
    struct.pack_into(">QBBH", out, offset + C.HEADER_SIZE, observed_at_us,
                     state, reason, 0)
    return C.HEADER_SIZE + 12


def write_transport_drop(out, offset, flags, transport_sequence, pico_boot_id,
                         observed_at_us, first_sequence, last_sequence, count,
                         reason):
    if (first_sequence == 0 or last_sequence < first_sequence or
            count != last_sequence - first_sequence + 1):
        raise CodecError("TRANSPORT_DROP requires exact non-empty bounds")
    _check_space(out, offset, C.HEADER_SIZE + 32)
    write_header(out, offset, C.TRANSPORT_DROP, flags, 32,
                 transport_sequence, pico_boot_id, C.GLOBAL_SCOPE)
    struct.pack_into(">QQQIB3s", out, offset + C.HEADER_SIZE, observed_at_us,
                     first_sequence, last_sequence, count, reason, b"\0\0\0")
    return C.HEADER_SIZE + 32


def write_tlv(out, offset, tag, value_type, value):
    size = len(value)
    if size > 0xFFFF:
        raise CodecError("TLV value too large")
    _check_space(out, offset, 4 + size)
    struct.pack_into(">BBH", out, offset, tag, value_type, size)
    memoryview(out)[offset + 4:offset + 4 + size] = value
    return 4 + size


def write_diagnostic(out, offset, flags, transport_sequence, pico_boot_id,
                     encoded_tlvs):
    return write_message(out, offset, C.PICO_DIAGNOSTIC, flags,
                         transport_sequence, pico_boot_id, C.GLOBAL_SCOPE,
                         encoded_tlvs)


def write_log(out, offset, flags, transport_sequence, pico_boot_id,
              log_sequence, uptime_ms, severity, component, message, detail):
    component_length = len(component)
    message_length = len(message)
    detail_length = len(detail)
    if component_length > C.MAX_LOG_COMPONENT_BYTES:
        raise CodecError("component too large")
    if message_length > C.MAX_LOG_MESSAGE_BYTES:
        raise CodecError("message too large")
    if detail_length > C.MAX_LOG_DETAIL_BYTES:
        raise CodecError("detail too large")
    payload_length = 22 + component_length + message_length + detail_length
    _check_space(out, offset, C.HEADER_SIZE + payload_length)
    write_header(out, offset, C.PICO_LOG, flags, payload_length,
                 transport_sequence, pico_boot_id, C.GLOBAL_SCOPE)
    pos = offset + C.HEADER_SIZE
    struct.pack_into(">QQBBHH", out, pos, log_sequence, uptime_ms, severity,
                     component_length, message_length, detail_length)
    pos += 22
    view = memoryview(out)
    view[pos:pos + component_length] = component
    pos += component_length
    view[pos:pos + message_length] = message
    pos += message_length
    view[pos:pos + detail_length] = detail
    return C.HEADER_SIZE + payload_length


def write_hello(out, offset, flags, pico_boot_id, device_id, firmware, links,
                replay_boot_ranges, auth_hmac):
    if len(device_id) > C.MAX_DEVICE_ID_BYTES:
        raise CodecError("device id too large")
    if len(firmware) > C.MAX_FIRMWARE_BYTES:
        raise CodecError("firmware too large")
    if len(links) > C.MAX_LINK_COUNT:
        raise CodecError("too many links")
    if (not replay_boot_ranges or
            len(replay_boot_ranges) > C.MAX_REPLAY_BOOT_RANGES):
        raise CodecError("invalid replay boot range count")
    if replay_boot_ranges[0][0] != pico_boot_id:
        raise CodecError("current boot range must be first")
    if len(auth_hmac) != 32:
        raise CodecError("HELLO HMAC must be 32 bytes")
    payload_length = (1 + len(device_id) + 1 + len(firmware) + 1 + 1 +
                      len(replay_boot_ranges) * 24 + 32)
    for link_index, link_id in links:
        if link_index < 0 or link_index >= C.MAX_LINK_COUNT:
            raise CodecError("link index out of range")
        if len(link_id) > C.MAX_LINK_ID_BYTES:
            raise CodecError("link id too large")
        payload_length += 2 + len(link_id)
    _check_space(out, offset, C.HEADER_SIZE + payload_length)
    write_header(out, offset, C.HELLO, flags, payload_length, 0,
                 pico_boot_id, C.GLOBAL_SCOPE)
    pos = offset + C.HEADER_SIZE
    out[pos] = len(device_id)
    pos += 1
    memoryview(out)[pos:pos + len(device_id)] = device_id
    pos += len(device_id)
    out[pos] = len(firmware)
    pos += 1
    memoryview(out)[pos:pos + len(firmware)] = firmware
    pos += len(firmware)
    out[pos] = len(links)
    pos += 1
    for link_index, link_id in links:
        out[pos] = link_index
        out[pos + 1] = len(link_id)
        pos += 2
        memoryview(out)[pos:pos + len(link_id)] = link_id
        pos += len(link_id)
    out[pos] = len(replay_boot_ranges)
    pos += 1
    for boot_id, oldest_sequence, newest_sequence in replay_boot_ranges:
        struct.pack_into(">QQQ", out, pos, boot_id, oldest_sequence,
                         newest_sequence)
        pos += 24
    memoryview(out)[pos:pos + 32] = auth_hmac
    return C.HEADER_SIZE + payload_length


def write_control_result(out, offset, flags, transport_sequence, pico_boot_id,
                         link_index, command_sequence, result, detail,
                         auth_hmac):
    if len(detail) > C.MAX_CONTROL_RESULT_DETAIL_BYTES:
        raise CodecError("CONTROL_RESULT detail too large")
    if len(auth_hmac) != 32:
        raise CodecError("CONTROL_RESULT HMAC must be 32 bytes")
    payload_length = 12 + len(detail) + 32
    _check_space(out, offset, C.HEADER_SIZE + payload_length)
    write_header(out, offset, C.CONTROL_RESULT, flags, payload_length,
                 transport_sequence, pico_boot_id, link_index)
    pos = offset + C.HEADER_SIZE
    struct.pack_into(">QBBH", out, pos, command_sequence, result, 0,
                     len(detail))
    pos += 12
    memoryview(out)[pos:pos + len(detail)] = detail
    pos += len(detail)
    memoryview(out)[pos:pos + 32] = auth_hmac
    return C.HEADER_SIZE + payload_length


def write_ping(out, offset, message_type, pico_boot_id, token):
    if message_type != C.PING and message_type != C.PONG:
        raise CodecError("message type must be PING or PONG")
    _check_space(out, offset, C.HEADER_SIZE + 8)
    write_header(out, offset, message_type, 0, 8, 0, pico_boot_id,
                 C.GLOBAL_SCOPE)
    struct.pack_into(">Q", out, offset + C.HEADER_SIZE, token)
    return C.HEADER_SIZE + 8


def write_protocol_error(out, offset, pico_boot_id, code, detail):
    if len(detail) > C.MAX_ERROR_DETAIL_BYTES:
        raise CodecError("PROTOCOL_ERROR detail too large")
    payload_length = 4 + len(detail)
    _check_space(out, offset, C.HEADER_SIZE + payload_length)
    write_header(out, offset, C.PROTOCOL_ERROR, 0, payload_length, 0,
                 pico_boot_id, C.GLOBAL_SCOPE)
    struct.pack_into(">HH", out, offset + C.HEADER_SIZE, code, len(detail))
    memoryview(out)[offset + C.HEADER_SIZE + 4:
                    offset + C.HEADER_SIZE + payload_length] = detail
    return C.HEADER_SIZE + payload_length


class Message:
    __slots__ = ("message_type", "flags", "payload_length",
                 "transport_sequence", "pico_boot_id", "link_index",
                 "header", "payload")

    def __init__(self, message_type, flags, payload_length,
                 transport_sequence, pico_boot_id, link_index, header, payload):
        self.message_type = message_type
        self.flags = flags
        self.payload_length = payload_length
        self.transport_sequence = transport_sequence
        self.pico_boot_id = pico_boot_id
        self.link_index = link_index
        self.header = header
        self.payload = payload


class StreamParser:
    """Bounded parser backed by storage supplied by the caller."""

    def __init__(self, storage):
        if len(storage) < C.MAX_MESSAGE_SIZE:
            raise ValueError("parser storage must hold one maximum message")
        self._storage = storage
        self._start = 0
        self._end = 0

    @property
    def buffered(self):
        return self._end - self._start

    def _compact(self):
        count = self.buffered
        if self._start and count:
            for index in range(count):
                self._storage[index] = self._storage[self._start + index]
        self._start = 0
        self._end = count

    def feed(self, data):
        if len(data) > len(self._storage) - self.buffered:
            raise CodecError("receive buffer overflow")
        if len(self._storage) - self._end < len(data):
            self._compact()
        memoryview(self._storage)[self._end:self._end + len(data)] = data
        self._end += len(data)

    def next_message(self):
        if self.buffered < C.HEADER_SIZE:
            return None
        pos = self._start
        magic, version, message_type, flags, payload_length, sequence, boot_id, \
            link_index, reserved = struct.unpack_from(">4sBBHIQQB3s",
                                                      self._storage, pos)
        if magic != C.MAGIC or version != C.VERSION:
            raise CodecError("invalid BMB1 header")
        if payload_length > C.MAX_PAYLOAD_SIZE:
            raise CodecError("payload too large")
        total = C.HEADER_SIZE + payload_length
        if self.buffered < total:
            return None
        view = memoryview(self._storage)
        header = view[pos:pos + C.HEADER_SIZE]
        payload = view[pos + C.HEADER_SIZE:pos + total]
        self._start += total
        if self._start == self._end:
            self._start = 0
            self._end = 0
        return Message(message_type, flags, payload_length, sequence, boot_id,
                       link_index, header, payload)


def parse_challenge(message):
    if message.message_type != C.SERVER_CHALLENGE or len(message.payload) != 32:
        raise CodecError("invalid SERVER_CHALLENGE")
    return message.payload


def parse_pong(message):
    if message.message_type != C.PONG or len(message.payload) != 8:
        raise CodecError("not PONG")
    return struct.unpack_from(">Q", message.payload, 0)[0]


def parse_hello_accepted(message):
    if message.message_type != C.HELLO_ACCEPTED or len(message.payload) != 16:
        raise CodecError("invalid HELLO_ACCEPTED")
    return struct.unpack_from(">QII", message.payload, 0)


def parse_hello(message):
    if message.message_type != C.HELLO:
        raise CodecError("not HELLO")
    payload = message.payload
    pos = 0

    def field(maximum):
        nonlocal pos
        if pos >= len(payload):
            raise CodecError("truncated HELLO")
        size = payload[pos]
        pos += 1
        if size > maximum or size > len(payload) - pos:
            raise CodecError("invalid HELLO field")
        result = payload[pos:pos + size]
        pos += size
        return result

    device_id = field(C.MAX_DEVICE_ID_BYTES)
    firmware = field(C.MAX_FIRMWARE_BYTES)
    if pos >= len(payload):
        raise CodecError("truncated HELLO links")
    link_count = payload[pos]
    pos += 1
    if link_count > C.MAX_LINK_COUNT:
        raise CodecError("too many HELLO links")
    links = []
    for _ in range(link_count):
        if pos >= len(payload):
            raise CodecError("truncated HELLO link")
        link_index = payload[pos]
        pos += 1
        links.append((link_index, field(C.MAX_LINK_ID_BYTES)))
    if pos >= len(payload):
        raise CodecError("truncated replay boot table")
    boot_count = payload[pos]
    pos += 1
    if not boot_count or boot_count > C.MAX_REPLAY_BOOT_RANGES:
        raise CodecError("invalid replay boot count")
    if len(payload) - pos != boot_count * 24 + 32:
        raise CodecError("invalid HELLO replay table")
    ranges = []
    for _ in range(boot_count):
        ranges.append(struct.unpack_from(">QQQ", payload, pos))
        pos += 24
    return device_id, firmware, tuple(links), tuple(ranges), payload[pos:]


def parse_ack(message):
    payload = message.payload
    if message.message_type != C.ACK or len(payload) < 20:
        raise CodecError("invalid ACK")
    boot_id, scope, reject_count, reserved, watermark = struct.unpack_from(
        ">QBBHQ", payload, 0)
    if scope != C.GLOBAL_SCOPE or reserved != 0:
        raise CodecError("invalid ACK scope")
    expected = 20 + reject_count * 9
    if len(payload) != expected:
        raise CodecError("invalid ACK reject list")
    return boot_id, watermark, reject_count, payload[20:]


def parse_control(message):
    payload = message.payload
    if message.message_type != C.CONTROL or len(payload) < 54:
        raise CodecError("invalid CONTROL")
    command_sequence, issued_at_us, ttl_ms, command, argument_length = \
        struct.unpack_from(">QQIBB", payload, 0)
    if argument_length > C.MAX_CONTROL_ARGUMENT_BYTES:
        raise CodecError("CONTROL arguments too large")
    expected = 22 + argument_length + 32
    if len(payload) != expected:
        raise CodecError("invalid CONTROL length")
    return (command_sequence, issued_at_us, ttl_ms, command,
            payload[22:22 + argument_length], payload[22 + argument_length:])


def parse_link_state(message):
    if message.message_type != C.LINK_STATE or len(message.payload) != 12:
        raise CodecError("invalid LINK_STATE")
    observed, state, reason, reserved = struct.unpack_from(
        ">QBBH", message.payload, 0)
    if reserved:
        raise CodecError("invalid LINK_STATE reserved value")
    return observed, state, reason


def parse_transport_drop(message):
    if message.message_type != C.TRANSPORT_DROP or len(message.payload) != 32:
        raise CodecError("invalid TRANSPORT_DROP")
    observed, first, last, count, reason, reserved = struct.unpack_from(
        ">QQQIB3s", message.payload, 0)
    if (reserved != b"\0\0\0" or first == 0 or last < first or
            count != last - first + 1):
        raise CodecError("invalid TRANSPORT_DROP fields")
    return observed, first, last, count, reason


def parse_bmcu_frame(message):
    if message.message_type != C.BMCU_FRAME or len(message.payload) < 10:
        raise CodecError("invalid BMCU_FRAME")
    received_at_us, wire_length = struct.unpack_from(">QH", message.payload, 0)
    if len(message.payload) != 10 + wire_length:
        raise CodecError("invalid BMCU wire length")
    return received_at_us, message.payload[10:]


def parse_tlvs(payload):
    """Yield ``(tag, value_type, value_view)`` while checking every bound."""
    pos = 0
    while pos < len(payload):
        if len(payload) - pos < 4:
            raise CodecError("truncated TLV")
        tag, value_type, size = struct.unpack_from(">BBH", payload, pos)
        pos += 4
        if size > len(payload) - pos:
            raise CodecError("truncated TLV value")
        yield tag, value_type, payload[pos:pos + size]
        pos += size


def parse_log(message):
    payload = message.payload
    if message.message_type != C.PICO_LOG or len(payload) < 22:
        raise CodecError("invalid PICO_LOG")
    log_sequence, uptime_ms, severity, component_length, message_length, \
        detail_length = struct.unpack_from(">QQBBHH", payload, 0)
    if component_length > C.MAX_LOG_COMPONENT_BYTES:
        raise CodecError("PICO_LOG component too large")
    if message_length > C.MAX_LOG_MESSAGE_BYTES:
        raise CodecError("PICO_LOG message too large")
    if detail_length > C.MAX_LOG_DETAIL_BYTES:
        raise CodecError("PICO_LOG detail too large")
    expected = 22 + component_length + message_length + detail_length
    if len(payload) != expected:
        raise CodecError("invalid PICO_LOG length")
    pos = 22
    component = payload[pos:pos + component_length]
    pos += component_length
    text = payload[pos:pos + message_length]
    pos += message_length
    return (log_sequence, uptime_ms, severity, component, text,
            payload[pos:pos + detail_length])


def parse_control_result(message):
    payload = message.payload
    if message.message_type != C.CONTROL_RESULT or len(payload) < 44:
        raise CodecError("invalid CONTROL_RESULT")
    command_sequence, result, reserved, detail_length = struct.unpack_from(
        ">QBBH", payload, 0)
    if reserved or detail_length > C.MAX_CONTROL_RESULT_DETAIL_BYTES:
        raise CodecError("invalid CONTROL_RESULT fields")
    if len(payload) != 12 + detail_length + 32:
        raise CodecError("invalid CONTROL_RESULT length")
    return (command_sequence, result, payload[12:12 + detail_length],
            payload[12 + detail_length:])
