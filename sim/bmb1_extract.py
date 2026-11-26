"""Cut a time window out of a Bambuddy BMB1 export and decode it.

The export is the SQLite file made from Bambuddy's ``bmcu_binary_*`` tables.
Each stored BMCU frame is decoded with the Pico's own decoder
(pico/bmcu_link.py), the same code the simulator's monitor link runs, so a
recording and a simulator trace can be compared field by field::

    python -m sim.bmb1_extract export.db --from "2026-11-25 10:03:50" \\
        --to "2026-11-25 10:04:20" --out window.jsonl [--with-printer]

Times are the server's receive time (UTC), as stored.
"""
from __future__ import annotations

import argparse
import json
import sqlite3

from .link_peer import bmcu_link as L

KIND_HELLO, KIND_STATUS, KIND_EVENT, KIND_SNAPSHOT = 1, 2, 3, 115


class _Snap:
    _snapshot_parts = None


def decode(frame: bytes) -> dict | None:
    frames = L.FrameDecoder().feed(bytes(frame))
    if not frames:
        return None
    f = frames[0]
    rec = {"kind": f["kind"], "seq": f["sequence"]}
    if f["kind"] == KIND_STATUS:
        rec["status"] = L.BMCUMonitor._decode_status(f["payload"])
    elif f["kind"] == KIND_EVENT:
        e = L.BMCUMonitor._decode_event(f["payload"])
        e.pop("raw", None)
        e.pop("payload", None)
        rec["event"] = e
    elif f["kind"] == KIND_SNAPSHOT:
        msg: dict = {}
        try:
            L.BMCUMonitor._handle_snapshot(_Snap(), f["payload"], msg)
        except Exception:      # a partial snapshot still has its record decoded
            pass
        msg.pop("record_data", None)
        rec["snapshot"] = msg
    else:
        rec["payload"] = f["payload"].hex()
    return rec


def extract(db, start=None, end=None, printer=False):
    q = ("select server_received_at, pico_boot_id, raw_bmcu_frame from bmcu_binary_records "
         "where message_type = 16 and raw_bmcu_frame is not null")
    args = []
    if start:
        q += " and server_received_at >= ?"
        args.append(start)
    if end:
        q += " and server_received_at <= ?"
        args.append(end)
    for at, boot, frame in sqlite3.connect(db).execute(q + " order by id", args):
        rec = decode(frame)
        if rec is None:
            continue
        if not printer and rec.get("event", {}).get("event_name", "").startswith("printer_"):
            continue
        rec["at"], rec["boot"] = at, boot
        yield rec


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("db")
    ap.add_argument("--from", dest="start")
    ap.add_argument("--to", dest="end")
    ap.add_argument("--out", required=True)
    ap.add_argument("--with-printer", action="store_true", help="keep printer transaction events")
    a = ap.parse_args(argv)
    n = 0
    with open(a.out, "w") as f:
        for rec in extract(a.db, a.start, a.end, a.with_printer):
            f.write(json.dumps(rec, default=str) + "\n")
            n += 1
    print(f"{n} records -> {a.out}")


if __name__ == "__main__":
    main()
