import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
PICO = ROOT / "pico"


def load(name):
    """Load a pico module by name, registering it for its dependents.

    ``pico/`` is deliberately never put on ``sys.path``: the gitignored
    ``pico/secrets.py`` would otherwise shadow the CPython stdlib ``secrets``
    module for the whole CI process.
    """
    spec = importlib.util.spec_from_file_location(name, PICO / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


load("bambuddy_session")
load("bambuddy_ws")
config_module = load("bambuddy_config")
web_module = load("web_ui")


class FakeNonBlockingClient:
    def __init__(self, operation):
        self.operation = operation
        self.closed = False

    def recv(self, _size):
        if self.operation == "recv":
            raise BlockingIOError(web_module.errno.EAGAIN, "would block")
        return b""

    def send(self, _data):
        if self.operation == "send":
            raise BlockingIOError(web_module.errno.EAGAIN, "would block")
        return 0

    def close(self):
        self.closed = True


class FixedRandom:
    def __init__(self):
        self.value = 0

    def __call__(self, size):
        self.value += 1
        return bytes([self.value]) * size


class BambuddyConfigTests(unittest.TestCase):
    def test_update_persists_but_never_returns_token(self):
        random = FixedRandom()
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "bambuddy.json")
            settings = config_module.BambuddyConfig(
                path=path, random_bytes=random)
            public = settings.public()
            saved = settings.update({
                "csrf": public["csrf"],
                "enabled": True,
                "url": "ws://bambuddy.local:8000/api/v1/bmcu-link/ws",
                "token": "private-token",
            })
            self.assertTrue(saved["token_set"])
            self.assertNotIn("token", saved)
            reloaded = config_module.BambuddyConfig(
                path=path, random_bytes=random)
            self.assertEqual(reloaded.token, "private-token")
            self.assertTrue(reloaded.enabled)

    def test_auth_disabled_allows_enabled_transport_without_token(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = config_module.BambuddyConfig(
                path=str(Path(directory) / "bambuddy.json"),
                random_bytes=FixedRandom())
            result = settings.update({
                "csrf": settings.public()["csrf"],
                "enabled": True,
                "url": "ws://bambuddy.local:8000/api/v1/bmcu-link/ws",
            })
            self.assertTrue(result["enabled"])
            self.assertFalse(result["token_set"])
            self.assertEqual(result["scope"], ["bmcu_link:telemetry"])
    def test_invalid_persisted_config_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bambuddy.json"
            path.write_text('{"enabled":true,"url":"https://bad","token":"x"}')
            settings = config_module.BambuddyConfig(path=str(path))
            self.assertFalse(settings.enabled)
            self.assertEqual(settings.url, "")

    def test_csrf_is_required_and_rotates(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = config_module.BambuddyConfig(
                path=str(Path(directory) / "bambuddy.json"),
                random_bytes=FixedRandom())
            old = settings.public()["csrf"]
            with self.assertRaises(ValueError):
                settings.update({"csrf": "bad", "enabled": False})
            result = settings.update({"csrf": old, "enabled": False})
            self.assertNotEqual(result["csrf"], old)


class WebUIConfigTests(unittest.TestCase):

    def test_recv_would_block_keeps_client_open(self):
        web = self.web(self.settings())
        client = FakeNonBlockingClient("recv")
        web.client = client
        web.poll()
        self.assertIs(web.client, client)
        self.assertFalse(client.closed)

    def test_send_would_block_keeps_client_open(self):
        web = self.web(self.settings())
        client = FakeNonBlockingClient("send")
        web.client = client
        web.response = b"response"
        web.poll()
        self.assertIs(web.client, client)
        self.assertFalse(client.closed)
        self.assertEqual(web.response, b"response")

    def test_url_must_not_embed_token(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = config_module.BambuddyConfig(
                path=str(Path(directory) / "bambuddy.json"))
            with self.assertRaises(ValueError):
                settings.update({
                    "csrf": settings.public()["csrf"],
                    "enabled": True,
                    "url": "ws://host/ws?token=leak",
                    "token": "separate",
                })

    def settings(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        return config_module.BambuddyConfig(
            path=str(Path(directory.name) / "bambuddy.json"),
            random_bytes=FixedRandom())

    def web(self, settings):
        return web_module.WebUI(
            lambda _path: {"ok": True},
            config_provider=settings.public,
            config_updater=settings.update)

    def test_get_config_contains_no_secret(self):
        settings = self.settings()
        web = self.web(settings)
        web.request = bytearray(
            b"GET /api/bambuddy/config HTTP/1.1\r\nHost: pico\r\n\r\n")
        self.assertTrue(web._request_ready())
        web._finish_request()
        self.assertIn(b"200 OK", web.response)
        self.assertNotIn(b'"token":', web.response)

    def test_post_waits_for_full_body_and_updates(self):
        settings = self.settings()
        web = self.web(settings)
        body = json.dumps({
            "csrf": settings.public()["csrf"],
            "enabled": True,
            "url": "ws://host:8000/ws",
            "token": "secret",
        }).encode()
        header = (
            b"POST /api/bambuddy/config HTTP/1.1\r\n"
            b"Content-Type: application/json\r\n"
            + ("Content-Length: %d\r\n\r\n" % len(body)).encode()
        )
        web.request = bytearray(header + body[:-1])
        self.assertFalse(web._request_ready())
        web.request.extend(body[-1:])
        self.assertTrue(web._request_ready())
        web._finish_request()
        self.assertIn(b"200 OK", web.response)
        self.assertTrue(settings.enabled)
        self.assertNotIn(b"secret", web.response)

    def test_local_soft_reset_route_requires_explicit_command_handler(self):
        calls = []

        def command(path, request):
            calls.append((path, request))
            return {"state": "requested", "operation_id": 7}

        web = web_module.WebUI(
            lambda _path: {"ok": True}, command_updater=command)
        body = json.dumps({"csrf": "token", "confirm": "RESET BMCU"}).encode()
        header = (
            b"POST /api/devices/bmcu-a/soft-reset HTTP/1.1\r\n"
            b"Content-Type: application/json\r\n"
            + ("Content-Length: %d\r\n\r\n" % len(body)).encode()
        )
        web.request = bytearray(header + body)
        self.assertTrue(web._request_ready())
        web._finish_request()
        self.assertIn(b"202 Accepted", web.response)
        self.assertEqual(calls[0][0], "/api/devices/bmcu-a/soft-reset")
        self.assertEqual(calls[0][1]["confirm"], "RESET BMCU")


if __name__ == "__main__":
    unittest.main()
