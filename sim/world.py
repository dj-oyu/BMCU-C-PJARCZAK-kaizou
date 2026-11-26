"""The physical world around the BMCU: sensors, motors and filament.

Scope rule: model only what is needed to explain the signals the Pico
monitor records (pull/pressure, key switches, insertion, motion).  Every
number below is a *parameter*, not a fact about the firmware; defaults are
board facts where a board fact exists and labelled assumptions otherwise,
to be fitted from Pico recordings.

Board wiring (BMCU board, per channel ch0..ch3):

  pull (buffer hall) sensor  ADC IN6 / IN4 / IN2 / IN0   (PA6 / PA4 / PA2 / PA0)
  key (two microswitches)    ADC IN7 / IN5 / IN3 / IN1   (PA7 / PA5 / PA3 / PA1)
  AS5600 SCL                 PB15 / PB14 / PB13 / PB12
  AS5600 SDA                 PD0  / PC15 / PC14 / PC13
  motor H-bridge IN1, IN2    TIM4 CH3,CH4 / TIM4 CH1,CH2 / TIM3 CH1,CH2 / TIM2 CH1,CH2

The AS5600 model follows the ams-OSRAM AS5600 datasheet (7-bit address
0x36, STATUS 0x0B with MD/ML/MH, RAW ANGLE 0x0C-0x0D, ANGLE 0x0E-0x0F,
AGC 0x1A, MAGNITUDE 0x1B-0x1C, auto-incrementing register pointer).  The
H-bridge follows the usual two-input PWM driver truth table: IN1 drives
forward, IN2 reverse, both high brakes.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from .clock import HCLK_HZ

PULL_ADC = (6, 4, 2, 0)
KEY_ADC = (7, 5, 3, 1)
SCL = (("B", 15), ("B", 14), ("B", 13), ("B", 12))
SDA = (("D", 0), ("C", 15), ("C", 14), ("C", 13))
MOTOR_PWM = (("TIM4", 3, 4), ("TIM4", 1, 2), ("TIM3", 1, 2), ("TIM2", 1, 2))


@dataclass
class Params:
    # --- board / mechanics ---
    gear_diameter_mm: float = 7.5          # drive gear on the AS5600 shaft
    vref: float = 3.3
    # --- assumptions until fitted from recordings ---
    motor_mm_s_full: float = 60.0          # filament speed at 100 % duty, no load
    motor_tau_s: float = 0.02              # first-order speed response
    motor_deadband: float = 0.06           # duty below which the motor does not turn
    forward_is_positive_drive: bool = True  # IN1 duty feeds toward the printer
    angle_counts_down_when_feeding: bool = True
    pull_center_v: float = 1.65
    pull_swing_v: float = 1.10             # buffer fully compressed / stretched
    buffer_travel_mm: float = 12.0         # spring travel of the buffer slider
    pull_polarity: int = 1                 # +1: compression raises the voltage
    # Key levels: none and both are fitted to Pico DM_KEY snapshot records
    # (2026-11-25/26, AMS-A: 8-13 mV with no filament, 1845-1868 mV loaded).
    # The single-switch levels were never caught by a snapshot; the outer-only
    # one only has to land in the band that reports ks=2, which the recorded
    # dm_teardown events confirm exists.
    key_v_none: float = 0.01
    key_v_external: float = 1.55           # outer (spool-side) switch only -- assumed
    key_v_internal: float = 1.20           # inner switch only -- assumed
    key_v_both: float = 1.85
    s1_mm: float = -40.0                   # outer switch, upstream of the drive gear
    s2_mm: float = -10.0                   # inner switch, just before the gear
    gear_mm: float = 0.0                   # filament must reach here to be driven
    head_mm: float = 800.0                 # gear to extruder entry (path length)
    user_push_mm_s: float = 15.0           # a hand feeding filament in until the gear bites
    user_push_s: float = 1.5               # how long the hand keeps pushing
    magnitude: int = 2000                  # AS5600 magnet magnitude
    agc: int = 128


@dataclass
class Channel:
    index: int
    module_connected: bool = True          # feeder module (pull sensor) plugged in
    tip_mm: float | None = None            # filament front; None = no filament
    tail_mm: float | None = None           # filament end; None = still on the spool
    speed_mm_s: float = 0.0
    angle: float = 1000.0                  # AS5600 angle, counts (0..4096)
    buffer_mm: float = 0.0                 # + compressed, - stretched
    jam_at_mm: float | None = None         # filament cannot pass this point
    as5600_present: bool = True
    magnet_ok: bool = True
    overrides: dict = field(default_factory=dict)   # name -> value (forced reading)
    travel_mm: float = 0.0
    push_until_s: float = 0.0              # the user's hand is pushing until this time


class AS5600Bus:
    """One AS5600 on a bit-banged I2C bus (SCL push-pull, SDA open drain)."""

    ADDR = 0x36

    def __init__(self, world: "World", ch: int):
        self.w, self.ch = world, ch
        self.scl_port, self.scl_pin = SCL[ch]
        self.sda_port, self.sda_pin = SDA[ch]
        self.regs = bytearray(256)
        self.ptr = 0
        self.state = "idle"
        self.bits = 0
        self.shift = 0
        self.rw = 0
        self._first_write = True
        self._saw_ack_clock = False
        self._nack = False
        self._scl = 1
        self._sda = 1
        self.transactions = 0
        self.nacks = 0

    # ---- electrical helpers
    def _line(self, port, pin):
        return self.w.m.gpio[port].level(pin)

    def _drive_sda(self, low: bool):
        g = self.w.m.gpio[self.sda_port]
        if low:
            g.drive[self.sda_pin] = 0
        else:
            g.drive.pop(self.sda_pin, None)
        self._sda = g.level(self.sda_pin)

    def _refresh_regs(self):
        c = self.w.channels[self.ch]
        raw = int(c.angle) % 4096
        ov = c.overrides
        raw = int(ov.get("as5600_raw", raw)) & 0xFFF
        md = bool(ov.get("magnet_detected", c.magnet_ok))
        st = (0x20 if md else 0) | (0x10 if ov.get("magnet_weak") else 0) | (0x08 if ov.get("magnet_strong") else 0)
        r = self.regs
        r[0x0B] = st
        r[0x0C], r[0x0D] = raw >> 8, raw & 0xFF
        r[0x0E], r[0x0F] = raw >> 8, raw & 0xFF
        r[0x1A] = self.w.p.agc
        mag = self.w.p.magnitude if md else 0
        r[0x1B], r[0x1C] = (mag >> 8) & 0x0F, mag & 0xFF

    def _present(self):
        c = self.w.channels[self.ch]
        return c.as5600_present and not c.overrides.get("as5600_offline")

    # ---- edge handling
    def on_edge(self):
        scl = self._line(self.scl_port, self.scl_pin)
        sda = self._line(self.sda_port, self.sda_pin)
        pscl, psda = self._scl, self._sda
        self._scl, self._sda = scl, sda
        if scl and pscl and psda != sda:
            if not sda:               # START or repeated START
                self._drive_sda(False)
                self.state, self.bits, self.shift = "addr", 0, 0
            else:                     # STOP
                self._drive_sda(False)
                self.state = "idle"
            return
        if scl and not pscl:
            self._rise(sda)
        elif pscl and not scl:
            self._fall()

    def _rise(self, sda):
        if self.state in ("addr", "write"):
            self.shift = (self.shift << 1 | sda) & 0xFF
            self.bits += 1
        elif self.state == "ack_out":
            self._saw_ack_clock = True
        elif self.state == "ack_in":
            self._nack = bool(sda)
            self._saw_ack_clock = True

    def _fall(self):
        st = self.state
        if st in ("addr", "write") and self.bits == 8:
            byte = self.shift
            if st == "addr":
                if byte >> 1 != self.ADDR or not self._present():
                    self.nacks += 1
                    self.state = "idle"
                    return
                self.rw = byte & 1
                self._first_write = True
                self.transactions += 1
            else:
                if self._first_write:
                    self.ptr = byte
                    self._first_write = False
                else:
                    self.regs[self.ptr] = byte       # writable config; not interpreted
                    self.ptr = (self.ptr + 1) & 0xFF
            self._drive_sda(True)                   # ACK
            self.state, self._saw_ack_clock = "ack_out", False
        elif st == "ack_out" and self._saw_ack_clock:
            self._drive_sda(False)
            if self.rw:
                self._refresh_regs()
                self._load_read_byte()
            else:
                self.state, self.bits, self.shift = "write", 0, 0
        elif st == "read":
            self.bits += 1
            if self.bits < 8:
                self._drive_sda(not (self.shift >> (7 - self.bits) & 1))
            else:
                self._drive_sda(False)
                self.state, self._saw_ack_clock = "ack_in", False
        elif st == "ack_in" and self._saw_ack_clock:
            if self._nack:
                self.state = "idle"
                self._drive_sda(False)
            else:
                self.ptr = (self.ptr + 1) & 0xFF
                self._load_read_byte()

    def _load_read_byte(self):
        self.shift = self.regs[self.ptr]
        self.state, self.bits = "read", 0
        self._drive_sda(not (self.shift >> 7 & 1))


class World:
    TICK_S = 0.001

    def __init__(self, m, params: Params | None = None, connected=(True, True, True, True)):
        self.m = m
        self.p = params or Params()
        self.channels = [Channel(i, module_connected=connected[i]) for i in range(4)]
        self.extruder_mm_s = 0.0            # printer pulling filament at the head
        self.buses = [AS5600Bus(self, ch) for ch in range(4)]
        self._pins = {}
        for bus in self.buses:
            self._pins.setdefault((bus.scl_port, bus.scl_pin), []).append(bus)
            self._pins.setdefault((bus.sda_port, bus.sda_pin), []).append(bus)
        for port in ("B", "C", "D"):
            m.gpio[port].on_change.append(self._on_gpio)
        m.adc1.source = self.adc_volts
        m.adc2.source = self.adc_volts
        self.log: list[dict] = []
        self._last = m.cycles
        m.after(int(HCLK_HZ * self.TICK_S), self._tick)

    # ---- GPIO -> I2C
    def _on_gpio(self, port, pin, level, t):
        for bus in self._pins.get((port, pin), ()):
            bus.on_edge()

    # ---- ADC
    def key_volts(self, ch: int) -> float:
        c = self.channels[ch]
        if "key_v" in c.overrides:
            return float(c.overrides["key_v"])
        p = self.p
        s1 = self._covers(c, p.s1_mm)
        s2 = self._covers(c, p.s2_mm)
        if s1 and s2:
            return p.key_v_both
        if s1:
            return p.key_v_external
        if s2:
            return p.key_v_internal
        return p.key_v_none

    def pull_volts(self, ch: int) -> float:
        c = self.channels[ch]
        if "pull_v" in c.overrides:
            return float(c.overrides["pull_v"])
        if not c.module_connected:
            return 0.0
        p = self.p
        x = max(-1.0, min(1.0, c.buffer_mm / p.buffer_travel_mm))
        return p.pull_center_v + p.pull_polarity * x * p.pull_swing_v

    def adc_volts(self, adc_channel: int, t: int) -> float:
        for ch in range(4):
            if PULL_ADC[ch] == adc_channel:
                return self.pull_volts(ch)
            if KEY_ADC[ch] == adc_channel:
                return self.key_volts(ch)
        return 0.0

    @staticmethod
    def _covers(c: Channel, pos: float) -> bool:
        if c.tip_mm is None or c.tip_mm < pos:
            return False
        return c.tail_mm is None or c.tail_mm <= pos

    # ---- motors and filament
    def drive(self, ch: int) -> float:
        """Signed duty in [-1, 1] from the H-bridge inputs (0 when braking)."""
        name, ca, cb = MOTOR_PWM[ch]
        tim = self.m.tim[name]
        a, b = tim.duty(ca), tim.duty(cb)
        if a > 0.95 and b > 0.95:
            return 0.0
        d = a - b
        return d if self.p.forward_is_positive_drive else -d

    def _tick(self):
        now = self.m.cycles
        dt = (now - self._last) / HCLK_HZ
        self._last = now
        p = self.p
        for c in self.channels:
            d = self.drive(c.index)
            target = 0.0 if abs(d) < p.motor_deadband else d * p.motor_mm_s_full
            c.speed_mm_s += (target - c.speed_mm_s) * min(1.0, dt / p.motor_tau_s)
            if c.overrides.get("motor_stalled"):
                c.speed_mm_s = 0.0
            if c.tip_mm is not None and c.tip_mm < p.gear_mm and now / HCLK_HZ < c.push_until_s:
                # The hand pushes the front up to the gear; once it bites the motor takes over.
                c.tip_mm = min(p.gear_mm, c.tip_mm + p.user_push_mm_s * dt)
            self._move(c, c.speed_mm_s * dt, dt)
        self.m.after(int(HCLK_HZ * self.TICK_S), self._tick)

    def _move(self, c: Channel, dx: float, dt: float) -> None:
        p = self.p
        engaged = c.tip_mm is not None and c.tip_mm >= p.gear_mm and (c.tail_mm is None or c.tail_mm <= p.gear_mm)
        if not engaged:
            # Gear spins free.
            c.angle = self._turn(c.angle, dx)
            return
        at_head = c.tip_mm >= p.head_mm
        consume = self.extruder_mm_s * dt if at_head else 0.0
        if c.jam_at_mm is not None and c.tip_mm + dx > c.jam_at_mm and dx > 0:
            dx = max(0.0, c.jam_at_mm - c.tip_mm)     # stalls against the jam
            c.speed_mm_s = 0.0
        if at_head or c.tip_mm + dx > p.head_mm:
            # The strand is held at the extruder: feeding compresses the
            # buffer, the extruder pulling stretches it.
            room = p.head_mm - c.tip_mm
            advance = max(0.0, min(dx, room)) if dx > 0 else 0.0
            c.tip_mm += advance
            c.buffer_mm += (dx - advance) - consume
            if c.buffer_mm > p.buffer_travel_mm:          # fully compressed: motor stalls
                c.buffer_mm = p.buffer_travel_mm
                c.speed_mm_s = min(c.speed_mm_s, 0.0)
            if c.buffer_mm < -p.buffer_travel_mm:         # stretched: extruder drags the strand
                c.buffer_mm = -p.buffer_travel_mm
            if dx < 0 and c.buffer_mm <= 0:
                c.tip_mm += dx                            # retract pulls the tip back
        else:
            c.tip_mm += dx
            c.buffer_mm *= 0.9                            # relaxes when the strand is free
        if c.tail_mm is not None:
            c.tail_mm += dx
        c.travel_mm += dx
        c.angle = self._turn(c.angle, dx)

    def _turn(self, angle, dx):
        counts = dx / (math.pi * self.p.gear_diameter_mm) * 4096
        if self.p.angle_counts_down_when_feeding:
            counts = -counts
        return (angle + counts) % 4096

    # ---- scenario actions (also usable directly from tests)
    def insert(self, ch, tip_mm=None, push_s=None):
        """A user feeds filament in from the spool side.

        The front appears at tip_mm (default: just past the inner switch) and
        the hand keeps pushing toward the gear for push_s seconds.  Whether the
        firmware spins the gear while the hand is still there decides whether
        the filament is caught, as on the real feeder.
        """
        c = self.channels[ch]
        c.tip_mm = self.p.s2_mm + 2.0 if tip_mm is None else tip_mm
        c.tail_mm = None
        c.push_until_s = self.m.cycles / HCLK_HZ + (self.p.user_push_s if push_s is None else push_s)

    def remove(self, ch):
        c = self.channels[ch]
        c.tip_mm = c.tail_mm = None
        c.buffer_mm = 0.0

    def runout(self, ch, tail_mm=None):
        """The spool runs out: the filament end appears at tail_mm and travels with the strand."""
        c = self.channels[ch]
        c.tail_mm = self.p.s1_mm - 50.0 if tail_mm is None else tail_mm

    def set(self, ch, **kv):
        for k, v in kv.items():
            setattr(self.channels[ch], k, v)

    def override(self, ch, name, value):
        if value is None:
            self.channels[ch].overrides.pop(name, None)
        else:
            self.channels[ch].overrides[name] = value

    def snapshot(self):
        return [{"ch": c.index, "tip_mm": None if c.tip_mm is None else round(c.tip_mm, 2),
                 "tail_mm": None if c.tail_mm is None else round(c.tail_mm, 2),
                 "speed_mm_s": round(c.speed_mm_s, 2), "buffer_mm": round(c.buffer_mm, 2),
                 "angle": int(c.angle), "drive": round(self.drive(c.index), 3),
                 "pull_v": round(self.pull_volts(c.index), 3), "key_v": round(self.key_volts(c.index), 3),
                 "overrides": dict(c.overrides)} for c in self.channels]
