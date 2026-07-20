import pathlib
import sys
import unittest


PICO_DIR = pathlib.Path(__file__).parents[1] / "pico"
sys.path.insert(0, str(PICO_DIR))

from web_ui import WebUI, _Response


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
    def test_partial_response_uses_memoryview_without_copying_tail(self):
        response = _Response(b"header", b"x" * 100)
        response.consume(len(b"header"))
        self.assertEqual(response.current(), b"x" * 100)
        response.consume(25)
        tail = response.current()
        self.assertIsInstance(tail, memoryview)
        self.assertEqual(len(tail), 75)

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
            b"GET /api/status HTTP/1.1\r\nHost: pico\r\n\r\n")
        web.poll()

        self.assertIn(b"500 Internal Server Error", web.response)
        self.assertIn(b"internal Pico error", web.response)
        self.assertEqual(reported, [("request", "status failed")])


if __name__ == "__main__":
    unittest.main()
