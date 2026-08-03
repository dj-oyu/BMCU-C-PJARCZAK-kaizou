"""Fixed-memory health metrics and PICO_DIAGNOSTIC encoder."""

try:
    import ustruct as struct
except ImportError:
    import struct
import gc

import bmcu_binary as binary
import bmcu_binary_constants as C


def _bit_length(value):
    """Return the positive integer bit length on MicroPython and CPython."""
    length = 0
    while value:
        length += 1
        value >>= 1
    return length


def rp2_temperature_milli_c(reading):
    """Convert an RP2040/RP2350 temperature ADC reading to milli-Celsius."""
    voltage_uv = max(0, min(65535, int(reading))) * 3300000 // 65535
    return 27000 - ((voltage_uv - 706000) * 1000 // 1721)

class MetricWindow:
    """Approximate quantiles with fixed power-of-two microsecond buckets."""

    def __init__(self, bucket_count=32):
        self.buckets = [0] * bucket_count
        self.count = 0
        self.total = 0
        self.maximum = 0

    def add(self, value):
        value = max(0, int(value))
        bucket = min(len(self.buckets) - 1,
                     _bit_length(value))
        self.buckets[bucket] += 1
        self.count += 1
        self.total += value
        self.maximum = max(self.maximum, value)
        if self.count >= 1024:
            self.count = 0
            self.total = 0
            for index in range(len(self.buckets)):
                self.buckets[index] //= 2
                self.count += self.buckets[index]
                midpoint = 0 if index == 0 else 1 << (index - 1)
                self.total += self.buckets[index] * midpoint

    def average(self):
        return self.total // self.count if self.count else 0

    def percentile(self, percent):
        if not self.count:
            return 0
        target = (self.count * percent + 99) // 100
        seen = 0
        for index, count in enumerate(self.buckets):
            seen += count
            if seen >= target:
                return 0 if index == 0 else 1 << index
        return self.maximum


class DeviceMetrics:
    def __init__(self):
        self.loop_gap = MetricWindow()
        self.transport_encode = MetricWindow()
        self.transport_send = MetricWindow()
        self.heap_min_free = None
        self.gc_count = 0
        self.gc_time_us = 0
        self.gc_max_time_us = 0
        self.buffer = bytearray(C.MAX_PAYLOAD_SIZE)
        self.value = bytearray(8)

    def observe_loop_gap(self, value_us):
        self.loop_gap.add(value_us)

    def observe_transport_encode(self, value_us):
        self.transport_encode.add(value_us)

    def observe_transport_send(self, value_us):
        self.transport_send.add(value_us)

    def observe_gc(self, duration_us):
        self.gc_count += 1
        self.gc_time_us = int(duration_us)
        self.gc_max_time_us = max(self.gc_max_time_us, self.gc_time_us)

    def _u64(self, offset, tag, value):
        struct.pack_into(">Q", self.value, 0, max(0, int(value)))
        return offset + binary.write_tlv(
            self.buffer, offset, tag, C.VALUE_UINT64, self.value)

    def _i32(self, offset, tag, value):
        struct.pack_into(">i", self.value, 0, int(value))
        return offset + binary.write_tlv(
            self.buffer, offset, tag, C.VALUE_INT32,
            memoryview(self.value)[:4])

    def snapshot(self, uptime_ms, monitors, outbox=None, client=None,
                 journal=None, wifi_rssi=None, exception_count=0,
                 temperature_milli_c=None):
        heap_free = gc.mem_free() if hasattr(gc, "mem_free") else 0
        if self.heap_min_free is None or heap_free < self.heap_min_free:
            self.heap_min_free = heap_free
        offset = 0
        for tag, value in (
            (C.DIAG_UPTIME_MS, uptime_ms),
            (C.DIAG_HEAP_FREE, heap_free),
            (C.DIAG_HEAP_MIN_FREE, self.heap_min_free),
            (C.DIAG_LOOP_GAP_AVG_US, self.loop_gap.average()),
            (C.DIAG_LOOP_GAP_P95_US, self.loop_gap.percentile(95)),
            (C.DIAG_LOOP_GAP_P99_US, self.loop_gap.percentile(99)),
            (C.DIAG_LOOP_MAX_DELAY_US, self.loop_gap.maximum),
            (C.DIAG_TRANSPORT_ENCODE_AVG_US,
             self.transport_encode.average()),
            (C.DIAG_TRANSPORT_SEND_AVG_US, self.transport_send.average()),
            (C.DIAG_TRANSPORT_SEND_MAX_US, self.transport_send.maximum),
            (C.DIAG_GC_COUNT, self.gc_count),
            (C.DIAG_GC_TIME_US, self.gc_time_us),
            (C.DIAG_GC_MAX_TIME_US, self.gc_max_time_us),
            (C.DIAG_EXCEPTION_COUNT, exception_count),
            (C.DIAG_QUEUE_DEPTH, outbox.queue_depth if outbox else 0),
            (C.DIAG_QUEUE_DROP_COUNT,
             outbox.forced_drop_count if outbox else 0),
            (C.DIAG_STATUS_REPLACEMENT_COUNT,
             outbox.status_replacements if outbox else 0),
            (C.DIAG_LAST_ACK_WATERMARK,
             outbox.last_ack_watermark if outbox else 0),
            (C.DIAG_OLDEST_UNACKNOWLEDGED_SEQUENCE,
             (outbox.durable.peek()[0]
              if outbox and outbox.durable.peek() else 0)),
            (C.DIAG_TCP_RX_BYTES, client.rx_bytes if client else 0),
            (C.DIAG_TCP_TX_BYTES, client.tx_bytes if client else 0),
            (C.DIAG_TCP_RECONNECT_COUNT,
             client.reconnect_count if client else 0),
            (C.DIAG_REPLAY_COUNT, client.replay_count if client else 0),
            (C.DIAG_JOURNAL_USED_BYTES,
             journal.bytes_written if journal else 0),
            (C.DIAG_JOURNAL_FAILURE_COUNT,
             journal.failure_count if journal else 0),
            (C.DIAG_JOURNAL_STAGE_FAILURE_COUNT,
             outbox.journal_failure_count if outbox else 0),
        ):
            offset = self._u64(offset, tag, value)
        if temperature_milli_c is not None:
            offset = self._i32(
                offset, C.DIAG_TEMPERATURE_MILLI_C,
                temperature_milli_c)
        if wifi_rssi is not None:
            offset = self._i32(offset, C.DIAG_WIFI_RSSI_DBM, wifi_rssi)
        uart_tags = (
            (C.DIAG_UART0_BACKLOG, C.DIAG_UART0_MAX_BACKLOG,
             C.DIAG_UART0_RX_BYTES, C.DIAG_UART0_CRC_ERRORS,
             C.DIAG_UART0_FRAME_ERRORS, C.DIAG_UART0_SEQUENCE_GAPS,
             C.DIAG_UART0_MAX_SERVICE_DELAY_US, C.DIAG_UART0_DRAIN_BYTES,
             C.DIAG_UART0_OVERFLOW_COUNT),
            (C.DIAG_UART1_BACKLOG, C.DIAG_UART1_MAX_BACKLOG,
             C.DIAG_UART1_RX_BYTES, C.DIAG_UART1_CRC_ERRORS,
             C.DIAG_UART1_FRAME_ERRORS, C.DIAG_UART1_SEQUENCE_GAPS,
             C.DIAG_UART1_MAX_SERVICE_DELAY_US, C.DIAG_UART1_DRAIN_BYTES,
             C.DIAG_UART1_OVERFLOW_COUNT),
        )
        for index, monitor in enumerate(monitors[:2]):
            values = (
                monitor.uart_backlog, monitor.uart_max_backlog,
                monitor.uart_drain_bytes, monitor.decoder.crc_errors,
                monitor.decoder.frame_errors, monitor.sequence_gap_count,
                monitor.uart_max_service_gap_us, monitor.uart_drain_bytes,
                monitor.uart_overflow_count,
            )
            for tag, value in zip(uart_tags[index], values):
                offset = self._u64(offset, tag, value)
        return memoryview(self.buffer)[:offset]
