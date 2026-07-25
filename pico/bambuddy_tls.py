"""Non-blocking TLS wrapper presenting the plain duck-socket contract.

The Pico runs one cooperative single-threaded loop, so a TLS handshake must
never be driven to completion inside a single call. ``TLSSocket`` performs at
most one bounded ``do_handshake`` step per ``send``/``recv`` and raises
``OSError(EAGAIN)`` while the handshake is still pending. Both Bambuddy
adapters already treat ``EAGAIN`` as a cooperative yield point, so enabling
``wss://`` or ``https://`` needs no state-machine change at all.
"""

_EAGAIN = 11
_WOULD_BLOCK = (11, 35, 57, 107, 115, 10035, 10057)
# ENOTCONN / EINPROGRESS / EALREADY while the underlying TCP connect is still
# in flight are yield points too, not failures.
_CONNECTING = (114, 119, 10036, 10037, 10057)
_WANT = ("SSLWantReadError", "SSLWantWriteError")


def _errno(exc):
    return getattr(exc, "errno", exc.args[0] if exc.args else None)


def _is_pending(exc):
    if type(exc).__name__ in _WANT:
        return True
    code = _errno(exc)
    return code in _WOULD_BLOCK or code in _CONNECTING


def _pending(reason):
    return OSError(_EAGAIN, reason)


def _resolve_ssl_module():
    try:
        import tls  # MicroPython >= 1.23
        return tls
    except ImportError:
        pass
    try:
        import ussl
        return ussl
    except ImportError:
        pass
    import ssl
    return ssl


def wrap_tls(sock, server_hostname, ca_pem=None, insecure=False,
             ssl_module=None):
    """Wrap a connected/connecting socket in a cooperative TLS session.

    There is no system trust store on MicroPython, so verification material is
    commissioned rather than discovered: either a CA PEM or an explicit
    ``insecure`` opt-out is required.
    """
    if not ca_pem and not insecure:
        raise ValueError("tls_ca or tls_insecure required")
    module = ssl_module or _resolve_ssl_module()
    context = module.SSLContext(module.PROTOCOL_TLS_CLIENT)
    if insecure:
        # check_hostname must be cleared before CERT_NONE on CPython.
        try:
            context.check_hostname = False
        except (AttributeError, ValueError):
            pass
        context.verify_mode = module.CERT_NONE
    else:
        context.verify_mode = module.CERT_REQUIRED
        context.load_verify_locations(cadata=ca_pem)
        try:
            context.check_hostname = True
        except (AttributeError, ValueError):
            pass
    try:
        wrapped = context.wrap_socket(
            sock, server_hostname=server_hostname,
            do_handshake_on_connect=False)
    except TypeError:
        # Falling back to an implicit handshake is not an option: on this
        # cooperative loop it would either run the whole handshake inline
        # (stalling BMCU UART service for hundreds of milliseconds) or never
        # complete at all against a still-connecting non-blocking socket.
        raise OSError(
            "cooperative TLS is unsupported on this MicroPython build: "
            "wrap_socket has no do_handshake_on_connect")
    return TLSSocket(wrapped)


class TLSSocket:
    """Duck-socket over a TLS session: ``setblocking``/``send``/``recv``/``close``."""

    def __init__(self, sock, handshake_done=False):
        self.sock = sock
        self.handshake_done = handshake_done
        self.handshake_steps = 0

    def setblocking(self, flag):
        setter = getattr(self.sock, "setblocking", None)
        if setter is not None:
            try:
                setter(flag)
            except Exception:
                pass

    def _step(self):
        """Perform at most one handshake step; raise EAGAIN while pending."""
        if self.handshake_done:
            return
        handshake = getattr(self.sock, "do_handshake", None)
        if handshake is None:
            self.handshake_done = True
            return
        self.handshake_steps += 1
        try:
            handshake()
        except Exception as exc:
            if _is_pending(exc):
                raise _pending("TLS handshake pending")
            raise
        self.handshake_done = True

    def send(self, data):
        self._step()
        writer = getattr(self.sock, "send", None)
        if writer is None:
            writer = self.sock.write
        try:
            sent = writer(data)
        except Exception as exc:
            if _is_pending(exc):
                raise _pending("TLS write would block")
            raise
        if sent is None:
            raise _pending("TLS write would block")
        return sent

    def recv(self, size):
        self._step()
        reader = getattr(self.sock, "recv", None)
        if reader is None:
            reader = self.sock.read
        try:
            data = reader(size)
        except Exception as exc:
            if _is_pending(exc):
                raise _pending("TLS read would block")
            raise
        if data is None:
            raise _pending("TLS read would block")
        return data

    def close(self):
        try:
            self.sock.close()
        except Exception:
            pass
