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


uart = UART(config.UART_ID, baudrate=config.UART_BAUDRATE, bits=8, parity=0, stop=1,
            tx=Pin(config.UART_TX_PIN), rx=Pin(config.UART_RX_PIN))
monitor = BMCUMonitor(uart, publish)
wifi = WiFiStation(secrets, publish)


def web_state():
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
    }


web = WebUI(web_state, getattr(config, "WEB_PORT", 80))
now = time.ticks_ms()
wifi.start(now)
web.start()
last_ping = now
was_stale = None
last_full_status = now

while True:
    now = time.ticks_ms()
    # Keep BMCU UART service ahead of Wi-Fi and HTTP work every iteration.
    monitor.poll(now)
    wifi.poll(now)
    web.poll()
    stale = monitor.is_stale(now)
    if stale != was_stale:
        publish({"type": "link_state", "state": "stale" if stale else "online"})
        was_stale = stale
    if time.ticks_diff(now, last_ping) >= 2000:
        monitor.ping(now)
        last_ping = now
    if (not stale and monitor._snapshot_parts is None and
            time.ticks_diff(now, last_full_status) >= 1000):
        monitor.get_full_status()
        last_full_status = now
    time.sleep_ms(1)
