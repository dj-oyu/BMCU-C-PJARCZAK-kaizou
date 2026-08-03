import contextlib
import importlib.util
import os
import pathlib
import tempfile
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


class ReadIntoClient(Client):
    def readinto(self, target):
        count = min(len(target), len(self.request))
        target[:count] = self.request[:count]
        self.request = self.request[count:]
        return count

    def recv(self, _):
        raise AssertionError("readinto should be used")


class WouldBlockReadIntoClient(ReadIntoClient):
    def readinto(self, _target):
        return None


@contextlib.contextmanager
def staged_index(content):
    """Runs the server against a throwaway littlefs root.

    web_ui resolves INDEX_PATH relative to the working directory, which on the
    device is the filesystem root. Pass None to exercise a missing artifact.
    """
    previous = os.getcwd()
    with tempfile.TemporaryDirectory() as root:
        if content is not None:
            target = pathlib.Path(root) / web_ui.INDEX_PATH
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        os.chdir(root)
        try:
            yield root
        finally:
            os.chdir(previous)


def drain(web, limit=4096):
    """Polls until the response is fully written and the client is closed."""
    client = web.client
    for _ in range(limit):
        web.poll()
        if web.client is None:
            break
    return bytes(client.sent)


class WebUIRuntimeTests(unittest.TestCase):
    def test_listener_allows_a_small_burst_queue(self):
        self.assertEqual(web_ui.HTTP_LISTEN_BACKLOG, 4)

    def test_index_is_streamed_from_littlefs_with_gzip_encoding(self):
        staged = b"\x1f\x8b" + bytes(range(256)) * 6
        with staged_index(staged):
            web = web_ui.WebUI(lambda _: None)
            web.client = Client(b"GET / HTTP/1.1\r\nHost: pico\r\n\r\n")
            sent = drain(web)

        header, _, body = sent.partition(b"\r\n\r\n")
        self.assertIn(b"200 OK", header)
        self.assertIn(b"Content-Encoding: gzip", header)
        self.assertIn(b"Content-Length: %d" % len(staged), header)
        self.assertEqual(body, staged)

    def test_index_response_never_holds_the_whole_page_in_ram(self):
        # The point of streaming: at most one chunk is resident, no matter how
        # large the staged page grows.
        staged = bytes(range(256)) * 40
        with staged_index(staged):
            web = web_ui.WebUI(lambda _: None)
            web.client = Client(b"GET / HTTP/1.1\r\nHost: pico\r\n\r\n")
            web.poll()
            response = web.response
            sizes = []
            while web.client is not None:
                sizes.append(len(response.current()))
                web.poll()

        self.assertGreater(len(sizes), 1)
        self.assertLessEqual(max(sizes), web_ui.FILE_CHUNK_BYTES)

    def test_a_query_string_still_reaches_the_page_and_the_schema(self):
        # The binary routes always stripped it; these two matched exactly, so a
        # link carrying any query answered 404 instead of the UI.
        staged = b"\x1f\x8bpage"
        for target, request in (
            (web_ui.INDEX_PATH, b"GET /?probe=1 HTTP/1.1\r\nHost: pico\r\n\r\n"),
            (web_ui.SCHEMA_PATH,
             b"GET /api/schema.json?v=2 HTTP/1.1\r\nHost: pico\r\n\r\n"),
        ):
            with staged_index(None) as root:
                path = pathlib.Path(root) / target
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(staged)
                web = web_ui.WebUI(lambda _: None)
                web.client = Client(request)
                sent = drain(web)
            self.assertIn(b"200 OK", sent, target)
            self.assertTrue(sent.endswith(staged), target)

    def test_missing_web_asset_answers_with_an_actionable_error(self):
        with staged_index(None):
            web = web_ui.WebUI(lambda _: None)
            web.client = Client(b"GET / HTTP/1.1\r\nHost: pico\r\n\r\n")
            sent = drain(web)

        self.assertIn(b"503 Service Unavailable", sent)
        self.assertIn(b"tools/build_web_ui.py", sent)

    def test_disconnect_closes_the_staged_file_handle(self):
        # littlefs descriptors are a bounded resource; an aborted request must
        # not leak one per connection.
        with staged_index(b"\x1f\x8bstaged"):
            web = web_ui.WebUI(lambda _: None)
            web.client = Client(b"GET / HTTP/1.1\r\nHost: pico\r\n\r\n")
            web.poll()
            handle = web.response.file
            self.assertFalse(handle.closed)
            web._close_client()

        self.assertTrue(handle.closed)

    def test_binary_response_is_not_serialized_or_copied(self):
        payload = bytearray(b"BMB1-payload")
        web = web_ui.WebUI(lambda _: memoryview(payload))
        web.client = Client(
            b"GET /api/current.bin HTTP/1.1\r\nHost: pico\r\n\r\n")
        web.poll()
        self.assertIn(b"application/vnd.bmcu-monitor.v1", web.response)
        self.assertIsInstance(web.response.parts[-1], memoryview)

    def test_request_buffer_is_fixed_and_uses_readinto(self):
        web = web_ui.WebUI(lambda _: b"ok")
        request_buffer = web.request
        web.client = ReadIntoClient(
            b"GET /api/current.bin HTTP/1.1\r\nHost: pico\r\n\r\n")

        web.poll()
        self.assertIs(web.request, request_buffer)
        self.assertGreater(web.request_length, 0)

        for _ in range(4):
            if web.client is None:
                break
            web.poll()
        self.assertIs(web.request, request_buffer)
        self.assertEqual(web.request_length, 0)

    def test_readinto_none_keeps_http_client_open(self):
        web = web_ui.WebUI(lambda _: b"ok")
        client = WouldBlockReadIntoClient(b"")
        web.client = client

        web.poll()

        self.assertIs(web.client, client)
        self.assertFalse(client.closed)
        self.assertEqual(web.request_length, 0)

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

    def test_transport_post_is_routed_to_settings_provider(self):
        calls = []

        def settings(*args):
            calls.append(args)
            return "204 No Content", "text/plain", b""

        body = b"bambuddy.local\n8766"
        web = web_ui.WebUI(lambda _: None, settings_provider=settings)
        web.client = Client(
            b"POST /api/transport HTTP/1.1\r\n"
            b"Content-Type: application/octet-stream\r\n"
            b"X-BMCU-Settings-Action: update\r\n"
            b"Content-Length: 19\r\n\r\n" + body)

        web.poll()

        self.assertEqual(calls[0][0:2], ("POST", "/api/transport"))
        self.assertEqual(calls[0][3], body)
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
