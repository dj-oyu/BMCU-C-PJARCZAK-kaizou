"""Persistent BMB1 device-key provisioning without read-back."""

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


KEY_BYTES = 32
KEY_HEX_BYTES = KEY_BYTES * 2
DEFAULT_PATH = "bmcu_device.key"


def decode_key(value):
    if isinstance(value, str):
        value = value.encode()
    value = bytes(value).strip()
    if len(value) != KEY_HEX_BYTES:
        raise ValueError("device key must be exactly 64 hex characters")
    try:
        decoded = binascii.unhexlify(value)
    except (ValueError, TypeError):
        raise ValueError("device key must contain hexadecimal characters only")
    if len(decoded) != KEY_BYTES:
        raise ValueError("device key must decode to 32 bytes")
    return decoded


def _remove(path):
    try:
        os.remove(path)
    except OSError:
        pass


class DeviceKeyStore:
    """Keeps a 32-byte key in memory and atomically persists replacements."""

    def __init__(self, initial_hex="", path=DEFAULT_PATH):
        self.path = path
        self.backup_path = path + ".bak"
        self.temporary_path = path + ".tmp"
        self.key = None
        self.load_error = None
        for candidate in (self.path, self.backup_path):
            try:
                with open(candidate, "rb") as stream:
                    value = stream.read(KEY_BYTES + 1)
                if len(value) == KEY_BYTES:
                    self.key = value
                    return
                self.load_error = "stored device key has invalid length"
            except OSError:
                pass
        if initial_hex:
            try:
                self.key = decode_key(initial_hex)
            except ValueError as error:
                self.load_error = str(error)

    @property
    def configured(self):
        return self.key is not None

    def fingerprint(self):
        if self.key is None:
            return ""
        digest = hashlib.sha256(self.key).digest()
        return binascii.hexlify(digest[:6]).decode()

    def status_body(self):
        return ("configured=%d\nfingerprint=%s\n" %
                (1 if self.configured else 0, self.fingerprint())).encode()

    def update_hex(self, value):
        key = decode_key(value)
        with open(self.temporary_path, "wb") as stream:
            stream.write(key)
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
        self.key = key
        self.load_error = None
        return key


class DeviceKeyAPI:
    """Small same-origin API for key status and write-only provisioning."""

    def __init__(self, store, apply_key, on_update=None):
        self.store = store
        self.apply_key = apply_key
        self.on_update = on_update

    def handle(self, method, path, headers, body):
        if method == "GET" and path == "/api/device-key/status":
            return "200 OK", "text/plain; charset=utf-8", \
                self.store.status_body()
        if path != "/api/device-key" or method != "POST":
            return None
        if headers.get("content-type") != "application/octet-stream" or \
                headers.get("x-bmcu-key-action") != "update":
            return "403 Forbidden", "text/plain", b"protected request\n"
        try:
            key = self.store.update_hex(body)
        except ValueError as error:
            return "400 Bad Request", "text/plain", (str(error) + "\n").encode()
        self.apply_key(key)
        if self.on_update is not None:
            self.on_update()
        return "204 No Content", "text/plain", b""
