"""DMA ring bookkeeping, exercised against a simulated DMA channel.

The hardware cannot be modelled here, but the arithmetic that turns a
monotonically decreasing transfer count into a byte stream can be, and that is
where a wraparound bug would silently corrupt every frame.
"""
import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "pico_uart_dma_rx", ROOT / "pico" / "uart_dma_rx.py")
dma_rx = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(dma_rx)


class FakeDMA:
    """Writes into the ring the way the hardware would: wrapping, no CPU."""

    def __init__(self, ring_bytes):
        self.ring_bytes = ring_bytes
        self.count = 0
        self.ring = None
        self.config_calls = 0
        self.active_calls = []
        self.written = 0

    def pack_ctrl(self, **kwargs):
        self.ctrl = kwargs
        return 0x1234

    def config(self, read=None, write=None, count=None, ctrl=None,
               trigger=False):
        self.read = read
        self.ring = write
        self.count = count
        self.trigger = trigger
        self.config_calls += 1
        self.written = 0

    def active(self, value):
        self.active_calls.append(value)

    def feed(self, data):
        for byte in data:
            self.ring[self.written % self.ring_bytes] = byte
            self.written += 1
            self.count -= 1


class FakeUART:
    def __init__(self):
        self.writes = []

    def write(self, data):
        self.writes.append(bytes(data))
        return len(data)


class FakeMem:
    def __init__(self, values=None):
        self.values = dict(values or {})

    def __getitem__(self, address):
        return self.values.get(address, 0)

    def __setitem__(self, address, value):
        self.values[address] = value


def reader(ring_bytes=64, link_index=0, imsc=0x70, initial_count=1000,
           rearm_threshold=dma_rx.REARM_THRESHOLD,
           critical_count=dma_rx.CRITICAL_COUNT):
    fake = FakeDMA(ring_bytes)
    uart = FakeUART()
    mem = FakeMem({dma_rx.UART_BASE[link_index] + dma_rx.UARTIMSC_OFFSET: imsc})
    store = bytearray(ring_bytes * 2)
    instance = dma_rx.DmaUartReader(
        uart, link_index, ring_bytes=ring_bytes, dma=fake, mem32=mem,
        address_of=lambda _obj: 0, store=store, initial_count=initial_count,
        rearm_threshold=rearm_threshold, critical_count=critical_count)
    return instance, fake, mem, uart


class AlignmentTests(unittest.TestCase):
    def test_window_is_carved_at_a_natural_boundary(self):
        store = bytearray(8192)
        window = dma_rx.aligned_window(store, lambda _o: 0x200754d0, 4096)
        self.assertEqual(len(window), 4096)
        # 0x200754d0 % 4096 == 1232, so 2864 bytes of padding are needed.
        self.assertEqual(bytes(window), bytes(store[2864:2864 + 4096]))

    def test_an_already_aligned_buffer_needs_no_padding(self):
        store = bytearray(8192)
        window = dma_rx.aligned_window(store, lambda _o: 0x20040000, 4096)
        self.assertEqual(bytes(window), bytes(store[:4096]))

    def test_a_store_too_small_to_align_is_rejected(self):
        with self.assertRaises(dma_rx.DmaRingError):
            dma_rx.aligned_window(bytearray(4096 + 8), lambda _o: 0x1000 + 16,
                                  4096)

    def test_log2_matches_every_supported_ring_size(self):
        # The first attempt used int.bit_length(), which CPython has and
        # MicroPython does not, so both links fell back to interrupt receive on
        # the device while every host test passed. Only the values are asserted
        # here; MicroPython compatibility itself cannot be checked from CPython.
        for exponent in range(1, 16):
            self.assertEqual(dma_rx._log2_exact(1 << exponent), exponent)

    def test_ring_size_must_be_a_power_of_two(self):
        with self.assertRaises(dma_rx.DmaRingError):
            dma_rx.DmaUartReader(FakeUART(), 0, ring_bytes=3000,
                                 dma=FakeDMA(3000), mem32=FakeMem(),
                                 address_of=lambda _o: 0,
                                 store=bytearray(6000))


class RingReadTests(unittest.TestCase):
    def test_start_masks_only_the_receive_interrupts(self):
        instance, _fake, mem, _uart = reader(imsc=0x70)
        instance.start()
        # 0x70 is RXIM | TXIM | RTIM; the transmit interrupt must survive.
        self.assertEqual(mem[dma_rx.UART_BASE[0] + dma_rx.UARTIMSC_OFFSET],
                         0x20)

    def test_stop_restores_the_interrupt_mask(self):
        instance, fake, mem, _uart = reader(imsc=0x70)
        instance.start()
        instance.stop()
        self.assertEqual(mem[dma_rx.UART_BASE[0] + dma_rx.UARTIMSC_OFFSET],
                         0x70)
        self.assertEqual(fake.active_calls, [0])

    def test_uses_the_measured_dreq_and_a_write_side_ring(self):
        instance, fake, _mem, _uart = reader(ring_bytes=64)
        instance.start()
        self.assertEqual(fake.ctrl["treq_sel"], 29)
        self.assertEqual(fake.ctrl["ring_sel"], 1, "the write address must wrap")
        self.assertEqual(fake.ctrl["ring_size"], 6)
        self.assertFalse(fake.ctrl["inc_read"])
        self.assertTrue(fake.ctrl["inc_write"])

    def test_link_one_uses_its_own_base_and_dreq(self):
        instance, fake, _mem, _uart = reader(link_index=1)
        instance.start()
        self.assertEqual(fake.read, 0x40078000)
        self.assertEqual(fake.ctrl["treq_sel"], 31)

    def test_reads_what_the_dma_produced(self):
        instance, fake, _mem, _uart = reader(ring_bytes=64)
        instance.start()
        fake.feed(b"hello world")
        self.assertEqual(instance.any(), 11)
        self.assertEqual(instance.read(), b"hello world")
        self.assertEqual(instance.any(), 0)
        self.assertIsNone(instance.read())

    def test_partial_reads_leave_the_remainder(self):
        instance, fake, _mem, _uart = reader(ring_bytes=64)
        instance.start()
        fake.feed(b"0123456789")
        self.assertEqual(instance.read(4), b"0123")
        self.assertEqual(instance.any(), 6)
        self.assertEqual(instance.read(99), b"456789")

    def test_a_record_spanning_the_wrap_is_reassembled_in_order(self):
        instance, fake, _mem, _uart = reader(ring_bytes=16)
        instance.start()
        fake.feed(bytes(range(12)))
        self.assertEqual(instance.read(), bytes(range(12)))
        fake.feed(bytes(range(100, 108)))
        # Bytes 12..15 then 0..3 of the ring: contiguous to the caller.
        self.assertEqual(instance.read(), bytes(range(100, 108)))

    def test_overflow_drops_the_oldest_and_is_counted(self):
        instance, fake, _mem, _uart = reader(ring_bytes=16)
        instance.start()
        fake.feed(bytes(range(40)))
        self.assertEqual(instance.any(), 16)
        self.assertEqual(instance.overflow_count, 1)
        self.assertEqual(instance.lost_bytes, 24)
        # What survives is the newest ring-full, in order.
        self.assertEqual(instance.read(), bytes(range(24, 40)))

    def test_writes_are_delegated_to_the_uart(self):
        instance, _fake, _mem, uart = reader()
        instance.start()
        instance.write(b"ping")
        self.assertEqual(uart.writes, [b"ping"])


class RearmTests(unittest.TestCase):
    def test_a_drained_ring_rearms_without_losing_bytes(self):
        # 20 bytes consumed of a 30-byte budget leaves 10, under the threshold.
        instance, fake, _mem, _uart = reader(
            ring_bytes=64, initial_count=30, rearm_threshold=12)
        instance.start()
        fake.feed(bytes(range(20)))
        self.assertEqual(instance.read(), bytes(range(20)))
        self.assertEqual(instance.rearm_count, 1)
        self.assertEqual(instance.lost_bytes, 0)
        self.assertEqual(fake.count, 30, "the budget is restored")

    def test_a_partially_drained_ring_is_not_rearmed(self):
        instance, fake, _mem, _uart = reader(
            ring_bytes=64, initial_count=30, rearm_threshold=12)
        instance.start()
        fake.feed(bytes(range(20)))
        instance.read(5)
        self.assertEqual(instance.rearm_count, 0,
                         "re-arming here would orphan the unread tail")
        self.assertEqual(instance.read(), bytes(range(5, 20)))
        self.assertEqual(instance.rearm_count, 1)

    def test_service_is_a_no_op_while_the_count_is_healthy(self):
        instance, _fake, _mem, _uart = reader(
            initial_count=1000, rearm_threshold=0, critical_count=100)
        instance.start()
        self.assertFalse(instance.service())
        self.assertEqual(instance.rearm_count, 0)

    def test_service_rearms_a_stream_that_never_drains_and_counts_the_loss(self):
        instance, fake, _mem, _uart = reader(
            ring_bytes=64, initial_count=30, rearm_threshold=0,
            critical_count=12)
        instance.start()
        fake.feed(bytes(range(20)))
        self.assertTrue(instance.service())
        self.assertEqual(instance.rearm_count, 1)
        self.assertEqual(instance.lost_bytes, 20)
        self.assertEqual(instance.any(), 0)

    def test_reading_continues_normally_after_a_rearm(self):
        instance, fake, _mem, _uart = reader(
            ring_bytes=64, initial_count=30, rearm_threshold=12)
        instance.start()
        fake.feed(bytes(range(20)))
        instance.read()
        fake.feed(b"defgh")
        self.assertEqual(instance.read(), b"defgh")


if __name__ == "__main__":
    unittest.main()
