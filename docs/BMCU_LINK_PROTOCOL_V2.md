# BMCU Link Protocol v2

Status: base protocol implemented; `GET_FULL_STATUS` and full-status records are specified for the next firmware/Pico phase.

This document is the canonical wire contract between BMCU, Raspberry Pi Pico 2 W, and Bambuddy.
Physical wiring is defined separately in `BMCU_UART_PHYSICAL_SPEC.md`.

## 1. Design goals

- Keep printer control and motor timing authoritative on BMCU.
- Give Pico/Bambuddy a complete baseline state after boot or reconnect.
- Send incremental binary events after that baseline instead of repeatedly sending full state.
- Use fixed offsets, integer fields, stable enum values, and bounded frames.
- Perform no string concatenation, formatting, JSON generation, wall-clock conversion, or dynamic allocation on BMCU.
- Never trigger fresh ADC/I2C measurements merely to answer a management request; snapshot cached state only.

The normal synchronization flow is:

```text
HELLO -> GET_FULL_STATUS -> FULL_STATUS_RECORD x N -> STATUS/EVENT updates -> periodic PING/PONG
```

Consequently, a Pico implementation needs one common frame decoder plus fixed-layout dispatchers. Decoding only
the full-status response is insufficient because later state changes arrive as `STATUS` and `EVENT` frames.

## 2. Physical and framing layer

- UART: 115200 baud, 8 data bits, even parity, 1 stop bit (`8E1`)
- Multibyte integers: little-endian
- Frame encoding: COBS
- Frame delimiter: `0x00`
- Maximum decoded frame: 64 bytes
- Maximum payload: 57 bytes
- CRC: CRC-16/CCITT-FALSE, polynomial `0x1021`, initial value `0xFFFF`

Decoded frame:

```text
offset  size  field
0       1     version
1       1     kind
2       2     sequence
4       1     payload_length
5       N     payload
5+N     2     crc16
```

CRC covers bytes `0` through `4+N`. A receiver must reject malformed COBS, decoded lengths outside `7..64`,
payload-length mismatches, unsupported versions, and CRC mismatches before dispatch.

## 3. Version, sequence, and enums

The current protocol version is `2`. Every enum value is wire ABI: existing numeric values must never be
renumbered or reused. New values may be appended.

- Responses use the request's sequence.
- Unsolicited BMCU frames use a BMCU-local wrapping `u16` sequence.
- A sequence gap is diagnostic evidence, not necessarily a fatal link error.

### 3.1 Message kinds

| Value | Name | Direction | Status |
| ---: | --- | --- | --- |
| `0x01` | `HELLO` | BMCU → Pico | implemented |
| `0x02` | `STATUS` | BMCU → Pico | implemented |
| `0x03` | `EVENT` | BMCU → Pico | ABI reserved |
| `0x04` | `PRINTER_TRANSACTION` | BMCU → Pico | ABI reserved |
| `0x05` | `SENSOR_RECORD` | BMCU → Pico | ABI reserved |
| `0x10` | `GET_STATUS` | Pico → BMCU | implemented |
| `0x11` | `SET_LED_MODE` | Pico → BMCU | implemented |
| `0x12` | `PING` | Pico → BMCU | implemented |
| `0x17` | `GET_FULL_STATUS` | Pico → BMCU | specified |
| `0x72` | `PONG` | BMCU → Pico | implemented |
| `0x73` | `FULL_STATUS_RECORD` | BMCU → Pico | specified |
| `0x7F` | `ACK` | BMCU → Pico | implemented |

### 3.2 ACK results

| Value | Name |
| ---: | --- |
| 0 | `OK` |
| 1 | `BAD_VALUE` |
| 2 | `UNSUPPORTED` |
| 3 | `BUSY` |
| 4 | `BAD_STATE` |
| 5 | `DENIED` |
| 6 | `EXPIRED` |
| 7 | `DUPLICATE` |
| 8 | `INTERNAL` |

`ACK` payload is `request_kind:u8, result:u8`. A successful request that has a typed response does not also
need `ACK_OK`. Invalid requests and requests rejected before execution return `ACK`.

## 4. Hardware time

BMCU does not send uptime or wall-clock time. It sends the existing 32-bit SysTick counter as `hw_tick32`.
`HELLO.tick_hz` declares its frequency.

```text
delta_ticks = (new_tick - old_tick) & 0xffffffff
delta_s     = delta_ticks / tick_hz
```

At the current 18 MHz tick rate the counter wraps roughly every 238.6 seconds. Pico must extend it while the
link is active by observing successive values. Pico/Bambuddy owns receive timestamps and wall-clock mapping.
After a disconnect long enough to make wrap count ambiguous, discard the old extension and establish a new
baseline with `GET_FULL_STATUS`.

## 5. Implemented payloads

### 5.1 HELLO — 9 bytes

| Offset | Type | Field |
| ---: | --- | --- |
| 0 | u8 | protocol version |
| 1 | u16 | capability bits |
| 3 | u8 | firmware major |
| 4 | u8 | firmware minor |
| 5 | u32 | `tick_hz` |

HELLO is emitted once after BMCU Link initialization. Reconnection is driven by Pico `PING` and status
requests rather than periodic HELLO traffic.

### 5.2 STATUS — 27 bytes

| Offset | Type | Field |
| ---: | --- | --- |
| 0 | u32 | `hw_tick32` |
| 4 | u16 | TX drop count, saturated/truncated view |
| 6 | u16 | RX drop count, saturated/truncated view |
| 8 | u16 | CRC error count, saturated/truncated view |
| 10 | u16 | frame error count, saturated/truncated view |
| 12 | u8 | current slot; `0xFF` means none/unknown |
| 13 | u8 | inserted mask, low four bits |
| 14 | u8 | online mask, low four bits |
| 15 | u8[4] | per-channel motion enum |
| 19 | u8[4] | per-channel pull percentage |
| 23 | u16 | pressure |
| 25 | u8 | LED override mode |
| 26 | u8 | control-error flag |

`GET_STATUS` has an empty payload and returns one STATUS with the request sequence. Unsolicited STATUS is
event-driven and reports semantic state changes.

### 5.3 SET_LED_MODE, PING, and PONG

```text
SET_LED_MODE request: mode:u8 | timeout_s:u16
PING request:         token:u32
PONG response:        token:u32 | hw_tick32:u32
```

## 6. Full status synchronization

### 6.1 GET_FULL_STATUS request — 2 bytes

```text
offset  type  field
0       u8    section_mask
1       u8    channel_mask
```

`section_mask`:

| Bit | Section |
| ---: | --- |
| 0 | global state |
| 1 | per-channel state |
| 2 | printer-bus state |
| 3 | diagnostic counters |

Only the low four bits are valid. `channel_mask` uses bits `0..3`; it is ignored if the channel section is not
requested. A true full request is `section_mask=0x0F, channel_mask=0x0F`.

The command owner is `OWNER_USER`. It is read-only, requires no lease, and cannot displace printer ownership.
If another full snapshot is being emitted, return `ACK_BUSY`. Invalid masks return `ACK_BAD_VALUE`.

### 6.2 FULL_STATUS_RECORD response — 26 bytes

Every selected section is returned as one or more records with a common fixed header and a 16-byte union.

```text
offset  type      field
0       u16       snapshot_id
2       u8        record_index, zero based
3       u8        record_count
4       u8        record_type
5       u8        record_flags; currently zero
6       u32       hw_tick32 shared by the snapshot
10      u8[16]    record_data union
```

All records use the request sequence. Completion is reached after receiving every unique index in
`0..record_count-1`. Records may be decoded in arrival order but must be assembled by index. Missing or
duplicate indices invalidate the snapshot; Pico may retry after a short delay.

Full-status record types:

| Value | Name | Count |
| ---: | --- | ---: |
| 1 | `GLOBAL` | zero or one |
| 2 | `CHANNEL` | zero to four |
| 3 | `PRINTER_BUS` | zero or one |
| 4 | `COUNTERS` | zero or one |

#### GLOBAL record_data — 16 bytes

| Offset | Type | Field |
| ---: | --- | --- |
| 0 | u8 | current slot |
| 1 | u8 | inserted mask |
| 2 | u8 | online mask |
| 3 | u8 | control-error flag |
| 4 | u8[4] | motion |
| 8 | u8[4] | pull percentage |
| 12 | u16 | pressure |
| 14 | u8 | LED override mode |
| 15 | u8 | reserved, zero |

#### CHANNEL record_data — 16 bytes

| Offset | Type | Field |
| ---: | --- | --- |
| 0 | u8 | channel index |
| 1 | u8 | motion enum |
| 2 | u8 | inserted flag |
| 3 | u8 | online flag |
| 4 | u8 | pull percentage |
| 5 | u8 | sensor-validity enum |
| 6 | u16 | channel flags |
| 8 | u16 | cached angle/raw position |
| 10 | i16 | cached position delta |
| 12 | i16 | cached motor command/PWM |
| 14 | u16 | reserved, zero |

Unavailable measurements must use zero data with the appropriate validity/flag indication; they must not
cause synchronous sensor access.

#### PRINTER_BUS record_data — 16 bytes

| Offset | Type | Field |
| ---: | --- | --- |
| 0 | u8 | online flag |
| 1 | u8 | last RX class |
| 2 | u8 | last command |
| 3 | u8 | last transaction outcome |
| 4 | u16 | valid RX count |
| 6 | u16 | invalid RX count |
| 8 | u16 | TX count |
| 10 | u16 | TX/drop count |
| 12 | u32 | age of last valid RX in hardware ticks |

#### COUNTERS record_data — 16 bytes

| Offset | Type | Field |
| ---: | --- | --- |
| 0 | u32 | management TX drops |
| 4 | u32 | management RX drops |
| 8 | u32 | management CRC errors |
| 12 | u32 | management frame errors |

### 6.3 Capture and transmission rules

- Capture all requested cached values once in the normal main-loop context.
- Give every record the same `snapshot_id` and `hw_tick32`.
- Do not hold interrupts disabled while copying the full snapshot.
- Stage the bounded snapshot, then enqueue at most one record per service opportunity.
- Preserve capacity for safety/critical events; full-status records are lower priority.
- Never block for UART completion; DMA remains responsible for transmission.
- Do not emit a success ACK. The complete typed record set is the success response.

A full request selects at most seven records: one GLOBAL, four CHANNEL, one PRINTER_BUS, and one COUNTERS.
At 115200 8E1 this is only a few hundred wire bytes, but staged emission prevents a burst from occupying all
seven usable TX queue entries.

## 7. Binary event records

EVENT uses a 16-byte binary record. BMCU never embeds text.

```text
LogRecordHeader (8 bytes):
  hw_tick32:u32 | type:u8 | severity:u8 | source:u8 | payload_length:u8

LogRecord (16 bytes):
  LogRecordHeader | union payload[8]
```

The payload union has specialized layouts for boot, printer link, printer transaction, state change, sensor,
command result, safety decision, and diagnostic counter records. Unused union bytes must be zero. Record type,
severity, source, command owner, outcome, reason, ACK result, and sensor validity are numeric enums defined in
`src/bmcu_link_protocol.h`. Pico/Bambuddy owns their human-readable labels.

## 8. Pico decoder requirements

The recommended hot path is:

1. Accumulate bytes until `0x00` into a fixed 66-byte buffer.
2. COBS-decode into a fixed 64-byte buffer.
3. Validate length, version, and CRC before reading payload fields.
4. Dispatch on `kind` with a table or switch.
5. Decode integers by fixed offsets; do not parse strings or JSON.
6. Extend `hw_tick32`, add Pico receive time, and forward a typed object to Bambuddy.

Do not cast arbitrary receive-buffer addresses directly to native structs unless packing, alignment, endianness,
and ABI size are explicitly verified. Little-endian load helpers or `memcpy` into size-asserted structures are
safe and still far faster than UART arrival at 115200 baud.

Pico state handling:

- On HELLO or reconnect: request `GET_FULL_STATUS(0x0F, 0x0F)`.
- Install the complete record set atomically as the new baseline.
- Apply later STATUS/EVENT records incrementally.
- Use PING/PONG for liveness, not periodic full snapshots.
- On a sequence gap or incomplete snapshot: mark state uncertain and request a new full snapshot.

## 9. Compatibility and resource limits

- Protocol v1 and v2 are wire-incompatible because STATUS offset 0 changed from uptime seconds to raw ticks.
- Unknown kinds and enum values must be preserved numerically by Pico/Bambuddy and must not crash decoding.
- Reserved fields must be transmitted as zero and ignored on receive.
- BMCU must reject commands with unexpected payload lengths.
- Full-status rate limiting is a host policy; once on connect and on explicit diagnosis is expected. It must not
  be used as the normal polling mechanism.
