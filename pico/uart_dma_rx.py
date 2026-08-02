"""DMA-backed UART receive for the BMCU links.

The PL011 receive FIFO is 32 bytes, which at 115200 8E1 is 3.06 ms of tolerance
for interrupt latency. Flash commits on RP2350 run with interrupts disabled and
were measured at 32 ms average and 107 ms worst case, so the FIFO overran on
every journal commit: frames arrived truncated and failed CRC at ~4/s per link.

DMA is a bus master. Moving UARTDR into SRAM involves neither the CPU nor the
flash, so a core stalled in a flash write no longer costs bytes. A 4 KiB ring
raises the tolerance from 3.06 ms to 391 ms, which covers the worst loop stall
measured on this device (492 ms is covered by 8 KiB).

The same approach is already used on the BMCU side for the printer bus; see
``src/_bus_hardware.cpp`` and ``docs/PRINTER_RX_DMA_PARSER_SPEC.md``.

This exposes ``any``/``read``/``write``, the three methods BMCUMonitor uses, so
nothing above it changes.
"""

# RP2350 peripheral bases, confirmed on the device by reading the PrimeCell
# identification registers at base+0xFE0 (0x11, 0x10, 0x34, 0x00).
UART_BASE = (0x40070000, 0x40078000)
UARTDR_OFFSET = 0x000
UARTIMSC_OFFSET = 0x038
IMSC_RXIM = 1 << 4
IMSC_RTIM = 1 << 6

# DREQ indices, determined empirically on RP2350: sweeping treq_sel against the
# live link, 29 was the only value that produced BMCU sync bytes on UART0. The
# table is shifted from RP2040 because RP2350 adds PIO2.
UART_RX_DREQ = (29, 31)

DEFAULT_RING_BYTES = 4096
# TRANS_COUNT is reloaded well before it can reach zero; at full line rate this
# is over a day of traffic, and re-arming is counted so it stays visible.
INITIAL_COUNT = 0x0FFFFFFF
# Preferred re-arm point: still ~9 hours of traffic left, so a drained moment
# will almost certainly arrive before the hard limit.
REARM_THRESHOLD = 1 << 20
# Hard limit: re-arm even with bytes unread rather than let the DMA stop.
CRITICAL_COUNT = 1 << 16


class DmaRingError(RuntimeError):
    pass


def _log2_exact(value):
    # int.bit_length() is CPython-only; MicroPython small ints do not have it.
    if value <= 0:
        raise DmaRingError("ring size must be a power of two")
    bits = 0
    remaining = value
    while remaining > 1:
        if remaining & 1:
            raise DmaRingError("ring size must be a power of two")
        remaining >>= 1
        bits += 1
    return bits


def aligned_window(store, address_of, size):
    """Carve a naturally aligned ``size`` window out of a larger buffer.

    The DMA ring wraps by masking address bits, so the buffer has to start on a
    multiple of its own size. MicroPython cannot allocate aligned memory, but
    its collector does not move objects, so over-allocating and slicing gives a
    window whose address is stable for the lifetime of the store.
    """
    base = address_of(store)
    offset = (-base) % size
    if offset + size > len(store):
        raise DmaRingError("backing store too small to align")
    return memoryview(store)[offset:offset + size]


class DmaUartReader:
    """Reads one UART through a self-wrapping DMA ring.

    ``uart`` is still used for transmission and for its baud configuration; only
    reception is taken over. The PL011 receive interrupts are masked so
    MicroPython's own handler cannot race the DMA for the FIFO.
    """

    def __init__(self, uart, link_index, ring_bytes=DEFAULT_RING_BYTES,
                 dma=None, mem32=None, address_of=None, store=None,
                 initial_count=INITIAL_COUNT, rearm_threshold=REARM_THRESHOLD,
                 critical_count=CRITICAL_COUNT):
        self.uart = uart
        self.ring_bytes = ring_bytes
        # Injectable so tests can exhaust the transfer count by feeding bytes
        # rather than by writing the register behind the reader's back.
        self.initial_count = initial_count
        self.rearm_threshold = rearm_threshold
        self.critical_count = critical_count
        self.ring_size_log2 = _log2_exact(ring_bytes)
        self.base = UART_BASE[link_index]
        self.treq_sel = UART_RX_DREQ[link_index]

        if mem32 is None or address_of is None or dma is None:
            import machine
            import rp2
            import uctypes
            mem32 = machine.mem32 if mem32 is None else mem32
            address_of = uctypes.addressof if address_of is None else address_of
            dma = rp2.DMA() if dma is None else dma

        self.mem32 = mem32
        self.dma = dma
        # Held so the collector cannot free the memory the DMA writes into.
        self.store = store if store is not None else bytearray(ring_bytes * 2)
        self.ring = aligned_window(self.store, address_of, ring_bytes)

        self.consumed = 0
        self.overflow_count = 0
        self.rearm_count = 0
        self.lost_bytes = 0
        self._saved_imsc = None
        self._count_base = 0

    def start(self):
        imsc = self.base + UARTIMSC_OFFSET
        self._saved_imsc = self.mem32[imsc]
        self.mem32[imsc] = self._saved_imsc & ~(IMSC_RXIM | IMSC_RTIM)
        self._arm()
        return self

    def _arm(self):
        control = self.dma.pack_ctrl(
            size=0, inc_read=False, inc_write=True,
            ring_sel=1, ring_size=self.ring_size_log2,
            treq_sel=self.treq_sel, irq_quiet=True)
        self.dma.config(read=self.base + UARTDR_OFFSET, write=self.ring,
                        count=self.initial_count, ctrl=control, trigger=True)
        self._count_base = self.initial_count

    def stop(self):
        try:
            self.dma.active(0)
        except Exception:  # noqa: BLE001 - teardown must not mask the caller
            pass
        if self._saved_imsc is not None:
            self.mem32[self.base + UARTIMSC_OFFSET] = self._saved_imsc
            self._saved_imsc = None

    def _produced(self):
        """Total bytes the DMA has written since arming.

        TRANS_COUNT is sampled until two reads agree, because the DMA can retire
        a transfer between the two halves of a non-atomic read. This mirrors
        rx_dma_produced() in src/_bus_hardware.cpp.
        """
        while True:
            first = self.dma.count
            second = self.dma.count
            if first == second:
                return self._count_base - first

    def any(self):
        produced = self._produced()
        available = produced - self.consumed
        if available > self.ring_bytes:
            # The ring wrapped over unread bytes: the oldest data is gone and
            # the reader has to resynchronise from whatever is still resident.
            self.overflow_count += 1
            self.lost_bytes += available - self.ring_bytes
            self.consumed = produced - self.ring_bytes
            available = self.ring_bytes
        return available

    def read(self, count=None):
        available = self.any()
        if count is not None and count < available:
            available = count
        if available <= 0:
            return None
        start = self.consumed % self.ring_bytes
        end = start + available
        if end <= self.ring_bytes:
            data = bytes(self.ring[start:end])
        else:
            first = self.ring_bytes - start
            data = bytes(self.ring[start:]) + bytes(self.ring[:available - first])
        self.consumed += available
        # Re-arming resets the write pointer to the start of the ring, so it is
        # only lossless once everything has been consumed. Doing it here, right
        # after a drain, means the common case costs nothing.
        if self.dma.count <= self.rearm_threshold and self.consumed == self._produced():
            self._rearm()
        return data

    def write(self, data):
        return self.uart.write(data)

    def _rearm(self):
        self.rearm_count += 1
        self.consumed = 0
        self._arm()

    def service(self):
        """Last-resort re-arm for a stream that never fully drains.

        Without this the transfer count would eventually reach zero and the DMA
        would stop silently, which looks exactly like a dead link. Reaching here
        costs the unread tail, so it is counted rather than hidden.
        """
        if self.dma.count > self.critical_count:
            return False
        pending = self._produced() - self.consumed
        if pending > 0:
            self.lost_bytes += pending
        self._rearm()
        return True
