# BMCU Binary Transport v1

Status: reviewed implementation contract  
Wire identifier: `BMB1`  
Document revision: 1

This document is the shared contract between Bambuddy and the MicroPython
monitor in `BMCU-C-PJARCZAK-kaizou/pico`. An identical copy must exist in both
repositories. A wire-format change is not complete until both copies and both
implementations are updated in the same development cycle.

## 1. Decision

The production BMCU Monitor transport is replaced by a dedicated persistent
binary TCP connection. Backward compatibility is not required.

The following production paths are removed after v1 is functional:

- JSON BMCU envelopes;
- the JSON WebSocket telemetry connection;
- the HTTP/HTTPS NDJSON fallback;
- JSON ACK and CONTROL messages;
- schema negotiation with the former `bmcu.management.v2` transport.

The Pico local diagnostic UI also uses binary state and event endpoints. Its
HTML and JavaScript are static assets; the browser decodes BMCU wire frames.
The Pico must not build a UI-only object tree or serialize live state as JSON.

## 2. Design principles

1. UART receive and frame validation have the highest scheduling priority.
2. A validated BMCU wire frame is transported without conversion to a Python
   dictionary or a second semantic serialization format.
3. Transport queues contain bytes, not nested Python objects.
4. Delivery buffering and local historical retention are separate concerns.
5. EVENT, fault, link transition, and loss records are durable and must not be
   silently replaced by STATUS samples.
6. Repeated STATUS is coalescible; state transitions are not.
7. All variable-length input is bounded before allocation or parsing.
8. Power loss may truncate the last local record but must not invalidate
   earlier journal records.
9. Bambuddy is the authority for durable-ingest ACKs and long-term analysis.
10. The Pico retains enough local history for disconnection recovery and local
    diagnosis.

CBOR, MessagePack, Protocol Buffers, FlatBuffers, and a new Pico-side semantic
STATUS encoding are intentionally not used. The existing BMCU frame is already
a compact typed binary record.

## 3. Data flow

```text
BMCU UART
  -> bounded frame scanner and CRC validation
  -> minimal local-state update
  -> transport record in RAM
       -> priority delivery queue
       -> append-only local journal
  -> persistent TCP connection
  -> Bambuddy binary decoder
  -> database rows, current state, anomaly analysis, and UI
```

The local-state update may decode fields required by the Pico UI, link
liveness, safe CONTROL handling, and STATUS coalescing. The transport record
must be built from the validated wire bytes rather than from that decoded
state.

## 4. TCP framing

Every TCP message starts with this fixed 32-byte header. All multibyte integers
are unsigned big-endian.

| Offset | Size | Field |
|---:|---:|---|
| 0 | 4 | Magic, ASCII `BMB1` |
| 4 | 1 | Protocol version, `1` |
| 5 | 1 | Message type |
| 6 | 2 | Flags |
| 8 | 4 | Payload length |
| 12 | 8 | Transport sequence |
| 20 | 8 | Pico boot ID |
| 28 | 1 | Link index |
| 29 | 3 | Reserved, zero |

The payload immediately follows the header. `payload_length` excludes the
header. A receiver must reject a payload larger than 4096 bytes before
allocating storage for it. Reserved bits and bytes must be sent as zero and
ignored by v1 receivers.

The TCP parser must accept partial headers, partial payloads, and multiple
messages in one read. It must never assume that one `recv()` equals one
message.

### 4.1 Message types

| Value | Name | Direction |
|---:|---|---|
| `0x01` | `SERVER_CHALLENGE` | Bambuddy to Pico |
| `0x02` | `HELLO` | Pico to Bambuddy |
| `0x03` | `HELLO_ACCEPTED` | Bambuddy to Pico |
| `0x10` | `BMCU_FRAME` | Pico to Bambuddy |
| `0x11` | `LINK_STATE` | Pico to Bambuddy |
| `0x12` | `TRANSPORT_DROP` | Pico to Bambuddy |
| `0x13` | `PICO_DIAGNOSTIC` | Pico to Bambuddy or local UI |
| `0x14` | `PICO_LOG` | Pico to Bambuddy or local UI |
| `0x20` | `ACK` | Bambuddy to Pico |
| `0x30` | `CONTROL` | Bambuddy to Pico |
| `0x31` | `CONTROL_RESULT` | Pico to Bambuddy |
| `0x40` | `PING` | either |
| `0x41` | `PONG` | either |
| `0x7f` | `PROTOCOL_ERROR` | either |

`transport_sequence` is zero for messages that do not participate in durable
telemetry ordering. It is monotonically increasing per Pico boot for durable
Pico-to-Bambuddy records.

### 4.2 Flags

| Bit | Name | Meaning |
|---:|---|---|
| 0 | `REPLAY` | Record was restored from local history |
| 1 | `CRITICAL` | Record has critical delivery priority |
| 2 | `SNAPSHOT` | Frame belongs to a requested full snapshot |
| 3 | `WALL_TIME_VALID` | Optional wall time in the payload is valid |
| 4 | `JOURNALED` | Pico has committed the record to local history |

Other bits are reserved.

## 5. Session and authentication

Each Pico has a stable device ID and a provisioned 256-bit device key.
Authentication uses HMAC-SHA256.

1. Bambuddy accepts TCP and sends `SERVER_CHALLENGE` containing 32 random
   bytes.
2. Pico sends `HELLO`.
3. Bambuddy verifies the HMAC and sends `HELLO_ACCEPTED`.
4. No telemetry or CONTROL is accepted before step 3 completes.

### 5.1 HELLO payload

```text
u8   device_id_length
bytes device_id, UTF-8, maximum 63 bytes
u8   firmware_length
bytes firmware, UTF-8, maximum 63 bytes
u8   link_count, maximum 2 in v1
repeated:
  u8 link_index
  u8 link_id_length
  bytes link_id, UTF-8, maximum 31 bytes
u64  oldest_available_sequence
u64  newest_available_sequence
bytes hmac_sha256, 32 bytes
```

The HMAC input is the exact concatenation:

```text
"BMB1-AUTH" || challenge || pico_boot_id ||
device_id_length || device_id ||
firmware_length || firmware ||
link table ||
oldest_available_sequence || newest_available_sequence
```

CONTROL messages use a session key derived as:

```text
HMAC-SHA256(device_key, "BMB1-SESSION" || challenge || pico_boot_id)
```

## 6. BMCU_FRAME

`BMCU_FRAME` carries the validated BMCU wire frame without semantic
re-encoding.

```text
u64  received_at_us
u16  wire_length
bytes complete validated BMCU wire frame
```

`received_at_us` is the Pico monotonic timestamp at which the complete frame
was accepted. The BMCU frame's own version, kind, sequence, length, payload,
and CRC are not duplicated in the transport header.

Bambuddy must:

1. enforce the transport payload bound;
2. enforce the BMCU wire-length bound;
3. validate the embedded BMCU frame again;
4. decode directly into current-state and persistence structures;
5. avoid reconstructing the former JSON/Pydantic envelope.

## 7. Link, loss, and control records

### 7.1 LINK_STATE payload

```text
u64 observed_at_us
u8  state
u8  reason
u16 reserved
```

States are `0=unknown`, `1=resyncing`, `2=online`, `3=stale`,
`4=offline`, and `5=incompatible`.

### 7.2 TRANSPORT_DROP payload

```text
u64 observed_at_us
u64 first_dropped_sequence
u64 last_dropped_sequence
u32 dropped_record_count
u8  reason
u8[3] reserved
```

No loss may be silent. If exact dropped sequence bounds are unavailable, both
bounds are zero and the count remains mandatory.

### 7.3 CONTROL

CONTROL retains the existing ownership and safety rules. Its payload is fixed
binary and ends with a 32-byte HMAC over the complete transport header and
CONTROL payload excluding the HMAC.

```text
u64 command_sequence
u64 issued_at_us
u32 ttl_ms
u8  command
u8  argument_length
bytes arguments, maximum 128 bytes
bytes hmac_sha256
```

The Pico rejects an expired, replayed, unauthenticated, unknown, or unsafe
command and returns a `CONTROL_RESULT`. No motor, slot, or filament movement
command is introduced by this transport specification.

## 8. ACK and replay

Bambuddy ACKs only after durable database commit. ACK is a cumulative
contiguous watermark, scoped by Pico boot ID and link.

```text
u64 pico_boot_id
u8  link_index
u8  reject_count
u16 reserved
u64 persisted_through_sequence
repeated reject_count times:
  u64 transport_sequence
  u8  reason
```

The Pico may release delivery-queue records at or below the acknowledged
watermark. Locally retained history is not deleted merely because it was
ACKed.

After reconnect:

1. authenticate;
2. send the current complete snapshot;
3. send live critical records;
4. replay unacknowledged EVENT/fault/link/loss records;
5. interleave live normal EVENT records;
6. replay sampled STATUS history with the lowest priority.

Bambuddy deduplicates by:

```text
device_id, pico_boot_id, transport_sequence
```

Replay ordering is reconstructed from `transport_sequence` and
`received_at_us`, not TCP arrival order alone.

## 9. Pico RAM queues

The implementation maintains separate byte-oriented storage:

### 9.1 Latest STATUS

Each link has one replaceable latest-STATUS slot. A newer STATUS may overwrite
an unsent STATUS for the same link. Activity transitions detected between the
two samples must first emit a non-replaceable transition/event record.

### 9.2 Durable event queue

EVENT, HELLO-related state, faults, link transitions, CONTROL results, and loss
records use a non-replaceable byte ring. An initial layout of 128-byte slots is
permitted. No queue entry may contain a nested Python dictionary, list, or JSON
string.

Queue-full eviction order is:

1. replaceable STATUS;
2. ACKed normal historical samples;
3. ACKed notice records;
4. ACKed warning records;
5. ACKed error records;
6. ACKed critical records.

Unacknowledged EVENT and critical records are protected until no protected
storage remains. Any forced eviction emits `TRANSPORT_DROP`.

## 10. Local history

The Pico maintains an append-only binary journal independent from the live
delivery queue. It supports Bambuddy outages, Pico restart recovery, and local
diagnosis.

### 10.1 Retention classes

Always journal:

- BMCU EVENT;
- warning, error, and critical records;
- motion and control faults;
- link state transitions;
- Pico and BMCU reboot/session transitions;
- BMCU sequence gaps;
- decoder CRC/frame errors;
- transport drops;
- slot, motion, inserted-mask, and online-mask transitions;
- CONTROL and CONTROL_RESULT.
- Pico warning and error log records.

Sample before journaling:

- repeated STATUS;
- pull percentage;
- pressure;
- monotonically increasing error counters.

Initial STATUS policy:

- active motion: at most one sample per second;
- idle: at most one sample per 15 seconds;
- meaningful state or threshold change: immediate;
- anomaly window: temporarily one sample per second.

Identical repeated STATUS, PING/PONG, ACK, local HTTP access, and transport
replays are not journaled again.

### 10.2 Journal segments

History uses rotating segment files. Initial segment size is 64 KiB. The total
history allocation is configuration with a conservative device-specific
default.

Segment header:

```text
bytes magic "BMJ1"
u8    journal version
u8[3] reserved
u64   pico_boot_id
u32   segment_sequence
u64   created_at_us
u32   header_crc32
```

Journal record:

```text
u16 record_length
u8  record_type
u8  flags
u8  link_index
u8[3] reserved
u64 transport_sequence
u64 received_at_us
bytes record payload
u32 record_crc32
```

The journal writer buffers records in preallocated RAM and writes a bounded
chunk only after UART servicing. A flash write must never be performed inline
from the UART callback or frame parser.

On startup, scanning stops at the first truncated or invalid record in the
newest segment. Earlier valid records remain usable. The invalid tail may be
discarded when the segment is next rotated.

ACK progress is stored as a separate, batched watermark checkpoint. Journal
records are never rewritten merely to mark them ACKed.

### 10.3 Retention and deletion

Deletion occurs at whole-segment granularity. Old ACKed STATUS segments are
removed first. Unacknowledged critical records have the highest retention
priority. Every forced loss is represented by a durable
`TRANSPORT_DROP`.

## 11. Scheduling

The Pico cooperative loop observes this strict priority:

1. drain UARTs;
2. validate frames and append to preallocated RAM;
3. update minimum local state;
4. service TCP receive/ACK/CONTROL;
5. service bounded TCP send work;
6. flush a bounded journal chunk when UART backlog is empty;
7. service local HTTP diagnostics;
8. perform maintenance and garbage collection only at a safe point.

Dual UART draining is round-robin. A pass continues while backlog exists,
subject to an explicit total byte/time budget so one link cannot starve the
other or permanently starve networking. The former single 128-byte read per
link is not the v1 scheduling contract.

Transport and journal buffers are allocated during startup. HTTP request
handling must not invoke unconditional garbage collection. No socket send,
flash operation, JSON serialization, or local UI response may execute from the
UART receive path.

## 12. Local diagnostic UI and binary API

The Pico serves static HTML, CSS, and JavaScript. No live state is interpolated
into the HTML. Browser JavaScript fetches `ArrayBuffer` responses and decodes
them with `DataView`.

The local endpoints are:

- `GET /api/current.bin`;
- `GET /api/events.bin?after=<sequence>&limit=<n>`;
- `GET /api/history/status.bin`;
- `GET /api/diagnostics.bin`;
- `GET /api/logs.bin?after=<sequence>&limit=<n>`.

Responses use `Content-Type: application/vnd.bmcu-monitor.v1` and consist of
one or more complete BMB1 messages. Because every message carries a fixed
header and payload length, messages may be concatenated without a JSON array
or a separate response envelope.

`/api/current.bin` returns the latest retained `BMCU_FRAME` STATUS and
`LINK_STATE` for each link. `/api/events.bin` reads bounded records directly
from the byte ring or journal. `/api/history/status.bin` returns bounded,
sampled STATUS records. No endpoint may reconstruct the former Python
dictionary envelope.

`PICO_DIAGNOSTIC` is a compact TLV payload for values that do not originate in
a BMCU frame:

```text
repeated:
  u8  tag
  u8  value_type
  u16 value_length
  bytes value
```

Defined v1 tags include uptime, free journal capacity, queue depth, oldest
unacknowledged sequence, last ACK watermark, Wi-Fi state, Bambuddy connection
state, and exception count. The initial registry also covers firmware and reset
identity, free/allocated/minimum heap, temperature, garbage-collection count,
loop delay, UART backlog and error counters, Wi-Fi RSSI and reconnects, TCP
traffic and reconnects, journal usage and failures, replay count, and STATUS
replacement count. CPU percentage is not reported: cooperative-loop busy/idle
time and maximum service delay are the meaningful health signals on the Pico.
Unknown tags are ignored. Strings have explicit maximum lengths. Runtime logs
are not embedded in diagnostics; they use `PICO_LOG`.

The Pico sends a complete diagnostic snapshot after authentication, a normal
snapshot at most once every 15 seconds, and an immediate snapshot when a
health value crosses a warning or critical boundary. Bambuddy stores the
long-term series. The Pico journals lower-frequency normal samples and
higher-frequency samples around an anomaly.

### 12.1 Binary device log

The device log is binary from creation through delivery. The Pico must not
build a dictionary entry, JSON snapshot, or JSON crash file for it.

`PICO_LOG` payload:

```text
u64 log_sequence
u64 uptime_ms
u8  severity
u8  component_length
u16 message_length
u16 detail_length
bytes component, UTF-8, maximum 40 bytes
bytes message, UTF-8, maximum 320 bytes
bytes detail TLV, maximum 512 bytes
```

Severity values are `0=debug`, `1=info`, `2=notice`, `3=warning`,
`4=error`, and `5=critical`.

Detail TLV uses the same `tag`, `value_type`, `value_length`, `value` layout as
`PICO_DIAGNOSTIC`. Tags are component-specific and optional. Implementations
must prefer typed numeric details over formatted text. Tracebacks are UTF-8,
truncated from the beginning so the most recent frames remain, and bounded by
the detail limit.

The in-memory runtime log is a preallocated byte ring. A fixed-slot
implementation may initially use 1024-byte slots with a small configured
count. Repeated identical exceptions within the suppression window update a
numeric suppression counter when the entry is still mutable; they do not
allocate another object tree.

`/api/logs.bin` returns concatenated `PICO_LOG` messages directly from the log
ring and journal. It is sequence-paginated and bounded to at most 64 records or
32 KiB per response, whichever comes first. The browser decodes and formats
the records. There is no `/api/pico/logs` JSON compatibility endpoint.

Warning, error, and critical logs are journaled. Debug, info, and notice logs
remain in the bounded RAM ring unless a diagnostic capture mode explicitly
enables their persistence. Sending a log to Bambuddy does not remove it from
the local ring.

The last-crash record uses a bounded binary `BMCR1` file:

```text
bytes magic "BMCR1"
u16  record_length
bytes one PICO_LOG payload
u32  crc32
```

It is written only by the existing rate-limited crash persistence policy.
Startup validates its length and CRC before exposing it as a recovered
`PICO_LOG`. A malformed or torn crash record is ignored. No JSON parsing or
serialization is used for crash persistence.

The browser retains the last sequence it rendered and requests only newer
events. A full snapshot, complete journal, and runtime log must not be
regenerated every second. Current-state refresh may use conditional requests
or a two-to-five-second interval.

The JavaScript decoder is the UI presentation adapter. It maps numeric BMCU
states and enums to labels, constructs the slot view, and renders the event
list. This work is not performed on the Pico Python heap.

The UI exposes:

- current link and four-slot state;
- recent and anomalous events;
- Bambuddy connection state;
- delivery queue depth;
- oldest unacknowledged sequence and age;
- journal usage and retained time range;
- last durable ACK watermark;
- explicit drop ranges.
- sequence-paginated binary device logs and the recovered last-crash record.

Bambuddy presents these diagnostics as a managed BMCU Monitor device, with an
overview, hardware, communications, device-log, and local-history view.
Aggregate health is green, warning, critical, or offline. UART overflow,
sequence loss, journal failure, and prolonged durable-ACK delay are critical
signals; low heap, weak Wi-Fi, growing queue depth, and low journal capacity
are warning signals before their configured critical boundary.

Configuration writes use bounded HTML form encoding or a dedicated compact
binary request, not JSON. Configuration-file representation at rest is outside
the live telemetry contract, but reading or writing it must not occur in the
UART receive path.

## 13. Bambuddy persistence

Bambuddy runs a bounded incremental TCP parser and maps binary records directly
to:

- device/link session state;
- raw-event persistence;
- decoded current state;
- anomaly and loader-timeline inputs;
- durable ACK watermarks.

Raw BMCU bytes may be stored alongside decoded fields to allow later decoder
correction. Database uniqueness must enforce the replay identity. UI WebSocket
broadcasts are produced after persistence and are not part of the Pico
transport contract.

## 14. Failure behavior

- Malformed length, invalid authentication, invalid embedded CRC, unknown
  mandatory message, or replayed CONTROL closes the session after an optional
  bounded `PROTOCOL_ERROR`.
- Network failure never deletes unacknowledged records.
- Bambuddy unavailability does not block UART processing.
- Journal failure degrades to RAM delivery and raises a durable diagnostic when
  storage becomes available; it does not block UART.
- Local UI or browser-decoder failure is isolated from UART, transport, and
  journal processing.
- A Pico restart creates a new boot ID and restores unacknowledged journal
  records with their original boot ID and sequence identity.

## 15. Implementation completion criteria

Implementation is complete when automated integration tests demonstrate:

- two links can produce simultaneous traffic without sequence loss caused by
  transport, journal, or local UI work;
- EVENT and critical records survive Bambuddy disconnection and Pico restart;
- STATUS replacement never removes a state transition;
- Bambuddy ACKs only committed records;
- replay does not create duplicate database events;
- a torn final journal write preserves earlier records;
- journal rotation follows the documented retention priority;
- every forced queue or journal loss produces a drop range;
- malformed and unauthenticated inputs are bounded and rejected;
- the local UI reads current and historical binary data without constructing
  a Pico-side JSON or semantic object-tree response;
- device logs, exception details, and the last-crash record are binary and
  render correctly in the browser without Pico-side JSON generation;
- no production JSON/WebSocket/NDJSON telemetry path remains enabled.
