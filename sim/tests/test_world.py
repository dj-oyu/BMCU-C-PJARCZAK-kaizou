"""The sensor world on its own, driven from Python instead of firmware.

The I2C master here follows the AS5600 datasheet's read sequence (START,
address+W, register pointer, repeated START, address+R, data, NACK, STOP), so
the slave model is checked against the bus protocol rather than against how
the firmware happens to bit-bang it.
"""
from __future__ import annotations

import unittest

try:
    import unicorn  # noqa: F401
    HAVE_UNICORN = True
except ImportError:
    HAVE_UNICORN = False

GPIO_BASE = {"A": 0x4001_0800, "B": 0x4001_0C00, "C": 0x4001_1000, "D": 0x4001_1400}


def _machine():
    from sim.machine import Machine
    from sim.periph import build_ch32v203
    m = Machine((0xA001).to_bytes(2, "little"))      # c.j . (spin)
    build_ch32v203(m)
    return m


class I2CMaster:
    """Bit-bangs one feeder's AS5600 bus through the GPIO registers."""

    def __init__(self, m, ch):
        from sim.world import SCL, SDA
        self.m = m
        (self.cp, self.cpin), (self.dp, self.dpin) = SCL[ch], SDA[ch]
        self._cfg(self.cp, self.cpin, 0x3)           # output push-pull, 50 MHz
        self._cfg(self.dp, self.dpin, 0x7)           # output open drain, 50 MHz
        self.scl(1)
        self.sda(1)

    def _cfg(self, port, pin, nib):
        g = self.m.gpio[port]
        off = 0x00 if pin < 8 else 0x04
        reg = g.read(off, 4)
        sh = 4 * (pin % 8)
        self.m._mmio_write(GPIO_BASE[port] + off, 4, reg & ~(0xF << sh) | nib << sh)

    def _pin(self, port, pin, level):
        self.m._mmio_write(GPIO_BASE[port] + (0x10 if level else 0x14), 4, 1 << pin)

    def scl(self, v):
        self._pin(self.cp, self.cpin, v)

    def sda(self, v):
        self._pin(self.dp, self.dpin, v)

    def read_sda(self):
        return self.m._mmio_read(GPIO_BASE[self.dp] + 0x08, 4) >> self.dpin & 1

    def start(self):
        self.sda(1)
        self.scl(1)
        self.sda(0)
        self.scl(0)

    def stop(self):
        self.sda(0)
        self.scl(1)
        self.sda(1)

    def write_byte(self, b) -> bool:
        for i in range(7, -1, -1):
            self.sda(b >> i & 1)
            self.scl(1)
            self.scl(0)
        self.sda(1)
        self.scl(1)
        ack = self.read_sda() == 0
        self.scl(0)
        return ack

    def read_byte(self, ack) -> int:
        self.sda(1)
        v = 0
        for _ in range(8):
            self.scl(1)
            v = v << 1 | self.read_sda()
            self.scl(0)
        self.sda(0 if ack else 1)
        self.scl(1)
        self.scl(0)
        self.sda(1)
        return v

    def read_regs(self, addr, reg, n):
        self.start()
        if not self.write_byte(addr << 1):
            self.stop()
            return None
        self.write_byte(reg)
        self.start()
        self.write_byte(addr << 1 | 1)
        out = [self.read_byte(ack=i < n - 1) for i in range(n)]
        self.stop()
        return out


@unittest.skipUnless(HAVE_UNICORN, "unicorn not installed")
class AS5600Slave(unittest.TestCase):
    def setUp(self):
        from sim.world import World
        self.m = _machine()
        self.w = World(self.m)

    def test_reads_raw_angle_on_every_channel(self):
        for ch, angle in enumerate((0, 1234, 2048, 4095)):
            self.w.channels[ch].angle = angle
            got = I2CMaster(self.m, ch).read_regs(0x36, 0x0C, 2)
            self.assertEqual(got, [angle >> 8, angle & 0xFF], f"ch{ch}")

    def test_status_magnitude_and_overrides(self):
        bus = I2CMaster(self.m, 1)
        self.assertEqual(bus.read_regs(0x36, 0x0B, 1), [0x20])          # MD: magnet detected
        self.w.override(1, "magnet_weak", True)
        self.w.override(1, "as5600_raw", 0x321)
        self.assertEqual(bus.read_regs(0x36, 0x0B, 1), [0x30])
        self.assertEqual(bus.read_regs(0x36, 0x0E, 2), [0x03, 0x21])
        self.w.override(1, "magnet_detected", False)
        self.assertEqual(bus.read_regs(0x36, 0x1B, 2), [0, 0])

    def test_absent_or_wrong_address_does_not_acknowledge(self):
        bus = I2CMaster(self.m, 2)
        self.assertIsNone(bus.read_regs(0x37, 0x0C, 2))
        self.w.set(2, as5600_present=False)
        self.assertIsNone(bus.read_regs(0x36, 0x0C, 2))
        self.w.set(2, as5600_present=True)
        self.assertIsNotNone(bus.read_regs(0x36, 0x0C, 2))


@unittest.skipUnless(HAVE_UNICORN, "unicorn not installed")
class FilamentPath(unittest.TestCase):
    def setUp(self):
        from sim.world import World
        self.m = _machine()
        self.w = World(self.m)
        self.duty = [0.0] * 4
        self.w.drive = lambda ch: self.duty[ch]

    def run_s(self, s):
        # Only world time matters here: fire the scheduled events without
        # executing guest code (a spin loop would cost one block per cycle).
        import heapq

        from sim.clock import HCLK_HZ
        m = self.m
        end = m.cycles + int(s * HCLK_HZ)
        while m._events and m._events[0].when <= end:
            ev = heapq.heappop(m._events)
            m.cycles = max(m.cycles, ev.when)
            ev.fn()
        m.cycles = end

    def test_switches_follow_the_filament_front(self):
        p, w = self.w.p, self.w
        self.assertAlmostEqual(w.key_volts(0), p.key_v_none)
        w.insert(0, tip_mm=p.s1_mm + 1, push_s=0)
        self.assertAlmostEqual(w.key_volts(0), p.key_v_external)
        w.insert(0, tip_mm=p.s2_mm + 1, push_s=0)
        self.assertAlmostEqual(w.key_volts(0), p.key_v_both)
        w.runout(0, tail_mm=p.s1_mm + 1)
        self.assertAlmostEqual(w.key_volts(0), p.key_v_internal)
        w.remove(0)
        self.assertAlmostEqual(w.key_volts(0), p.key_v_none)

    def test_gear_catches_only_while_the_hand_pushes(self):
        w = self.w
        w.insert(0, push_s=0)              # the hand lets go at once
        self.duty[0] = 0.9
        self.run_s(0.5)
        self.assertLess(w.channels[0].tip_mm, w.p.gear_mm)
        self.assertNotEqual(w.channels[0].angle, 1000)       # the gear spins free
        w.insert(1)                        # default: pushes for user_push_s
        self.duty[1] = 0.9
        self.run_s(1.0)
        self.assertGreater(w.channels[1].tip_mm, 10)

    def test_feeding_turns_the_encoder_by_the_gear_circumference(self):
        import math
        w = self.w
        w.set(0, tip_mm=10.0)
        self.duty[0] = 0.5
        a0 = w.channels[0].angle
        self.run_s(0.2)
        moved = w.channels[0].tip_mm - 10.0
        self.assertGreater(moved, 0)
        counts = (a0 - w.channels[0].angle) % 4096          # counts down when feeding
        self.assertAlmostEqual(counts, moved / (math.pi * w.p.gear_diameter_mm) * 4096, delta=2)

    def test_jam_stops_the_front(self):
        w = self.w
        w.set(0, tip_mm=10.0, jam_at_mm=30.0)
        self.duty[0] = 1.0
        self.run_s(1.0)
        self.assertAlmostEqual(w.channels[0].tip_mm, 30.0)

    def test_buffer_compresses_at_the_head_and_raises_pull_voltage(self):
        w = self.w
        w.set(0, tip_mm=w.p.head_mm - 1)
        self.duty[0] = 0.5
        self.run_s(0.5)
        self.assertGreater(w.pull_volts(0), w.p.pull_center_v + 0.5)
        self.duty[0] = 0.0
        w.extruder_mm_s = 10.0
        self.run_s(3.0)
        self.assertLess(w.pull_volts(0), w.p.pull_center_v - 0.5)

    def test_unplugged_module_reads_zero(self):
        from sim.world import World
        w = World(_machine(), connected=(False, True, True, True))
        self.assertEqual(w.pull_volts(0), 0.0)
        self.assertAlmostEqual(w.pull_volts(1), w.p.pull_center_v)


@unittest.skipUnless(HAVE_UNICORN, "unicorn not installed")
class DmaBulk(unittest.TestCase):
    def test_bulk_matches_per_item_transfers(self):
        from sim.machine import SRAM_BASE
        snaps = []
        for bulk in (False, True):
            m = _machine()
            d = m.dma
            d.write(0x08 + 20 * 0 + 12, 4, SRAM_BASE + 0x100)  # ch1 MADDR
            d.write(0x08 + 20 * 0 + 4, 4, 10)                  # CNTR
            d.write(0x08 + 20 * 0, 4, 0x20 | 0x80 | 0xA00 | 1)  # circ, minc, 32-bit, EN
            vals = list(range(100, 127))
            if bulk:
                d.bulk_write(1, vals)
            else:
                for v in vals:
                    d.transfer(1, v)
            snaps.append((bytes(m.uc.mem_read(SRAM_BASE + 0x100, 40)), d.cntr[1], d.pos[1], d.intfr))
        self.assertEqual(snaps[0], snaps[1])


if __name__ == "__main__":
    unittest.main()
