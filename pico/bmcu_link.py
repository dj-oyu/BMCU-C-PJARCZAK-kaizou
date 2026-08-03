"""Bounded MicroPython decoder for BMCU Link protocol alpha.3 (0x83)."""

try:
    import ustruct as struct
except ImportError:  # Lets the decoder be unit-tested with CPython.
    import struct
try:
    import utime as time
except ImportError:
    import time


SYNC = b"\xa5\x5a"
MAX_DECODER_BUFFER = 128
VERSION_ALPHA3 = 0x83
MAX_PAYLOAD = 57
# The alpha wire is lock-step: BMCU and monitor ship together and the transport
# contract states backward compatibility is not required, so this is an exact
# length rather than a minimum with a tolerated legacy 27.
STATUS_PAYLOAD_SIZE = 31
# 27 is the pre-channel-flags encoding. Both are accepted so the bridge and the
# BMCU can be updated independently: requiring 31 exactly means two BMCUs and a
# Pico have to be flashed in lockstep, and flashing a BMCU carries a real risk
# of a board that looks bricked. Any other length is still invalid.
STATUS_PAYLOAD_SIZE_LEGACY = 27
STATUS_PAYLOAD_SIZES = (STATUS_PAYLOAD_SIZE_LEGACY, STATUS_PAYLOAD_SIZE)

HELLO = 0x01
STATUS = 0x02
EVENT = 0x03
GET_STATUS = 0x10
SET_LED_MODE = 0x11
PING = 0x12
GET_FULL_STATUS = 0x17
REQUEST_SOFT_RESET = 0x18
PONG = 0x72
FULL_STATUS_RECORD = 0x73
ACK = 0x7f

FULL_RECORD_PRINTER_AUTH = 5
FULL_RECORD_PRINTER_RX_CORE = 6
FULL_RECORD_PRINTER_RX_LOSS = 7
FULL_RECORD_PRINTER_RX_DMA = 8
FULL_RECORD_PRINTER_TX_CORE = 9
FULL_RECORD_PRINTER_TX_FAULT = 10
FULL_RECORD_AMS_SERVICE = 11
FULL_RECORD_AMS_REGISTRATION = 12
FULL_RECORD_PROBE = 13
RECORD_PRINTER_TRANSACTION = 3
RECORD_PRINTER_LONG_TRANSACTION = 9
RECORD_RESET_STATE = 10


def crc16_ccitt_false(data):
    crc = 0xffff
    for value in data:
        crc ^= value << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xffff if crc & 0x8000 else (crc << 1) & 0xffff
    return crc


def _u16(data, offset):
    return data[offset] | (data[offset + 1] << 8)


def _u32(data, offset):
    return (data[offset] | (data[offset + 1] << 8) | (data[offset + 2] << 16) |
            (data[offset + 3] << 24))


def _i16(data, offset):
    value = _u16(data, offset)
    return value - 0x10000 if value & 0x8000 else value


# Per-channel fault/switch byte, STATUS offset 27 and channel-record flag bits
# 8..12. Shifted explicitly here rather than mirrored from the firmware union
# in src/bmcu_link.h, whose bitfield order the compiler picks; only the raw
# byte is contract. See docs/bmcu_wire_layout.json.
def decode_channel_flags(raw):
    return {
        # 0 none, 1 both switches, 2 external only, 3 internal only. Builds
        # without the DM dual microswitch only ever report 0 or 1.
        "ks": raw & 0x03,
        # Pull fell below 40% during pressure control on use; motor latched off.
        "low_latch": bool(raw & 0x04),
        # The jam variant of low_latch, which also raises HMS 0xF06F.
        "jam_latch": bool(raw & 0x08),
        # DM autoload stage 1 or 2 failed. Clears only on a full withdrawal.
        "dm_fail_latch": bool(raw & 0x10),
        "raw": raw,
    }

def _i32(data, offset):
    value = _u32(data, offset)
    return value - 0x100000000 if value & 0x80000000 else value


REJECT_CRC = 1
REJECT_LENGTH = 2
REJECT_OVERSIZED = 3

CAPTURE_SLOTS = 8
CAPTURE_BYTES = 72


class RejectCapture:
    """Keeps the most recent rejected byte runs so they can be inspected.

    The decoder used to discard malformed input silently, leaving only counters,
    which cannot distinguish a single flipped bit from a truncated frame. Storage
    is preallocated because this runs inside the UART drain path.
    """

    def __init__(self, slots=CAPTURE_SLOTS, size=CAPTURE_BYTES):
        self.slots = slots
        self.size = size
        self.storage = bytearray(slots * size)
        self.lengths = bytearray(slots)
        self.reasons = bytearray(slots)
        self.timestamps = [0] * slots
        self.ordinals = [0] * slots
        self.captured = 0
        self._next = 0

    def add(self, reason, data, now_ms=0):
        count = len(data)
        if count > self.size:
            count = self.size
        start = self._next * self.size
        self.storage[start:start + count] = data[:count]
        self.lengths[self._next] = count
        self.reasons[self._next] = reason
        self.timestamps[self._next] = now_ms
        self.captured += 1
        self.ordinals[self._next] = self.captured
        self._next = (self._next + 1) % self.slots

    def records(self):
        """Oldest first, so a reader sees the runs in the order they arrived."""
        for offset in range(self.slots):
            index = (self._next + offset) % self.slots
            if not self.ordinals[index]:
                continue
            start = index * self.size
            yield (self.ordinals[index], self.reasons[index],
                   self.timestamps[index],
                   memoryview(self.storage)[start:start + self.lengths[index]])


class FrameDecoder:
    """Consumes arbitrary UART chunks and emits only valid bounded frames."""

    def __init__(self, capture=None):
        self._buffer = bytearray()
        self.crc_errors = 0
        self.frame_errors = 0
        # Bytes dropped as unparseable noise. Kept apart from frame_errors so a
        # bad length byte in an otherwise healthy stream stays distinguishable
        # from a stream the decoder cannot make sense of at all.
        self.discarded_bytes = 0
        self.capture = capture
        self.clock_ms = 0

    def _reject(self, reason, data):
        if self.capture is not None:
            self.capture.add(reason, data, self.clock_ms)

    def feed(self, data, on_valid_wire=None, on_frame=None):
        """Parse every byte handed in; retain only an unparsed remainder.

        This used to drop all but the last MAX_DECODER_BUFFER bytes of any
        larger chunk, before parsing. With a 512-byte drain chunk against a
        128-byte bound that silently discarded up to 384 bytes of intact frames
        per read, which showed up as CRC errors on whatever frame straddled the
        cut. The noise bound belongs on the *unparsed remainder*, which is at
        most one legal frame, not on the arriving chunk.
        """
        frames = []
        view = memoryview(data)
        for start in range(0, len(view), MAX_DECODER_BUFFER) or (0,):
            self._buffer.extend(view[start:start + MAX_DECODER_BUFFER])
            self._parse(frames, on_valid_wire, on_frame)
            if len(self._buffer) > MAX_DECODER_BUFFER:
                # Nothing legal can be this long once complete frames have been
                # consumed, so the head is noise rather than a partial frame.
                self.discarded_bytes += len(self._buffer) - MAX_DECODER_BUFFER
                self._reject(REJECT_OVERSIZED,
                             self._buffer[:CAPTURE_BYTES])
                self._buffer = self._buffer[-MAX_DECODER_BUFFER:]
        return frames

    def _parse(self, frames, on_valid_wire, on_frame):
        while True:
            start = self._buffer.find(SYNC)
            if start < 0:
                # Retain a possible leading sync byte for the next UART read.
                if self._buffer[-1:] == b"\xa5":
                    self._buffer = self._buffer[-1:]
                else:
                    self._buffer = bytearray()
                break
            if start:
                self._buffer = self._buffer[start:]
            if len(self._buffer) < 7:
                break
            payload_length = self._buffer[6]
            if payload_length > MAX_PAYLOAD:
                self.frame_errors += 1
                # Capture the surrounding run, not just the header: a bad length
                # byte is usually the tail of an earlier desync.
                self._reject(REJECT_LENGTH, self._buffer[:CAPTURE_BYTES])
                self._buffer = self._buffer[1:]
                continue
            wire_length = payload_length + 9
            if len(self._buffer) < wire_length:
                break
            body = self._buffer[2:wire_length - 2]
            expected_crc = _u16(self._buffer, wire_length - 2)
            if crc16_ccitt_false(body) != expected_crc:
                self.crc_errors += 1
                # The whole candidate frame, so a reader can recompute the CRC
                # offline and tell a flipped bit from a truncated frame.
                self._reject(REJECT_CRC, self._buffer[:wire_length])
                self._buffer = self._buffer[1:]
                continue
            wire = bytes(self._buffer[:wire_length])
            if on_valid_wire is not None:
                on_valid_wire(memoryview(wire), body[1])
            frame = {
                "version": body[0], "kind": body[1], "sequence": _u16(body, 2),
                "payload": bytes(body[5:]),
            }
            if on_frame is not None:
                on_frame(frame)
            else:
                frames.append(frame)
            self._buffer = self._buffer[wire_length:]


def encode_frame(kind, sequence, payload=b""):
    if len(payload) > MAX_PAYLOAD:
        raise ValueError("BMCU Link payload exceeds 57 bytes")
    body = bytearray((VERSION_ALPHA3, kind, sequence & 0xff, (sequence >> 8) & 0xff, len(payload)))
    body.extend(payload)
    crc = crc16_ccitt_false(body)
    return SYNC + bytes(body) + bytes((crc & 0xff, crc >> 8))


class BMCUMonitor:
    """Protocol state machine; ``on_message`` receives typed dictionaries."""
    SNAPSHOT_TIMEOUT_MS = 1200
    # A snapshot describes motor state, which can change in milliseconds, so
    # the soft-reset gate treats anything older than this as unusable. It is
    # comfortably longer than SNAPSHOT_TIMEOUT_MS so a refresh requested on a
    # refused attempt has time to complete before the caller retries.
    MAX_SNAPSHOT_AGE_MS = 3000
    # filament_motion_enum values a soft reset may be requested from. Mirrors
    # kMotionParked in the firmware's Motion_control.cpp, which carries the
    # per-state reasoning; keep the two in step.
    #
    # These are controller phases. The AMS-side _filament_motion enum in ams.h
    # reuses the same numbers for different states -- 3 is stop here and
    # before_pull_back there -- so read them against the right enum.
    MOTION_STOP = 3
    MOTION_PRESSURE_CTRL_IDLE = 7
    RESET_IDLE_MOTIONS = (MOTION_STOP, MOTION_PRESSURE_CTRL_IDLE)
    SNAPSHOT_MAX_RETRIES = 3
    OUTSTANDING_GET_STATUS_TTL_MS = 3000


    def __init__(self, uart, on_message=None, link_id="bmcu-a",
                 link_index=0, on_valid_frame=None, uart_capacity=None,
                 capture_slots=CAPTURE_SLOTS):
        self.uart = uart
        self.on_message = on_message
        self.capture = RejectCapture(capture_slots) if capture_slots else None
        self.decoder = FrameDecoder(self.capture)
        self.link_id = link_id
        self.link_index = link_index
        self.on_valid_frame = on_valid_frame
        self.uart_capacity = uart_capacity
        self.next_sequence = 1
        self.last_valid_ms = None
        self.last_ping_ms = None
        self.tick_hz = None
        self.status = None
        self.snapshot = None
        self.snapshot_at_ms = None
        self.channels = [None, None, None, None]
        self.printer_auth = None
        self.printer_rx = None
        self.printer_tx = None
        self.ams_service = None
        self.ams_registration = None
        self.soft_reset = None
        self._snapshot_parts = None
        self._snapshot_count = 0
        self._snapshot_id = None
        self.events = []
        self.sensors = {}

        self._snapshot_deadline_ms = None
        self._snapshot_retry_ms = None
        self._snapshot_retries = 0
        self._last_unsolicited_sequence = None
        self._outstanding_get_status = []
        self._last_hw_tick32 = None
        self._hw_tick_epoch = 0
        self.bmcu_boot_session = 0
        self.link_state = "stale"
        self._clock_ms = 0
        self.uart_backlog = 0
        self.uart_max_backlog = 0
        self.uart_drain_bytes = 0
        self.uart_overflow_count = 0
        self.uart_max_service_gap_us = 0
        self._last_uart_service_ms = None
        self.sequence_gap_count = 0
        self._overflow_latched = False
    def _emit(self, message):
        message.setdefault("link_id", self.link_id)
        if self.on_message:
            self.on_message(message)

    def _send(self, kind, payload=b""):
        sequence = self.next_sequence
        self.next_sequence = (sequence + 1) & 0xffff
        self.uart.write(encode_frame(kind, sequence, payload))
        return sequence

    @staticmethod
    def _ticks_diff(now, then):
        return time.ticks_diff(now, then) if hasattr(time, "ticks_diff") else now - then

    @staticmethod
    def _ticks_add(base, delta):
        return time.ticks_add(base, delta) if hasattr(time, "ticks_add") else base + delta

    def get_status(self):
        sequence = self._send(GET_STATUS)
        self._prune_outstanding_get_status(self._clock_ms)
        self._outstanding_get_status.append((sequence, self._clock_ms))
        if len(self._outstanding_get_status) > 4:
            self._outstanding_get_status.pop(0)
        return sequence

    def _prune_outstanding_get_status(self, now_ms):
        self._outstanding_get_status = [
            entry for entry in self._outstanding_get_status
            if self._ticks_diff(now_ms, entry[1]) < self.OUTSTANDING_GET_STATUS_TTL_MS
        ]

    def get_full_status(self):
        sequence = self._send(GET_FULL_STATUS, b"\x0f\x0f")
        self._snapshot_deadline_ms = self._ticks_add(self._clock_ms, self.SNAPSHOT_TIMEOUT_MS)
        return sequence

    def ping(self, token):
        return self._send(PING, struct.pack("<I", token & 0xffffffff))

    def soft_reset_guard_error(self, now_ms=None):
        """Why a soft reset must be refused, or None if it may proceed.

        The idle test needs motor PWM and controller phase, which only the
        FULL_STATUS snapshot carries; live STATUS has neither. A snapshot is
        requested only when the baseline is invalidated, so in steady operation
        it can be arbitrarily old, and this gate used to claim freshness it
        never checked: a snapshot taken while idle would keep permitting a reset
        long after motion had started.

        This is a pre-check, not the decision. The BMCU re-evaluates every
        request in Motion_control_is_reset_safe() with state the link never
        carries -- the DM autoload gate needs the tri-state microswitch reading
        and dm_loaded, and STATUS only has one online bit per channel. So the
        rule here is to refuse what is plainly moving and leave the rest to the
        side that can prove it. A gate stricter than the BMCU's would only
        relocate the refusal.

        controller_motion 7 (pressure_ctrl_idle) is where a loaded channel
        rests. Requiring 3 (stop) made this unreachable with filament loaded,
        which is the state 0500_409D recovery starts from.

        Pure by design. The caller decides whether to request a refresh.
        """
        if self.link_state != "online" or self.snapshot is None:
            return "complete fresh BMCU status is required"
        if any(channel is None for channel in self.channels):
            return "complete channel status is required"
        if now_ms is not None:
            age = (self.MAX_SNAPSHOT_AGE_MS if self.snapshot_at_ms is None
                   else self._ticks_diff(now_ms, self.snapshot_at_ms))
            if age >= self.MAX_SNAPSHOT_AGE_MS or age < 0:
                return "BMCU status is stale; retry once it refreshes"
        if any(channel["motor_pwm"] != 0 or
               channel["controller_motion"] not in self.RESET_IDLE_MOTIONS or
               channel["ams_motion"] != 0 for channel in self.channels):
            return "BMCU motion is not idle"
        return None

    def refresh_snapshot_if_idle(self):
        """Ask for a new FULL_STATUS unless one is already on its way."""
        if (self._snapshot_parts is not None or
                self._snapshot_deadline_ms is not None or
                self._snapshot_retry_ms is not None):
            return False
        self.get_full_status()
        return True

    def request_snapshot_refresh(self, now_ms=None):
        """Force a refresh, cancelling a pending retry backoff.

        refresh_snapshot_if_idle declines whenever a request is outstanding or
        a retry is scheduled, which is right for a background refresh and wrong
        for an operator who has just reproduced a fault. A retry backoff grows
        to seconds, and during it the served snapshot silently keeps answering
        with whatever it last held -- for as long as the fault persists, which
        is exactly when it is being read.

        Returns True when a request went out. A request already in flight is
        left alone and reported as False: it will answer the caller anyway.
        """
        if self._snapshot_parts is not None or self._snapshot_deadline_ms is not None:
            return False
        self._snapshot_retry_ms = None
        self._snapshot_retries = 0
        self.get_full_status()
        return True

    def snapshot_age_ms(self, now_ms=None):
        """Age of the held snapshot, saturating at 65535.

        65535 also stands for "never taken" and for "no clock yet". Both mean
        the same thing to a reader: do not trust this as current.
        """
        if self.snapshot is None or self.snapshot_at_ms is None:
            return 0xFFFF
        if now_ms is None:
            now_ms = self._clock_ms
        if now_ms is None:
            return 0xFFFF
        age = self._ticks_diff(now_ms, self.snapshot_at_ms)
        if age < 0 or age >= 0xFFFF:
            return 0xFFFF
        return age

    def request_soft_reset(self, operation_id, reason=0, ttl_ms=5000):
        if not 1 <= operation_id <= 0xffffffff:
            raise ValueError("operation_id must be a non-zero u32")
        if not 0 <= reason <= 2 or not 1 <= ttl_ms <= 5000:
            raise ValueError("invalid soft reset request")
        sequence = self._send(
            REQUEST_SOFT_RESET,
            struct.pack("<IBBH", operation_id, reason, 0, ttl_ms))
        self.soft_reset = {
            "operation_id": operation_id, "reason": reason, "ttl_ms": ttl_ms,
            "sequence": sequence, "state": "requested", "ack_result": None,
        }
        return sequence

    def ping_if_idle(self, now_ms, interval_ms=2000):
        """Probe only when no valid frame has demonstrated liveness recently."""
        if self.last_ping_ms is None:
            self.last_ping_ms = now_ms
            return False
        if self._ticks_diff(now_ms, self.last_ping_ms) < interval_ms:
            return False
        if (self.last_valid_ms is not None and
                self._ticks_diff(now_ms, self.last_valid_ms) < interval_ms):
            return False
        self.ping(now_ms)
        self.last_ping_ms = now_ms
        return True
    def set_led_mode(self, mode, timeout_s):
        if not 0 <= mode <= 0xff or not 0 <= timeout_s <= 0xffff:
            raise ValueError("invalid LED mode or timeout")
        return self._send(SET_LED_MODE, bytes((mode, timeout_s & 0xff, timeout_s >> 8)))

    def _request_missing_baseline(self):
        if self.status is None:
            self.get_status()
        if (self.snapshot is None and self._snapshot_parts is None and
                self._snapshot_deadline_ms is None and self._snapshot_retry_ms is None):
            self.get_full_status()

    def _invalidate_baseline(self, now_ms, reason):
        self.status = None
        self.snapshot = None
        self.snapshot_at_ms = None
        self.channels = [None, None, None, None]
        self.printer_auth = None
        self.printer_rx = None
        self.printer_tx = None
        self.ams_service = None
        self.ams_registration = None
        self._snapshot_parts = None
        self._snapshot_id = None
        self._snapshot_count = 0
        self._snapshot_deadline_ms = None
        self._snapshot_retries = 0
        self.link_state = "resyncing"
        self._emit({"type": "resync", "reason": reason})
        self.get_status()
        self.get_full_status()

    def _schedule_snapshot_retry(self, now_ms):
        self._snapshot_deadline_ms = None
        delay_ms = 250 << min(self._snapshot_retries, 4)
        self._snapshot_retry_ms = self._ticks_add(now_ms, delay_ms)

    def _service_snapshot_timeout(self, now_ms):
        if self._snapshot_deadline_ms is not None:
            if self._ticks_diff(now_ms, self._snapshot_deadline_ms) >= 0:
                self._snapshot_parts = None
                self._snapshot_id = None
                self._snapshot_retries += 1
                self._emit({"type": "snapshot_error", "reason": "timeout"})
                self._schedule_snapshot_retry(now_ms)
        if self._snapshot_retry_ms is not None and self._ticks_diff(now_ms, self._snapshot_retry_ms) >= 0:
            self._snapshot_retry_ms = None
            if self._snapshot_retries <= self.SNAPSHOT_MAX_RETRIES:
                self.get_full_status()
            else:
                self.link_state = "stale"
                self._snapshot_retries = 0
                self._emit({"type": "snapshot_error", "reason": "retry_exhausted"})

    def _extend_hw_tick(self, tick32, message):
        if self._last_hw_tick32 is not None and tick32 < self._last_hw_tick32:
            if self._last_hw_tick32 - tick32 > 0x80000000:
                self._hw_tick_epoch += 1 << 32
            else:
                self._hw_tick_epoch = 0
                message["tick_epoch_reset"] = True
        self._last_hw_tick32 = tick32
        message["hw_tick64"] = self._hw_tick_epoch + tick32

    def poll_uart(self, now_ms, max_bytes=MAX_DECODER_BUFFER):
        backlog = self.uart.any()
        self.uart_backlog = backlog
        self.uart_max_backlog = max(self.uart_max_backlog, backlog)
        at_capacity = (self.uart_capacity is not None and
                       backlog >= self.uart_capacity)
        if at_capacity and not self._overflow_latched:
            self.uart_overflow_count += 1
        self._overflow_latched = at_capacity
        if self._last_uart_service_ms is not None:
            gap_us = self._ticks_diff(
                now_ms, self._last_uart_service_ms) * 1000
            self.uart_max_service_gap_us = max(
                self.uart_max_service_gap_us, gap_us)
        self._last_uart_service_ms = now_ms
        available = min(backlog, max_bytes)
        self._clock_ms = now_ms
        self.decoder.clock_ms = now_ms
        if available:
            data = self.uart.read(available)
            if data:
                received_at_us = now_ms * 1000

                def accepted(wire, kind):
                    if self.on_valid_frame is not None:
                        self.on_valid_frame(
                            self.link_index, received_at_us, wire, kind)

                self.decoder.feed(
                    data, accepted,
                    lambda frame: self._handle_frame(frame, now_ms))
                self.uart_drain_bytes += len(data)
                self.uart_backlog = self.uart.any()
                return len(data)
        return 0

    def service_maintenance(self, now_ms):
        self._clock_ms = now_ms
        self._service_snapshot_timeout(now_ms)
        if self.is_stale(now_ms) and self.link_state not in ("stale", "incompatible"):
            self.link_state = "stale"
            self._last_hw_tick32 = None
            self._last_unsolicited_sequence = None
            self._emit({"type": "link_state", "state": "stale"})

    def poll(self, now_ms):
        self.poll_uart(now_ms)
        self.service_maintenance(now_ms)

    def is_stale(self, now_ms):
        if self.last_valid_ms is None:
            return True
        return self._ticks_diff(now_ms, self.last_valid_ms) > 6000

    def _handle_frame(self, frame, now_ms):
        self._clock_ms = now_ms
        if frame["version"] != VERSION_ALPHA3:
            self.link_state = "incompatible"
            self._emit({"type": "protocol_error", "reason": "unsupported_version", "frame": frame})
            return
        self.last_valid_ms = now_ms
        kind, payload = frame["kind"], frame["payload"]
        message = {"type": "frame", "kind": kind, "sequence": frame["sequence"]}
        if kind == HELLO and len(payload) == 9:
            self.bmcu_boot_session += 1
            if self.soft_reset is not None and self.soft_reset.get("state") == "scheduled":
                self.soft_reset["state"] = "rebooted"
                self.soft_reset["bmcu_boot_session"] = self.bmcu_boot_session
            self._last_unsolicited_sequence = frame["sequence"]
            self._outstanding_get_status = []
            self._last_hw_tick32 = None
            self._hw_tick_epoch = 0
            self.status = None
            self.snapshot = None
            self.channels = [None, None, None, None]
            self.printer_auth = None
            self.printer_rx = None
            self.printer_tx = None
            self.ams_service = None
            self.ams_registration = None
            self._snapshot_parts = None
            self._snapshot_retries = 0
            self.link_state = "resyncing"
            self.tick_hz = _u32(payload, 5)
            message.update({"type": "hello", "protocol": payload[0], "capabilities": _u16(payload, 1),
                            "firmware": [payload[3], payload[4]], "tick_hz": self.tick_hz,
                            "bmcu_boot_session": self.bmcu_boot_session})
            self._emit(message)
            self._request_missing_baseline()
            return
        solicited = False
        if kind == STATUS:
            self._prune_outstanding_get_status(now_ms)
            for entry in self._outstanding_get_status:
                if entry[0] == frame["sequence"]:
                    self._outstanding_get_status.remove(entry)
                    solicited = True
                    break
        if kind in (STATUS, EVENT) and not solicited:
            previous = self._last_unsolicited_sequence
            expected = None if previous is None else (previous + 1) & 0xffff
            self._last_unsolicited_sequence = frame["sequence"]
            if expected is not None and frame["sequence"] != expected:
                self.sequence_gap_count += 1
                self._invalidate_baseline(now_ms, "sequence_gap")
                message["sequence_gap"] = {"expected": expected, "received": frame["sequence"]}

        if kind == STATUS and len(payload) in STATUS_PAYLOAD_SIZES:
            self.status = self._decode_status(payload)
            message.update({"type": "status", "data": self.status})
            self._extend_hw_tick(self.status["hw_tick32"], message)
            self._request_missing_baseline()
        elif kind == EVENT and len(payload) == 16:
            event = self._decode_event(payload)
            self._apply_event(event)
            message.update({"type": "event", "data": event})
            self._extend_hw_tick(event["hw_tick32"], message)
        elif kind == PONG and len(payload) == 8:
            message.update({"type": "pong", "token": _u32(payload, 0), "hw_tick32": _u32(payload, 4)})
            self._request_missing_baseline()
        elif kind == ACK and len(payload) == 2:
            message.update({"type": "ack", "request_kind": payload[0], "result": payload[1]})
            if payload[0] == GET_FULL_STATUS and payload[1] == 3:
                self._snapshot_retries += 1
                self._schedule_snapshot_retry(now_ms)
            if payload[0] == REQUEST_SOFT_RESET and self.soft_reset is not None:
                self.soft_reset["ack_result"] = payload[1]
                self.soft_reset["state"] = "scheduled" if payload[1] == 0 else "rejected"
        elif kind == FULL_STATUS_RECORD and len(payload) == 26:
            self._handle_snapshot(payload, message, now_ms)
        else:
            message.update({"type": "unknown_or_invalid", "payload": payload})
        self._emit(message)
    @staticmethod
    def _decode_status(data):
        status = {"hw_tick32": _u32(data, 0), "tx_drop": _u16(data, 4), "rx_drop": _u16(data, 6),
                  "crc_error": _u16(data, 8), "frame_error": _u16(data, 10), "current_slot": data[12],
                  "inserted_mask": data[13], "online_mask": data[14], "motion": list(data[15:19]),
                  "pull_pct": list(data[19:23]), "pressure": _u16(data, 23),
                  "led_mode": data[25], "control_error": data[26]}
        # Branch on length rather than reading a short payload as a prefix.
        # Absent is not the same as clear: silently reporting every latch as 0
        # against an older BMCU would invent a healthy machine, which is the
        # class of mistake the channel-flags byte exists to prevent.
        if len(data) >= STATUS_PAYLOAD_SIZE:
            status["channel_flags"] = [decode_channel_flags(data[27 + index])
                                       for index in range(4)]
        else:
            status["channel_flags"] = None
        return status

    @staticmethod
    def _decode_event(data):
        # The raw record is retained so /api/snapshot.bin can hand back exactly
        # what arrived; re-encoding it here would put a second copy of the wire
        # layout on the device.
        event = {"raw": bytes(data[:16]),
                 "hw_tick32": _u32(data, 0), "record_type": data[4],
                 "severity": data[5], "source": data[6],
                 "payload_length": data[7]}
        payload = bytes(data[8:16])
        event["payload"] = payload
        if event["record_type"] == 4 and event["payload_length"] >= 6:
            event.update({"event_name": "state_change", "field": payload[0],
                          "slot": payload[1], "previous_value": _u16(payload, 2),
                          "value": _u16(payload, 4)})
        elif event["record_type"] == 5 and event["payload_length"] >= 8:
            event.update({"event_name": "sensor", "sensor": payload[0],
                          "slot": payload[1], "validity": payload[2],
                          "value_format": payload[3], "value": _i32(payload, 4)})
        elif event["record_type"] == RECORD_RESET_STATE and event["payload_length"] >= 8:
            event.update({"event_name": "reset_state",
                          "operation_id": _u32(payload, 0), "reset_state": payload[4],
                          "request_reason": payload[5], "cancel_reason": payload[6]})
        elif (event["record_type"] == RECORD_PRINTER_LONG_TRANSACTION and
              event["payload_length"] >= 8):
            event.update({"event_name": "printer_long_transaction",
                          "frame_type": _u16(payload, 0), "owner": payload[2],
                          "outcome": payload[3], "reason": payload[4],
                          "request_length": payload[5], "response_length": payload[6],
                          "payload_hash": payload[7]})
        elif (event["record_type"] == RECORD_PRINTER_TRANSACTION and
              event["payload_length"] >= 7):
            # rx_class is the bambubus_package_type the parser resolved.
            # command is a raw wire byte and does not identify the frame, so
            # without rx_class an unanswered transaction cannot be attributed.
            event.update({"event_name": "printer_transaction",
                          "command": payload[0], "owner": payload[1],
                          "outcome": payload[2], "reason": payload[3],
                          "request_length": payload[4],
                          "response_length": payload[5],
                          "rx_class": payload[6]})
        else:
            event["event_name"] = "record_%d" % event["record_type"]
        return event

    def _apply_event(self, event):
        self.events.append(event)
        if len(self.events) > 16:
            self.events.pop(0)
        if event.get("event_name") == "sensor":
            self.sensors[event["sensor"]] = event
            return
        if event.get("event_name") == "reset_state" and self.soft_reset is not None:
            if event["operation_id"] == self.soft_reset.get("operation_id"):
                self.soft_reset["state"] = (
                    "scheduled" if event["reset_state"] == 1 else "cancelled")
                self.soft_reset["cancel_reason"] = event["cancel_reason"]
            return
        if event.get("event_name") != "state_change" or self.status is None:
            return
        field, slot, value = event["field"], event["slot"], event["value"]
        if field == 1:
            self.status["current_slot"] = value & 0xff
        elif field == 2:
            self.status["inserted_mask"] = value & 0xff
        elif field == 3:
            self.status["online_mask"] = value & 0xff
        elif field == 4 and slot < 4:
            self.status["motion"][slot] = value & 0xff
        elif field == 5:
            self.status["pressure"] = value
        elif field == 6:
            self.status["led_mode"] = value & 0xff
        elif field == 7:
            self.status["control_error"] = value & 0xff
        elif field == 8 and slot < 4:
            faults = self.status.setdefault("motion_fault", [0, 0, 0, 0])
            faults[slot] = value & 0xff
    def _handle_snapshot(self, data, message, now_ms=0):
        snapshot_id, index, count, record_type = _u16(data, 0), data[2], data[3], data[4]
        record_data = bytes(data[10:26])
        message.update({"type": "full_status_record", "snapshot_id": snapshot_id, "record_index": index,
                        "record_count": count, "record_type": record_type, "hw_tick32": _u32(data, 6),
                        "record_data": record_data})
        if record_type == 2 and record_data[0] < 4:
            flags = _u16(record_data, 6)
            channel = {
                "channel": record_data[0], "ams_motion": record_data[1],
                "inserted": bool(record_data[2]), "online": bool(record_data[3]),
                "pull_pct": record_data[4], "sensor_validity": record_data[5],
                "flags": flags, "sensor_online": bool(flags & (1 << 2)),
                "sensor_good": bool(flags & (1 << 3)),
                # Bits 8..12 are the STATUS channel-flags byte for this channel,
                # shifted up whole so one decoder serves both carriers.
                "channel_flags": decode_channel_flags((flags >> 8) & 0xff),
                "raw_angle": _u16(record_data, 8),
                "position_delta": _i16(record_data, 10),
                "motor_pwm": _i16(record_data, 12),
                "motion_fault": record_data[14],
                "controller_motion": (record_data[15] & 0x7f) if record_data[15] & 0x80 else None,
            }
            message["channel_data"] = channel
        if record_type == FULL_RECORD_PRINTER_AUTH:
            message["printer_auth_data"] = {
                "last_type": _u16(record_data, 0), "count_040d": _u16(record_data, 2),
                "count_040e": _u16(record_data, 4), "payload_length": _u16(record_data, 6),
                "hw_tick32": _u32(record_data, 8), "outcome": record_data[12],
                "reason": record_data[13], "response_length": record_data[14],
                "payload_hash": record_data[15],
            }
        if record_type == FULL_RECORD_PRINTER_RX_CORE:
            message["printer_rx_data"] = {
                "rx_bytes": _u32(record_data, 0),
                "rx_frames_valid": _u32(record_data, 4),
                "rx_bad_length": _u32(record_data, 8),
                "rx_header_crc_error": _u32(record_data, 12),
            }
        if record_type == FULL_RECORD_PRINTER_RX_LOSS:
            message["printer_rx_data"] = {
                "rx_resync_bytes": _u32(record_data, 0),
                "rx_publish_drop": _u32(record_data, 4),
                "rx_dma_error": _u32(record_data, 8),
                "rx_usart_overrun": _u32(record_data, 12),
            }
        if record_type == FULL_RECORD_PRINTER_RX_DMA:
            message["printer_rx_data"] = {
                "rx_dma_overrun": _u32(record_data, 0),
                "rx_dma_wrap": _u32(record_data, 4),
                "rx_dma_max_pending": _u32(record_data, 8),
                "rx_compat_copy": _u32(record_data, 12),
            }
        if record_type == FULL_RECORD_PRINTER_TX_CORE:
            message["printer_tx_data"] = {
                "tx_started": _u32(record_data, 0),
                "tx_completed": _u32(record_data, 4),
                "tx_response_busy": _u32(record_data, 8),
                "tx_response_missing": _u32(record_data, 12),
            }
        if record_type == FULL_RECORD_PRINTER_TX_FAULT:
            message["printer_tx_data"] = {
                "tx_invalid_length": _u32(record_data, 0),
                "tx_dma_error": _u32(record_data, 4),
                "tx_timeout": _u32(record_data, 8),
                "tx_no_response_expected": _u32(record_data, 12),
            }
        if record_type == FULL_RECORD_AMS_SERVICE:
            message["ams_service_data"] = {
                "gap_now_ms": _u32(record_data, 0),
                "gap_max_ms": _u32(record_data, 4),
                "gap_max_since_confirm_ms": _u32(record_data, 8),
                "ms_since_confirm": _u32(record_data, 12),
            }
        if record_type == FULL_RECORD_AMS_REGISTRATION:
            flags = record_data[14]
            message["ams_registration_data"] = {
                "count_motion": _u16(record_data, 0),
                "count_stu_motion": _u16(record_data, 2),
                "count_mc_online": _u16(record_data, 4),
                "registration_query_count": _u16(record_data, 6),
                "would_reoffer_count": _u16(record_data, 8),
                "confirm_count": _u16(record_data, 10),
                "reset_count": _u16(record_data, 12),
                "flags": flags,
                "registered": bool(flags & (1 << 0)),
                "confirm_settled": bool(flags & (1 << 1)),
                "service_stale": bool(flags & (1 << 2)),
                "reoffer_armed": bool(flags & (1 << 3)),
                "have_service": bool(flags & (1 << 4)),
            }
        if count == 0 or index >= count:
            message["snapshot_error"] = "invalid_index"
            self._snapshot_parts = None
            return
        if self._snapshot_parts is not None:
            if self._snapshot_id != snapshot_id or self._snapshot_count != count:
                message["snapshot_error"] = "inconsistent_metadata"
                self._snapshot_parts = None
                self._snapshot_retries += 1
                self._schedule_snapshot_retry(now_ms)
                return
            if index in self._snapshot_parts:
                message["snapshot_error"] = "duplicate_index"
                self._snapshot_parts = None
                self._snapshot_retries += 1
                self._schedule_snapshot_retry(now_ms)
                return
        if self._snapshot_parts is None:
            self._snapshot_parts, self._snapshot_count = {}, count
            self._snapshot_id = snapshot_id
            self._snapshot_deadline_ms = self._ticks_add(now_ms, self.SNAPSHOT_TIMEOUT_MS)
        self._snapshot_parts[index] = message.copy()
        if len(self._snapshot_parts) == count:
            self.snapshot = [self._snapshot_parts[i] for i in range(count)]
            self._snapshot_parts = None
            self._snapshot_id = None
            channels = [None, None, None, None]
            printer_auth = None
            printer_rx = {}
            printer_tx = {}
            ams_service = None
            ams_registration = None
            self._snapshot_deadline_ms = None
            self._snapshot_retry_ms = None
            self._snapshot_retries = 0
            self.link_state = "online"
            if self.soft_reset is not None and self.soft_reset.get("state") == "rebooted":
                self.soft_reset["state"] = "completed"
            for part in self.snapshot:
                channel = part.get("channel_data")
                if channel is not None:
                    channels[channel["channel"]] = channel
                if part.get("printer_auth_data") is not None:
                    printer_auth = part["printer_auth_data"]
                rx_data = part.get("printer_rx_data")
                if rx_data is not None:
                    printer_rx.update(rx_data)
                tx_data = part.get("printer_tx_data")
                if tx_data is not None:
                    printer_tx.update(tx_data)
                if part.get("ams_service_data") is not None:
                    ams_service = part["ams_service_data"]
                if part.get("ams_registration_data") is not None:
                    ams_registration = part["ams_registration_data"]
            self.channels = channels
            self.snapshot_at_ms = now_ms
            self.printer_auth = printer_auth
            self.printer_rx = printer_rx or None
            self.printer_tx = printer_tx or None
            self.ams_service = ams_service
            self.ams_registration = ams_registration
            message["snapshot_complete"] = True


def drain_monitors(monitors, now_ms, byte_budget=1024, chunk_size=128,
                   start_index=0):
    """Fairly drain multiple UARTs within one explicit byte budget."""
    if not monitors or byte_budget <= 0:
        return start_index, 0
    index = start_index % len(monitors)
    drained = 0
    idle = 0
    while drained < byte_budget and idle < len(monitors):
        monitor = monitors[index]
        amount = monitor.poll_uart(
            now_ms, min(chunk_size, byte_budget - drained))
        index = (index + 1) % len(monitors)
        if amount:
            drained += amount
            idle = 0
        else:
            idle += 1
    for monitor in monitors:
        monitor.service_maintenance(now_ms)
    return index, drained
