# Pico to Bambuddy transport

The production transport is BMB1 persistent binary TCP. The normative
contract is `BMCU_BINARY_TRANSPORT_V1.md`.

The Pico sends validated BMCU wire frames, binary diagnostics, and binary
device logs. It does not provide JSON WebSocket, HTTPS NDJSON, or JSON ACK and
CONTROL compatibility paths.

Configure `BMCU_BINARY_HOST`, `BMCU_BINARY_PORT`,
`BMCU_BINARY_DEVICE_ID`, and the 64-hex-character
`BMCU_BINARY_DEVICE_KEY` in `config.py`.

UART draining has priority over TCP, journal, local HTTP, and explicitly
scheduled garbage collection. Unacknowledged protected records are retained
in RAM and BMJ1 history and replayed after authentication.
