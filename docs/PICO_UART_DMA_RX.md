# Pico BMCU-link DMA RX specification

Status: Implemented
Date: 2026-08-03
Scope: Pico-side ingress from the BMCU links only
Related: [Printer USART1 DMA RX](PRINTER_RX_DMA_PARSER_SPEC.md),
[BMB1 transport v1](BMCU_BINARY_TRANSPORT_V1.md),
[BMCU Link protocol alpha.3](BMCU_LINK_PROTOCOL_ALPHA3.md)

This is the Pico counterpart of the printer-side DMA ingress. The BMCU already
receives the printer bus this way; the same reasoning applies to the Pico
receiving the BMCU.

## 1. The problem this solves

The PL011 receive FIFO is 32 bytes. At 115200 8E1 each byte occupies 11 bit
times, so the FIFO holds

```
32 bytes x 11 bits / 115200 bps = 3.06 ms
```

of tolerance for interrupt latency. Beyond that the FIFO overruns and the lost
bytes never reach MicroPython's ring buffer, so neither `uart.any()` nor the
overflow counter can see them.

A littlefs commit on RP2350 runs with interrupts disabled, because flash
programming requires XIP to be off. Measured on the device:

| Main-loop call | Average | Worst |
| --- | --- | --- |
| `journal.flush_one` (per record, with commit) | 32.8 ms | 107 ms |
| `client.poll` | 3.0 ms | 32 ms |
| `web.poll` | 1.3 ms | 50 ms |
| `wifi.poll` | 0.1 ms | 0.2 ms |

Every journal commit therefore exceeded the FIFO budget by an order of
magnitude, and frames arrived truncated.

### 1.1 How this was established

Removing the main loop and running only the UART drain, on the same wires at
the same data rate, dropped CRC failures from 4.0/s to 0.04/s per link. That
ruled out the cable, the BMCU, and the baud rate in one measurement.

Captured rejects (`/api/capture.bin`) then showed 15 of 15 failures were byte
losses and none were bit errors; a 36-byte STATUS frame was cut at exactly 32
bytes, the FIFO depth.

## 2. Design

DMA is a bus master. Moving `UARTDR` into SRAM involves neither the CPU nor the
flash, so a core stalled inside a flash commit no longer costs bytes. Tolerance
becomes the ring size rather than the FIFO depth.

```
ring bytes x 11 bits / 115200 bps = tolerance
   2048                          = 196 ms at full line rate
```

One DMA channel per link, configured with:

| Field | Value | Why |
| --- | --- | --- |
| `read` | `UART_BASE + 0x000` | `UARTDR`, no increment |
| `write` | aligned ring | incrementing, wrapping |
| `ring_sel` | 1 | the *write* address wraps, not the read address |
| `ring_size` | `log2(ring_bytes)` | hardware wrap by address masking |
| `size` | 0 | byte transfers |
| `treq_sel` | 29 (UART0), 31 (UART1) | paced by receive data available |
| `count` | 0x0FFFFFFF | reloaded long before exhaustion, see §5 |

MicroPython's own PL011 receive interrupts are masked (`UARTIMSC` bits RXIM and
RTIM) so its handler cannot race the DMA for the FIFO. The transmit interrupt
is left enabled; transmission still goes through `machine.UART`.

Only `any`, `read` and `write` are exposed, the three methods `BMCUMonitor`
uses, so no code above the reader changes.

## 3. Values determined on the device

These are not taken from a datasheet reading; each was confirmed on the target.

**Peripheral bases.** `0x40070000` (UART0) and `0x40078000` (UART1), identified
by reading the PrimeCell identification registers at `base + 0xFE0`, which
returned `0x11, 0x10, 0x34, 0x00`. The RP2040 addresses read as all zeroes on
this part.

**DREQ indices.** `treq_sel` was swept from 16 to 39 against live BMCU traffic,
judging each candidate by whether the ring filled with `A5 5A` sync pairs. Only
29 produced them on UART0 (204 bytes and 10 sync pairs in 250 ms; the next best
candidate produced 40 bytes and one). This is consistent with the RP2040 table
shifted by the addition of PIO2:

```
PIO0 0-7   PIO1 8-15   PIO2 16-23   SPI0 24-25   SPI1 26-27
UART0_TX 28   UART0_RX 29   UART1_TX 30   UART1_RX 31
```

## 4. Ring alignment

The hardware wraps the write address by masking address bits, so the ring must
start on a multiple of its own size. MicroPython cannot allocate aligned
memory, but its collector does not move objects, so the address of a `bytearray`
is stable once allocated.

A buffer of twice the ring size is allocated and the aligned window is carved
out of it:

```python
base = uctypes.addressof(store)
offset = (-base) % size
ring = memoryview(store)[offset:offset + size]
```

The backing store is retained by the reader so the collector cannot free memory
the DMA is still writing into.

## 5. Transfer count and re-arming

`TRANS_COUNT` decrements per byte and does not reload on wrap, so the channel
would eventually stop with no error anywhere, which is indistinguishable from a
dead link. It is armed with 0x0FFFFFFF, over a day of traffic at the observed
rate, and reloaded on two paths:

1. **Preferred, lossless.** After a read that leaves the ring fully drained and
   the count below `REARM_THRESHOLD`. Re-arming resets the write pointer to the
   start of the ring, so it is only safe when nothing is unread.
2. **Fallback, lossy.** `service()`, called from the main loop, reloads once the
   count falls below `CRITICAL_COUNT` even with bytes unread. The discarded tail
   is added to `lost_bytes` rather than hidden.

## 6. Bookkeeping

Bytes produced are derived from the transfer count, not from an interrupt:

```
produced = count_base - TRANS_COUNT
available = produced - consumed
```

`TRANS_COUNT` is sampled until two consecutive reads agree, because the DMA can
retire a transfer between the halves of a non-atomic read. This mirrors
`rx_dma_produced()` in `src/_bus_hardware.cpp`.

When `available` exceeds the ring size the ring has wrapped over unread bytes.
The oldest data is gone; `consumed` is advanced to the newest ring-full,
`overflow_count` and `lost_bytes` are incremented, and the decoder resynchronises
from the next sync byte. Because `uart_capacity` is the ring size, the monitor's
own overflow counter also reports this, so it is visible in the web UI.

## 7. Sizing

Sized against the worst loop stall measured on this device, 492 ms:

| Traffic | Bytes in 492 ms |
| --- | --- |
| 668 B/s, links settled | 329 |
| 2.9 KB/s, during the resync storm | 1,427 |
| 10.5 KB/s, full line rate | 5,166 |

2 KiB covers both observed regimes with margin, and costs 8 KiB of heap for two
links including alignment padding, on a device whose minimum free heap has
touched 1.4 KB. Measured after the change, peak backlog was 253 and 289 bytes,
12 to 14 percent of the ring, with overflow zero.

Full line rate is *not* covered. That is deliberate: the links are periodic
STATUS reporters, sustained full-rate traffic would itself be a fault, and the
overflow counter makes it visible rather than silent. Raise
`BMCU_UART_DMA_RING_BYTES` if that counter ever moves.

## 8. Fallback

Every link falls back to interrupt-driven receive if DMA setup raises, and the
reason is logged under the `uart.dma` component. This is not theoretical: the
first deployment used `int.bit_length()`, which CPython has and MicroPython does
not, and both links fell back with every host test green. Set
`BMCU_UART_DMA_RX = False` in `config.py` to select the fallback deliberately.

## 9. Result

Measured over 120 seconds on both links after the change:

| Counter | Before | After |
| --- | --- | --- |
| CRC errors | 3.9/s | 0 |
| Frame errors | 8.9/s | 0 |
| Sequence gaps | 4.0/s | 0 |
| Link RX | 2,811 B/s | 668 B/s |
| BMCU `tx_drop` | 0.15/s | 0 |

The traffic reduction is a consequence, not a regression. Byte loss produced
sequence gaps, which invalidated the link baseline, which made the Pico request
a full status snapshot, which made the BMCU emit a burst of
`FULL_STATUS_RECORD` frames large enough to overflow its own transmit queue,
which raised the loss probability again. Removing the loss broke that loop and
the links returned to their intended periodic STATUS rate.
