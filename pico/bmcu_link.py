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

HELLO = 0x01
STATUS = 0x02
EVENT = 0x03
GET_STATUS = 0x10
SET_LED_MODE = 0x11
PING = 0x12
GET_FULL_STATUS = 0x17
PONG = 0x72
FULL_STATUS_RECORD = 0x73
ACK = 0x7f

FULL_RECORD_PRINTER_AUTH = 5
FULL_RECORD_PRINTER_RX_CORE = 6
FULL_RECORD_PRINTER_RX_LOSS = 7
FULL_RECORD_PRINTER_RX_DMA = 8
FULL_RECORD_PRINTER_TX_CORE = 9
FULL_RECORD_PRINTER_TX_FAULT = 10
RECORD_PRINTER_LONG_TRANSACTION = 9


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

def _i32(data, offset):
    value = _u32(data, offset)
    return value - 0x100000000 if value & 0x80000000 else value


class FrameDecoder:
    """Consumes arbitrary UART chunks and emits only valid bounded frames."""

    def __init__(self):
        self._buffer = bytearray()
        self.crc_errors = 0
        self.frame_errors = 0

    def feed(self, data):
        if len(data) > MAX_DECODER_BUFFER:
            self.frame_errors += 1
            data = data[-MAX_DECODER_BUFFER:]
        self._buffer.extend(data)
        if len(self._buffer) > MAX_DECODER_BUFFER:
            self._buffer = self._buffer[-MAX_DECODER_BUFFER:]
        frames = []
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
                self._buffer = self._buffer[1:]
                continue
            wire_length = payload_length + 9
            if len(self._buffer) < wire_length:
                break
            body = self._buffer[2:wire_length - 2]
            expected_crc = _u16(self._buffer, wire_length - 2)
            if crc16_ccitt_false(body) != expected_crc:
                self.crc_errors += 1
                self._buffer = self._buffer[1:]
                continue
            frames.append({
                "version": body[0], "kind": body[1], "sequence": _u16(body, 2),
                "payload": bytes(body[5:]),
            })
            self._buffer = self._buffer[wire_length:]
        return frames


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
    SNAPSHOT_MAX_RETRIES = 3


    def __init__(self, uart, on_message=None, link_id="bmcu-a"):
        self.uart = uart
        self.on_message = on_message
        self.decoder = FrameDecoder()
        self.link_id = link_id
        self.next_sequence = 1
        self.last_valid_ms = None
        self.last_ping_ms = None
        self.tick_hz = None
        self.status = None
        self.snapshot = None
        self.channels = [None, None, None, None]
        self.printer_auth = None
        self.printer_rx = None
        self.printer_tx = None
        self._snapshot_parts = None
        self._snapshot_count = 0
        self._snapshot_id = None
        self.events = []
        self.sensors = {}

        self._snapshot_deadline_ms = None
        self._snapshot_retry_ms = None
        self._snapshot_retries = 0
        self._last_unsolicited_sequence = None
        self._last_hw_tick32 = None
        self._hw_tick_epoch = 0
        self.bmcu_boot_session = 0
        self.link_state = "stale"
        self._clock_ms = 0
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

    def get_status(self):
        return self._send(GET_STATUS)

    def get_full_status(self):
        sequence = self._send(GET_FULL_STATUS, b"\x0f\x0f")
        self._snapshot_deadline_ms = self._clock_ms + self.SNAPSHOT_TIMEOUT_MS
        return sequence

    def ping(self, token):
        return self._send(PING, struct.pack("<I", token & 0xffffffff))

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
        self.channels = [None, None, None, None]
        self.printer_auth = None
        self.printer_rx = None
        self.printer_tx = None
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
        delay_ms = 250 << min(self._snapshot_retries, 2)
        self._snapshot_retry_ms = now_ms + delay_ms

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

    def poll(self, now_ms):
        available = min(self.uart.any(), MAX_DECODER_BUFFER)
        self._clock_ms = now_ms
        if available:
            data = self.uart.read(available)
            if data:
                for frame in self.decoder.feed(data):
                    self._handle_frame(frame, now_ms)
        self._service_snapshot_timeout(now_ms)
        if self.is_stale(now_ms) and self.link_state not in ("stale", "incompatible"):
            self.link_state = "stale"
            self._last_hw_tick32 = None
            self._last_unsolicited_sequence = None
            self._emit({"type": "link_state", "state": "stale"})


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
            self._last_unsolicited_sequence = frame["sequence"]
            self._last_hw_tick32 = None
            self._hw_tick_epoch = 0
            self.status = None
            self.snapshot = None
            self.channels = [None, None, None, None]
            self.printer_auth = None
            self.printer_rx = None
            self.printer_tx = None
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
        if kind in (STATUS, EVENT):
            previous = self._last_unsolicited_sequence
            expected = None if previous is None else (previous + 1) & 0xffff
            self._last_unsolicited_sequence = frame["sequence"]
            if expected is not None and frame["sequence"] != expected:
                self._invalidate_baseline(now_ms, "sequence_gap")
                message["sequence_gap"] = {"expected": expected, "received": frame["sequence"]}

        if kind == STATUS and len(payload) == 27:
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
                self._schedule_snapshot_retry(now_ms)
        elif kind == FULL_STATUS_RECORD and len(payload) == 26:
            self._handle_snapshot(payload, message, now_ms)
        else:
            message.update({"type": "unknown_or_invalid", "payload": payload})
        self._emit(message)

    @staticmethod
    def _decode_status(data):
        return {"hw_tick32": _u32(data, 0), "tx_drop": _u16(data, 4), "rx_drop": _u16(data, 6),
                "crc_error": _u16(data, 8), "frame_error": _u16(data, 10), "current_slot": data[12],
                "inserted_mask": data[13], "online_mask": data[14], "motion": list(data[15:19]),
                "pull_pct": list(data[19:23]), "pressure": _u16(data, 23),
                "led_mode": data[25], "control_error": data[26]}

    @staticmethod
    def _decode_event(data):
        event = {"hw_tick32": _u32(data, 0), "record_type": data[4],
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
        elif (event["record_type"] == RECORD_PRINTER_LONG_TRANSACTION and
              event["payload_length"] >= 8):
            event.update({"event_name": "printer_long_transaction",
                          "frame_type": _u16(payload, 0), "owner": payload[2],
                          "outcome": payload[3], "reason": payload[4],
                          "request_length": payload[5], "response_length": payload[6],
                          "payload_hash": payload[7]})
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
            self._snapshot_deadline_ms = now_ms + self.SNAPSHOT_TIMEOUT_MS
        self._snapshot_parts[index] = message.copy()
        if len(self._snapshot_parts) == count:
            self.snapshot = [self._snapshot_parts[i] for i in range(count)]
            self._snapshot_parts = None
            self._snapshot_id = None
            channels = [None, None, None, None]
            printer_auth = None
            printer_rx = {}
            printer_tx = {}
            self._snapshot_deadline_ms = None
            self._snapshot_retry_ms = None
            self._snapshot_retries = 0
            self.link_state = "online"
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
            self.channels = channels
            self.printer_auth = printer_auth
            self.printer_rx = printer_rx or None
            self.printer_tx = printer_tx or None
            message["snapshot_complete"] = True
