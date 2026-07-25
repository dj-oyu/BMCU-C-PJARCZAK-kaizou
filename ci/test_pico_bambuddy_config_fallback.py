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

PEM = ("-----BEGIN CERTIFICATE-----\n"
       "MIIBkTCB+wIJAKb\n"
       "-----END CERTIFICATE-----\n")
NDJSON_URL = "https://bambuddy.local/api/v1/bmcu-link/ndjson"


class FixedRandom:
    def __init__(self):
        self.value = 0

    def __call__(self, size):
        self.value += 1
        return bytes([self.value]) * size


class TlsConfigTests(unittest.TestCase):
    def settings(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = str(Path(directory.name) / "bambuddy.json")
        return config_module.BambuddyConfig(
            path=self.path, random_bytes=FixedRandom())

    def test_https_url_with_ca_saves_and_never_echoes_pem(self):
        settings = self.settings()
        result = settings.update({
            "csrf": settings.public()["csrf"],
            "enabled": True,
            "url": NDJSON_URL,
            "token": "private-token",
            "tls_ca": PEM,
        })
        self.assertTrue(result["tls_ca_set"])
        self.assertNotIn("tls_ca", result)
        self.assertNotIn("BEGIN CERTIFICATE", json.dumps(result))
        self.assertNotIn("BEGIN CERTIFICATE", json.dumps(settings.public()))
        self.assertEqual(settings.scheme, "https")
        reloaded = config_module.BambuddyConfig(
            path=self.path, random_bytes=FixedRandom())
        self.assertEqual(reloaded.tls_ca, PEM.strip())
        self.assertTrue(reloaded.enabled)
        self.assertEqual(reloaded.scheme, "https")

    def test_tls_scheme_without_ca_or_insecure_is_rejected(self):
        settings = self.settings()
        for url in (NDJSON_URL, "wss://bambuddy.local:8443/ws"):
            with self.assertRaises(ValueError):
                settings.update({
                    "csrf": settings.public()["csrf"],
                    "enabled": True,
                    "url": url,
                    "token": "t",
                })

    def test_stored_tls_config_without_trust_material_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bambuddy.json"
            path.write_text(json.dumps({
                "enabled": True, "url": NDJSON_URL, "token": "x"}))
            settings = config_module.BambuddyConfig(path=str(path))
            self.assertFalse(settings.enabled)
            self.assertEqual(settings.url, "")

    def test_tls_insecure_allows_tls_scheme_and_clear_tls_ca_disarms(self):
        settings = self.settings()
        result = settings.update({
            "csrf": settings.public()["csrf"],
            "enabled": True,
            "url": "wss://bambuddy.local:8443/ws",
            "tls_insecure": True,
            "tls_ca": PEM,
        })
        self.assertTrue(result["tls_insecure"])
        self.assertTrue(result["tls_ca_set"])
        result = settings.update({
            "csrf": result["csrf"],
            "clear_tls_ca": True,
        })
        self.assertFalse(result["tls_ca_set"])
        self.assertEqual(settings.tls_ca, "")
        with self.assertRaises(ValueError):
            settings.update({
                "csrf": result["csrf"],
                "tls_insecure": False,
            })

    def test_ws_scheme_ignores_tls_requirement(self):
        settings = self.settings()
        result = settings.update({
            "csrf": settings.public()["csrf"],
            "enabled": True,
            "url": "ws://bambuddy.local:8000/api/v1/bmcu-link/ws",
            "token": "secret",
        })
        self.assertTrue(result["enabled"])
        self.assertFalse(result["tls_ca_set"])
        self.assertFalse(result["tls_insecure"])
        self.assertEqual(settings.scheme, "ws")

    def test_malformed_or_oversized_tls_material_is_rejected(self):
        settings = self.settings()
        csrf = settings.public()["csrf"]
        with self.assertRaises(ValueError):
            settings.update({"csrf": csrf, "tls_ca": "not a certificate"})
        with self.assertRaises(ValueError):
            settings.update({
                "csrf": settings.public()["csrf"],
                "tls_ca": "-----BEGIN CERTIFICATE-----\n" + "A" * 5000})
        with self.assertRaises(ValueError):
            settings.update({
                "csrf": settings.public()["csrf"], "tls_insecure": "yes"})

    def test_http_fallback_scheme_is_accepted_without_tls_material(self):
        settings = self.settings()
        result = settings.update({
            "csrf": settings.public()["csrf"],
            "enabled": True,
            "url": "http://bambuddy.local:8000/api/v1/bmcu-link/ndjson",
        })
        self.assertTrue(result["enabled"])
        self.assertEqual(settings.scheme, "http")


class CiImportIsolationTests(unittest.TestCase):
    def test_pico_is_never_on_sys_path_so_secrets_stays_the_stdlib(self):
        # pico/secrets.py holds live Wi-Fi credentials and the Bambuddy token
        # and is gitignored; on sys.path it would shadow the stdlib module for
        # every test in this process.
        entries = set()
        for entry in sys.path:
            if entry:
                try:
                    entries.add(Path(entry).resolve())
                except OSError:
                    pass
        self.assertNotIn(PICO.resolve(), entries)
        import secrets as stdlib_secrets
        self.assertTrue(hasattr(stdlib_secrets, "token_bytes"))
        self.assertNotEqual(Path(stdlib_secrets.__file__).resolve(),
                            (PICO / "secrets.py").resolve())


class ChunkedClient:
    """Non-blocking duck client that releases the request in recv-sized bites."""

    def __init__(self, request):
        self.pending = bytes(request)
        self.sent = bytearray()
        self.closed = False

    def recv(self, size):
        if not self.pending:
            raise BlockingIOError(web_module.errno.EAGAIN, "would block")
        chunk, self.pending = self.pending[:size], self.pending[size:]
        return chunk

    def send(self, data):
        self.sent.extend(data)
        return len(data)

    def close(self):
        self.closed = True


def long_pem(body_lines=60):
    return ("-----BEGIN CERTIFICATE-----\n" +
            "".join("M" * 64 + "\n" for _ in range(body_lines)) +
            "-----END CERTIFICATE-----\n")


class CommissioningRequestSizeTests(unittest.TestCase):
    """A real CA PEM must actually fit through the local HTTP endpoint."""

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

    @staticmethod
    def post(body):
        return (b"POST /api/bambuddy/config HTTP/1.1\r\nHost: pico\r\n"
                b"Content-Type: application/json\r\n" +
                ("Content-Length: %d\r\n\r\n" % len(body)).encode() + body)

    @staticmethod
    def drive(web, client):
        web.client = client
        for _ in range(400):
            web.poll()
            if web.response is not None or web.client is None:
                break
        return web.response

    @staticmethod
    def response_json(response):
        raw = b"".join(response.parts)
        return json.loads(raw.split(b"\r\n\r\n", 1)[1].decode())

    def test_full_ca_pem_commissioning_post_is_accepted(self):
        pem = long_pem()
        self.assertLessEqual(len(pem), 4096)
        settings = self.settings()
        web = self.web(settings)
        body = json.dumps({
            "csrf": settings.public()["csrf"],
            "enabled": True,
            "url": NDJSON_URL,
            "token": "private-token",
            "tls_ca": pem,
            "control_key": "a" * 64,
        }).encode()
        # The escaped JSON body is well past the old 2048/3072 byte caps.
        self.assertGreater(len(body), 3072)
        response = self.drive(web, ChunkedClient(self.post(body)))
        self.assertIn(b"200 OK", response)
        self.assertEqual(settings.tls_ca, pem.strip())
        self.assertEqual(settings.scheme, "https")

    def test_oversized_body_is_refused_as_json_not_silence(self):
        settings = self.settings()
        web = self.web(settings)
        body = json.dumps({"csrf": settings.public()["csrf"],
                           "token": "x" * (web_module.MAX_BODY_BYTES + 64)})
        web.request = bytearray(self.post(body.encode()))
        self.assertFalse(web._request_ready())
        self.assertIn(b"413 Payload Too Large", web.response)
        self.assertIn(b"application/json", web.response)
        # The settings page reads r.json(): a text/plain body would surface as
        # nothing happening at all.
        self.assertIn("error", self.response_json(web.response))
        self.assertFalse(settings.enabled)

    def test_oversized_whole_request_is_refused_as_json(self):
        settings = self.settings()
        web = self.web(settings)
        raw = (b"POST /api/bambuddy/config HTTP/1.1\r\nX-Pad: " +
               b"p" * (web_module.MAX_REQUEST_BYTES + 512))
        self.drive(web, ChunkedClient(raw))
        self.assertIn(b"413 Payload Too Large", web.response)
        self.assertIn("error", self.response_json(web.response))


class SettingsPageTests(unittest.TestCase):
    def test_commissioning_page_carries_the_tls_fields(self):
        page = web_module.SETTINGS_PAGE
        for marker in (b"id='tca'", b"id='tins'", b"id='tclr'",
                       b"tls_ca:tca.value", b"tls_insecure:tins.checked",
                       b"clear_tls_ca:tclr.checked", b"x.tls_ca_set",
                       b"tca.value=''"):
            self.assertIn(marker, page)
        # The PEM is write-only: the page never renders a stored certificate.
        self.assertNotIn(b"tca.value=x", page)

    def test_commissioning_page_states_that_ndjson_disables_control(self):
        page = web_module.SETTINGS_PAGE
        self.assertIn(b"CONTROL stays inert on those schemes", page)
        for marker in (b"id='cstate'", b"x.control_active"):
            self.assertIn(marker, page)

    def test_patch_helper_fails_loudly_on_a_silent_non_match(self):
        with self.assertRaises(ValueError):
            web_module._patch_settings(b"page", b"missing", b"new")


if __name__ == "__main__":
    unittest.main()
