# Pico to Bambuddy telemetry envelope

Status: proposed production contract for BMCU Link alpha.3 telemetry.

This document defines the payload sent from a Pico bridge to Bambuddy. It does
not change the BMCU UART ABI. Bambuddy is the server; a Pico initiates one
authenticated, persistent outbound WebSocket connection. A batched NDJSON POST
transport is permitted only when WebSocket is unavailable. Both transports carry
the same envelopes and acknowledgement semantics.

The connection lifecycle and the strict separation from local diagnostic HTTP
are defined in [`PICO_BAMBUDDY_TRANSPORT.md`](PICO_BAMBUDDY_TRANSPORT.md).
Bambuddy must not poll or scrape the Pico diagnostic API for production data.

## 1. Identity, time, and ordering

Every envelope has this shape (additional fields are allowed):

```json
{
  "schema": "bmcu.management.v2",
  "registry_version": "alpha.3",
  "device_id": "stable-pico-id",
  "mode": "production_monitor",
  "received_at_us": 123456789,
  "link": {
    "id": "default",
    "state": "online",
    "pico_boot_session": "pico-random-at-boot",
    "bmcu_boot_session": 4,
    "transport_sequence": 1234,
    "bmcu_sequence": 77,
    "queue_depth": 2
  },
  "frame": {"kind": "status", "kind_id": 2, "protocol": 131},
  "data": {}
}
```

- `device_id` is stable across Pico reboots. In a multi-link bridge, `link.id`
  is stable and the pair identifies the optional device.
- `received_at_us` is the Pico monotonic microsecond clock captured when the
  UART frame was decoded. It is required. It resets with `pico_boot_session`;
  it is not a wall clock.
- `received_at`, when present, is an optional, best-effort RFC 3339 wall-clock
  value. Bambuddy assigns the authoritative wall-clock arrival time.
- `pico_boot_session` is a fresh unpredictable identifier generated once per
  Pico boot. `bmcu_boot_session` starts at zero for a new Pico session and is
  incremented after each valid BMCU `HELLO`; it changes even if BMCU sequence
  numbers happen to repeat.
- `transport_sequence` is a Pico-generated `u64`, starts at zero for each
  `(link.id, pico_boot_session)`, and increments for every envelope. It is the
  transport ordering and acknowledgement value.
- `bmcu_sequence`, when present, is the received wrapping BMCU UART `u16`.
  Multiple full-status records may share it; it is diagnostic metadata and is
  never a transport deduplication key.
- `mode` is exactly `production_monitor` or `bench_stub`.

Bambuddy deduplicates with `(device_id, link.id, pico_boot_session,
`transport_sequence)`. It must retain `bmcu_boot_session`, `bmcu_sequence`,
raw numeric enum values and
unknown fields. `registry_version` selects
[`bmcu_link_enum_registry.json`](bmcu_link_enum_registry.json); an unknown
number is displayed/stored numerically rather than rejected.

## 2. Session and loss signalling

The Pico keeps a bounded FIFO of the most recent 30 seconds of envelopes while
the transport is disconnected. It sends the oldest retained envelope first on
reconnect. The queue has a fixed configured record limit; it never blocks UART
decoding or motor/printer work.

When the FIFO overwrites one or more records, Pico emits this envelope before
later queued telemetry:

```json
{
  "schema": "bmcu.management.v2",
  "device_id": "stable-pico-id",
  "received_at_us": 123456999,
  "link": {"id": "default", "pico_boot_session": "pico-random-at-boot",
           "bmcu_boot_session": 4, "transport_sequence": 1235,
           "queue_depth": 30},
  "frame": {"kind": "transport_drop", "kind_id": null, "protocol": 131},
  "data": {"dropped_count": 7, "reason": "queue_overflow"}
}
```

`dropped_count` is cumulative for the current Pico boot and is never silently
reset except by a new `pico_boot_session`. A reconnect begins with a transport
HELLO containing `device_id`, Pico firmware version, BMCU firmware/protocol
range, capabilities, mode, both boot sessions, and current cumulative drop
count. Bambuddy acknowledges the highest fully persisted
`(link_id, pico_boot_session, transport_sequence)` watermark. Pico may discard
only acknowledged records. A partial batch rejection identifies its zero-based
batch index, `transport_sequence` when parseable, stable error code, and
`retryable` flag. Pico retains retryable failures; it quarantines non-retryable
records, increments the cumulative drop count, and continues with later data.

## 3. Rate and backpressure

STATUS is sent only for semantic state changes and at most once per second as a
heartbeat. EVENT, HELLO, link-state, and `transport_drop` are sent immediately.
Normal operation is limited to 2 envelopes/s/link; bursts may reach 20
envelopes/s/link for at most 5 seconds. The queue is the backpressure boundary:
on saturation, drop the oldest non-critical STATUS first, preserve ERROR and
CRITICAL events when possible, then report the resulting loss.

Bambuddy should batch database writes and must treat sequence gaps, transport
drops, decoder errors, and incomplete snapshots as telemetry-quality signals,
not as filament faults.

## 4. Security and control scope

When Bambuddy authentication is enabled, the WebSocket/POST endpoint requires a
device-scoped `bmcu_link:telemetry` credential before it accepts telemetry.
Trusted-LAN deployments with Bambuddy authentication disabled may omit the token.
Trusted private LAN deployments may use `ws://`; routed or untrusted
deployments require TLS termination. Phase 5 is telemetry-only. All CONTROL
operations remain disabled until their separate scope, authentication, replay,
TTL, and safety contracts are implemented.