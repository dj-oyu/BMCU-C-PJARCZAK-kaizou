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

The current Pico adapter deliberately accepts only `ws://` on an explicitly
trusted LAN. The bearer credential provides application authentication but not
transport confidentiality. Routed or untrusted deployments require a
WSS-capable gateway or a future non-blocking TLS adapter.

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

Credentials are least-privilege. Telemetry uses `telemetry:write`; LED feedback
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

STATUS is queued on semantic change and at most once per second as a heartbeat.
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

## 6. Fallback

If the deployed MicroPython build cannot support WebSocket, the only permitted
fallback is Pico-initiated batched HTTPS NDJSON POST carrying the same envelope,
deduplication key, and durable ACK semantics. It is not periodic Bambuddy-side
polling. This fallback adds connection/TLS overhead and cannot carry commands;
therefore WebSocket remains the preferred transport.

## 7. Implemented Phase 5 profile

- endpoint: `ws://<host>:8000/api/v1/bmcu-link/ws?token=<token>` on a trusted
  private LAN;
- commissioning: bounded CSRF-protected local POST, atomic flash replacement,
  token never returned, plus `secrets.py` bootstrap fallback;
- reconnect: transport HELLO envelope, then oldest-unacknowledged arrays;
- ACK: at least 10 second progress timeout, per-link persisted watermark, and
  partial rejection by batch index;
- retry: retryable rejection remains queued; non-retryable rejection is
  quarantined and counted as a transport drop; and
- CONTROL: excluded from Phase 5. Future presentation, reset, and motion
  commands require their separately scoped and authenticated contract.
