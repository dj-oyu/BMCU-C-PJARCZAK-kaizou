# Merger ownership: the loaded latch, its missing state, and the write funnel

Status: design, authoritative for what it marks as specification (section 0).
The implementation has landed through `6dd50fb` — the host shim, the TAIL
state, the session/preempt intent split, and the timer consolidation are in
the tree. Descriptions below are current as of `6dd50fb` where cited; the
transition table's line references were re-verified at that commit.

## 0. Convention: specification versus description

Every claim in this document is one of two kinds, and the distinction is
load-bearing:

- **Specification** — what must be true. Authoritative. If the code disagrees,
  **the code is wrong** and gets brought to the document.
- **Description** — what is true today, cited to file and line, offered as
  orientation. If the code disagrees, **the description is stale** and gets
  corrected. Descriptions rot; that is their nature, and a description is
  never a reason to preserve behaviour.

The rule exists because this document violated it and was caught by the host
harness: row 9 asserted that the `0xFF` idle clears were "today's recovery
path" for a restarted printer. That was a description wearing specification's
clothes — and it was false on the day it was written (see row 9's note for
the mechanism; measured across 40,000 frames, the clear it described never
once affected the merger). A specification can be wrong and argued with; a
description can only be checked, and this one never was.

Markings: transition-table Notes carry explicit **Spec:** and **Desc:**
segments. Prose sections that are wholly one kind say so at the head.
`docs/BMCU_TARGET_HARDWARE.md` is description by design and says so; that is
the correct handling for that kind of document and it is not repeated here.

A drifted line number in a description is not a defect to sweep. Citations
are dated; the tree moves; a stale citation gets corrected when the passage
it lives in is next touched, not chased wholesale on every commit. What the
convention forbids is only the thing row 9 did: a description asserting a
*property* the code does not have. Numbers rot harmlessly; properties do not.

## 1. What the latch is, and what is inherent

Four channels feed one PTFE merger; exactly one strand may occupy the shared
output path at a time. `g_loaded_ch` (`src/main.cpp:71`) is the record of which
channel that is. It is flash-backed (`Flash_AMS_state_write`,
`src/Flash_saves.cpp:397-422`) because physical occupancy survives reboot, and
boot restore (`src/main.cpp:234-254`) rebuilds the whole protocol-session
presentation from it: `now_filament_num`, `filament_use_flag = 0x04`,
`pressure = 0x2B00`, `motion = on_use` on the restored channel.

Three things are inherent and this design keeps all of them:

- **The latch exists.** A physical mutex needs a record. The printer never
  states what it believes is loaded; the BMCU can only infer it.
- **The flash backing exists.** Occupancy survives power loss; the record must.
- **The accept gates exist.** `set_motion` refuses commands that would drive a
  second strand into an occupied merger (`allow_any` / `allow_stop`,
  `src/bambu_bus_ams.cpp:240-249`). That is the safety property everything else
  serves.

What is *not* inherent is the shape: the latch is binary where the physical
process has three states, it has four logical writer classes with no owner, and
divergence from the printer is silent. Those are the three defects this design
removes.

## 2. Two facts that the current code conflates

The single most important structural statement in this document:

- **Presence** — "filament rests on this channel's switch" — is a sensor fact.
  It is read continuously (`MC_ONLINE_key_stu`, recomputed every pass), it is
  advertised to the printer in every motion reply (`get_filament_left_char`,
  `src/bambu_bus_ams.cpp:183-197`; `filament_online_flag`, `:798-800`), and it
  is *supposed* to drop the instant filament runs out — that drop is very likely
  what triggers the printer's spool switchover in the first place.
- **Ownership** — "this channel's strand occupies the merger" — is an inferred
  fact. It gates which motion commands are plausible, and it must *not* follow
  the switch, because a runout tail that has passed the switch still occupies
  the merger and still must be retractable.

Today one variable serves both masters and the sensor writes it. Runout
therefore releases ownership at the exact moment ownership is about to be
exercised. The TAIL state is nothing more than making "not present, still
owned" representable.

## 3. States

```text
UNLOADED      nobody owns the merger. Loads of any channel may be accepted.
LOADED(ch)    ch owns the merger and its filament is (believed) present.
TAIL(ch)      ch owns the merger; its filament is no longer at the switch.
              Retract commands for ch are still accepted. Loads of other
              channels are refused until a send_out preempts (section 5,
              row 7); there is no time bound.
```

Presence advertisement is unchanged in all three states: it follows the sensor
(and the motion-hold for `pull_back`/`redetect`,
`src/Motion_control.cpp:3199-3211`). In TAIL the slot is advertised empty —
deliberately, because an empty slot is what tells the printer to switch spools —
while the ownership gates keep the retract path open. That separation is the fix.

## 4. The write funnel

All ownership transitions go through one module — the same shape as
`src/ams_online_detect_policy.h` and `src/ams_loaded_latch_policy.h`:
header-only, allocation-free, pure decision logic, host-testable, with the I/O
(flash write, event emit) done by the thin caller.

RAM representation:

```c
struct MergerOwnership {
    uint8_t owner;   // 0-3, or 0xFF when UNLOADED
    uint8_t state;   // UNLOADED / LOADED / TAIL
    uint8_t epoch;   // increments on every transition; wraps; anchors events
};
```

On the two-candidate representation question (owner/state/epoch struct versus a
two-nibble LOADED/TAIL mask byte):

- The mask's argued advantage — corruption of flash-backed state is *detectable*
  (popcount > 1) — is already provided, and more strongly, by the existing STA
  flash record: every slot carries a tag byte and a full 32-bit XOR check
  (`w0 ^ w1 == MAGIC_STA`, `src/Flash_saves.cpp:363-364`). A corrupted slot is
  rejected before its payload is ever interpreted. The flash layer does not need
  the mask's help.
- The claim "no consumer needs the owner index" is **false** as of `9747e99`:
  the debounce feed indexes the key array by owner
  (`MC_ONLINE_key_stu[...]`, `src/Motion_control.cpp:3140-3148`). With a
  mask that becomes a ctz, and CH32V203 is RV32IMAC with no B extension. A
  four-iteration loop would do, but it is a cost, not the absence of one.
- The debugger-legibility argument stands on its own: `owner=2, state=TAIL` is
  readable; `0x24` is not, in an area that has already cost a day of
  misdiagnosis.

Decision: **struct in RAM, existing STA format in flash** (section 6). Illegal
states are unconstructible in RAM because the funnel is the only writer; they
are detectable in flash because the STA record already checks itself. The mask
buys nothing that is not already bought.

The funnel API shape (shape, not code — the implementation owns the spelling):

```c
// Only these mutate MergerOwnership. Every call that changes state emits one
// event carrying {old state, new state, owner, cause, epoch}.
bool merger_try_acquire(uint8_t ch, uint8_t cause);        // -> LOADED(ch)
bool merger_enter_tail(uint8_t ch, uint8_t cause);         // LOADED -> TAIL
bool merger_release(uint8_t ch, uint8_t cause);            // -> UNLOADED, owner-checked
void merger_force_release(uint8_t cause);                  // preempt path (row 7), logged loudly
```

`merger_release` checking the owner is the "ownership" in this mutex. Today's
equivalent (`ams_state_set_unloaded`, `src/main.cpp:141-147`) accepts a
mismatched clear only for the wildcard `0xFF`; the funnel keeps that strictness
and adds the cause code and the event.

One deliberate non-goal, confirmed from the sketches: **no compile-time
move-only token on the motor API**, because the invariant is about the merger,
not the motors — `filament_redetect` legitimately drives a channel at high PWM
while owning nothing (commit `4e9adf6`). And **no atomics/CAS**: `bambubus_run`
and `Motion_control_run` are both called from the single superloop
(`src/main.cpp:271-277`); there is no concurrency to defend against, and the
existing `irq_save_wch` critical sections cover the IRQ boundary.

## 5. Transition table

Inputs come from three sources: printer command edges already decoded by
`set_motion` (`src/bambu_bus_ams.cpp:220-463`), the debounced key signal
(`src/ams_loaded_latch_policy.h`), and the motion sequencer's terminal
transitions. Every row cites where the input is produced today.

| # | From | Input (source) | To | Cause code | Notes |
|---|------|-------|----|-------|-------|
| 1 | UNLOADED | `before_on_use(ch)` / `on_use(ch)` accepted (`:308`, `:378`) | LOADED(ch) | `cmd_load` | **Spec.** Desc: implemented — both sites funnel to `ams_merger::acquire`. |
| 2 | LOADED(ch) | `on_use(ch)` (`:378`) | LOADED(ch) | — | **Spec:** a redundant re-latch is a no-op — no event, no flash write. Desc: the funnel returns unchanged for the current owner and the flash write dedups (`Flash_saves.cpp:399` shape). |
| 3 | LOADED(ch) | `before_pull_back(ch)` or `0xFF` pull command | **TAIL(ch)** | `cmd_retract` | **Spec, deferred — see the note below this table.** Desc: today both paths release at command time (`:398`, `:428`), i.e. "retract commanded" is treated as "path clear", wrong in the opposite direction from the sensor bug: a failed retract leaves the path occupied and unowned. |
| 4 | LOADED(ch) | key-zero held ≥ `LOADED_LATCH_DROP_MS` | **TAIL(ch)** | `sensor_runout` | **Spec — the central change.** Desc: implemented (`Motion_control.cpp:3140-3148`, `ams_merger::to_tail`; the debounce disarms while TAIL is held). |
| 5 | TAIL(ch) | retract sequence terminal: `redetect` exits — key reported or redetect timeout (bounded by `4e9adf6`) | UNLOADED | `retract_done` / `retract_gone` | **Spec — contingent, unverified.** Nobody chose to defer this row; its unreachability is a *consequence* of the row-3 deferral (which was the decision): the command-time release (`:398`, `:428`) precedes any motion terminal, so this transition would exit a state the code has already left. It becomes implementable, and verifiable, only when row 3 lands ("together with row 3 or not at all", `ams_merger_policy.h:55-59`). |
| 6 | TAIL(ch) | ~~debounced key-close + motion idle~~ commanded acquire (`before_on_use`/`on_use`, row 1 machinery) | LOADED(ch) | `cmd_load` | **Spec, revised — the original sensor form is retracted.** The implementation showed the sensor promotion would be *actively wrong* after a runout: new filament at the key says nothing about the old tail still in the bowden (`ams_merger_policy.h:61-69`). Promotion happens on the printer's next commanded acquire; the wire bits may lag the physical change and nothing gates on the lag (`allow_any`/`allow_stop` answer the same in TAIL and LOADED for the owner). |
| 7 | LOADED/TAIL | `send_out` from a claiming channel (`:275-280`) | UNLOADED, then row 1 as the load proceeds | `preempted` | **Spec:** the printer is master; refusing its load buys nothing. Release and **report** — a preempted TAIL is the witness that a strand was still in the tube when the next was pushed in. Desc: implemented (`ams_merger_policy.h:225-231`, `release_preempted_tail`; call site distinguishes claiming from session-ending, `:275-277` comment). |
| 8 | LOADED(ch) | `send_out(ch)` — same channel | LOADED(ch) | — | **Spec as written is NOT implemented, and is demoted to an open question (OQ-5).** Desc: the call site preempts whenever an owner exists and `prev != send_out` (`:278-280`) — including the owner's own re-feed — and `preempt()` takes no channel (`ams_merger_policy.h:225`), so it cannot distinguish. Today: transient UNLOADED during a same-channel re-feed, then re-acquire — which is also pre-TAIL behaviour, preserved. Nobody has verified whether the transient window matters; see OQ-5 before either implementing the guard or retracting this row. |
| 9 | LOADED(ch) | `0xFF` idle reset (`:437-461`) or `0xFF/0x01` (`:431-436`) | **LOADED(ch)** — no change | — | **Spec, rewritten: session-idle inputs never release a held merger, in any state.** Recovery from a printer restart is row 7 — a resuming printer opens with `send_out`. Desc + history, the motivating case for section 0: the original row claimed these clears were "today's recovery path". False when written — `:435`'s release sits behind `filament_use_flag != 0x04`, and `use_flag` is 0x04 for exactly as long as the merger is held (set at `:297`/`:368`, lines above both acquires at `:308`/`:378`), so the guard is shut on every frame where releasing would mean anything. Measured across 40,000 frames: zero effect on the merger, ever. |
| 10 | TAIL(ch) | `0xFF` idle reset / `0xFF/0x01` | **TAIL(ch)** | — | **Spec — a requirement, not an observation:** a session going idle leaves TAIL held; the escapes are rows 5-7, not time. Desc: **not yet structural.** The landed `release_refused_tail` guard (`ams_merger_policy.h:157-158`) protects only the *wildcard* arm (`ch >= kChannels`) — and that arm is exactly the door the harness proved unreachable while the merger is owned. The reachable door, `:435` passing a real channel, takes the owner-checked arm and resets unconditionally, TAIL included. What holds this row today is still the row-9 `use_flag` coincidence, demonstrated fragile by mutation — which is why the row is written as a requirement. In flight: a third funnel intent for "the session went quiet", distinct from the commanded retract that shares its shape; acceptance criterion: removing the `ch < 4` guard **and** setting `use_flag = 0x02` at the `on_use` acquire must each leave the TAIL vectors green. |
| 12 | any | boot restore | LOADED(ch), TAIL(ch), or UNLOADED | `boot` | **Spec.** Desc: implemented — `STA_TAIL_TAG` record (`Flash_saves.cpp:249`, `:383-412`, write `:448`), restored through the funnel. Section 6. |

Gate changes that consume the state: `allow_any` becomes
`state == UNLOADED || owner == ch`; `allow_stop` becomes
`owner == ch` (i.e. LOADED **or TAIL**). That one-line widening of `allow_stop`
is what lets a runout retract through; everything else in the table exists to
keep that widening from becoming a new way to hold a stale lock.

**There is no time bound on TAIL.** A bound sized against the longest
legitimate retract — travel budget plus 3 s no-progress
(`Motion_control.cpp:2396-2406`) plus 10 s redetect (`:2342`), so roughly 60 s —
is the obvious design and it is wrong, for three reasons:

1. Any finite bound re-creates "a timer overrides ownership", which is the
   disease this design exists to remove. The pre-change firmware's release at
   the debounce *was* a 1500 ms instance of exactly that, and the complaint was
   never the width of the window — it was that the retract had not arrived yet.
   Sizing a timeout means bounding how long the printer may take, and on a
   runout the printer pauses and can legitimately wait for a human.
2. The stale-lock role the timeout played is covered by an escape the old
   latch never had: `send_out` opens every printer-driven load, is accepted
   unconditionally, and preempts TAIL (row 7). A stale TAIL yields to the very
   next load attempt.
3. The counter-case: runout, printer pauses, the human resumes the next
   morning. The retract prelude arrives hours later; a 60 s T_tail expired
   overnight; `allow_stop` reads false; the original bug is reproduced by the
   escape hatch that was supposed to bound it.

The original cost table stands with one correction: the "too long — blocked
switchover" edge does not exist, because switchover arrives via `send_out` and
`send_out` preempts. What the timeout would have provided is diagnostic, not
protective, and is replaced by observability: **commands refused while TAIL is
held must be counted and evented** (OQ-3). The one wedge that survives with no
fuse is an `on_use` resume of a *different* channel with no preceding
`send_out`; no known printer sequence does this, and the counter is how we
find out if one exists.

A wrong transition in row 7 (preempt) costs a collision with nothing behind it
— true with or without a timeout, since a timeout never guarded that edge —
which is why row 7 is the row that must always emit an event, and why the
event carries the epoch: a collision report and the preempt that allowed it
must be joinable after the fact.

**Row 3 deferral (description as of `6dd50fb`).** The commanded-retract paths
still release at command time (`:398`, `:428`). Stated plainly so this
document does not overclaim: on every retract, commanded or runout alike,
**the merger is occupied-but-unowned from the moment of `:398`/`:428` until
redetect completes, and a jammed retract leaves it that way indefinitely.**
That is today's behaviour, not a
regression — but ownership does not track physical occupancy until row 3
lands, and no text in this document should be read as claiming it does. The
deferral is sound separability, worth recording: the widening (`allow_stop`
false→true while TAIL) and the narrowing (row 3) act in opposite directions
with no overlap, and the retract motion itself never consults the latch, so
the runout fix does not depend on row 3.

## 6. Flash

Current format, kept unchanged: 8-byte wear-levelled slots, payload
`w0 = STA_TAG<<24 | seq<<8 | ch`, integrity `w1 = w0 ^ MAGIC_STA`, newest
selected by wrapping sequence compare (`src/Flash_saves.cpp:347-422`). The
payload byte is the raw channel: `0-3` or `0xFF`.

**TAIL is persisted, in a separate flash record.** It has to be, because the
no-timeout decision (section 5) changed what persistence is worth:

**No-timeout and persist-TAIL are a coupled pair.** With no T_tail, TAIL's
legitimate lifetime includes the overnight runout pause — and an overnight
pause will sometimes contain a power cycle. If TAIL does not survive the
reboot, the morning retract is refused and the original bug returns through
the reboot instead of through the timer. Persistence is therefore not a
nice-to-have (the original "buys little" reasoning) but load-bearing.
**Reverting either decision later requires reverting both.**

The *form* of persistence matters, and the original rollback argument decided
it. The first implementation round persisted TAIL in-band as `0x80|ch` in the
existing STA payload byte and claimed downgrade was graceful. The claim is
false, and the defect is precise: boot restore assigns `g_loaded_ch = ch`
*before* its `ch < 4u` guard (`src/main.cpp:247`), so a downgraded firmware
keeps `0x82` in the latch — `allow_any` and `allow_stop` then read false for
every channel, only `send_out` is accepted, and a resumed job (which continues
with `on_use`, not `send_out`) is refused.

Adopted form: **a separate flash tag (`STA_TAIL_TAG`) in the same slot ring.**
Old firmware's record scan skips unknown tags via its existing
`continue` (`src/Flash_saves.cpp:363`) and falls back to the newest plain STA
record — which the new firmware writes as `0xFF` on TAIL entry, i.e. exactly
the value the old firmware would have written for itself after a runout. Both
properties hold at once: new firmware restores TAIL across a reboot; rolled-back
firmware behaves identically to today.

Firmware/state matrix, all four cells defined:

- new firmware over old state: plain byte `0-3` → LOADED(ch), `0xFF` →
  UNLOADED, no TAIL record present → never TAIL. Identical to today's boot
  restore.
- old firmware over new state: plain record reads `0-3`/`0xFF` as today; a
  TAIL record is skipped as an unknown tag. Identical to today.
- reboot mid-LOADED: restored, as today.
- reboot mid-TAIL: new firmware restores TAIL(ch); old firmware sees the
  plain `0xFF` and comes up unloaded — today's post-runout behaviour.

Flash churn note: with the current code, a runout during active printing flaps
the latch — the debounced clear fires (`:3113`), the next `on_use` poll
re-latches (`:375`), each edge marking `g_state_dirty` and costing a slot write
roughly every 1.5-2.5 s for as long as the tail feeds. Row 4 ends the flap:
TAIL entry writes its record pair once (plain `0xFF` plus the `STA_TAIL_TAG`
record), and `on_use(ch)` while TAIL — the printer still printing the tail —
is the owner speaking and transitions TAIL→LOADED (row 6 semantics apply via
the command path, immediate, no hold) which writes the channel once. A handful
of writes per runout instead of dozens. The wear ring absorbs either, but the
event stream should not have to.

## 7. Wire reporting

Spec: **bit 5 = owns (LOADED or TAIL); bit 6 = TAIL.** Desc: implemented
(`src/Motion_control.cpp:3311-3313`); bit 5's pre-TAIL meaning and its
behaviour through the debounce window are per `bd30fd3` (`src/bmcu_link.h:12`).
Rationale:

- bit 5 keeping its meaning as "owns" is truthful for existing decoders — a
  TAIL channel does hold the latch — so old decoders degrade gracefully.
- TAIL cannot be inferred from `bit5 & (ks == 0)`: that signature is already
  taken by the debounce-pending window (`bd30fd3` documents exactly this
  inference). Distinguishing "clear pending" from "TAIL held" is the difference
  between "about to lose the retract" and "retract protected" — the two states
  this whole design separates — so it is worth one of the two reserved bits.
- Cost: layout-revision bump, `docs/bmcu_wire_layout.json`, and the Python and
  TypeScript decoders, which must shift explicitly and never mirror the
  bitfield (`src/bmcu_link.h:26-29`).

The ownership-transition event (section 4) is the other wire artifact: one new
record type carrying `{from, to, owner, cause, epoch}`. Sized like the existing
`LogRecord` payloads; every row in section 5 that names a cause emits it.

## 8. The runout story, corrected

The sketch said: "normal changes work because the retract is commanded while
filament still rests on the switch; runout reverses the order, so the retract
is refused." Verified against the code, that is **directionally right but
mechanically incomplete**, and the incompleteness matters for testing:

- After the sensor clear, `allow_any` is true again (`loaded == 0xFF`,
  `:241`), so the very next `on_use` poll **re-latches** the latch
  (`:331` gate passes, `:375` latches). During tail feed-out the printer is
  still printing and still polling `on_use` at sub-second cadence.
- **Pre-debounce**, the clear ran every control pass while the key read 0, so
  between any `on_use` and the retract prelude the clear had always won:
  `stop_on_use` and `before_pull_back` land on `allow_stop == false` and return
  before writing anything (`:314`, `:384`). Deterministic refusal — consistent
  with "switchover has never once worked".
- **Post-`a705cc3`**, the clear needs 1500 ms of continuous key-zero, and each
  `on_use` re-latch restarts the window. Whether the retract prelude finds the
  latch held now depends on whether the printer opens its pause sequence within
  1500 ms of its last `on_use` poll. Plausibly it usually does — meaning **the
  debounce alone may already make runout switchover mostly work**, by accident
  of cadence, with a failure race remaining.

Consequence for acceptance testing. An earlier plan required a
debounce-only baseline run before the TAIL run. That requirement is dropped:
flashing is the scarce, brick-risking step, and the baseline's information is
recoverable from the TAIL run itself. **The acceptance run must capture the
event stream and compute d — the delay from the last `on_use` re-latch to the
retract prelude.** `d > 1500 ms` means the debounce alone would have lost the
latch and TAIL is credited with the pass. `d < 1500 ms` means the run cannot
distinguish TAIL from debounce cadence, and the report must say so rather than
crediting TAIL. Either way, do not credit TAIL with a fix the debounce may
already have bought without stating which case the data showed.

One confound neither state fixes: presence advertisement drops raw and
undebounced the moment the key opens (the `online` computation takes
`MC_ONLINE_key_stu` directly, `:3199-3211`). If the printer's switchover or
retract sequencing depends on the *presence* bits rather than on the retract
being accepted, that is printer-side logic this repository cannot see (OQ-1).

## 9. Timer consolidation — adjacent, riding the same branch

Description as of `6dd50fb` — the consolidation has landed
(`signal_hold_policy.h`, consumed at `Motion_control.cpp:2852`, `:2996`); the
inventory below records what it found and why the split came out as it did:

| Constant | Value | Site | Kind |
|---|---|---|---|
| `DM_AUTO_S1_DEBOUNCE_MS` | 100 | `:272`, used `:1317` | **state dwell** (revised — see below) |
| `DM_LOADED_DROP_MS` (was inline `100u`) | 100 | `:311`, via `signal_hold::held_for` `:2852` | signal hold — **consolidated** |
| `LOADED_LATCH_DROP_MS` | 1500 | `:340`, used `:3112` | signal hold |
| `AUTO_UNLOAD_EMPTY_MS` | 1500 | `:349`, used `:2986` | signal hold |
| `AUTO_UNLOAD_ARM_MS` | 1000 | `:347`, used `:2941`, `:2952` | **gesture window** |
| `AUTO_UNLOAD_MAX_MS` | 15000 | `:348`, used `:2972` | operation timeout |
| `REDETECT_TIMEOUT_MS` | 10000 | `:2342`, used `:2488` | operation timeout |

The sketch's four-holds/three-timeouts split was **one row wrong**:
`AUTO_UNLOAD_ARM_MS` is neither. It bounds the time between two *edges* — a
buffer spike past 80 % arming, then a return to neutral 45-55 within the window
activating (`:2920-2956`). It is a gesture detector and belongs to the gesture
machine, not to a hold primitive and not to the operation timeouts.

The implementation then found this document's own table one further row wrong,
recorded here: `DM_AUTO_S1_DEBOUNCE_MS` is not a signal hold either. It is a
**state dwell** — `dm_auto_t0_ms` is the autoload machine's generic
state-entry stamp, shared across roughly 25 sites, and the S1 window is merely
one comparison against it. Extracting it frees nothing and would entangle the
primitive with the autoload machine's whole lifecycle.

Two further findings from the implementation, both corrections to the original
sketch of the primitive:

- **A `{current value, when it changed}` primitive cannot express the
  remaining consumers.** Both watch `ks != 1`, and on DM builds `ks` takes
  four values (`dm_key_to_state`, `:260-268`) — a raw change-stamp restarts on
  a 2↔3 transition that the current code deliberately rides through. The
  primitive must store the **caller's predicate**, not the raw value.
- **The consolidation reclaims no state.** Of the four holds originally
  counted, one was reclassified out (the state dwell) and two consolidated
  onto the predicate-holding primitive; the measured RAM delta is zero, and
  the earlier estimate that it would shrink hold state was wrong. What the
  consolidation buys is one definition of "the key has read X for T ms" and
  named predicates at the call sites — legibility and a single retuning point,
  not bytes.

The two operation timeouts stay as they are (`4e9adf6` is explicit that
redetect times driving, not state residence). The gesture window stays with
its gesture.

Sequencing. Putting the consolidation after TAIL in a
separate change; it now rides the same branch as a final mechanical commit,
because flashing two BMCUs is the scarce, risky step and a second burn buys
attribution that commit granularity mostly buys anyway. The constraint that
makes this acceptable: same windows, same values, same edges, no retuning,
report-don't-fix on anything found. Two requirements keep a hardware failure
attributable without the second burn: **one commit per ported hold**, so
bisection is revert-and-reflash of a named commit; and **host vectors for the
predicate-holding primitive replicating each site's restart semantics before
porting** — of the ported holds, only the loaded-latch one had tests, and
"mechanical" is unverifiable by eye for the others without them.

## 10. Test strategy

No hardware is available; the strategy leans on the pattern the repository
already runs in CI (`ci/ams_loaded_latch_policy_test.cpp`,
`ci/test_ams_loaded_latch_policy.py`, and siblings): the policy is a pure
header, compiled and driven on the host.

1. **Table tests** — every row of section 5, plus every *rejected* transition
   (wrong owner, release when UNLOADED), asserting state, epoch increment, and
   emitted cause.
2. **Conversation tests** — drive the policy with command sequences taken from
   the captures: the healthy unload/reload
   (`tmp/a1-108-filament-failure/healthy-unload-reload-relabelled.log` gives
   the phase order: `stop_on_use → before_pull_back → 0xFF pull → send_out →
   before_on_use → on_use`), asserting the ownership trace
   LOADED→TAIL→UNLOADED→LOADED with no spurious releases.
3. **Funnel-routing tests.** This family guards the funnel: the `ams_state_*`
   call sites in `bambu_bus_ams.cpp` are the bus layer's only interface to
   the state machine, and the family exists because the first implementation
   contained exactly the leak it targets — **`release(s, 0xFF)` skipped its
   owner check and reached `reset()`, so the idle-frame wildcard (now `:461`)
   dropped TAIL** — the overnight case, defeating the whole branch,
   invisibly. The conversation test that catches it: runout entry, continued
   `on_use` polls, interleaved idle frames (`0xFF/0x01` and the `0xFF` reset
   pattern), pause, retract prelude — assert the prelude is accepted.
   Companion assertions: a commanded acquire while TAIL(ch) goes TAIL→LOADED
   without passing through UNLOADED (row 6, revised form); the commanded
   releases (`:398`, `:428`) are owner-checked and evented. Desc: this family
   ran, and found rows 9 and 10 as recorded in the table.
4. **Runout race sweep** — the scenario of section 8 as a parameterized test:
   key-zero at t=0, `on_use` polls at cadence *p*, pause sequence at delay *d*
   after the last poll; assert the retract prelude is accepted for **all**
   (p, d), where the debounce alone passes only for d < 1500 ms. This is the
   test that distinguishes TAIL from the debounce.
5. **Flap test** — runout during continuous `on_use` polling produces at most
   one flash-write pair per direction and exactly the events of rows 4 and 6,
   not a write per 1.5 s.
6. **Stale-TAIL escape** — jammed retract (no terminal transition): TAIL is
   held indefinitely (there is no timeout), other-channel
   loads are refused *until* a `send_out` arrives, and the `send_out` preempt
   (row 7) then releases with its event. Refused commands during the hold are
   counted (OQ-3).
7. **Boot matrix** — the four cells of section 6, including TAIL restoration
   from the `STA_TAIL_TAG` record and the rolled-back-firmware fallback.

Hardware acceptance, when a unit is free: **runout switchover with a paired
spool** — the owner reports it has never once worked, which makes it the
acceptance test. The debounce-only baseline run originally required here is
dropped (section 8): instead, the acceptance run must capture the event
stream and compute d, the delay from the last `on_use` re-latch to the retract
prelude. `d > 1500 ms` credits TAIL; `d < 1500 ms` means the run cannot
distinguish TAIL from debounce cadence and the report must say so rather than
crediting it.

Measurement worth taking regardless (feeds OQ-1): with `88697cc` firmware, the
transaction events now carry the addressed AMS number — capture the 0x03/0x04
cadence and addressing through one real runout to learn the printer's actual
pause timing and whether it stops polling before or after the retract prelude.

## 11. Corrections to the sketches this design was built from

- **The silent-refusal location.** `main.cpp:136` cannot block a legitimate
  load: both `ams_state_set_loaded` call sites sit behind
  `if (!allow_any) return true;` (`:287`, `:331`), so by the time `set_loaded`
  runs, the latch is either free or already naming that channel — the guard
  only suppresses a redundant re-latch. The blocking behaviour is real but
  lives in the bus-layer `allow_any` gate (`:241`). Earlier statements placing
  it in `main.cpp:136` were wrong.
- **The runout mechanism** is a clear-beats-relatch race, not a simple
  refused-retract, and the debounce may already have largely won it
  (section 8). Attribution therefore needs the d-computation on the
  acceptance run (the baseline run originally required for this was itself
  dropped — section 8).
- **The timer split** is four holds, two timeouts, and one gesture window —
  not four and three (section 9).
- **"No consumer needs the owner index"** is false; the debounce feed does
  (`:3108-3113`). It killed one argument for the mask representation; the flash
  integrity argument for it was already moot (section 4).
- Confirmed as sketched: the defect pair (sensor-driven release `:3113`;
  release path requiring possession `:241-242`, `:311`, `:381`); the rejection
  of motor-API tokens and of atomics; the single-superloop call structure
  (`main.cpp:271-277`).

## 12. Where the in-flight implementation is expected to diverge

Historical description: this list was written against `9747e99`, before any
TAIL code existed, as a prediction of where implementation would diverge. Its
scorecard, now that implementation has landed: items 1-3 materialized (item 1
is the recorded row-3 deferral; item 2 was the `release(s, 0xFF)` owner-check
leak, caught by test family 3; item 3 was the in-band `0x80|ch` persistence,
caught in review). Item 5 materialized as the row-8 finding, now OQ-5. Item 6
was resolved by revising row 6 rather than by code — the implementation's
argument won. Items 4 and 7 did not materialize. Kept as a record of what
this kind of checklist catches:

1. **Row 3** — commanded retract entering TAIL rather than clearing
   immediately. The obvious minimal patch keeps `:395`/`:425` as releases and
   adds TAIL only on the sensor path; that reintroduces "retract commanded ≡
   path clear" and loses row 5's failed-retract protection.
2. **Row 10** — the `0xFF` idle resets (`:432`, `:458`) clearing TAIL. They
   clear today; not clearing from TAIL is a behavioural change easy to miss.
3. **Flash encoding** — persisting TAIL in-band (e.g. `0x80|ch`) instead of
   writing `0xFF`. Section 6 reason 1 is why that is a rollback hazard.
4. **`stop_on_use`** — widening `allow_stop` for `before_pull_back` but
   forgetting `is_stop_on_use` (`:248`), which shares the gate and opens the
   printer's pause sequence.
5. **Row 8** — leaving the `send_out` force-clear (`:275-276`) unconditional,
   so the owner's own re-feed still drops ownership.
6. **The flap** (section 6) — TAIL entered from the sensor but exited by the
   next `on_use` re-latch *through the UNLOADED path*, producing
   TAIL→UNLOADED→LOADED churn with events and flash writes instead of the
   quiet TAIL→LOADED of row 6.
7. **No event emission** — the state added but not put on the wire, leaving
   the next desync as invisible as this one was.

## 13. Budget

Description; the authoritative budget numbers live in
`docs/BMCU_TARGET_HARDWARE.md` §2 and `docs/BMCU_REFACTOR_PLAN.md` §3 and
supersede the snapshot here. At design time the DM build stood at ~93 % of
61440. This design adds: the
policy header (pure functions, inlined — the comparable
`ams_loaded_latch_policy.h` costs well under 200 bytes of text), one event
record constructor, one channel-flags bit, and the `STA_TAIL_TAG` read/write
paths on the existing slot machinery. No timer exists, so
no timestamp for one. Estimated total well under 1 KB. RAM: 3 bytes of
ownership state; the timer consolidation's measured RAM delta is zero
(section 9) — any earlier implication that it would reclaim state was wrong.

## Open questions, marked rather than smoothed over

- **OQ-1**: Does the printer's runout/switchover sequencing key off the
  presence bits, the retract acknowledgement, both, or its own hub sensor?
  Unknowable from this repository; the section 10 measurement narrows it.
- **OQ-2** (settled): TAIL survives reboot —
  persisted via the separate `STA_TAIL_TAG` record (section 6), and coupled to
  the no-timeout decision: reverting either requires reverting both.
- **OQ-3**: With no timeout there is no fuse, so the diagnostic role a timeout
  would have played must be filled by counting and eventing commands refused
  while TAIL is held. That makes the one unfused wedge loud if it exists: an
  `on_use` resume of a different channel with no preceding `send_out`. No known
  printer sequence does that, but its absence is unproven. The counter is a
  requirement, not yet implemented.
- **OQ-4** (settled by measurement): the printer-idle clears row 9 originally
  preserved never fired — the `use_flag` guard is shut whenever the merger is
  held (row 9's note). The requirement replacing them is idle-neutrality:
  session-idle inputs never release ownership, in any state.
- **OQ-5**: Row 8's disposition. The same-channel `send_out` guard specified
  there was never implemented — the preempt fires for the owner's own re-feed
  and `preempt()` cannot distinguish channels (`ams_merger_policy.h:225`).
  The transient UNLOADED window it causes is also pre-TAIL behaviour, and it
  emits a row-7 event each time, so the ownership event stream will show how
  often it happens in practice. Decide from that data whether to implement
  the guard (spec kept) or retract the row (spec withdrawn); until then the
  row is neither.
