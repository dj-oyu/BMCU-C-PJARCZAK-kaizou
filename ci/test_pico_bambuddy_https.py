import importlib.util
import json
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
PICO = ROOT / "pico"


def load(name, path):
    """Load a pico module by path, registering it for its dependents.

    ``pico/`` is deliberately never put on ``sys.path``: the gitignored
    ``pico/secrets.py`` would otherwise shadow the CPython stdlib ``secrets``
    module for the whole CI process.
    """
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


core = load("bambuddy_transport", PICO / "bambuddy_transport.py")
load("bambuddy_session", PICO / "bambuddy_session.py")
load("bambuddy_ws", PICO / "bambuddy_ws.py")
https = load("bambuddy_https", PICO / "bambuddy_https.py")


def http_response(status_line, obj, close=False, content_length=None):
    body = json.dumps(obj).encode()
    length = len(body) if content_length is None else content_length
    head = ("HTTP/1.1 " + status_line + "\r\n"
            "Content-Type: application/json\r\n"
            "Content-Length: " + str(length) + "\r\n")
    if close:
        head += "Connection: close\r\n"
    return head.encode() + b"\r\n" + body


def http_ack(obj=None, close=False):
    if obj is None:
        obj = {"type": "ack", "accepted": 1, "persisted": [], "rejected": []}
    return http_response("200 OK", obj, close)


def request_body(raw):
    return raw.split(b"\r\n\r\n", 1)[1]


def body_lines(raw):
    return [line for line in request_body(raw).split(b"\n") if line]


def envelopes(raw):
    return [json.loads(line) for line in body_lines(raw)]


class FakeSocket:
    """Duck socket that releases one scripted response per complete request."""

    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.sent = bytearray()
        self.requests = []
        self.closed = False
        self._buffer = bytearray()
        self._pending = []
        self.max_recv_backlog = 0

    def setblocking(self, _value):
        pass

    def send(self, data):
        self.sent.extend(data)
        self._buffer.extend(data)
        self._split()
        return len(data)

    def _split(self):
        while True:
            marker = self._buffer.find(b"\r\n\r\n")
            if marker < 0:
                return
            head = bytes(self._buffer[:marker]).decode()
            length = 0
            for line in head.split("\r\n")[1:]:
                name, separator, value = line.partition(":")
                if separator and name.strip().lower() == "content-length":
                    length = int(value.strip())
            end = marker + 4 + length
            if len(self._buffer) < end:
                return
            self.requests.append(bytes(self._buffer[:end]))
            self._buffer = bytearray(self._buffer[end:])
            if self.responses:
                self._pending.append(self.responses.pop(0))

    def push(self, data):
        """Deliver bytes the client never asked for (idle 408, EOF, ...)."""
        self._pending.append(data)

    def recv(self, _size):
        if not self._pending:
            raise BlockingIOError(11, "would block")
        return self._pending.pop(0)

    def close(self):
        self.closed = True


class Factory:
    def __init__(self, *sockets):
        self.sockets = list(sockets)
        self.calls = []

    def __call__(self, host, port):
        self.calls.append((host, port))
        return self.sockets.pop(0) if self.sockets else FakeSocket()


class NdjsonTests(unittest.TestCase):
    def random(self, size):
        return bytes([1]) * size

    def outbox(self):
        return core.BambuddyOutbox("bridge-a", boot_session="pico-boot")

    def client(self, outbox=None, socket_factory=None, **kwargs):
        kwargs.setdefault("request_interval_ms", 0)
        return https.BambuddyNdjsonClient(
            outbox or self.outbox(),
            "https://bambuddy.local:8443/api/v1/bmcu-link/ndjson",
            "secret", socket_factory=socket_factory,
            random_bytes=self.random, tls_insecure=True, **kwargs)

    def drive(self, client, polls, start=0, step=1):
        now = start
        for _ in range(polls):
            client.poll(now, True)
            now += step
        return now

    # ------------------------------------------------------------------ wire

    def test_hello_post_precedes_telemetry_and_uses_bearer_header(self):
        outbox = self.outbox()
        outbox.publish({"type": "status", "link_id": "a"}, 1, 1000)
        fake = FakeSocket([http_ack(), http_ack()])
        client = self.client(outbox, Factory(fake))
        self.drive(client, 6)
        self.assertEqual(len(fake.requests), 2)
        first = fake.requests[0]
        start_line = first.split(b"\r\n")[0]
        self.assertEqual(
            start_line, b"POST /api/v1/bmcu-link/ndjson HTTP/1.1")
        self.assertIn(b"Host: bambuddy.local:8443\r\n", first)
        self.assertIn(b"Authorization: Bearer secret\r\n", first)
        self.assertIn(b"Content-Type: application/x-ndjson\r\n", first)
        # The token is a header, never part of the request target.
        self.assertNotIn(b"secret", start_line)
        hello = envelopes(first)
        self.assertEqual(len(hello), 1)
        self.assertEqual(hello[0]["frame"]["kind"], "hello")
        self.assertEqual(hello[0]["data"]["scope"], "bmcu_link:telemetry")
        self.assertEqual(envelopes(fake.requests[1])[0]["frame"]["kind"],
                         "status")

    def test_batch_is_ndjson_lines_in_queue_order(self):
        outbox = self.outbox()
        outbox.publish({"type": "event", "link_id": "a"}, 1, 1000)
        outbox.publish({"type": "event", "link_id": "b"}, 2, 2000)
        outbox.publish({"type": "event", "link_id": "c"}, 3, 3000)
        expected = [envelope["link"]["id"]
                    for envelope in outbox.queue.batch()]
        fake = FakeSocket([http_ack(), http_ack()])
        client = self.client(outbox, Factory(fake))
        self.drive(client, 6)
        batch = fake.requests[1]
        lines = body_lines(batch)
        self.assertEqual(len(lines), 3)
        self.assertEqual([json.loads(line)["link"]["id"] for line in lines],
                         expected)
        header = batch.split(b"\r\n\r\n", 1)[0].decode()
        declared = [line for line in header.split("\r\n")
                    if line.lower().startswith("content-length")][0]
        self.assertEqual(int(declared.split(":")[1]),
                         len(request_body(batch)))

    def test_url_parser_accepts_https_but_ws_parser_still_refuses_it(self):
        endpoint = https.parse_endpoint_url("https://host/ndjson", ("https",))
        self.assertEqual(endpoint["port"], 443)
        endpoint = https.parse_endpoint_url("http://host:8080/x", ("http",))
        self.assertEqual(endpoint["port"], 8080)
        with self.assertRaises(ValueError):
            https.parse_endpoint_url("ws://host/x", ("https", "http"))

    def test_token_with_crlf_is_rejected(self):
        with self.assertRaises(ValueError):
            https.BambuddyNdjsonClient(
                self.outbox(), "https://host/ndjson",
                "bad\r\nX-Injected: 1", tls_insecure=True)

    # ------------------------------------------------------- delivery semantics

    def test_queue_advances_only_on_persisted_watermark(self):
        outbox = self.outbox()
        outbox.publish({"type": "event", "link_id": "a"}, 1, 1000)
        outbox.publish({"type": "event", "link_id": "b"}, 2, 2000)
        ack = {"type": "ack", "accepted": 1, "rejected": [], "persisted": [{
            "link_id": "a", "pico_boot_session": "pico-boot",
            "transport_sequence": 0}]}
        fake = FakeSocket([http_ack(), http_ack(ack), http_ack()])
        client = self.client(outbox, Factory(fake))
        self.drive(client, 6)
        self.assertEqual(len(outbox.queue), 1)
        self.assertEqual(outbox.queue.records[0]["envelope"]["link"]["id"], "b")
        self.drive(client, 4, start=6)
        self.assertEqual(envelopes(fake.requests[2])[0]["link"]["id"], "b")

    def test_accepted_only_ack_releases_batch_and_resets_backoff(self):
        outbox = self.outbox()
        outbox.publish({"type": "status", "link_id": "a"}, 1, 1000)
        outbox.publish({"type": "event", "link_id": "b"}, 2, 2000)
        ack = {"type": "ack", "accepted": 2, "deduplicated": 0,
               "persisted": [], "rejected": []}
        fake = FakeSocket([http_ack(), http_ack(ack)])
        client = self.client(outbox, Factory(fake))
        client._backoff_index = 3
        self.drive(client, 6)
        self.assertEqual(len(outbox.queue), 0)
        self.assertIsNone(client._inflight)
        self.assertEqual(client._backoff_index, 0)

    def test_rejected_index_is_enriched_and_quarantined(self):
        outbox = self.outbox()
        outbox.publish({"type": "status", "link_id": "a"}, 1, 1000)
        outbox.publish({"type": "status", "link_id": "b"}, 2, 2000)
        ack = {"type": "ack", "accepted": 1,
               "persisted": [{"link_id": "a", "pico_boot_session": "pico-boot",
                              "transport_sequence": 0}],
               "rejected": [{"index": 1, "transport_sequence": 0,
                             "code": "bad", "retryable": False}]}
        fake = FakeSocket([http_ack(), http_ack(ack)])
        client = self.client(outbox, Factory(fake))
        self.drive(client, 6)
        self.assertEqual(len(outbox.queue), 0)
        self.assertEqual(outbox.queue.quarantined[0]["identity"][0], "b")

    def test_retryable_rejection_stays_queued_and_is_resent(self):
        outbox = self.outbox()
        outbox.publish({"type": "event", "link_id": "a"}, 1, 1000)
        ack = {"type": "ack", "accepted": 0, "persisted": [],
               "rejected": [{"index": 0, "code": "busy", "retryable": True}]}
        fake = FakeSocket([http_ack(), http_ack(ack), http_ack()])
        client = self.client(outbox, Factory(fake))
        self.drive(client, 6)
        self.assertEqual(len(outbox.queue), 1)
        self.assertEqual(outbox.queue.quarantined, [])
        # A zero-progress ACK paces the replay instead of hot-looping.
        self.drive(client, 2, start=10)
        self.assertEqual(len(fake.requests), 2)
        self.drive(client, 4, start=3000)
        self.assertEqual(len(fake.requests), 3)
        self.assertEqual(envelopes(fake.requests[2])[0]["link"]["id"], "a")

    # ------------------------------------------------------------- failures

    def test_http_500_backs_off_and_replays_oldest_with_new_hello(self):
        outbox = self.outbox()
        outbox.publish({"type": "event", "link_id": "a"}, 1, 1000)
        sequence = outbox.queue.batch()[0]["link"]["transport_sequence"]
        first = FakeSocket([http_ack(),
                            http_response("500 Internal Server Error",
                                          {"error": "boom"})])
        second = FakeSocket([http_ack(), http_ack()])
        factory = Factory(first, second)
        client = self.client(outbox, factory)
        self.drive(client, 6)
        self.assertEqual(client.state, "backoff")
        self.assertEqual(len(outbox.queue), 1)
        self.assertEqual(len(factory.calls), 1)
        self.assertTrue(first.closed)
        self.drive(client, 6, start=client._retry_at)
        self.assertEqual(len(factory.calls), 2)
        self.assertEqual(envelopes(second.requests[0])[0]["frame"]["kind"],
                         "hello")
        replayed = envelopes(second.requests[1])[0]
        self.assertEqual(replayed["link"]["id"], "a")
        self.assertEqual(replayed["link"]["transport_sequence"], sequence)

    def test_401_redacts_token_in_last_error(self):
        fake = FakeSocket([http_response("401 Unauthorized",
                                         {"error": "bad secret"})])
        client = self.client(None, Factory(fake))
        self.drive(client, 4)
        self.assertEqual(client.state, "backoff")
        self.assertIn("authentication", client.last_error)
        self.assertNotIn("secret", client.last_error)
        self.assertNotIn("secret", str(client.status()))

    def test_ack_timeout_enters_bounded_backoff_without_dropping_fifo(self):
        outbox = self.outbox()
        outbox.publish({"type": "event", "link_id": "a"}, 1, 1000)
        fake = FakeSocket([http_ack()])
        client = self.client(outbox, Factory(fake))
        self.drive(client, 5)
        self.assertEqual(client.state, "response")
        self.assertIsNotNone(client._inflight)
        base = client._request_at
        client.poll(base + client.ack_timeout_ms, True)
        self.assertEqual(client.state, "backoff")
        self.assertIn("timeout", client.last_error)
        self.assertEqual(len(outbox.queue), 1)
        self.assertGreaterEqual(client._retry_at,
                                base + client.ack_timeout_ms + 1000)
        self.assertLessEqual(client._retry_at,
                             base + client.ack_timeout_ms + 1250)

    def test_hello_rejected_by_ack_is_not_persisted_and_is_reused(self):
        rejected = {"type": "ack", "accepted": 0, "persisted": [],
                    "rejected": [{"index": 0, "code": "bad_scope",
                                  "retryable": False}]}
        first = FakeSocket([http_response("200 OK", rejected, close=True)])
        second = FakeSocket([http_ack()])
        factory = Factory(first, second)
        client = self.client(None, factory)
        self.drive(client, 6)
        # An ACK that rejects the HELLO is not a durable HELLO: the same
        # envelope (and its transport_sequence) must be replayed verbatim.
        self.assertFalse(client._hello_persisted)
        original = envelopes(first.requests[0])[0]["link"]
        self.drive(client, 6, start=client._retry_at)
        self.assertEqual(len(factory.calls), 2)
        replayed = envelopes(second.requests[0])[0]["link"]
        self.assertEqual(replayed["transport_sequence"],
                         original["transport_sequence"])
        self.assertEqual(replayed["pico_boot_session"],
                         original["pico_boot_session"])
        # Only a durable ACK rotates it.
        self.assertTrue(client._hello_persisted)

    def test_unpersisted_hello_is_reused_then_rotates_after_persist(self):
        first = FakeSocket([http_response("503 Unavailable", {"error": "x"})])
        second = FakeSocket([http_ack()])
        factory = Factory(first, second)
        client = self.client(None, factory)
        self.drive(client, 4)
        self.assertEqual(client.state, "backoff")
        original = client._hello()["link"]["transport_sequence"]
        self.drive(client, 4, start=client._retry_at)
        self.assertEqual(envelopes(second.requests[0])[0]["link"]
                         ["transport_sequence"], original)
        self.assertTrue(client._hello_persisted)
        client._close()
        self.assertGreater(client._hello()["link"]["transport_sequence"],
                           original)

    def test_connection_close_triggers_clean_reconnect(self):
        first = FakeSocket([http_ack(close=True)])
        second = FakeSocket([http_ack()])
        factory = Factory(first, second)
        client = self.client(None, factory)
        self.drive(client, 6)
        # An idle graceful close is paced: never an unpaced TLS reconnect loop.
        self.assertEqual(len(factory.calls), 1)
        self.assertGreaterEqual(client._retry_at, https.MIN_RECONNECT_MS)
        self.drive(client, 4, start=client._retry_at)
        self.assertEqual(len(factory.calls), 2)
        self.assertTrue(first.closed)
        self.assertEqual(client._backoff_index, 0)
        self.assertEqual(envelopes(second.requests[0])[0]["frame"]["kind"],
                         "hello")

    def test_chunked_response_is_rejected(self):
        raw = (b"HTTP/1.1 200 OK\r\n"
               b"Content-Type: application/json\r\n"
               b"Transfer-Encoding: chunked\r\n\r\n"
               b"2\r\n{}\r\n0\r\n\r\n")
        client = self.client(None, Factory(FakeSocket([raw])))
        self.drive(client, 4)
        self.assertEqual(client.state, "backoff")
        self.assertIn("chunked", client.last_error)

    def test_response_without_content_length_is_rejected(self):
        raw = (b"HTTP/1.1 200 OK\r\n"
               b"Content-Type: application/json\r\n\r\n"
               b'{"type": "ack", "accepted": 1}')
        client = self.client(None, Factory(FakeSocket([raw])))
        self.drive(client, 4)
        self.assertEqual(client.state, "backoff")
        self.assertIn("Content-Length", client.last_error)

    def test_malformed_status_line_is_rejected(self):
        raw = (b"HELLO\r\nContent-Length: 2\r\n\r\n{}")
        client = self.client(None, Factory(FakeSocket([raw])))
        self.drive(client, 4)
        self.assertEqual(client.state, "backoff")
        self.assertIn("status line", client.last_error)

    def test_header_block_over_the_bound_is_rejected(self):
        raw = (b"HTTP/1.1 200 OK\r\nX-Pad: " +
               b"p" * (https.MAX_HEADER_BYTES + 64))
        fake = FakeSocket([raw])
        client = self.client(None, Factory(fake))
        self.drive(client, 6)
        self.assertEqual(client.state, "backoff")
        self.assertIn("header too large", client.last_error)
        # The bounded buffer is released rather than growing with the peer.
        self.assertEqual(len(client._response), 0)
        self.assertTrue(fake.closed)

    def test_404_reports_the_feature_as_disabled(self):
        fake = FakeSocket([http_response("404 Not Found", {"error": "no"})])
        client = self.client(None, Factory(fake))
        self.drive(client, 4)
        self.assertEqual(client.state, "backoff")
        self.assertIn("feature disabled", client.last_error)

    # ------------------------------------------------- keep-alive recycling

    def test_idle_unsolicited_data_recycles_without_growing_the_backoff(self):
        first = FakeSocket([http_ack()])
        second = FakeSocket([http_ack()])
        factory = Factory(first, second)
        client = self.client(None, factory)
        self.drive(client, 3)
        self.assertEqual(client.state, "online")
        # A proxy answering 408 just before it closes an idle keep-alive
        # connection is not a data problem: no request is in flight.
        first.push(b"HTTP/1.1 408 Request Timeout\r\nContent-Length: 0\r\n\r\n")
        client.poll(3, True)
        self.assertEqual(client.state, "backoff")
        self.assertEqual(client._backoff_index, 0)
        self.assertIsNone(client.last_error)
        self.assertTrue(first.closed)
        self.assertGreaterEqual(client._retry_at, https.MIN_RECONNECT_MS)
        self.drive(client, 4, start=client._retry_at)
        self.assertEqual(len(factory.calls), 2)
        self.assertEqual(client.state, "online")

    def test_in_flight_corruption_still_fails_and_backs_off(self):
        # _fail semantics are kept for data problems while a request is in
        # flight, so the idle-recycle path cannot mask a broken server.
        fake = FakeSocket([b"HTTP/1.1 200 OK\r\nContent-Length: 4\r\n\r\nnope"])
        client = self.client(None, Factory(fake))
        self.drive(client, 4)
        self.assertEqual(client.state, "backoff")
        self.assertEqual(client._backoff_index, 1)
        self.assertIsNotNone(client.last_error)

    def test_close_after_every_response_still_delivers_telemetry(self):
        outbox = self.outbox()
        outbox.publish({"type": "event", "link_id": "a"}, 1, 1000)
        both = {"type": "ack", "accepted": 2, "persisted": [], "rejected": []}
        first = FakeSocket([http_ack(close=True)])
        second = FakeSocket([http_ack(both, close=True)])
        factory = Factory(first, second)
        client = self.client(outbox, factory)
        self.drive(client, 6)
        # HELLO alone was answered and the server closed. If every reconnect
        # only re-sent HELLO the queue would age out after 30 s.
        self.assertEqual(len(factory.calls), 1)
        self.assertEqual(len(outbox.queue), 1)
        self.assertTrue(client._pipeline_hello)
        self.drive(client, 6, start=client._retry_at)
        self.assertEqual(len(factory.calls), 2)
        # The batch rides in the same NDJSON body, right behind HELLO.
        body = envelopes(second.requests[0])
        self.assertEqual([item["frame"]["kind"] for item in body],
                         ["hello", "event"])
        self.assertEqual(len(outbox.queue), 0)
        self.assertEqual(client._backoff_index, 0)

    def test_no_progress_connections_grow_the_backoff_index(self):
        outbox = self.outbox()
        outbox.publish({"type": "event", "link_id": "a"}, 1, 1000)
        zero = {"type": "ack", "accepted": 0, "persisted": [], "rejected": []}
        factory = Factory(*[FakeSocket([http_ack(zero, close=True)])
                            for _ in range(3)])
        client = self.client(outbox, factory)
        self.drive(client, 6)
        # The first graceful close is only paced, not penalised.
        self.assertEqual(client._backoff_index, 0)
        self.assertTrue(client._pipeline_hello)
        paced = client._retry_at
        self.drive(client, 6, start=paced)
        # The pipelined retry delivered nothing durable either: consecutive
        # no-progress connections are the slow-motion failure they look like.
        self.assertEqual(len(factory.calls), 2)
        self.assertEqual(client._backoff_index, 1)
        self.assertGreaterEqual(client._retry_at,
                                paced + client.BACKOFF_MS[0])
        self.assertEqual(len(outbox.queue), 1)

    def test_oversized_response_is_rejected(self):
        fake = FakeSocket([http_response("200 OK", {"type": "ack"},
                                         content_length=999999)])
        client = self.client(None, Factory(fake))
        self.drive(client, 4)
        self.assertEqual(client.state, "backoff")
        self.assertIn("too large", client.last_error)
        self.assertEqual(len(client._response), 0)

    def test_would_block_never_errors_and_no_request_when_idle(self):
        fake = FakeSocket([http_ack()])
        client = self.client(None, Factory(fake))
        self.drive(client, 3)
        self.assertEqual(client.state, "online")
        self.drive(client, 20, start=100, step=100)
        self.assertEqual(client.state, "online")
        # No ping frame exists in HTTP mode: an idle queue sends nothing.
        self.assertEqual(len(fake.requests), 1)
        self.assertIsNone(client.last_error)

    def test_wifi_loss_closes_session_and_rearms(self):
        fake = FakeSocket([http_ack()])
        client = self.client(None, Factory(fake))
        self.drive(client, 3)
        client.poll(10, False)
        self.assertEqual(client.state, "wifi_wait")
        self.assertTrue(fake.closed)
        self.assertIsNone(client.sock)

    def test_status_shape_matches_the_websocket_adapter(self):
        client = self.client(None, Factory(FakeSocket()))
        self.assertEqual(
            sorted(client.status()),
            ["dropped_count", "last_error", "pico_boot_session",
             "queue_depth", "state"])


if __name__ == "__main__":
    unittest.main()
