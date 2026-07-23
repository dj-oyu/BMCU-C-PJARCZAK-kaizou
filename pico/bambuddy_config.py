"""Persistent Bambuddy commissioning configuration for the Pico."""

try:
    import ujson as json
except ImportError:
    import json
try:
    import uos as os
except ImportError:
    import os

from bambuddy_ws import parse_ws_url


DEFAULT_PATH = "bambuddy.json"


class BambuddyConfig:
    def __init__(self, secrets_module=None, path=DEFAULT_PATH, random_bytes=None):
        self.path = path
        self.random_bytes = random_bytes or os.urandom
        self.revision = 0
        self._csrf = self.random_bytes(16).hex()
        defaults = {
            "enabled": bool(getattr(secrets_module, "BAMBUDDY_ENABLED", False)),
            "url": getattr(secrets_module, "BAMBUDDY_WS_URL", ""),
            "token": getattr(secrets_module, "BAMBUDDY_TOKEN", ""),
            "control_enabled": False,
            "control_key": "",
        }
        try:
            self._validate(defaults)
        except ValueError:
            defaults = {"enabled": False, "url": "", "token": "",
                        "control_enabled": False, "control_key": ""}
        self._values = defaults
        stored = self._load()
        if isinstance(stored, dict):
            candidate = dict(defaults)
            for key in ("enabled", "url", "token",
                        "control_enabled", "control_key"):
                if key in stored:
                    candidate[key] = stored[key]
            try:
                self._validate(candidate)
                self._values = candidate
            except ValueError:
                pass

    def _load(self):
        try:
            with open(self.path, "r") as source:
                return json.load(source)
        except (OSError, ValueError):
            return None

    @staticmethod
    def _validate(values):
        if not isinstance(values.get("enabled"), bool):
            raise ValueError("enabled must be a boolean")
        url = values.get("url", "")
        token = values.get("token", "")
        if not isinstance(url, str) or not isinstance(token, str):
            raise ValueError("url and token must be strings")
        if len(url) > 512 or len(token) > 512:
            raise ValueError("configuration value is too long")
        if url:
            if "token=" in url.lower():
                raise ValueError("put token in the separate token field")
            parse_ws_url(url)
        if values["enabled"] and not url:
            raise ValueError("url is required when enabled")
        if not isinstance(values.get("control_enabled"), bool):
            raise ValueError("control_enabled must be a boolean")
        control_key = values.get("control_key", "")
        if not isinstance(control_key, str):
            raise ValueError("control_key must be a string")
        if control_key:
            if len(control_key) != 64:
                raise ValueError("control_key must be 64 hex characters")
            for character in control_key:
                if character not in "0123456789abcdefABCDEF":
                    raise ValueError("control_key must be 64 hex characters")
        if values["control_enabled"] and not control_key:
            raise ValueError("control_key is required when control is enabled")

    def _save(self):
        temporary = self.path + ".tmp"
        with open(temporary, "w") as target:
            json.dump(self._values, target)
            try:
                target.flush()
            except AttributeError:
                pass
        try:
            os.rename(temporary, self.path)
        except OSError:
            # Some MicroPython filesystems cannot replace an existing name.
            os.remove(self.path)
            os.rename(temporary, self.path)


    @property
    def enabled(self):
        return self._values["enabled"]

    @property
    def url(self):
        return self._values["url"]

    @property
    def token(self):
        return self._values["token"]

    @property
    def control_enabled(self):
        return self._values["control_enabled"]

    @property
    def control_key(self):
        return self._values["control_key"]

    def public(self):
        return {
            "enabled": self.enabled,
            "url": self.url,
            "token_set": bool(self.token),
            "control_enabled": self.control_enabled,
            "control_key_set": bool(self.control_key),
            "scope": ["bmcu_link:telemetry"],
            "csrf": self._csrf,
            "revision": self.revision,
        }

    def update(self, request):
        if not isinstance(request, dict):
            raise ValueError("JSON object required")
        if request.get("csrf") != self._csrf:
            raise ValueError("invalid CSRF token")
        values = dict(self._values)
        if "enabled" in request:
            values["enabled"] = request["enabled"]
        if "url" in request:
            values["url"] = request["url"].strip()
        if "token" in request:
            token = request["token"]
            if not isinstance(token, str):
                raise ValueError("token must be a string")
            if token:
                values["token"] = token
        if request.get("clear_token") is True:
            values["token"] = ""
        if "control_enabled" in request:
            values["control_enabled"] = request["control_enabled"]
        if "control_key" in request:
            control_key = request["control_key"]
            if not isinstance(control_key, str):
                raise ValueError("control_key must be a string")
            if control_key:
                values["control_key"] = control_key.lower()
        if request.get("clear_control_key") is True:
            values["control_key"] = ""
            values["control_enabled"] = False
        self._validate(values)
        self._values = values
        self._save()
        self.revision += 1
        self._csrf = self.random_bytes(16).hex()
        return self.public()
