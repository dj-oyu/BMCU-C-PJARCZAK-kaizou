"""Pico W and Pico 2 W entry point for the BMCU H1 monitor link."""

import time
import gc
import machine
try:
    import ujson as json
except ImportError:
    import json
from machine import UART, Pin

from bambuddy_config import BambuddyConfig
from bambuddy_transport import BambuddyOutbox
from bambuddy_ws import BambuddyWebSocketClient
from bmcu_link import BMCUMonitor
from runtime_log import PicoRuntimeLog
from wifi import WiFiStation
from web_ui import WebUI

try:
    import config
except ImportError:
    class config:
        UART_ID = 0
        UART_TX_PIN = 0
        UART_RX_PIN = 1
        UART_BAUDRATE = 115200
        WEB_PORT = 80
        DEBUG_USB = False
        BMCU_LINKS = None

try:
    import secrets
except ImportError:
    secrets = None


class MonotonicMicros:
    """Extends wrapping MicroPython ticks_us into a boot-scoped monotonic u64."""

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


def json_safe(value):
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, dict):
        return {key: json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [json_safe(item) for item in value]
    return value


bridge_id = getattr(
    config, "BRIDGE_ID", getattr(secrets, "MDNS_HOSTNAME", "pico-bmcu-bridge"))
bambuddy_settings = BambuddyConfig(secrets)
bambuddy_outbox = BambuddyOutbox(bridge_id)
bambuddy_client = None
bambuddy_revision = -1
monotonic_us = MonotonicMicros()
reset_operation_nonce = time.ticks_ms() & 0xffffffff
runtime_log = PicoRuntimeLog(time.ticks_ms)
runtime_log.info("boot", "Pico application started", {
    "reset_cause": machine.reset_cause(),
})


def publish(message):
    # UART callbacks only build and enqueue. Socket I/O runs later in the loop.
    if bambuddy_settings.enabled:
        bambuddy_outbox.publish(
            message, time.ticks_ms(), monotonic_us.now())
    if getattr(config, "DEBUG_USB", False):
        print(json.dumps(json_safe(message)))


def publish_wifi(message):
    if message.get("type") == "wifi_state":
        details = {"state": message.get("state")}
        if message.get("ip"):
            details["ip"] = message["ip"]
        if message.get("error"):
            details["error"] = str(message["error"])[:160]
        runtime_log.info("wifi", "state changed", details)
    publish(message)


def reconcile_bambuddy():
    global bambuddy_client, bambuddy_revision
    if bambuddy_revision == bambuddy_settings.revision:
        return
    if bambuddy_client is not None:
        bambuddy_client.stop()
        bambuddy_client = None
    if bambuddy_settings.enabled:
        bambuddy_client = BambuddyWebSocketClient(
            bambuddy_outbox,
            bambuddy_settings.url,
            bambuddy_settings.token,
            firmware=getattr(config, "PICO_FIRMWARE_VERSION", "alpha.3"),
            capabilities=["telemetry", "multi_link", "bounded_replay"],
            clock_us=monotonic_us.now,
        )
    bambuddy_revision = bambuddy_settings.revision


link_configs = getattr(config, "BMCU_LINKS", None)
if not link_configs:
    link_configs = ({"id": "bmcu-a", "uart": config.UART_ID,
                     "tx": config.UART_TX_PIN, "rx": config.UART_RX_PIN},)

monitors = []
for link in link_configs:
    link_id = link["id"]
    uart = UART(link["uart"], baudrate=link.get("baudrate", config.UART_BAUDRATE),
                bits=8, parity=0, stop=1, tx=Pin(link["tx"]), rx=Pin(link["rx"]))
    monitors.append(BMCUMonitor(uart, publish, link_id=link_id))

monitor_by_id = {monitor.link_id: monitor for monitor in monitors}
wifi = WiFiStation(secrets, publish_wifi)


def device_summary(monitor):
    return {"id": monitor.link_id, "link": monitor.link_state,
            "bmcu_boot_session": monitor.bmcu_boot_session,
            "tick_hz": monitor.tick_hz}


def transport_state():
    if bambuddy_client is not None:
        return bambuddy_client.status()
    return {
        "state": "disabled",
        "last_error": None,
        "queue_depth": len(bambuddy_outbox.queue),
        "dropped_count": bambuddy_outbox.queue.dropped_count,
        "pico_boot_session": bambuddy_outbox.pico_boot_session,
    }


def commissioning_state():
    result = bambuddy_settings.public()
    result["transport"] = transport_state()
    return result


def pico_state(log_limit=None, include_details=True):
    gc.collect()
    result = runtime_log.snapshot(log_limit, include_details)
    result.update({
        "uptime_ms": time.ticks_ms(),
        "heap_free": gc.mem_free(),
        "heap_alloc": gc.mem_alloc(),
        "reset_cause": machine.reset_cause(),
    })
    return result


def web_state():
    # Legacy aggregate kept for the local page; every device also has a scoped API.
    monitor = monitors[0]
    online = not monitor.is_stale(time.ticks_ms())
    return {
        "wifi": {"state": wifi.state, "ip": wifi.ip, "hostname": wifi.hostname},
        "bambuddy": transport_state(),
        "bmcu": {
            "link": "online" if online else "stale",
            "tick_hz": monitor.tick_hz,
            "status": monitor.status,
            "snapshot": monitor.snapshot,
            "channels": monitor.channels,
            "printer_auth": monitor.printer_auth,
            "printer_rx": monitor.printer_rx,
            "printer_tx": monitor.printer_tx,
            "soft_reset": monitor.soft_reset,
            "events": monitor.events,
            "sensors": monitor.sensors,
            "decoder_crc_errors": monitor.decoder.crc_errors,
            "decoder_frame_errors": monitor.decoder.frame_errors,
        },
        "bridge_id": bridge_id,
        "devices": [device_summary(item) for item in monitors],
        "pico": pico_state(8, False),
    }


def device_state(monitor):
    return {
        "bridge_id": bridge_id,
        "link_id": monitor.link_id,
        "link": monitor.link_state,
        "bmcu_boot_session": monitor.bmcu_boot_session,
        "tick_hz": monitor.tick_hz,
        "status": monitor.status,
        "snapshot": monitor.snapshot,
        "channels": monitor.channels,
        "printer_auth": monitor.printer_auth,
        "printer_rx": monitor.printer_rx,
        "printer_tx": monitor.printer_tx,
        "soft_reset": monitor.soft_reset,
        "sensors": monitor.sensors,
        "decoder_crc_errors": monitor.decoder.crc_errors,
        "decoder_frame_errors": monitor.decoder.frame_errors,
    }


def api_state(path="/api/status"):
    if path == "/api/status":
        return web_state()
    if path == "/api/devices":
        return {"bridge_id": bridge_id,
                "devices": [device_summary(item) for item in monitors]}
    if path == "/api/pico/logs":
        return pico_state(12, True)
    pieces = path.split("/")
    if len(pieces) == 5 and pieces[:3] == ["", "api", "devices"]:
        monitor = monitor_by_id.get(pieces[3])
        if monitor is None:
            return None
        if pieces[4] == "status":
            return device_state(monitor)
        if pieces[4] == "events":
            return {"bridge_id": bridge_id,
                    "link_id": monitor.link_id, "events": monitor.events}
    return None


def local_soft_reset(path, request):
    global reset_operation_nonce
    pieces = path.split("/")
    if len(pieces) != 5 or pieces[:3] != ["", "api", "devices"]:
        raise ValueError("invalid device path")
    monitor = monitor_by_id.get(pieces[3])
    if monitor is None:
        raise ValueError("unknown BMCU link")
    expected_csrf = bambuddy_settings.public()["csrf"]
    if request.get("csrf") != expected_csrf:
        raise ValueError("invalid CSRF token")
    if request.get("confirm") != "RESET BMCU":
        raise ValueError("explicit confirmation is required")
    guard_error = monitor.soft_reset_guard_error()
    if guard_error is not None:
        raise ValueError(guard_error)

    reason = int(request.get("reason", 0))
    ttl_ms = int(request.get("ttl_ms", 5000))
    reset_operation_nonce = (reset_operation_nonce + 1) & 0xffffffff
    if reset_operation_nonce == 0:
        reset_operation_nonce = 1
    sequence = monitor.request_soft_reset(reset_operation_nonce, reason, ttl_ms)
    return {
        "link_id": monitor.link_id, "operation_id": reset_operation_nonce,
        "sequence": sequence, "state": "requested",
    }


web = WebUI(
    api_state,
    getattr(config, "WEB_PORT", 80),
    config_provider=commissioning_state,
    config_updater=bambuddy_settings.update,
    command_updater=local_soft_reset,
    error_handler=lambda component, error: runtime_log.exception(
        "web." + component, error),
)
now = time.ticks_ms()
wifi.start(now)
web.start()
reconcile_bambuddy()


def recover_web():
    web._close_client()


last_transport_state = None
last_transport_log_ms = None
last_transport_error = None


def record_exception(component, error):
    try:
        runtime_log.exception(component, error)
    except Exception:
        pass


def service_once(now_ms):
    global last_transport_state, last_transport_log_ms, last_transport_error
    # Keep BMCU UART service ahead of Wi-Fi, WebSocket, and HTTP work.
    for monitor in monitors:
        try:
            monitor.poll(now_ms)
        except Exception as error:
            record_exception("bmcu." + monitor.link_id + ".poll", error)
    try:
        wifi.poll(now_ms)
    except Exception as error:
        record_exception("wifi.poll", error)
    try:
        reconcile_bambuddy()
    except Exception as error:
        record_exception("bambuddy.reconcile", error)
    if bambuddy_client is not None:
        try:
            bambuddy_client.poll(now_ms, wifi.state == "online")
        except Exception as error:
            record_exception("bambuddy.poll", error)
        current_transport_state = bambuddy_client.state
        if current_transport_state != last_transport_state:
            current_error = bambuddy_client.last_error
            elapsed = (None if last_transport_log_ms is None else
                       time.ticks_diff(now_ms, last_transport_log_ms))
            if (last_transport_log_ms is None or
                    current_error != last_transport_error or
                    elapsed < 0 or elapsed >= 10000):
                runtime_log.info("bambuddy", "state changed", {
                    "state": current_transport_state,
                    "error": current_error,
                })
                last_transport_log_ms = now_ms
                last_transport_error = current_error
            last_transport_state = current_transport_state
    try:
        web.poll()
    except Exception as error:
        record_exception("web.poll", error)
        try:
            recover_web()
        except Exception as recovery_error:
            record_exception("web.poll.recovery", recovery_error)
    for monitor in monitors:
        try:
            monitor.ping_if_idle(now_ms)
        except Exception as error:
            record_exception("bmcu." + monitor.link_id + ".ping", error)


while True:
    now = time.ticks_ms()
    try:
        service_once(now)
    except Exception as error:
        record_exception("main.loop", error)
    time.sleep_ms(1)
