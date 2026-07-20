# BMCU Link Protocol alpha.3

Status: protocol, events, and full-status synchronization are implemented.

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
- Synchronization bytes: `0xA5 0x5A`
- Maximum body (`version` through CRC): 64 bytes
- Maximum payload: 57 bytes
- CRC: CRC-16/CCITT-FALSE, polynomial `0x1021`, initial value `0xFFFF`

Wire frame:

```text
offset  size  field
0       2     sync = A5 5A
2       1     version
3       1     kind
4       2     sequence
6       1     payload_length
7       N     payload
7+N     2     crc16
```

CRC covers `version` through the last payload byte; synchronization bytes are excluded. The wire size is
`N+9`, exactly the same overhead as the former COBS-delimited format. A receiver scans for `A5 5A`, reads
the fixed five-byte body header, bounds-checks `payload_length`, and then reads exactly
`payload_length+2` bytes. It must reject body lengths outside `7..64`, payload-length mismatches,
unsupported versions, and CRC mismatches before dispatch. After an invalid length or CRC, resume scanning
for the synchronization bytes.
## 3. Version, sequence, and enums

The current wire version is `0x83` (`alpha.3`). Bit 7 marks a prerelease and bits 6..0 carry the prerelease revision. After real-device validation and ABI freeze, the first stable release will use `0x01` (stable v1). Every enum value is wire ABI: existing numeric values must never be
renumbered or reused. New values may be appended.

- Responses use the request's sequence.
- Unsolicited BMCU frames use a BMCU-local wrapping `u16` sequence.
- A sequence gap is diagnostic evidence, not necessarily a fatal link error.

### 3.1 Message kinds

| Value | Name | Direction | Status |
| ---: | --- | --- | --- |
| `0x01` | `HELLO` | BMCU → Pico | implemented |
| `0x02` | `STATUS` | BMCU → Pico | implemented |
| `0x03` | `EVENT` | BMCU → Pico | implemented |
| `0x04` | `PRINTER_TRANSACTION` | BMCU → Pico | ABI reserved |
| `0x05` | `SENSOR_RECORD` | BMCU → Pico | ABI reserved |
| `0x10` | `GET_STATUS` | Pico → BMCU | implemented |
| `0x11` | `SET_LED_MODE` | Pico → BMCU | implemented |
| `0x12` | `PING` | Pico → BMCU | implemented |
| `0x17` | `GET_FULL_STATUS` | Pico → BMCU | implemented |
| `0x72` | `PONG` | BMCU → Pico | implemented |
| `0x73` | `FULL_STATUS_RECORD` | BMCU → Pico | implemented |
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
| 5 | `PRINTER_AUTH` | zero or one |
| 6 | `PRINTER_RX_CORE` | zero or one |
| 7 | `PRINTER_RX_LOSS` | zero or one |
| 8 | `PRINTER_RX_DMA` | zero or one |
| 9 | `PRINTER_TX_CORE` | zero or one |
| 10 | `PRINTER_TX_FAULT` | zero or one |

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
| 1 | u8 | AMS logical motion enum |
| 2 | u8 | inserted flag |
| 3 | u8 | online flag |
| 4 | u8 | pull percentage |
| 5 | u8 | sensor-validity enum |
| 6 | u16 | channel flags |
| 8 | u16 | cached angle/raw position |
| 10 | i16 | cached position delta |
| 12 | i16 | cached motor command/PWM |
| 14 | u8 | motion fault enum |
| 15 | u8 | BMCU controller motion: bit 7 valid, bits 0..6 enum |

Controller motion values are `0=send`, `1=redetect`, `2=pull`, `3=stop`, `4=before-on-use`,
`5=stop-on-use`, `6=pressure-control-on-use`, `7=pressure-control-idle`, and `8=before-pull-back`.
This is a sampled controller phase, not proof of physical movement. Consumers must evaluate it together
with motor PWM, position delta, sensor validity, and motion fault.

Channel flag bit 4 is set when `motion fault enum` is nonzero. Defined fault values are:

- `0`: none
- `1`: pull-back made no expected-direction progress for the safety interval
- `2`: pull-back exceeded its direction-independent travel budget

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

#### PRINTER_AUTH record_data — 16 bytes

This record is emitted with the printer-bus section when `CAP_PRINTER_TRACE` is set.

| Offset | Type | Field |
| ---: | --- | --- |
| 0 | u16 | last long-frame type |
| 2 | u16 | received `0x040D` count, saturating |
| 4 | u16 | received `0x040E` count, saturating |
| 6 | u16 | last long-frame payload length |
| 8 | u32 | last long-frame receive hardware tick |
| 12 | u8 | last transaction outcome |
| 13 | u8 | last decision reason |
| 14 | u8 | last response length, saturated |
| 15 | u8 | FNV-1a fingerprint of at most the first 32 payload bytes |

#### PRINTER_RX_CORE record_data — 16 bytes

| Offset | Type | Field |
| ---: | --- | --- |
| 0 | u32 | bytes delivered from the active RX transport to the CPU parser |
| 4 | u32 | frames completed by the compatibility framer |
| 8 | u32 | invalid or over-limit declared lengths |
| 12 | u32 | header CRC8 failures |

#### PRINTER_RX_LOSS record_data — 16 bytes

| Offset | Type | Field |
| ---: | --- | --- |
| 0 | u32 | bytes skipped while seeking synchronization, including overrun loss |
| 4 | u32 | complete frames dropped because a prior frame was still pending |
| 8 | u32 | DMA transfer errors |
| 12 | u32 | USART hardware overruns |

#### PRINTER_RX_DMA record_data — 16 bytes

| Offset | Type | Field |
| ---: | --- | --- |
| 0 | u32 | DMA producer-over-consumer ring overruns |
| 4 | u32 | completed DMA ring revolutions |
| 8 | u32 | maximum observed unconsumed DMA bytes |
| 12 | u32 | wrapped frames copied into the compatibility buffer |

#### PRINTER_TX_CORE record_data — 16 bytes

| Offset | Type | Field |
| ---: | --- | --- |
| 0 | u32 | USART1 TX DMA transfers started |
| 4 | u32 | USART1 transmission-complete interrupts observed |
| 8 | u32 | response handlers skipped because a prior response was still queued |
| 12 | u32 | response-required handlers that returned without a response |

#### PRINTER_TX_FAULT record_data — 16 bytes

| Offset | Type | Field |
| ---: | --- | --- |
| 0 | u32 | invalid response lengths rejected before DMA start |
| 4 | u32 | DMA1 Channel 4 transfer errors |
| 8 | u32 | USART1 TX operations aborted after the 25 ms completion deadline |
| 12 | u32 | intentional no-response decisions, such as an already-completed online-detect registration |

All printer TX counters are saturating and reset at firmware initialization. A TX DMA error or timeout disables
the DMA request and channel, clears the DMA/USART completion state, returns RS-485 DE to receive, and releases
the bus for a later printer retry. Repeated identical rejected/failed transaction EVENTs are rate-limited to one per 5 s.
Intentional no-response decisions are IGNORED, do not generate warning EVENTs, and are counted separately from
	x_response_missing.

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

A full request selects at most thirteen records: one GLOBAL, four CHANNEL, one PRINTER_BUS, one PRINTER_AUTH,
three PRINTER_RX records, two PRINTER_TX records, and one COUNTERS.
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

The payload union has specialized layouts for boot, printer link, printer transaction, printer long transaction,
state change, sensor, command result, safety decision, and diagnostic counter records. Unused union bytes must be
zero. Record type, severity, source, command owner, outcome, reason, ACK result, and sensor validity are numeric
enums defined in `src/bmcu_link_protocol.h`. Pico/Bambuddy owns their human-readable labels.
Additive decision reasons `14=NO_RESPONSE` and `15=NO_RESPONSE_EXPECTED` distinguish response construction
failure and intentional protocol silence from a queued-response conflict; `8=TX_BUSY` is used only when a prior response is queued. Asynchronous DMA errors and timeouts are
reported by the PRINTER_TX_FAULT counters rather than attributed to a transaction until transaction-ID
correlation reaches TX completion.

`PRINTER_LONG_TRANSACTION (9)` preserves the complete long-frame `type:u16` and then carries
`owner:u8, outcome:u8, reason:u8, request_length:u8, response_length:u8, payload_hash:u8`.
The existing `PRINTER_TRANSACTION (3)` layout is unchanged for alpha.3 compatibility.

`STATE_CHANGE` field `8` is the per-channel motion-fault latch. Its `slot` is the channel index and its
value uses the motion-fault enum documented in the CHANNEL full-status record.

## 8. Pico decoder requirements

The recommended hot path is:

1. Scan the byte stream for `A5 5A`.
2. Read the five-byte body header and reject `payload_length > 57`.
3. Read the exact remaining payload and CRC bytes into a fixed 64-byte body buffer.
4. Validate length, version, and CRC before reading payload fields.
5. Dispatch on `kind` with a `switch`.
6. Decode integers by fixed offsets; do not parse strings or JSON.
7. Extend `hw_tick32`, add Pico receive time, and forward a typed object to Bambuddy.
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

- `0x83` is alpha.3 and must not be accepted as stable v1.
- Promotion to stable v1 changes the version byte to `0x01` only after payloads and behavior are frozen.
- Alpha and stable peers reject each other explicitly; there is no implicit downgrade.
- Unknown kinds and enum values must be preserved numerically by Pico/Bambuddy and must not crash decoding.
- Reserved fields must be transmitted as zero and ignored on receive.
- BMCU must reject commands with unexpected payload lengths.
- Full-status rate limiting is a host policy; once on connect and on explicit diagnosis is expected. It must not
  be used as the normal polling mechanism.
