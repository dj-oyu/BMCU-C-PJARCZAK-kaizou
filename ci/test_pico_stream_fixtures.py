"""The committed BMS2 fixtures are what the Pico client produces today.

The receiver repository decodes these bytes; a silent drift here would make it
test against a format the bridge no longer speaks.
"""

import hashlib
import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "ci"))

import generate_bmcu_stream_fixtures as generator  # noqa: E402
from test_bmcu_binary_e2e import parse_datagram, parse_heartbeat  # noqa: E402

FIXTURES = os.path.join(ROOT, "tests", "fixtures", "bmcu_stream")


def read(name):
    with open(os.path.join(FIXTURES, name), "rb") as handle:
        return handle.read()


class StreamFixtureTests(unittest.TestCase):
    def test_committed_bytes_match_the_client(self):
        heartbeat, records = generator.build()
        self.assertEqual(read("heartbeat.bin"), heartbeat,
                         "run python ci/generate_bmcu_stream_fixtures.py")
        self.assertEqual(read("records.bin"), records,
                         "run python ci/generate_bmcu_stream_fixtures.py")

    def test_manifest_hashes_and_expectations_hold(self):
        manifest = json.loads(read("manifest.json"))
        self.assertEqual(manifest,
                         generator.manifest(read("heartbeat.bin"),
                                            read("records.bin")))
        for name, entry in manifest["files"].items():
            data = read(name)
            self.assertEqual(entry["bytes"], len(data))
            self.assertEqual(entry["sha256"], hashlib.sha256(data).hexdigest())
        for name, expected in manifest["expect"].items():
            device_id, records = parse_datagram(read(name))
            self.assertEqual(device_id.decode(), expected["device_id"])
            self.assertEqual(len(records), len(expected["records"]))
            for record, want in zip(records, expected["records"]):
                self.assertEqual(record["type"], want["message_type"])
                self.assertEqual(record["sequence"],
                                 want["transport_sequence"])
                self.assertEqual(record["boot"], want["pico_boot_id"])
                self.assertEqual(record["link"], want["link_index"])
                if "heartbeat" in want:
                    beat = parse_heartbeat(record["payload"])
                    hb = want["heartbeat"]
                    self.assertEqual(beat["interval_ms"],
                                     hb["heartbeat_interval_ms"])
                    self.assertEqual(beat["uptime_ms"], hb["uptime_ms"])
                    self.assertEqual(beat["next_sequence"],
                                     hb["next_transport_sequence"])
                    self.assertEqual(beat["firmware"].decode(),
                                     hb["firmware"])
                    self.assertEqual(
                        [(i, l.decode(), s) for i, l, s in beat["links"]],
                        [(link["link_index"], link["link_id"],
                          link["link_state"]) for link in hb["links"]])
                if "bmcu_wire_hex" in want:
                    payload = record["payload"]
                    self.assertEqual(
                        int.from_bytes(payload[0:8], "big"),
                        want["received_at_us"])
                    self.assertEqual(payload[10:].hex(), want["bmcu_wire_hex"])


if __name__ == "__main__":
    unittest.main()
