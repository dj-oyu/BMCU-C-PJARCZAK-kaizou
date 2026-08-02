"""Render the Pico's HTTP responses into fixtures the Vite dev server replays.

The point is to develop the browser UI without a device on the bench. Encoding
goes through the real ``pico/`` codec so a mock response can never drift from
what the hardware actually serves.
"""
import os
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "pico"))

import bmcu_binary as binary  # noqa: E402
import bmcu_binary_constants as C  # noqa: E402
import bmcu_link as link  # noqa: E402
import device_metrics  # noqa: E402
from binary_api import BinaryAPI  # noqa: E402
from device_metrics import DeviceMetrics  # noqa: E402

OUTPUT = ROOT / "web" / "src" / "mock" / "fixtures"
BOOT_ID = 0x0102030405060708


class FakeGc:
    """Stands in for MicroPython's gc, which CPython does not provide."""

    def __init__(self, free):
        self._free = free

    def mem_free(self):
        return self._free


class FakeDecoder:
    def __init__(self, crc_errors, frame_errors):
        self.crc_errors = crc_errors
        self.frame_errors = frame_errors


class FakeMonitor:
    """Only the attributes DeviceMetrics.snapshot reads."""

    def __init__(self, backlog, drained, crc_errors, frame_errors, gaps,
                 overflows):
        self.uart_backlog = backlog
        self.uart_max_backlog = backlog * 2
        self.uart_drain_bytes = drained
        self.decoder = FakeDecoder(crc_errors, frame_errors)
        self.sequence_gap_count = gaps
        self.uart_max_service_gap_us = 4200
        self.uart_overflow_count = overflows


def status_payload(current_slot, inserted_mask, online_mask, motion, pull):
    payload = bytearray(27)
    payload[12] = current_slot
    payload[13] = inserted_mask
    payload[14] = online_mask
    payload[15:19] = bytes(motion)
    payload[19:23] = bytes(pull)
    return bytes(payload)


def encoded(writer, *args):
    out = bytearray(C.MAX_MESSAGE_SIZE)
    return bytes(memoryview(out)[:writer(out, 0, *args)])


def main():
    OUTPUT.mkdir(parents=True, exist_ok=True)

    # Link 0 mirrors a healthy loader with filament in slots 1 and 3; link 1 is
    # a channel row that is plugged in but empty, which is the case the old UI
    # mislabelled as "Filament: Present".
    current = b""
    for index, (slot, inserted, online, motion, pull) in enumerate((
        (0, 0b1111, 0b0101, (2, 0, 1, 0), (61, 50, 58, 50)),
        (255, 0b1111, 0b0000, (0, 0, 0, 0), (50, 50, 50, 50)),
    )):
        wire = link.encode_frame(
            link.STATUS, index + 1,
            status_payload(slot, inserted, online, motion, pull))
        current += encoded(binary.write_bmcu_frame, 0, index + 1, BOOT_ID,
                           index, 100000 + index, wire)
    (OUTPUT / "current.bin").write_bytes(current)

    # DeviceMetrics reads gc.mem_free, which only exists on MicroPython. Left
    # alone, CPython reports 0 bytes free and the mock renders a heap warning
    # that no real device is showing.
    device_metrics.gc = FakeGc(148_512)
    metrics = DeviceMetrics()
    metrics.observe_loop_gap(1800)
    metrics.observe_loop_gap(90000)
    metrics.observe_gc(5100)
    monitors = [
        FakeMonitor(48, 182364, 0, 0, 0, 0),
        FakeMonitor(4096, 918273, 314, 271, 12, 3),
    ]
    payload = metrics.snapshot(
        86_400_000, monitors, wifi_rssi=-57, exception_count=1483,
        temperature_milli_c=31_250)
    (OUTPUT / "diagnostics.bin").write_bytes(
        encoded(binary.write_diagnostic, 0, 1, BOOT_ID, payload))

    logs = b""
    for sequence, (severity, component, message) in enumerate((
        (C.LOG_ERROR, b"journal.flush", b"OSError: [Errno 28] ENOSPC"),
        (C.LOG_WARNING, b"bmcu.drain", b"uart1 framing errors 271"),
        (C.LOG_INFO, b"wifi.poll", b"associated rssi -57 dBm"),
        (C.LOG_ERROR, b"bambuddy.binary", b"OSError: [Errno 104] ECONNRESET"),
    ), start=1):
        logs += encoded(
            binary.write_log, 0, sequence, BOOT_ID, sequence,
            86_400_000 - sequence * 137, severity, component, message, b"")
    (OUTPUT / "logs.bin").write_bytes(logs)

    # newline="\n" above is not cosmetic: the device writes "%d\n" from
    # transport_settings.status_body, and a CRLF fixture made the mock report
    # "Not configured" for a configured endpoint.
    # Snapshot: built through the real BinaryAPI so the mock cannot encode a
    # record shape the device would never produce.
    channel_records = []
    for channel, (inserted, online, pull, angle, delta, pwm, fault) in enumerate((
        (1, 1, 61, 2048, 12, 480, 0),
        (1, 0, 50, 1310, 0, 0, 0),
        (1, 1, 58, 3072, -6, -420, 0),
        (1, 0, 50, 200, 0, 0, 3),
    )):
        body = bytearray(16)
        body[0] = channel
        body[1] = 2 if online else 0
        body[2] = inserted
        body[3] = online
        body[4] = pull
        body[5] = 1
        struct.pack_into("<H", body, 6, 0x000C if fault == 0 else 0x0004)
        struct.pack_into("<Hhh", body, 8, angle, delta, pwm)
        body[14] = fault
        body[15] = 0x80 | 3
        channel_records.append({"record_type": 2, "hw_tick32": 900 + channel,
                                "record_data": bytes(body)})

    def counters(record_type, values):
        body = bytearray(16)
        for index, value in enumerate(values):
            struct.pack_into("<I", body, index * 4, value)
        return {"record_type": record_type, "hw_tick32": 950,
                "record_data": bytes(body)}

    snapshot = channel_records + [
        counters(6, (918273, 91820, 3, 1)),          # printer rx core
        counters(7, (12, 0, 0, 0)),                  # printer rx loss
        counters(8, (0, 4471, 96, 0)),               # printer rx dma
        counters(9, (91820, 91818, 2, 0)),           # printer tx core
        counters(10, (0, 0, 1, 4)),                  # printer tx fault
        counters(11, (12, 480, 96, 3120)),           # ams service
    ]
    registration = bytearray(16)
    struct.pack_into("<7H", registration, 0, 41, 39, 4, 7, 1, 3, 0)
    registration[14] = 0x03
    snapshot.append({"record_type": 12, "hw_tick32": 950,
                     "record_data": bytes(registration)})
    auth = bytearray(16)
    struct.pack_into("<4H", auth, 0, 0x040D, 118, 117, 8)
    struct.pack_into("<I", auth, 8, 88120)
    auth[12:16] = bytes((0, 0, 12, 0x5A))
    snapshot.append({"record_type": 5, "hw_tick32": 950,
                     "record_data": bytes(auth)})

    events = []
    for hw_tick, record_type, severity, source, detail in (
        (88010, 4, 1, 1, bytes((3, 0, 0, 0, 1, 0, 0, 0))),
        (88120, 9, 2, 2, bytes((0x0D, 0x04, 1, 0, 0, 8, 12, 0x5A))),
        (88200, 4, 3, 1, bytes((1, 3, 0, 0, 3, 0, 0, 0))),
    ):
        raw = bytearray(16)
        struct.pack_into("<I", raw, 0, hw_tick)
        raw[4:8] = bytes((record_type, severity, source, len(detail)))
        raw[8:16] = detail
        events.append(link.BMCUMonitor._decode_event(bytes(raw)))

    class SnapshotMonitor:
        def __init__(self, index, state, parts, event_list):
            self.link_index = index
            self.link_state = state
            self.snapshot = parts
            self.events = event_list
            self.channels = [object()] * 4 if parts else [None] * 4
            self.bmcu_boot_session = 3
            self.tick_hz = 144000000
            self.sequence_gap_count = 0
            self.capture = None

    api = BinaryAPI(
        [SnapshotMonitor(0, "online", snapshot, events),
         SnapshotMonitor(1, "resyncing", [], [])],
        None, None, lambda: b"")
    (OUTPUT / "snapshot.bin").write_bytes(b"".join(api.snapshot_records()))

    (OUTPUT / "transport_status.txt").write_text(
        "configured=1\nhost=bambuddy.local\nport=8766\n", encoding="utf-8", newline="\n")
    (OUTPUT / "device_key_status.txt").write_text(
        "configured=1\nfingerprint=3f9a c1d0 77e2 4b18\n", encoding="utf-8", newline="\n")

    for name in sorted(os.listdir(OUTPUT)):
        print("wrote", (OUTPUT / name).relative_to(ROOT))


if __name__ == "__main__":
    main()
