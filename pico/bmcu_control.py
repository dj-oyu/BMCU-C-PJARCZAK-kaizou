"""Authenticated Bambuddy CONTROL path (issue #6).

Implements MAC input byte contract v1 (issue #2): fixed-order, length-prefixed
fields, HMAC-SHA256 with a device-scoped control key that is independent from
the telemetry token. Every check fails closed; the BMCU stays the final safety
authority for any accepted command.
"""

try:
    import ujson as json
except ImportError:
    import json
try:
    import uhashlib as hashlib
except ImportError:
    import hashlib
try:
    import ubinascii as binascii
except ImportError:
    import binascii
try:
    import uos as os
except ImportError:
    import os


CTX_MESSAGE = b"BMCU-CTRL-v1"
CTX_RESULT = b"BMCU-CTRL-RES-v1"
FIELD_LIMIT = 1024
MESSAGE_TTL_MIN_MS = 1
MESSAGE_TTL_MAX_MS = 60000
SOFT_RESET_TTL_MAX_MS = 5000
OPERATION_WINDOW = 32
RESULT_QUEUE_LIMIT = 8


def hmac_sha256(key, message):
    """RFC 2104 HMAC over SHA-256 (MicroPython has no hmac module)."""
    if len(key) > 64:
        key = hashlib.sha256(key).digest()
    key = key + b"\x00" * (64 - len(key))
    inner = hashlib.sha256(bytes(k ^ 0x36 for k in key))
    inner.update(message)
    outer = hashlib.sha256(bytes(k ^ 0x5C for k in key))
    outer.update(inner.digest())
    return outer.digest()


def _lp(value):
    data = value.encode() if isinstance(value, str) else bytes(value)
    if len(data) > FIELD_LIMIT:
        raise ValueError("field too long")
    return len(data).to_bytes(2, "big") + data


def mac_input(version, device_id, link_id, pico_boot_session, session_nonce,
              control_sequence, operation_id, command, ttl_ms, payload_b64):
    return (CTX_MESSAGE + bytes((version,)) + _lp(device_id) + _lp(link_id) +
            _lp(pico_boot_session) + _lp(session_nonce) +
            control_sequence.to_bytes(8, "big") + _lp(operation_id) +
            _lp(command) + ttl_ms.to_bytes(4, "big") + _lp(payload_b64))


def result_input(version, device_id, link_id, pico_boot_session, session_nonce,
                 control_sequence, operation_id, status, result_payload_b64):
    return (CTX_RESULT + bytes((version,)) + _lp(device_id) + _lp(link_id) +
            _lp(pico_boot_session) + _lp(session_nonce) +
            control_sequence.to_bytes(8, "big") + _lp(operation_id) +
            _lp(status) + _lp(result_payload_b64))


def _constant_time_equal(left, right):
    if len(left) != len(right):
        return False
    result = 0
    for index in range(len(left)):
        result |= left[index] ^ right[index]
    return result == 0


def _decode_payload(payload_b64):
    if payload_b64 == "":
        return {}
    raw = binascii.a2b_base64(payload_b64)
    value = json.loads(raw.decode())
    if not isinstance(value, dict):
        raise ValueError("payload must be a JSON object")
    return value


class ControlGateway:
    """Verify CONTROL messages and emit signed results.

    ``executor(link_id, command, payload)`` performs the accepted command and
    returns a detail dict; it raises ``ValueError(reason)`` on refusal so the
    refusal reaches the operator as a signed rejection.
    """

    def __init__(self, device_id, pico_boot_session, key_hex, enabled,
                 link_ids, executor, random_bytes=None):
        self.device_id = device_id
        self.pico_boot_session = pico_boot_session
        self.enabled = bool(enabled)
        self.link_ids = tuple(link_ids)
        self.executor = executor
        self.random_bytes = random_bytes or os.urandom
        self.key = None
        if isinstance(key_hex, str) and len(key_hex) == 64:
            try:
                self.key = binascii.unhexlify(key_hex)
            except ValueError:
                self.key = None
        self.session_nonce = ""
        self.last_sequence = -1
        self._operations = []
        self.results = []
        self.rejected_unauthenticated = 0
        self.last_error = None
        self._pending = {}

    def new_session(self):
        """Per-WS-session replay window; announced to Bambuddy in HELLO."""
        self.session_nonce = self.random_bytes(16).hex()
        self.last_sequence = -1
        self._operations = []
        self.results = []
        self._pending = {}
        return self.session_nonce

    def _sign_result(self, message, status, payload):
        result_payload_b64 = ""
        if payload:
            result_payload_b64 = binascii.b2a_base64(
                json.dumps(payload).encode()).strip().decode()
        mac = hmac_sha256(self.key, result_input(
            1, self.device_id, message["link_id"], self.pico_boot_session,
            message["session_nonce"], message["control_sequence"],
            message["operation_id"], status, result_payload_b64))
        return {
            "type": "control_result", "version": 1,
            "device_id": self.device_id, "link_id": message["link_id"],
            "pico_boot_session": self.pico_boot_session,
            "session_nonce": message["session_nonce"],
            "control_sequence": message["control_sequence"],
            "operation_id": message["operation_id"], "status": status,
            "result_payload_b64": result_payload_b64,
            "mac": binascii.hexlify(mac).decode(),
        }

    def _queue_result(self, result):
        self.results.append(result)
        if len(self.results) > RESULT_QUEUE_LIMIT:
            self.results.pop(0)

    def _reject(self, message, reason):
        self._queue_result(self._sign_result(message, "rejected",
                                             {"reason": reason}))

    @staticmethod
    def _structural(message):
        for name in ("device_id", "link_id", "pico_boot_session",
                     "session_nonce", "operation_id", "command",
                     "payload_b64", "mac"):
            value = message.get(name)
            if not isinstance(value, str) or len(value) > FIELD_LIMIT:
                return False
        if message.get("version") != 1:
            return False
        sequence = message.get("control_sequence")
        if not isinstance(sequence, int) or not 0 <= sequence < (1 << 64):
            return False
        ttl = message.get("ttl_ms")
        if not isinstance(ttl, int) or not MESSAGE_TTL_MIN_MS <= ttl <= MESSAGE_TTL_MAX_MS:
            return False
        if not message["operation_id"]:
            return False
        return True

    def handle(self, message):
        """Process one CONTROL message; queues at most one signed result.

        Structural or MAC failures never produce a signed response: an
        unauthenticated peer learns nothing but a counter increment.
        """
        if self.key is None or not self.enabled:
            self.rejected_unauthenticated += 1
            self.last_error = "control disabled"
            return
        if not self._structural(message):
            self.rejected_unauthenticated += 1
            self.last_error = "malformed control message"
            return
        try:
            expected = hmac_sha256(self.key, mac_input(
                1, message["device_id"], message["link_id"],
                message["pico_boot_session"], message["session_nonce"],
                message["control_sequence"], message["operation_id"],
                message["command"], message["ttl_ms"], message["payload_b64"]))
        except ValueError:
            self.rejected_unauthenticated += 1
            self.last_error = "malformed control message"
            return
        try:
            received = binascii.unhexlify(message["mac"].lower())
        except ValueError:
            received = b""
        if not _constant_time_equal(expected, received):
            self.rejected_unauthenticated += 1
            self.last_error = "control MAC mismatch"
            return

        if message["device_id"] != self.device_id:
            return self._reject(message, "device_mismatch")
        if message["pico_boot_session"] != self.pico_boot_session:
            return self._reject(message, "boot_session_mismatch")
        if (not self.session_nonce or
                message["session_nonce"] != self.session_nonce):
            return self._reject(message, "session_mismatch")
        if message["control_sequence"] <= self.last_sequence:
            return self._reject(message, "sequence_replay")
        self.last_sequence = message["control_sequence"]
        if message["operation_id"] in self._operations:
            return self._reject(message, "operation_replay")
        self._operations.append(message["operation_id"])
        if len(self._operations) > OPERATION_WINDOW:
            self._operations.pop(0)
        if message["link_id"] not in self.link_ids:
            return self._reject(message, "unknown_link")
        if message["command"] != "soft_reset":
            return self._reject(message, "command_disabled")
        try:
            payload = _decode_payload(message["payload_b64"])
        except (ValueError, TypeError):
            return self._reject(message, "payload_invalid")
        ttl = payload.get("ttl_ms", message["ttl_ms"])
        if (not isinstance(ttl, int) or
                not MESSAGE_TTL_MIN_MS <= ttl <= SOFT_RESET_TTL_MAX_MS):
            return self._reject(message, "ttl_invalid")
        try:
            detail = self.executor(message["link_id"], message["command"],
                                   {"reason": payload.get("reason", 0),
                                    "ttl_ms": ttl})
        except ValueError as exc:
            return self._reject(message, str(exc))
        self._pending[message["operation_id"]] = {
            "message": {key: message[key] for key in
                        ("link_id", "session_nonce", "control_sequence",
                         "operation_id")},
            "reported": "scheduled",
        }
        self._queue_result(self._sign_result(message, "scheduled",
                                             detail or {}))

    def report_transition(self, operation_id, status, payload=None):
        """Emit a follow-up signed result (cancelled/rebooted/completed)."""
        pending = self._pending.get(operation_id)
        if pending is None or pending["reported"] == status:
            return
        pending["reported"] = status
        self._queue_result(self._sign_result(pending["message"], status,
                                             payload or {}))
        if status in ("cancelled", "completed"):
            del self._pending[operation_id]

    def pending_links(self):
        return [(operation_id, item["message"]["link_id"])
                for operation_id, item in self._pending.items()]

    def status(self):
        return {
            "enabled": self.enabled,
            "key_set": self.key is not None,
            "session_active": bool(self.session_nonce),
            "pending": len(self._pending),
            "rejected_unauthenticated": self.rejected_unauthenticated,
            "last_error": self.last_error,
        }

    def take_results(self):
        results, self.results = self.results, []
        return results
