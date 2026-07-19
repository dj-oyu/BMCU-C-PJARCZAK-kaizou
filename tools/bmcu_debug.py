#!/usr/bin/env python3
"""Safe BMCU UART monitor and command client; never flashes firmware."""

import argparse
import sys
import time
import struct
from datetime import datetime

import serial
from serial.tools import list_ports

from bmcu_link import *


PARITY = {
    "none": serial.PARITY_NONE,
    "even": serial.PARITY_EVEN,
    "odd": serial.PARITY_ODD,
}


def open_serial(port_name, baud, parity):
    port = serial.Serial(
        port=None,
        baudrate=baud,
        bytesize=serial.EIGHTBITS,
        parity=PARITY[parity],
        stopbits=serial.STOPBITS_ONE,
        timeout=0.1,
        write_timeout=1,
        rtscts=False,
        dsrdtr=False,
    )
    # Match BMCU-Flasher's released/idle line state after a completed flash.
    port.dtr = True
    port.rts = True
    port.port = port_name
    port.open()
    return port


def open_port(args):
    return open_serial(args.port, args.baud, args.parity)


def show(raw):
    try:
        frame = decode_frame(raw)
        now = datetime.now().isoformat(timespec="milliseconds")
        print(
            f"{now} {kind_name(frame.kind)} seq={frame.sequence} "
            f"v={frame.version} payload={frame.payload.hex()}"
        )
    except LinkError as error:
        now = datetime.now().isoformat(timespec="milliseconds")
        print(f"{now} INVALID ({error}): {raw.hex()}")


def read_wire_frame(port, deadline):
    state = 0
    body = bytearray()
    expected = None
    byte_count = 0
    while time.monotonic() < deadline:
        data = port.read(1)
        byte_count += len(data)
        if not data:
            continue
        value = data[0]
        if state == 0:
            if value == SYNC[0]:
                state = 1
            continue
        if state == 1:
            if value == SYNC[1]:
                state = 2
                body.clear()
                expected = None
            elif value != SYNC[0]:
                state = 0
            continue

        body.append(value)
        if len(body) == 5:
            payload_length = body[4]
            if payload_length > MAX_DECODED_FRAME - 7:
                state = 1 if value == SYNC[0] else 0
                body.clear()
                expected = None
                continue
            expected = payload_length + 7
        if expected is not None and len(body) == expected:
            return SYNC + bytes(body), byte_count
    return None, byte_count


def read_one(port, deadline):
    raw, _ = read_wire_frame(port, deadline)
    if raw is None:
        return False
    show(raw)
    return True


def read_valid_frame(port, deadline):
    byte_count = 0
    while time.monotonic() < deadline:
        raw, consumed = read_wire_frame(port, deadline)
        byte_count += consumed
        if raw is None:
            break
        try:
            return decode_frame(raw), byte_count
        except LinkError:
            pass
    return None, byte_count

def csv_values(value, cast=str):
    try:
        values = [cast(item.strip()) for item in value.split(",") if item.strip()]
    except ValueError as error:
        raise SystemExit(f"Invalid comma-separated value: {value}") from error
    if not values:
        raise SystemExit("Scan list must not be empty")
    return values


def scan(args):
    port_names = args.port or [item.device for item in list_ports.comports()]
    if args.active and not args.port:
        raise SystemExit("--active requires at least one explicit --port")
    bauds = csv_values(args.bauds, int)
    parities = csv_values(args.parities)
    unknown = [item for item in parities if item not in PARITY]
    if unknown:
        raise SystemExit(f"Unknown parity: {', '.join(unknown)}")
    if not port_names:
        print("No serial ports found.", file=sys.stderr)
        return 2

    found = []
    for port_name in port_names:
        for baud in bauds:
            for parity in parities:
                label = f"{port_name} {baud} 8{parity[0].upper()}1"
                try:
                    with open_serial(port_name, baud, parity) as port:
                        port.reset_input_buffer()
                        frame, byte_count = read_valid_frame(
                            port, time.monotonic() + args.listen_seconds
                        )
                        if frame is None and args.active:
                            port.write(encode_frame(KIND_GET_STATUS, 0x5A5A, b""))
                            port.flush()
                            frame, more = read_valid_frame(
                                port, time.monotonic() + args.probe_timeout
                            )
                            byte_count += more
                        if frame is not None:
                            print(
                                f"MATCH {label}: {kind_name(frame.kind)} "
                                f"seq={frame.sequence} payload={frame.payload.hex()}"
                            )
                            found.append((port_name, baud, parity))
                        else:
                            print(f"MISS  {label}: {byte_count} byte(s)")
                except (OSError, serial.SerialException) as error:
                    print(f"SKIP  {label}: {error}")
    if found:
        print("Detected BMCU link setting(s):")
        for port_name, baud, parity in found:
            print(f"  --port {port_name} --baud {baud} --parity {parity}")
        return 0
    print("No valid BMCU frames detected.", file=sys.stderr)
    return 2


def cmd_list(_args):
    for port in list_ports.comports():
        vid_pid = (
            f" VID:PID={port.vid:04X}:{port.pid:04X}"
            if port.vid is not None and port.pid is not None
            else ""
        )
        print(f"{port.device}: {port.description}{vid_pid}")
    return 0


def monitor(args):
    with open_port(args) as port:
        print(f"Monitoring {args.port} at {args.baud} 8{args.parity[0].upper()}1. Ctrl+C to stop.")
        try:
            while True:
                read_one(port, time.monotonic() + 3600)
        except KeyboardInterrupt:
            print("\nStopped.")
    return 0


def raw_monitor(args):
    with open_port(args) as port:
        port.reset_input_buffer()
        print(f"Raw monitoring {args.port} at {args.baud} 8{args.parity[0].upper()}1.")
        deadline = None if args.seconds <= 0 else time.monotonic() + args.seconds
        count = 0
        try:
            while deadline is None or time.monotonic() < deadline:
                data = port.read(port.in_waiting or 1)
                if not data:
                    continue
                count += len(data)
                text = data.decode("ascii", "backslashreplace")
                print(f"RX {data.hex(' ')}  {text!r}", flush=True)
        except KeyboardInterrupt:
            print("\nStopped.")
        print(f"Received {count} byte(s).")
    return 0


def send(args, kind, payload):
    if not 0 <= args.sequence <= 65535:
        raise SystemExit("--sequence must be 0..65535")
    with open_port(args) as port:
        port.reset_input_buffer()
        port.write(encode_frame(kind, args.sequence, payload))
        port.flush()
        print(f"TX {kind_name(kind)} seq={args.sequence} payload={payload.hex()}")
        if not read_one(port, time.monotonic() + args.timeout):
            print("No response before timeout.", file=sys.stderr)
            return 2
    return 0


def ping(args):
    if not 0 <= args.token <= 0xFFFFFFFF:
        raise SystemExit("--token must be 0..4294967295")
    if not 0 <= args.sequence <= 65535:
        raise SystemExit("--sequence must be 0..65535")
    token = args.token.to_bytes(4, "little")
    with open_port(args) as port:
        port.reset_input_buffer()
        started = time.monotonic()
        port.write(encode_frame(KIND_PING, args.sequence, token))
        port.flush()
        deadline = started + args.timeout
        while time.monotonic() < deadline:
            frame, _ = read_valid_frame(port, deadline)
            if frame is None:
                break
            now = datetime.now().isoformat(timespec="milliseconds")
            print(
                f"{now} {kind_name(frame.kind)} seq={frame.sequence} "
                f"v={frame.version} payload={frame.payload.hex()}"
            )
            if frame.kind == KIND_PONG and frame.sequence == args.sequence:
                if len(frame.payload) != 8 or frame.payload[:4] != token:
                    print("Malformed PONG payload.", file=sys.stderr)
                    return 2
                hw_tick32 = int.from_bytes(frame.payload[4:8], "little")
                rtt_ms = (time.monotonic() - started) * 1000.0
                print(f"BMCU alive: hw_tick32={hw_tick32} (0x{hw_tick32:08X}) rtt={rtt_ms:.1f}ms")
                return 0
        print("No matching PONG before timeout.", file=sys.stderr)
        return 2


def format_full_status_record(record):
    data = record.data
    if record.record_type == FULL_RECORD_GLOBAL:
        slot, inserted, online, error = data[:4]
        pressure = int.from_bytes(data[12:14], "little")
        return (
            f"GLOBAL slot={slot} inserted=0x{inserted:02X} online=0x{online:02X} "
            f"error={error} motion={list(data[4:8])} pull={list(data[8:12])} "
            f"pressure={pressure} led={data[14]}"
        )
    if record.record_type == FULL_RECORD_CHANNEL:
        ch, motion, inserted, online, pull, validity, flags, angle, delta, pwm, _ = struct.unpack(
            "<BBBBBBHHhhH", data
        )
        return (
            f"CHANNEL ch={ch} motion={motion} inserted={inserted} online={online} "
            f"pull={pull}% validity={validity} flags=0x{flags:04X} "
            f"angle={angle} delta={delta} pwm={pwm}"
        )
    if record.record_type == FULL_RECORD_PRINTER_BUS:
        online, rx_class, command, outcome, valid_rx, invalid_rx, tx, tx_drop, age = struct.unpack(
            "<BBBBHHHHI", data
        )
        return (
            f"PRINTER online={online} class={rx_class} command=0x{command:02X} "
            f"outcome={outcome} valid_rx={valid_rx} invalid_rx={invalid_rx} "
            f"tx={tx} tx_drop={tx_drop} age_ticks={age}"
        )
    if record.record_type == FULL_RECORD_PRINTER_AUTH:
        trace = decode_printer_auth_trace(data)
        return (
            f"PRINTER_AUTH type=0x{trace.last_type:04X} 040D={trace.count_040d} "
            f"040E={trace.count_040e} payload_len={trace.payload_length} "
            f"tick={trace.hw_tick32} outcome={trace.outcome} reason={trace.reason} "
            f"response_len={trace.response_length} hash=0x{trace.payload_hash:02X}"
        )
    if record.record_type == FULL_RECORD_COUNTERS:
        tx_drop, rx_drop, crc_error, frame_error = struct.unpack("<IIII", data)
        return (
            f"COUNTERS tx_drop={tx_drop} rx_drop={rx_drop} "
            f"crc_error={crc_error} frame_error={frame_error}"
        )
    return f"TYPE_{record.record_type} data={data.hex()}"


def full_status(args):
    if not 0 <= args.sequence <= 65535:
        raise SystemExit("--sequence must be 0..65535")
    if not 0 <= args.section_mask <= FULL_SECTION_ALL:
        raise SystemExit("--section-mask must be 1..15")
    if not 0 <= args.channel_mask <= 0x0F:
        raise SystemExit("--channel-mask must be 0..15")

    with open_port(args) as port:
        port.reset_input_buffer()
        payload = bytes([args.section_mask, args.channel_mask])
        port.write(encode_frame(KIND_GET_FULL_STATUS, args.sequence, payload))
        port.flush()
        deadline = time.monotonic() + args.timeout
        records = {}
        snapshot_id = None
        expected_count = None
        while time.monotonic() < deadline:
            frame, _ = read_valid_frame(port, deadline)
            if frame is None:
                break
            if frame.kind == KIND_ACK and frame.sequence == args.sequence:
                print(f"Request rejected: {frame.payload.hex()}", file=sys.stderr)
                return 2
            if frame.kind != KIND_FULL_STATUS_RECORD or frame.sequence != args.sequence:
                continue
            record = decode_full_status_record(frame.payload)
            if snapshot_id is None:
                snapshot_id = record.snapshot_id
                expected_count = record.count
            if record.snapshot_id != snapshot_id or record.count != expected_count:
                print("Inconsistent snapshot metadata.", file=sys.stderr)
                return 2
            if record.index in records:
                print(f"Duplicate record index {record.index}.", file=sys.stderr)
                return 2
            records[record.index] = record
            if len(records) == expected_count:
                for index in range(expected_count):
                    print(format_full_status_record(records[index]))
                print(
                    f"Complete snapshot id={snapshot_id} records={expected_count} "
                    f"hw_tick32={records[0].hw_tick32}"
                )
                return 0
        print(
            f"Incomplete snapshot: received={len(records)} expected={expected_count}",
            file=sys.stderr,
        )
        return 2


def serial_options(parser, default_parity="even"):
    parser.add_argument("--port", required=True)
    parser.add_argument("--baud", type=int, default=115200)
    parser.add_argument("--parity", choices=PARITY, default=default_parity)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    command = commands.add_parser("list")
    command.set_defaults(func=cmd_list)

    command = commands.add_parser("scan")
    command.add_argument("--port", action="append", help="explicit port; repeatable")
    command.add_argument("--bauds", default="57600,115200,230400")
    command.add_argument("--parities", default="even,none,odd")
    command.add_argument("--listen-seconds", type=float, default=1.2)
    command.add_argument("--active", action="store_true", help="send GET_STATUS probes")
    command.add_argument("--probe-timeout", type=float, default=0.5)
    command.set_defaults(func=scan)

    command = commands.add_parser("monitor")
    serial_options(command)
    command.set_defaults(func=monitor)

    command = commands.add_parser("raw")
    serial_options(command, default_parity="none")
    command.add_argument("--seconds", type=float, default=0)
    command.set_defaults(func=raw_monitor)

    command = commands.add_parser("status")
    serial_options(command)
    command.add_argument("--sequence", type=int, default=1)
    command.add_argument("--timeout", type=float, default=2)
    command.set_defaults(func=lambda args: send(args, KIND_GET_STATUS, b""))

    command = commands.add_parser("full-status")
    serial_options(command)
    command.add_argument("--section-mask", type=lambda value: int(value, 0), default=FULL_SECTION_ALL)
    command.add_argument("--channel-mask", type=lambda value: int(value, 0), default=0x0F)
    command.add_argument("--sequence", type=int, default=1)
    command.add_argument("--timeout", type=float, default=3)
    command.set_defaults(func=full_status)

    command = commands.add_parser("ping")
    serial_options(command)
    command.add_argument("--token", type=lambda value: int(value, 0), default=0x12345678)
    command.add_argument("--sequence", type=int, default=1)
    command.add_argument("--timeout", type=float, default=2)
    command.set_defaults(func=ping)

    command = commands.add_parser("led")
    serial_options(command)
    command.add_argument("--mode", type=int, required=True)
    command.add_argument("--timeout-s", type=int, default=10)
    command.add_argument("--sequence", type=int, default=1)
    command.add_argument("--timeout", type=float, default=2)
    command.set_defaults(
        func=lambda args: send(
            args,
            KIND_SET_LED_MODE,
            bytes([args.mode]) + args.timeout_s.to_bytes(2, "little"),
        )
    )

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())