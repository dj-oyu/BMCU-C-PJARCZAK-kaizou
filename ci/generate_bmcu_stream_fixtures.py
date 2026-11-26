"""Generate the shared BMS2 datagram fixtures with the Pico's own client.

The receiver (dj-oyu/bambuddy) asserts its decoder against these bytes and
against the decoded expectations in manifest.json. Regenerate with:

    python ci/generate_bmcu_stream_fixtures.py

ci/test_pico_stream_fixtures.py fails when the committed files drift from what
the client produces.
"""

import hashlib
import json
import os
import struct
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "pico"))

import bmcu_binary_constants as C  # noqa: E402
import bmcu_stream_udp as stream  # noqa: E402
from bmcu_binary_outbox import BMB1Outbox  # noqa: E402

OUT = os.path.join(ROOT, "tests", "fixtures", "bmcu_stream")
BOOT = 0x0102030405060708
DEVICE_ID = b"pico-bmcu-bridge"
# A valid alpha3 EVENT frame as it arrives from the BMCU link: opaque here.
EVENT_WIRE = bytes.fromhex("a55a8303070010") + bytes(16) + b"\x12\x34"


class Capture:
    def __init__(self):
        self.datagrams = []

    def sendto(self, data, address):
        self.datagrams.append(bytes(data))
        return len(data)

    def close(self):
        pass


def build():
    outbox = BMB1Outbox(BOOT, link_count=2, durable_slots=16)
    sock = Capture()
    client = stream.BMS2UDPClient(
        outbox, "receiver", 8799, DEVICE_ID, b"alpha.3",
        ((0, b"bmcu-a"), (1, b"bmcu-b")),
        link_states=lambda: (C.LINK_ONLINE, C.LINK_STALE),
        uptime_ms=lambda: 123456789, socket_factory=lambda: sock,
        resolver=lambda host, port: (host, port), heartbeat_ms=5000)
    outbox.enqueue_link_state(1, 1000, C.LINK_ONLINE, 1)
    outbox.enqueue_raw(0, 2000, EVENT_WIRE, {"kind": 3, "sequence": 7})
    outbox.enqueue_payload(C.PICO_DIAGNOSTIC, 0, C.GLOBAL_SCOPE, 3000,
                           struct.pack(">BBHQ", C.DIAG_UPTIME_MS,
                                       C.VALUE_UINT64, 8, 123456),
                           False)
    client.poll(0)
    heartbeat, records = sock.datagrams
    return heartbeat, records


def manifest(heartbeat, records):
    def entry(data):
        return {"bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest()}

    return {
        "format": "bmcu-stream-fixtures-v1",
        "spec": "docs/BMCU_MONITOR_STREAM_V2.md",
        "files": {"heartbeat.bin": entry(heartbeat),
                  "records.bin": entry(records)},
        "expect": {
            "heartbeat.bin": {
                "device_id": DEVICE_ID.decode(),
                "records": [{
                    "message_type": C.HEARTBEAT, "transport_sequence": 0,
                    "pico_boot_id": BOOT, "link_index": C.GLOBAL_SCOPE,
                    "heartbeat": {
                        "heartbeat_interval_ms": 5000,
                        "uptime_ms": 123456789,
                        "next_transport_sequence": 4,
                        "firmware": "alpha.3",
                        "links": [
                            {"link_index": 0, "link_id": "bmcu-a",
                             "link_state": C.LINK_ONLINE},
                            {"link_index": 1, "link_id": "bmcu-b",
                             "link_state": C.LINK_STALE}],
                    }}],
            },
            "records.bin": {
                "device_id": DEVICE_ID.decode(),
                "records": [
                    {"message_type": C.LINK_STATE, "transport_sequence": 1,
                     "pico_boot_id": BOOT, "link_index": 1},
                    {"message_type": C.BMCU_FRAME, "transport_sequence": 2,
                     "pico_boot_id": BOOT, "link_index": 0,
                     "received_at_us": 2000,
                     "bmcu_wire_hex": EVENT_WIRE.hex()},
                    {"message_type": C.PICO_DIAGNOSTIC,
                     "transport_sequence": 3, "pico_boot_id": BOOT,
                     "link_index": C.GLOBAL_SCOPE},
                ],
            },
        },
    }


def main():
    heartbeat, records = build()
    os.makedirs(OUT, exist_ok=True)
    for name, data in (("heartbeat.bin", heartbeat),
                       ("records.bin", records)):
        with open(os.path.join(OUT, name), "wb") as handle:
            handle.write(data)
    with open(os.path.join(OUT, "manifest.json"), "w", encoding="utf-8",
              newline="\n") as handle:
        json.dump(manifest(heartbeat, records), handle, indent=2,
                  sort_keys=True)
        handle.write("\n")


if __name__ == "__main__":
    main()
