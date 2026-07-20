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
        }
        try:
            self._validate(defaults)
        except ValueError:
            defaults = {"enabled": False, "url": "", "token": ""}
        self._values = defaults
        stored = self._load()
        if isinstance(stored, dict):
            candidate = dict(defaults)
            for key in ("enabled", "url", "token"):
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

    def public(self):
        return {
            "enabled": self.enabled,
            "url": self.url,
            "token_set": bool(self.token),
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
        self._validate(values)
        self._values = values
        self._save()
        self.revision += 1
        self._csrf = self.random_bytes(16).hex()
        return self.public()
