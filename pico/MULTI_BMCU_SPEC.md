# Pico 2 W multi-BMCU bridge specification

Status: target design; the checked-in Pico implementation currently supports one UART link and only `GET /api/status`.

## Scope

One Pico 2 W bridges multiple independent BMCU H1 management links to one
Bambuddy host.  This does not alter BMCU firmware or put multiple BMCUs on one
TTL bus.

## Capacity and wiring

Each BMCU needs its own full-duplex UART.

| Link | Pico UART | Pico pins | BMCU H1 |
| --- | --- | --- | --- |
| `bmcu-a` | UART0 | GP0 TX, GP1 RX | H1-2 RX, H1-1 TX, H1-4 GND |
| `bmcu-b` | UART1 | GP4 TX, GP5 RX | H1-2 RX, H1-1 TX, H1-4 GND |

All links are 115200 8E1 and 3.3 V TTL.  Each BMCU and Pico must share GND;
Pico remains USB powered and no BMCU H1-3 rail is connected.

Do not join H1 TX lines together, do not join H1 RX lines together, and do not
use a passive UART splitter.  Multiple active BMCU TX drivers would contend.

Pico 2 W therefore supports two directly attached BMCUs.  More than two
requires one independent UART per BMCU via a supported hardware UART expander
(for example a dual-UART SPI/I2C bridge) or separate Pico bridges.  Software
UART is not accepted at 115200 8E1.

## Identity

The bridge has one stable name, for example `bmcu-bridge-a6f4.local`.
Each attached BMCU has a configured stable link ID:

```python
BMCU_LINKS = [
    {"id": "ams-left",  "uart": 0, "tx": 0, "rx": 1},
    {"id": "ams-right", "uart": 1, "tx": 4, "rx": 5},
]
```

The external identity is `<bridge-id>/<link-id>`; e.g.
`bmcu-bridge-a6f4/ams-left`.  A BMCU protocol sequence number is only unique
within that link.  Bambuddy must key histories and deduplication by `bridge_id`, `link_id`,
`pico_boot_session`, `bmcu_boot_session`, and `sequence`, as defined in
[`PICO_BAMBUDDY_ENVELOPE.md`](../docs/PICO_BAMBUDDY_ENVELOPE.md).

## Protocol ownership

Every UART has its own `BMCUMonitor`, frame decoder, status baseline, event
ring, CRC counters, PING timer, and liveness state.  Frames are never merged
before decoding.  A command is routed only to its explicitly selected link.

The initial command set remains read-only (`GET_FULL_STATUS`, `GET_STATUS`,
`PING`) plus the existing safe LED command.  No broadcast write command is
permitted.

## Planned HTTP and Bambuddy surface

After the multi-link phase, the diagnostic HTTP surface will expose:

```text
GET /api/devices
GET /api/devices/<link-id>/status
GET /api/devices/<link-id>/events
```

The UI shows a device selector and one isolated dashboard per link.  The
summary page may show link health, but does not combine slots, pressure, or
sensor values from different BMCUs.

Bambuddy registers each link as a separate optional device through the Pico's
outbound transport, not by opening a connection to the Pico. Registration uses
the envelope transport HELLO and includes `link_id`, firmware version,
capabilities, link health, protocol version, and both boot sessions.

## Failure isolation

A failed UART, unplugged BMCU, UART CRC burst, or stalled full snapshot marks
only that link `STALE`/`FAULT`.  It cannot stop the other UART or BMCU control
loop.  Each link receives bounded service work per Pico loop iteration.

## Acceptance criteria

- Loss or corrupt input on one link does not change status, timing, or command
  routing on another link.
- A device ID is present in every HTTP response and Bambuddy envelope.
- A UI action cannot target another BMCU by changing an implicit global slot.
- Pico power loss leaves every BMCU locally functional.
