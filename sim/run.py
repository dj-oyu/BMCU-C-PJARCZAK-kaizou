"""Command-line entry: boot a BMCU image and record what the Pico and the printer see.

    python -m sim.run firmware.bin --seconds 8 --trace out.jsonl

The trace is JSON Lines.  Every record has ``t`` (virtual seconds) and
``src``:

- ``pico``     a message exactly as pico/bmcu_link.py's BMCUMonitor emits it
- ``printer``  a frame the printer stand-in sent (``dir: "tx"``) or a BMCU
               reply it received and CRC-checked (``dir: "rx"``)
- ``flash``    an erase/program operation seen by the flash controller
- ``bus``      an RS-485 half-duplex violation
- ``sim``      end-of-run summary (fault, unmodelled registers, speed)
"""
from __future__ import annotations

import argparse
import json
import time

from .bambu_bus import PrinterStandIn, short_frame
from .clock import HCLK_HZ
from .link_peer import LinkPeer
from .machine import Machine
from .periph import build_ch32v203

# Printer requests used by the default scenario: the AMS-lite "read filament
# motion" short frame (type 0x03) for this AMS with no channel selected.  The
# payload layout (ams, status, channel, flag) is the public Bambu-Bus short
# frame; 0xFF means "no channel".
def idle_motion_query(ams: int = 0) -> bytes:
    return short_frame(0x03, bytes((ams, 0x00, 0xFF, 0x00)))


def make_machine(image: bytes) -> Machine:
    m = Machine(image)
    build_ch32v203(m)
    return m


def default_scenario(ams=0, start_ms=5600, end_ms=8000, period_ms=200):
    return [(t, idle_motion_query(ams)) for t in range(start_ms, end_ms, period_ms)]


def run(image: bytes, seconds: float, script=None, ams: int = 0):
    m = make_machine(image)
    pico = LinkPeer(m)
    printer = PrinterStandIn(m, default_scenario(ams) if script is None else script)
    t0 = time.time()
    m.run_for(seconds)
    return m, pico, printer, time.time() - t0


def trace_records(m, pico, printer, wall):
    recs = []
    for msg in pico.messages:
        recs.append({"t": msg.get("sim_ms", 0) / 1000, "src": "pico", **msg})
    for t, frame in printer.sent:
        recs.append({"t": t / HCLK_HZ, "src": "printer", "dir": "tx", "hex": frame.hex()})
    for f in printer.replies:
        recs.append({"t": f.t / HCLK_HZ, "src": "printer", "dir": "rx", "kind": f.kind, "ok": f.ok,
                     "type": f.ftype, "errors": f.errors, "hex": f.raw.hex()})
    for t, op, off, length in m.flashctl.log:
        recs.append({"t": t / HCLK_HZ, "src": "flash", "op": op, "offset": off, "length": length})
    for v in printer.violations:
        recs.append({"t": float(v.split("s ", 1)[0]), "src": "bus", "violation": v})
    recs.sort(key=lambda r: r["t"])
    recs.append({"t": m.seconds, "src": "sim", "fault": m.fault, "wall_s": round(wall, 2),
                 "skipped_s": round(m.skipped_cycles / HCLK_HZ, 3), "xw_insns": m.xw_count,
                 "flash_unerased_writes": m.flashctl.unerased_writes,
                 "unmodelled": {f"{k:#x}": v for k, v in sorted(m.unmodelled.items())}})
    return recs


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("image")
    ap.add_argument("--seconds", type=float, default=8.0)
    ap.add_argument("--trace", help="write JSON Lines trace here")
    ap.add_argument("--ams", type=int, default=0, help="AMS number the printer addresses (AMS-A=0, AMS-B=1)")
    a = ap.parse_args(argv)
    with open(a.image, "rb") as f:
        image = f.read()
    m, pico, printer, wall = run(image, a.seconds, ams=a.ams)
    recs = trace_records(m, pico, printer, wall)
    if a.trace:
        with open(a.trace, "w") as f:
            for r in recs:
                f.write(json.dumps(r, default=str) + "\n")
    kinds = {}
    for r in recs:
        key = r["src"] if r["src"] != "pico" else f"pico:{r.get('type')}"
        kinds[key] = kinds.get(key, 0) + 1
    print(f"virtual {m.seconds:.2f}s in {wall:.1f}s wall, fault={m.fault}")
    print("records:", ", ".join(f"{k}={v}" for k, v in sorted(kinds.items())))
    bad = [r for r in recs if r["src"] == "printer" and r.get("dir") == "rx" and not r["ok"]]
    print(f"printer replies: {len(printer.replies)} ({len(bad)} failed CRC)")
    return 0 if m.fault is None and not bad and not printer.violations else 1


if __name__ == "__main__":
    raise SystemExit(main())
