"""Cooperative Bambuddy WebSocket sender for MicroPython and host tests."""

try:
    import ujson as json
except ImportError:
    import json
try:
    import usocket as socket
except ImportError:
    import socket
try:
    import ubinascii as binascii
except ImportError:
    import binascii
try:
    import uhashlib as hashlib
except ImportError:
    import hashlib
try:
    import uos as os
except ImportError:
    import os


_GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
_WOULD_BLOCK = (11, 35, 57, 107, 115, 10035, 10057)


def ticks_diff(now, then):
    try:
        import utime as time
    except ImportError:
        import time
    return time.ticks_diff(now, then) if hasattr(time, "ticks_diff") else now - then


def ticks_add(value, delta):
    try:
        import utime as time
    except ImportError:
        import time
    return time.ticks_add(value, delta) if hasattr(time, "ticks_add") else value + delta


def parse_ws_url(url):
    if not isinstance(url, str) or "://" not in url:
        raise ValueError("invalid WebSocket URL")
    if "\r" in url or "\n" in url:
        raise ValueError("invalid characters in WebSocket URL")
    scheme, rest = url.split("://", 1)
    if scheme != "ws":
        raise ValueError("only trusted-LAN ws:// is currently supported")
    authority, slash, path = rest.partition("/")
    if not authority:
        raise ValueError("WebSocket host is required")
    if "@" in authority:
        raise ValueError("userinfo is not allowed")
    host = authority
    port = 80
    if authority.startswith("["):
        end = authority.find("]")
        if end < 0:
            raise ValueError("invalid IPv6 authority")
        host = authority[1:end]
        if len(authority) > end + 1:
            if authority[end + 1] != ":":
                raise ValueError("invalid IPv6 port")
            port = int(authority[end + 2:])
    elif ":" in authority:
        host, port_text = authority.rsplit(":", 1)
        port = int(port_text)
    if not host or not 0 < port < 65536:
        raise ValueError("invalid WebSocket endpoint")
    return {"scheme": scheme, "host": host, "port": port,
            "path": "/" + path if slash else "/"}


def _b64(data):
    encoded = binascii.b2a_base64(data)
    if isinstance(encoded, bytes):
        encoded = encoded.strip().decode()
    return encoded.strip()


def _quote(value):
    safe = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"
    result = []
    for value_byte in value.encode():
        if value_byte in safe:
            result.append(chr(value_byte))
        else:
            result.append("%%%02X" % value_byte)
    return "".join(result)


def _sha1(data):
    digest = hashlib.sha1(data)
    return digest.digest()


def websocket_accept(key):
    return _b64(_sha1(key.encode() + _GUID))


def client_frame(payload, opcode=1, mask=None):
    if isinstance(payload, str):
        payload = payload.encode()
    mask = mask or os.urandom(4)
    length = len(payload)
    if length < 126:
        header = bytes((0x80 | opcode, 0x80 | length))
    elif length < 65536:
        header = bytes((0x80 | opcode, 0xFE, (length >> 8) & 0xFF, length & 0xFF))
    else:
        header = bytes((0x80 | opcode, 0xFF)) + length.to_bytes(8, "big")
    masked = bytes(value ^ mask[index & 3] for index, value in enumerate(payload))
    return header + mask + masked


class ServerFrameParser:
    """Incrementally parses unmasked server frames with a strict memory bound."""

    def __init__(self, max_payload=16384):
        self.buffer = bytearray()
        self.max_payload = max_payload

    def feed(self, data):
        self.buffer.extend(data)
        frames = []
        while True:
            if len(self.buffer) < 2:
                break
            first, second = self.buffer[0], self.buffer[1]
            if not first & 0x80:
                raise ValueError("fragmented frames are not supported")
            if second & 0x80:
                raise ValueError("server frame must not be masked")
            length = second & 0x7F
            offset = 2
            if length == 126:
                if len(self.buffer) < 4:
                    break
                length = (self.buffer[2] << 8) | self.buffer[3]
                offset = 4
            elif length == 127:
                if len(self.buffer) < 10:
                    break
                length = int.from_bytes(self.buffer[2:10], "big")
                offset = 10
            if length > self.max_payload:
                raise ValueError("WebSocket payload too large")
            if len(self.buffer) < offset + length:
                break
            payload = bytes(self.buffer[offset:offset + length])
            self.buffer = bytearray(self.buffer[offset + length:])
            frames.append((first & 0x0F, payload))
        if len(self.buffer) > self.max_payload + 10:
            raise ValueError("WebSocket receive buffer too large")
        return frames


def _errno(exc):
    return getattr(exc, "errno", exc.args[0] if exc.args else None)


def _would_block(exc):
    return _errno(exc) in _WOULD_BLOCK


class BambuddyWebSocketClient:
    """One-in-flight-batch WebSocket transport with bounded cooperative work."""

    BACKOFF_MS = (1000, 2000, 4000, 8000, 16000, 30000)

    def __init__(self, outbox, url, token, firmware="unknown", capabilities=None,
                 batch_limit=16, ack_timeout_ms=10000, socket_factory=None,
                 random_bytes=None, clock_us=None, control=None):
        self.outbox = outbox
        self.control = control
        self.endpoint = parse_ws_url(url)
        self.token = token
        self.firmware = firmware
        self.capabilities = capabilities or []
        self.batch_limit = min(max(1, batch_limit), 500)
        self.ack_timeout_ms = max(10000, ack_timeout_ms)
        self.socket_factory = socket_factory or self._default_socket
        self.random_bytes = random_bytes or os.urandom
        self.clock_us = clock_us or (lambda: 0)
        self.state = "wifi_wait"
        self.last_error = None
        self.sock = None
        self._key = None
        self._handshake_out = b""
        self._handshake_in = bytearray()
        self._send_buffer = b""
        self._parser = ServerFrameParser()
        self._hello_sent = False
        self._hello_acked = False
        self._hello_at = 0
        self._inflight = None
        self._inflight_at = 0
        self._backoff_index = 0
        self._retry_at = 0

        self._resend_not_before = 0
        self._hello_envelope = None
        self._hello_persisted = False
        self._ping_at = 0
        self._pong_deadline = 0

    def _default_socket(self, host, port):
        address = socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM)[0][-1]
        sock = socket.socket()
        sock.setblocking(False)
        try:
            sock.connect(address)
        except OSError as exc:
            if not _would_block(exc):
                sock.close()
                raise
        return sock

    def _request(self):
        endpoint = self.endpoint
        target = endpoint["path"]
        separator = "&" if "?" in target else "?"
        if self.token:
            target += separator + "token=" + _quote(self.token)
        host = endpoint["host"]
        if endpoint["port"] != 80:
            host += ":" + str(endpoint["port"])
        lines = [
            "GET " + target + " HTTP/1.1",
            "Host: " + host,
            "Upgrade: websocket",
            "Connection: Upgrade",
            "Sec-WebSocket-Key: " + self._key,
            "Sec-WebSocket-Version: 13",
            "",
            "",
        ]
        return "\r\n".join(lines).encode()

    def _connect(self):
        self.sock = self.socket_factory(self.endpoint["host"], self.endpoint["port"])
        self._key = _b64(self.random_bytes(16))
        self._handshake_out = self._request()
        self._handshake_in = bytearray()
        self._parser = ServerFrameParser()
        self.state = "authenticate"

    def _close(self):
        if self.sock is not None:
            try:
                self.sock.close()
            except Exception:
                pass
        self.sock = None
        self._send_buffer = b""
        self._hello_sent = False
        self._hello_acked = False
        self._hello_at = 0
        if self._hello_persisted:
            self._hello_envelope = None
            self._hello_persisted = False
        self._ping_at = 0
        self._pong_deadline = 0

    def _fail(self, now_ms, reason):
        self.last_error = str(reason)
        self._close()
        delay = self.BACKOFF_MS[min(self._backoff_index, len(self.BACKOFF_MS) - 1)]
        jitter = int.from_bytes(self.random_bytes(2), "big") % 251
        self._retry_at = ticks_add(now_ms, delay + jitter)
        self._backoff_index = min(self._backoff_index + 1,
                                  len(self.BACKOFF_MS) - 1)
        self.state = "backoff"

    def _send_some(self):
        if not self._send_buffer:
            return
        try:
            sent = self.sock.send(self._send_buffer)
        except OSError as exc:
            if _would_block(exc):
                return
            raise
        if sent:
            self._send_buffer = self._send_buffer[sent:]

    def _recv_some(self):
        try:
            return self.sock.recv(2048)
        except OSError as exc:
            if _would_block(exc):
                return None
            raise

    def _poll_handshake(self):
        if self._handshake_out:
            self._send_buffer = self._handshake_out
            self._handshake_out = b""
        self._send_some()
        if self._send_buffer:
            return
        data = self._recv_some()
        if data is None:
            return
        if not data:
            raise OSError("socket closed during handshake")
        self._handshake_in.extend(data)
        if len(self._handshake_in) > 4096:
            raise ValueError("WebSocket handshake too large")
        marker = self._handshake_in.find(b"\r\n\r\n")
        if marker < 0:
            return
        header = bytes(self._handshake_in[:marker]).decode()
        lines = header.split("\r\n")
        if len(lines) < 2 or " 101 " not in (" " + lines[0] + " "):
            raise ValueError("WebSocket upgrade rejected")
        fields = {}
        for line in lines[1:]:
            name, separator, value = line.partition(":")
            if separator:
                fields[name.strip().lower()] = value.strip()
        if fields.get("sec-websocket-accept") != websocket_accept(self._key):
            raise ValueError("invalid WebSocket accept")
        remainder = bytes(self._handshake_in[marker + 4:])
        self.state = "online"
        if remainder:
            self._handle_frames(remainder)

    def _hello(self):
        if self._hello_envelope is None:
            payload = {
                "type": "hello",
                "link_id": "transport",
                "firmware": self.firmware,
                "capabilities": self.capabilities,
                "links": self.outbox.builder.link_sessions(),
                "scope": "bmcu_link:telemetry",
                "drop_count": self.outbox.queue.dropped_count,
            }
            if self.control is not None:
                # The replay window is scoped to this HELLO announcement: an
                # unpersisted HELLO is resent verbatim, so the nonce rotates
                # only when a fresh HELLO envelope is built.
                payload["control_session_nonce"] = self.control.new_session()
                payload["control_enabled"] = self.control.enabled
            self._hello_envelope = self.outbox.builder.build(
                payload, self.clock_us(), len(self.outbox.queue))
        return self._hello_envelope

    def _queue_json(self, value):
        self._send_buffer = client_frame(json.dumps(value),
                                         mask=self.random_bytes(4))

    def _enrich_ack(self, message):
        if self._inflight is None:
            return message
        enriched = dict(message)
        rejected = []
        for item in message.get("rejected", []):
            candidate = dict(item)
            index = candidate.get("index")
            if isinstance(index, int) and 0 <= index < len(self._inflight):
                link = self._inflight[index]["link"]
                sent_sequence = link["transport_sequence"]
                parsed_sequence = candidate.get("transport_sequence")
                if parsed_sequence is None or parsed_sequence == sent_sequence:
                    candidate["link_id"] = link["id"]
                    candidate["pico_boot_session"] = link["pico_boot_session"]
                    candidate["transport_sequence"] = sent_sequence
            rejected.append(candidate)
        enriched["rejected"] = rejected
        return enriched

    def _accepted_watermarks(self, message):
        """Adapt Bambuddy accepted-only ACKs into durable per-link watermarks."""
        if (self._inflight is None or message.get("persisted") or message.get("rejected")):
            return message
        accepted = message.get("accepted")
        if not isinstance(accepted, int) or accepted < len(self._inflight):
            return message
        watermarks = {}
        for envelope in self._inflight:
            link = envelope["link"]
            key = (link["id"], link["pico_boot_session"])
            sequence = link["transport_sequence"]
            watermarks[key] = max(sequence, watermarks.get(key, -1))
        adapted = dict(message)
        adapted["persisted"] = [{
            "link_id": key[0],
            "pico_boot_session": key[1],
            "transport_sequence": sequence,
        } for key, sequence in watermarks.items()]
        return adapted
    def _handle_message(self, payload):
        message = json.loads(payload.decode())
        if message.get("type") == "error":
            detail = str(message.get("detail", message.get("code", "server error")))
            if self.token:
                detail = detail.replace(self.token, "***")
            self.last_error = "Bambuddy: " + detail[:160]
            return
        if message.get("type") == "control":
            if self.control is not None:
                self.control.handle(message)
            return
        if message.get("type") != "ack":
            return
        hello_ack = (self._hello_envelope is not None and self._hello_sent and
                     not self._hello_acked)
        if (hello_ack and message.get("accepted", 0) > 0 and
                not message.get("rejected")):
            self._hello_persisted = True
        if self._hello_envelope is not None:
            hello_link = self._hello_envelope["link"]
            for item in message.get("persisted", []):
                if (item.get("link_id") == hello_link["id"] and
                        item.get("pico_boot_session") ==
                        hello_link["pico_boot_session"] and
                        item.get("transport_sequence", -1) >=
                        hello_link["transport_sequence"]):
                    self._hello_persisted = True
        if hello_ack:
            self._hello_acked = True
            self._hello_at = 0
            return
        message = self._accepted_watermarks(message)
        result = self.outbox.apply_ack(self._enrich_ack(message))
        if result["persisted"] == 0 and result["rejected"] == 0:
            self._resend_not_before = ticks_add(self._inflight_at, 1000)
        else:
            self._resend_not_before = 0
            # WebSocket/HELLO success alone does not prove telemetry health.
            # Reset retry backoff only after a batch makes durable progress.
            self._backoff_index = 0
        self._inflight = None

    def _handle_frames(self, data):
        for opcode, payload in self._parser.feed(data):
            if opcode == 1:
                self._handle_message(payload)
            elif opcode == 8:
                code = ((payload[0] << 8) | payload[1]) if len(payload) >= 2 else 0
                reason = payload[2:].decode() if len(payload) > 2 else ""
                if code == 4401:
                    reason = reason or "authentication failed"
                elif code == 4404:
                    reason = reason or "BMCU Link feature disabled"
                raise OSError("WebSocket close %d: %s" % (code, reason))
            elif opcode == 9:
                if self._send_buffer:
                    raise OSError("ping received while send is pending")
                self._send_buffer = client_frame(payload, opcode=10,
                                                 mask=self.random_bytes(4))
            elif opcode not in (0, 10):
                raise ValueError("unsupported WebSocket opcode")

    def _poll_online(self, now_ms):
        self._send_some()
        if self._send_buffer:
            return
        data = self._recv_some()
        if data == b"":
            raise OSError("WebSocket closed")
        if data:
            self._handle_frames(data)
            self._ping_at = ticks_add(now_ms, 30000)
            self._pong_deadline = 0
        if self._send_buffer:
            return
        if (self._pong_deadline and
                ticks_diff(now_ms, self._pong_deadline) >= 0):
            raise OSError("WebSocket liveness timeout")
        if not self._ping_at:
            self._ping_at = ticks_add(now_ms, 30000)
        elif ticks_diff(now_ms, self._ping_at) >= 0:
            self._send_buffer = client_frame(
                b"bmcu", opcode=9, mask=self.random_bytes(4))
            self._pong_deadline = ticks_add(now_ms, 10000)
            self._ping_at = ticks_add(now_ms, 30000)
            return
        if not self._hello_sent:
            self._queue_json(self._hello())
            self._hello_sent = True
            self._hello_at = now_ms
            return
        if not self._hello_acked:
            if ticks_diff(now_ms, self._hello_at) >= self.ack_timeout_ms:
                raise OSError("Bambuddy HELLO ACK timeout")
            return
        if self.control is not None and self.control.results:
            self._queue_json(self.control.results.pop(0))
            return
        if self._inflight is not None:
            if ticks_diff(now_ms, self._inflight_at) >= self.ack_timeout_ms:
                raise OSError("Bambuddy ACK timeout")
            return
        if (self._resend_not_before and
                ticks_diff(now_ms, self._resend_not_before) < 0):
            return
        batch = self.outbox.queue.batch(self.batch_limit, now_ms)
        if batch:
            self._inflight = batch
            self._inflight_at = now_ms
            self._queue_json(batch)

    def poll(self, now_ms, wifi_online=True):
        self.outbox.flush(now_ms)
        if not wifi_online:
            self._close()
            self.state = "wifi_wait"
            return
        if self.state == "wifi_wait":
            self.state = "backoff"
            self._retry_at = now_ms
        if self.state == "backoff":
            if ticks_diff(now_ms, self._retry_at) < 0:
                return
            try:
                self._connect()
            except Exception as exc:
                self._fail(now_ms, exc)
                return
        try:
            if self.state == "authenticate":
                self._poll_handshake()
            elif self.state == "online":
                self._poll_online(now_ms)
        except Exception as exc:
            self._fail(now_ms, exc)

    def stop(self):
        self._close()
        self.state = "disabled"

    def status(self):
        return {
            "state": self.state,
            "last_error": self.last_error,
            "queue_depth": len(self.outbox.queue),
            "dropped_count": self.outbox.queue.dropped_count,
            "pico_boot_session": self.outbox.pico_boot_session,
        }
