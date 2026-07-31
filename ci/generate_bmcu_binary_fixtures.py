"""Generate deterministic cross-repository BMB1 fixtures with the Pico codec."""

import hashlib
import hmac
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


def crc16(data):
    value = 0xFFFF
    for octet in data:
        value ^= octet << 8
        for _ in range(8):
            value = (((value << 1) ^ 0x1021) & 0xFFFF
                     if value & 0x8000 else (value << 1) & 0xFFFF)
    return value


def bmcu(kind, sequence, payload):
    body = bytes((0x83, kind)) + sequence.to_bytes(2, "little")
    body += bytes((len(payload),)) + payload
    return b"\xA5\x5A" + body + crc16(body).to_bytes(2, "little")


def auth_transcript(device_id, firmware, links, replay_boot_ranges):
    result = bytes((len(device_id),)) + device_id
    result += bytes((len(firmware),)) + firmware + bytes((len(links),))
    for index, link_id in links:
        result += bytes((index, len(link_id))) + link_id
    result += bytes((len(replay_boot_ranges),))
    for boot_id, oldest, newest in replay_boot_ranges:
        result += (boot_id.to_bytes(8, "big") +
                   oldest.to_bytes(8, "big") +
                   newest.to_bytes(8, "big"))
    return result


def hmac256(key, *parts):
    value = hmac.new(key, digestmod=hashlib.sha256)
    for part in parts:
        value.update(part)
    return value.digest()


def main():
    boot = 0x0102030405060708
    key, challenge = bytes(range(32)), bytes(range(32, 64))
    device_id, firmware = b"pico-fixture", b"1.0.0"
    links = ((0, b"bmcu-a"), (1, b"bmcu-b"))
    oldest, newest = 7, 42
    replay_boot_ranges = (
        (boot, oldest, newest),
        (0x8877665544332211, 2, 9),
    )
    transcript = auth_transcript(
        device_id, firmware, links, replay_boot_ranges)
    hello_mac = hmac256(
        key, b"BMB1-AUTH", challenge, boot.to_bytes(8, "big"), transcript)

    records = {
        "server_challenge.bin": encoded(
            binary.write_message, C.SERVER_CHALLENGE, 0, 0, 0,
            0, challenge),
        "hello.bin": encoded(
            binary.write_hello, 0, boot, device_id, firmware, links,
            replay_boot_ranges, hello_mac),
        "link_state.bin": encoded(
            binary.write_link_state, 0, 8, boot, 0, 123456, C.LINK_ONLINE, 0),
        "transport_drop.bin": encoded(
            binary.write_transport_drop, 0, 9, boot, 123500, 3, 6, 4,
            C.DROP_RAM_QUEUE_FULL),
    }

    ack = (boot.to_bytes(8, "big") + bytes((C.GLOBAL_SCOPE, 0)) +
           b"\0\0" + (42).to_bytes(8, "big"))
    records["ack.bin"] = encoded(
        binary.write_message, C.ACK, 0, 0, boot, C.GLOBAL_SCOPE, ack)
    ack_reject = (boot.to_bytes(8, "big") + bytes((C.GLOBAL_SCOPE, 1)) +
                  b"\0\0" + (41).to_bytes(8, "big") +
                  (42).to_bytes(8, "big") + bytes((C.REJECT_MALFORMED,)))
    records["ack_reject.bin"] = encoded(
        binary.write_message, C.ACK, 0, 0, boot, C.GLOBAL_SCOPE, ack_reject)

    tlvs = bytearray(32)
    used = binary.write_tlv(tlvs, 0, 240, C.VALUE_UINT8, b"\x01")
    used += binary.write_tlv(tlvs, used, 241, C.VALUE_INT32, b"future")
    records["diagnostic_unknown_tags.bin"] = encoded(
        binary.write_diagnostic, 0, 10, boot, memoryview(tlvs)[:used])

    detail = bytearray(16)
    detail_size = binary.write_tlv(detail, 0, 1, C.VALUE_UINT8, b"\x02")
    records["pico_log_utf8.bin"] = encoded(
        binary.write_log, 0, 11, boot, 5, 9000, C.LOG_WARNING, b"uart",
        "通信警告".encode(), memoryview(detail)[:detail_size])

    session_key = hmac256(
        key, b"BMB1-SESSION", challenge, boot.to_bytes(8, "big"))
    unsigned_control = (
        (77).to_bytes(8, "big") + (500000).to_bytes(8, "big") +
        (5000).to_bytes(4, "big") + bytes((C.CONTROL_SOFT_RESET, 1, 1))
    )
    control_header = bytearray(C.HEADER_SIZE)
    binary.write_header(control_header, 0, C.CONTROL, 0,
                        len(unsigned_control) + 32, 0, boot, 0)
    control_mac = hmac256(session_key, control_header, unsigned_control)
    records["control.bin"] = encoded(
        binary.write_message, C.CONTROL, 0, 0, boot, 0,
        unsigned_control + control_mac)

    unsigned_result = (
        (77).to_bytes(8, "big") + bytes((C.RESULT_OK, 0)) +
        (8).to_bytes(2, "big") + b"accepted"
    )
    result_header = bytearray(C.HEADER_SIZE)
    binary.write_header(result_header, 0, C.CONTROL_RESULT, 0,
                        len(unsigned_result) + 32, 0, boot, 0)
    result_mac = hmac256(session_key, result_header, unsigned_result)
    records["control_result.bin"] = encoded(
        binary.write_control_result, 0, 0, boot, 0, 77, C.RESULT_OK,
        b"accepted", result_mac)

    max_detail = bytearray(C.MAX_LOG_DETAIL_BYTES)
    max_detail_size = binary.write_tlv(
        max_detail, 0, 2, C.VALUE_BYTES, b"d" * 508)
    records["pico_log_max.bin"] = encoded(
        binary.write_log, 0, 13, boot, 6, 9001, C.LOG_ERROR, b"c" * 40,
        b"m" * 320, memoryview(max_detail)[:max_detail_size])
    records["recovered_replay.bin"] = encoded(
        binary.write_log, C.FLAG_REPLAY | C.FLAG_JOURNALED, 2,
        0x8877665544332211, 1, 500, C.LOG_ERROR, b"boot",
        b"recovered crash", b"")

    for name, kind, sequence, payload in (
        ("bmcu_status.bin", 2, 1, bytes(range(27))),
        ("bmcu_event.bin", 3, 2, bytes(range(16))),
        ("bmcu_full_status.bin", 115, 3, bytes(range(26))),
        ("bmcu_unknown.bin", 126, 4, b"\xDE\xAD"),
    ):
        records[name] = encoded(
            binary.write_bmcu_frame, 0, 20 + sequence, boot, 0,
            100000 + sequence, bmcu(kind, sequence, payload))

    records["concatenated.bin"] = records["link_state.bin"] + records["ack.bin"]
    records["truncated_header.bin"] = records["hello.bin"][:17]

    fixture_dir = os.path.join(ROOT, "tests", "fixtures", "bmcu_binary")
    os.makedirs(fixture_dir, exist_ok=True)
    for name, raw in records.items():
        with open(os.path.join(fixture_dir, name), "wb") as handle:
            handle.write(raw)
    manifest = {
        "format": "bmcu-binary-fixtures-v1",
        "files": {
            name: {"bytes": len(raw),
                   "sha256": hashlib.sha256(raw).hexdigest()}
            for name, raw in sorted(records.items())
        },
        "auth": {
            "device_key_hex": key.hex(),
            "challenge_hex": challenge.hex(),
            "pico_boot_id": boot,
            "hello_hmac_hex": hello_mac.hex(),
        },
    }
    with open(os.path.join(fixture_dir, "manifest.json"), "w",
              encoding="utf-8") as handle:
        json.dump(manifest, handle, indent=2)
        handle.write("\n")


if __name__ == "__main__":
    main()
