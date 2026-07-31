"""Pico W / Pico 2 W BMCU monitor with binary-only production paths."""

import gc
import machine
import time
try:
    import ubinascii as binascii
except ImportError:
    import binascii
try:
    import uos as os
except ImportError:
    import os
from machine import UART, Pin

import bmcu_binary as binary
import bmcu_binary_constants as C
from bambuddy_binary_tcp import BMB1TCPClient
from bmcu_binary_outbox import BMB1Outbox
from bmcu_journal import BMJ1Journal, JournalReplayCursor
from bmcu_link import BMCUMonitor, drain_monitors
from device_metrics import DeviceMetrics
from runtime_log import PicoRuntimeLog
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
boot_id = int.from_bytes(os.urandom(8), "big")
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
    getattr(config, "BMCU_BINARY_JOURNAL_STAGING_SLOTS", 4))
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


for link_index, item in enumerate(link_configs):
    rxbuf = item.get("rxbuf", getattr(config, "UART_RXBUF", 2048))
    uart = UART(
        item["uart"], baudrate=item.get("baudrate", config.UART_BAUDRATE),
        bits=8, parity=0, stop=1, tx=Pin(item["tx"]), rx=Pin(item["rx"]),
        rxbuf=rxbuf)
    monitors.append(BMCUMonitor(
        uart, link_id=item["id"], link_index=link_index,
        on_valid_frame=enqueue_raw_metric, uart_capacity=rxbuf))


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


key_hex = getattr(config, "BMCU_BINARY_DEVICE_KEY", "")
if len(key_hex) != 64:
    raise ValueError("BMCU_BINARY_DEVICE_KEY must be 64 hex characters")
client = BMB1TCPClient(
    outbox, getattr(config, "BMCU_BINARY_HOST", ""),
    int(getattr(config, "BMCU_BINARY_PORT", 8766)),
    getattr(config, "BMCU_BINARY_DEVICE_ID", bridge_id).encode(),
    binascii.unhexlify(key_hex),
    getattr(config, "PICO_FIRMWARE_VERSION", "alpha.3").encode(),
    tuple((index, item["id"].encode())
          for index, item in enumerate(link_configs)),
    clock_ms=time.ticks_ms, control_handler=binary_control,
    clock_us=monotonic_us.now,
    send_metric=metrics.observe_transport_send,
    ticks_diff=time.ticks_diff, ticks_add=time.ticks_add)


def wifi_event(message):
    if message.get("type") == "wifi_state":
        runtime_log.info("wifi", "state changed")


wifi = WiFiStation(secrets, wifi_event)
diagnostic_frame = bytearray(C.MAX_MESSAGE_SIZE)


def diagnostic_message():
    rssi = None
    try:
        rssi = wifi.wlan.status("rssi")
    except Exception:
        pass
    payload = metrics.snapshot(
        time.ticks_ms(), monitors, outbox, client, journal, rssi,
        runtime_log.exception_count)
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


def binary_api(path):
    base = path.split("?", 1)[0]
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
        runtime_log.exception("web." + component, error))
wifi.start(time.ticks_ms())
web.start()

next_uart_index = 0
last_loop_us = monotonic_us.now()
last_diagnostic_ms = None
last_gc_ms = None


def record_exception(component, error):
    try:
        runtime_log.exception(component, error)
    except Exception:
        pass


def service_once(now_ms):
    global next_uart_index, last_loop_us, last_diagnostic_ms, last_gc_ms
    current_us = monotonic_us.now()
    metrics.observe_loop_gap(max(0, current_us - last_loop_us))
    last_loop_us = current_us
    try:
        next_uart_index, _ = drain_monitors(
            monitors, now_ms,
            getattr(config, "BMCU_UART_DRAIN_BUDGET", 1024),
            getattr(config, "BMCU_UART_DRAIN_CHUNK", 128),
            next_uart_index)
    except Exception as error:
        record_exception("bmcu.drain", error)
    try:
        wifi.poll(now_ms)
    except Exception as error:
        record_exception("wifi.poll", error)
    try:
        client.poll(now_ms, wifi.state == "online")
    except Exception as error:
        record_exception("bambuddy.binary", error)
    uart_idle = not any(monitor.uart.any() for monitor in monitors)
    if uart_idle:
        try:
            journal.flush_one(monotonic_us.now())
        except Exception as error:
            journal.failure_count += 1
            record_exception("journal.flush", error)
    # One non-blocking HTTP accept/read/write step per loop. Gating this on
    # every UART being empty starves the UI when two BMCUs stream continuously.
    try:
        web.poll()
    except Exception as error:
        record_exception("web.poll", error)
    if last_diagnostic_ms is None or ticks_diff(
            now_ms, last_diagnostic_ms) >= 15000:
        payload = metrics.snapshot(
            now_ms, monitors, outbox, client, journal, None,
            runtime_log.exception_count)
        outbox.enqueue_payload(
            C.PICO_DIAGNOSTIC, 0, C.GLOBAL_SCOPE, monotonic_us.now(),
            payload, False)
        last_diagnostic_ms = now_ms
    if (last_gc_ms is None or ticks_diff(now_ms, last_gc_ms) >= 60000) and \
            uart_idle:
        started = monotonic_us.now()
        gc.collect()
        metrics.observe_gc(monotonic_us.now() - started)
        last_gc_ms = now_ms
    for monitor in monitors:
        try:
            monitor.ping_if_idle(now_ms)
        except Exception as error:
            record_exception("bmcu.ping", error)


while True:
    now = time.ticks_ms()
    try:
        service_once(now)
    except Exception as error:
        record_exception("main.loop", error)
    time.sleep_ms(1)
