"""Generate the canonical BMB1 binary interoperability fixtures."""

import hashlib
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "pico"))

import bmcu_binary as binary
import bmcu_binary_constants as C


def encoded(writer, *args):
    out = bytearray(C.MAX_MESSAGE_SIZE)
    size = writer(out, 0, *args)
    return bytes(memoryview(out)[:size])


def main():
    fixture_dir = os.path.join(ROOT, "tests", "fixtures")
    paths = {
        "bmcu_frame.bin": encoded(
            binary.write_bmcu_frame, C.FLAG_JOURNALED, 7,
            0x0102030405060708, 0, 123456789,
            bytes.fromhex(
                "a55a830202001b000102030405060708090a0b0c0d0e0f"
                "101112131415161718191a929c"
            )
        ),
        "link_state.bin": encoded(
            binary.write_link_state, 0, 8, 0x0102030405060708, 1,
            123456999, C.LINK_ONLINE, 0
        ),
        "max_log.bin": encoded(
            binary.write_log, C.FLAG_CRITICAL, 9, 0x0102030405060708,
            17, 123456, C.LOG_CRITICAL, b"C" * 40, b"M" * 320, b"D" * 512
        ),
    }
    for name, data in paths.items():
        with open(os.path.join(fixture_dir, name), "wb") as handle:
            handle.write(data)
    manifest_path = os.path.join(fixture_dir, "bmcu_binary_manifest.json")
    with open(manifest_path, "r", encoding="utf-8") as handle:
        manifest = json.load(handle)
    for name, data in paths.items():
        manifest["fixtures"][name]["sha256"] = hashlib.sha256(data).hexdigest()
    with open(manifest_path, "w", encoding="utf-8") as handle:
        json.dump(manifest, handle, indent=2, sort_keys=True)
        handle.write("\n")


if __name__ == "__main__":
    main()
