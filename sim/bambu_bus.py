"""Printer stand-in on USART1 (Bambu bus over RS-485).

Framing, CRCs and short-frame types follow the public reverse-engineering
notes of the Bambu Research Group (github.com/Bambu-Research-Group/Bambu-Bus,
README "Headers and Devices Addressing"), not the BMCU firmware:

  short frame: 3D | flag>=0x80 | L | crc8(bytes 0..2) | type | payload | crc16 LE
  long frame:  3D | flag<0x80 | seq u16 | L u16 | crc8(bytes 0..5) | dst u16 | src u16 | ... | crc16 LE
  crc8:  poly 0x39, init 0x66, no reflection, no xorout
  crc16: poly 0x1021, init 0x913D, no reflection, no xorout, low byte first

The printer's half-duplex discipline is modelled too: the BMCU must assert
RS-485 DE (PA12, per docs/BMCU_UART_PHYSICAL_SPEC.md, a board fact) for every
byte it sends, and the stand-in never talks while DE is asserted.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .clock import HCLK_HZ

if TYPE_CHECKING:
    from .machine import Machine


def crc8(data: bytes, poly=0x39, init=0x66) -> int:
    c = init
    for b in data:
        c ^= b
        for _ in range(8):
            c = ((c << 1) ^ poly) & 0xFF if c & 0x80 else (c << 1) & 0xFF
    return c


def crc16(data: bytes, poly=0x1021, init=0x913D) -> int:
    c = init
    for b in data:
        c ^= b << 8
        for _ in range(8):
            c = ((c << 1) ^ poly) & 0xFFFF if c & 0x8000 else (c << 1) & 0xFFFF
    return c


def short_frame(ftype: int, payload: bytes, flag: int = 0xC5) -> bytes:
    length = 4 + 1 + len(payload) + 2
    head = bytes((0x3D, flag, length))
    body = head + bytes((crc8(head), ftype)) + payload
    c = crc16(body)
    return body + bytes((c & 0xFF, c >> 8))


def long_frame(ftype: int, payload: bytes, dst: int = 0x0700, src: int = 0x0600,
               seq: int = 0, flag: int = 0x05) -> bytes:
    # The layout bambubus_long_package_analysis reads: header fields are
    # little-endian, length counts the whole frame including the CRC16.
    length = 13 + len(payload) + 2
    head = bytes((0x3D, flag, seq & 0xFF, seq >> 8, length & 0xFF, length >> 8))
    body = (head + bytes((crc8(head),)) + dst.to_bytes(2, "little") +
            src.to_bytes(2, "little") + ftype.to_bytes(2, "little") + payload)
    c = crc16(body)
    return body + bytes((c & 0xFF, c >> 8))


@dataclass
class Frame:
    t: int
    raw: bytes
    kind: str            # "short" | "long"
    ok: bool
    ftype: int | None = None
    payload: bytes = b""
    errors: list[str] = field(default_factory=list)


def parse_frames(data: bytes, t: int = 0) -> list[Frame]:
    """Split a received byte run into frames and validate both CRCs."""
    out: list[Frame] = []
    i = 0
    while i < len(data):
        if data[i] != 0x3D:
            i += 1
            continue
        if i + 4 > len(data):
            break
        flag = data[i + 1]
        if flag >= 0x80:
            hlen, length = 4, data[i + 2]
        else:
            if i + 7 > len(data):
                break
            hlen, length = 7, data[i + 4] | data[i + 5] << 8
        raw = data[i:i + length]
        errs = []
        if len(raw) < length or length < hlen + 2:
            errs.append("truncated")
            out.append(Frame(t, raw, "short" if hlen == 4 else "long", False, errors=errs))
            break
        if crc8(raw[:hlen - 1]) != raw[hlen - 1]:
            errs.append("crc8")
        if crc16(raw[:-2]) != (raw[-2] | raw[-1] << 8):
            errs.append("crc16")
        if hlen == 4:
            out.append(Frame(t, raw, "short", not errs, raw[4], raw[5:-2], errs))
        else:
            out.append(Frame(t, raw, "long", not errs, None, raw[7:-2], errs))
        i += length
    return out


class PrinterStandIn:
    """Sends a scripted request stream and records the BMCU's replies.

    ``script`` is a list of (time_ms, frame_bytes).  A periodic heartbeat
    (short type 0x20) is added unless heartbeat_ms is None.
    """

    REPLY_GAP_US = 300   # silence that ends a reply

    def __init__(self, m: Machine, script=(), heartbeat_ms: int | None = 1000, de_pin=("A", 12)):
        self.m = m
        self.de_port, self.de_pin = de_pin
        self.replies: list[Frame] = []
        self.sent: list[tuple[int, bytes]] = []
        self.violations: list[str] = []
        self._rx = bytearray()
        self._rx_t = 0
        self._last_rx = 0
        m.usart1.peer = self._on_byte
        for t_ms, frame in script:
            m.at(int(t_ms * HCLK_HZ // 1000), lambda f=frame: self.send(f))
        if heartbeat_ms:
            self.heartbeat_ms = heartbeat_ms
            m.at(int(heartbeat_ms * HCLK_HZ // 1000), self._heartbeat)

    def de(self) -> int:
        return self.m.gpio[self.de_port].level(self.de_pin)

    def send(self, frame: bytes) -> None:
        if self.de():
            self.violations.append(f"{self.m.seconds:.6f}s printer would collide: BMCU DE asserted")
            self.m.after(HCLK_HZ // 1000, lambda f=frame: self.send(f))
            return
        self.sent.append((self.m.cycles, frame))
        self.m.usart1.rx(frame)

    def _heartbeat(self):
        self.send(short_frame(0x20, b""))
        self.m.after(self.heartbeat_ms * HCLK_HZ // 1000, self._heartbeat)

    def _on_byte(self, byte, t):
        if not self.de():
            self.violations.append(f"{t / HCLK_HZ:.6f}s BMCU sent {byte:#04x} with DE released")
        if not self._rx:
            self._rx_t = t
        self._rx.append(byte)
        self._last_rx = t
        self.m.at(t + HCLK_HZ * self.REPLY_GAP_US // 1_000_000, self._flush)

    def _flush(self):
        if self._rx and self.m.cycles - self._last_rx >= HCLK_HZ * self.REPLY_GAP_US // 1_000_000:
            self.replies.extend(parse_frames(bytes(self._rx), self._rx_t))
            self._rx.clear()
