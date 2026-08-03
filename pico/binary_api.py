"""Read-only diagnostic endpoints served over the local HTTP interface.

Endpoints are grouped by how often their data changes, not by which page reads
them, because the Pico serves one HTTP client at a time: every extra request a
page needs is another serialised round trip. A page-shaped API would also have
to re-serialise the boot-time snapshot on every three-second poll.

    /api/current.bin      latest STATUS per link          changes many times a second
    /api/diagnostics.bin  bridge counters                 recomputed every 15 s
    /api/snapshot.bin     per-link detail                 arrives once per link session
    /api/logs.bin         runtime log                     cursor paginated
    /api/events.bin       durable queue                   cursor paginated
    /api/capture.bin      rejected UART runs              bounded ring

BSNP and BCAP are deliberately not BMB1 message types. They are read straight
off the device and never sent to Bambuddy, so they do not belong in the wire
registry.
"""

try:
    import ustruct as struct
except ImportError:
    import struct

import bmcu_binary_constants as C

CAPTURE_MAGIC = b"BCAP"
CAPTURE_HEADER = 16

SNAPSHOT_MAGIC = b"BSNP"
SNAPSHOT_HEADER = 16
SNAPSHOT_VERSION = 1

# Synthetic record types. Real FULL_STATUS record types are 1..127 and are
# passed through untouched, so these sit above them.
RECORD_LINK = 0xF0
RECORD_EVENT = 0xF1
LINK_RECORD_BYTES = 16

# Mirrors the link_states group of docs/bmcu_binary_registry.json.
LINK_STATE_CODES = {
    "unknown": 0, "resyncing": 1, "online": 2, "stale": 3,
    "offline": 4, "incompatible": 5,
}


def query_number(path, name, default, maximum):
    marker = name + "="
    if marker not in path:
        return default
    value = path.split(marker, 1)[1].split("&", 1)[0]
    try:
        return min(maximum, max(0, int(value)))
    except ValueError:
        return default


def _snapshot_record(link_index, record_type, record_index, hw_tick32, data):
    """One BSNP record, big-endian.

        0  4s magic "BSNP"
        4  B  version
        5  B  link index
        6  B  record type
        7  B  record index within the snapshot
        8  I  hw_tick32 as reported by the BMCU
       12  H  reserved
       14  H  payload length
       16 ..  payload, verbatim
    """
    header = bytearray(SNAPSHOT_HEADER)
    header[0:4] = SNAPSHOT_MAGIC
    header[4] = SNAPSHOT_VERSION
    header[5] = link_index
    header[6] = record_type
    header[7] = record_index
    struct.pack_into(">IHH", header, 8, hw_tick32 & 0xFFFFFFFF, 0, len(data))
    return bytes(header) + bytes(data)


def _link_record(monitor):
    """Link state that no FULL_STATUS record carries.

        0  B  link state code      4  I  bmcu boot session
        1  B  channels present     8  I  tick_hz, 0 when the BMCU has not said
        2  H  reserved            12  I  sequence gap count
    """
    body = bytearray(LINK_RECORD_BYTES)
    body[0] = LINK_STATE_CODES.get(monitor.link_state, 0)
    body[1] = sum(1 for channel in monitor.channels if channel is not None)
    struct.pack_into(
        ">III", body, 4,
        monitor.bmcu_boot_session & 0xFFFFFFFF,
        (monitor.tick_hz or 0) & 0xFFFFFFFF,
        monitor.sequence_gap_count & 0xFFFFFFFF)
    return bytes(body)


class BinaryAPI:
    """Routes local diagnostic reads. Holds no state of its own."""

    def __init__(self, monitors, outbox, runtime_log, diagnostic_message):
        self.monitors = monitors
        self.outbox = outbox
        self.runtime_log = runtime_log
        self.diagnostic_message = diagnostic_message

    def capture_records(self):
        """Rejected UART runs.

            0  4s magic "BCAP"      8  I  capture ordinal for that link
            4  B  version           12 H  uptime_ms & 0xFFFF at capture time
            5  B  link index        14 H  payload length
            6  B  reject reason (1 CRC, 2 bad length, 3 oversized chunk)
            7  B  reserved          16 .. the rejected bytes
        """
        out = []
        for monitor in self.monitors:
            capture = getattr(monitor, "capture", None)
            if capture is None:
                continue
            for ordinal, reason, timestamp, data in capture.records():
                header = bytearray(CAPTURE_HEADER)
                header[0:4] = CAPTURE_MAGIC
                header[4] = 1
                header[5] = monitor.link_index
                header[6] = reason
                struct.pack_into(
                    ">IHH", header, 8, ordinal & 0xFFFFFFFF,
                    timestamp & 0xFFFF, len(data))
                out.append(bytes(header) + bytes(data))
        return out

    def snapshot_records(self):
        """Per-link detail: channels, printer and AMS counters, recent events.

        The FULL_STATUS payloads are handed back exactly as the BMCU sent them.
        Decoding lives in one place on the browser side; re-encoding here would
        put a second copy of the wire layout on the device.
        """
        out = []
        for monitor in self.monitors:
            link = monitor.link_index
            out.append(_snapshot_record(
                link, RECORD_LINK, 0, 0, _link_record(monitor)))
            for index, part in enumerate(monitor.snapshot or ()):
                data = part.get("record_data")
                if not data:
                    continue
                out.append(_snapshot_record(
                    link, part.get("record_type", 0), index,
                    part.get("hw_tick32", 0), data))
            for index, event in enumerate(monitor.events):
                raw = event.get("raw")
                if raw:
                    out.append(_snapshot_record(
                        link, RECORD_EVENT, index,
                        event.get("hw_tick32", 0), raw))
        return out

    def __call__(self, path):
        base = path.split("?", 1)[0]
        if base == "/api/diagnostics.bin":
            return self.diagnostic_message()
        if base in ("/api/current.bin", "/api/history/status.bin"):
            return list(self.outbox.iter_current())
        if base == "/api/snapshot.bin":
            return self.snapshot_records()
        if base == "/api/capture.bin":
            return self.capture_records()
        if base == "/api/logs.bin":
            after = query_number(path, "after", 0, 0xFFFFFFFFFFFFFFFF)
            limit = query_number(path, "limit", 32, 64)
            return list(self.runtime_log.iter_messages(after, limit))
        if base == "/api/events.bin":
            after = query_number(path, "after", 0, 0xFFFFFFFFFFFFFFFF)
            limit = query_number(path, "limit", 32, 64)
            result = []
            used = 0
            for sequence, message, _, _ in self.outbox.durable.iter_records():
                if sequence <= after:
                    continue
                if len(result) >= limit or used + len(message) > 32768:
                    break
                result.append(message)
                used += len(message)
            return result
        return None
