# Pico to Bambuddy transport

Status: telemetry-only Phase 5 transport implemented against the Bambuddy
Issue #2 contract.

## 1. Data path

Production telemetry is push-only from the Pico toward Bambuddy:

```text
BMCU --UART STATUS/EVENT--> Pico decoder --> envelope queue
                                              |
                                              +-- authenticated WebSocket --> Bambuddy
```

`ws://` and `http://` carry the bearer credential in the clear: they are
permitted on an explicitly trusted LAN only, because the credential provides
application authentication but not transport confidentiality. Routed or shared
networks must use `wss://` or `https://`, which run over the cooperative
non-blocking TLS adapter in `pico/bambuddy_tls.py`.

The Pico initiates and owns one persistent authenticated WebSocket connection.
Bambuddy must not discover a Pico and poll its local HTTP API. Its telemetry
endpoints are independent, read-only diagnostics with no delivery ACK, replay,
or completeness guarantee. The only local write is commissioning configuration.

Phase 5 accepts no CONTROL messages. The Pico does not open a production
listener and does not accept inbound Bambuddy connections.

## 2. Responsibilities

The Pico:

- decodes BMCU UART frames without waiting for network I/O;
- creates the envelope defined in
  [`PICO_BAMBUDDY_ENVELOPE.md`](PICO_BAMBUDDY_ENVELOPE.md);
- stores envelopes in a bounded FIFO, sends oldest first, and discards only
  acknowledged records;
- reconnects without blocking UART service; and
- exposes local HTTP only for best-effort diagnostics.

Bambuddy:

- provides the authenticated WebSocket ingest endpoint and credentials;
- persists envelopes before acknowledging their deduplication keys;
- deduplicates reconnect replay and reports telemetry gaps;
- owns history, UI, notification, authorization, and retention; and
- sends only commands covered by a separately approved command contract.

When Bambuddy authentication is enabled, credentials are least-privilege.
Telemetry uses `bmcu_link:telemetry`; LED feedback
uses `presentation:write`; soft reset uses `device:reset`; future mechanical
operations require `motion:control` and remain disabled. The telemetry token
never authorizes a control operation.

## 3. Connection state machine

The network service is cooperative and non-blocking:

```text
disabled -> wifi_wait -> connect -> authenticate -> online
                ^          |             |            |
                +----------+-------------+------------+
                         bounded backoff
```

After Wi-Fi loss, DNS failure, authentication failure, socket close, or ACK
timeout, the Pico closes the session and retries with exponential backoff and
jitter. The target delays are 1, 2, 4, 8, 16, then at most 30 seconds. UART
decoding runs before every network-service pass.

Each connection starts with a transport HELLO. It identifies the Pico, its
firmware and capabilities, all link sessions, and the cumulative transport-drop
count. Bambuddy then accepts oldest-first telemetry and acknowledges only data
that it has durably persisted. Replayed records are safe because their dedup
keys are stable for the boot/link session.

Each telemetry text message is either one envelope or a JSON array of envelopes;
there is no batch wrapper object. The Pico uses at most 16 records per batch,
below Bambuddy's 500-envelope server limit.

## 4. Queue and backpressure

The FIFO covers the most recent 30 seconds and has a fixed record limit. Socket
backpressure never blocks UART receive. When full, it removes the oldest
non-critical STATUS before an ERROR or CRITICAL EVENT when possible and emits a
`transport_drop` envelope with the cumulative loss count.

STATUS is coalesced per link: at most once every 3 seconds while any AMS motion is active, once every 15 seconds while idle, and not periodically after the link becomes stale. Activity transitions are queued immediately.
EVENT, HELLO, link-state, and loss records are queued immediately. The rate and
deduplication rules are normative in the envelope contract.

## 5. HTTP and mDNS boundary

The Pico may publish its hostname with mDNS for commissioning and browser
diagnostics. Neither mDNS nor `GET /api/*` is a production Bambuddy ingestion
mechanism. Bambuddy endpoint discovery/configuration is the opposite direction:
the Pico is provisioned with the Bambuddy WebSocket URL and token.

The diagnostic API may show a newer in-memory snapshot than Bambuddy has
persisted, and it may omit transient events. No production adapter may scrape
it or use its one-second browser refresh as a telemetry clock.

## 6. Fallback and TLS (implemented)

If the deployed MicroPython build cannot support WebSocket, the only permitted
fallback is Pico-initiated batched HTTPS NDJSON POST carrying the same envelope,
deduplication key, and durable ACK semantics. It is not periodic Bambuddy-side
polling. This fallback adds connection/TLS overhead and cannot carry commands;
therefore WebSocket remains the preferred transport.

### 6.1 HTTPS NDJSON adapter

Implemented in `pico/bambuddy_https.py` (`BambuddyNdjsonClient`). It is selected
automatically when the commissioned URL scheme is `https://` or `http://`; the
CONTROL gateway is then not constructed and `control:soft_reset` is not
announced, because the fallback carries no commands. Selecting `https://` or
`http://` therefore disables remote soft reset even when `control_enabled` and
a `control_key` are stored: the effective state is reported as `control_active`
by `/api/bambuddy/config` and on the settings page, so an operator is never left
believing an inert command path is armed.

- endpoint: `https://<host>/api/v1/bmcu-link/ndjson`; the telemetry token is
  sent as `Authorization: Bearer <token>` and never as a URL parameter, so it
  cannot leak into server access logs;
- request: `POST` with `Content-Type: application/x-ndjson`, an explicit
  `Content-Length`, `Connection: keep-alive`, and a body of one
  `json.dumps(envelope)` per line;
- the first request on every TCP/TLS connection is a HELLO-only POST; an
  unpersisted HELLO envelope is resent verbatim, so its `transport_sequence`
  is not burned on retries;
- at most one request is in flight, paced by a request interval; an empty queue
  sends nothing (there is no ping in HTTP mode);
- response: bounded parse (header <= 4096 B, `Content-Length` <= 16384 B,
  chunked rejected). A `200` body is the same ACK object the WebSocket server
  returns and is fed through the shared adaptation helpers in
  `pico/bambuddy_session.py` into `BambuddyOutbox.apply_ack`, so persisted
  watermark advance, retryable retention, and non-retryable quarantine are the
  same single implementation as the WebSocket path;
- failures (`401`/`403`/`404`/`413`/`429`/`5xx`, malformed body, ACK timeout)
  never touch the queue: the oldest unacknowledged record is replayed on the
  next connection, and Bambuddy deduplicates by
  `(device_id, link.id, pico_boot_session, transport_sequence)`;
- a graceful `Connection: close`, a server EOF, or unsolicited bytes on an idle
  keep-alive connection (a proxy's `408` just before it closes) all recycle the
  connection instead of failing: no request is in flight, so none of them is a
  data problem. The retry is paced by at least 250 ms so a close-after-every-
  response server cannot drive an unpaced TLS reconnect loop, and the backoff
  index is only grown when consecutive connections deliver nothing durable
  while telemetry is queued;
- if a connection is closed after answering only the HELLO while telemetry is
  waiting, the next connection folds the batch into the same NDJSON body right
  behind the HELLO line. A server that closes after every response can never
  carry a second request, and a HELLO-only reconnect loop would otherwise lose
  every record to the 30 s queue-age expiry.

### 6.2 Non-blocking TLS

`pico/bambuddy_tls.py` wraps a connecting socket so that `send`/`recv` perform
at most one bounded `do_handshake` step per call and raise `OSError(EAGAIN)`
while the handshake is pending. A build whose `wrap_socket` cannot defer the
handshake (`do_handshake_on_connect`) is refused outright rather than silently
downgraded: an implicit handshake would either run inline and stall the loop or
never complete against the still-connecting non-blocking socket. Because one
handshake step can still hold the CPU for hundreds of milliseconds, the BMCU
UARTs are constructed with an explicit `rxbuf` (2048 bytes by default, and
overridable per link or via `config.UART_RXBUF`) instead of the 256-byte default
ring, which overflows in ~22 ms at 115200 baud. Both adapters already treat `EAGAIN` as a
cooperative yield, so `wss://` and `https://` need no state-machine change and
a handshake cannot stall UART decoding or the web UI. The WebSocket client also
enforces a 20 s connect/handshake deadline so a silent peer cannot wedge it.

There is no system trust store on MicroPython, so verification material is
commissioned, not discovered: `tls_ca` (a PEM, at most 4096 bytes) or an
explicit `tls_insecure` opt-out is required for any TLS scheme. Both are stored
through the existing CSRF-protected atomic-save path; the PEM is write-only,
reported as `tls_ca_set` and never echoed or logged. Prefer ECDSA server
certificates: a single RSA verification step can occupy one loop pass for
hundreds of milliseconds on the RP2350.

## 7. Implemented Phase 5 profile

- endpoint: `ws://<host>:8000/api/v1/bmcu-link/ws?token=<token>` on a trusted
  private LAN, or `wss://` / `https://` with commissioned trust material
  (section 6);
- commissioning: bounded CSRF-protected local POST, atomic flash replacement,
  token and CA PEM never returned, plus `secrets.py` bootstrap fallback
  (`BAMBUDDY_WS_URL`, `BAMBUDDY_TOKEN`, `BAMBUDDY_TLS_CA`,
  `BAMBUDDY_TLS_INSECURE`);
- reconnect: transport HELLO envelope, then oldest-unacknowledged arrays;
- ACK: at least 10 second progress timeout, per-link persisted watermark, and
  partial rejection by batch index;
- retry: retryable rejection remains queued; non-retryable rejection is
  quarantined and counted as a transport drop; and
- CONTROL: excluded from Phase 5. Future presentation, reset, and motion
  commands require their separately scoped and authenticated contract.

## 8. Authenticated CONTROL path (soft reset only)

Implemented per MAC input byte contract v1 (issue #2/#6) in
`pico/bmcu_control.py`; `ci/test_pico_control.py` pins the byte layout with
known-answer vectors shared with Bambuddy.

- Authorization is a device-scoped 32-byte `control_key` (64 hex via the
  commissioning UI), fully independent from the telemetry token. CONTROL is
  default-disabled; enabling requires a stored key, and clearing the key also
  disables CONTROL. The key is never echoed by any API or page.
- Messages and results are HMAC-SHA256 over fixed-order, length-prefixed
  fields with separated contexts (`BMCU-CTRL-v1` / `BMCU-CTRL-RES-v1`).
- Replay protection: a per-WS-session `control_session_nonce` announced in the
  transport HELLO (rotates with each freshly built HELLO envelope), a strictly
  increasing `control_sequence`, and a bounded `operation_id` window. Message
  TTL is bounded to 1..60000 ms; the soft-reset payload TTL to 1..5000 ms.
- Fail-closed: structural or MAC failures are never answered (a counter and
  diagnostic string record them); post-authentication failures return a signed
  `rejected` result with a reason. The only enabled command is `soft_reset`,
  converted to `REQUEST_SOFT_RESET` only after the local idle guard passes —
  and the BMCU still re-checks its own safety predicates before resetting.
- Lifecycle results (`scheduled`, `rebooted`, `cancelled`, `completed`) are
  signed with the originating session's nonce and drain one frame at a time
  between telemetry batches.
