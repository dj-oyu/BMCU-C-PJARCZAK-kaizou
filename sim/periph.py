"""CH32V20x peripheral models, written from the WCH reference manual (RM v2.x).

Each class cites the register layout it implements.  Behaviour that the
manual leaves to analogue reality (ADC voltages, what is wired to a GPIO) is
delegated to the world model through small callbacks.
"""
from __future__ import annotations

from typing import Callable

from .machine import HCLK_HZ, Machine, Peripheral

# PFIC interrupt numbers, CH32V20x RM "Interrupt and event" vector table.
IRQ_SYSTICK = 12
IRQ_DMA1_CH = {n: 26 + n for n in range(1, 8)}     # DMA1_Channel1 = 27
IRQ_ADC = 34
IRQ_USART1 = 53
IRQ_USART2 = 54
IRQ_USART3 = 55


class Store(Peripheral):
    """Plain read/write register file for blocks whose side effects we do not need."""

    def __init__(self, base: int, size: int = 0x400, reset: dict[int, int] | None = None):
        self.base, self.size = base, size
        self.r = dict(reset or {})

    def read(self, off, width):
        return self.r.get(off, 0)

    def write(self, off, width, value):
        self.r[off] = value


# --------------------------------------------------------------------- PFIC
class PFIC(Peripheral):
    """Programmable Fast Interrupt Controller (RM chapter PFIC), base 0xE000E000."""

    base, size = 0xE000_E000, 0x1000

    def __init__(self):
        self.enabled = 0
        self.pending = 0
        self.active = 0
        self.prio = bytearray(256)
        self.lines: dict[int, Callable[[], bool]] = {}
        self.vtf = [0, 0, 0, 0]
        self.vtf_id = 0
        self.regs: dict[int, int] = {}

    def line(self, irq: int, level: Callable[[], bool]) -> None:
        """Attach a level-sensitive peripheral interrupt line."""
        self.lines[irq] = level

    def set_pending(self, irq: int) -> None:
        self.pending |= 1 << irq

    def priority(self, irq: int) -> int:
        return self.prio[irq]

    def _asserted(self) -> int:
        bits = self.pending
        for irq, level in self.lines.items():
            if level():
                bits |= 1 << irq
        return bits

    def highest_pending(self, depth: int, active_prio):
        cand = self._asserted() & self.enabled & ~self.active
        if not cand:
            return None
        best = None
        for irq in range(256):
            if cand >> irq & 1 and (best is None or self.prio[irq] < self.prio[best]):
                best = irq
        if active_prio is not None:
            # Preemption only for a strictly higher preemption level and only
            # one level deep beyond the current handler (QingKe V4: 2 levels).
            if depth >= 2 or (self.prio[best] >> 6) >= (active_prio >> 6):
                return None
        return best

    def handler_override(self, irq: int):
        for i, addr in enumerate(self.vtf):
            if addr & 1 and (self.vtf_id >> (8 * i)) & 0xFF == irq:
                return addr & ~1
        return None

    def activate(self, irq):
        self.active |= 1 << irq
        self.pending &= ~(1 << irq)

    def deactivate(self, irq):
        self.active &= ~(1 << irq)

    def _word(self, bits: int, off: int) -> int:
        return (bits >> (32 * (off // 4))) & 0xFFFF_FFFF

    def read(self, off, width):
        if 0x000 <= off < 0x010:
            return self._word(self.enabled, off)
        if 0x020 <= off < 0x030:
            return self._word(self._asserted(), off - 0x20)
        if 0x300 <= off < 0x310:
            return self._word(self.active, off - 0x300)
        if 0x400 <= off < 0x500:
            return int.from_bytes(self.prio[off - 0x400: off - 0x400 + width], "little")
        if off == 0x50:
            return self.vtf_id
        if 0x60 <= off < 0x70:
            return self.vtf[(off - 0x60) // 4]
        return self.regs.get(off, 0)

    def write(self, off, width, value):
        sh = 32 * ((off & 0xF) // 4)
        if 0x100 <= off < 0x110:
            self.enabled |= value << sh
        elif 0x180 <= off < 0x190:
            self.enabled &= ~(value << sh)
        elif 0x200 <= off < 0x210:
            self.pending |= value << sh
        elif 0x280 <= off < 0x290:
            self.pending &= ~(value << sh)
        elif 0x400 <= off < 0x500:
            self.prio[off - 0x400: off - 0x400 + width] = value.to_bytes(width, "little")
        elif off == 0x50:
            self.vtf_id = value
        elif 0x60 <= off < 0x70:
            self.vtf[(off - 0x60) // 4] = value
        else:
            self.regs[off] = value


# ------------------------------------------------------------------ SysTick
class SysTick(Peripheral):
    """QingKe V4 system timer (RM "STK"): 64-bit counter at HCLK or HCLK/8."""

    base, size = 0xE000_F000, 0x20

    def __init__(self):
        self.ctlr = 0
        self.sr = 0
        self.cmp = (1 << 64) - 1
        self._cnt0 = 0
        self._t0 = 0

    def attach(self, m):
        super().attach(m)
        m.pfic.line(IRQ_SYSTICK, lambda: bool(self.ctlr & 2 and self._update_flag()))

    def _div(self):
        return 1 if self.ctlr & 4 else 8

    def count(self) -> int:
        if not self.ctlr & 1:
            return self._cnt0
        ticks = (self.m.now() - self._t0) // self._div()
        if self.ctlr & 8 and self.cmp:              # STRE: reload to 0 on compare
            return (self._cnt0 + ticks) % (self.cmp + 1)
        return (self._cnt0 + ticks) & ((1 << 64) - 1)

    def _update_flag(self) -> bool:
        if self.ctlr & 1 and not self.ctlr & 8 and self.count() >= self.cmp:
            self.sr |= 1
        return bool(self.sr & 1)

    def _rebase(self, cnt):
        self._cnt0, self._t0 = cnt, self.m.now()

    def read(self, off, width):
        if off in (0x08, 0x0C):
            self.m.note_poll()
        c = self.count()
        return {0x00: self.ctlr, 0x04: self.sr, 0x08: c & 0xFFFF_FFFF, 0x0C: c >> 32,
                0x10: self.cmp & 0xFFFF_FFFF, 0x14: self.cmp >> 32}.get(off, 0)

    def write(self, off, width, value):
        c = self.count()
        if off == 0x00:
            self.ctlr = value & ~(1 << 5)
            self._rebase(0 if value & (1 << 5) else c)   # INIT: reload counter
        elif off == 0x04:
            self.sr = value & self.sr
        elif off == 0x08:
            self._rebase((c & ~0xFFFF_FFFF) | value)
        elif off == 0x0C:
            self._rebase((c & 0xFFFF_FFFF) | (value << 32))
        elif off == 0x10:
            self.cmp = (self.cmp & ~0xFFFF_FFFF) | value
        elif off == 0x14:
            self.cmp = (self.cmp & 0xFFFF_FFFF) | (value << 32)


# ---------------------------------------------------------------------- RCC
class RCC(Store):
    """Reset and clock control: oscillators and the PLL lock immediately."""

    def __init__(self):
        super().__init__(0x4002_1000, reset={0x00: 0x83, 0x24: 0x0C00_0000})  # HSION|HSIRDY, PORRSTF|PINRSTF

    def read(self, off, width):
        v = self.r.get(off, 0)
        if off == 0x00:   # CTLR: xxxRDY follows xxxON (HSI bit0->1, HSE 16->17, PLL 24->25)
            rdy = ((v & 1) << 1) | ((v >> 16 & 1) << 17) | ((v >> 24 & 1) << 25)
            v = (v & ~((1 << 1) | (1 << 17) | (1 << 25))) | rdy
        elif off == 0x04:  # CFGR0: SWS mirrors SW
            v = (v & ~0xC) | ((v & 3) << 2)
        return v

    def pclk(self, apb: int) -> int:
        cfg = self.r.get(0x04, 0)
        bits = (cfg >> (8 if apb == 1 else 11)) & 7
        return HCLK_HZ >> (bits - 3) if bits & 4 else HCLK_HZ

    def adcclk(self) -> int:
        return self.pclk(2) // (2 * (((self.r.get(0x04, 0) >> 14) & 3) + 1))


class FlashCtl(Peripheral):
    """Flash interface (RM "FLASH"): unlock keys, standard and fast programming.

    Programming can only clear bits (NOR behaviour), so a store into a word
    that was not erased first leaves the AND of old and new data and is
    counted in ``unerased_writes``.  Fast page programming (FTPG) buffers
    stores and commits them only on PGSTRT, as the manual describes.
    """

    base, size = 0x4002_2000, 0x400
    KEY1, KEY2 = 0x4567_0123, 0xCDEF_89AB
    PG, PER, MER, STRT, LOCK = 1, 1 << 1, 1 << 2, 1 << 6, 1 << 7
    FLOCK, FTPG, FTER, BUFRST, PGSTRT = 1 << 15, 1 << 16, 1 << 17, 1 << 19, 1 << 21

    def __init__(self):
        self.ctlr = self.LOCK | self.FLOCK
        self.statr = 0
        self.addr = 0
        self._key = 0
        self._mkey = 0
        self.r: dict[int, int] = {}
        self.buffer: dict[int, int] = {}       # byte offset -> value (fast page)
        self.log: list[tuple[int, str, int, int]] = []   # (cycle, op, offset, length)
        self.unerased_writes = 0
        self.locked_writes = 0

    def attach(self, m):
        super().attach(m)
        m.flashctl = self

    def _program(self, off, data: bytes):
        mem = self.m.flash
        for i, b in enumerate(data):
            o = (off + i) % len(mem.raw)
            cur = mem.raw[o]
            if cur & b != b:
                self.unerased_writes += 1
            mem[o] = bytes((cur & b,))
        self.log.append((self.m.now(), "program", off, len(data)))

    def _erase(self, off, length):
        off -= off % length
        self.m.flash[off:off + length] = b"\xff" * length
        self.m.uc.ctl_remove_cache(off, off + length)
        self.m.uc.ctl_remove_cache(0x0800_0000 + off, 0x0800_0000 + off + length)
        self.log.append((self.m.now(), "erase", off, length))

    def cpu_store(self, off, size, value):
        data = value.to_bytes(size, "little")
        if self.ctlr & self.LOCK:
            self.locked_writes += 1
            return
        if self.ctlr & self.FTPG:
            for i, b in enumerate(data):
                self.buffer[off + i] = b
        elif self.ctlr & self.PG:
            self._program(off, data)
        else:
            self.locked_writes += 1

    def read(self, off, width):
        if off == 0x0C:
            return self.statr
        if off == 0x10:
            return self.ctlr
        if off == 0x14:
            return self.addr
        return self.r.get(off, 0)

    def write(self, off, width, value):
        if off == 0x04:                       # KEYR
            if self._key == 0 and value == self.KEY1:
                self._key = 1
            elif self._key == 1 and value == self.KEY2:
                self.ctlr &= ~self.LOCK
                self._key = 0
            else:
                self._key = 0
        elif off == 0x24:                     # MODEKEYR: fast-mode unlock
            if self._mkey == 0 and value == self.KEY1:
                self._mkey = 1
            elif self._mkey == 1 and value == self.KEY2:
                self.ctlr &= ~self.FLOCK
                self._mkey = 0
            else:
                self._mkey = 0
        elif off == 0x0C:                     # STATR: EOP / WRPRTERR write-1-to-clear
            self.statr &= ~(value & 0x30)
        elif off == 0x14:
            self.addr = value
        elif off == 0x10:
            keep_locks = self.ctlr & (self.LOCK | self.FLOCK)
            new = (value & ~(self.LOCK | self.FLOCK)) | keep_locks | (value & (self.LOCK | self.FLOCK))
            self.ctlr = new & ~(self.STRT | self.PGSTRT | self.BUFRST)
            if self.ctlr & self.LOCK:
                return
            off_flash = self.addr & 0xFFFF
            if value & self.BUFRST:
                self.buffer.clear()
            if value & self.STRT:
                if value & self.FTER:
                    self._erase(off_flash, 256)
                elif value & self.PER:
                    self._erase(off_flash, 4096)
                elif value & self.MER:
                    self._erase(0, len(self.m.flash.raw))
                self.statr |= 0x20
            if value & self.PGSTRT:
                page = off_flash - off_flash % 256
                data = bytearray(self.m.flash.raw[page:page + 256])
                for o, b in self.buffer.items():
                    if page <= o < page + 256:
                        data[o - page] = b
                self._program(page, bytes(data))
                self.buffer.clear()
                self.m.uc.ctl_remove_cache(page, page + 256)
                self.m.uc.ctl_remove_cache(0x0800_0000 + page, 0x0800_0000 + page + 256)
                self.statr |= 0x20
        else:
            self.r[off] = value


# --------------------------------------------------------------------- GPIO
class GPIO(Peripheral):
    """GPIO port (RM "GPIO"): CFGLR/CFGHR/INDR/OUTDR/BSHR/BCR/LCKR."""

    size = 0x400

    def __init__(self, name: str, base: int):
        self.name, self.base = name, base
        self.cfg = [0x4] * 16          # reset: floating input
        self.outdr = 0
        self.drive: dict[int, int] = {}            # pin -> level forced by the world
        self.on_change: list[Callable[[str, int, int, int], None]] = []

    def mode(self, pin):
        return self.cfg[pin] & 3

    def is_output(self, pin):
        return self.mode(pin) != 0

    def level(self, pin) -> int:
        """Electrical level seen on the pin."""
        if self.is_output(pin):
            if self.cfg[pin] & 0x4 and self.outdr >> pin & 1:   # open drain released
                return self.drive.get(pin, 1)
            return self.outdr >> pin & 1
        if pin in self.drive:
            return self.drive[pin]
        cnf = self.cfg[pin] >> 2
        if cnf == 2:                                  # pull-up / pull-down
            return self.outdr >> pin & 1
        return 0

    def indr(self):
        return sum(self.level(p) << p for p in range(16))

    def _set_out(self, new):
        old, self.outdr = self.outdr, new & 0xFFFF
        diff = old ^ self.outdr
        if diff and self.on_change:
            for p in range(16):
                if diff >> p & 1:
                    for cb in self.on_change:
                        cb(self.name, p, self.outdr >> p & 1, self.m.now())

    def read(self, off, width):
        if off == 0x00:
            return sum(c << (4 * i) for i, c in enumerate(self.cfg[:8]))
        if off == 0x04:
            return sum(c << (4 * i) for i, c in enumerate(self.cfg[8:]))
        if off == 0x08:
            return self.indr()
        if off == 0x0C:
            return self.outdr
        return 0

    def write(self, off, width, value):
        if off == 0x00:
            self.cfg[:8] = [(value >> (4 * i)) & 0xF for i in range(8)]
        elif off == 0x04:
            self.cfg[8:] = [(value >> (4 * i)) & 0xF for i in range(8)]
        elif off == 0x0C:
            self._set_out(value)
        elif off == 0x10:
            self._set_out((self.outdr | (value & 0xFFFF)) & ~(value >> 16))
        elif off == 0x14:
            self._set_out(self.outdr & ~(value & 0xFFFF))


# ---------------------------------------------------------------------- DMA
class DMA(Peripheral):
    """DMA1 (RM "DMA"): INTFR/INTFCR and per-channel CFGR/CNTR/PADDR/MADDR."""

    base, size = 0x4002_0000, 0x400
    CHANNELS = 8

    def __init__(self):
        self.intfr = 0
        self.cfgr = [0] * (self.CHANNELS + 1)
        self.cntr = [0] * (self.CHANNELS + 1)
        self.reload = [0] * (self.CHANNELS + 1)
        self.paddr = [0] * (self.CHANNELS + 1)
        self.maddr = [0] * (self.CHANNELS + 1)
        self.pos = [0] * (self.CHANNELS + 1)   # items transferred since (re)load
        self.kick: dict[int, Callable[[], None]] = {}

    def attach(self, m):
        super().attach(m)
        for ch, irq in IRQ_DMA1_CH.items():
            m.pfic.line(irq, lambda ch=ch: self._irq(ch))

    def _irq(self, ch):
        f = self.intfr >> (4 * (ch - 1)) & 0xF
        c = self.cfgr[ch]
        return bool((f & 2 and c & 2) or (f & 4 and c & 4) or (f & 8 and c & 8))

    def enabled(self, ch):
        return bool(self.cfgr[ch] & 1) and self.cntr[ch] > 0

    def _size(self, bits):
        return 1 << bits if bits < 3 else 4

    def transfer(self, ch: int, periph_value: int | None = None) -> int | None:
        """Perform one request on channel ch.

        Peripheral-to-memory: periph_value is stored.  Memory-to-peripheral:
        the fetched value is returned to the requesting peripheral.
        """
        if not self.enabled(ch):
            return None
        c = self.cfgr[ch]
        psize = self._size(c >> 8 & 3)
        msize = self._size(c >> 10 & 3)
        i = self.pos[ch]
        ma = self.maddr[ch] + (i * msize if c & 0x80 else 0)
        uc = self.m.uc
        out = None
        if c & 0x10:   # DIR=1: read memory
            out = int.from_bytes(uc.mem_read(ma, msize), "little")
        else:
            uc.mem_write(ma, (periph_value & ((1 << (8 * msize)) - 1)).to_bytes(msize, "little"))
        self.pos[ch] += 1
        self.cntr[ch] -= 1
        sh = 4 * (ch - 1)
        if self.cntr[ch] == self.reload[ch] // 2:
            self.intfr |= (1 | 4) << sh
        if self.cntr[ch] == 0:
            self.intfr |= (1 | 2) << sh
            if c & 0x20:  # circular
                self.cntr[ch] = self.reload[ch]
                self.pos[ch] = 0
        del psize
        return out

    def read(self, off, width):
        if off == 0x00:
            return self.intfr
        ch, reg = divmod(off - 0x08, 20)
        ch += 1
        if not 1 <= ch <= self.CHANNELS:
            return 0
        return [self.cfgr[ch], self.cntr[ch], self.paddr[ch], self.maddr[ch], 0][reg // 4]

    def write(self, off, width, value):
        if off == 0x04:
            clr = value
            for ch in range(1, self.CHANNELS + 1):     # CGIFx clears all of the channel's flags
                if clr >> (4 * (ch - 1)) & 1:
                    clr |= 0xF << (4 * (ch - 1))
            self.intfr &= ~clr
            return
        if off < 0x08:
            return
        ch, reg = divmod(off - 0x08, 20)
        ch += 1
        if not 1 <= ch <= self.CHANNELS:
            return
        reg //= 4
        if reg == 0:
            was = self.cfgr[ch] & 1
            self.cfgr[ch] = value
            if value & 1 and not was:
                self.pos[ch] = 0
                if ch in self.kick:
                    self.m.after(1, self.kick[ch])
        elif reg == 1:
            if not self.cfgr[ch] & 1:      # CNTR is writable only while disabled
                self.cntr[ch] = self.reload[ch] = value & 0xFFFF
        elif reg == 2:
            self.paddr[ch] = value
        elif reg == 3:
            self.maddr[ch] = value


# -------------------------------------------------------------------- USART
class USART(Peripheral):
    """USART (RM "USART"), STM32F1-compatible layout.

    Bytes travel as 11-bit characters (start + 8 data + parity + stop, the
    8E1 used on both BMCU links).  A peer object receives transmitted bytes
    via ``peer(byte, cycle)`` and injects received bytes with ``rx()``.
    """

    size = 0x400
    STATR, DATAR, BRR, CTLR1, CTLR2, CTLR3, GPR = range(0, 0x1C, 4)
    PE, FE, NE, ORE, IDLE, RXNE, TC, TXE = (1 << i for i in range(8))

    def __init__(self, name, base, irq, apb, dma_tx=None, dma_rx=None):
        self.name, self.base, self.irq, self.apb = name, base, irq, apb
        self.dma_tx, self.dma_rx = dma_tx, dma_rx
        self.sr = self.TC | self.TXE
        self.r = {self.CTLR1: 0, self.CTLR2: 0, self.CTLR3: 0, self.BRR: 0, self.GPR: 0}
        self.rdr = 0
        self.tdr: int | None = None
        self.shifting = False
        self._sr_read = False
        self.peer: Callable[[int, int], None] | None = None
        self._rx_queue: list[int] = []
        self._rx_busy_until = 0
        self._idle_armed = False
        self.tx_log: list[tuple[int, int]] = []
        self.rx_overruns = 0

    def attach(self, m):
        super().attach(m)
        m.pfic.line(self.irq, self._irq)
        if self.dma_tx:
            m.dma.kick[self.dma_tx] = self._dma_tx_pull

    # ---- helpers
    def _enabled(self, bit):
        c1 = self.r[self.CTLR1]
        return bool(c1 & (1 << 13) and c1 & bit)

    def char_cycles(self) -> int:
        brr = self.r[self.BRR] or 1
        baud = self.m.rcc.pclk(self.apb) * 16 // (16 * brr) if brr else 1
        return max(1, HCLK_HZ * 11 // max(baud, 1))

    def _irq(self):
        c1 = self.r[self.CTLR1]
        sr = self.sr
        return bool((sr & self.TXE and c1 & (1 << 7)) or (sr & self.TC and c1 & (1 << 6)) or
                    (sr & (self.RXNE | self.ORE) and c1 & (1 << 5)) or (sr & self.IDLE and c1 & (1 << 4)) or
                    (sr & self.PE and c1 & (1 << 8)))

    # ---- transmit
    def _start_shift(self, byte):
        self.shifting = True
        self.sr &= ~self.TC
        self.m.after(self.char_cycles(), lambda b=byte: self._shift_done(b))

    def _shift_done(self, byte):
        t = self.m.cycles
        self.tx_log.append((t, byte))
        if self.peer:
            self.peer(byte, t)
        if self.tdr is not None:
            b, self.tdr = self.tdr, None
            self.sr |= self.TXE
            self._start_shift(b)
            self._dma_tx_pull()
        else:
            self.shifting = False
            self.sr |= self.TC

    def _write_tdr(self, byte):
        if not self._enabled(1 << 3):
            return
        if not self.shifting:
            self._start_shift(byte)
            self.sr |= self.TXE
        else:
            self.tdr = byte
            self.sr &= ~self.TXE
        self.sr &= ~self.TC

    def _dma_tx_pull(self):
        if self.dma_tx and self.r[self.CTLR3] & (1 << 7) and self.sr & self.TXE and self.tdr is None:
            v = self.m.dma.transfer(self.dma_tx)
            if v is not None:
                self._write_tdr(v & 0xFF)
                if self.sr & self.TXE:
                    self.m.after(1, self._dma_tx_pull)

    # ---- receive
    def rx(self, data: bytes, gap_cycles: int = 0) -> None:
        """Queue bytes from the peer; they arrive back to back after ``gap_cycles``."""
        start = max(self.m.now() + gap_cycles, self._rx_busy_until)
        cc = self.char_cycles()
        for i, b in enumerate(data):
            self.m.at(start + (i + 1) * cc, lambda b=b: self._rx_byte(b))
        self._rx_busy_until = start + len(data) * cc
        self.m.at(self._rx_busy_until + cc, self._maybe_idle)

    def _rx_byte(self, b):
        if not self._enabled(1 << 2):
            return
        self._idle_armed = True
        if self.dma_rx and self.r[self.CTLR3] & (1 << 6) and self.m.dma.enabled(self.dma_rx):
            self.m.dma.transfer(self.dma_rx, b)
            return
        if self.sr & self.RXNE:
            self.sr |= self.ORE
            self.rx_overruns += 1
            return
        self.rdr = b
        self.sr |= self.RXNE

    def _maybe_idle(self):
        if self.m.now() >= self._rx_busy_until and self._idle_armed:
            self._idle_armed = False
            self.sr |= self.IDLE

    # ---- registers
    def read(self, off, width):
        if off == self.STATR:
            self._sr_read = True
            return self.sr
        if off == self.DATAR:
            self.sr &= ~self.RXNE
            if self._sr_read:
                self.sr &= ~(self.IDLE | self.ORE | self.NE | self.FE | self.PE)
            self._sr_read = False
            return self.rdr
        return self.r.get(off, 0)

    def write(self, off, width, value):
        if off == self.STATR:
            self.sr &= value | ~(self.TC | self.RXNE)     # rc_w0 bits
            return
        if off == self.DATAR:
            if self._sr_read:
                self.sr &= ~self.TC
            self._sr_read = False
            self._write_tdr(value & 0xFF)
            return
        self.r[off] = value
        if off == self.CTLR3:
            self._dma_tx_pull()


# ---------------------------------------------------------------------- ADC
class ADC(Peripheral):
    """ADC1/ADC2 (RM "ADC") with regular sequence, continuous mode and DMA.

    Conversions are produced in batches every ``BATCH_US`` of virtual time;
    within a batch every conversion of the sequence sees the same analogue
    input, which is accurate for the slowly varying signals on this board.
    Volts come from ``source(channel, cycle)`` and are quantised to 12 bits
    against a 3.3 V reference.
    """

    size = 0x400
    BATCH_US = 200
    VREF = 3.3
    SMP_CYCLES = (1.5, 7.5, 13.5, 28.5, 41.5, 55.5, 71.5, 239.5)

    def __init__(self, name, base, master: "ADC | None" = None):
        self.name, self.base, self.master = name, base, master
        self.r: dict[int, int] = {}
        self.sr = 0
        self.source: Callable[[int, int], float] = lambda ch, t: 0.0
        self.slave: ADC | None = None
        self._running = False
        self.samples = 0

    def seq(self) -> list[int]:
        n = (self.r.get(0x2C, 0) >> 20 & 0xF) + 1
        regs = [self.r.get(0x34, 0), self.r.get(0x30, 0), self.r.get(0x2C, 0)]
        return [(regs[i // 6] >> (5 * (i % 6))) & 0x1F for i in range(n)]

    def code(self, ch, t) -> int:
        v = self.source(ch, t)
        return max(0, min(4095, int(round(v / self.VREF * 4095))))

    def conv_cycles(self) -> int:
        adcclk = self.m.rcc.adcclk() or 1
        total = 0.0
        for ch in self.seq():
            reg = 0x10 if ch < 10 else 0x0C
            smp = (self.r.get(reg, 0) >> (3 * (ch % 10))) & 7
            total += self.SMP_CYCLES[smp] + 12.5
        return max(1, int(total * HCLK_HZ / adcclk))

    def _start(self):
        if not self._running:
            self._running = True
            self.m.after(1, self._batch)

    def _batch(self):
        c2 = self.r.get(0x08, 0)
        if not c2 & 1:
            self._running = False
            return
        t = self.m.cycles
        per_seq = self.conv_cycles()
        batch = HCLK_HZ * self.BATCH_US // 1_000_000
        n_seq = max(1, batch // per_seq) if c2 & 2 else 1
        seq = self.seq()
        dual = self.slave is not None and (self.r.get(0x04, 0) >> 16 & 0xF) != 0
        codes = [self.code(ch, t) for ch in seq]
        if dual:
            scodes = [self.slave.code(ch, t) for ch in self.slave.seq()]
            words = [(scodes[i] if i < len(scodes) else 0) << 16 | codes[i] for i in range(len(seq))]
        else:
            words = codes
        dma_on = c2 & (1 << 8) and self.m.dma.enabled(1)
        # Circular DMA only keeps the newest buffer's worth of conversions.
        total = n_seq * len(words)
        keep = total
        if dma_on and self.m.dma.cfgr[1] & 0x20:
            keep = min(total, self.m.dma.reload[1] + total % max(1, self.m.dma.reload[1]))
        start = total - keep
        for k in range(start, total):
            w = words[k % len(words)]
            if dma_on:
                self.m.dma.transfer(1, w)
        if dma_on and start:
            self.m.dma.intfr |= 0x7         # wrapped at least once: HT+TC seen
        self.r[0x4C] = words[-1]
        self.sr |= 2                         # EOC
        self.samples += total
        if c2 & 2:
            self.m.after(n_seq * per_seq, self._batch)
        else:
            self._running = False

    def read(self, off, width):
        if off == 0x00:
            return self.sr
        if off == 0x4C:
            self.sr &= ~2
            return self.r.get(0x4C, 0)
        return self.r.get(off, 0)

    def write(self, off, width, value):
        if off == 0x00:
            self.sr &= value
            return
        if off == 0x08:
            old = self.r.get(0x08, 0)
            value &= ~0xC                    # CAL/RSTCAL complete immediately
            self.r[off] = value & ~(1 << 22)
            # Conversion starts on SWSTART, or on ADON written while already on.
            if value & 1 and (value & (1 << 22) or old & 1):
                if self.master is None:
                    self._start()
            return
        self.r[off] = value


# ---------------------------------------------------------------------- CRC
class CRCUnit(Peripheral):
    """CRC calculation unit (RM "CRC"): CRC-32/MPEG-2 over 32-bit words.

    Polynomial 0x04C11DB7, initial value 0xFFFFFFFF, no reflection, no final
    XOR; DATAR write feeds a word, CTLR.RESET reloads the initial value.
    """

    base, size = 0x4002_3000, 0x400

    def __init__(self):
        self.crc = 0xFFFF_FFFF
        self.idr = 0

    def feed(self, word):
        c = self.crc ^ (word & 0xFFFF_FFFF)
        for _ in range(32):
            c = ((c << 1) ^ 0x04C1_1DB7) & 0xFFFF_FFFF if c & 0x8000_0000 else (c << 1) & 0xFFFF_FFFF
        self.crc = c

    def read(self, off, width):
        return {0x00: self.crc, 0x04: self.idr}.get(off, 0)

    def write(self, off, width, value):
        if off == 0x00:
            self.feed(value)
        elif off == 0x04:
            self.idr = value & 0xFF
        elif off == 0x08 and value & 1:
            self.crc = 0xFFFF_FFFF


# ----------------------------------------------------------------- TIMERS
class Timer(Store):
    """General-purpose timer: registers only, plus a time-derived CNT.

    The world model reads ARR/CCRx/CCER to recover PWM duty.
    """

    def __init__(self, name, base):
        super().__init__(base)
        self.name = name
        self._t0 = 0

    def duty(self, channel: int) -> float:
        arr = self.r.get(0x2C, 0) + 1
        ccr = self.r.get(0x34 + 4 * (channel - 1), 0)
        en = self.r.get(0x20, 0) >> (4 * (channel - 1)) & 1
        return min(1.0, ccr / arr) if en and self.r.get(0x00, 0) & 1 else 0.0

    def read(self, off, width):
        if off == 0x24 and self.r.get(0x00, 0) & 1:
            psc = self.r.get(0x28, 0) + 1
            arr = self.r.get(0x2C, 0) + 1
            return ((self.m.now() - self._t0) // psc) % arr
        return super().read(off, width)

    def write(self, off, width, value):
        if off == 0x00 and value & 1 and not self.r.get(0x00, 0) & 1:
            self._t0 = self.m.now()
        super().write(off, width, value)


def build_ch32v203(m: Machine) -> None:
    """Instantiate the CH32V203C8 peripheral set on machine m."""
    m.rcc = m.add(RCC())
    m.add(FlashCtl())
    m.systick = m.add(SysTick())
    m.dma = m.add(DMA())
    m.crc = m.add(CRCUnit())
    m.gpio = {}
    for i, name in enumerate("ABCD"):
        m.gpio[name] = m.add(GPIO(name, 0x4001_0800 + 0x400 * i))
    m.usart1 = m.add(USART("USART1", 0x4001_3800, IRQ_USART1, apb=2, dma_tx=4, dma_rx=5))
    m.usart2 = m.add(USART("USART2", 0x4000_4400, IRQ_USART2, apb=1, dma_tx=7, dma_rx=6))
    m.usart3 = m.add(USART("USART3", 0x4000_4800, IRQ_USART3, apb=1, dma_tx=2, dma_rx=3))
    m.adc1 = m.add(ADC("ADC1", 0x4001_2400))
    m.adc2 = m.add(ADC("ADC2", 0x4001_2800, master=m.adc1))
    m.adc1.slave = m.adc2
    m.tim = {}
    for name, base in (("TIM1", 0x4001_2C00), ("TIM2", 0x4000_0000), ("TIM3", 0x4000_0400), ("TIM4", 0x4000_0800)):
        m.tim[name] = m.add(Timer(name, base))
