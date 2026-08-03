# The BMCU + Pico pair as a debug probe

Status: design
Date: 2026-08-03
Scope: what the Pico bridge and the BMCU's instrumentation must guarantee when
they are used to diagnose a fault, rather than to display state

## 1. Why this document exists

On 2026-08-03 a filament change failed on an A1 mini running printer firmware
1.08. The probe was attached to both BMCUs, both links were online, and the UART
was flawless — zero CRC errors, zero frame errors, zero sequence gaps, zero
overflows on both links across 23 MB and 25 MB of traffic.

The probe still could not answer the question.

The cause was eventually found by reading a single field in a cached snapshot:
the printer was sending long-frame type `0x0411`, and the BMCU was answering it
with `OUTCOME_IGNORED` / `REASON_UNSUPPORTED`. Everything else needed to
characterise that — how often, since when, with what payload, in what relation
to the filament-change attempt — was unavailable.

Nine distinct properties of the probe contributed. Each is a design defect, not
a bug in the ordinary sense, because in each case the system did what it was
built to do.

| # | Observed | Why it defeated the investigation |
| --- | --- | --- |
| 1 | `notable` at `bmcu_link.cpp:1347` emits a long-transaction event only for `0x040D`, `0x040E`, `REJECTED` or `FAILED` | `0x0411` is `IGNORED`, so no event was ever emitted. Only a one-slot "most recent" field in a snapshot survived |
| 2 | `tick_hz` was 0, because `HELLO` is sent once, at `bmcu_link_init` | No tick-to-seconds conversion. Every timestamp was uninterpretable |
| 3 | `hw_tick32` runs at 18 MHz and wraps every ~238 s | Even with `tick_hz`, correlation beyond four minutes is ambiguous |
| 4 | The event endpoint held 32 `TRANSPORT_DROP` records, `RAM_QUEUE_FULL`, and nothing else | Every BMCU event had been evicted by volume |
| 5 | `STATUS_REPLACEMENT_COUNT` was 439,220 | Transient states are coalesced away by design; a state that exists for 200 ms may never be transmitted |
| 6 | `journal.flush` raised `OSError: 84` (`LFS_ERR_CORRUPT`) while `JOURNAL_FAILURE_COUNT` and `EXCEPTION_COUNT` both read 0 | The counters are per-boot and had been reset; the retained ERROR outlived them. A reader sees zeros and concludes healthy while the log says otherwise. Separately, the outbox's own staging-failure counter was incremented at four sites and reported at none |
| 7 | `HEAP_MIN_FREE` was 32 bytes | The bridge was one allocation from failure while reporting itself healthy |
| 8 | `LOOP_MAX_DELAY_US` was 627,000 | Cooperative service stalled for over half a second |
| 9 | `/api/snapshot.bin` is refreshed only when the bridge is idle | The snapshot cannot be sampled during the moment of interest, which is never idle |

Two further constraints emerged from the attempt to recover:

- The Pico runs from a USB power adapter with no host attached. There is no
  remote maintenance path — no way to clear a corrupt journal, restart the
  application, or deploy a fix without physically moving the device.
- The one instrument that worked perfectly was the DMA UART ingress. Under a
  627 ms service stall it still lost nothing. That is the standard the rest of
  the probe should be held to.

## 2. What the probe is for

Three roles, with different and partly conflicting requirements. Conflating them
is what produced the failure above.

**Role A — live display.** Current state of eight channels. High rate, lossy by
design, only the newest value matters. Coalescing is correct here.

**Role B — incident capture.** A rare event happened and must be explained after
the fact. Loss is unacceptable; latency is irrelevant; volume is low.

**Role C — bulk trace.** Characterising a protocol or a timing problem. High
volume, bounded duration, triggered deliberately, read out offline.

Today the probe implements A well, implements B as a side effect of A, and does
not implement C at all.

## 3. Requirements

### R1. The device must not decide what is interesting

`notable` encodes a diagnostic policy at the emission site, in firmware, months
before the question is asked. `0x0411` was invisible because someone had to
guess in advance which frame types would matter.

Replace judgement with cheap totals. The device always counts; the host asks for
detail. A counter per `(long type, outcome)` costs a few bytes and answers "what
is this printer asking for that we ignore" without anyone having predicted the
type.

### R2. The timebase must be recoverable at any moment

A one-shot `HELLO` means any resync permanently loses the ability to date
anything. `tick_hz` must be obtainable on demand and must appear in the snapshot
so that a snapshot is self-describing.

### R3. Wrap must be unambiguous

`hw_tick32` at 18 MHz wraps every 238 seconds. Either carry a wrap counter or
have the bridge maintain a 64-bit extension by observing wraps. The bridge
already sees every frame, so it can extend cheaply, but it must record when it
started observing so a reader knows the extension is trustworthy.

### R4. Rare records must survive volume

A flood of `STATUS` must never evict a fault, a latch transition, or an
unsupported-frame record. This needs reserved capacity per class, not a single
FIFO with a drop policy. The classes are the roles in §2.

### R5. Silent failure is prohibited

Every path that discards data must increment a counter, and those counters must
themselves be in the never-dropped class. A probe that under-reports its own
health is worse than no probe, because it converts "no data" into "no problem".

Two distinct ways that was violated, and they need different remedies.

A counter that is never surfaced is the simple case: the outbox increments
`journal_failure_count` at four sites and nothing read it, so durable records
could fail to stage while every visible counter stayed at zero. Surfacing it is
the whole fix.

A counter that resets is the harder case. `JOURNAL_FAILURE_COUNT` and
`EXCEPTION_COUNT` both read 0 while a retained ERROR in the log described an
`OSError` on the journal. Nothing was wrong with the counters; they are
per-boot, the bridge had restarted, and the log record outlived them. So a
count of zero does not mean "never happened", and a reader has no way to tell
the two apart from the counter alone. Health reporting therefore has to carry
something that survives the reset — at minimum the last error and when it
happened, so a restart cannot erase the fact that something failed.

### R6. Triggered capture

Role C needs an armed window: capture raw frames matching a filter for N seconds
or M kilobytes, then stop and hold for readout. Without it, characterising
`0x0411` means either capturing everything (impossible at this rate) or guessing
in advance (R1).

### R7. Remote recovery

The probe must be restartable and its persistent state clearable without
physical access. Otherwise a corrupt journal ends the investigation until
someone walks to the printer.

### R8. Instrumentation must not endanger the BMCU

Unchanged and non-negotiable. The BMCU's printer bus and motor control take
precedence over all of the above. The DMA ingress shows this is achievable: it
lost nothing under a 627 ms stall because it never depended on being serviced
promptly.

## 4. Design

### 4.1 BMCU side

Flash is the binding constraint: the `BMCU_DM_TWO_MICROSWITCH` build sits at
91.9% of 61,440 bytes, leaving roughly 5,000 bytes. Everything here is chosen to
be small.

**Unsupported long-frame table.** A fixed array of eight `(type:u16, count:u16)`
entries, first-seen-wins, emitted as a new `FULL_STATUS` record. Answers R1 for
the class that just cost a day. Roughly 32 bytes of RAM and a short insertion
loop.

**`tick_hz` in the global snapshot record.** Satisfies R2 at the cost of four
bytes in a record that is already assembled. A snapshot becomes self-describing:
whoever reads it can convert its own timestamps without having witnessed boot.

**Widen `notable` to include `IGNORED` with `REASON_UNSUPPORTED`.** One term. It
makes the individual occurrences visible once the Pico can carry them, while the
table above remains the thing that works even when events are being dropped.

**Deliberately not done:** a general trace mode on the BMCU. The printer bus
runs at 1.25 Mbps and the management UART at 115200. The BMCU cannot forward raw
printer traffic in real time and must not try. Role C belongs on the Pico, or on
a separate listener on the printer bus.

### 4.2 Pico side

**Priority classes with reserved capacity (R4).** Three queues rather than one:

| Class | Contents | Policy |
| --- | --- | --- |
| Vital | faults, latch transitions, unsupported frames, link state, drop counters | Never evicted. Small, bounded, persisted first |
| Live | `STATUS` | Coalesced, newest wins, dropped freely |
| Bulk | triggered capture | Fixed arena, filled once, held for readout |

The present single queue with `RAM_QUEUE_FULL` is exactly the failure this
prevents: 439,220 status replacements crowded out every event.

**Static preallocation (R5, and heap).** `HEAP_MIN_FREE` of 32 bytes means the
bridge is surviving on garbage collection timing. The vital class in particular
must own preallocated buffers, so that reporting a fault never requires an
allocation at the moment the system is least able to satisfy one.

**Honest health reporting (R5).** Every discard path increments a counter;
journal write failures increment `JOURNAL_FAILURE_COUNT` — including the path
that raised `OSError: 84` today; and the diagnostics record carries the last
error string, not only counts.

**Snapshot on demand (R9 in effect).** `refresh_snapshot_if_idle` exists to
avoid disturbing a busy bridge, which is right for background refresh and wrong
for an operator who has just reproduced a fault and wants state now. Add an
explicit forced refresh, accepting the cost, and stamp every snapshot with its
own age so a stale one can never be mistaken for current.

**Remote maintenance (R7).** A minimal authenticated local endpoint that can
restart the application and clear the journal directory. This is a write path
and needs the same treatment as soft reset: idle-only, explicit confirmation,
and no capability beyond those two operations.

### 4.3 Host side

**Capture arming (R6).** Arm from the host with a filter and a bound, read out
when it fires. The filter must be expressible as "long-frame types not in the
handled set" so that the next unknown type is caught without being named in
advance.

**Wrap extension (R3).** The bridge extends `hw_tick32` to 64 bits by observing
wraps, and records the tick at which it began observing so a reader knows how
far back the extension is valid.

## 5. Staged plan

Ordered so that each stage is useful alone, and so the cheapest answers to
today's open question come first.

1. **BMCU: unsupported long-frame table, `tick_hz` in the global record, widen
   `notable`.** Small, and together they would have answered today's question
   from a single snapshot. Costs a few hundred bytes of flash.
2. **Pico: honest counters and forced snapshot refresh.** No new mechanism, and
   it removes the class of failure where the probe under-reports itself.
3. **Pico: priority classes.** The largest change, and the one that makes the
   probe trustworthy under load.
4. **Pico: remote restart and journal clear.** Unblocks recovery without
   physical access.
5. **Triggered bulk capture.** Only worth building once 1-4 hold.

## 6. Open questions

- Whether the printer bus deserves a dedicated listener rather than being
  instrumented from inside the BMCU. A second device on the bus would remove the
  flash ceiling and the real-time constraint entirely, at the cost of another
  thing to build and power.
- Whether the journal should be abandoned in favour of streaming to the server
  and keeping only a small local ring. Today's corruption cost the entire event
  history; a smaller local buffer would have lost less.
- How the vital class should behave when the server is unreachable for hours.
  Bounded loss is acceptable for `STATUS` and is not obviously acceptable for
  faults.
