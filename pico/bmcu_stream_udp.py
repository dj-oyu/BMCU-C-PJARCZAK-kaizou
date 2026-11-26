"""BMS2: unacknowledged UDP stream of BMB1 records to the receiver.

The contract is docs/BMCU_MONITOR_STREAM_V2.md. In short: records leave the
outbox in sequence order, packed into datagrams of at most 1200 bytes, and are
released as soon as the socket accepts the datagram. Nothing comes back. A
HEARTBEAT every few seconds carries identity and liveness.

The bridge forwards what it was given. Nothing here looks inside a record.
"""

try:
    import usocket as socket
except ImportError:
    import socket
try:
    import ustruct as struct
except ImportError:
    import struct

import bmcu_binary as binary
import bmcu_binary_constants as C

MAGIC = b"BMS2"
VERSION = 1
MAX_DATAGRAM = 1200
MAX_RECORDS = 255
MAX_DEVICE_ID = 63
MAX_SHORT_TEXT = 31

# Uplink states, as the OLED header and health page print them:
# no endpoint configured; Wi-Fi down; endpoint known but not resolved yet;
# the last send was accepted by the socket; the last send failed and is
# retried after a backoff.
DISABLED = "disabled"
WIFI_WAIT = "wifi_wait"
RESOLVING = "resolving"
ONLINE = "online"
SEND_ERROR = "send_error"
STATES = (DISABLED, WIFI_WAIT, RESOLVING, ONLINE, SEND_ERROR)

# Errors that mean "try the same datagram again shortly": EAGAIN/EWOULDBLOCK,
# ENOMEM (lwIP out of pbufs) and ENOBUFS, on MicroPython and CPython hosts.
_RETRY_ERRNOS = (11, 12, 35, 55, 105, 10035, 10055)
ERROR_BACKOFF_MS = 1000


def _check_text(value, maximum, name):
    if not 1 <= len(value) <= maximum:
        raise ValueError("%s must be 1..%d bytes" % (name, maximum))
    return value


def envelope_size(device_id):
    return 8 + len(device_id)


def write_envelope(out, device_id, record_count):
    """The 8 + N byte BMS2 envelope header (spec section 4)."""
    out[0:4] = MAGIC
    out[4] = VERSION
    out[5] = 0
    out[6] = len(device_id)
    end = 7 + len(device_id)
    out[7:end] = device_id
    out[end] = record_count
    return end + 1


def heartbeat_payload(interval_ms, uptime_ms, next_sequence, firmware, links):
    """HEARTBEAT payload (spec section 5.2).

    links: iterable of (link_index, link_id bytes, link_state code).
    """
    links = tuple(links)
    size = 21 + len(firmware) + 1
    for _, link_id, _ in links:
        size += 3 + len(link_id)
    out = bytearray(size)
    struct.pack_into(">IQQ", out, 0, interval_ms & 0xFFFFFFFF,
                     uptime_ms, next_sequence)
    out[20] = len(firmware)
    pos = 21
    out[pos:pos + len(firmware)] = firmware
    pos += len(firmware)
    out[pos] = len(links)
    pos += 1
    for index, link_id, state in links:
        out[pos] = index
        out[pos + 1] = len(link_id)
        pos += 2
        out[pos:pos + len(link_id)] = link_id
        pos += len(link_id)
        out[pos] = state
        pos += 1
    return out


class BMS2UDPClient:
    def __init__(self, outbox, host, port, device_id, firmware, links,
                 link_states=None, uptime_ms=None, clock_ms=None,
                 socket_factory=None, resolver=None, heartbeat_ms=5000,
                 datagrams_per_loop=4, ticks_diff=None, ticks_add=None):
        self.outbox = outbox
        self.host = host
        self.port = int(port) if port else 0
        self.device_id = _check_text(bytes(device_id), MAX_DEVICE_ID,
                                     "device_id")
        self.firmware = _check_text(bytes(firmware), MAX_SHORT_TEXT,
                                    "firmware")
        self.links = tuple((index, _check_text(bytes(link_id), MAX_SHORT_TEXT,
                                               "link_id"))
                           for index, link_id in links)
        # () -> sequence of link_state codes, one per entry of `links`.
        self.link_states = link_states or (lambda: (0,) * len(self.links))
        self.uptime_ms = uptime_ms or (lambda: 0)
        self.clock_ms = clock_ms or (lambda: 0)
        self.socket_factory = socket_factory or self._new_socket
        self.resolver = resolver or self._resolve
        self.heartbeat_ms = int(heartbeat_ms)
        self.datagrams_per_loop = max(1, int(datagrams_per_loop))
        self.ticks_diff = ticks_diff or (lambda left, right: left - right)
        self.ticks_add = ticks_add or (lambda value, delta: value + delta)
        self.state = DISABLED if not self.configured else WIFI_WAIT
        self.sock = None
        self.address = None
        self.buffer = bytearray(MAX_DATAGRAM)
        self.heartbeat_buffer = bytearray(MAX_DATAGRAM)
        # A built datagram whose send was refused with a retry errno. Its
        # records have already left the outbox, so it must go out before
        # anything else is built.
        self.pending = None
        self.pending_records = 0
        self.last_heartbeat_ms = None
        self.retry_at_ms = None
        self.last_error = None
        # Counters (PICO_DIAGNOSTIC, spec section 9).
        self.datagrams_sent = 0
        self.records_sent = 0
        self.bytes_sent = 0
        self.send_errors = 0
        self.heartbeats_sent = 0
        self.oversize_records = 0
        self.reconnect_count = 0
        # v1 diagnostic fields device_metrics still reads; always 0 on v2.
        self.rx_bytes = 0
        self.replay_count = 0

    @property
    def tx_bytes(self):
        return self.bytes_sent

    @property
    def configured(self):
        return bool(self.host and self.port)

    @staticmethod
    def _new_socket():
        value = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            value.setblocking(False)
        except AttributeError:
            pass
        return value

    @staticmethod
    def _resolve(host, port):
        return socket.getaddrinfo(host, port, 0, socket.SOCK_DGRAM)[0][-1]

    def _close(self):
        if self.sock is not None:
            try:
                self.sock.close()
            except Exception:
                pass
        self.sock = None
        self.address = None

    def set_endpoint(self, host, port, now_ms=None):
        """Replace the destination without a reboot (local UI)."""
        if not host or int(port) < 1 or int(port) > 65535:
            raise ValueError("invalid endpoint")
        self.host = host
        self.port = int(port)
        self._close()
        self.retry_at_ms = None
        self.last_heartbeat_ms = None
        self.state = WIFI_WAIT

    # -- building --------------------------------------------------------

    def _build_records(self):
        """Pack queued records into self.buffer; returns (view, count)."""
        out = self.buffer
        offset = envelope_size(self.device_id)
        count = 0
        outbox = self.outbox
        while count < MAX_RECORDS:
            current = outbox.peek()
            if current is None:
                break
            sequence, record = current[0], current[1]
            size = len(record)
            if size > MAX_DATAGRAM - envelope_size(self.device_id):
                # Cannot travel in any datagram. Nothing this size exists
                # today; counted rather than wedging the queue behind it.
                self.oversize_records += 1
                outbox.acknowledge(outbox.pico_boot_id, sequence)
                continue
            if offset + size > MAX_DATAGRAM:
                break
            out[offset:offset + size] = record
            offset += size
            count += 1
            outbox.acknowledge(outbox.pico_boot_id, sequence)
        if not count:
            return None, 0
        write_envelope(out, self.device_id, count)
        return memoryview(out)[:offset], count

    def _build_heartbeat(self):
        states = tuple(self.link_states())
        links = tuple((index, link_id, states[position]
                       if position < len(states) else 0)
                      for position, (index, link_id) in enumerate(self.links))
        payload = heartbeat_payload(
            self.heartbeat_ms, self.uptime_ms(), self.outbox.next_sequence,
            self.firmware, links)
        out = self.heartbeat_buffer
        start = envelope_size(self.device_id)
        size = binary.write_message(
            out, start, C.HEARTBEAT, 0, 0, self.outbox.pico_boot_id,
            C.GLOBAL_SCOPE, payload)
        write_envelope(out, self.device_id, 1)
        return memoryview(out)[:start + size]

    # -- sending ---------------------------------------------------------

    def _send(self, view, now_ms):
        """True: sent. False: retry later. Raises on a hard failure."""
        try:
            sent = self.sock.sendto(view, self.address)
        except OSError as error:
            code = error.args[0] if error.args else None
            if code in _RETRY_ERRNOS:
                return False
            raise
        if sent is not None and sent != len(view):
            # UDP sends whole datagrams or nothing; a short count is a stack
            # refusing it, so treat it like a refusal.
            return False
        self.datagrams_sent += 1
        self.bytes_sent += len(view)
        self.state = ONLINE
        return True

    def _fail(self, now_ms, error):
        self.send_errors += 1
        self.last_error = str(error)
        self.state = SEND_ERROR
        self._close()
        self.retry_at_ms = self.ticks_add(now_ms, ERROR_BACKOFF_MS)

    def _ensure_socket(self, now_ms):
        if self.sock is not None:
            return True
        if self.retry_at_ms is not None and \
                self.ticks_diff(now_ms, self.retry_at_ms) < 0:
            return False
        self.retry_at_ms = None
        self.state = RESOLVING
        try:
            self.address = self.resolver(self.host, self.port)
            self.sock = self.socket_factory()
            self.reconnect_count += 1
        except OSError as error:
            self._fail(now_ms, error)
            return False
        return True

    def poll(self, now_ms, wifi_online=True):
        if not self.configured:
            self.state = DISABLED
            return
        if not wifi_online:
            if self.sock is not None:
                self._close()
            self.state = WIFI_WAIT
            # A fresh heartbeat is owed as soon as Wi-Fi returns.
            self.last_heartbeat_ms = None
            return
        if not self._ensure_socket(now_ms):
            return
        try:
            # The heartbeat goes in a datagram of its own, ahead of the
            # backlog, so that a long queue cannot delay liveness.
            if self.last_heartbeat_ms is None or self.ticks_diff(
                    now_ms, self.last_heartbeat_ms) >= self.heartbeat_ms:
                if self._send(self._build_heartbeat(), now_ms):
                    self.heartbeats_sent += 1
                    self.last_heartbeat_ms = now_ms
            for _ in range(self.datagrams_per_loop):
                if self.pending is None:
                    view, count = self._build_records()
                    if view is None:
                        break
                    self.pending, self.pending_records = view, count
                if not self._send(self.pending, now_ms):
                    break
                self.records_sent += self.pending_records
                self.pending, self.pending_records = None, 0
        except OSError as error:
            # The pending datagram stays: its records already left the
            # outbox, and a fresh socket after the backoff may still send it.
            self._fail(now_ms, error)
