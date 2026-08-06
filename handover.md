# Handover — per-slot autoload failures on both BMCUs

Bench session 2026-08-04 into 08-05. Paused with both links `stale` (BMCUs
powered down). Nothing was flashed during the session; no firmware change was
made. Branch `alpha`, tree head `34b9932`, 12 unpushed commits, CI green.

**Updated 2026-08-07.** Tree head `df4727a`, 22 unpushed commits on `alpha`,
245 Python tests and 50 web tests green. Ten commits since `34b9932`. Read the
"Session 2026-08-05 to 08-07" section below before anything else: it retires
several items in this document, corrects a hardware attribution, and the
per-slot table further down is now WRONG in ways that section names.

**One BMCU only.** The second board's motherboard was destroyed by 24 V on the
wrong pin. Half the evidence in this document can never be reproduced.

The point of this document is to keep the next session from re-deriving what was
settled and, more importantly, from re-adopting the hypotheses that were raised
and then killed.

Reviewed after drafting; the corrections that review produced are folded in below
and the places where this document was wrong are called out rather than quietly
patched.

---

## Read this first: what firmware was actually running? -- SOLVED 2026-08-06

**No longer a problem.** `36f4272` put a build identifier in `KIND_HELLO`:
`variant_flags` names the matrix variant exactly and `build_hash` identifies
the build. The surviving board answered in thirty seconds -- `ams_a`, retract
0.25 m, DM autoload on, RGB off. A board running firmware older than that
sends the 9-byte HELLO and reports nothing, which is itself the answer.
The section below is kept as the record of why it was added.

**Not recorded, and it cannot be recovered from the device.** Every observation
below came from a build flashed to both BMCUs early on 2026-08-04, before the
bench work started. The line numbers cited here are for tree head `34b9932`. There
is no way to confirm those are the same code:

- `KIND_HELLO` (`src/bmcu_link.cpp:358`) carries a protocol `VERSION` byte and
  capability bits, and **no build identity** — no commit hash, no matrix variant.
- `dist-merged/` was built 2026-08-03 14:35, which predates every commit from
  `83355ed` onward. `dist/standard(A1)` is older still, 2026-07-26.
- The 780-variant matrix means "the firmware" is not even a single artefact.

So the first job on resume is to establish what is on the boards — reflash from a
known commit, or accept that the cited line numbers are approximate. **Everything
downstream of this is conditional on it.** Adding a build identifier to `HELLO` is
on the change list below for exactly this reason.

---

## Session 2026-08-05 to 08-07 - what changed

### Hardware, corrected

The fault attribution in "Established" below was wrong and is corrected in
place: **ams-a slot 3 was an assembly fault** (screws tight enough to pinch the
gear and hold the motor), **ams-b slot 4 was the solder joint**. ams-a slot 3 is
therefore a confirmed instance of the mechanical bind, not a separate electrical
failure, and its polarity never learned because the motor could not turn inside
the 2 s window - route 1 of the four, the one whose diagnostic is discarded.

The surviving board runs `ams_a` firmware, retract 0.25 m, read from its own
HELLO. Its UART was fine all along; an earlier "the board is dead" conclusion
was wrong pins.

### The per-slot table at the bottom of this document is stale

It describes two boards, one of which no longer exists, and its `ks` readings
came from single frozen snapshots. Live sampling on 2026-08-07 (183 polls over
45 s) contradicts them:

| | ks observed live |
|---|---|
| slot 2 (ch1) | `both` 183/183 - rock steady |
| slot 4 (ch3) | `inner` 134 / `both` 49, ten transitions in 45 s |

Two traps this exposed, both of which cost time here:

- **A snapshot is one frozen moment** and is not evidence of a steady state.
  Both readings that sent this session down blind alleys came from single
  snapshots.
- **Channel-record bodies are little-endian** while the BSNP header around them
  is big-endian. Decoding the body big-endian produces plausible-looking
  garbage: it read `sensor_good` as false on all four channels and `loaded` as
  true on all four, neither of which is possible, and both were believed
  briefly.

### All four channels are healthy

`polarity_valid` 1, `sensor_good` 1, no motion faults, no `dm_fail`. The
"silently dead channel" failure this document is largely about is **not present
on the surviving board**.

### Autoload: what is actually known about slot 2

Not settled. Three explanations were raised and two were killed:

- ~~"`dm_loaded` is 1 at boot because ks reads `both`, so the autoload block is
  never entered"~~ - **dead**. That reasoning used the state *after* filament
  was inserted. At the time of the failure the slot was empty.
- ~~"insertion first reaches `inner`, which has no entry branch"~~ - **dead**.
  That read the voltage ordering (`none` < `inner` < `outer` < `both`) as a
  physical progression. It is not: the bands are a resistor sum, and a gear sits
  between the two microswitches, so filament cannot reach the inner switch
  without the motor driving it. Physically insertion goes `none` -> `outer`.
- **live**: the outer switch is pressed but its voltage falls short of the
  1400 mV `outer` band, so it decodes as `inner` - for which the IDLE entry
  branch has **no case at all**. Not Stage1, not Stage2, no fault latch, no
  event. Silence.

That last one is what `FULL_RECORD_DM_KEY` was added to test, and it cannot be
answered from the decoded `ks` alone.

### Structural findings worth keeping

- **`dm_key_to_state` has no hysteresis.** Three bare comparisons against
  `none_thr` / 1.4 V / 1.7 V. A voltage resting on a boundary flips forever.
  This is what slot 4 is doing. The 1.4/1.7 bounds are hardcoded and shared by
  every channel; only `none_thr` is per-channel.
- **`ks` on the wire is not necessarily the switch.** Buffer Gesture Load
  overwrites a `none` reading with `outer` (the `gst_active` branch), and the
  non-inserted paths force it to `none`. Nothing on the wire distinguishes a
  synthetic value from a sensed one. Judged acceptable this session, but every
  `ks` in telemetry and in teardown events carries that caveat.
- **The autoload IDLE entry branch handles only `ks == 2` and `ks == 1`.**
  `inner` and `none` fall through to nothing at all.
- **`RECORD_SENSOR` / `KIND_SENSOR_RECORD` are declared and never emitted.**
  A dead capability, in case someone assumes it works.

### Why the teardown events were empty

Two independent reasons, both now fixed in the tree:

1. The BMCU event ring is eight slots, and buffer pressure was emitting a
   `state_change` at the printer's poll rate - measured 12.5 events/s, which
   evicts a teardown in well under a second.
2. The Pico's durable queue was full (`reject_durable_full_count` 1201) and its
   journal was growing 1.6 MB/hour.

The pressure flood was a 1% dither on `MC_PULL_pct` amplified by 1311 counts per
point. `c4ff9bc` deadbands it against the last *reported* value, which is
hysteresis; masking low bits was measured and rejected, because a rule without
memory still chatters at 15 of 50 operating points. **Nothing has confirmed the
teardown events survive now** - that needs the new firmware on the board.

---

## Resume from, as of 2026-08-07

The board is flashed with `dist-2026-08-05/.../ams_a/ams_a_0.25f.bin`, which
**predates** the pressure deadband, the key-voltage record, and the snapshot
capacity fix.

1. **Flash `dist-bench/ams_a_0.25f_dmkey.bin`** (built from `df4727a`, same
   variant). Everything below needs it.
2. **Read `FULL_RECORD_DM_KEY`** while pressing slot 2's outer switch by hand
   with the slot empty. Does the voltage clear 1400 mV? That answers the only
   surviving explanation for slot 2. Compare each channel's `none_thr_mv`
   against its resting voltage while there - that is Resume step 5 of the
   original list, and it decides whether a reprint is even the right fix.
3. **Retry autoload on slot 2** and read the teardown events. They should now
   survive. `held_ms` on a `s1_debounce` / `ks_deviated` teardown is the
   excursion width that item 5 below has been blocked on since 08-04.
4. **Close out issue #16.** `FULL_RECORD_DM_KEY` is on loan, not part of the
   interface. https://github.com/dj-oyu/BMCU-C-PJARCZAK-kaizou/issues/16 holds
   the end condition, the deletion inventory, and the one thing that must NOT be
   reverted with it (`kMaxFullStatusRecords` stays at 18 - a complete snapshot
   built 16 records against a cap of 15 even before that record existed, so
   `counters` was dropped from every snapshot, and the drop counter that would
   have reported it lives inside `counters`).
5. **Confirm the pressure fix on hardware**: `journal_used_bytes` and
   `oldest_unacknowledged_sequence` growth should collapse from 448 B/s and
   12.5/s to near zero with the buffer idle.
6. Then decide `dm_key_to_state` hysteresis for the slot 4 chatter, sized from
   the widths step 3 produces.

### Deliberately not on that list

- **Bambuddy print queue** - not a BMCU problem. Two jobs were held by an AMS
  colour check (`#D6001C` required vs `#EB3A3A` loaded; the tolerance is a
  per-channel RGB box of +/-40 and the green channel missed by 18). Nothing was
  cancelled and the queue was never corrupted. Filed as
  https://github.com/dj-oyu/bambuddy/issues/5 with measurements, a CIELAB
  proposal, and why cosine similarity is disqualified for it. The AMS
  registration was edited to `D6001CFF`; **whether the holds actually cleared
  was never confirmed.**
- **Pico journal at ~12.7 MB** and `heap_min_free` 48 bytes. The heap recovered
  to 81 KB on its own, so that was a GC trough rather than a steady state, and
  the growth driver is fixed at source. The accumulated journal was not cleaned
  up.
- **Pico link config** is now single-link on UART0 (GPIO 0/1). `config.py` lives
  on the device only - it holds the Bambuddy device key and is deliberately not
  in this repo.

---

## What was being chased

Filament pushed in from the rear did not start autoload. Once telemetry was
available, four slots per BMCU turned out to behave four different ways, and the
failures were **silent**: no LED, no fault latch, no telemetry field. Most of the
session went into building the ability to see the problem, not into the problem.

---

## Established

### Switch band decode is correct

Bench test with the loader open, actuating each microswitch by hand
(`diagnostics/.../runs/bench.txt`, 22:11:46-22:11:58):

| pressed | `ks` reported |
|---|---|
| outer only | `outer` (2) |
| inner only | `inner` (3) |
| both | `both` (1) |

The `1.4f` / `1.7f` constants in `dm_key_to_state` (`src/Motion_control.cpp:262`)
fit this hardware. One-to-one, no ambiguity.

### A channel with no learned polarity is completely dead, silently

`MOTOR_get_dir()` (`:3438`) runs once at boot. `motor_polarity_valid` (`:3569`)
is 0 whenever the stored polarity is 0, and `_MOTOR_CONTROL::run()` (`:1224`)
returns at `:1246` before doing anything else. That kills **every** motor path for
the channel: DM autoload, the idle pull PID, the buffer gesture, and
`manual_empty_pull` / auto-unload, which check the flag explicitly at `:3042` and
`:3028`.

*(An earlier draft of this document attributed the early return to `set_motion`.
It is `run()`. The conclusion is unchanged — `run()` is the single PWM producer —
but the name was wrong.)*

Polarity ends up 0 through **four** different routes, only one of which even
produces a diagnostic:

1. tested, no movement within 2 s → `timed_out` computed, then **discarded** at
   `:3548` with `(void)timed_out;`
2. AS5600 not good at boot → channel never tested at all (`:3469`)
3. AS5600 drops out mid-test → aborted without saving (`:3497`)
4. `Motion_control_read()` fails → all polarities zeroed (`:3448`)

Routes 2-4 leave no trace whatsoever. This is why the change proposed below
reports the **level** `motor_polarity_valid == 0` rather than the timeout: the
timeout covers one cause out of four.

The learner needs >= 163 AS5600 counts (~14 deg) within 2 s at PWM 1000 (`:3506`).

### ams-a slot 3 was an assembly fault, not a solder joint — CORRECTED 2026-08-05

**The original draft of this section attributed the solder joint to ams-a slot 3.
That was the wrong slot on the wrong board.** Corrected from the bench:

- **ams-a slot 3** — assembly. The screws were tight enough to pinch the gear and
  hold the motor. Not an electrical fault at all.
- **ams-b slot 4** — the solder joint, apparently.

This matters beyond bookkeeping. ams-a slot 3 is now a **direct witness of the
mechanical bind documented below**, not a separate one-off electrical failure:
screws tight → gear binds → motor stalls, exactly as written. And the reason its
polarity never learned is route 1 of the four — the motor could not turn within
the 2 s window, so `timed_out` was computed and then discarded at `:3548`. The
silent-failure path is no longer hypothetical; it has a confirmed instance.

**Evidence that has NOT been re-attributed:** the bench-supply observation (no
rotation at 3-6 V, above that turning 1-2 s then slowing and stalling — joint
resistance rather than a dead winding) was recorded against "the slot with the
solder fault". If that slot is ams-b 4, the observation belongs there. It is left
unmoved rather than silently relocated, because nobody has re-confirmed which
board it was taken on. Re-take it if it matters.

(The older theory tying any of this to the event where the board was burned by
mis-wired printer power stays dead either way.)

### Stage1 entry is far stricter than "the switch closed"

`:1313` and `:1333`:

```c
if (ks == 2u) { gate = 1; state = S1_DEBOUNCE; t0 = now; }
case S1_DEBOUNCE:
    if (ks != 2u) { state = IDLE; t0 = 0; }        // any deviation, full reset
    else if (now - t0 >= 100ms) state = S1_PUSH;   // DM_AUTO_S1_DEBOUNCE_MS, :273
```

`ks` must sit at exactly 2, alone, for 100 ms with no excursion — to `none`, to
`inner`, or to `both`. And `gate` is set on the **first** flicker to `outer`,
before the debounce is satisfied; if the debounce then aborts, `gate` stays 1 and
the entry branch is closed until `ks == 0` with the channel idle clears it
(`:2836`). A failed attempt consumes the retry until full withdrawal.

Observed: gentle insertion never triggers; a firm push that pins the lever holds
`outer` for seconds and does.

### A momentary `ks == 0` tears the machine down — including faults already latched

`:2833-2846` clears, in one block: `dm_autoload_gate`, `dm_loaded`,
**`dm_fail_latch`**, `dm_auto_state`, `dm_auto_try`, `dm_auto_t0_ms`, the
remaining-counts pair, and the drop timestamp. `:1353` separately sends `S1_PUSH`
back to `IDLE`, **restarting the 5 s timeout** that would otherwise set
`dm_fail_latch` at `:1367`.

The `dm_fail_latch` line at `:2839` is the important one and the first draft of
this document missed it. Chatter does not merely prevent a fault from latching —
**it erases faults that already latched.** The single `dm_fail_latch` observed in
the whole session (ams-b slot 4, 15:21:17, `runs/ks3.txt`) survived only because
no glitch followed it before the channel was withdrawn.

So: across eight channels and tens of thousands of samples, the fault-reporting
mechanism fired once. **The failure mode suppresses its own report.**

`signal_hold::held_for` already exists and is used for the `dm_loaded` drop at
`:2852` (`DM_LOADED_DROP_MS = 100`, `:311`). It is applied to none of these paths,
and `src/signal_hold_policy.h`'s own comment notes the omission.

**Do not call this "contact bounce".** `:320-329` documents the sampling chain:
ADCCLK 18 MHz, 84 cycles a conversion, 8-channel scan = 37.3 us, DMA half-buffer
of 32 scans every ~1.2 ms, and `ADC_DMA_get_value` returns a boxcar over the last
four — **~4.8 ms**. Sub-millisecond switch bounce never reaches
`dm_key_to_state`. The 13 excursions measured were mechanical lever float, tens to
hundreds of milliseconds wide. **Their widths were never recorded, only their
count**, which is why the hold constant in the change list cannot be sized from
this session.

### Buffer Gesture Load is a real feature, not a workaround

`:612-687`. Push the buffer below 10% for >= 100 ms, return to 45-55% within 2 s,
and the firmware fakes `ks = outer` for 5.5 s. It is the only way in when the
outer switch cannot be reached by hand.

---

## Retracted — do not re-adopt these

Five hypotheses were argued in detail and then disproved. Each was plausible
enough to cost time twice.

1. **"This board's key voltage never reaches the upper bands, so `outer` and
   `both` are unreachable."** Disproved twice: ams-a slot 3 held `outer` for 14
   minutes, and the bench test produced all three states cleanly.

2. **"`manual_empty_pull` (`:3020`) is what swallows filament on a channel whose
   `ks` never moves."** It requires `MC_PULL_pct_q > 8000`; the channel's pull sat
   at 43% throughout, never near 80%. **This retraction leaves a vacuum — see
   Open.**

3. **"The autoload gate latched and never re-armed — that is why ams-a slot 3 sat
   at `outer` doing nothing."** It was `motor_polarity_valid == 0`. The
   discriminator: pushing the buffer moved the motor on slots 1, 2 and 4 of the
   same board but not on 3. The gate only blocks Stage1 entry at `:1315`; the idle
   PID goes through `run()`, and only polarity kills that.

4. **"The chattering survived reassembly, so it is not an assembly artefact."**
   Wrong: the screws are deliberately loosened and that did not change across the
   reassembly. The confound was never removed.

5. **"Microswitches bounce as a matter of physics, so a hold is worth adding
   regardless."** Undercut by the boxcar above. The argument for a hold has to
   rest on the teardown *structure*, not on bounce.

A sixth, from earlier in the week and killed by the host shim, is recorded
elsewhere: the `bd2e2e1` explanation for the auto-unload failure is dead code —
`bambu_bus_ams.cpp:443` makes `:461` unreachable while the merger is owned.

### And one hypothesis in this document that should itself be doubted

**"Loose screws → lever floats → `ks` chatters."** This is asserted with a
confidence denied to every measurement taken on the same hardware, and it has an
unexcluded alternative.

`MC_DM_KEY_NONE_THRESH` is per-channel and derived from the *calibration-time idle
key voltage* (`MC_PULL_calibration.cpp:385`, restored from NVM at
`Motion_control.cpp:762`, floored at 0.60 V). The observed chatter was
`inner -> none -> inner` — crossings of exactly that threshold. **A channel
calibrated while its lever was partially floating gets a `none_thr` sitting close
to its resting voltage, and will chatter forever after, screws tight or not.**

Cheap check before committing to a reprint: dump `dm_key_none_cv` per channel per
board and compare against the live resting key voltage.

---

## The mechanical bind

Printed-part tolerance compresses the gear when the assembly screws are tight,
enough to stall the motor. The screws are therefore run deliberately loose, which
gives the slot driving the filament-sensor lever extra travel.

```
screws tight  -> gear binds -> motor stalls
screws loose  -> lever floats -> ks crosses none_thr -> autoload will not trigger
```

Likely root fix is dimensional — reprint with clearance so the screws can be
tight. Check first whether one screw sets both the gear preload and the switch
bracket position; if they can be separated (shim the switch side), the bind
resolves without waiting on a reprint.

**Every chatter measurement in this session was taken on a deliberately loosened
assembly** and cannot be used to argue that healthy hardware misbehaves.

---

## Open

### Does a fully loaded channel rest at `both` or at `inner`?

The first draft called this unanswerable without a correctly assembled loader.
That was too pessimistic — the question splits in two, and half of it is already
settled by the code.

**The firmware's premise is `both`, and the code is only coherent that way.**
Stage2 success sets `dm_loaded = 1` at `:1452`. The drop logic at `:2852` clears
it after 100 ms of `ks != 1`. `S2_PUSH` aborts the instant `ks` leaves 1
(`:1401`). If the intended resting state were `inner`, every successful autoload
would self-cancel ~100 ms after the motor stops, `dm_loaded` would be unusable,
and the boot derivation at `:3604` would be nonsense. So `:3604` and `:2852` are
not *wrong*; they encode a premise.

**The open half is whether this hardware meets that premise**, and there is a
ten-minute experiment that tolerates the loose assembly: with filament loaded,
hold or tape the outer lever closed by hand — hand actuation already decoded
one-to-one on the bench.

- stable `both` → `inner`-at-rest is the float artefact; the reprint or shim fixes
  it and the premise holds
- still `inner` → the geometry never holds the outer switch, and the premise fails
  on this hardware regardless of screws

Observed so far, all contaminated: ams-a slots 1/2 rested at `inner`; ams-b
reached `both`; the bench oscillated `inner <-> both` with `both` holding 29 s and
18 s when the lever seated well.

### Two slots have symptoms and no surviving explanation

- **ams-a slot 4** — swallows a few cm of filament on insertion, `ks` never
  leaves `none`, pull never approaches the 80% `manual_empty_pull` needs. Whatever
  drove the motor is unidentified. Retraction 2 removed the only candidate.
- **ams-b slot 3** — autoloads but shallow, churns `outer`/`inner`/`none`.

Neither has a step in Resume from. They should.

### Is AHUB mode (`src/ahub_bus.cpp`) alive?

Unchanged from before: one instrumentation call in the whole file. If anyone uses
it, every gap closed this week reopens there.

---

## Worth changing when there is a flash window

Flash headroom is 3824 bytes (largest build 57616 of 61440, from the full
780-variant matrix, not a hand-picked config).

**Observability first — these only add signal and are safe to do alone:**

1. **DONE (`274f49f`). Report `motor_polarity_valid == 0` as a level.** Not "the learn timed out" —
   that is one of four routes to a dead channel (see above). `BmcuChannelFlags`
   (`src/bmcu_link.h`) has one `reserved` bit left; `tail` already spent the other.
   Spending the last bit here is defensible since polarity gates every motor path,
   but it is a one-way door — the snapshot channel record, which already carries
   AS5600 validity, is the alternative. **Highest value on this list and nearly
   free**: it turns a day of bench work into thirty seconds.
2. **DONE (`cf9c086`). Export state-machine teardown events** (state, cause, `ks`) into the event
   ring. This is the instrument that measures excursion widths on healthy
   hardware, which is what turns items 5-6 below from guesses into engineering.
3. **DONE (`36f4272`). Put a build identifier in `KIND_HELLO`** (`bmcu_link.cpp:358`). Right now
   there is no way to tell what is running on a board — see the top of this
   document.

**Behavioural — these change what the firmware does:**

4. **DONE (`c15193c`). Stop `ks == 0` from erasing `dm_fail_latch`** at `:2839`. Requiring a held
   `ks == 0` before clearing protects the one fault signal that already exists.
   Arguably worth more than preventing future misses.
5. **STILL OPEN, and now measurable.** Item 2 landed, so a S1_DEBOUNCE
   teardown event reports the excursion width directly. Take that data before
   picking a constant. **Hold `ks == 0` before acting on it** at `:2833`, `:1353`, `:2836`, using the
   `signal_hold::held_for` already in the tree. **Prerequisite: measure excursion
   widths** (item 2) — this session recorded counts, not durations, and 100 ms may
   well be shorter than lever float. Note this shifts the timing invariant
   documented at `bmcu_link.h:25`; benign, but the decoder docs track it.
6. **DONE (`d3b6f99`). Move the gate set** from the first `outer` flicker (`:1317`) to where Stage1
   actually starts, so a failed debounce does not consume the retry. Pure
   logic-shape fix, independent of chatter magnitude, as safe as item 1 — do it in
   the same window. One decision to make explicitly: the direct S2 entry at
   `:1322-1328` sets no gate at all today; either fix that asymmetry or preserve
   it deliberately.

**Items 5 and 6 overlap with a "tolerant debounce" (N-of-M instead of `:1337`'s
zero-tolerance reset). Pick one shape, not both** — doing both is redundant
complexity.

---

## Resources carried over

All untracked. Decide what earns a commit — **`tools/bmcu_live_watch.py` in
particular, since one `git clean` would take it.** It is mirrored into the
diagnostics tree below as insurance.

| path | what it is |
|---|---|
| `handover.md` | this document, repo root |
| `tools/bmcu_live_watch.py` | live per-channel watcher |
| `..\diagnostics\2026-08-04-bench\README.md` | index of the captures, with what each proves |
| `..\diagnostics\2026-08-04-bench\runs\*.txt` | distilled state runs — **start here**, not the raw logs |
| `..\diagnostics\2026-08-04-bench\raw\*.log` | full telemetry, ~6 MB, mostly pull dither |
| `..\diagnostics\2026-08-04-bench\raw\*.bin` | single-shot endpoint pulls, usable as decoder fixtures |
| `..\diagnostics\2026-08-04-bench\distil.py` | collapses a raw log to state runs |
| `..\diagnostics\2026-08-04-bench\bmcu_live_watch.py` | mirror of the watcher, outside the repo |

The diagnostics tree sits beside the repo so a few megabytes of telemetry does not
become repo history. **None of it is reproducible** — the BMCUs have since been
reassembled and one motor resoldered.

Two captures worth knowing by name: `runs/bench.txt` holds the switch band test at
22:11:46-22:11:58 that settles the decode question; `runs/ks3.txt` holds the
session's only `dm_fail_latch`, at 15:21:17.

Background already in the repo: `docs/BMCU_LOADED_LATCH_DESIGN.md`,
`docs/BMCU_REFACTOR_PLAN.md`, `docs/BMCU_TARGET_HARDWARE.md`, and `ci/host/` — the
host shim that made `bambu_bus_ams.cpp` testable and killed the `bd2e2e1`
explanation.

## Tooling

`python tools/bmcu_live_watch.py http://bmcu-monitor-a.local [link...]` — prints
per-channel `ks`, masks, pull, motion and latches, only on change.

Two traps, both of which cost time this session:

- **`/api/snapshot.bin` arrives once per link session and then sits frozen** — its
  per-record `hw_tick32` does not advance. Rich (encoder angle, motor PWM, AS5600
  validity, event ring) but not live; `?refresh=1` lands on a *following* request.
  **`/api/current.bin` is the live one** and is what to watch a hand movement with.
- **Both endpoints keep serving the last bytes received when a BMCU goes quiet.**
  An unchanging reading is not evidence of unchanging hardware. Decode the
  synthetic link record (type `0xF0`, byte 0: 2 = online, 3 = stale) **first**. A
  45-minute "nothing moved" run turned out to be two stale links.

Also: `pull_pct` dithers by 1% forever, so a change-triggered dump plus `tail`
shows only noise and hides the events — that produced a false "the ams-b test does
not appear in telemetry at all" mid-session. Collapse to state runs.

---

## Where each slot stood at pause

| | slot 1 | slot 2 | slot 3 | slot 4 |
|---|---|---|---|---|
| **ams-a** | loads; rests at `inner` | loads; rests at `inner` | **assembly** — screws had pinched the gear and held the motor; corrected. **Polarity should relearn on next boot** | swallows a few cm; `ks` never moved; **no explanation** |
| **ams-b** | normal | normal | shallow, churns; **no explanation** | **solder joint**, apparently. Reached `both`, latched DMFAIL once |

Fault attribution corrected 2026-08-05 — the earlier draft had the solder joint
on ams-a 3. See the Established section. ams-a 3 is a witness of the mechanical
bind, not a separate electrical failure.

ams-a slot 3 needs no NVM wipe: a plain power cycle re-attempts the learn for
polarity-0 channels only and leaves the other slots' calibration intact. The wipe
(hold a buffer lever at its minimum 5 s, `:3218`) would clear pull offsets and key
thresholds for the whole board and is not needed here.

---

## Resume from

1. **Establish what firmware is on the boards** (top of this document). Everything
   below is conditional on it.
2. Power up; confirm the link reads `online` before trusting any number.
3. **ams-a slot 3**: confirm the screws are not re-pinching the gear, then power
   cycle and push the buffer — does the motor run? Yes → polarity relearned, slot
   closed out. No → the encoder or magnet is next, and an NVM wipe would not help.
   Since the fault was assembly rather than electrical, a slot that fails again
   here is most likely a screw that was retightened, not a new fault.
4. **Settle the resting state** with the taped-outer-lever test. Ten minutes, no
   reprint needed. Everything about `dm_loaded` hangs off the answer.
5. **Dump `dm_key_none_cv` per channel per board** and compare against live resting
   key voltage, before committing to a reprint. It may be a calibration artefact
   rather than geometry.
6. Pick up **ams-a slot 4** and **ams-b slot 3**, which currently have symptoms and
   no hypothesis.
7. Only then decide the behavioural changes (items 4-6 above). Items 1-3 are
   observability and can go in any time.

---

## Still not carried by this document

Named so the next session knows what it is missing rather than assuming coverage:

- excursion **durations** for the chatter (counts only were recorded), and which
  channel/board and poll cadence each measurement came from
- stored `dm_key_none_cv` per channel per board
- ams-b's monitor hostname, and whether both Picos run the same bridge build
- what the 12 unpushed commits on `alpha` contain
- which suite "CI green" refers to
