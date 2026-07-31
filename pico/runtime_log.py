"""Bounded binary PICO_LOG ring and BMCR1 crash persistence."""

try:
    import ustruct as struct
except ImportError:
    import struct
try:
    import ubinascii as binascii
except ImportError:
    import binascii
import sys

import bmcu_binary as binary
import bmcu_binary_constants as C
from byte_ring import ByteRing, RingFull

BMCR_MAGIC = b"BMCR1"
LOG_SLOT_SIZE = 1024
DETAIL_EXCEPTION = 240
DETAIL_TRACEBACK = 241
DETAIL_SUPPRESSED = 242

_SEVERITY = {
    "debug": C.LOG_DEBUG,
    "info": C.LOG_INFO,
    "notice": C.LOG_NOTICE,
    "warning": C.LOG_WARNING,
    "error": C.LOG_ERROR,
    "critical": C.LOG_CRITICAL,
}


def _crc32(data):
    return binascii.crc32(data) & 0xFFFFFFFF


def _utf8(value, maximum):
    encoded = str(value).replace("\r", " ").encode()
    if len(encoded) <= maximum:
        return encoded
    encoded = encoded[-maximum:]
    while encoded and encoded[0] & 0xC0 == 0x80:
        encoded = encoded[1:]
    return encoded


def _traceback_tail(error, maximum=480):
    try:
        import uio as io
    except ImportError:
        import io
    stream = io.StringIO()
    printer = getattr(sys, "print_exception", None)
    if printer is not None:
        printer(error, stream)
        return _utf8(stream.getvalue(), maximum)
    try:
        import traceback
        value = "".join(traceback.format_exception(
            type(error), error, error.__traceback__))
    except Exception:
        value = "%s: %s" % (type(error).__name__, error)
    return _utf8(value, maximum)


class PicoRuntimeLog:
    def __init__(self, clock_ms, limit=32, crash_path="pico_crash.bmcr",
                 pico_boot_id=0, sink=None):
        self.clock_ms = clock_ms
        self.limit = max(8, int(limit))
        self.crash_path = crash_path
        self.pico_boot_id = pico_boot_id
        self.sink = sink
        self.ring = ByteRing(
            bytearray(LOG_SLOT_SIZE * self.limit), LOG_SLOT_SIZE)
        self.buffer = bytearray(LOG_SLOT_SIZE)
        self.detail = bytearray(C.MAX_LOG_DETAIL_BYTES)
        self.next_sequence = 1
        self.exception_count = 0
        self.suppressed_count = 0
        self._last_exception_key = None
        self._last_exception_ms = None
        self._last_persist_key = None
        self._last_persist_ms = None
        self.previous_crash = self._load_previous_crash()
        if self.previous_crash is not None:
            self._install_previous_crash(self.previous_crash)

    def _install_previous_crash(self, payload):
        if len(payload) < 22:
            return
        try:
            log_sequence = struct.unpack_from(">Q", payload, 0)[0]
            size = binary.write_message(
                self.buffer, 0, C.PICO_LOG,
                C.FLAG_REPLAY | C.FLAG_CRITICAL, 0, self.pico_boot_id,
                C.GLOBAL_SCOPE, payload)
            self.ring.append(
                log_sequence, memoryview(self.buffer)[:size], protected=True)
            self.next_sequence = max(self.next_sequence, log_sequence + 1)
            if self.sink is not None:
                self.sink(C.PICO_LOG, C.FLAG_REPLAY | C.FLAG_CRITICAL,
                          C.GLOBAL_SCOPE, int(self.clock_ms()) * 1000,
                          payload, True)
        except Exception:
            return

    def _payload_view(self, message):
        return message[C.HEADER_SIZE:]

    def add(self, level, component, message):
        severity = _SEVERITY.get(str(level).lower(), C.LOG_INFO)
        component_bytes = _utf8(component, C.MAX_LOG_COMPONENT_BYTES)
        message_bytes = _utf8(message, C.MAX_LOG_MESSAGE_BYTES)
        detail = b""
        sequence = self.next_sequence
        self.next_sequence += 1
        size = binary.write_log(
            self.buffer, 0, C.FLAG_CRITICAL if severity >= C.LOG_ERROR else 0,
            0, self.pico_boot_id, sequence, int(self.clock_ms()), severity,
            component_bytes, message_bytes, detail)
        try:
            self.ring.append(sequence, memoryview(self.buffer)[:size],
                             protected=severity >= C.LOG_ERROR)
        except RingFull:
            if severity < C.LOG_ERROR:
                return sequence
            if self.ring.release_one():
                self.ring.append(sequence, memoryview(self.buffer)[:size],
                                 protected=True)
        if self.sink is not None:
            try:
                self.sink(
                    C.PICO_LOG,
                    C.FLAG_CRITICAL if severity >= C.LOG_ERROR else 0,
                    C.GLOBAL_SCOPE, int(self.clock_ms()) * 1000,
                    memoryview(self.buffer)[C.HEADER_SIZE:size],
                    severity >= C.LOG_WARNING)
            except Exception:
                pass
        return sequence

    def info(self, component, message):
        return self.add("info", component, message)

    def warning(self, component, message):
        return self.add("warning", component, message)

    def exception(self, component, error):
        self.exception_count += 1
        now_ms = int(self.clock_ms())
        key = (str(component), type(error).__name__, str(error))
        elapsed = (None if self._last_exception_ms is None
                   else now_ms - self._last_exception_ms)
        if (key == self._last_exception_key and elapsed is not None and
                0 <= elapsed < 5000):
            self.suppressed_count += 1
            return self.next_sequence - 1
        detail_size = binary.write_tlv(
            self.detail, 0, DETAIL_EXCEPTION, C.VALUE_UTF8,
            _utf8(type(error).__name__, 40))
        trace = _traceback_tail(error)
        detail_size += binary.write_tlv(
            self.detail, detail_size, DETAIL_TRACEBACK, C.VALUE_UTF8, trace)
        sequence = self.next_sequence
        self.next_sequence += 1
        component_bytes = _utf8(component, C.MAX_LOG_COMPONENT_BYTES)
        message_bytes = _utf8(error, C.MAX_LOG_MESSAGE_BYTES)
        size = binary.write_log(
            self.buffer, 0, C.FLAG_CRITICAL, 0, self.pico_boot_id, sequence,
            now_ms, C.LOG_ERROR, component_bytes, message_bytes,
            memoryview(self.detail)[:detail_size])
        try:
            self.ring.append(sequence, memoryview(self.buffer)[:size],
                             protected=True)
        except RingFull:
            if self.ring.release_one():
                self.ring.append(sequence, memoryview(self.buffer)[:size],
                                 protected=True)
        if self.sink is not None:
            try:
                self.sink(C.PICO_LOG, C.FLAG_CRITICAL, C.GLOBAL_SCOPE,
                          now_ms * 1000,
                          memoryview(self.buffer)[C.HEADER_SIZE:size], True)
            except Exception:
                pass
        self._last_exception_key = key
        self._last_exception_ms = now_ms
        persist_elapsed = (None if self._last_persist_ms is None
                           else now_ms - self._last_persist_ms)
        if (self.crash_path and
                (key != self._last_persist_key or persist_elapsed is None or
                 persist_elapsed < 0 or persist_elapsed >= 60000)):
            self._persist_crash(memoryview(self.buffer)[C.HEADER_SIZE:size])
            self._last_persist_key = key
            self._last_persist_ms = now_ms
        return sequence

    def _persist_crash(self, payload):
        raw = bytearray(7 + len(payload) + 4)
        raw[:5] = BMCR_MAGIC
        struct.pack_into(">H", raw, 5, len(payload))
        raw[7:7 + len(payload)] = payload
        struct.pack_into(">I", raw, 7 + len(payload),
                         _crc32(memoryview(raw)[:7 + len(payload)]))
        try:
            with open(self.crash_path, "wb") as target:
                target.write(raw)
                target.flush()
        except OSError:
            pass

    def _load_previous_crash(self):
        if not self.crash_path:
            return None
        try:
            with open(self.crash_path, "rb") as source:
                raw = source.read(LOG_SLOT_SIZE)
        except OSError:
            return None
        if len(raw) < 11 or raw[:5] != BMCR_MAGIC:
            return None
        size = struct.unpack_from(">H", raw, 5)[0]
        if size > LOG_SLOT_SIZE - 11 or len(raw) != 7 + size + 4:
            return None
        if _crc32(memoryview(raw)[:-4]) != struct.unpack_from(
                ">I", raw, len(raw) - 4)[0]:
            return None
        return bytes(memoryview(raw)[7:7 + size])

    def iter_messages(self, after=0, limit=64, byte_limit=32768):
        count = 0
        used = 0
        for sequence, message, _, _ in self.ring.iter_records():
            if sequence <= after:
                continue
            if count >= limit or used + len(message) > byte_limit:
                break
            yield message
            count += 1
            used += len(message)


def guarded_call(runtime_log, component, callback, recovery=None):
    try:
        callback()
        return True
    except Exception as error:
        try:
            runtime_log.exception(component, error)
        except Exception:
            pass
        if recovery is not None:
            try:
                recovery()
            except Exception as recovery_error:
                try:
                    runtime_log.exception(
                        component + ".recovery", recovery_error)
                except Exception:
                    pass
        return False
