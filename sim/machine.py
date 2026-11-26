"""CH32V203 machine model that runs a released BMCU firmware image unmodified.

Independence rule for this package: nothing here is derived from the firmware
source tree (src/).  The CPU core is Unicorn (QEMU's RISC-V), the QingKe/PFIC
behaviour and every peripheral register come from the WCH CH32V20x reference
manual and the QingKe V4 processor manual, and the outside world comes from
public protocol documents and recordings of the real device.  If the firmware
is wrong, the machine must not be "wrong in the same way".

Time is virtual.  One executed instruction counts as one HCLK cycle; the clock
only moves when the CPU runs or when a detected busy-wait is fast-forwarded.
"""
from __future__ import annotations

import ctypes
import heapq
from dataclasses import dataclass, field
from typing import Callable

from unicorn import (UC_ARCH_RISCV, UC_HOOK_CODE, UC_HOOK_BLOCK, UC_HOOK_INTR, UC_HOOK_MEM_WRITE, UC_MODE_RISCV32,
                     UC_PROT_ALL, UC_PROT_EXEC, UC_PROT_READ, Uc, UcError)
from unicorn import riscv_const as R

from .clock import HCLK_HZ  # noqa: E402,F401

FLASH_BASE = 0x0800_0000
FLASH_ALIAS = 0x0000_0000
FLASH_SIZE = 0x1_0000          # CH32V203C8: 64 KiB
SRAM_BASE = 0x2000_0000
SRAM_SIZE = 0x5000             # 20 KiB
SYSMEM_BASE = 0x1FFF_F000      # ESIG (flash size, UID) lives here
ESIG_FLACAP = 0x1FFF_F7E0
ESIG_UNIID = 0x1FFF_F7E8
TRAMPOLINE = 0x3000_0000       # unmapped on the chip; used as the HPE return address

# QingKe vendor CSRs (QingKe V4 manual, "CSR register list").
CSR_GINTENR = 0x800            # alias of mstatus.MIE/MPIE
CSR_INTSYSCR = 0x804           # bit0 HWSTKEN (HPE), bit1 INESTEN (nesting)
CSR_CORECFGR = 0xBC0

# Registers the HPE saves on interrupt entry.  The V4 manual lists the
# caller-saved integer registers; restoring a superset is harmless because a
# "WCH-Interrupt-fast" handler, by contract, may clobber exactly those.
HPE_REGS = [R.UC_RISCV_REG_X1] + [R.UC_RISCV_REG_X0 + i for i in
                                  (5, 6, 7, 10, 11, 12, 13, 14, 15, 16, 17, 28, 29, 30, 31)]

MSTATUS_MIE = 1 << 3
MSTATUS_MPIE = 1 << 7


@dataclass(order=True)
class _Event:
    when: int
    seq: int
    fn: Callable[[], None] = field(compare=False)


def _is_xw(h: int) -> bool:
    op, f3 = h & 3, h >> 13
    return (op in (0, 2) and f3 in (1, 5)) or (op == 0 and h >> 11 == 0b10000)


class Peripheral:
    """Base class: a register block mapped at [base, base+size)."""

    base = 0
    size = 0x400

    def attach(self, m: "Machine") -> None:
        self.m = m

    def read(self, off: int, width: int) -> int:  # pragma: no cover - overridden
        return 0

    def write(self, off: int, width: int, value: int) -> None:  # pragma: no cover
        pass


class Machine:
    SLICE = 1_440   # max cycles between interrupt checks (10 us)
    POLL_STEP = 7_200   # 50 us: busy-wait fast-forward quantum

    def __init__(self, image: bytes, uid: bytes = bytes(range(0x10, 0x1C))):
        if len(image) > FLASH_SIZE:
            raise ValueError("image larger than CH32V203C8 flash")
        self.uc = uc = Uc(UC_ARCH_RISCV, UC_MODE_RISCV32)
        uc.ctl_set_cpu_model(R.UC_CPU_RISCV32_SIFIVE_E31)
        # One host buffer backs both the 0x0 boot alias and 0x08000000, as on
        # the chip.  Guest stores into it are intercepted (see _on_flash_store)
        # and replayed through the flash controller model.
        self.flash = ctypes.create_string_buffer(image + b"\xff" * (FLASH_SIZE - len(image)), FLASH_SIZE)
        for base in (FLASH_ALIAS, FLASH_BASE):
            uc.mem_map_ptr(base, FLASH_SIZE, UC_PROT_ALL, ctypes.addressof(self.flash))
            uc.hook_add(UC_HOOK_MEM_WRITE, self._on_flash_store, begin=base, end=base + FLASH_SIZE - 1)
        self._flash_pending: list[tuple[int, int, int, bytes]] = []
        uc.mem_map(SRAM_BASE, SRAM_SIZE, UC_PROT_ALL)
        uc.mem_map(SYSMEM_BASE, 0x1000, UC_PROT_READ)
        uc.mem_write(ESIG_FLACAP, (FLASH_SIZE // 1024).to_bytes(2, "little"))
        uc.mem_write(ESIG_UNIID, uid)
        uc.mem_map(TRAMPOLINE, 0x1000, UC_PROT_READ | UC_PROT_EXEC)
        uc.mem_write(TRAMPOLINE, b"\x13\x00\x00\x00" * 4)  # nop
        uc.hook_add(UC_HOOK_INTR, self._on_trap)
        uc.hook_add(UC_HOOK_BLOCK, self._on_block)
        self._block_len: dict[int, int] = {}
        self._deadline = 0
        self._resume_pc = None
        self._first_block = True
        # The QingKe "XW" compressed loads/stores reuse the c.fld/c.fsd/
        # c.fldsp/c.fsdsp encoding space, which Unicorn decodes as FPU
        # instructions even on an FPU-less model.  Every halfword in flash that
        # could be one gets an address hook; a hook only fires when execution
        # actually starts an instruction there, so data that happens to match
        # costs nothing.
        for a in range(0, len(image) - 1, 2):
            h = image[a] | image[a + 1] << 8
            if _is_xw(h):
                uc.hook_add(UC_HOOK_CODE, self._on_xw, begin=a, end=a)
            if a + 4 <= len(image):
                w = int.from_bytes(image[a:a + 4], "little")
                if w & 0x7F == 0x73 and (w >> 12) & 7 and w >> 20 == 0x305:
                    # QEMU rejects mtvec mode 3 (QingKe "absolute address
                    # vector table"), so mtvec accesses are executed here.
                    uc.hook_add(UC_HOOK_CODE, self._on_mtvec, begin=a, end=a)
                if w == 0x3020_0073:
                    # mret is executed here too: interrupts are injected by
                    # this model (Unicorn has no PFIC), and QEMU's mret would
                    # also apply mstatus.MPP, which the QingKe startup leaves 0.
                    uc.hook_add(UC_HOOK_CODE, self._on_mret, begin=a, end=a)
        self.mtvec = 0

        self.cycles = 0                # virtual HCLK cycles since reset
        self._slice_start = 0
        self._mmio_in_slice = 0
        self._events: list[_Event] = []
        self._event_seq = 0
        self.csr = {CSR_INTSYSCR: 0, CSR_CORECFGR: 0}
        self.periph: list[Peripheral] = []
        self._hpe_stack: list[tuple[int, list[int], int]] = []
        self._stop_reason: str | None = None
        self.fault: str | None = None
        self.pc = 0
        self._poll_count = 0     # busy-wait detector (see note_poll)
        self.systick = None
        self.skipped_cycles = 0
        self.xw_count = 0
        self._xw_cache: dict = {}

        self.unmodelled: dict[int, int] = {}
        self._unmodelled_regs: dict[int, int] = {}
        self._map_buses()
        from . import periph  # local import: periph imports Machine types
        self.pfic = periph.PFIC()
        self.add(self.pfic)

    # ------------------------------------------------------------------ setup
    def add(self, p: Peripheral) -> Peripheral:
        p.attach(self)
        self.periph.append(p)
        self.periph.sort(key=lambda q: q.base)
        self._find_cache = {}
        return p

    def _find(self, addr: int):
        # Cached per 256-byte page when one answer holds for the whole page.
        key = addr >> 8
        try:
            return self._find_cache[key]
        except (KeyError, AttributeError):
            pass
        hit = None
        for p in self.periph:
            if p.base <= addr < p.base + p.size:
                hit = p
                break
        lo, hi = key << 8, (key + 1) << 8
        if (hit is not None and hit.base <= lo and hi <= hit.base + hit.size) or (
                hit is None and not any(p.base < hi and lo < p.base + p.size for p in self.periph)):
            if not hasattr(self, "_find_cache"):
                self._find_cache = {}
            self._find_cache[key] = hit
        return hit

    def _map_buses(self) -> None:
        # Unicorn maps MMIO in 4 KiB pages, finer than the chip's 1 KiB blocks,
        # so whole bus windows are mapped once and dispatched here.
        for base, size in ((0x4000_0000, 0x3_0000), (0xE000_0000, 0x1_0000)):
            self.uc.mmio_map(base, size,
                             lambda uc, off, sz, _d, b=base: self._mmio_read(b + off, sz), None,
                             lambda uc, off, sz, val, _d, b=base: self._mmio_write(b + off, sz, val), None)

    # ------------------------------------------------------------------ time
    def now(self) -> int:
        """Best estimate of the current cycle, valid inside an MMIO callback."""
        return self.cycles

    def at(self, when: int, fn: Callable[[], None]) -> None:
        self._event_seq += 1
        heapq.heappush(self._events, _Event(when, self._event_seq, fn))

    def after(self, delay: int, fn: Callable[[], None]) -> None:
        self.at(self.now() + max(1, delay), fn)

    def next_event_time(self) -> int | None:
        return self._events[0].when if self._events else None

    def note_poll(self) -> None:
        """Called by the timer that firmware spins on (SysTick CNT).

        A run of counter reads with no other bus traffic in between is a
        busy-wait (delay loops read CNTH/CNTL/CNTH, so the PC varies).  Such a
        wait can only end when time passes, so the clock is advanced in
        bounded steps -- never past the next scheduled peripheral event --
        instead of executing the loop one cycle per cycle.
        """
        self._poll_count += 1
        if self._poll_count >= 6:
            step = self.POLL_STEP
            nxt = self.next_event_time()
            if nxt is not None:
                step = max(0, min(step, nxt - self.now()))
            self.cycles += step
            self.skipped_cycles += step

    def _on_flash_store(self, uc, access, addr, size, value, _data):
        off = addr & (FLASH_SIZE - 1)
        self._flash_pending.append((off, size, value & ((1 << (8 * size)) - 1),
                                    bytes(self.flash.raw[off:off + size])))

    def flush_flash_stores(self) -> None:
        """Undo raw guest stores into flash and hand them to the controller."""
        pend, self._flash_pending = self._flash_pending, []
        for off, size, value, old in pend:
            ctypes.memmove(ctypes.addressof(self.flash) + off, old, size)
        for off, size, value, _old in pend:
            self.flashctl.cpu_store(off, size, value)

    def _mmio_read(self, addr: int, size: int) -> int:
        if self._flash_pending:
            self.flush_flash_stores()
        self._mmio_in_slice += 1
        p = self._find(addr)
        if p is not self.systick:
            self._poll_count = 0
        if p is None:
            self.unmodelled[addr] = self.unmodelled.get(addr, 0) + 1
            return self._unmodelled_regs.get(addr, 0)
        return p.read(addr - p.base, size) & ((1 << (8 * size)) - 1)

    def _mmio_write(self, addr: int, size: int, val: int) -> None:
        if self._flash_pending:
            self.flush_flash_stores()
        self._mmio_in_slice += 1
        self._poll_count = 0
        p = self._find(addr)
        if p is None:
            self.unmodelled[addr] = self.unmodelled.get(addr, 0) + 1
            self._unmodelled_regs[addr] = val
            return
        p.write(addr - p.base, size, val)

    def cur_pc(self) -> int:
        return self.uc.reg_read(R.UC_RISCV_REG_PC)

    # ------------------------------------------------------------- CSR / traps
    def _mstatus(self) -> int:
        return self.uc.reg_read(R.UC_RISCV_REG_MSTATUS)

    def _on_trap(self, uc, intno, _data):
        # On an illegal-instruction trap Unicorn has already advanced PC past
        # the trapping instruction; returning from this hook resumes there.
        nxt = uc.reg_read(R.UC_RISCV_REG_PC)
        word = int.from_bytes(uc.mem_read(nxt - 4, 4), "little")
        f3 = (word >> 12) & 7
        if word & 0x7F == 0x73 and f3 and self._vendor_csr(word, f3):
            return
        self.fault = f"trap {intno} near {nxt:#x} word {word:#010x}"
        uc.emu_stop()

    def _on_mret(self, uc, addr, size, _data):
        if size != 4:
            return
        ms = self._mstatus()
        ms = (ms & ~MSTATUS_MIE) | (MSTATUS_MIE if ms & MSTATUS_MPIE else 0) | MSTATUS_MPIE
        uc.reg_write(R.UC_RISCV_REG_MSTATUS, ms)
        target = uc.reg_read(R.UC_RISCV_REG_MEPC)
        if target == TRAMPOLINE and self._hpe_stack:
            self._stop_reason = "iret"
            uc.reg_write(R.UC_RISCV_REG_PC, TRAMPOLINE)
            uc.emu_stop()
        else:
            uc.reg_write(R.UC_RISCV_REG_PC, target)

    def _on_mtvec(self, uc, addr, size, _data):
        if size != 4:
            return
        w = int.from_bytes(uc.mem_read(addr, 4), "little")
        f3, rd, rs = (w >> 12) & 7, (w >> 7) & 31, (w >> 15) & 31
        src = rs if f3 >= 5 else (uc.reg_read(R.UC_RISCV_REG_X0 + rs) if rs else 0)
        old = self.mtvec
        if f3 & 3 == 1:
            self.mtvec = src
        elif f3 & 3 == 2 and rs:
            self.mtvec = old | src
        elif f3 & 3 == 3 and rs:
            self.mtvec = old & ~src
        if rd:
            uc.reg_write(R.UC_RISCV_REG_X0 + rd, old)
        uc.reg_write(R.UC_RISCV_REG_PC, addr + 4)

    def _on_xw(self, uc, addr, size, _data):
        d = self._xw_cache.get(addr)
        if d is None:
            d = self._xw_decode(int.from_bytes(uc.mem_read(addr, 2), "little")) if size == 2 else False
            self._xw_cache[addr] = d
        if d:
            self._xw_exec(d)
            self.xw_count += 1
            uc.reg_write(R.UC_RISCV_REG_PC, addr + 2)

    def _xw(self, h: int) -> bool:
        """Decode and execute one XW instruction; False if h is not one."""
        d = self._xw_decode(h)
        if d:
            self._xw_exec(d)
        return bool(d)

    def _xw_exec(self, d) -> None:
        base, data, imm, width, store = d
        uc = self.uc
        addr = (uc.reg_read(base) + imm) & 0xFFFF_FFFF
        if store:
            self._store(addr, width, uc.reg_read(data))
        else:
            uc.reg_write(data, self._load(addr, width))

    @staticmethod
    def _xw_decode(h: int):
        """QingKe XW (Xwchc) compressed byte/halfword loads and stores.

        Encodings from LLVM's RISCVInstrInfoXwch.td (the vendor extension as
        upstreamed by the toolchain, independent of this firmware).  Returns
        (base reg, data reg, imm, width, is_store) or False.
        """
        op, f3 = h & 3, h >> 13

        def creg(v):
            return R.UC_RISCV_REG_X0 + 8 + (v & 7)
        rdp, rs1p = creg(h >> 2), creg(h >> 7)
        if op == 0 and f3 in (1, 5):                       # qk.c.lbu / qk.c.sb
            imm = (h >> 12 & 1) | (h >> 5 & 3) << 1 | (h >> 10 & 3) << 3
            return (rs1p, rdp, imm, 1, f3 == 5)
        if op == 2 and f3 in (1, 5):                       # qk.c.lhu / qk.c.sh
            imm = (h >> 5 & 3) << 1 | (h >> 10 & 7) << 3
            return (rs1p, rdp, imm, 2, f3 == 5)
        if op == 0 and h >> 11 == 0b10000:                  # sp-relative forms
            f2 = h >> 5 & 3
            if f2 & 1:
                imm = (h >> 8 & 7) << 1 | (h >> 7 & 1) << 4
                width = 2
            else:
                imm = h >> 7 & 0xF
                width = 1
            return (R.UC_RISCV_REG_X2, rdp, imm, width, bool(f2 & 2))
        return False

    def _load(self, addr: int, width: int) -> int:
        if 0x4000_0000 <= addr < 0x4003_0000 or 0xE000_0000 <= addr < 0xE001_0000:
            return self._mmio_read(addr, width)
        return int.from_bytes(self.uc.mem_read(addr, width), "little")

    def _store(self, addr: int, width: int, value: int) -> None:
        value &= (1 << (8 * width)) - 1
        if 0x4000_0000 <= addr < 0x4003_0000 or 0xE000_0000 <= addr < 0xE001_0000:
            self._mmio_write(addr, width, value)
        else:
            self.uc.mem_write(addr, value.to_bytes(width, "little"))

    def _vendor_csr(self, insn: int, f3: int) -> bool:
        num = insn >> 20
        if num not in (CSR_GINTENR, CSR_INTSYSCR, CSR_CORECFGR):
            return False
        rd = (insn >> 7) & 31
        rs = (insn >> 15) & 31
        src = rs if f3 >= 5 else (self.uc.reg_read(R.UC_RISCV_REG_X0 + rs) if rs else 0)
        if num == CSR_GINTENR:
            ms = self._mstatus()
            old = ms & (MSTATUS_MIE | MSTATUS_MPIE)
        else:
            old = self.csr[num]
        if f3 & 3 == 1:
            new = src
        elif f3 & 3 == 2:
            new = old | src
        else:
            new = old & ~src
        if num == CSR_GINTENR:
            mask = MSTATUS_MIE | MSTATUS_MPIE
            self.uc.reg_write(R.UC_RISCV_REG_MSTATUS, (ms & ~mask) | (new & mask))
        elif not (f3 & 3 != 1 and rs == 0):
            self.csr[num] = new
        if rd:
            self.uc.reg_write(R.UC_RISCV_REG_X0 + rd, old)
        return True

    # ----------------------------------------------------------- interrupts
    def _maybe_take_interrupt(self) -> None:
        if not self._mstatus() & MSTATUS_MIE:
            return
        irq = self.pfic.highest_pending(len(self._hpe_stack), self._active_prio())
        if irq is None:
            return
        uc = self.uc
        vec_base = self.mtvec & ~3
        handler = self.pfic.handler_override(irq)
        if handler is None:
            handler = int.from_bytes(uc.mem_read(vec_base + 4 * irq, 4), "little")
        saved = [uc.reg_read(r) for r in HPE_REGS] if self.csr[CSR_INTSYSCR] & 1 else None
        self._hpe_stack.append((self.pc, saved, irq))
        self.pfic.activate(irq)
        ms = self._mstatus()
        ms = (ms & ~MSTATUS_MPIE) | (MSTATUS_MPIE if ms & MSTATUS_MIE else 0)
        ms &= ~MSTATUS_MIE
        # Nesting (INESTEN) lets a higher-priority source preempt; we keep MIE
        # cleared like the hardware does and re-check priority on every slice.
        uc.reg_write(R.UC_RISCV_REG_MSTATUS, ms)
        uc.reg_write(R.UC_RISCV_REG_MEPC, TRAMPOLINE)
        uc.reg_write(R.UC_RISCV_REG_MCAUSE, 0x8000_0000 | irq)
        self.pc = handler

    def _active_prio(self):
        if not self._hpe_stack:
            return None
        return self.pfic.priority(self._hpe_stack[-1][2])

    def _return_from_interrupt(self) -> None:
        pc, saved, irq = self._hpe_stack.pop()
        if saved is not None:
            for r, v in zip(HPE_REGS, saved):
                self.uc.reg_write(r, v)
        self.pfic.deactivate(irq)
        self.pc = pc

    # ------------------------------------------------------------------ run
    def _on_block(self, uc, addr, size, _data):
        n = self._block_len.get(addr)
        if n is None:
            n = self._count_insns(addr, size)
            self._block_len[addr] = n
        self.cycles += n
        if self._first_block:
            self._first_block = False    # always make progress
        elif self.cycles >= self._deadline:
            # Stopping here abandons this block before it runs.  Unicorn's PC
            # register is not reliably synced at a block hook, so remember
            # where to resume, and un-count the block.
            self.cycles -= n
            self._resume_pc = addr
            uc.emu_stop()

    def _count_insns(self, addr, size):
        code = self.uc.mem_read(addr, size)
        i = n = 0
        while i < size:
            i += 4 if code[i] & 3 == 3 else 2
            n += 1
        return max(n, 1)

    def run_until(self, cycles: int) -> None:
        """Run until the virtual clock reaches ``cycles``.

        Unicorn runs free between stops; a basic-block hook counts executed
        instructions (1 instruction = 1 cycle) and stops the CPU at the next
        scheduled peripheral event, the end of the run, or after
        ``SLICE`` cycles so that newly asserted interrupts are taken.
        """
        uc = self.uc
        while self.cycles < cycles and self.fault is None:
            while self._events and self._events[0].when <= self.cycles:
                heapq.heappop(self._events).fn()
            self._maybe_take_interrupt()
            self._stop_reason = None
            self._resume_pc = None
            self._first_block = True
            self._mmio_in_slice = 0
            dl = min(cycles, self.cycles + self.SLICE)
            if self._events:
                dl = min(dl, self._events[0].when)
            self._deadline = max(dl, self.cycles + 1)
            try:
                uc.emu_start(self.pc, 0xFFFF_FFFF)
            except UcError as e:
                self.fault = f"{e} at {self.cur_pc():#x}"
                break
            self.pc = self._resume_pc if self._resume_pc is not None else self.cur_pc()
            if self._flash_pending:
                self.flush_flash_stores()
            if self._stop_reason == "iret":
                self._return_from_interrupt()

    def run_for(self, seconds: float) -> None:
        self.run_until(self.cycles + int(seconds * HCLK_HZ))

    @property
    def seconds(self) -> float:
        return self.cycles / HCLK_HZ
