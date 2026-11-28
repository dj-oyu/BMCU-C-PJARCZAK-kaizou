# BMCU Monitor Stream v2 (UDP, unacknowledged)

Status: **agreed 2026-11-26; the Pico side is implemented** (alpha `06350bb`:
`pico/bmcu_stream_udp.py`, `pico/local_reset.py`). Supersedes the delivery,
session and
control parts of [BMCU_BINARY_TRANSPORT_V1.md](BMCU_BINARY_TRANSPORT_V1.md).
The BMB1 message header (V1 §4) and the record payloads (V1 §6, §7.1, §7.2,
§12.1) are unchanged and are reused as-is.

Audience: the Pico bridge (`pico/`, this repository) and the Bambuddy receiver
(`dj-oyu/bambuddy`, `backend/app/services/bmcu_binary/`). Each side can be built
from this document alone.

## 1. Why v1 delivery is replaced

v1 delivers every non-STATUS record durably, stop-and-wait. The Pico sends one
record, waits for Bambuddy's ACK, then sends the next. Bambuddy commits one
database transaction per record before it ACKs. With two BMCUs on the bus,
measured on 2026-11-26:

| | |
|---|---|
| records offered by the BMCUs | ~29 /s |
| records drained (ACKed) | ~23 /s, the stop-and-wait ceiling |
| records rejected, durable queue full | ~6 /s, 24,252 in one hour |
| TRANSPORT_DROP markers in the queue | 87 of 142 records (61 %) |

The queue never drains, so loss is continuous. Most of what is offered is
high-rate telemetry (printer bus transaction warnings) that gains nothing from
per-record acknowledgement.

## 2. Decisions

1. **The Pico is a faithful bridge.** It forwards every record it receives from
   a BMCU, unaltered. It does not aggregate, filter, deduplicate or summarise.
   The BMCU firmware does not throttle its own records to suit the transport.
   All processing (statistics, deduplication, sampling, retention) belongs to
   the receiver.
2. **Data travels over UDP, unacknowledged.** The Pico never waits for the
   receiver. A lost datagram is lost; the receiver detects the gap from the
   transport sequence and records it.
3. **No authentication on the data path.** The Pico and the receiver share a
   trusted home LAN. A forged datagram can only pollute stored telemetry. It
   cannot move a motor or change device state, because the stream carries
   nothing towards the Pico.
4. **No network control from the receiver.** Bambuddy never sends anything to
   the Pico. The only control action that exists, the BMCU soft reset, is
   available only on the Pico's own local HTTP interface, and needs a 4-digit
   code shown on the Pico's OLED panel (§7). The rule is: whoever can see the
   panel is physically present and is trusted.
   - **Why not remote reset:** a reset clears the BMCU's merger ownership. Done
     mid-print without seeing the machine, it recreates the merger desync of
     handover.md §10.5 (the strand is cut and the buffer stuffs).
   - The soft reset travels over the monitor link, so it cannot rescue a BMCU
     that is truly hung anyway.
   - Pausing the printer remotely remains the safe remote action.
   - The token-gated long-reply route of §7.4 is also local HTTP, not part of
     the stream, and cannot reset or move anything.
5. **No journal replay.** The Pico stops writing the BMJ1 flash journal and
   stops replaying earlier boots. History lives only at the receiver.
6. **The receiver persists in batches**, not one transaction per record.

Removed relative to v1: the TCP session, SERVER_CHALLENGE / HELLO /
HELLO_ACCEPTED, ACK, PING / PONG, CONTROL / CONTROL_RESULT, device keys and
provisioning, the FLAG_REPLAY path, and the Pico flash journal.

## 3. Transport

- **UDP, Pico to receiver only.** The destination is the existing
  `BMCU_BINARY_HOST` / `BMCU_BINARY_PORT` setting, port 8799 by default. The
  receiver binds UDP on the same port number the v1 TCP listener used.
- The receiver never sends anything to the Pico. The Pico does not listen on
  UDP.
- **Datagram size:** at most **1200 bytes** of UDP payload, so that a datagram
  never fragments on Wi-Fi or a tunnel. A record that cannot fit on its own (a
  BMB1 message over 1200 − 12 bytes) is counted on the Pico as
  `reject_oversize` and is not sent. No such record exists today: the largest
  BMCU frame is well under 300 bytes.
- **Pacing:** the Pico sends whenever its queue is non-empty, up to a per-loop
  budget (§6.3). There is no window and no retransmission.

## 4. Datagram format

All integers are big-endian.

```text
offset size field
0      4    magic "BMS2"
4      1    version = 1
5      1    flags (reserved, 0; receivers ignore unknown bits)
6      1    device_id length N, 1..63
7      N    device_id, ASCII, as v1 BRIDGE_ID / BMCU_BINARY_DEVICE_ID
7+N    1    record count R, 1..255
8+N    ...  R BMB1 messages back to back, each a complete v1 message
            (32-byte header + payload, V1 §4), parsed by its own length field
```

Envelope overhead is `8 + N` bytes. Each record keeps its own
`pico_boot_id`, `transport_sequence` and `link_index` in its BMB1 header, so the
envelope carries nothing that a record does not already say.

The receiver:

1. drops a datagram with the wrong magic or version, or with a truncated record,
   and counts it per source address;
2. drops a datagram whose device_id is not in the receiver's device allowlist
   (§8.4), and counts it;
3. otherwise accepts every complete record in it independently. One bad record
   (an unknown type or a malformed payload) is counted and skipped; the rest of
   the datagram is still accepted.

## 5. Records

### 5.1 Sequenced records

These message types are sequenced: `BMCU_FRAME` (0x10), `LINK_STATE` (0x11),
`TRANSPORT_DROP` (0x12), `PICO_DIAGNOSTIC` (0x13) and `PICO_LOG` (0x14).

- `transport_sequence` is one counter per Pico boot, shared by all links and
  types. It starts at 1, increases by exactly 1 per record, and is never reused
  within a boot. **A gap is loss.** It is either reported by the Pico (below) or
  detected by the receiver (§8.2).
- **STATUS is the one coalesced kind**, as in v1 §9.1. The Pico keeps the latest
  STATUS per link, and a newer STATUS replaces an unsent older one. The count of
  replacements is `status_replacements` in PICO_DIAGNOSTIC. When the send path
  is not backed up, every STATUS is forwarded.
  - Coalescing is bridge buffering, not processing: the replaced STATUS never
    left the Pico and has no sequence number.
  - A STATUS is given its sequence number when it is taken for sending.
- **TRANSPORT_DROP** (V1 §7.2) is still emitted by the Pico, with reason
  `RAM_QUEUE_FULL`, when its own send queue overflows. That can now happen only
  while the network path is down or Wi-Fi is congested.
  - The marker waits until a quarter of the queue is free
    (`BMB1Outbox.drop_marker_free_slots`) so that one marker covers a whole
    overload run.

### 5.2 HEARTBEAT (new message type 0x42)

A HEARTBEAT is not sequenced (`transport_sequence` = 0) and is never coalesced.
`link_index` = 0xFF. It replaces v1's HELLO (identity) and PING (liveness).

```text
offset size field
0      4    heartbeat_interval_ms          the Pico's own interval (§6.2)
4      8    uptime_ms
12     8    next_transport_sequence        the sequence the next record will
                                           take, read when the heartbeat is
                                           built; every lower sequence has
                                           already been allocated. Lets the
                                           receiver detect a trailing gap with
                                           nothing after it
20     1    firmware length F, 1..31
21     F    firmware, ASCII (PICO_FIRMWARE_VERSION)
21+F   1    link count L, 0..4
...    L x  { link_index u8, link_id length u8 (1..31), link_id ASCII,
              link_state u8 (V1 §7.1 codes) }
```

The receiver uses it to:

- **register the device, the boot and the links**, which v1 did from HELLO. A new
  `pico_boot_id` in any record or heartbeat starts a new boot.
- **decide liveness** (§8.3).
- **detect a trailing gap**: if `next_transport_sequence - 1` exceeds the
  highest sequence received for that boot, the records in between were lost.

## 6. Pico behaviour

### 6.1 Queue

The v1 outbox rings (`pico/bmcu_binary_outbox.py`) stay: the latest-STATUS slot
per link, the durable ring and the large ring. "Acknowledged" now means **handed
to the UDP socket**: once `sendto` accepts a datagram, every record in it is
released. A record is never sent twice.

- While Wi-Fi is down, or `sendto` raises, records accumulate up to the ring
  capacity, and overflow becomes a TRANSPORT_DROP as in v1.
- `EAGAIN` / `ENOMEM` from `sendto` leaves the records queued; the Pico retries
  them on the next loop.

### 6.2 Heartbeat

- One HEARTBEAT every **5 s** (`BMCU_STREAM_HEARTBEAT_MS`, default 5000).
- One immediately after Wi-Fi comes up, and after the endpoint is changed
  in the local UI.
- A heartbeat travels in a datagram of its own, never behind queued records, so
  that a backlog cannot delay it.

### 6.3 Packing and budget

- Records are packed in queue order into one datagram until the next one would
  exceed 1200 bytes or 255 records.
- At most `BMCU_STREAM_DATAGRAMS_PER_LOOP` datagrams per main-loop pass
  (default 4), so the UART drain keeps its share of the loop.

### 6.4 Removed on the Pico

- `bambuddy_binary_tcp.py`, and the challenge / HELLO / ACK / PING / CONTROL
  code paths.
- The BMJ1 journal: staging, flushing, replay cursor and boot ranges
  (`bmcu_journal.py` usage in `main.py`). The module may stay in the tree,
  unused, until a cleanup commit removes it.
- Device key provisioning: `BMCU_BINARY_DEVICE_KEY`, `/api/device-key`.

`/api/transport` (endpoint host/port in the local UI) stays and sets the UDP
destination.

## 7. Local soft reset with an OLED code

The only control action. It is served by the Pico's local HTTP interface
(`pico/web_ui.py`) and used from the Pico's own web UI (`web/`, built into
`pico/www/index.html.gz`). Bambuddy has no part in it.

### 7.1 Flow

1. `POST /api/control/reset-request` with body `{"link": <link_index>}`.
   - The Pico draws a uniformly random 4-digit code, 0000-9999, from
     `os.urandom`.
   - It shows the code on the OLED with the link name, for example
     `RESET bmcu-b` / `4827`, and the seconds left.
   - It answers `202 {"expires_in_s": 60, "attempts": 3}`.
   - **The code never appears in any HTTP response, log, record or datagram.**
2. The person reads the code off the panel and enters it in the web UI.
3. `POST /api/control/reset` with body `{"link": <link_index>, "code": "4827"}`.
   - On a match, the Pico runs the existing soft-reset path for that link (the
     v1 `CONTROL_SOFT_RESET` handler and its guards, unchanged) and answers
     `200 {"result": "accepted"}`.
   - If the guards refuse (motor running, stale snapshot, ...), it answers
     `409 {"error": "<guard reason>"}`. The code is consumed either way.

### 7.2 Rules

- **One pending code at a time.** A new request replaces any pending one,
  whichever link it was for.
- **The code expires 60 s after the request.** After that, a reset answers
  `410`.
- **3 wrong attempts invalidate the code**: `403 {"attempts_left": n}` for each
  wrong one, and `410` once none are left. A fresh request is then needed.
  - Guessing is bounded at 3 tries per code shown on the panel, so a 4-digit
    space is enough.
- **The code is single use**, consumed on the first correct entry whatever the
  handler's result.
- **No pending code:** `409`.
- A reset whose link does not match the pending request is a wrong attempt.
- **No panel, no reset.** If the OLED did not initialise or stopped answering,
  `reset-request` answers `503` and no code is created. Fail-safe: no network
  reset is possible without the panel.
- **The panel shows the code until it is used, expires or is invalidated**,
  pausing the normal page rotation. Afterwards it shows the result for 3 s
  (`OK`, `FAILED` for a guard refusal, `EXPIRED`, `CANCELLED` after three
  wrong codes) and resumes rotation.
- Rate limit: at most one `reset-request` per 5 s (`429`), so that a script
  cannot keep the panel occupied.

### 7.3 What this does and does not establish

It establishes that the requester could see the Pico's panel within the last
60 s. It does not authenticate the HTTP client, and the code crosses the LAN in
clear text. Someone on the LAN who can also see the panel is, under the trust
rule above, someone allowed to reset. A sniffed code is useless once it has
been used or has expired.

### 7.4 Long-reply requests with a shared token

A second local control route, for a PC or an agent on the LAN that wants the
BMCU to answer a printer long frame it does not answer today (0x0411, 0x023C,
0x0237, see `PRINTER_LONG_PAYLOAD`). It is served by `pico/long_reply.py`.
Bambuddy still sends nothing to the Pico.

- **Off unless provisioned.** `secrets.py` must define `BMCU_CONTROL_TOKEN`, at
  least 16 characters. Without it every request answers `503`.
- **Every request carries `X-BMCU-Control-Token`.** POST also needs
  `X-BMCU-Control-Action: long-reply` and `Content-Type: application/json`.
- `POST /api/control/long-reply` with
  `{"link": 0, "type": "0x0411", "match": "01", "reply": "01000000"}` sends
  `SET_LONG_REPLY` (BMCU_LINK_PROTOCOL_ALPHA3.md §5.5) and answers
  `202 {"link", "sequence", "state": "sent"}`. `{"link", "type", "clear": true}`
  asks for that type's entry to be dropped. `match` is at most 8 bytes and
  `reply` at most 32, both hex.
- `GET /api/control/long-reply?link=0` reports the last request on that link:
  `state` is `sent`, `applied` (ACK OK), `rejected` (any other ACK, named in
  `ack`) or `no_ack` after 2 s of silence.
- **5 wrong tokens lock the route for 60 s** (`429`), including for the right
  token. A right token resets the count.
- The token never appears in a response, the runtime log, a record or a
  datagram.

What it establishes: the client knows the token. The token crosses the LAN in
clear text, so anyone who can read LAN traffic can reuse it. That is weaker
than the panel rule of §7.3, and it is accepted because a long reply cannot
move a motor. It can still change what the printer believes about the AMS, so
the BMCU side keeps the table in RAM only: a power cycle always returns the
BMCU to answering nothing.

A BMCU whose firmware predates the handler answers every request
`rejected` with `ack: "unsupported"`. That still proves the route end to end
(client → Pico → BMCU → Pico) without changing what the printer sees. With the
handler, a request reads `applied`, or `rejected` with `ack: "denied"` when all
eight entries are taken (BMCU_LINK_PROTOCOL_ALPHA3.md §5.5).

## 8. Receiver (Bambuddy) behaviour

### 8.1 Ingest

- An asyncio UDP endpoint on `bmcu_binary_host:bmcu_binary_port`.
- **Never awaits I/O in the datagram callback.** It parses the envelope,
  validates the header of each record (V1 §4 rules: magic, version, length,
  known type, sequenced types have a nonzero sequence, HEARTBEAT has zero), and
  appends the records to a bounded per-device ingest queue.
- The ingest queue holds **8192 records** by default. On overflow the newest
  record is discarded and counted as `ingest_overflow`. Those sequences then
  appear as gaps (§8.2) with reason `RECEIVER_OVERFLOW`, so the loss is visible
  in the same place as network loss.

### 8.2 Batch persistence and gap detection

- A flush task per device writes the ingest queue in **one transaction**
  whenever 250 ms has passed or 512 records are pending, whichever comes first.
  Both values are settings.
- Records are deduplicated on `(device_id, pico_boot_id, transport_sequence)`,
  using one `IN` query per batch rather than one per record.
- **No ACK watermark.** `last_ack_sequence` and the
  `advance_contiguous_with_losses` scan are not used on this path, because a
  single lost datagram would stall a contiguous watermark forever.
- **Gap detection**, per `(device, boot)`:
  - The receiver keeps the highest sequence seen.
  - A record that jumps past it opens a candidate gap.
  - A candidate gap is written as a loss range only after a **2 s reorder
    window** without being filled. UDP on a LAN rarely reorders, but it can.
  - A late record that fills part of a written gap is still stored, and the
    stored loss range is split or shrunk.
  - A gap that covers sequences the Pico already reported in a TRANSPORT_DROP
    is not recorded twice. The Pico's report wins, because it carries the real
    reason.
- New loss reasons, extending V1's DropReason (`DropReason` in
  `constants.py`):

  | value | name | meaning |
  |---|---|---|
  | 6 | `NETWORK_GAP` | sequences never received, after the reorder window |
  | 7 | `RECEIVER_OVERFLOW` | received, but discarded by the receiver's ingest queue |

- In-memory derived state (current STATUS view, last event, last BMCU HELLO,
  full-status pairing; `persistence.py` after `commit`) is updated per record
  in sequence order after the batch commits, exactly as `persist()` does today.

### 8.3 Liveness

- The device is **online** while a HEARTBEAT arrived within **3 ×
  heartbeat_interval_ms** (15 s at the default).
- It is **stale** after that, and **offline** after 60 s.
- Data records alone do not keep a device online, so liveness means "the bridge
  is running and reaching us", not "some packet arrived".
- The online state, last heartbeat time, uptime and link table are exposed where
  v1's session presence was (the monitor list and detail endpoints).

### 8.4 Device allowlist

- Records are accepted only for device_ids the receiver knows: configured in
  settings, or already present in `bmcu_binary_devices`.
- `BAMBUDDY_BMCU_STREAM_ACCEPT_NEW=1` accepts and registers an unknown device_id
  on its first heartbeat. Use it for first setup.
- This is not security (§2.3), only protection against strays and typos.

### 8.5 Control

- `POST /bmcu-monitors/{device_id}/control` and the session registry's control
  path go away.
- Any UI that offered a reset from Bambuddy should be removed. Optionally,
  replace it with a link to the Pico's local UI at the source address of the
  latest heartbeat.

### 8.6 Toggles and transition

Per the Bambuddy fork's rules:

- `BAMBUDDY_BMCU_STREAM_UDP` (default `1`) enables the UDP receiver.
- During the transition the v1 TCP listener may stay enabled side by side. A v1
  Pico keeps working until it is reflashed.
- Once both Picos run v2, the TCP path is removed in its own commit.

## 9. Diagnostics

PICO_DIAGNOSTIC (V1 §12) keeps its TLVs. On v2:

- these are always 0: `DIAG_LAST_ACK_WATERMARK`, `DIAG_REPLAY_COUNT`,
  `DIAG_TCP_*`, and the journal counters;
- new TLVs, added to `docs/bmcu_binary_registry.json` with the implementation:
  - `DIAG_UDP_DATAGRAMS_SENT` = 90
  - `DIAG_UDP_RECORDS_SENT` = 91
  - `DIAG_UDP_SEND_ERRORS` = 92
  - `DIAG_UDP_BYTES_SENT` = 93
  - `DIAG_HEARTBEATS_SENT` = 94

The receiver should expose its counters on its monitor detail endpoint:
datagrams received, envelope rejects, record rejects by reason, ingest
overflow, gaps detected, batch size and flush duration.

## 10. Acceptance

The work is done when:

1. With both BMCUs on the bus and the 2026-11-26 load, Pico-side
   `reject_durable_full_count` stays at 0 while Wi-Fi is up.
2. The receiver stores every record it receives. Its loss ranges account
   exactly for the sequences it did not store, and none are recorded with
   reason `RAM_QUEUE_FULL` while Wi-Fi was up.
3. Unplugging the receiver (or stopping Bambuddy) costs the Pico nothing but
   queue overflow. On recovery the device comes back online within one
   heartbeat interval, and the outage appears as one loss range.
4. The soft reset works from the Pico web UI with the code on the panel. It
   refuses a wrong code three times then invalidates it, refuses after 60 s,
   and refuses entirely with the panel unplugged.
5. Unit tests on both sides cover the datagram format byte for byte, from a
   shared fixture, written by the Pico side (§11).

## 11. Numbers and fixtures

Registry values added in `docs/bmcu_binary_registry.json`:

| kind | name | value |
|---|---|---|
| message type | `HEARTBEAT` | `0x42` |
| drop reason | `NETWORK_GAP` | 6 (receiver only) |
| drop reason | `RECEIVER_OVERFLOW` | 7 (receiver only) |
| diagnostic tag | `UDP_DATAGRAMS_SENT` .. `HEARTBEATS_SENT` | 90 .. 94 |

Fixtures are generated by `ci/generate_bmcu_stream_fixtures.py` with the Pico
client itself and kept fresh by `ci/test_pico_stream_fixtures.py`. They live
in `tests/fixtures/bmcu_stream/` here and are mirrored byte for byte to
`backend/tests/fixtures/bmcu_stream/` in Bambuddy:

- `heartbeat.bin`: one datagram holding one HEARTBEAT, for device
  `pico-bmcu-bridge`, boot `0x0102030405060708`, two links (`bmcu-a` online,
  `bmcu-b` stale), next sequence 4;
- `records.bin`: one datagram holding LINK_STATE (sequence 1, link 1),
  BMCU_FRAME (2, link 0, an EVENT wire frame) and PICO_DIAGNOSTIC (3, global);
- `manifest.json`: size and SHA-256 of each file, and under `expect` the
  decoded fields a receiver must produce for each.

The Pico-side reader in `ci/test_bmcu_binary_e2e.py` (`parse_datagram`,
`parse_heartbeat`) is written from this document with `struct` alone, and is
a reference for the receiver's decoder.
