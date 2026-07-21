# Printer USART1 DMA RX / CPU parser specification

Status: Draft
Date: 2026-07-19
Scope: BMCU printer-facing USART1 ingress only
Related: [BMCU Link v1 implementation plan](BMCU_LINK_V1_IMPLEMENTATION_PLAN.md),
[Pico printer stub](PICO_PRINTER_STUB.md), and
[BMCU validation report](BMCU_LINK_TEST_REPORT_TEMPLATE.md)

## 1. Objective

Replace per-byte printer RX processing with a bounded DMA ingress and a CPU-owned
framer/validator without changing printer-visible behavior.

The design must:

1. avoid full-frame copies and buffer zero-fill in the normal path;
2. keep one authoritative synchronous printer-command handler;
3. preserve the exact accepted requests, state transitions, response bytes, and
   response timing envelope of the current implementation;
4. make every drop, parser abort, DMA error, and overrun observable;
5. provide an authoritative printer-bus quiescent predicate for recovery operations;
6. keep management telemetry optional and unable to block printer or motor work.

This work does not change the Bambu wire protocol, add authorization responses, or
make raw printer frames available to asynchronous consumers.

## 2. Current baseline

The current implementation uses:

- USART1 at 1,250,000 baud, 8E1 through the printer RS-485 transceiver;
- RXNE interrupt processing for every received byte;
- two 1,280-byte RX buffers with pointer exchange on complete frames;
- one published pending frame through `bus_recv_data_ptr`, `recv_data_len`, and
  `bus_package_type`;
- synchronous consumption in `bambubus_run()` or `ahubus_run()`;
- two TX build/DMA buffers;
- compact management EVENT and TX rings after printer handling.

The existing RX buffers are not zero-filled. A published buffer remains owned by
the main loop while `recv_data_len != 0`, so the ISR does not overwrite it.

Known baseline limitations:

- a complete second frame is silently lost if a previous published frame is still pending;
- completed-frame drops, partial-frame aborts, USART overrun, and parser resynchronization
  are not separately counted;
- printer transaction telemetry is produced after the handler and does not yet correlate
  RX, decision, response queued, DMA start, and USART TC with one transaction ID;
- the authoritative reset predicate is implemented, but its reset/abort paths still require target fault injection;
- current automated tests do not execute the USART1 ingress or Bambu parser.

## 3. Non-negotiable invariants

| ID | Invariant |
| --- | --- |
| RXD-001 | DMA or management failure must not change accepted printer commands or response payloads. |
| RXD-002 | DMA must never write into bytes retained by a running handler. |
| RXD-003 | A raw frame view is valid only during synchronous dispatch and is never queued by pointer. |
| RXD-004 | Normal, contiguous frames are parsed without a full-frame copy. |
| RXD-005 | No RX buffer is zero-filled; every read is bounded by validated length. |
| RXD-006 | Invalid length, CRC, overrun, and capacity loss are counted by distinct saturating counters. |
| RXD-007 | Unknown valid commands reach the decision/telemetry path as unsupported; they are not reported as malformed. |
| RXD-008 | Parser work per main-loop service call is bounded. A request awaiting a reply has priority over telemetry. |
| RXD-009 | Printer quiescent is false for partial headers, incomplete bodies, unread frames, pending replies, or active TX. |
| RXD-010 | Management queue saturation can drop management records only, never printer frames or responses. |
| RXD-011 | The existing RXNE implementation remains available behind a build-time fallback until DMA hardware gates pass. |
| RXD-012 | Production RAM remains below 75% and flash below 90%, unless a reviewed measured exception is recorded. |

## 4. Target architecture

```text
USART1 DATAR
    |
    v
continuous circular DMA byte ring
    |
    v
CPU Framer -> Validator -> CommandView -> Safety/ownership -> Handler
                                                        |
                                                        v
                                            compact transaction record
                                                        |
                                                        v
                                            management EVENT/TX rings
```

DMA owns byte transport only. CPU code owns protocol boundaries, validation,
command decoding, safety decisions, response construction, and telemetry.

No header-to-body DMA mode switch is used in the target design. This avoids a
race between the last header byte and the first body byte at 1.25 Mbps.

## 5. DMA ring and ownership

### 5.1 Storage

The initial target reuses the current RX memory budget:

```cpp
alignas(4) uint8_t printer_rx_dma_ring[2560];
```

A non-power-of-two size is intentional: it preserves the existing two-frame memory
budget. Index wrap uses compare/subtract rather than division or modulo in hot code.

No second 1,280-byte packet copy is added. A temporary compatibility copy for a
wrapped legacy handler is permitted only during migration and must have:

- a dedicated counter;
- a measured frequency;
- a removal issue;
- no use for the common contiguous path.

### 5.2 Producer accounting

DMA write position alone cannot distinguish an empty ring from one overrun by a
whole revolution. The implementation therefore maintains a monotonic producer total.

- DMA half-transfer and transfer-complete events advance a producer epoch.
- Main-loop code reads DMA remaining count until two consecutive reads agree.
- `produced_total - consumed_total` is the authoritative available-byte count.
- If available bytes exceed ring capacity, increment `rx_dma_overrun`, discard to a
  validated synchronization boundary, invalidate any partial frame, and never dispatch
  overwritten bytes.

DMA-visible memory ordering must be explicit for the CH32V target. A compiler/memory
barrier is required between a stable DMA position snapshot and reading newly written bytes.

### 5.3 Consumer lifetime

The CPU parser may expose a frame as one or two immutable spans:

```cpp
struct ByteSpan {
    const uint8_t* data;
    uint16_t length;
};

struct PrinterFrameView {
    ByteSpan first;
    ByteSpan second;
    uint16_t total_length;
};
```

The authoritative handler consumes the view synchronously. The consumer total is
advanced only after validation, handler execution, response construction, and compact
transaction-record commit complete.

Raw pointers or spans must not be stored in EVENT, operation, retry, or Bambuddy queues.

## 6. CPU parser

### 6.1 Layers

The parser is split into portable layers:

1. **Framer**: synchronization, short/long/AHub format, header availability, expected length.
2. **Validator**: length bounds, header CRC8, frame CRC16, target/address validity.
3. **Decoder**: produces a bounded `PrinterCommandView` without copying payload data.
4. **Arbiter**: checks state, ownership, and safety preconditions.
5. **Handler**: applies state and builds the exact printer response.
6. **Observer**: commits one compact immutable transaction result.

Hardware register access, DMA accounting, and RS-485 direction control are not allowed
inside the portable framer/validator module.

### 6.2 State machine

```cpp
enum class PrinterParserState : uint8_t {
    SEEK_SYNC,
    WAIT_FORMAT,
    WAIT_HEADER,
    WAIT_FRAME,
};
```

Rules:

- noise scanning is bounded per service call;
- the parser does not inspect body fields before the complete validated frame is available;
- partial frames remain pending until more DMA bytes arrive or a bounded timeout expires;
- invalid headers consume the minimum bytes needed to make forward progress;
- resynchronization must handle a synchronization byte embedded at the rejection boundary;
- a valid unsupported frame is dispatched as unsupported rather than discarded by the framer.

### 6.3 Wrapped frames

CRC and fixed-field readers operate on two spans. They must not assume alignment or
contiguous storage.

The common path has `second.length == 0`. Existing handlers may temporarily use a
compatibility adapter for wrapped frames, but new parser, validator, telemetry, and
soft-reset safety code must use span-safe readers.

### 6.4 Service budget

Production constants must define:

- maximum noise bytes scanned per call;
- maximum complete frames dispatched per call;
- partial-frame timeout;
- maximum frame length.

Budget exhaustion returns to the main loop without discarding valid pending data.
At least one complete request requiring a response is dispatched before lower-priority
management work.

## 7. Transaction and response correlation

Each complete validated printer frame receives a wrapping `transaction_id:u16`.

The compact record must distinguish:

```text
RX_VALIDATED
DECISION
RESPONSE_QUEUED
TX_DMA_STARTED
TX_COMPLETE
TX_FAILED
```

Not every phase requires a separate management EVENT. The latest phase and counters may
be cached, but the same transaction ID must correlate decision and response completion.

For `0x040D` and `0x040E`, retain the full `type:u16`, payload length, bounded
fingerprint, outcome, reason, response length, and RX tick. No certificate or authorization
payload is synthesized.

## 8. Quiescent predicate

The printer bus is quiescent only when all conditions are true:

```text
parser state == SEEK_SYNC
available DMA bytes == 0
no partial-frame timeout pending
no frame retained by a handler
no response queued
RX DMA has no unaccounted error/overrun
TX DMA inactive
USART transmission complete
RS-485 DE is receive
elapsed time since last RX byte or TX complete >= requested interval
```

This predicate is authoritative for a future soft reset. Telemetry history is not used as
a safety predicate because telemetry may be dropped.

## 9. Required counters

All counters saturate at their declared width and are snapshot-readable:

| Counter | Meaning |
| --- | --- |
| `rx_bytes` | DMA-accounted USART1 bytes |
| `rx_frames_valid` | complete frames passing header and body validation |
| `rx_bad_length` | impossible or over-limit declared length |
| `rx_header_crc_error` | short/long header CRC8 rejection |
| `rx_body_crc_error` | complete-frame CRC16 rejection |
| `rx_partial_timeout` | incomplete frame abandoned after timeout |
| `rx_resync_bytes` | bytes skipped while searching for a valid boundary |
| `rx_dma_error` | DMA transfer error |
| `rx_usart_overrun` | USART overrun |
| `rx_dma_overrun` | producer overtook consumer/ring capacity |
| `rx_dispatch_budget_hit` | work deferred by the per-call budget |
| `rx_wrap_frame` | valid frame split across ring end |
| `rx_compat_copy` | transitional wrapped-frame full copy |
| `tx_started` | USART1 DMA responses started |
| `tx_completed` | USART TC observed after DMA completion |
| `tx_response_busy` | handler skipped because a prior response remained queued |
| `tx_response_missing` | response-required handler returned without building a response |
| `tx_invalid_length` | response rejected before DMA start because its length was invalid |
| `tx_dma_error` | DMA1 Channel 4 transfer-error flag observed |
| `tx_timeout` | TX aborted because USART TC was not observed within 25 ms |
| `tx_no_response_expected` | handler explicitly selected normal protocol silence; not a TX failure |

## 9.1 Implementation status

The first DMA migration increment is implemented with `BMCU_PRINTER_RX_DMA=1` as the
source default and `BMCU_PRINTER_RX_DMA=0` as the RXNE rollback build:

- USART1 RX uses DMA1 Channel 5 in a 2,560-byte circular ring;
- HT/TC events plus stable `CNTR` reads provide the monotonic producer position;
- TX echo, DMA errors, USART overruns, and producer-over-consumer overruns are handled separately;
- the authoritative quiescent check includes unconsumed DMA bytes;
- three full-status records expose the current ingress, loss, and DMA counters;
- two additional full-status records expose printer TX progress, response classification, DMA error, and timeout counters;
- TX DMA TE and a 25 ms completion timeout share one recovery path that disables TX DMA, clears flags, returns DE to RX, and releases the bus;
- response classification uses a hardware-independent result helper with native regression vectors;
- `printer_rx_framer` is hardware-independent and directly covered by native golden vectors;
- contiguous frames are published as pointers into the DMA ring without a full-frame copy;
- the DMA consumer remains pinned until the synchronous handler releases the frame;
- USART error recovery observed while a frame is retained is deferred until that release;
- retained-frame ring overrun is detected and counted at release;
- only a frame crossing the ring boundary is copied into one 1,280-byte compatibility buffer.

`rx_compat_copy` now increases only for wrapped frames. This increment removes one legacy
1,280-byte packet buffer; the final two-span handler conversion removes the remaining wrap
buffer and its copy. At 1.25 Mbps with 8E1 framing, a maximum 1,280-byte frame leaves at
least another 1,280 bytes, approximately 11.3 ms, before DMA can overwrite its retained
start. Handler latency must remain below that interval with margin; the existing
`rx_dma_overrun` counter makes a violation observable only after release and cannot make an
already-overwritten direct view safe. Hardware timing and endurance gates therefore remain
open.

## 10. Migration plan

### Stage A — characterization, no behavior change

- add missing ingress/drop/error counters to the current RXNE/double-buffer implementation;
- capture current request/response golden vectors from real printer and Pico stub;
- record printer RX ISR, parser/handler, and response latency distributions;
- record RAM, flash, and hot-function assembly baseline.

### Stage B — portable parser extraction

- extract framer and validator from `_bus_hardware.h` into a hardware-independent module;
- feed the new module from the existing RXNE path;
- preserve current handler entry points;
- run host unit tests and golden differential tests before enabling DMA.

No DMA code is merged in Stage B if any existing response or state-transition vector differs.

### Stage C — DMA ingress behind a build flag

- add continuous circular DMA producer;
- feed the same tested portable parser;
- retain `BMCU_PRINTER_RX_DMA=0` as the production rollback path;
- build and test both modes in CI;
- compare request/response output and state snapshots between modes.

### Stage D — hardware validation

- exercise real printer and isolated Pico stub;
- run minimum-gap bursts, CRC faults, partial frames, wrap positions, TX/RX direction changes,
  and persistence/calibration interference;
- complete endurance and timing gates;
- enable DMA by default only after all P0 hardware gates pass.

The RXNE fallback is removed only in a later reviewed change with equivalent field evidence.

## 11. Current test coverage audit

“Covered” below means the current test executes the modified behavior, not merely that the
firmware compiles.

| Area to be modified | Current evidence | Current direct coverage | Required before DMA default |
| --- | --- | --- | --- |
| `_bus_port_deal::irq()` framing | firmware build only | **none** | portable framer unit tests and differential corpus |
| RX double-buffer ownership | source inspection only | **none** | producer/consumer state-model tests and hardware burst test |
| USART1 RXNE/ORE behavior | build and historical printer use | **none automated** | target test with ORE/error injection and logic trace |
| DMA1 USART1 RX setup | not implemented | none | register/config test plus target smoke test |
| DMA producer epoch/CNTR accounting | not implemented | none | wrap, exact-full, multi-epoch, overrun model tests |
| short Bambu framing | real-use evidence only | **none automated** | split-at-every-byte and golden corpus tests |
| long Bambu framing | real-use evidence only | **none automated** | min/max/wrap/CRC/unsupported-type tests |
| AHub framing on shared ingress | real-use evidence only | **none automated** | retained AHub golden vectors and differential tests |
| `bambubus_run()` dispatch | firmware build only | **none** | handler spy tests and response golden vectors |
| heartbeat fast path | source inspection only | **none** | cadence, truncated, timeout, and recovery tests |
| reset quiescent policy | pure host policy vectors + firmware build | request/safety decisions covered | target partial-RX/TX/DE/error/tick-wrap fault injection |
| management alpha.3 codec | host/Pico corpus | covered | retain existing corpus |
| Pico snapshot/event handling | `ci/test_pico_monitor.py` | covered | retain existing tests |
| issue #3 auth trace decoder | host and Pico unit tests | covered | retain and add real ingress vectors |
| build variants | firmware matrix generation and representative build | compile coverage | build RXNE and DMA modes across required profiles |
| timing and no-copy claims | report template only | **none** | assembly/timing baseline plus hardware measurements |

The existing management/Pico test count must not be presented as Printer RX parser coverage.
Before parser implementation, direct automated coverage of the target USART1 ingress is effectively zero.

## 12. Required regression suite

### 12.1 Portable parser tests — mandatory in CI

Every case runs with all meaningful input split positions and selected ring wrap positions.

- empty input and noise only;
- synchronization split across service calls;
- valid short frame, minimum and maximum valid lengths;
- valid long frame, minimum and maximum valid lengths;
- valid AHub frame;
- multiple frames in one DMA availability window;
- partial header and partial body;
- invalid/zero/over-limit length;
- bad header CRC8;
- bad body CRC16;
- target mismatch;
- unknown short command;
- unknown long type, including `0x040D` and `0x040E`;
- frame ending exactly at ring end;
- header and CRC fields split at ring end;
- partial timeout followed by a valid frame;
- synchronization byte at a rejected-frame boundary;
- tick counter wrap during timeout calculation;
- service-budget exhaustion without data loss.

All parser states and all rejection exits require at least one test. Once the portable C++
module is host-buildable, CI targets at least 90% line and 85% branch coverage, while the
enumerated state-transition/error matrix remains the authoritative gate.

### 12.2 Differential behavior tests — mandatory in CI

For the same captured request corpus, RXNE and DMA modes must produce identical:

- classification and decision reason;
- AMS/slot state changes;
- response length and response bytes;
- response/no-response decision;
- transaction type and bounded fingerprint;
- heartbeat online/offline transition.

Expected differences are limited to transport counters and timing measurements.

### 12.3 DMA/ring model tests — mandatory in CI

- stable CNTR snapshot;
- half/full epoch transitions;
- producer and consumer wrap;
- exactly empty versus exactly full;
- producer overtakes consumer by one byte;
- overrun by more than one revolution;
- handler view retained until synchronous completion;
- consumer advancement only after dispatch;
- no stale descriptor after resynchronization.

### 12.4 Target hardware tests — mandatory before default enable

- logic-analyzer proof of no missing first/body byte;
- minimum observed inter-frame gap burst;
- real printer boot/discovery and normal print start;
- read-only and state-changing requests;
- `0x040D/0x040E` observation;
- CRC/header corruption and recovery;
- DMA transfer error/USART ORE recovery;
- RX frame spanning the physical ring boundary;
- RX during response direction changes;
- calibration and persistence interference;
- soft-reset quiescent predicate fault injection;
- 8-hour idle and active endurance.

## 13. Merge and rollback gates

A parser/DMA change must not merge unless:

1. all existing CI tests pass;
2. new parser state/error tests pass;
3. RXNE-versus-DMA differential corpus is identical;
4. `git diff --check` passes;
5. representative RXNE and DMA firmware builds pass;
6. RAM/flash and hot-path assembly deltas are recorded;
7. no new unbounded loop, dynamic allocation, full-buffer zero-fill, or unconditional
   full-frame copy is present;
8. hardware-dependent claims remain marked open until measured;
9. rollback firmware with `BMCU_PRINTER_RX_DMA=0` remains buildable.

Default enable additionally requires all P0 hardware cases in the validation report.

### 13.1 Current measured build exception (2026-07-21)

The default DMA `fw` build uses 15,464 / 20,480 RAM bytes (75.5%) and
55,108 / 61,440 flash bytes (89.7%). RAM is 104 bytes above the nominal 75%
RXD-012 threshold; this is a recorded measured exception, not a claim that the
budget gate passed. The RXNE rollback build uses 14,140 RAM bytes (69.0%) and
54,056 flash bytes (88.0%). Both builds pass, and the default artifact remains
the DMA build. Review of the 104-byte RAM exception and P0 hardware validation
remain required before closing issue #3.

## 14. Open decisions

The following are intentionally unresolved until Stage A measurements:

- 2,560-byte ring versus a larger power-of-two ring;
- main-loop polling only versus USART IDLE/HT/TC wake hints;
- whether wrapped legacy handlers justify a temporary scratch buffer;
- service byte/frame budgets and partial timeout values;
- duration required by the authoritative quiescent predicate;
- production exposure width for the new counters.
