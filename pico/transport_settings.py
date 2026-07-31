"""Persistent Bambuddy BMB1 host and port provisioning."""

try:
    import uos as os
except ImportError:
    import os


MAGIC = b"BTS1"
MAX_HOST_BYTES = 240
DEFAULT_PATH = "bmcu_transport.cfg"


def validate_host(value):
    if isinstance(value, bytes):
        try:
            value = value.decode()
        except UnicodeError:
            raise ValueError("host must be ASCII")
    host = str(value).strip()
    try:
        encoded = host.encode("ascii")
    except UnicodeError:
        raise ValueError("host must be ASCII")
    if not encoded or len(encoded) > MAX_HOST_BYTES:
        raise ValueError("host must be 1 to 240 characters")
    for character in encoded:
        if not (48 <= character <= 57 or 65 <= character <= 90 or
                97 <= character <= 122 or character in (45, 46, 95)):
            raise ValueError("host must be an IPv4 address or DNS name")
    if b".." in encoded:
        raise ValueError("host contains an empty DNS label")
    return host


def validate_port(value):
    try:
        port = int(value)
    except (TypeError, ValueError):
        raise ValueError("port must be an integer")
    if port < 1 or port > 65535:
        raise ValueError("port must be between 1 and 65535")
    return port


def _remove(path):
    try:
        os.remove(path)
    except OSError:
        pass


class TransportSettingsStore:
    """Loads config defaults and atomically persists UI replacements."""

    def __init__(self, initial_host="", initial_port=8766,
                 path=DEFAULT_PATH):
        self.path = path
        self.backup_path = path + ".bak"
        self.temporary_path = path + ".tmp"
        self.host = ""
        self.port = 0
        self.load_error = None
        for candidate in (self.path, self.backup_path):
            try:
                with open(candidate, "rb") as stream:
                    value = stream.read(MAX_HOST_BYTES + 8)
                self.host, self.port = self._decode(value)
                return
            except OSError:
                pass
            except ValueError as error:
                self.load_error = str(error)
        try:
            self.host = validate_host(initial_host)
            self.port = validate_port(initial_port)
        except ValueError as error:
            self.host = ""
            self.port = 0
            self.load_error = str(error)

    @property
    def configured(self):
        return bool(self.host and self.port)

    @staticmethod
    def _encode(host, port):
        encoded = host.encode()
        return MAGIC + bytes((port >> 8, port & 0xff, len(encoded))) + encoded

    @staticmethod
    def _decode(value):
        if len(value) < 7 or value[:4] != MAGIC:
            raise ValueError("stored transport settings are invalid")
        port = (value[4] << 8) | value[5]
        length = value[6]
        if len(value) != 7 + length:
            raise ValueError("stored transport settings have invalid length")
        return validate_host(value[7:]), validate_port(port)

    def status_body(self):
        return ("configured=%d\nhost=%s\nport=%d\n" %
                (1 if self.configured else 0, self.host,
                 self.port)).encode()

    def update(self, host, port):
        host = validate_host(host)
        port = validate_port(port)
        value = self._encode(host, port)
        with open(self.temporary_path, "wb") as stream:
            stream.write(value)
            if hasattr(stream, "flush"):
                stream.flush()
        _remove(self.backup_path)
        try:
            os.rename(self.path, self.backup_path)
        except OSError:
            pass
        try:
            os.rename(self.temporary_path, self.path)
        except Exception:
            try:
                os.rename(self.backup_path, self.path)
            except OSError:
                pass
            raise
        _remove(self.backup_path)
        self.host = host
        self.port = port
        self.load_error = None
        return host, port


class TransportSettingsAPI:
    """Same-origin status and write API for the BMB1 TCP endpoint."""

    def __init__(self, store, apply_endpoint, on_update=None):
        self.store = store
        self.apply_endpoint = apply_endpoint
        self.on_update = on_update

    def handle(self, method, path, headers, body):
        if method == "GET" and path == "/api/transport/status":
            return "200 OK", "text/plain; charset=utf-8", \
                self.store.status_body()
        if path != "/api/transport" or method != "POST":
            return None
        if headers.get("content-type") != "application/octet-stream" or \
                headers.get("x-bmcu-settings-action") != "update":
            return "403 Forbidden", "text/plain", b"protected request\n"
        try:
            lines = body.decode().splitlines()
            if len(lines) != 2:
                raise ValueError("expected host and port")
            host, port = self.store.update(lines[0], lines[1])
        except (UnicodeError, ValueError) as error:
            return "400 Bad Request", "text/plain", (str(error) + "\n").encode()
        self.apply_endpoint(host, port)
        if self.on_update is not None:
            self.on_update()
        return "204 No Content", "text/plain", b""
