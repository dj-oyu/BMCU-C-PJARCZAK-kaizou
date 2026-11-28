"""Long-reply table requests from a PC or an agent, gated by a shared token.

docs/BMCU_MONITOR_STREAM_V2.md section 7.3. The Pico forwards the request to
the BMCU as SET_LONG_REPLY and reports the BMCU's ACK; it does not decide what
the printer should be answered. The token lives in secrets.py as
``BMCU_CONTROL_TOKEN`` and never appears in a response, a log or a datagram.
Without a token of at least TOKEN_MIN_LENGTH characters the endpoint is off.

    POST /api/control/long-reply  {"link": 0, "type": "0x0411",
                                   "match": "01", "reply": "01000000"}
    POST /api/control/long-reply  {"link": 0, "type": "0x0411", "clear": true}
    GET  /api/control/long-reply?link=0

Every request needs ``X-BMCU-Control-Token``. POST also needs
``X-BMCU-Control-Action: long-reply`` and ``Content-Type: application/json``,
the same cross-site guard as the other control routes.
"""

try:
    import ujson as json
except ImportError:
    import json

PATH = "/api/control/long-reply"
TOKEN_MIN_LENGTH = 16
FAILURES_BEFORE_LOCKOUT = 5
LOCKOUT_MS = 60000
# An ACK normally arrives within one link round trip, tens of milliseconds.
ACK_TIMEOUT_MS = 2000

ACK_NAMES = {0: "ok", 1: "bad_value", 2: "unsupported", 3: "busy",
             4: "bad_state", 5: "denied", 6: "expired", 7: "duplicate",
             8: "internal"}


def _json(status, value):
    return status, "application/json", json.dumps(value).encode()


def _same(left, right):
    """Compare without stopping at the first differing byte."""
    if not isinstance(left, str) or len(left) != len(right):
        return False
    difference = 0
    for a, b in zip(left, right):
        difference |= ord(a) ^ ord(b)
    return difference == 0


def _hex(value, field):
    if value is None:
        return b""
    if not isinstance(value, str):
        raise ValueError(field + " must be a hex string")
    text = value.replace(" ", "")
    if len(text) % 2:
        raise ValueError(field + " must have an even number of hex digits")
    try:
        return bytes(int(text[i:i + 2], 16) for i in range(0, len(text), 2))
    except ValueError:
        raise ValueError(field + " must be hex")


def _type(value):
    if isinstance(value, bool):
        raise ValueError("type required")
    if isinstance(value, str):
        value = int(value, 16) if value.lower().startswith("0x") else int(value)
    if not isinstance(value, int) or not 0 <= value <= 0xFFFF:
        raise ValueError("type must be a u16")
    return value


def _query_link(path):
    if "?" not in path:
        return None
    for part in path.split("?", 1)[1].split("&"):
        if part.startswith("link="):
            return int(part[5:])
    return None


class LongReplyAPI:
    def __init__(self, monitors, link_names, token, clock_ms,
                 ticks_diff=None, on_event=None):
        # monitors[i].request_long_reply(...) and .long_reply, per link.
        self.monitors = monitors
        self.link_names = tuple(link_names)
        self.token = token if isinstance(token, str) and \
            len(token) >= TOKEN_MIN_LENGTH else None
        self.clock_ms = clock_ms
        self.ticks_diff = ticks_diff or (lambda left, right: left - right)
        # (message) -> None, for the runtime log. Never given the token.
        self.on_event = on_event or (lambda _message: None)
        self.failures = 0
        self.locked_since_ms = None

    # -- token -----------------------------------------------------------

    def _check_token(self, headers, now_ms):
        """None when the token matches, else the response to send."""
        if self.token is None:
            return _json("503 Service Unavailable",
                         {"error": "disabled: no BMCU_CONTROL_TOKEN"})
        if self.locked_since_ms is not None:
            if self.ticks_diff(now_ms, self.locked_since_ms) < LOCKOUT_MS:
                return _json("429 Too Many Requests",
                             {"error": "locked after wrong tokens"})
            self.locked_since_ms = None
            self.failures = 0
        if _same(headers.get("x-bmcu-control-token"), self.token):
            self.failures = 0
            return None
        self.failures += 1
        if self.failures >= FAILURES_BEFORE_LOCKOUT:
            self.locked_since_ms = now_ms
            self.on_event("long-reply locked after %d wrong tokens"
                          % self.failures)
        return _json("403 Forbidden", {"error": "bad token"})

    # -- HTTP ------------------------------------------------------------

    def handle(self, method, path, headers, body):
        if path.split("?", 1)[0] != PATH:
            return None
        if method not in ("GET", "POST"):
            return "405 Method Not Allowed", "text/plain", b"GET or POST\n"
        if method == "POST" and (
                headers.get("content-type") != "application/json" or
                headers.get("x-bmcu-control-action") != "long-reply"):
            return "403 Forbidden", "text/plain", b"protected request\n"
        now_ms = self.clock_ms()
        refused = self._check_token(headers, now_ms)
        if refused is not None:
            return refused
        if method == "GET":
            return self._status(path, now_ms)
        return self._set(body, now_ms)

    def _link(self, link):
        if not isinstance(link, int) or isinstance(link, bool) or \
                not 0 <= link < len(self.monitors):
            raise ValueError("unknown link")
        return link

    def _set(self, body, now_ms):
        try:
            value = json.loads(body)
            if not isinstance(value, dict):
                raise ValueError("expected a JSON object")
            link = self._link(value.get("link"))
            frame_type = _type(value.get("type"))
            clear = value.get("clear") is True
            match = _hex(value.get("match"), "match")
            reply = _hex(value.get("reply"), "reply")
            sequence = self.monitors[link].request_long_reply(
                frame_type, match, reply, clear, now_ms)
        except (ValueError, TypeError) as error:
            return _json("400 Bad Request", {"error": str(error)})
        self.on_event("long-reply %s for %s type 0x%04x (%d B)" % (
            "clear" if clear else "set", self.link_names[link], frame_type,
            len(reply)))
        return _json("202 Accepted", {"link": link, "sequence": sequence,
                                      "state": "sent"})

    def _status(self, path, now_ms):
        try:
            link = self._link(_query_link(path))
        except (ValueError, TypeError):
            return _json("400 Bad Request", {"error": "link required"})
        request = self.monitors[link].long_reply
        if request is None:
            return _json("200 OK", {"link": link, "state": "none"})
        age = self.ticks_diff(now_ms, request["sent_ms"])
        state = request["state"]
        if state == "sent" and age >= ACK_TIMEOUT_MS:
            state = "no_ack"
        result = request["ack_result"]
        return _json("200 OK", {
            "link": link, "sequence": request["sequence"],
            "type": "0x%04x" % request["type"], "clear": request["clear"],
            "match_len": request["match_len"],
            "reply_len": request["reply_len"], "state": state,
            "ack_result": result,
            "ack": None if result is None else ACK_NAMES.get(result, "unknown"),
            "age_ms": age})
