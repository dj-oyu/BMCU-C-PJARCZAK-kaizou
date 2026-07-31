import importlib.util
import pathlib
import subprocess
import sys
import unittest

PICO = pathlib.Path(__file__).parents[1] / "pico"
spec = importlib.util.spec_from_file_location("web_ui_binary", PICO / "web_ui.py")
web_ui = importlib.util.module_from_spec(spec)
spec.loader.exec_module(web_ui)


class Client:
    def __init__(self, request):
        self.request = request
        self.sent = bytearray()
        self.closed = False

    def recv(self, _):
        value, self.request = self.request, b""
        return value

    def send(self, data):
        self.sent.extend(data)
        return len(data)

    def close(self):
        self.closed = True


class WebUIRuntimeTests(unittest.TestCase):
    def test_page_uses_arraybuffer_dataview_and_delta_endpoints(self):
        page = web_ui.PAGE
        self.assertIn(b"arrayBuffer()", page)
        self.assertIn(b"DataView", page)
        self.assertIn(b"/api/diagnostics.bin", page)
        self.assertIn(b"/api/current.bin", page)
        self.assertIn(b"/api/logs.bin?after=", page)
        self.assertNotIn(b"JSON.stringify", page)
        self.assertNotIn(b"/api/devices", page)
        self.assertIn(b"getBigUint64(o).toString()", page)

    def test_page_separates_two_bmcu_links_and_uart_health(self):
        page = web_ui.PAGE

        self.assertIn(b"Dual BMCU Loader Monitor", page)
        self.assertIn(b"getUint8(o+28)", page)
        self.assertIn(b"bmcu-", page)
        self.assertIn(b"GP0 TX / GP1 RX", page)
        self.assertIn(b"GP4 TX / GP5 RX", page)
        self.assertIn(b"Sequence gaps", page)
        self.assertIn(b"Overflows", page)
        self.assertLess(len(page), 15000)

    def test_page_has_write_only_device_key_component(self):
        page = web_ui.PAGE

        self.assertIn(b"BMB1 device key", page)
        self.assertIn(b"crypto.getRandomValues", page)
        self.assertIn(b"/api/device-key/status", page)
        self.assertIn(b"'X-BMCU-Key-Action':'update'", page)
        self.assertIn(b"cannot be read back", page)

    def test_embedded_javascript_has_valid_syntax(self):
        source = web_ui.PAGE.decode().split("<script>", 1)[1].split(
            "</script>", 1)[0]
        result = subprocess.run(
            ["node", "--check", "--input-type=module"], input=source,
            text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_binary_response_is_not_serialized_or_copied(self):
        payload = bytearray(b"BMB1-payload")
        web = web_ui.WebUI(lambda _: memoryview(payload))
        web.client = Client(
            b"GET /api/current.bin HTTP/1.1\r\nHost: pico\r\n\r\n")
        web.poll()
        self.assertIn(b"application/vnd.bmcu-monitor.v1", web.response)
        self.assertIsInstance(web.response.parts[-1], memoryview)

    def test_partial_response_uses_bounded_memoryview_offset(self):
        response = web_ui._Response(b"head", b"0123456789")
        response.consume(6)
        self.assertIsInstance(response.current(), memoryview)
        self.assertEqual(bytes(response.current()), b"23456789")
        self.assertEqual(bytes(response.current(3)), b"234")

    def test_non_binary_api_is_absent(self):
        web = web_ui.WebUI(lambda _: None)
        web.client = Client(
            b"GET /api/devices HTTP/1.1\r\nHost: pico\r\n\r\n")
        web.poll()
        self.assertIn(b"404 Not Found", web.response)

    def test_device_key_post_waits_for_and_passes_complete_body(self):
        calls = []

        def settings(*args):
            calls.append(args)
            return "204 No Content", "text/plain", b""

        web = web_ui.WebUI(lambda _: None, settings_provider=settings)
        client = Client(
            b"POST /api/device-key HTTP/1.1\r\n"
            b"Content-Type: application/octet-stream\r\n"
            b"X-BMCU-Key-Action: update\r\n"
            b"Content-Length: 64\r\n\r\n" + b"a" * 20)
        web.client = client
        web.poll()
        self.assertIsNone(web.response)
        self.assertEqual(calls, [])

        client.request = b"a" * 44
        web.poll()
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0:2], ("POST", "/api/device-key"))
        self.assertEqual(calls[0][2]["x-bmcu-key-action"], "update")
        self.assertEqual(calls[0][3], b"a" * 64)
        self.assertIn(b"204 No Content", web.response)

    def test_device_key_body_is_bounded(self):
        web = web_ui.WebUI(lambda _: None, settings_provider=lambda *_: None)
        web.client = Client(
            b"POST /api/device-key HTTP/1.1\r\n"
            b"Content-Length: 300\r\n\r\n")

        web.poll()

        self.assertIn(b"413 Payload Too Large", web.response)

    def test_provider_error_is_contained(self):
        errors = []
        web = web_ui.WebUI(
            lambda _: (_ for _ in ()).throw(RuntimeError("broken")),
            error_handler=lambda component, error:
                errors.append((component, str(error))))
        web.client = Client(
            b"GET /api/current.bin HTTP/1.1\r\nHost: pico\r\n\r\n")
        web.poll()
        self.assertIn(b"500 Internal Server Error", web.response)
        self.assertEqual(errors, [("request", "broken")])


if __name__ == "__main__":
    unittest.main()
