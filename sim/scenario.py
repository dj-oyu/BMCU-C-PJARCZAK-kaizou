"""Timed scenarios: what the user, the filament and the printer do, and when.

A scenario is JSON::

    {
      "seconds": 20,
      "ams": 0,
      "connected": [true, true, true, true],
      "params": {"motor_mm_s_full": 55},
      "nvm": "calibrated.bin",                 # optional flash dump to start from
      "events": [
        {"t": 0.5, "do": "calibration_gesture", "until": 40},
        {"t": 6.0, "do": "insert", "ch": 0},
        {"t": 7.0, "do": "override", "ch": 1, "name": "key_v", "value": 1.3},
        {"t": 7.5, "do": "override", "ch": 1, "name": "key_v", "value": null},
        {"t": 8.0, "do": "extruder", "mm_s": 4.0},
        {"t": 9.0, "do": "printer", "type": 3, "payload": "00 03 00 00"},
        {"t": 9.0, "do": "printer_repeat", "type": 3, "payload": "00 00 ff 00",
         "every": 0.2, "until": 12},
        {"t": 12, "do": "jam", "ch": 0, "at_mm": 300},
        {"t": 14, "do": "runout", "ch": 0},
        {"t": 16, "do": "remove", "ch": 0},
        {"t": 16, "do": "set", "ch": 2, "as5600_present": false}
      ]
    }

Overridable readings (``override``): ``key_v``, ``pull_v``, ``as5600_raw``,
``as5600_offline``, ``magnet_detected``, ``magnet_weak``, ``magnet_strong``,
``motor_stalled``.  A ``null`` value clears the override.
"""
from __future__ import annotations

from .bambu_bus import short_frame
from .clock import HCLK_HZ


def _at(m, t_s, fn):
    m.at(int(t_s * HCLK_HZ), fn)


def schedule(m, world, printer, events):
    for ev in events:
        ev = dict(ev)
        t = float(ev.pop("t"))
        do = ev.pop("do")
        ch = ev.pop("ch", None)
        if do == "insert":
            _at(m, t, lambda ch=ch, ev=ev: world.insert(ch, ev.get("tip_mm"), ev.get("push_s")))
        elif do == "remove":
            _at(m, t, lambda ch=ch: world.remove(ch))
        elif do == "runout":
            _at(m, t, lambda ch=ch, ev=ev: world.runout(ch, ev.get("tail_mm")))
        elif do == "jam":
            _at(m, t, lambda ch=ch, ev=ev: world.set(ch, jam_at_mm=ev.get("at_mm")))
        elif do == "set":
            _at(m, t, lambda ch=ch, ev=ev: world.set(ch, **ev))
        elif do == "override":
            _at(m, t, lambda ch=ch, ev=ev: world.override(ch, ev["name"], ev.get("value")))
        elif do == "extruder":
            _at(m, t, lambda ev=ev: setattr(world, "extruder_mm_s", float(ev["mm_s"])))
        elif do == "printer":
            frame = _frame(ev)
            _at(m, t, lambda f=frame: printer.send(f))
        elif do == "printer_repeat":
            frame = _frame(ev)
            k = 0
            while t + k * ev["every"] < ev["until"]:
                _at(m, t + k * ev["every"], lambda f=frame: printer.send(f))
                k += 1
        elif do == "calibration_gesture":
            calibration_gesture(m, world, t, float(ev.get("until", t + 40)))
        else:
            raise ValueError(f"unknown scenario action {do!r}")


def _frame(ev):
    payload = bytes.fromhex(ev.get("payload", "").replace(" ", ""))
    if "hex" in ev:
        return bytes.fromhex(ev["hex"].replace(" ", ""))
    return short_frame(int(ev["type"]), payload)


def calibration_gesture(m, world, start, until, swing=0.45):
    """A user repeatedly pushing and pulling every buffer through its range.

    The first-boot calibration asks for exactly this; the gesture repeats so
    that each channel is caught whenever its turn comes.
    """
    c = world.p.pull_center_v
    steps = [(0.0, c - swing), (0.5, c), (1.1, c + swing), (1.6, c), (2.3, None)]
    period = 2.5
    t = start
    while t < until:
        for dt, v in steps:
            for ch in range(4):
                if world.channels[ch].module_connected:
                    _at(m, t + dt, lambda ch=ch, v=v: world.override(ch, "pull_v", v))
        t += period
    for ch in range(4):
        _at(m, until, lambda ch=ch: world.override(ch, "pull_v", None))
