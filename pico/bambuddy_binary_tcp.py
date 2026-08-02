"""Cooperative authenticated BMB1 TCP client."""

try:
    import usocket as socket
except ImportError:
    import socket
try:
    import uhashlib as hashlib
except ImportError:
    import hashlib
try:
    import ustruct as struct
except ImportError:
    import struct

import bmcu_binary as binary
import bmcu_binary_constants as C

DISABLED = "disabled"
WIFI_WAIT = "wifi_wait"
CONNECTING = "connecting"
CHALLENGE_WAIT = "challenge_wait"
HELLO_SEND = "hello_send"
ACCEPT_WAIT = "accept_wait"
ONLINE = "online"
BACKOFF = "backoff"


def _sha256(data):
    digest = hashlib.sha256(data)
    return digest.digest()


def hmac_sha256(key, *parts):
    if len(key) > 64:
        key = _sha256(key)
    key = key + b"\0" * (64 - len(key))
    inner = bytes(value ^ 0x36 for value in key)
    outer = bytes(value ^ 0x5C for value in key)
    digest = hashlib.sha256(inner)
    for part in parts:
        digest.update(part)
    return _sha256(outer + digest.digest())


def hello_transcript(device_id, firmware, links, replay_boot_ranges, out):
    pos = 0
    out[pos] = len(device_id)
    pos += 1
    out[pos:pos + len(device_id)] = device_id
    pos += len(device_id)
    out[pos] = len(firmware)
    pos += 1
    out[pos:pos + len(firmware)] = firmware
    pos += len(firmware)
    out[pos] = len(links)
    pos += 1
    for index, link_id in links:
        out[pos] = index
        out[pos + 1] = len(link_id)
        pos += 2
        out[pos:pos + len(link_id)] = link_id
        pos += len(link_id)
    out[pos] = len(replay_boot_ranges)
    pos += 1
    for boot_id, oldest, newest in replay_boot_ranges:
        struct.pack_into(">QQQ", out, pos, boot_id, oldest, newest)
        pos += 24
    return pos


class BMB1TCPClient:
    def __init__(self, outbox, host, port, device_id, device_key, firmware,
                 links, socket_factory=None, clock_ms=None,
                 control_handler=None, clock_us=None,
                 send_metric=None, ticks_diff=None, ticks_add=None):
        self.outbox = outbox
        self.host = host
        self.port = port
        self.device_id = device_id
        self.device_key = device_key
        self.firmware = firmware
        self.links = links
        self.socket_factory = socket_factory or self._new_socket
        self.clock_ms = clock_ms or (lambda: 0)
        self.control_handler = control_handler
        self.clock_us = clock_us
        self.send_metric = send_metric
        self.ticks_diff = ticks_diff or (lambda left, right: left - right)
        self.ticks_add = ticks_add or (lambda value, delta: value + delta)
        self.state = WIFI_WAIT
        self.sock = None
        self.parser = binary.StreamParser(bytearray(C.MAX_MESSAGE_SIZE * 2))
        self.rx_buffer = bytearray(512)
        self._receive_into = None
        self.tx_buffer = bytearray(C.MAX_MESSAGE_SIZE)
        self.auth_buffer = bytearray(512)
        self.control_buffer = bytearray(256)
        self.control_pending_length = 0
        self.tx_view = None
        self.tx_offset = 0
        self.tx_sequence = 0
        self.highest_sent = 0
        self.replay_through = 0
        self.challenge = None
        self.session_key = None
        self.next_action_ms = 0
        self.last_error = None
        self.ping_interval_ms = 15000
        self.ack_timeout_ms = 5000
        self.last_rx_ms = 0
        self._connect_pending = False
        self.last_control_sequence = 0
        self.session_epoch_ms = None
        self.rx_bytes = 0
        self.tx_bytes = 0
        self.reconnect_count = 0
        self.replay_count = 0
        self.state_deadline_ms = 0
        self.inflight_sequence = 0
        self.inflight_boot_id = 0
        self.inflight_sent_ms = 0

    @staticmethod
    def _new_socket():
        value = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        value.setblocking(False)
        return value

    def _close(self, now_ms, error):
        if self.sock is not None:
            try:
                self.sock.close()
            except Exception:
                pass
        self.sock = None
        self._receive_into = None
        self.tx_view = None
        self.tx_offset = 0
        self.control_pending_length = 0
        self.inflight_sequence = 0
        self.replay_through = max(self.replay_through, self.highest_sent)
        self.state = BACKOFF
        self.next_action_ms = self.ticks_add(now_ms, 1000)
        self.last_error = str(error)

    def _queue_bytes(self, view, sequence=0):
        if self.tx_view is not None:
            return False
        self.tx_view = view
        self.tx_offset = 0
        self.tx_sequence = sequence
        return True

    def _send_step(self):
        if self.tx_view is None:
            return True
        try:
            started = self.clock_us() if self.clock_us is not None else 0
            sent = self.sock.send(self.tx_view[self.tx_offset:])
        except OSError as error:
            if (error.args[0] if error.args else None) in (11, 35, 10035):
                return False
            raise
        if self.send_metric is not None and self.clock_us is not None:
            self.send_metric(max(0, self.clock_us() - started))
        if sent is None or sent <= 0:
            return False
        self.tx_offset += sent
        self.tx_bytes += sent
        if self.tx_offset == len(self.tx_view):
            completed_sequence = self.tx_sequence
            self.highest_sent = max(self.highest_sent, self.tx_sequence)
            self.tx_view = None
            self.tx_offset = 0
            self.tx_sequence = 0
            if completed_sequence:
                self.inflight_sequence = completed_sequence
                self.inflight_sent_ms = self.clock_ms()
        return True

    def _build_hello(self):
        ranges = self.outbox.available_boot_ranges()
        length = hello_transcript(
            self.device_id, self.firmware, self.links, ranges,
            self.auth_buffer)
        mac = hmac_sha256(
            self.device_key, b"BMB1-AUTH", self.challenge,
            self.outbox.pico_boot_id.to_bytes(8, "big"),
            memoryview(self.auth_buffer)[:length])
        size = binary.write_hello(
            self.tx_buffer, 0, 0, self.outbox.pico_boot_id, self.device_id,
            self.firmware, self.links, ranges, mac)
        self._queue_bytes(memoryview(self.tx_buffer)[:size])

    def _receive(self, now_ms):
        try:
            if self._receive_into is None:
                self._receive_into = getattr(self.sock, "readinto", None)
                if self._receive_into is None:
                    self._receive_into = self.sock.recv_into
            count = self._receive_into(self.rx_buffer)
        except OSError as error:
            if (error.args[0] if error.args else None) in (11, 35, 10035):
                return True
            raise
        if count is None:
            return True
        if count == 0:
            return False
        self.last_rx_ms = now_ms
        self.rx_bytes += count
        self.parser.feed(memoryview(self.rx_buffer)[:count])
        while True:
            message = self.parser.next_message()
            if message is None:
                break
            self._handle_message(message)
        return True

    def _handle_message(self, message):
        if self.state == CHALLENGE_WAIT:
            self.challenge = bytes(binary.parse_challenge(message))
            self.session_key = hmac_sha256(
                self.device_key, b"BMB1-SESSION", self.challenge,
                self.outbox.pico_boot_id.to_bytes(8, "big"))
            self._build_hello()
            self.state = HELLO_SEND
            self.state_deadline_ms = self.ticks_add(self.clock_ms(), 5000)
            return
        if self.state == ACCEPT_WAIT:
            persisted, ack_timeout, ping_interval = \
                binary.parse_hello_accepted(message)
            self.outbox.acknowledge(self.outbox.pico_boot_id, persisted)
            self.ack_timeout_ms = min(max(ack_timeout, 1000), 60000)
            self.ping_interval_ms = min(max(ping_interval, 1000), 60000)
            self.session_epoch_ms = self.clock_ms()
            self.last_control_sequence = 0
            self.state = ONLINE
            self.state_deadline_ms = 0
            return
        if self.state == ONLINE and message.message_type == C.ACK:
            boot_id, watermark, _, _ = binary.parse_ack(message)
            self.outbox.acknowledge(boot_id, watermark)
            if (boot_id == self.inflight_boot_id and
                    watermark >= self.inflight_sequence):
                self.inflight_sequence = 0
        elif self.state == ONLINE and message.message_type == C.CONTROL:
            self._handle_control(message)
        elif self.state == ONLINE and message.message_type == C.PING:
            # struct.error is not in poll()'s except clause, so a short PING
            # payload would escape as an uncounted-for runtime exception.
            if len(message.payload) < 8:
                return
            token = struct.unpack_from(">Q", message.payload, 0)[0]
            size = binary.write_ping(
                self.tx_buffer, 0, C.PONG, self.outbox.pico_boot_id, token)
            self._queue_bytes(memoryview(self.tx_buffer)[:size])

    @staticmethod
    def _same(left, right):
        if len(left) != len(right):
            return False
        different = 0
        for index in range(len(left)):
            different |= left[index] ^ right[index]
        return different == 0

    def _handle_control(self, message):
        try:
            command_sequence, issued_at_us, ttl_ms, command, arguments, supplied = \
                binary.parse_control(message)
            unsigned_length = len(message.payload) - 32
            expected = hmac_sha256(
                self.session_key, message.header,
                message.payload[:unsigned_length])
            if not self._same(expected, supplied):
                result, detail = C.RESULT_UNAUTHENTICATED, b"bad hmac"
            elif (self.session_epoch_ms is None or
                  self.ticks_diff(self.clock_ms(),
                                  self.session_epoch_ms) * 1000 >
                  issued_at_us + ttl_ms * 1000):
                result, detail = C.RESULT_EXPIRED, b"expired"
            elif command_sequence <= self.last_control_sequence:
                result, detail = C.RESULT_REPLAYED, b"replayed"
            elif (command != C.CONTROL_SOFT_RESET or
                  self.control_handler is None):
                result, detail = C.RESULT_UNKNOWN_COMMAND, b"unsupported"
            else:
                self.last_control_sequence = command_sequence
                try:
                    returned = self.control_handler(
                        message.link_index, command, arguments)
                    result, detail = C.RESULT_OK, returned or b"accepted"
                except Exception:
                    result, detail = C.RESULT_UNSAFE, b"rejected"
        except binary.CodecError:
            return
        unsigned = bytearray(12 + len(detail))
        struct.pack_into(">QBBH", unsigned, 0, command_sequence, result, 0,
                         len(detail))
        unsigned[12:] = detail
        binary.write_header(
            self.control_buffer, 0, C.CONTROL_RESULT, 0,
            len(unsigned) + 32, 0, self.outbox.pico_boot_id,
            message.link_index)
        mac = hmac_sha256(
            self.session_key,
            memoryview(self.control_buffer)[:C.HEADER_SIZE], unsigned)
        size = binary.write_control_result(
            self.control_buffer, 0, 0, 0,
            self.outbox.pico_boot_id, message.link_index, command_sequence,
            result, detail, mac)
        self.control_pending_length = size

    def attach_connected_socket(self, sock):
        """Test/embedded hook after a nonblocking connect has completed."""
        self.sock = sock
        self._receive_into = None
        self.state = CHALLENGE_WAIT
        self.state_deadline_ms = self.ticks_add(self.clock_ms(), 5000)

    def set_device_key(self, device_key, now_ms):
        """Replace the authentication key and reconnect without a reboot."""
        if len(device_key) != 32:
            raise ValueError("device key must be 32 bytes")
        self.device_key = bytes(device_key)
        if self.sock is not None:
            self._close(now_ms, "device key updated")
        else:
            self.state = WIFI_WAIT
            self.next_action_ms = 0

    def set_endpoint(self, host, port, now_ms):
        """Replace the TCP endpoint and reconnect without a reboot."""
        if not host or int(port) < 1 or int(port) > 65535:
            raise ValueError("invalid endpoint")
        self.host = host
        self.port = int(port)
        if self.sock is not None:
            self._close(now_ms, "transport endpoint updated")
        else:
            self.state = WIFI_WAIT
            self.next_action_ms = 0

    def poll(self, now_ms, wifi_online=True):
        if not wifi_online:
            if self.sock is not None:
                self._close(now_ms, "wifi offline")
            self.state = WIFI_WAIT
            return
        if self.state in (WIFI_WAIT, BACKOFF):
            if self.ticks_diff(now_ms, self.next_action_ms) < 0:
                return
            try:
                self.sock = self.socket_factory()
                self.reconnect_count += 1
                try:
                    self.sock.setblocking(False)
                except AttributeError:
                    pass
                try:
                    self.sock.connect((self.host, self.port))
                    self.state = CHALLENGE_WAIT
                    self.state_deadline_ms = self.ticks_add(now_ms, 5000)
                except OSError as error:
                    code = error.args[0] if error.args else None
                    if code not in (11, 36, 10035, 115, 119):
                        raise
                    self._connect_pending = True
                    self.state = CONNECTING
            except OSError as error:
                self._close(now_ms, error)
                return
        if self.state == CONNECTING:
            try:
                pending_error = self.sock.getsockopt(
                    socket.SOL_SOCKET, socket.SO_ERROR)
            except (AttributeError, OSError):
                pending_error = 0
            if pending_error:
                self._close(now_ms, pending_error)
                return
            self._connect_pending = False
            self.state = CHALLENGE_WAIT
            self.state_deadline_ms = self.ticks_add(now_ms, 5000)
        if (self.state in (CHALLENGE_WAIT, HELLO_SEND, ACCEPT_WAIT) and
                self.ticks_diff(now_ms, self.state_deadline_ms) >= 0):
            self._close(now_ms, "handshake timeout")
            return
        try:
            alive = self._receive(now_ms)
        except (OSError, binary.CodecError) as error:
            self._close(now_ms, error)
            return
        if not alive:
            self._close(now_ms, "peer closed")
            return
        if self.state == HELLO_SEND:
            try:
                sent = self._send_step()
            except OSError as error:
                self._close(now_ms, error)
                return
            if sent and self.tx_view is None:
                self.state = ACCEPT_WAIT
        elif self.state == ONLINE:
            if self.tx_view is not None:
                try:
                    self._send_step()
                except OSError as error:
                    self._close(now_ms, error)
                return
            if self.control_pending_length:
                self._queue_bytes(memoryview(
                    self.control_buffer)[:self.control_pending_length])
                self.control_pending_length = 0
                return
            if self.inflight_sequence:
                if self.ticks_diff(
                        now_ms, self.inflight_sent_ms) < self.ack_timeout_ms:
                    return
                self.inflight_sequence = 0
            if self.last_rx_ms and self.ticks_diff(
                    now_ms, self.last_rx_ms) > self.ping_interval_ms * 2:
                self._close(now_ms, "liveness timeout")
                return
            current = self.outbox.peek()
            if current is not None:
                sequence, record, _, _ = current
                self.inflight_boot_id = struct.unpack_from(">Q", record, 20)[0]
                if sequence <= self.replay_through:
                    self.tx_buffer[:len(record)] = record
                    flags = struct.unpack_from(">H", self.tx_buffer, 6)[0]
                    struct.pack_into(">H", self.tx_buffer, 6,
                                     flags | C.FLAG_REPLAY)
                    record = memoryview(self.tx_buffer)[:len(record)]
                    self.replay_count += 1
                self._queue_bytes(record, sequence)
                # Same guard as the other two _send_step call sites: an
                # unwrapped OSError here escapes to main.py and is counted as a
                # runtime exception on every reconnect instead of closing.
                try:
                    self._send_step()
                except OSError as error:
                    self._close(now_ms, error)
                    return
