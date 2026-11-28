"""Pico W / Pico 2 W BMCU monitor with binary-only production paths."""

import gc
import machine
import time

try:
    import uos as os
except ImportError:
    import os
from machine import UART, Pin

import bmcu_binary as binary
import bmcu_binary_constants as C
from binary_api import LINK_STATE_CODES, BinaryAPI
from boot_session import next_boot_id
from bmcu_binary_outbox import BMB1Outbox
from bmcu_link import BMCUMonitor, drain_monitors
from bmcu_stream_udp import BMS2UDPClient
from device_metrics import DeviceMetrics, rp2_temperature_milli_c
from local_reset import LocalReset
from long_reply import LongReplyAPI
from runtime_log import PicoRuntimeLog
from transport_settings import TransportSettingsAPI, TransportSettingsStore
from uart_dma_rx import (DEFAULT_RING_BYTES as DMA_DEFAULT_RING_BYTES,
                         UART_BASE as DMA_UART_BASE, DmaUartReader)
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
    "id": "bmcu-a", "uart": getattr(config, "UART_ID", 0),
    "tx": getattr(config, "UART_TX_PIN", 0), "rx": getattr(config, "UART_RX_PIN", 1),
},)
# v2 stream (docs/BMCU_MONITOR_STREAM_V2.md): no flash journal and no replay.
# History lives at the receiver; the outbox only bridges a Wi-Fi outage.
outbox = BMB1Outbox(
    boot_id, len(link_configs),
    getattr(config, "BMCU_BINARY_QUEUE_SLOTS", 128))
journal = None


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
uart_dma_ring = int(getattr(config, "BMCU_UART_DMA_RING_BYTES", DMA_DEFAULT_RING_BYTES))
dma_readers = []

for link_index, item in enumerate(link_configs):
    rxbuf = item.get("rxbuf", getattr(config, "UART_RXBUF", 2048))
    uart = UART(
        item["uart"], baudrate=item.get("baudrate", getattr(config, "UART_BAUDRATE", 115200)),
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
    guard = monitor.soft_reset_guard_error(time.ticks_ms())
    if guard:
        # A stale refusal is recoverable: ask for a fresh snapshot now so the
        # caller's retry has current motor state to judge. Requesting it here
        # rather than inside the guard keeps the guard free of side effects.
        if "stale" in guard:
            monitor.refresh_snapshot_if_idle()
        raise ValueError(guard)
    reason = arguments[0] if len(arguments) else 0
    operation_id = (time.ticks_ms() & 0xFFFFFFFF) or 1
    monitor.request_soft_reset(operation_id, reason, 5000)
    return b"accepted"


transport_store = TransportSettingsStore(
    getattr(config, "BMCU_BINARY_HOST", ""),
    getattr(config, "BMCU_BINARY_PORT", 8799))
if transport_store.load_error:
    runtime_log.warning("settings", transport_store.load_error)


def link_state_codes():
    return tuple(LINK_STATE_CODES.get(monitor.link_state, 0)
                 for monitor in monitors)


client = BMS2UDPClient(
    outbox, transport_store.host, transport_store.port,
    getattr(config, "BMCU_BINARY_DEVICE_ID", bridge_id).encode(),
    getattr(config, "PICO_FIRMWARE_VERSION", "alpha.3").encode(),
    tuple((index, item["id"].encode())
          for index, item in enumerate(link_configs)),
    link_states=link_state_codes,
    uptime_ms=lambda: monotonic_us.now() // 1000,
    clock_ms=time.ticks_ms,
    heartbeat_ms=getattr(config, "BMCU_STREAM_HEARTBEAT_MS", 5000),
    datagrams_per_loop=getattr(config, "BMCU_STREAM_DATAGRAMS_PER_LOOP", 4),
    ticks_diff=time.ticks_diff, ticks_add=time.ticks_add)


def apply_transport_endpoint(host, port):
    client.set_endpoint(host, port, time.ticks_ms())


transport_api = TransportSettingsAPI(
    transport_store, apply_transport_endpoint,
    on_update=lambda: runtime_log.info(
        "settings", "transport endpoint updated"))


# Filled in once the panel is up (below): no panel, no local reset.
oled = None


def panel_ready():
    return oled is not None and oled.panel_ready()


local_reset = LocalReset(
    lambda link_index: binary_control(link_index, C.CONTROL_SOFT_RESET, b""),
    tuple(item["id"] for item in link_configs), panel_ready, time.ticks_ms,
    ticks_diff=time.ticks_diff,
    on_event=lambda message: runtime_log.info("control", message))


long_reply_api = LongReplyAPI(
    monitors, tuple(item["id"] for item in link_configs),
    getattr(secrets, "BMCU_CONTROL_TOKEN", None), time.ticks_ms,
    ticks_diff=time.ticks_diff,
    on_event=lambda message: runtime_log.info("control", message))
if long_reply_api.token is None:
    runtime_log.info("control", "long-reply off: no BMCU_CONTROL_TOKEN")


def settings_api(method, path, headers, body):
    return transport_api.handle(method, path, headers, body) or \
        local_reset.handle(method, path, headers, body) or \
        long_reply_api.handle(method, path, headers, body)


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


binary_api = BinaryAPI(monitors, outbox, runtime_log, diagnostic_message)


web = WebUI(
    binary_api, getattr(config, "WEB_PORT", 80),
    error_handler=lambda component, error:
        runtime_log.exception("web." + component, error),
    settings_provider=settings_api)
wifi.start(time.ticks_ms())
web.start()

# The panel is optional hardware on I2C1 (GPIO6/7 by default). A missing or
# miswired display must cost one warning and nothing else: the bridge's job is
# the UART link, and the ticker is a read-only view of it.
try:
    import oled_ticker

    oled = oled_ticker.build(
        config, monitors, wifi=wifi, client=client, metrics=metrics,
        outbox=outbox, journal=journal,
        platform_stats=lambda: (cached_wifi_rssi, cached_temperature_milli_c),
        exception_count=lambda: runtime_log.exception_count,
        overlay_provider=local_reset.overlay_lines)
except Exception as error:  # noqa: BLE001 - no display is not a boot failure
    runtime_log.warning("oled", "display unavailable: %s" % error)

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
            monitors, now_ms, uart_drain_budget, uart_drain_chunk,
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
        record_exception("bambuddy.stream", error)
    # A miswired or floating RX pin keeps uart.any() true forever; the GC
    # below has a deadline that fires regardless of how busy the UARTs look.
    uart_idle = not any(monitor.uart.any() for monitor in monitors)
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
    # One 128-byte page per loop, about 3 ms on the 400 kHz bus. Pushing a
    # whole 1 KB frame here instead would stall the UART drain for ~23 ms.
    if oled is not None:
        try:
            oled.service(now_ms)
        except Exception as error:
            record_exception("oled", error)
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
