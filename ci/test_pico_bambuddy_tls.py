import importlib.util
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
tls = load("bambuddy_tls", PICO / "bambuddy_tls.py")
load("bambuddy_session", PICO / "bambuddy_session.py")
ws = load("bambuddy_ws", PICO / "bambuddy_ws.py")


PEM = "-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n"


class SSLWantReadError(Exception):
    """Mirrors the CPython class name the wrapper normalizes."""


class SSLWantWriteError(Exception):
    pass


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


class FakeSSLSocket:
    def __init__(self, sock, want=0, never=False):
        self.sock = sock
        self.want = want
        self.never = never
        self.handshakes = 0
        self.read_none = False
        self.write_none = False

    def do_handshake(self):
        self.handshakes += 1
        if self.never or self.want > 0:
            self.want -= 1
            raise SSLWantReadError("want read")

    def send(self, data):
        if self.write_none:
            return None
        return self.sock.send(data)

    def recv(self, size):
        if self.read_none:
            return None
        return self.sock.recv(size)

    def close(self):
        self.sock.close()


class FakeContext:
    def __init__(self, protocol):
        self.protocol = protocol
        self.check_hostname = True
        self.verify_mode = None
        self.cadata = None
        self.wrap_args = None

    def load_verify_locations(self, cadata=None):
        self.cadata = cadata

    def wrap_socket(self, sock, server_hostname=None,
                    do_handshake_on_connect=True):
        self.wrap_args = (server_hostname, do_handshake_on_connect)
        return FakeSSLSocket(sock)


class LegacyContext(FakeContext):
    """A build whose wrap_socket cannot defer the handshake."""

    def wrap_socket(self, sock, server_hostname=None,
                    do_handshake_on_connect=True):
        raise TypeError("unexpected keyword argument "
                        "'do_handshake_on_connect'")


class FakeSSLModule:
    PROTOCOL_TLS_CLIENT = "tls-client"
    CERT_NONE = 0
    CERT_REQUIRED = 2

    def __init__(self, context_class=None):
        self.contexts = []
        self.context_class = context_class or FakeContext

    def SSLContext(self, protocol):
        context = self.context_class(protocol)
        self.contexts.append(context)
        return context


class TLSSocketTests(unittest.TestCase):
    def test_handshake_want_read_maps_to_would_block_one_step_per_call(self):
        inner = FakeSSLSocket(FakeSocket(), want=2)
        sock = tls.TLSSocket(inner)
        for expected in (1, 2):
            with self.assertRaises(OSError) as caught:
                sock.send(b"payload")
            self.assertEqual(caught.exception.args[0], 11)
            self.assertEqual(inner.handshakes, expected)
        self.assertEqual(sock.send(b"payload"), 7)
        self.assertTrue(sock.handshake_done)
        self.assertEqual(inner.handshakes, 3)
        # A completed handshake is never re-stepped.
        sock.send(b"more")
        self.assertEqual(inner.handshakes, 3)

    def test_micropython_read_none_write_none_map_to_would_block(self):
        inner = FakeSSLSocket(FakeSocket([b"data"]))
        sock = tls.TLSSocket(inner)
        inner.write_none = True
        with self.assertRaises(OSError) as caught:
            sock.send(b"x")
        self.assertEqual(caught.exception.args[0], 11)
        inner.write_none = False
        inner.read_none = True
        with self.assertRaises(OSError) as caught:
            sock.recv(16)
        self.assertEqual(caught.exception.args[0], 11)
        inner.read_none = False
        self.assertEqual(sock.recv(16), b"data")

    def test_want_write_and_enotconn_are_also_yield_points(self):
        class Stubborn(FakeSSLSocket):
            def do_handshake(self):
                self.handshakes += 1
                if self.handshakes == 1:
                    raise SSLWantWriteError("want write")
                if self.handshakes == 2:
                    raise OSError(107, "not connected")

        inner = Stubborn(FakeSocket())
        sock = tls.TLSSocket(inner)
        for _ in range(2):
            with self.assertRaises(OSError) as caught:
                sock.recv(16)
            self.assertEqual(caught.exception.args[0], 11)
        self.assertEqual(sock.send(b"ok"), 2)

    def test_real_socket_errors_are_not_swallowed(self):
        class Broken(FakeSSLSocket):
            def do_handshake(self):
                raise OSError(104, "connection reset")

        sock = tls.TLSSocket(Broken(FakeSocket()))
        with self.assertRaises(OSError) as caught:
            sock.send(b"x")
        self.assertEqual(caught.exception.args[0], 104)

    def test_wrap_tls_requires_ca_unless_insecure(self):
        module = FakeSSLModule()
        with self.assertRaises(ValueError):
            tls.wrap_tls(FakeSocket(), "host", ssl_module=module)
        wrapped = tls.wrap_tls(FakeSocket(), "host", insecure=True,
                               ssl_module=module)
        self.assertIsInstance(wrapped, tls.TLSSocket)
        context = module.contexts[0]
        self.assertFalse(context.check_hostname)
        self.assertEqual(context.verify_mode, FakeSSLModule.CERT_NONE)

    def test_wrap_tls_with_ca_requires_verification_and_defers_handshake(self):
        module = FakeSSLModule()
        tls.wrap_tls(FakeSocket(), "bambuddy.local", ca_pem=PEM,
                     ssl_module=module)
        context = module.contexts[0]
        self.assertEqual(context.verify_mode, FakeSSLModule.CERT_REQUIRED)
        self.assertEqual(context.cadata, PEM)
        self.assertTrue(context.check_hostname)
        self.assertEqual(context.wrap_args, ("bambuddy.local", False))

    def test_build_without_deferred_handshake_is_refused(self):
        module = FakeSSLModule(LegacyContext)
        with self.assertRaises(OSError) as caught:
            tls.wrap_tls(FakeSocket(), "bambuddy.local", ca_pem=PEM,
                         ssl_module=module)
        # An implicit handshake would either stall the cooperative loop for the
        # whole handshake or never complete on a non-blocking socket, so the
        # build is rejected outright instead of being quietly downgraded.
        self.assertIn("cooperative TLS is unsupported", str(caught.exception))
        self.assertIn("do_handshake_on_connect", str(caught.exception))


class WebSocketOverTLSTests(unittest.TestCase):
    def random(self, size):
        return bytes([1]) * size

    def upgrade_response(self):
        key = ws._b64(self.random(16))
        return (
            "HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            "Sec-WebSocket-Accept: " + ws.websocket_accept(key) + "\r\n\r\n"
        ).encode()

    def client(self, factory):
        return ws.BambuddyWebSocketClient(
            core.BambuddyOutbox("bridge-a", boot_session="pico-boot"),
            "wss://bambuddy.local:8443/api/v1/bmcu-link/ws",
            "secret", socket_factory=factory, random_bytes=self.random,
            tls_insecure=True)

    def test_ws_client_over_wss_completes_upgrade_cooperatively(self):
        plain = FakeSocket([self.upgrade_response()])
        inner = FakeSSLSocket(plain, want=4)
        secure = tls.TLSSocket(inner)
        client = self.client(lambda _host, _port: secure)
        client.poll(0, True)
        self.assertEqual(client.state, "authenticate")
        for step in range(1, 10):
            client.poll(step, True)
            self.assertIn(client.state, ("authenticate", "online"))
        self.assertEqual(client.state, "online")
        self.assertIsNone(client.last_error)
        # wss:// default port 8443 is explicit, and the token stays in the URL
        # query of the GET request only.
        self.assertIn(b"Host: bambuddy.local:8443", bytes(plain.sent))

    def test_default_wss_port_is_omitted_from_the_host_header(self):
        plain = FakeSocket([self.upgrade_response()])
        secure = tls.TLSSocket(FakeSSLSocket(plain))
        client = ws.BambuddyWebSocketClient(
            core.BambuddyOutbox("bridge-a", boot_session="pico-boot"),
            "wss://bambuddy.local/ws", "secret",
            socket_factory=lambda _host, _port: secure,
            random_bytes=self.random, tls_insecure=True)
        client.poll(0, True)
        self.assertEqual(client.state, "online")
        self.assertIn(b"Host: bambuddy.local\r\n", bytes(plain.sent))

    def test_stalled_handshake_hits_connect_deadline(self):
        plain = FakeSocket()
        secure = tls.TLSSocket(FakeSSLSocket(plain, never=True))
        client = self.client(lambda _host, _port: secure)
        client.poll(0, True)
        for step in range(1, 5):
            client.poll(step, True)
        self.assertEqual(client.state, "authenticate")
        client.poll(20000, True)
        self.assertEqual(client.state, "backoff")
        self.assertIn("connect timeout", client.last_error)
        self.assertTrue(plain.closed)


if __name__ == "__main__":
    unittest.main()
