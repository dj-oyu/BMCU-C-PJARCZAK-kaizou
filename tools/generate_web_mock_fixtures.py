"""Render the Pico's HTTP responses into fixtures the Vite dev server replays.

The point is to develop the browser UI without a device on the bench. Encoding
goes through the real ``pico/`` codec so a mock response can never drift from
what the hardware actually serves.
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "pico"))

import bmcu_binary as binary  # noqa: E402
import bmcu_binary_constants as C  # noqa: E402
import bmcu_link as link  # noqa: E402
import device_metrics  # noqa: E402
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
    (OUTPUT / "transport_status.txt").write_text(
        "configured=1\nhost=bambuddy.local\nport=8766\n", encoding="utf-8", newline="\n")
    (OUTPUT / "device_key_status.txt").write_text(
        "configured=1\nfingerprint=3f9a c1d0 77e2 4b18\n", encoding="utf-8", newline="\n")

    for name in sorted(os.listdir(OUTPUT)):
        print("wrote", (OUTPUT / name).relative_to(ROOT))


if __name__ == "__main__":
    main()
