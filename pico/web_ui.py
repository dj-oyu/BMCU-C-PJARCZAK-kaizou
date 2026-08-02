"""Bounded non-blocking HTTP server for static UI and BMB1 binary APIs."""

import socket
import time
try:
    import errno
except ImportError:
    import uerrno as errno
try:
    import uos as os
except ImportError:
    import os

MAX_REQUEST_BYTES = 2048
MAX_BODY_BYTES = 256
MAX_RECV_BYTES = 256
MAX_SEND_BYTES = 256
HTTP_LISTEN_BACKLOG = 4
BINARY_TYPE = "application/vnd.bmcu-monitor.v1"
# The page is built by web/ and staged by tools/build_web_ui.py. Serving it from
# littlefs instead of a module-level literal keeps ~14 KB off a heap that the UI
# itself flags as low below 20 KB.
INDEX_PATH = "www/index.html.gz"
FILE_CHUNK_BYTES = 512


def _would_block(error):
    code = error.args[0] if error.args else None
    return code in (
        getattr(errno, "EAGAIN", -1),
        getattr(errno, "EWOULDBLOCK", -1),
    )


class _Response:
    def __init__(self, header, body):
        self.parts = [header]
        if isinstance(body, (list, tuple)):
            self.parts.extend(body)
        else:
            self.parts.append(body)
        self.offset = 0

    def current(self, maximum=None):
        while self.parts and self.offset >= len(self.parts[0]):
            self.parts.pop(0)
            self.offset = 0
        if not self.parts:
            return b""
        value = memoryview(self.parts[0])[self.offset:]
        return value[:maximum] if maximum and len(value) > maximum else value

    def consume(self, count):
        while count and self.parts:
            remaining = len(self.parts[0]) - self.offset
            if count < remaining:
                self.offset += count
                return
            count -= remaining
            self.parts.pop(0)
            self.offset = 0

    def done(self):
        return not self.parts

    def __contains__(self, value):
        return any(value in part for part in self.parts)

    def close(self):
        pass


class _FileResponse:
    """Streams a staged file one bounded chunk at a time.

    Reading the whole page into RAM per request would trade a permanent 14 KB
    allocation for a recurring one, which is worse: the recurring version
    fragments the heap. Only ``chunk_size`` bytes are live at any moment.
    """

    def __init__(self, header, file_object, length,
                 chunk_size=FILE_CHUNK_BYTES):
        self.file = file_object
        self.remaining = length
        self.pending = memoryview(header)
        self.chunk_size = chunk_size

    def current(self, maximum=None):
        if not len(self.pending):
            if self.remaining <= 0:
                return b""
            data = self.file.read(min(self.chunk_size, self.remaining))
            if not data:
                # A truncated file must end the response rather than spin: the
                # client already has our Content-Length and will notice.
                self.remaining = 0
                return b""
            self.remaining -= len(data)
            self.pending = memoryview(data)
        value = self.pending
        return value[:maximum] if maximum and len(value) > maximum else value

    def consume(self, count):
        if count >= len(self.pending):
            self.pending = memoryview(b"")
            return
        self.pending = self.pending[count:]

    def done(self):
        return not len(self.pending) and self.remaining <= 0

    def __contains__(self, _value):
        # Staged bytes are gzip; substring assertions do not apply.
        return False

    def close(self):
        try:
            self.file.close()
        except OSError:
            pass


class WebUI:
    def __init__(self, binary_provider, port=80, error_handler=None,
                 settings_provider=None):
        self.binary_provider = binary_provider
        self.error_handler = error_handler
        self.settings_provider = settings_provider
        self.port = port
        self.server = None
        self.servers = []
        self.client = None
        self.request = bytearray(MAX_REQUEST_BYTES)
        self.request_view = memoryview(self.request)
        self.request_length = 0
        self.request_meta = None
        self.response = None
        self.close_at_ms = None
        self.client_deadline_ms = None

    @staticmethod
    def _listener(family, address, port, ipv6_only=False):
        server = socket.socket(family, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if ipv6_only:
            server.setsockopt(41, 27, 1)
        server.bind((address, port))
        server.listen(HTTP_LISTEN_BACKLOG)
        server.setblocking(False)
        return server

    def start(self):
        try:
            self.servers.append(self._listener(
                socket.AF_INET6, "::", self.port, True))
        except (AttributeError, OSError):
            pass
        self.server = self._listener(socket.AF_INET, "0.0.0.0", self.port)
        self.servers.append(self.server)

    @staticmethod
    def _header(status, content_type, size, extra=""):
        return ("HTTP/1.1 %s\r\nContent-Type: %s\r\nContent-Length: %d\r\n"
                "Cache-Control: no-store\r\nX-Content-Type-Options: nosniff\r\n"
                "Referrer-Policy: no-referrer\r\n%sConnection: close\r\n\r\n" %
                (status, content_type, size, extra)).encode()

    @classmethod
    def _http_response(cls, status, content_type, body):
        size = sum(len(item) for item in body) if isinstance(
            body, (list, tuple)) else len(body)
        return _Response(cls._header(status, content_type, size), body)

    @classmethod
    def _static_response(cls, path):
        """Serves the pre-compressed page; the Pico never gzips at runtime."""
        try:
            size = os.stat(path)[6]
            handle = open(path, "rb")
        except OSError:
            return cls._http_response(
                "503 Service Unavailable", "text/plain",
                b"web UI asset missing; run tools/build_web_ui.py\n")
        header = cls._header("200 OK", "text/html; charset=utf-8", size,
                             "Content-Encoding: gzip\r\n")
        return _FileResponse(header, handle, size)

    def _request_metadata(self):
        if self.request_meta is not None:
            return self.request_meta
        marker = self.request.find(b"\r\n\r\n", 0, self.request_length)
        if marker < 0:
            return None
        lines = bytes(self.request_view[:marker]).split(b"\r\n")
        request_line = lines[0].split() if lines else ()
        method = request_line[0] if request_line else b""
        path = request_line[1] if len(request_line) > 1 else b""
        headers = {}
        for line in lines[1:]:
            if b":" not in line:
                continue
            name, value = line.split(b":", 1)
            headers[name.strip().lower().decode()] = value.strip().decode()
        try:
            length = int(headers.get("content-length", "0"))
        except ValueError:
            length = -1
        self.request_meta = method, path, headers, length, marker + 4
        return self.request_meta

    def _request_complete(self):
        metadata = self._request_metadata()
        if metadata is None:
            return False
        length, body_start = metadata[3], metadata[4]
        return length < 0 or length > MAX_BODY_BYTES or \
            self.request_length - body_start >= length

    def _finish_request(self):
        method, path, headers, length, body_start = \
            self._request_metadata()
        if length < 0:
            self.response = self._http_response(
                "400 Bad Request", "text/plain", b"invalid content length\n")
            return
        if length > MAX_BODY_BYTES:
            self.response = self._http_response(
                "413 Payload Too Large", "text/plain", b"body too large\n")
            return
        body = bytes(self.request_view[body_start:body_start + length]) \
            if length else b""
        if (path.startswith(b"/api/device-key") or
                path.startswith(b"/api/transport")) and self.settings_provider:
            result = self.settings_provider(
                method.decode(), path.decode(), headers, body)
            self.response = self._http_response(*result) if result else \
                self._http_response("404 Not Found", "text/plain",
                                    b"Not found\n")
        elif method != b"GET":
            self.response = self._http_response(
                "405 Method Not Allowed", "text/plain", b"GET only\n")
        elif path == b"/":
            self.response = self._static_response(INDEX_PATH)
        elif path.startswith(b"/api/") and b".bin" in path:
            value = self.binary_provider(path.decode())
            self.response = self._http_response(
                "200 OK", BINARY_TYPE, value) if value is not None else \
                self._http_response("404 Not Found", "text/plain",
                                    b"Not found\n")
        else:
            self.response = self._http_response(
                "404 Not Found", "text/plain", b"Not found\n")

    def _close_client(self):
        if self.response is not None:
            # A file-backed response owns an open handle; dropping the reference
            # would leak a littlefs descriptor on every aborted request.
            self.response.close()
        if self.client:
            try:
                self.client.close()
            except OSError:
                pass
        self.client = None
        self.request_length = 0
        self.request_meta = None
        self.response = None
        self.close_at_ms = None
        self.client_deadline_ms = None

    @staticmethod
    def _now_ms():
        return time.ticks_ms() if hasattr(time, "ticks_ms") else int(
            time.monotonic() * 1000)

    def _touch(self):
        self.client_deadline_ms = self._now_ms() + 3000

    def poll(self):
        now = self._now_ms()
        if self.client and self.client_deadline_ms is not None and \
                now >= self.client_deadline_ms:
            self._close_client()
            return
        if self.client and self.response is not None:
            try:
                sent = self.client.send(self.response.current(MAX_SEND_BYTES))
            except OSError as error:
                if _would_block(error):
                    return
                self._close_client()
                return
            if not sent:
                self._close_client()
                return
            self.response.consume(sent)
            if self.response.done():
                self._close_client()
            else:
                self._touch()
            return
        if self.client:
            if self.request_length >= MAX_REQUEST_BYTES:
                self.response = self._http_response(
                    "413 Payload Too Large", "text/plain", b"Too large\n")
                return
            maximum = min(
                MAX_RECV_BYTES, MAX_REQUEST_BYTES - self.request_length)
            target = self.request_view[
                self.request_length:self.request_length + maximum]
            try:
                try:
                    count = self.client.readinto(target)
                except AttributeError:
                    data = self.client.recv(maximum)
                    count = len(data)
                    target[:count] = data
            except OSError as error:
                if _would_block(error):
                    return
                self._close_client()
                return
            if count is None:
                return
            if count == 0:
                self._close_client()
                return
            self.request_length += count
            self._touch()
            if self._request_complete():
                try:
                    self._finish_request()
                except Exception as error:
                    if self.error_handler:
                        self.error_handler("request", error)
                    self.response = self._http_response(
                        "500 Internal Server Error", "text/plain",
                        b"internal Pico error\n")
            return
        for server in self.servers or (self.server,):
            if server is None:
                continue
            try:
                self.client, _ = server.accept()
                self.client.setblocking(False)
                self._touch()
                return
            except OSError:
                pass
