# BMCU firmware refactoring plan

Status: plan. Assumes the host-test shim lands (in progress in parallel);
section 8 states what shrinks if it does not. Verified against the tree at
`83355ed`; budget numbers come from `docs/BMCU_TARGET_HARDWARE.md` and are
cited, not assumed.

Convention: this document follows the specification/description distinction
defined in `docs/BMCU_LOADED_LATCH_DESIGN.md` §0. The steps and rules (R1-R4)
are **specification** — if practice disagrees, practice is wrong or the step
is renegotiated here, not silently. Code citations and size figures are
**description**, dated to `83355ed`; line numbers drift as the tree moves
(the shim commits have already shifted `bambu_bus_ams.cpp` by a few lines)
and a stale citation is a correction, not a contradiction. The S4 section has
already had one description corrected this way — AHUB personality selection
is runtime, not compile-time — which is the distinction working as intended.

This plan is deliberately small. The binding constraints are: **3824 bytes of
flash headroom** (worst-configuration text+data 57616 against the 61440
postbuild ceiling,
`BMCU_TARGET_HARDWARE.md` §2 — the ceiling exists because the last 4 KiB of
the part is the NVM sector, `Flash_saves.h:9`, protected by convention only);
**hardware validation is the bottleneck** (one flash cycle per burn, brick
risk over TTL, part of the rig still blocked by an enclosure fault); and **the
wall-clock cost of a flash erase/program is unknown and will stay unknown**
(§6 of the hardware doc; the owner has decided not to measure it). A step
verifiable on the host is worth several that are not, and no step below is
justified by a flash-timing number nobody has.

## 1. The principle the plan implements

Established across this week's analysis and recorded here as the plan's
spine: the inherited code encodes decisions as control flow — early returns,
flag writes, raw comparisons — rather than as data: states, causes,
transitions. Everyone who needs a decision to exist as data hits that wall:
the observation layer hit it first (the 26,000-event misclassification), the
merger latch hit it for years before any observer existed (a raw sensor
comparison acting as a state transition), and the honest difference between
inherited and fork code is not cleanliness but **failure half-life** — fork
defects are host-testable and live days; inherited defects lived years.

The plan therefore does one kind of thing repeatedly: move a decision from
control flow into data, at a seam where doing so is cheap, and put a host
test on it. It does not restructure for structure's sake, and it leaves the
largest tangle deliberately untouched (section 6).

## 2. Standing rules, enforced from the first step

These are not aspirations; steps land only if they comply, and R1/R4 are
checkable in CI.

- **R1 — one funnel per shared state.** Every new write to shared state goes
  through a policy header with a cause code, from day one. The merger latch
  is the model (`ams_merger_policy.h` behind `ams_state_*`). Enforcement: a
  CI grep test asserting the direct-writer count of each funneled state does
  not grow (the pattern exists — `ci/` already runs source-shape tests).
- **R2 — no new silent early-returns.** Any handler early-return added or
  touched gains a cause: a classification, an event, or a counter. The
  `88697cc` addressed-AMS capture is the model.
- **R3 — every step lands with its size delta.** `riscv-wch-elf-size` before
  and after, in the commit message, against the 61440 ceiling
  (`platformio.ini:8`). Estimates below are estimates; the build is the
  authority.
- **R4 — no step is justified by flash-write timing.** The duration of a
  fast erase/program is unverified anywhere in this tree
  (`BMCU_TARGET_HARDWARE.md` §6). Reducing flash writes is defensible on
  endurance and on the verified interrupt-disable *shape*; any step whose
  value depends on "saves M milliseconds" is unjustifiable without the
  number and is excluded (section 6 lists one).

## 3. Budget policy

3824 bytes is the whole purse. The plan spends it in two installments with a
checkpoint: steps S2-S3 are budgeted at ≤ 600 bytes combined; if the measured
total exceeds that, stop and re-evaluate before S5. S4a, if it applies, is
the only step that *adds* headroom, which is why it runs early. At least
1.5 KiB must remain unspent at all times — that margin is for fixes, not
features.

## 4. Ordered steps

Each step is independently landable and states its verification class:
**host** (no burn needed), **ride-along** (behaviour observable on hardware,
validated on the next planned burn, does not justify one), or **burn**
(requires a dedicated hardware validation).

### S1 — Conversation-vector regression suite. Host. ~0 flash.

Precondition: the shim. Encode, in order of value: the two round-one routing
bugs as permanent vectors (both were caught only by tests, both were in the
routing rather than the state machine); the peer-addressed suite — frames
addressed to the other AMS number produce no reply and the correct
outcome/reason (`bambu_bus_ams.cpp:605`, `:790` and siblings; the
26,000-event story becomes an assertion); the funnel-routing family from
`BMCU_LOADED_LATCH_DESIGN.md` §10 re-run against the **real** `set_motion`
rather than the policy-only model; the runout race sweep (key-zero at t=0,
`on_use` cadence × pause delay, retract prelude accepted for all
combinations). Raw vector bytes already exist in the tree — the frame
comments at `bambu_bus_ams.cpp:464`, `:502-503`, `:696` and the healthy
capture's phase order.

Buys: the net under every subsequent step, and the first regression suite the
inherited bus code has ever had. Costs firmware nothing.

### S2 — Refused-command observability. Host logic; value realizes on hardware. ≤ ~200 bytes.

The successor obligation from the retracted TAIL timeout
(`BMCU_LOADED_LATCH_DESIGN.md`, OQ-3): commands refused by the `allow_any` /
`allow_stop` gates (`bambu_bus_ams.cpp:240-249`) are counted and evented with
the ownership state at refusal time. This is the only detector for the one
unfused wedge the no-timeout design accepts (an `on_use` resume of a
different channel with no preceding `send_out`), and it makes the next
desync loud instead of invisible — the property whose absence cost this
week. Reuses the existing event path; complies with R2 by construction.

Verification: host (the S1 harness asserts the count and cause); hardware
merely makes the data arrive.

### S3 — Sensor input boundary. Host logic; **ride-along** for behaviour. ≤ ~400 bytes.

`filament_channel_inserted` is recomputed every pass from a bare ADC window
comparison (`Motion_control.cpp:564`) with no hold anywhere; a flap instantly
zeroes the key state (`:536`, `:690`), resets the DM autoload machine, and
drops presence advertisement to the printer mid-print. The 1500 ms latch
debounce absorbs the *ownership* consequence; nothing absorbs the rest.
Apply `signal_hold_policy` at the two source sites — inserted and the key
publication — so every downstream consumer is filtered without touching any
of them. Asymmetric by design: insertion may assert immediately; removal is
held. This retires the P2 defect class at its source.

Honesty about verification: the logic is host-testable, but this **changes
observable behaviour** — removal detection is delayed by the hold window —
so it validates on the next planned burn, and its hold value is an open
question (OQ-B): a conservative 100 ms (the existing short-hold class) until
hardware says otherwise. The ADC pipeline already bounds signal freshness at
~4.8 ms (`BMCU_TARGET_HARDWARE.md` §5), so a 100 ms hold is ~20 boxcar
windows — comfortably above noise, far below human-scale removal.

### S4 — Resolve the AHUB question, both branches specified. Decision first; then host.

`ahub_bus.cpp` is a second inherited bus personality sharing `ams[]` and
writing `bus_now_ams_num` (`:183`), carrying exactly one instrumentation call
(`:341`). Whether any *user* runs AHUB is still unanswered (OQ-A), but how it is
selected is now settled from the code, and it is not what S4a assumed: **AHUB is
not a compile-time personality.** `main.cpp:355` calls `ahubus_run()`
unconditionally on every main-loop pass, in every shipped configuration, and
`bus_host_device_type` flips to `host_device_type_ahub` at runtime when an AHUB
heartbeat arrives (`:373-374`).

Two consequences. S4a is more expensive than "empty one arm of a switch" — the
flag has to be introduced, because there is no arm to empty today. And S4a buys
CPU as well as flash: both `ahubus_run()` and `bambubus_run()` open with a
`time_ticks32()` read and an **interrupt-disabled critical section** to snapshot
the same three receive fields (`ahub_bus.cpp:357-374`,
`bambu_bus_ams.cpp:1247-1252`), so every pass pays two of them and one is for a
personality that is probably idle. On a device answering a 1.25 Mbps bus, with
the flash-write stall already an unknown quantity (R4), interrupt-disabled time
is worth reclaiming where it is known.

The plan does not wait on the answer:

- **S4a — if dead:** compile it out behind a build flag (the build matrix
  already varies features; `FIRMWARE_BUILD_MATRIX.md`). This is the only
  step that *creates* headroom, plausibly 1-3 KiB — measured, not promised
  (R3) — and it funds everything after it. The main-loop personality switch
  (`main.cpp:275` region) keeps its shape; the flag empties one arm.
  Verification: host build matrix + S1 suite unchanged.
- **S4b — if alive:** minimum-viable observability only — route its dispatch
  through `printer_bus_result::classify_transaction` and the transaction
  event, exactly the `88697cc` treatment, sized ≤ ~300 bytes and landed only
  if the S2-S3 checkpoint left room. No restructuring. Every diagnosis gap
  closed on the bambu path this week is currently still open on this one,
  and if it is alive, the next failure will land in it.

Sequencing note: ask OQ-A immediately; run S4a/S4b after S1 so either branch
lands against the suite.

### S5 — Extract `set_motion`'s decision into a policy header. Host. Target ~0 net flash.

The accept-flag table (`bambu_bus_ams.cpp:232-249` plus the per-branch
guards) becomes a pure function: (frame class, statu/motion flags, ownership
state, motion enum) → {accepted, transition, reply class, cause}, on the
`printer_bus_result.h` pattern — writes stay where they are. This is where
three of this week's transition-table rows live and where both round-one
bugs were caught; after S5, the next state added to this area (row 3 is
already queued) is a table change under test instead of a flag-soup edit.

Preconditions, strict: shim landed, S1 green, and S1 vectors pass
**unchanged** across the extraction — that is the definition of done, and it
is why this step needs no burn despite touching the most protocol-critical
file in the tree. Flash target is neutral (a decision table against
flag soup, with `-msave-restore` already minimizing prologue cost —
`BMCU_TARGET_HARDWARE.md` §7); if the measured delta exceeds +300 bytes,
defer until S4a headroom exists or shrink the table.

### S6 — Land design row 3: commanded retract enters TAIL. **Burn.** ≤ ~200 bytes.

The deferred narrowing from `BMCU_LOADED_LATCH_DESIGN.md` §5: today
`:395`/`:425` release ownership at command time, so the merger is
occupied-but-unowned for the duration of every retract and indefinitely
after a jammed one. After S5 this is a policy-table change plus funnel
calls, with its host tests written before the change (S1 family extends).
It is the only step in this plan that requires a dedicated hardware
validation, because it changes when ownership releases during every normal
unload — and it should share a burn with whatever the TAIL acceptance
testing already requires, not claim one of its own.

### S7 — P3 containment: the sentinel vocabularies. Zero firmware flash.

`pressure` is one u16 carrying measurements, protocol sentinels
(0x4700/0x2B00/0x1E34/0xF9C6/0xFF74), and a jam flag (0xF06F), already
decoded by guesswork (`bmcu_pressure_class`, `bambu_bus_ams.cpp:203-208`);
`filament_use_flag` doubles as presentation and gate token (`:431`). The
affordable fix is containment, not re-encoding: document the sentinel table
in `docs/bmcu_wire_layout.json` so no consumer plots sentinels as telemetry
(check first whether Bambuddy already does — OQ-C), and adopt the rule that
no new sentinel values enter these fields; a new fact gets a new field under
R1. Re-encoding the wire is rejected below.

## 5. Verification summary

| Step | Class | Needs shim | Needs burn |
|---|---|---|---|
| S1 vectors | host | yes | no |
| S2 refused-command events | host | yes (for proof) | no |
| S3 sensor holds | host + ride-along | preferred | no (rides) |
| S4a AHUB compile-out | host | no | no |
| S4b AHUB observability | host | preferred | no |
| S5 set_motion decision | host | **required** | no |
| S6 row 3 | host + burn | **required** | **yes** (shared) |
| S7 sentinel containment | n/a (docs/Pico) | no | no |

One dedicated hardware validation in the whole plan (S6), and it shares a
burn with TAIL acceptance work already owed.

## 6. What this plan deliberately does not do

- **Restructure the motion megafunction.** Its per-channel decomposition
  exists (`motor_motion_switch`); the poison is shared flag soup across
  ~3600 lines, and extracting that is a rewrite the flash budget and the
  validation bottleneck cannot fund. The shim makes this file *buildable*
  on a host, not meaningfully *testable* — the distinction from
  `bambu_bus_ams.cpp`, where the shim buys real isolation. Bolting bounded
  fixes there (`4e9adf6` is the model) remains the correct engineering
  answer, not a concession.
- **Any write-batching or write-elision step justified by latency.** The
  candidate exists — coalescing merger-latch flash writes — and it is
  excluded under R4: its value depends on a per-write duration this tree
  does not have and will not measure. The endurance case is already
  adequate without it: the 10-page STA ring absorbs 320 ownership changes
  per wrap (`BMCU_TARGET_HARDWARE.md` §6), and the TAIL work already
  removed the runout write-flap.
- **Re-encode `pressure` or `use_flag` on the wire.** It would break every
  existing capture and decoder, cost a layout revision plus flash, and buy
  what S7's containment buys for free.
- **Self-modifying code or RAM-relocated code paths.** Rejected previously
  and confirmed unavailable by the hardware doc (§8): flash writes are
  blocking, interrupt-masked, and there is no write-while-execute mode in
  this tree.
- **Re-sync with upstream.** Merge-base `9573821` (2026-06-19); the src tree
  is past mechanical merging, and no step here is shaped to ease a merge
  that is not going to happen.
- **Representation changes that need `ctz`/`popcount`.** No B extension,
  verified down to the assembler refusing it (`BMCU_TARGET_HARDWARE.md` §3);
  the intrinsics are libgcc calls. The struct-over-mask decision in the
  latch design already accounts for this and stands.

## 7. Order and the quarter-sized honest answer

Order: **S1 → (OQ-A answer) → S4 → S2 → S3 → checkpoint → S5 → S6 → S7
whenever.** S7 has no dependencies and can land any time; S2 and S3 can
swap freely.

If only three things are affordable this quarter, they are **S1, S2, and
S4** — the net, the alarm, and the headroom question. Those three cost at
most ~200 bytes of flash between them, need zero dedicated burns, and leave
the tree strictly better positioned for everything else. S5/S6 are the
payoff steps but they are the ones that must not be attempted without the
net; deferring them costs nothing except leaving row 3's
occupied-but-unowned window open, which is today's behaviour, documented as
such.

## 8. If the shim does not land

The plan shrinks rather than dies. S4a survives intact (build-flag work,
no shim needed). S2 and S3 survive with weaker verification — review plus
ride-along observation instead of host proof — and should still land,
because their value does not depend on the shim, only their confidence
does. S7 survives untouched. **S1, S5, and S6 do not happen**: S5 without
the vector suite is exactly the kind of on-inspection restructuring of
protocol-critical code that this week demonstrated goes wrong twice before
it goes right, and S6 without S5 is a flag-soup edit to the accept table.
In that world the honest quarter is S4, S2, S3, S7 — and renewed pressure
on getting the shim built, because the two highest-value steps in this
plan are gated on it.

## Open questions

- **OQ-A**: Is AHUB mode in use on any deployed unit? One answer from the
  owner selects S4a or S4b; the plan proceeds either way.
- **OQ-B**: The S3 hold window. 100 ms is a defensible default (≈20 ADC
  boxcar windows, existing short-hold class); hardware margins for
  VMIN/VMAX at `Motion_control.cpp:564` are unmeasured and stay so until a
  ride-along burn reports.
- **OQ-C**: Does any current consumer plot `pressure` raw? Determines
  whether S7 is preventive or corrective. Checkable on the Pico/web side
  without touching firmware.
- **OQ-D**: S4a's actual headroom yield. Measured by building the flag-off
  variant; the plan's later budgeting firms up only after this number
  exists.
