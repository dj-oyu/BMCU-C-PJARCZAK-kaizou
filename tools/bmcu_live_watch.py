"""Live per-channel switch reading, from STATUS rather than the snapshot.

/api/snapshot.bin arrives once per link session and then sits frozen -- its
per-record hw_tick32 does not advance. Watching a hand movement needs
/api/current.bin, the latest STATUS per link, which changes many times a second.
Offsets mirror web/src/api/layout.ts StatusPayload.
"""
import struct
import sys
import time
import urllib.request

KS = {0: "none", 1: "both", 2: "outer", 3: "inner"}
MOTION = {0: "idle", 1: "send_out", 2: "on_use", 3: "before_pull_back",
          4: "pull_back", 5: "before_on_use", 6: "stop_on_use"}
HOST = sys.argv[1] if len(sys.argv) > 1 else "http://bmcu-monitor-a.local"
WATCH = [int(a) for a in sys.argv[2:]] or None  # link indices to print


def statuses(blob):
    off = 0
    while off + 32 <= len(blob):
        magic, ver, mtype, flags, plen, tseq, boot, link = struct.unpack_from(
            ">4sBBHIQQB", blob, off)
        if magic != b"BMB1":
            break
        p = blob[off + 32: off + 32 + plen]
        off += 32 + plen
        if len(p) < 10:
            continue
        wlen = struct.unpack_from(">H", p, 8)[0]
        w = p[10:10 + wlen]
        if len(w) < 7 + 27 + 2:
            continue
        s = w[7:-2]
        tick = struct.unpack_from("<I", s, 0)[0]
        slot, ins, onl = s[12], s[13], s[14]
        motion = s[15:19]
        pull = s[19:23]
        cf = s[27:31] if len(s) >= 31 else b"\0\0\0\0"
        yield link, tick, slot, ins, onl, motion, pull, cf


prev = None
while True:
    blob = urllib.request.urlopen(HOST + "/api/current.bin", timeout=5).read()
    rows = []
    for link, tick, slot, ins, onl, motion, pull, cf in statuses(blob):
        if WATCH and link not in WATCH:
            continue
        for ch in range(4):
            f = cf[ch]
            latch = "".join(n for b, n in ((2, "LOW"), (3, "JAM"), (4, "DMFAIL"))
                            if f & (1 << b)) or "-"
            rows.append(
                f"  link{link} ch{ch} ks={KS[f & 3]:<5} "
                f"ins={(ins >> ch) & 1} onl={(onl >> ch) & 1} "
                f"pull={pull[ch]:>3}% motion={MOTION.get(motion[ch], motion[ch]):<16} "
                f"latch={latch}")
    if rows != prev:
        print("--", time.strftime("%H:%M:%S"))
        print("\n".join(rows))
        prev = rows
    time.sleep(0.2)
