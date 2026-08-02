"""Pico W / Pico 2 W BMCU monitor with binary-only production paths."""

import gc
import machine
import time
try:
    import ustruct as struct
except ImportError:
    import struct

try:
    import uos as os
except ImportError:
    import os
from machine import UART, Pin

import bmcu_binary as binary
import bmcu_binary_constants as C
from bambuddy_binary_tcp import BMB1TCPClient
from boot_session import next_boot_id
from bmcu_binary_outbox import BMB1Outbox
from bmcu_journal import BMJ1Journal, JournalReplayCursor
from bmcu_link import BMCUMonitor, drain_monitors
from device_key_store import DeviceKeyAPI, DeviceKeyStore
from device_metrics import DeviceMetrics, rp2_temperature_milli_c
from runtime_log import PicoRuntimeLog
from transport_settings import TransportSettingsAPI, TransportSettingsStore
from uart_dma_rx import UART_BASE as DMA_UART_BASE, DmaUartReader
from web_ui import WebUI
from wifi import WiFiStation

try:
    import config
except ImportError:
    class config:
        UART_ID = 0
        UART_TX_PIN = 0
        UART_RX_PIN = 1
        UART_BAUDRATE = 115200
        UART_RXBUF = 4096
        WEB_PORT = 80
        DEBUG_USB = False
        BMCU_LINKS = (
            {"id": "bmcu-a", "uart": 0, "tx": 0, "rx": 1},
            {"id": "bmcu-b", "uart": 1, "tx": 4, "rx": 5},
        )

try:
    import secrets
except ImportError:
    secrets = None


class MonotonicMicros:
    def __init__(self):
        self.previous = time.ticks_us()
        self.total = 0

    def now(self):
        current = time.ticks_us()
        elapsed = time.ticks_diff(current, self.previous)
        self.previous = current
        if elapsed > 0:
            self.total += elapsed
        return self.total


def ticks_diff(left, right):
    return time.ticks_diff(left, right) if hasattr(
        time, "ticks_diff") else left - right


bridge_id = getattr(
    config, "BRIDGE_ID", getattr(secrets, "MDNS_HOSTNAME",
                                 "pico-bmcu-bridge"))
monotonic_us = MonotonicMicros()
boot_id = next_boot_id(os.urandom(8))
link_configs = getattr(config, "BMCU_LINKS", None) or ({
    "id": "bmcu-a", "uart": config.UART_ID,
    "tx": config.UART_TX_PIN, "rx": config.UART_RX_PIN,
},)
journal_path = getattr(config, "BMCU_BINARY_JOURNAL_PATH", "bmcu_history")
outbox = BMB1Outbox(
    boot_id, len(link_configs),
    getattr(config, "BMCU_BINARY_QUEUE_SLOTS", 128))
replay_cursor = JournalReplayCursor(journal_path)
outbox.historical_ranges = replay_cursor.available_ranges
outbox.replay_pager = lambda: replay_cursor.page_into(outbox, 16)
outbox.replay_pager()
journal = BMJ1Journal(
    journal_path, boot_id, monotonic_us.now(),
    getattr(config, "BMCU_BINARY_JOURNAL_STAGING_SLOTS", 4),
    max_segments=getattr(config, "BMCU_BINARY_JOURNAL_MAX_SEGMENTS", 8),
    commit_bytes=getattr(config, "BMCU_BINARY_JOURNAL_COMMIT_BYTES", 8192))
# One commit covers everything written since the last one, so this is the knob
# that decides how often the loop stalls for ~45 ms in flash.
JOURNAL_COMMIT_MS = int(getattr(config, "BMCU_BINARY_JOURNAL_COMMIT_MS", 10000))
outbox.journal = journal


def log_sink(record_type, flags, link_index, received_at_us, payload,
             journal_record):
    outbox.enqueue_payload(
        record_type, flags, link_index, received_at_us, payload,
        journal_record)


runtime_log = PicoRuntimeLog(
    time.ticks_ms, pico_boot_id=boot_id, sink=log_sink)
runtime_log.info("boot", "Pico application started")
metrics = DeviceMetrics()

monitors = []


def enqueue_raw_metric(link_index, received_at_us, wire, metadata):
    started = monotonic_us.now()
    result = outbox.enqueue_raw(
        link_index, received_at_us, wire, metadata)
    metrics.observe_transport_encode(monotonic_us.now() - started)
    return result


# The PL011 FIFO holds 32 bytes, 3.06 ms at 115200 8E1, and a flash commit was
# measured blocking interrupts for 32 ms. DMA drains the FIFO without the CPU,
# so a stalled core no longer costs bytes. Set BMCU_UART_DMA_RX = False to fall
# back to the interrupt-driven path.
uart_dma_rx = bool(getattr(config, "BMCU_UART_DMA_RX", True))
uart_dma_ring = int(getattr(config, "BMCU_UART_DMA_RING_BYTES", 4096))
dma_readers = []

for link_index, item in enumerate(link_configs):
    rxbuf = item.get("rxbuf", getattr(config, "UART_RXBUF", 2048))
    uart = UART(
        item["uart"], baudrate=item.get("baudrate", config.UART_BAUDRATE),
        bits=8, parity=0, stop=1, tx=Pin(item["tx"]), rx=Pin(item["rx"]),
        rxbuf=rxbuf)
    source, capacity = uart, rxbuf
    if uart_dma_rx and link_index < len(DMA_UART_BASE):
        try:
            reader = DmaUartReader(
                uart, link_index, ring_bytes=uart_dma_ring).start()
            source, capacity = reader, uart_dma_ring
            dma_readers.append(reader)
        except Exception as error:  # noqa: BLE001 - a link is worth more than DMA
            runtime_log.warning(
                "uart.dma",
                "link %d fell back to interrupt receive: %s" %
                (link_index, error))
    monitors.append(BMCUMonitor(
        source, link_id=item["id"], link_index=link_index,
        on_valid_frame=enqueue_raw_metric, uart_capacity=capacity))

# A blocking Wi-Fi stack call can span more than 100 ms. Reserve enough work
# to drain two full-rate UARTs on the following loop while retaining fairness.
uart_drain_budget = max(
    int(getattr(config, "BMCU_UART_DRAIN_BUDGET", 4096)),
    2048 * len(monitors))
uart_drain_chunk = max(
    int(getattr(config, "BMCU_UART_DRAIN_CHUNK", 512)), 256)


def binary_control(link_index, command, arguments):
    if command != C.CONTROL_SOFT_RESET or link_index >= len(monitors):
        raise ValueError("unsupported")
    monitor = monitors[link_index]
    guard = monitor.soft_reset_guard_error()
    if guard:
        raise ValueError(guard)
    reason = arguments[0] if len(arguments) else 0
    operation_id = (time.ticks_ms() & 0xFFFFFFFF) or 1
    monitor.request_soft_reset(operation_id, reason, 5000)
    return b"accepted"


key_store = DeviceKeyStore(getattr(config, "BMCU_BINARY_DEVICE_KEY", ""))
transport_store = TransportSettingsStore(
    getattr(config, "BMCU_BINARY_HOST", ""),
    getattr(config, "BMCU_BINARY_PORT", 8799))
for load_error in (key_store.load_error, transport_store.load_error):
    if load_error:
        runtime_log.warning("settings", load_error)
client = BMB1TCPClient(
    outbox, transport_store.host, transport_store.port,
    getattr(config, "BMCU_BINARY_DEVICE_ID", bridge_id).encode(),
    key_store.key if key_store.configured else bytes(32),
    getattr(config, "PICO_FIRMWARE_VERSION", "alpha.3").encode(),
    tuple((index, item["id"].encode())
          for index, item in enumerate(link_configs)),
    clock_ms=time.ticks_ms, control_handler=binary_control,
    clock_us=monotonic_us.now,
    send_metric=metrics.observe_transport_send,
    ticks_diff=time.ticks_diff, ticks_add=time.ticks_add)


def apply_device_key(key):
    client.set_device_key(key, time.ticks_ms())


key_api = DeviceKeyAPI(
    key_store, apply_device_key,
    on_update=lambda: runtime_log.info("settings", "device key updated"))


def apply_transport_endpoint(host, port):
    client.set_endpoint(host, port, time.ticks_ms())


transport_api = TransportSettingsAPI(
    transport_store, apply_transport_endpoint,
    on_update=lambda: runtime_log.info(
        "settings", "transport endpoint updated"))


def settings_api(method, path, headers, body):
    return key_api.handle(method, path, headers, body) or \
        transport_api.handle(method, path, headers, body)


def wifi_event(message):
    if message.get("type") == "wifi_state":
        runtime_log.info("wifi", "state changed")


wifi = WiFiStation(secrets, wifi_event)
try:
    temperature_adc = machine.ADC(getattr(machine.ADC, "CORE_TEMP", 4))
except Exception:
    temperature_adc = None
cached_wifi_rssi = None
cached_temperature_milli_c = None
diagnostic_frame = bytearray(C.MAX_MESSAGE_SIZE)


def refresh_platform_metrics():
    global cached_wifi_rssi, cached_temperature_milli_c
    try:
        cached_wifi_rssi = int(wifi.wlan.status("rssi"))
    except Exception:
        cached_wifi_rssi = None
    if temperature_adc is None:
        cached_temperature_milli_c = None
        return
    try:
        total = 0
        for _ in range(4):
            total += temperature_adc.read_u16()
        cached_temperature_milli_c = rp2_temperature_milli_c(total // 4)
    except Exception:
        cached_temperature_milli_c = None


def diagnostic_message():
    payload = metrics.snapshot(
        time.ticks_ms(), monitors, outbox, client, journal,
        wifi_rssi=cached_wifi_rssi,
        exception_count=runtime_log.exception_count,
        temperature_milli_c=cached_temperature_milli_c)
    size = binary.write_diagnostic(
        diagnostic_frame, 0, 0, 0, boot_id, payload)
    return memoryview(diagnostic_frame)[:size]


def _query_number(path, name, default, maximum):
    marker = name + "="
    if marker not in path:
        return default
    value = path.split(marker, 1)[1].split("&", 1)[0]
    try:
        return min(maximum, max(0, int(value)))
    except ValueError:
        return default


CAPTURE_MAGIC = b"BCAP"
CAPTURE_HEADER = 16


def capture_records():
    """Serialise rejected UART runs.

    Deliberately not a BMB1 message: this is local diagnostic instrumentation
    read straight off the device, and adding a transport message type would
    mean a wire-registry change for something that is not sent to Bambuddy.

    Record layout, big-endian:
        0  4s  magic "BCAP"
        4  B   version (1)
        5  B   link index
        6  B   reject reason (1 CRC, 2 bad length, 3 oversized chunk)
        7  B   reserved
        8  I   capture ordinal for that link
       12  H   uptime_ms & 0xFFFF at capture time
       14  H   payload length
       16  ..  the rejected bytes
    """
    out = []
    for monitor in monitors:
        capture = getattr(monitor, "capture", None)
        if capture is None:
            continue
        for ordinal, reason, timestamp, data in capture.records():
            header = bytearray(CAPTURE_HEADER)
            header[0:4] = CAPTURE_MAGIC
            header[4] = 1
            header[5] = monitor.link_index
            header[6] = reason
            struct.pack_into(
                ">IHH", header, 8, ordinal & 0xFFFFFFFF,
                timestamp & 0xFFFF, len(data))
            out.append(bytes(header) + bytes(data))
    return out


def binary_api(path):
    base = path.split("?", 1)[0]
    if base == "/api/capture.bin":
        return capture_records()
    if base == "/api/diagnostics.bin":
        return diagnostic_message()
    if base in ("/api/current.bin", "/api/history/status.bin"):
        return list(outbox.iter_current())
    if base == "/api/logs.bin":
        after = _query_number(path, "after", 0, 0xFFFFFFFFFFFFFFFF)
        limit = _query_number(path, "limit", 32, 64)
        return list(runtime_log.iter_messages(after, limit))
    if base == "/api/events.bin":
        after = _query_number(path, "after", 0, 0xFFFFFFFFFFFFFFFF)
        limit = _query_number(path, "limit", 32, 64)
        result = []
        used = 0
        for sequence, message, _, _ in outbox.durable.iter_records():
            if sequence <= after:
                continue
            if len(result) >= limit or used + len(message) > 32768:
                break
            result.append(message)
            used += len(message)
        return result
    return None


web = WebUI(
    binary_api, getattr(config, "WEB_PORT", 80),
    error_handler=lambda component, error:
        runtime_log.exception("web." + component, error),
    settings_provider=settings_api)
wifi.start(time.ticks_ms())
web.start()

next_uart_index = 0
last_loop_us = monotonic_us.now()
last_diagnostic_ms = None
last_gc_ms = None
last_flush_ms = None
last_commit_ms = None


def record_exception(component, error):
    try:
        runtime_log.exception(component, error)
    except Exception:
        pass


def service_once(now_ms):
    global next_uart_index, last_loop_us, last_diagnostic_ms, last_gc_ms
    global last_flush_ms, last_commit_ms
    current_us = monotonic_us.now()
    metrics.observe_loop_gap(max(0, current_us - last_loop_us))
    last_loop_us = current_us
    try:
        next_uart_index, _ = drain_monitors(
            monitors, now_ms, uart_drain_budget, uart_drain_chunk,
            next_uart_index)
    except Exception as error:
        record_exception("bmcu.drain", error)
    try:
        wifi.poll(now_ms)
    except Exception as error:
        record_exception("wifi.poll", error)
    if key_store.configured and transport_store.configured:
        try:
            client.poll(now_ms, wifi.state == "online")
        except Exception as error:
            record_exception("bambuddy.binary", error)
    uart_idle = not any(monitor.uart.any() for monitor in monitors)
    # A miswired or floating RX pin keeps uart.any() true forever, which used
    # to starve the journal flush and the GC below for the whole uptime. Both
    # now have a deadline that fires regardless of how busy the UARTs look.
    if uart_idle or last_flush_ms is None or ticks_diff(
            now_ms, last_flush_ms) >= 1000:
        try:
            journal.flush_one(monotonic_us.now())
        except Exception as error:
            journal.failure_count += 1
            record_exception("journal.flush", error)
        last_flush_ms = now_ms
    # Writing a record costs ~1.6 ms; committing it costs ~45 ms with
    # interrupts disabled. One commit covers every record written since the
    # last one, so it runs on its own, much slower schedule.
    if last_commit_ms is None or ticks_diff(
            now_ms, last_commit_ms) >= JOURNAL_COMMIT_MS:
        try:
            journal.commit()
        except Exception as error:
            journal.failure_count += 1
            record_exception("journal.commit", error)
        last_commit_ms = now_ms
    # One non-blocking HTTP accept/read/write step per loop. Gating this on
    # every UART being empty starves the UI when two BMCUs stream continuously.
    try:
        web.poll()
    except Exception as error:
        record_exception("web.poll", error)
    if last_diagnostic_ms is None or ticks_diff(
            now_ms, last_diagnostic_ms) >= 15000:
        refresh_platform_metrics()
        payload = metrics.snapshot(
            now_ms, monitors, outbox, client, journal,
            wifi_rssi=cached_wifi_rssi,
            exception_count=runtime_log.exception_count,
            temperature_milli_c=cached_temperature_milli_c)
        outbox.enqueue_payload(
            C.PICO_DIAGNOSTIC, 0, C.GLOBAL_SCOPE, monotonic_us.now(),
            payload, False)
        last_diagnostic_ms = now_ms
    gc_elapsed = None if last_gc_ms is None else ticks_diff(now_ms, last_gc_ms)
    if (gc_elapsed is None or gc_elapsed >= 60000) and \
            (uart_idle or gc_elapsed is None or gc_elapsed >= 300000):
        started = monotonic_us.now()
        gc.collect()
        metrics.observe_gc(monotonic_us.now() - started)
        last_gc_ms = now_ms
    for monitor in monitors:
        try:
            monitor.ping_if_idle(now_ms)
        except Exception as error:
            record_exception("bmcu.ping", error)
    for reader in dma_readers:
        # Reloads the transfer budget for a stream that never fully drains;
        # otherwise the channel would stop after about a day and the link would
        # look dead with no error anywhere.
        try:
            reader.service()
        except Exception as error:
            record_exception("uart.dma", error)


while True:
    now = time.ticks_ms()
    try:
        service_once(now)
    except Exception as error:
        record_exception("main.loop", error)
    time.sleep_ms(1)
