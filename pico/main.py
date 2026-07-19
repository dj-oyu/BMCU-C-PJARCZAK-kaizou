"""Pico 2 W entry point for the BMCU H1 monitor link."""

import time
try:
    import ujson as json
except ImportError:
    import json
from machine import UART, Pin

from bmcu_link import BMCUMonitor
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


def json_safe(value):
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, dict):
        return {key: json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [json_safe(item) for item in value]
    return value


def publish(message):
    # Bambuddy integration belongs here after its authenticated API is specified.
    # USB output is opt-in because console I/O must not delay UART RX.
    if getattr(config, "DEBUG_USB", False):
        print(json.dumps(json_safe(message)))


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
wifi = WiFiStation(secrets, publish)


def web_state():
    # Legacy aggregate kept for the local page; every device also has a scoped API.
    monitor = monitors[0]
    online = not monitor.is_stale(time.ticks_ms())
    return {
        "wifi": {"state": wifi.state, "ip": wifi.ip, "hostname": wifi.hostname},
        "bmcu": {
            "link": "online" if online else "stale",
            "tick_hz": monitor.tick_hz,
            "status": monitor.status,
            "snapshot": monitor.snapshot,
            "channels": monitor.channels,
            "events": monitor.events,
            "sensors": monitor.sensors,
            "decoder_crc_errors": monitor.decoder.crc_errors,
            "decoder_frame_errors": monitor.decoder.frame_errors,
        },
        "bridge_id": getattr(config, "BRIDGE_ID", wifi.hostname),
        "devices": [device_summary(item) for item in monitors],
    }


def device_summary(monitor):
    return {"id": monitor.link_id, "link": monitor.link_state,
            "bmcu_boot_session": monitor.bmcu_boot_session,
            "tick_hz": monitor.tick_hz}


def device_state(monitor):
    return {
        "bridge_id": getattr(config, "BRIDGE_ID", wifi.hostname),
        "link_id": monitor.link_id,
        "link": monitor.link_state,
        "bmcu_boot_session": monitor.bmcu_boot_session,
        "tick_hz": monitor.tick_hz,
        "status": monitor.status,
        "snapshot": monitor.snapshot,
        "channels": monitor.channels,
        "sensors": monitor.sensors,
        "decoder_crc_errors": monitor.decoder.crc_errors,
        "decoder_frame_errors": monitor.decoder.frame_errors,
    }


def api_state(path="/api/status"):
    if path == "/api/status":
        return web_state()
    if path == "/api/devices":
        return {"bridge_id": getattr(config, "BRIDGE_ID", wifi.hostname),
                "devices": [device_summary(item) for item in monitors]}
    pieces = path.split("/")
    if len(pieces) == 5 and pieces[:3] == ["", "api", "devices"]:
        monitor = monitor_by_id.get(pieces[3])
        if monitor is None:
            return None
        if pieces[4] == "status":
            return device_state(monitor)
        if pieces[4] == "events":
            return {"bridge_id": getattr(config, "BRIDGE_ID", wifi.hostname),
                    "link_id": monitor.link_id, "events": monitor.events}
    return None


web = WebUI(api_state, getattr(config, "WEB_PORT", 80))
now = time.ticks_ms()
wifi.start(now)
web.start()

while True:
    now = time.ticks_ms()
    # Keep BMCU UART service ahead of Wi-Fi and HTTP work every iteration.
    for monitor in monitors:
        monitor.poll(now)
    wifi.poll(now)
    web.poll()
    for monitor in monitors:
        monitor.ping_if_idle(now)
    time.sleep_ms(1)
