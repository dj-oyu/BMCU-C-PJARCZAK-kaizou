"""Pico stand-in on USART3 (BMCU Link, H1).

It runs the production Pico decoder (pico/bmcu_link.py, BMCUMonitor) against
the simulated UART, so what the simulator observes is exactly what the real
monitor would have recorded from the same byte stream.  The Pico code is not
firmware code; it is the instrument whose recordings are the ground truth.
"""
from __future__ import annotations

import sys
from pathlib import Path

_PICO = Path(__file__).resolve().parent.parent / "pico"
if str(_PICO) not in sys.path:
    sys.path.insert(0, str(_PICO))

import bmcu_link  # noqa: E402  (pico/bmcu_link.py)

from .machine import HCLK_HZ, Machine  # noqa: E402


class _SimUart:
    """The subset of machine.UART that BMCUMonitor uses."""

    def __init__(self, peer: "LinkPeer"):
        self.peer = peer
        self.buf = bytearray()

    def any(self):
        return len(self.buf)

    def read(self, n=None):
        n = len(self.buf) if n is None else min(n, len(self.buf))
        out, self.buf[:] = bytes(self.buf[:n]), self.buf[n:]
        return out or None

    def write(self, data):
        self.peer.m.usart3.rx(bytes(data))
        return len(data)


class LinkPeer:
    POLL_MS = 5

    def __init__(self, m: Machine, poll_status_ms: int | None = 500):
        self.m = m
        self.uart = _SimUart(self)
        self.messages: list[dict] = []
        self.wire: list[tuple[int, int]] = []
        self.monitor = bmcu_link.BMCUMonitor(self.uart, on_message=self._on_message)
        self.poll_status_ms = poll_status_ms
        self._last_status_req = None
        m.usart3.peer = self._on_byte
        m.after(HCLK_HZ * self.POLL_MS // 1000, self._poll)

    def _on_byte(self, byte, t):
        self.uart.buf.append(byte)
        self.wire.append((t, byte))

    def now_ms(self):
        return self.m.cycles * 1000 // HCLK_HZ

    def _on_message(self, msg):
        msg = dict(msg)
        msg.setdefault("sim_ms", self.now_ms())
        self.messages.append(msg)

    def _poll(self):
        now = self.now_ms()
        self.monitor.poll_uart(now)
        if self.poll_status_ms and self.monitor.last_valid_ms is not None and (
                self._last_status_req is None or now - self._last_status_req >= self.poll_status_ms):
            self._last_status_req = now
            self.monitor.get_status()
        self.m.after(HCLK_HZ * self.POLL_MS // 1000, self._poll)

    def kinds(self):
        return [m.get("type") or m.get("kind") for m in self.messages]
