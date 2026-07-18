"""Non-blocking Wi-Fi station helper for MicroPython on Pico W/Pico 2 W."""

try:
    import utime as time
except ImportError:
    import time
import network


class WiFiStation:
    """Maintains a station connection without waiting in the UART hot path."""

    def __init__(self, secrets, on_state=None, retry_ms=10000):
        self.on_state = on_state
        self.retry_ms = retry_ms
        self.wlan = network.WLAN(network.STA_IF)
        self.ssid = getattr(secrets, "WIFI_SSID", "") if secrets else ""
        self.password = getattr(secrets, "WIFI_PASSWORD", "") if secrets else ""
        self.enabled = bool(self.ssid)
        self.last_attempt_ms = None
        self.reported_connected = None
        self.state = "unconfigured" if not self.enabled else "offline"
        self.ip = None

    def _emit(self, state, **extra):
        self.state = state
        self.ip = extra.get("ip", self.ip if state == "online" else None)
        if self.on_state:
            message = {"type": "wifi_state", "state": state}
            message.update(extra)
            self.on_state(message)

    def start(self, now_ms):
        if not self.enabled:
            self._emit("unconfigured")
            return
        self.wlan.active(True)
        self._connect(now_ms)

    def _connect(self, now_ms):
        self.last_attempt_ms = now_ms
        try:
            if self.wlan.isconnected():
                self.wlan.disconnect()
            self.wlan.connect(self.ssid, self.password)
            self._emit("connecting", ssid=self.ssid)
        except OSError as error:
            self._emit("error", error=str(error))

    def poll(self, now_ms):
        if not self.enabled:
            return
        connected = self.wlan.isconnected()
        if connected:
            if self.reported_connected is not True:
                self.reported_connected = True
                self._emit("online", ip=self.wlan.ifconfig()[0])
            return
        if self.reported_connected is True:
            self._emit("offline")
        self.reported_connected = False
        if self.last_attempt_ms is None:
            self._connect(now_ms)
            return
        elapsed = (time.ticks_diff(now_ms, self.last_attempt_ms)
                   if hasattr(time, "ticks_diff") else now_ms - self.last_attempt_ms)
        if elapsed >= self.retry_ms:
            self._connect(now_ms)
