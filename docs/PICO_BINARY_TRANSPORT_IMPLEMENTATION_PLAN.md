# Pico implementation plan: BMCU Binary Transport v1

Status: implementation-ready  
Branch: `feature/bmcu-binary-transport`  
Normative contract: `docs/BMCU_BINARY_TRANSPORT_V1.md`

## 1. Scope and outcome

The Pico monitor will replace its JSON envelope, WebSocket, and NDJSON
production transports with a persistent authenticated BMB1 TCP client. It will
queue validated raw BMCU frames as bytes, retain local binary history, expose a
binary local UI API, emit hardware/communications diagnostics, and preserve
unacknowledged EVENT and critical records across reconnect and restart.

This plan is Pico-specific. Matching server work is tracked in
`docs/BMCU_BINARY_TRANSPORT_IMPLEMENTATION_PLAN.md` in Bambuddy.

## 2. Non-negotiable architecture

- UART handling remains the highest-priority work.
- The transport queue never stores JSON, dict envelopes, or decoded object
  trees.
- The complete validated BMCU wire frame is the telemetry payload.
- STATUS latest-value storage, durable events, runtime logs, and flash history
  are separate bounded stores.
- Local UI responses are binary and decoded by browser JavaScript.
- Flash and socket work are never performed from the UART receive path.
- Backward compatibility with JSON WebSocket/NDJSON is not required.

## 3. Phase P0: shared fixtures and constants

Import the reviewed fixtures shared with Bambuddy into `tests/fixtures/`.
Create one constants module generated from or validated against the shared
registry:

```text
pico/bmcu_binary_constants.py
```

Cover:

- BMB1 message types and flags;
- diagnostic tags and value types;
- log severity;
- link/drop/rejection reasons;
- CONTROL command identifiers;
- maximum field and payload sizes.

Tests run under CPython and, where practical, MicroPython unix/board.

Exit condition: Pico encoder output is byte-identical to Bambuddy fixtures.

## 4. Phase P1: allocation-bounded codec and byte stores

Add:

```text
pico/bmcu_binary.py
pico/byte_ring.py
```

`bmcu_binary.py`:

- writes BMB1 headers with `struct.pack_into`;
- parses ACK, CONTROL, challenge, and PONG incrementally;
- writes HELLO, BMCU_FRAME, LINK_STATE, DROP, DIAGNOSTIC, LOG, and results;
- uses caller-owned `bytearray` and `memoryview`;
- rejects lengths before copying;
- contains no JSON dependency.

`byte_ring.py` provides:

- fixed-capacity byte or fixed-slot rings;
- caller-provided storage;
- append, peek, release-through-watermark, and bounded iteration;
- protected and replaceable record classes;
- explicit dropped-range reporting;
- restart-safe sequence metadata adapters.

Stores:

1. latest STATUS slot per link;
2. durable EVENT/fault/link/loss/CONTROL-result ring;
3. runtime-log byte ring;
4. bounded TCP receive buffer;
5. bounded TCP transmit staging buffer;
6. journal staging buffer.

Tests:

- wraparound and full behavior;
- protected-event eviction rules;
- STATUS replacement;
- drop-range creation;
- partial parser input;
- maximum-sized LOG and diagnostics;
- no buffer slice is required for partial send.

Exit condition: all production message encoding works without a nested dict.

## 5. Phase P2: preserve raw BMCU frames

Modify `pico/bmcu_link.py` so the validated complete wire frame is available to
the transport sink before or alongside semantic local decoding.

Required callback contract:

```text
on_valid_frame(link_index, received_at_us, wire_memoryview, frame_metadata)
```

`wire_memoryview` is only valid for the callback duration unless copied into a
ring. Transport enqueue copies it directly into its preallocated slot.

Continue decoding only the fields needed for:

- current local display;
- link liveness and snapshot assembly;
- safe CONTROL behavior;
- STATUS classification/coalescing;
- anomaly-priority journaling.

Do not build the former publication dict for the production transport. FULL
STATUS bursts must enqueue raw frames without generating one envelope per
decoded dictionary.

Tests:

- raw bytes equal the accepted UART fixture;
- invalid CRC frames never reach the sink;
- local state still matches existing decoder expectations;
- unknown valid frame kinds can be transported.

Exit condition: UART input can reach a byte ring without `_json_safe()` or
`EnvelopeBuilder`.

## 6. Phase P3: UART scheduling

Replace one-read-per-link behavior with bounded round-robin draining.

Algorithm:

1. inspect backlog for each configured UART;
2. read a bounded chunk from the next link;
3. feed its decoder;
4. rotate links;
5. continue while backlog exists and total byte/time budget remains;
6. return to network/maintenance work;
7. begin the next loop with UART again.

Track per-link:

- current and maximum backlog;
- bytes drained;
- BMCU sequence gaps;
- CRC/frame errors;
- RX overflow indicators;
- maximum service delay;
- resync count.

Remove unconditional work from callbacks. Ping and diagnostic publication are
scheduled after drain.

Tests:

- simultaneous scripted UART input is serviced fairly;
- one noisy link cannot starve the other;
- total budget prevents permanent network starvation;
- snapshot bursts preserve frame order.

Exit condition: dual-link receive no longer depends on one 128-byte read per
main-loop pass.

## 7. Phase P4: persistent BMB1 TCP client

Replace:

```text
pico/bambuddy_ws.py
pico/bambuddy_https.py
pico/bambuddy_session.py
```

with a new production client:

```text
pico/bambuddy_binary_tcp.py
```

The implementation is a cooperative state machine:

```text
DISABLED
WIFI_WAIT
CONNECTING
CHALLENGE_WAIT
HELLO_SEND
ACCEPT_WAIT
ONLINE
BACKOFF
```

Requirements:

- nonblocking socket;
- bounded connect/auth/ACK/idle timeouts;
- HMAC challenge response;
- cumulative ACK processing;
- offset-based partial send using `memoryview`;
- no `buffer[sent:]` slicing;
- live critical records ahead of replay and sampled STATUS;
- PING/PONG liveness;
- deterministic reconnect/backoff;
- no queue mutation on network failure.

Initial production transport is trusted-LAN TCP. If transport encryption is
later required, it is a separate reviewed protocol decision because TLS CPU
work must not be silently introduced into the UART service loop.

Update `bambuddy_config.py`, settings UI, and examples for host, port, device
ID, and device key. The stored key is never returned by a read API.

Tests:

- shared authentication vectors;
- every partial send/receive boundary;
- ACK gaps and rejections;
- disconnect before/after send and before ACK;
- reconnect replay priority;
- duplicate ACK;
- server challenge timeout;
- secret redaction.

Exit condition: Pico can authenticate, deliver raw fixtures, and release only
durably ACKed queue entries.

## 8. Phase P5: append-only local journal

Add:

```text
pico/bmcu_journal.py
```

Implement:

- BMJ1 segment header and record codec;
- 64 KiB rotating segments;
- preallocated staging buffer;
- bounded flush work after UART servicing;
- per-record and header CRC;
- newest-segment tail recovery;
- whole-segment retention;
- separate batched ACK-watermark checkpoint;
- replay iterator that preserves original boot ID and sequence;
- explicit forced-loss records.

Retention:

- always persist EVENT, warning/error/critical, fault, link transition,
  reboot/session, sequence gap, decoder error, transport drop, state
  transition, CONTROL result, and warning-or-higher device log;
- active STATUS at most once per second;
- idle STATUS at most once per 15 seconds;
- immediate meaningful/threshold change;
- denser anomaly window;
- never re-journal replay.

Journal failures produce a RAM diagnostic and retry later. They do not block or
terminate UART processing.

Tests:

- clean append/read;
- power loss at every byte of a final record;
- corrupt header and record CRC;
- segment rotation and deletion priority;
- full storage with protected unacknowledged records;
- checkpoint loss;
- replay after Pico boot with original identity.

Exit condition: important unacknowledged records survive a Pico restart.

## 9. Phase P6: binary runtime log and crash record

Rewrite `pico/runtime_log.py` around preallocated binary PICO_LOG slots.

Replace:

- entry dictionaries;
- arbitrary details dictionaries;
- JSON snapshots;
- `pico_crash.json`;
- `StringIO`-backed unbounded traceback construction where avoidable.

Implement:

- numeric severity;
- fixed maximum component/message/detail sizes;
- typed detail TLV;
- repeated-exception suppression counter;
- bounded traceback tail;
- warning-or-higher journal submission;
- BMCR1 last-crash file with CRC;
- recovery as a PICO_LOG record.

Call sites in `main.py`, Wi-Fi, transport, web UI, and CONTROL use stable
component/tag constants and typed detail values.

Tests:

- truncation at UTF-8 boundaries;
- ring priority and wraparound;
- repeated-exception suppression;
- torn/corrupt BMCR1;
- secret redaction;
- binary fixture equality.

Exit condition: device logging and crash persistence contain no JSON.

## 10. Phase P7: hardware and communications diagnostics

Add:

```text
pico/device_metrics.py
```

Collect bounded numeric values:

- uptime, reset reason, firmware and boot identity;
- free, allocated, and minimum heap;
- internal temperature where supported;
- GC count;
- cooperative-loop busy/idle and maximum delay;
- per-UART backlog, service delay, byte/error/gap/resync counters;
- Wi-Fi state, RSSI, reconnect count;
- TCP state, traffic, reconnect, replay, and ACK age;
- delivery queue depth and drop count;
- journal use, retained range, write failure, and free capacity;
- STATUS replacements;
- exception count.

Do not invent Linux-style CPU percentage. Loop busy/idle and service delay are
the canonical workload indicators.

Emit:

- complete PICO_DIAGNOSTIC after authentication;
- regular snapshot at most once every 15 seconds;
- immediate snapshot on warning/critical boundary crossing;
- lower-frequency normal journal samples;
- higher-frequency anomaly-window samples.

Metric collection must read counters and preexisting state. It must not scan
the journal, format text, invoke GC, or allocate a large snapshot object.

Tests:

- TLV fixtures;
- unknown tags;
- threshold edge transitions without repeated alert storms;
- unavailable temperature/RSSI fields;
- counter wrap behavior.

Exit condition: Bambuddy can render current health and historical metrics.

## 11. Phase P8: binary local web UI

Rewrite `pico/web_ui.py` so PAGE and SETTINGS_PAGE remain static assets while
all live data is binary.

Endpoints:

```text
GET /api/current.bin
GET /api/events.bin?after=<sequence>&limit=<n>
GET /api/history/status.bin
GET /api/diagnostics.bin
GET /api/logs.bin?after=<sequence>&limit=<n>
```

Responses concatenate complete BMB1 messages and use
`application/vnd.bmcu-monitor.v1`. Serve records directly from current slots,
rings, or bounded journal iterators.

Browser JavaScript:

- fetches `ArrayBuffer`;
- incrementally parses BMB1 with `DataView`;
- validates lengths before view creation;
- decodes raw BMCU STATUS/EVENT/FULL STATUS;
- maps registries to labels;
- renders slots, current state, metrics, history, and logs;
- retains last event/log sequence and fetches only deltas;
- displays unknown records safely.

Configuration writes use bounded form encoding or compact binary. CSRF
protection and CONTROL safety remain.

Delete:

- `/api/devices` aggregate JSON;
- `/api/pico/logs` JSON;
- UI `JSON.stringify` diagnostic dump;
- one-second full-state polling;
- per-response unconditional `gc.collect()`.

Tests:

- endpoint bounds and pagination;
- concatenated records;
- static browser decoder fixtures;
- malformed response handling;
- config secret never returned;
- UI requests do not mutate delivery/journal queues.

Exit condition: opening the local UI causes no Pico-side live JSON generation.

## 12. Phase P9: binary CONTROL

Port existing CONTROL semantics to BMB1:

- session-derived HMAC;
- command sequence replay protection;
- issued time and TTL;
- bounded arguments;
- binary CONTROL_RESULT;
- existing safe command ownership only.

Keep idle/motion safety checks and local audit logs. Do not add motor, slot, or
filament movement commands.

Tests consume the shared known-answer and rejection vectors.

Exit condition: permitted CONTROL works without JSON parse/dump.

## 13. Phase P10: main-loop integration and cleanup

Update `pico/main.py` to instantiate preallocated stores during startup and
service work in this order:

1. round-robin UART drain;
2. decoder and RAM enqueue;
3. minimum local state;
4. TCP receive;
5. bounded TCP send;
6. bounded journal flush when UART backlog is empty;
7. local HTTP;
8. safe-point maintenance/GC.

Remove production imports and deployment entries for obsolete transport
modules after integration tests pass. Update:

- `pico/README.md`;
- `docs/PICO_BAMBUDDY_TRANSPORT.md`;
- `docs/PICO_BAMBUDDY_ENVELOPE.md`;
- user guide and config examples;
- deploy script file list.

No JSON telemetry compatibility mode remains.

## 14. Cross-repository integration sequence

1. Lock shared fixtures.
2. Implement Pico codec/rings and Bambuddy codec in parallel.
3. Connect Pico TCP client to Bambuddy fixture server.
4. Connect fixture Pico client to Bambuddy real server.
5. Enable persistence/ACK and replay.
6. Add journal and restart recovery.
7. Add diagnostics, logs, and local binary UI.
8. Add Bambuddy management UI.
9. Migrate CONTROL.
10. Remove JSON WebSocket/NDJSON/local JSON endpoints.

Each step must keep the branch internally testable. Legacy code may coexist
while a phase is under development but is removed before the feature branch is
release-ready.

## 15. Pico definition of done

- Validated BMCU frames enter byte storage without a production dict envelope.
- Dual UART draining is fair and bounded.
- Network and flash failures do not delete unacknowledged important records.
- Journal replay survives restart and preserves original identity.
- TCP sends use caller-owned buffers, offsets, and memoryviews without
  per-partial-send slicing.
- Runtime logs, crash record, metrics, and local UI delivery are binary.
- Bambuddy receives health, logs, local-history status, and raw BMCU events.
- The browser, not Pico Python, performs UI semantic decoding.
- Shared malformed-input, authentication, replay, journal, CONTROL, and
  end-to-end tests pass.
- Obsolete JSON WebSocket, NDJSON, aggregate JSON UI, and JSON log paths are
  removed.

