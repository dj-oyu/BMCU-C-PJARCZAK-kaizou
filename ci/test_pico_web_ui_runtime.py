import importlib.util
import pathlib
import sys
import unittest


PICO_DIR = pathlib.Path(__file__).parents[1] / "pico"

# ``pico/`` is deliberately never put on ``sys.path``: the gitignored
# ``pico/secrets.py`` would otherwise shadow the CPython stdlib ``secrets``
# module for the whole CI process.
_SPEC = importlib.util.spec_from_file_location(
    "web_ui", PICO_DIR / "web_ui.py")
web_ui = importlib.util.module_from_spec(_SPEC)
sys.modules["web_ui"] = web_ui
_SPEC.loader.exec_module(web_ui)

PAGE = web_ui.PAGE
WebUI = web_ui.WebUI
_JsonChunks = web_ui._JsonChunks
_Response = web_ui._Response
_write_json = web_ui._write_json


class RequestClient:
    def __init__(self, request):
        self.request = request
        self.closed = False

    def recv(self, _size):
        request, self.request = self.request, b""
        return request

    def send(self, data):
        return len(data)

    def close(self):
        self.closed = True


class WebUIRuntimeTests(unittest.TestCase):
    def test_json_is_encoded_in_bounded_chunks(self):
        value = {"payload": "x" * 1500, "items": [True, None, 7]}
        writer = _JsonChunks(chunk_size=128)
        _write_json(writer, value)
        parts, length = writer.finish()
        encoded = b"".join(parts)
        self.assertEqual(length, len(encoded))
        self.assertTrue(all(len(part) <= 128 for part in parts))
        self.assertEqual(__import__("json").loads(encoded), value)

    def test_partial_response_uses_memoryview_without_copying_tail(self):
        response = _Response(b"header", b"x" * 100)
        response.consume(len(b"header"))
        self.assertEqual(response.current(), b"x" * 100)
        response.consume(25)
        tail = response.current()
        self.assertIsInstance(tail, bytes)
        self.assertEqual(len(tail), 75)

    def test_accept_checks_each_listener(self):
        class EmptyServer:
            def accept(self):
                raise OSError("would block")

        class ReadyServer:
            def __init__(self, client):
                self.client = client

            def accept(self):
                return self.client, ("peer", 1)

        client = RequestClient(b"")
        client.setblocking = lambda _value: None
        web = WebUI(lambda _path: {"ok": True})
        web.servers = [EmptyServer(), ReadyServer(client)]
        web.poll()
        self.assertIs(web.client, client)
        self.assertIsNotNone(web.client_deadline_ms)

    def test_sendall_flushes_all_response_chunks(self):
        class SendAllClient(RequestClient):
            def __init__(self):
                super().__init__(b"")
                self.sent = []
                self.timeout = None

            def settimeout(self, value):
                self.timeout = value

            def sendall(self, data):
                self.sent.append(bytes(data))

            def shutdown(self, _direction):
                pass

        web = WebUI(lambda _path: {"ok": True})
        client = SendAllClient()
        web.client = client
        web.response = _Response(b"header", [b"a" * 512, b"tail"])
        web.poll()
        self.assertEqual(client.sent, [b"header", b"a" * 512, b"tail"])
        self.assertEqual(client.timeout, 1)
        self.assertIsNone(web.response)
        self.assertIsNotNone(web.close_at_ms)

    def test_zero_byte_send_closes_dead_client(self):
        class ZeroSendClient(RequestClient):
            def send(self, _data):
                return 0

        web = WebUI(lambda _path: {"ok": True})
        client = ZeroSendClient(b"")
        web.client = client
        web.response = _Response(b"header", b"body")
        web.poll()
        self.assertTrue(client.closed)
        self.assertIsNone(web.client)

    def test_provider_exception_returns_500_and_is_reported(self):
        reported = []

        def broken_provider(_path):
            raise RuntimeError("status failed")

        web = WebUI(
            broken_provider,
            error_handler=lambda component, error:
                reported.append((component, str(error))),
        )
        web.client = RequestClient(
            b"GET /api/devices HTTP/1.1\r\nHost: pico\r\n\r\n")
        web.poll()

        self.assertIn(b"500 Internal Server Error", web.response)
        self.assertIn(b"internal Pico error", web.response)
        self.assertEqual(reported, [("request", "status failed")])

    def test_dashboard_is_multi_device(self):
        # The page polls the aggregate and renders one section per link.
        self.assertIn(b"fetch('/api/devices'", PAGE)
        self.assertNotIn(b"/api/status", PAGE)
        # The soft-reset button targets the clicked link, never a fixed one.
        self.assertIn(b"'/api/devices/'+link+'/soft-reset'", PAGE)
        self.assertNotIn(b"/api/devices/bmcu-a/soft-reset", PAGE)


if __name__ == "__main__":
    unittest.main()
