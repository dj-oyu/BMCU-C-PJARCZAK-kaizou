import importlib.util
import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


core = load("bambuddy_transport", ROOT / "pico" / "bambuddy_transport.py")
ws = load("bambuddy_ws", ROOT / "pico" / "bambuddy_ws.py")


def server_frame(payload, opcode=1):
    if isinstance(payload, str):
        payload = payload.encode()
    assert len(payload) < 126
    return bytes((0x80 | opcode, len(payload))) + payload


def decode_client_frame(frame):
    second = frame[1]
    assert second & 0x80
    length = second & 0x7F
    offset = 2
    if length == 126:
        length = int.from_bytes(frame[2:4], "big")
        offset = 4
    mask = frame[offset:offset + 4]
    payload = frame[offset + 4:offset + 4 + length]
    return bytes(value ^ mask[index & 3] for index, value in enumerate(payload))


class FakeSocket:
    def __init__(self, incoming=None):
        self.incoming = list(incoming or [])
        self.sent = bytearray()
        self.closed = False

    def setblocking(self, _value):
        pass

    def send(self, data):
        self.sent.extend(data)
        return len(data)

    def recv(self, _size):
        if not self.incoming:
            raise BlockingIOError(11, "would block")
        return self.incoming.pop(0)

    def close(self):
        self.closed = True


class WebSocketTests(unittest.TestCase):
    def random(self, size):
        return bytes([1]) * size

    def outbox(self):
        return core.BambuddyOutbox("bridge-a", boot_session="pico-boot")

    def client(self, outbox=None, socket_factory=None):
        return ws.BambuddyWebSocketClient(
            outbox or self.outbox(),
            "ws://bambuddy.local:8000/api/v1/bmcu-link/ws",
            "secret", socket_factory=socket_factory, random_bytes=self.random)

    def test_url_parser_rejects_non_lan_transport_schemes(self):
        endpoint = ws.parse_ws_url("ws://host:8000/path?q=1")
        self.assertEqual(endpoint["host"], "host")
        self.assertEqual(endpoint["port"], 8000)
        self.assertEqual(endpoint["path"], "/path?q=1")
        with self.assertRaises(ValueError):
            ws.parse_ws_url("https://host/path")
        with self.assertRaises(ValueError):
            ws.parse_ws_url("ws://host/path\r\nInjected: value")
        self.assertEqual(ws._quote("a b&c"), "a%20b%26c")

    def test_client_frames_are_masked_and_round_trip(self):
        frame = ws.client_frame("hello", mask=b"1234")
        self.assertTrue(frame[1] & 0x80)
        self.assertEqual(decode_client_frame(frame), b"hello")

    def test_transport_hello_lists_observed_link_sessions(self):
        outbox = self.outbox()
        outbox.publish({
            "type": "hello", "link_id": "bmcu-a",
            "bmcu_boot_session": 3,
        }, 1, 1000)
        hello = self.client(outbox)._hello()
        self.assertEqual(hello["frame"]["kind"], "hello")
        self.assertEqual(hello["data"]["links"][0]["link_id"], "bmcu-a")
        self.assertEqual(hello["data"]["links"][0]["bmcu_boot_session"], 3)

    def test_unpersisted_transport_hello_is_reused_on_reconnect(self):
        client = self.client()
        first = client._hello()
        client._close()
        self.assertIs(client._hello(), first)
        link = first["link"]
        client._handle_message(json.dumps({
            "type": "ack",
            "persisted": [{
                "link_id": link["id"],
                "pico_boot_session": link["pico_boot_session"],
                "transport_sequence": link["transport_sequence"],
            }],
        }).encode())
        client._close()
        second = client._hello()
        self.assertGreater(second["link"]["transport_sequence"],
                           first["link"]["transport_sequence"])


    def test_server_parser_handles_split_input_and_ping(self):
        parser = ws.ServerFrameParser()
        encoded = server_frame("ok")
        self.assertEqual(parser.feed(encoded[:1]), [])
        self.assertEqual(parser.feed(encoded[1:]), [(1, b"ok")])
        self.assertEqual(parser.feed(server_frame(b"x", 9)), [(9, b"x")])

    def test_telemetry_message_is_envelope_array_without_wrapper(self):
        outbox = self.outbox()
        outbox.publish({"type": "status", "link_id": "a"}, 1, 1000)
        fake = FakeSocket()
        client = self.client(outbox, lambda _host, _port: fake)
        client.sock = fake
        client.state = "online"
        client._hello_sent = True
        client.poll(1, True)
        client.poll(2, True)
        message = json.loads(decode_client_frame(bytes(fake.sent)))
        self.assertIsInstance(message, list)
        self.assertEqual(message[0]["frame"]["kind"], "status")



    def test_idle_socket_uses_ping_and_liveness_deadline(self):
        fake = FakeSocket()
        client = self.client(socket_factory=lambda _host, _port: fake)
        client.sock = fake
        client.state = "online"
        client._hello_sent = True
        client.poll(0, True)
        client.poll(30000, True)
        client.poll(30001, True)
        frame = bytes(fake.sent)
        self.assertEqual(frame[0] & 0x0F, 9)
        client.poll(40000, True)
        self.assertEqual(client.state, "backoff")
        self.assertIn("liveness", client.last_error)


    def test_handshake_is_validated_and_token_is_only_in_request(self):
        key = ws._b64(self.random(16))
        response = (
            "HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            "Sec-WebSocket-Accept: " + ws.websocket_accept(key) + "\r\n\r\n"
        ).encode()
        fake = FakeSocket([response])
        client = self.client(socket_factory=lambda _host, _port: fake)
        client.poll(0, True)
        self.assertEqual(client.state, "online")
        request = bytes(fake.sent)
        self.assertIn(b"token=secret", request)
        self.assertNotIn(b"secret", str(client.status()).encode())

    def test_rejected_index_is_enriched_before_queue_ack(self):
        outbox = self.outbox()
        outbox.publish({"type": "status", "link_id": "a"}, 1, 1000)
        outbox.publish({"type": "status", "link_id": "b"}, 2, 2000)
        client = self.client(outbox)
        client._inflight = outbox.queue.batch()
        client._handle_message((
            '{"type":"ack","persisted":[{"link_id":"a",'
            '"pico_boot_session":"pico-boot","transport_sequence":0}],'
            '"rejected":[{"index":1,"transport_sequence":0,'
            '"code":"bad","retryable":false}]}'
        ).encode())
        self.assertEqual(len(outbox.queue), 0)
        self.assertEqual(outbox.queue.quarantined[0]["identity"][0], "b")

    def test_ack_timeout_enters_bounded_backoff_without_dropping_fifo(self):
        outbox = self.outbox()
        outbox.publish({"type": "event", "link_id": "a"}, 1, 1000)
        fake = FakeSocket()
        client = self.client(outbox, lambda _host, _port: fake)
        client.sock = fake
        client.state = "online"
        client._hello_sent = True
        client._inflight = outbox.queue.batch()
        client._inflight_at = 0
        client.poll(10000, True)
        self.assertEqual(client.state, "backoff")
        self.assertEqual(len(outbox.queue), 1)
        self.assertGreaterEqual(client._retry_at, 11000)
        self.assertLessEqual(client._retry_at, 11250)


if __name__ == "__main__":
    unittest.main()
