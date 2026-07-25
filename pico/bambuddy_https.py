"""Cooperative Bambuddy HTTPS NDJSON fallback adapter.

Permitted fallback per docs/PICO_BAMBUDDY_TRANSPORT.md section 6: Pico-initiated
batched NDJSON POST carrying the *same* envelope, deduplication key, and durable
ACK semantics as the WebSocket transport. Delivery semantics are not
reimplemented here - the queue, persisted watermark advance, retryable
retention, and non-retryable quarantine all live in ``bambuddy_transport`` and
the ACK adaptation rules in ``bambuddy_session``. Only the wire framing and the
transport session differ.

The fallback cannot carry commands, so no CONTROL gateway is wired to it.
"""

try:
    import ujson as json
except ImportError:
    import json
try:
    import usocket as socket
except ImportError:
    import socket
try:
    import uos as os
except ImportError:
    import os

from bambuddy_session import (accepted_only_watermarks, enrich_rejected,
                              hello_persisted_by_ack)
from bambuddy_ws import (DEFAULT_PORTS, parse_endpoint_url, ticks_add,
                         ticks_diff, _would_block)


MAX_HEADER_BYTES = 4096
MAX_BODY_BYTES = 16384
# A keep-alive recycle is not a failure, but it must never become an unpaced
# TLS reconnect loop either.
MIN_RECONNECT_MS = 250


class BambuddyNdjsonClient:
    """One-request-in-flight NDJSON transport with bounded cooperative work."""

    BACKOFF_MS = (1000, 2000, 4000, 8000, 16000, 30000)

    def __init__(self, outbox, url, token, firmware="unknown", capabilities=None,
                 batch_limit=16, ack_timeout_ms=10000, request_interval_ms=1000,
                 socket_factory=None, random_bytes=None, clock_us=None,
                 tls_ca=None, tls_insecure=False):
        self.outbox = outbox
        self.endpoint = parse_endpoint_url(url, ("https", "http"))
        if not isinstance(token, str):
            token = "" if token is None else str(token)
        if "\r" in token or "\n" in token:
            raise ValueError("invalid characters in token")
        self.token = token
        self.firmware = firmware
        self.capabilities = capabilities or []
        self.batch_limit = min(max(1, batch_limit), 500)
        self.ack_timeout_ms = max(10000, ack_timeout_ms)
        self.request_interval_ms = max(0, request_interval_ms)
        self.socket_factory = socket_factory or self._default_socket
        self.random_bytes = random_bytes or os.urandom
        self.clock_us = clock_us or (lambda: 0)
        self.tls_ca = tls_ca
        self.tls_insecure = tls_insecure

        self.state = "wifi_wait"
        self.last_error = None
        self.sock = None
        self._send_buffer = b""
        self._response = bytearray()
        self._head = None
        self._body_start = 0
        self._content_length = 0
        self._request_kind = None
        self._request_at = 0
        self._next_request_at = 0
        # None (not 0) is the disarmed sentinel: ticks_add wraps into
        # [0, 2**30) on MicroPython, so 0 is a legal deadline value.
        self._resend_not_before = None
        self._server_close = False
        self._inflight = None
        self._backoff_index = 0
        self._retry_at = 0
        self._hello_envelope = None
        self._hello_persisted = False
        self._hello_acked = False
        self._progress = False
        self._pipeline_hello = False

    # ------------------------------------------------------------------ setup

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
        if self.endpoint["scheme"] == "https":
            from bambuddy_tls import wrap_tls
            try:
                return wrap_tls(sock, host, self.tls_ca, self.tls_insecure)
            except Exception:
                sock.close()
                raise
        return sock

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
            self._hello_envelope = self.outbox.builder.build(
                payload, self.clock_us(), len(self.outbox.queue))
        return self._hello_envelope

    # --------------------------------------------------------------- requests

    def _host_header(self):
        endpoint = self.endpoint
        host = endpoint["host"]
        if endpoint["port"] != DEFAULT_PORTS.get(endpoint["scheme"], 443):
            host += ":" + str(endpoint["port"])
        return host

    def _build_request(self, envelopes):
        lines = []
        for envelope in envelopes:
            lines.append(json.dumps(envelope))
        body = ("\n".join(lines) + "\n").encode()
        head = [
            "POST " + self.endpoint["path"] + " HTTP/1.1",
            "Host: " + self._host_header(),
        ]
        if self.token:
            # Bearer header only: the token must never reach an access log.
            head.append("Authorization: Bearer " + self.token)
        head.append("Content-Type: application/x-ndjson")
        head.append("Content-Length: " + str(len(body)))
        head.append("Connection: keep-alive")
        head.append("")
        head.append("")
        return "\r\n".join(head).encode() + body

    def _begin_request(self, kind, envelopes, now_ms):
        self._send_buffer = self._build_request(envelopes)
        self._request_kind = kind
        self._request_at = now_ms
        self._response = bytearray()
        self._head = None
        self._body_start = 0
        self._content_length = 0
        self.state = "request"

    def _ready_to_send(self, now_ms):
        if ticks_diff(now_ms, self._next_request_at) < 0:
            return False
        return (self._resend_not_before is None or
                ticks_diff(now_ms, self._resend_not_before) >= 0)

    def _connect(self, now_ms):
        self.sock = self.socket_factory(self.endpoint["host"],
                                        self.endpoint["port"])
        self._server_close = False
        self._hello_acked = False
        self._progress = False
        self._inflight = None
        body = [self._hello()]
        kind = "hello"
        if self._pipeline_hello and self._ready_to_send(now_ms):
            # A server that closes after every response can never carry a
            # second request, so a HELLO-only POST would starve telemetry
            # until the 30 s queue age expires it. NDJSON lets the batch ride
            # in the very same body, right behind the HELLO line.
            batch = self.outbox.queue.batch(self.batch_limit, now_ms)
            if batch:
                body = body + batch
                self._inflight = body
                kind = "hello_batch"
        self._begin_request(kind, body, now_ms)

    def _close(self):
        if self.sock is not None:
            try:
                self.sock.close()
            except Exception:
                pass
        self.sock = None
        self._send_buffer = b""
        self._response = bytearray()
        self._head = None
        self._request_kind = None
        self._inflight = None
        self._hello_acked = False
        self._server_close = False
        self._progress = False
        if self._hello_persisted:
            self._hello_envelope = None
            self._hello_persisted = False

    def _recycle(self, now_ms):
        """Honour a keep-alive close or EOF without treating it as a failure.

        The connection is over either way, but the retry is paced so a server
        that closes after every response cannot drive an unpaced TLS reconnect
        hot loop, and a connection that carried nothing durable while telemetry
        was queued still grows the backoff index.
        """
        stalled = len(self.outbox.queue) > 0 and not self._progress
        # Consecutive connections that deliver nothing durable are a failure in
        # slow motion, even though each individual close was graceful.
        repeated = stalled and self._pipeline_hello
        if stalled:
            # Fold the batch into the next HELLO request.
            self._pipeline_hello = True
        self._close()
        delay = max(self.request_interval_ms, MIN_RECONNECT_MS)
        if repeated:
            index = min(self._backoff_index, len(self.BACKOFF_MS) - 1)
            delay = max(delay, self.BACKOFF_MS[index])
            self._backoff_index = min(self._backoff_index + 1,
                                      len(self.BACKOFF_MS) - 1)
        self._retry_at = ticks_add(now_ms, delay)
        self.state = "backoff"

    def _fail(self, now_ms, reason):
        reason = str(reason)
        if self.token:
            reason = reason.replace(self.token, "***")
        self.last_error = reason[:160]
        self._close()
        delay = self.BACKOFF_MS[min(self._backoff_index, len(self.BACKOFF_MS) - 1)]
        jitter = int.from_bytes(self.random_bytes(2), "big") % 251
        self._retry_at = ticks_add(now_ms, delay + jitter)
        self._backoff_index = min(self._backoff_index + 1,
                                  len(self.BACKOFF_MS) - 1)
        self.state = "backoff"

    # ------------------------------------------------------------------- I/O

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
            return self.sock.recv(1024)
        except OSError as exc:
            if _would_block(exc):
                return None
            raise

    def _parse_head(self):
        marker = self._response.find(b"\r\n\r\n")
        if marker < 0:
            if len(self._response) > MAX_HEADER_BYTES:
                raise ValueError("HTTP response header too large")
            return False
        header = bytes(self._response[:marker]).decode()
        lines = header.split("\r\n")
        parts = lines[0].split(" ")
        if len(parts) < 2 or not parts[1].isdigit():
            raise ValueError("malformed HTTP status line")
        fields = {}
        for line in lines[1:]:
            name, separator, value = line.partition(":")
            if separator:
                fields[name.strip().lower()] = value.strip()
        if "transfer-encoding" in fields:
            raise ValueError("chunked responses are not supported")
        length_text = fields.get("content-length")
        if length_text is None or not length_text.isdigit():
            raise ValueError("Content-Length is required")
        length = int(length_text)
        if length > MAX_BODY_BYTES:
            raise ValueError("HTTP response body too large")
        self._head = (int(parts[1]), fields)
        self._body_start = marker + 4
        self._content_length = length
        return True

    def _poll_response(self, now_ms):
        data = self._recv_some()
        if data is None:
            return
        if not data:
            raise OSError("connection closed before response")
        self._response.extend(data)
        if self._head is None and not self._parse_head():
            return
        if len(self._response) < self._body_start + self._content_length:
            if len(self._response) > self._body_start + MAX_BODY_BYTES:
                raise ValueError("HTTP response body too large")
            return
        body = bytes(self._response[self._body_start:
                                    self._body_start + self._content_length])
        status, fields = self._head
        self._response = bytearray()
        self._head = None
        if fields.get("connection", "").lower() == "close":
            self._server_close = True
        self._finish_response(now_ms, status, body)

    def _finish_response(self, now_ms, status, body):
        if status != 200:
            if status in (401, 403):
                raise OSError("authentication rejected")
            if status == 404:
                raise OSError("BMCU Link feature disabled")
            raise OSError("Bambuddy HTTP %d" % status)
        message = json.loads(body.decode())
        if not isinstance(message, dict) or message.get("type") != "ack":
            raise ValueError("unexpected Bambuddy response")
        kind = self._request_kind
        self._request_kind = None
        if kind in ("hello", "hello_batch"):
            if hello_persisted_by_ack(message,
                                      self._hello()["link"], True):
                self._hello_persisted = True
            self._hello_acked = True
        if kind == "hello":
            self._next_request_at = now_ms
        else:
            if kind == "batch":
                # A standalone request was answered, so this connection does
                # carry more than one exchange: stop pipelining.
                self._pipeline_hello = False
            message = accepted_only_watermarks(message, self._inflight)
            result = self.outbox.apply_ack(
                enrich_rejected(message, self._inflight))
            if result["persisted"] == 0 and result["rejected"] == 0:
                self._resend_not_before = ticks_add(self._request_at, 1000)
            else:
                self._resend_not_before = None
                self._progress = True
                # A reachable endpoint alone does not prove telemetry health.
                self._backoff_index = 0
            self._inflight = None
            self._next_request_at = ticks_add(now_ms, self.request_interval_ms)
        self.state = "online"

    def _poll_online(self, now_ms):
        if not self._server_close:
            data = self._recv_some()
            if data is None:
                # Idle keep-alive connection: nothing to read.
                if not len(self.outbox.queue):
                    return
                if not self._ready_to_send(now_ms):
                    return
                batch = self.outbox.queue.batch(self.batch_limit, now_ms)
                if batch:
                    self._inflight = batch
                    self._begin_request("batch", batch, now_ms)
                return
            # EOF, or unsolicited bytes such as a proxy's 408 sent just before
            # it closes: no request is in flight, so neither is a data problem
            # worth failing over. The connection is simply over.
        self._recycle(now_ms)

    # ------------------------------------------------------------------ loop

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
                self._connect(now_ms)
            except Exception as exc:
                self._fail(now_ms, exc)
                return
        try:
            if self.state in ("request", "response"):
                if ticks_diff(now_ms, self._request_at) >= self.ack_timeout_ms:
                    raise OSError("Bambuddy ACK timeout")
            if self.state == "request":
                self._send_some()
                if not self._send_buffer:
                    self.state = "response"
            elif self.state == "response":
                self._poll_response(now_ms)
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
