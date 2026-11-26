"""BMCU soft reset from the Pico's own web UI, confirmed by a code on the OLED.

docs/BMCU_MONITOR_STREAM_V2.md section 7. Whoever can read the panel is
physically at the machine and is trusted to reset it; nobody else can, because
the code never leaves the panel. No panel, no reset.

    POST /api/control/reset-request  {"link": 1}
    POST /api/control/reset          {"link": 1, "code": "4827"}

Both need the header ``X-BMCU-Control-Action`` (``request`` / ``reset``) and
``Content-Type: application/json``, so that a cross-site form post cannot reach
them. That is the same same-origin guard as the transport settings API.
"""

try:
    import ujson as json
except ImportError:
    import json
try:
    import uos as os
except ImportError:
    import os

CODE_TTL_MS = 60000
ATTEMPTS = 3
RESULT_SHOW_MS = 3000
REQUEST_GAP_MS = 5000

REQUEST_PATH = "/api/control/reset-request"
RESET_PATH = "/api/control/reset"


def _json(status, value):
    return status, "application/json", json.dumps(value).encode()


def draw_code(urandom=os.urandom):
    """Uniform 0000-9999. Rejection sampling: 65536 is not a multiple of 10000."""
    while True:
        value = urandom(2)
        number = (value[0] << 8) | value[1]
        if number < 60000:
            return "%04d" % (number % 10000)


class LocalReset:
    def __init__(self, control, link_names, panel_ready, clock_ms,
                 urandom=None, ticks_diff=None, on_event=None):
        # control(link_index) -> result bytes; raises ValueError when the
        # existing soft-reset guards refuse.
        self.control = control
        self.link_names = tuple(link_names)
        # () -> True while the OLED is initialised and answering.
        self.panel_ready = panel_ready
        self.clock_ms = clock_ms
        self.urandom = urandom or os.urandom
        self.ticks_diff = ticks_diff or (lambda left, right: left - right)
        # (message) -> None, for the runtime log. Never given the code.
        self.on_event = on_event or (lambda _message: None)
        self.pending_link = None
        self.pending_code = None
        self.pending_since_ms = None
        self.attempts_left = 0
        self.last_request_ms = None
        # (text, since_ms) shown for RESULT_SHOW_MS after an outcome.
        self.result = None

    # -- state -----------------------------------------------------------

    def _clear(self):
        self.pending_link = None
        self.pending_code = None
        self.pending_since_ms = None
        self.attempts_left = 0

    def _finish(self, text, now_ms):
        self._clear()
        self.result = (text, now_ms)

    def _expire(self, now_ms):
        if self.pending_code is not None and self.ticks_diff(
                now_ms, self.pending_since_ms) >= CODE_TTL_MS:
            self.on_event("reset code expired")
            self._finish("EXPIRED", now_ms)

    # -- panel -----------------------------------------------------------

    def overlay_lines(self, now_ms):
        """Rows for the OLED while a code or a result is up; None otherwise."""
        self._expire(now_ms)
        if self.pending_code is not None:
            left = (CODE_TTL_MS - self.ticks_diff(
                now_ms, self.pending_since_ms) + 999) // 1000
            code = self.pending_code
            return ("RESET " + self.link_names[self.pending_link],
                    "",
                    "  CODE " + " ".join(code),
                    "",
                    "enter it in the",
                    "Pico web UI",
                    "%2ds  tries %d" % (left, self.attempts_left))
        if self.result is not None:
            text, since = self.result
            if self.ticks_diff(now_ms, since) < RESULT_SHOW_MS:
                return ("RESET", "", text)
            self.result = None
        return None

    # -- HTTP ------------------------------------------------------------

    def handle(self, method, path, headers, body):
        if path not in (REQUEST_PATH, RESET_PATH):
            return None
        if method != "POST":
            return "405 Method Not Allowed", "text/plain", b"POST only\n"
        action = "request" if path == REQUEST_PATH else "reset"
        if headers.get("content-type") != "application/json" or \
                headers.get("x-bmcu-control-action") != action:
            return "403 Forbidden", "text/plain", b"protected request\n"
        try:
            value = json.loads(body)
            link = int(value["link"])
        except (ValueError, KeyError, TypeError):
            return _json("400 Bad Request", {"error": "link required"})
        if not 0 <= link < len(self.link_names):
            return _json("400 Bad Request", {"error": "unknown link"})
        now_ms = self.clock_ms()
        if action == "request":
            return self._request(link, now_ms)
        return self._reset(link, value.get("code"), now_ms)

    def _request(self, link, now_ms):
        if not self.panel_ready():
            return _json("503 Service Unavailable",
                         {"error": "no display: reset needs the panel"})
        if self.last_request_ms is not None and self.ticks_diff(
                now_ms, self.last_request_ms) < REQUEST_GAP_MS:
            return _json("429 Too Many Requests", {"error": "wait"})
        self.last_request_ms = now_ms
        self.pending_link = link
        self.pending_code = draw_code(self.urandom)
        self.pending_since_ms = now_ms
        self.attempts_left = ATTEMPTS
        self.result = None
        self.on_event("reset code shown for %s" % self.link_names[link])
        return _json("202 Accepted", {"expires_in_s": CODE_TTL_MS // 1000,
                                      "attempts": ATTEMPTS})

    def _reset(self, link, code, now_ms):
        self._expire(now_ms)
        if self.pending_code is None:
            if self.result is not None and self.result[0] == "EXPIRED":
                return _json("410 Gone", {"error": "code expired"})
            return _json("409 Conflict", {"error": "no pending code"})
        if link != self.pending_link or not isinstance(code, str) or \
                code != self.pending_code:
            self.attempts_left -= 1
            if self.attempts_left <= 0:
                self.on_event("reset code invalidated after wrong attempts")
                self._finish("CANCELLED", now_ms)
                return _json("410 Gone", {"error": "too many wrong codes"})
            return _json("403 Forbidden",
                         {"attempts_left": self.attempts_left})
        # Single use, whatever the handler answers.
        self._clear()
        try:
            returned = self.control(link)
        except ValueError as error:
            self.on_event("reset refused: %s" % error)
            self.result = ("FAILED", now_ms)
            return _json("409 Conflict", {"error": str(error)})
        self.on_event("reset accepted for %s" % self.link_names[link])
        self.result = ("OK", now_ms)
        text = returned.decode() if isinstance(returned, bytes) else \
            str(returned or "accepted")
        return _json("200 OK", {"result": text})
