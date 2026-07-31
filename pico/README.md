# Pico BMCU Monitor

The Pico firmware bridges up to two BMCU UART links to Bambuddy using the BMB1
persistent binary TCP protocol. Production telemetry, ACK, CONTROL,
diagnostics, runtime logs, crash recovery, and the local diagnostic UI are
binary. There is no JSON WebSocket or HTTPS NDJSON compatibility mode.

## Configuration

Copy `config_example.py` to `config.py` and configure:

- `BMCU_LINKS` for UART IDs and pins (the default maps `bmcu-a` to GP0/GP1
  and `bmcu-b` to GP4/GP5);
- `UART_RXBUF` for per-link receive headroom (4096 bytes by default);
- `BMCU_BINARY_HOST` and `BMCU_BINARY_PORT`;
- `BMCU_BINARY_DEVICE_ID`;
- `BMCU_BINARY_DEVICE_KEY`, an optional bootstrap 256-bit key encoded as 64 hex
  characters (the local UI can provision or replace it);
- journal and queue sizes when the defaults are unsuitable.

Wi-Fi credentials remain in separately provisioned `secrets.py`.

## Runtime design

Validated UART frames enter fixed byte storage before semantic display
decoding. STATUS is coalesced until it receives a global transport sequence;
EVENT, fault, link, loss, CONTROL result, warning log, and crash records are
protected. BMJ1 segments retain important records across restart.

The cooperative order is UART drain, local state, TCP receive/send, bounded
journal flush, bounded local HTTP, then safe-point maintenance. Garbage
collection is scheduled and measured; it is never invoked by an HTTP request.

## Local UI

The root page is static and fetches binary snapshots/deltas:

- `/api/current.bin`
- `/api/events.bin`
- `/api/history/status.bin`
- `/api/diagnostics.bin`
- `/api/logs.bin`

JavaScript performs BMB1 and TLV decoding with `DataView`.

The **Device authentication** card can generate a cryptographically random
256-bit key, copy it once for Bambuddy, accept an existing 64-hex-character
key, and persist it without rebooting. The saved key is write-only: the read
API exposes only configured state and a 48-bit SHA-256 fingerprint. Mutation
requires a non-simple content type and action header so a cross-origin form
cannot silently replace the key. The UI is intended only for a trusted LAN.

## Deployment and tests

`deploy.ps1` uploads every Python module and places `main.py` last. Host tests
live under `ci/`; canonical cross-repository binary fixtures live under
`tests/fixtures/bmcu_binary/`.
