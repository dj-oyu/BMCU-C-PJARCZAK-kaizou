# Handover — the loaders got fixed, and doing it turned up four firmware bugs

Rewritten 2026-08-08, replacing the 2026-08-07 pull-fault handover, which
survives at `git show 6f2a86f:handover.md`. Code line numbers are against tree
`6f2a86f` unless a symbol name is given; the two fixes shift them by a few
lines.

**What changed since the last version.** The 2026-08-07 session ended with two
channels behaving inexplicably and a firmware fix awaiting hardware validation.
The 2026-08-07/08 session resolved both channels — **both faults were
mechanical, and the electronics were innocent in both cases** — and along the
way produced the first-ever `dm_teardown` captures from the printer-side board.
Those captures exposed four firmware defects that nobody was looking for; one
of them, an autoload livelock that drives the motor forever, is fixed and
tested. The pull-fault fix from the previous session finally got its hardware
test too, and passed. **Nothing in the tree is now unvalidated.** The open items
are the three low-priority defects in §2.2/§2.3/§2.4, the untested runout path
in §5 Test C, and one non-urgent mechanical question in §6.9.

**Addendum, later on 2026-08-08 — read §10 before using any snapshot-derived
observation point in §7.** A second session that afternoon added an OLED status
panel to the Pico bridge (§10.1) and, in validating it, established that **the
printer-side link has had no baseline since the Pico last rebooted**: STATUS
still streams and every field in it is correct, but `snapshot.bin` carries no
channel data and `link_state` reads `stale` (§10.2). That is not a regression
and not a firmware fault — it is what the bridge does when it joins a BMCU that
is already running — but it silently disables the DM_KEY record, the
motion-fault code and `motor_pwm`, which several procedures below depend on.
The printer was mid-print at writing, so the BMCU reboot that would fix it was
deliberately not performed.

**State at writing:** branch `alpha` at `1f9d41d`, **pushed to origin** (the
firmware work below is at `b4725ee`; `11b38df` adds the Pico panel and its
tests, `1f9d41d` repairs CI). Host suite 288 tests green locally. **GitHub
`host-tests` had failed on every push since 2026-08-03** — see §10.3; it was a
CI toolchain gap, never a code defect, but it means the green ticks on
`b4725ee` and everything before it were absent, not passing. Both
firmware fixes are committed (`fd4ae8d` pull-fault, `d441061` livelock) and a
rebuild of that tree is byte-identical to the binary that passed Tests B and D,
sha256 `e87f5d9c...4de49120`. One working BMCU
(ams-a) on an **A1 mini**; the second unit's motherboard is destroyed and its
main board survives as a bench rig (24 V input broken, COM6, switch/ADC only).
Telemetry via the Pico bridge `bmcu-monitor-a` at `http://192.168.1.73` (use the
IPv4 — mDNS answers IPv6 first and urllib hangs).

**What is running on the board — wire-verified 2026-08-08:**
`variant_flags=0x9601`, `build_hash=0x0C0C8F38`. 0x9601 decodes as DM
two-microswitch **on**, RGB off, not P1S, not soft-load, ams_a, retract 600 mm.
This is `dist-2026-08-08\ams_a_060_autoload1_rgb0_livelock-fix.bin`, carrying
**both** fixes, and both have now passed on hardware: the Stage2 livelock fix
(§5 Test D) and the pull-fault veto fix (§5 Test B).

---

## 1. Loader repairs — what was wrong, and how it was proven

Two channels were misbehaving for reasons that looked like firmware and were
not. Both are now fixed. The evidence is in
`..\diagnostics\2026-08-07-exchange-failure\`.

### ch2 (slot 3) — inner microswitch closed on an empty loader

`FULL_RECORD_DM_KEY` shipped in this flash and immediately showed ch2 reading a
rock-steady **1042 mV with the loader empty**, against 8–13 mV on the other
three. Against the bench band survey (`none` 7–24 mV, `inner` 1025–1054,
`outer` 1542–1564, `both` 1850–1869) that is dead centre of `inner`: the inner
switch was electrically closed with nothing in the slot.

The calibration then made it invisible. `dm_key_none_threshold_from_idle`
(`MC_PULL_calibration.cpp:72`) sets `thr = idle + 0.10 V`, so it had learned
1.15 V and reported a tidy `none` at idle — converting a hardware fault into
silence and costing ch2 the `inner` state permanently, since `dm_key_to_state`
returns `none` below `none_thr` and no real `inner` press can clear 1.15 V.

**The threshold did not heal when the switch was repaired.** ch2 idled at 8 mV
while flash still held `thr = 1150`, so `outer` and `both` decoded fine and
autoload worked, but `inner` was still swallowed. A recalibration on empty
loaders brought all four channels back to the 600 mV floor
(`recalibration-1922.log`). This ordering matters: **repair, then recalibrate,
then verify all four thresholds read 600.**

### ch3 (slot 4) — filament never pressed the outer lever

ch3's autoload died every time. The trajectory with filament topped out at
**1041 mV (`inner`)** and never reached `outer` or `both`
(`band-sweep-slot4-slot1-1758.log`, 139 samples), while ch0 on the same board
went `none → outer (1573) → both (1883)` and **parked at `both`** for 45 s+.
That park is exactly what `S2_PUSH` requires held for its whole 120 mm push, so
ch0 autoloaded every time and ch3 could not, ever.

The decisive test was the daughterboard removed and the switches pressed by
finger (`ch3-finger-press-1813.log`, ~40 transitions): ch3 produced **every**
band inside the reference ranges — `none` 8 mV, `inner` mean 1017, `outer` mean
1556, **`both` mean 1867**. Switches, ladder and wiring all healthy; `both` was
electrically reachable all along. The fault was purely mechanical, in how the
loader presented filament to the outer lever.

Fixed mechanically by the operator. Confirmation is on the wire in
`recalibration-1922.log`, which caught it incidentally: **ch3 went
`none → outer (1557) → both (1864)` and held `both` for five minutes** — the
park-at-`both` behaviour it had never once shown. **Autoload now works on all
four slots.**

Two notes worth keeping. `filament_channel_inserted` is latched once at boot
(`MC_PULL_ONLINE_init`, no periodic re-check), so unplugging a daughterboard
mid-session does not blind the channel; and the raw `g_dm_key_v[]` is captured
at `Motion_control.cpp:646`, *before* the inserted gate, so `key_mv` stays
honest even when `ks` and `online` are forced to zero. Both facts are what made
the finger test possible. Also, `MC_PULL_calibration_boot()` early-returns when
`Flash_MC_PULL_cal_read()` succeeds, so a reboot with hardware detached does
**not** silently recalibrate.

## 2. Four firmware defects found while doing that

None of these caused the loader failures. All four are real and all four were
invisible before this session. §2.1 is fixed and hardware-tested; §2.4 is what
fixing it exposed; §2.2 and §2.3 are untouched.

### 2.1 Autoload Stage2 livelocks — the retry limiter is defeated

**Live on ch2, 2026-08-08 04:36**, filament in the loader and the printer idle:
autoload cycled **six times at a 25 s period with no sign of stopping**, motor
driving at ±900 PWM throughout, `dm_fail_latch` clear the whole time
(`..\diagnostics\2026-08-08-autoload-livelock\ch2-livelock-0436.log`).

```
DM_TEARDOWN ch2 state=S2_PUSH cause=BUFFER_ABORT ks=both held_ms=10507
... x6, held_ms 10329..11061, intervals 24.7..25.6 s, dmfail=False always
```

The cycle:

1. `S2_PUSH` drives ~10.4 s until `MC_PULL_pct_q > DM_AUTO_BUF_ABORT_PCT`
   (75 %) → `BUFFER_ABORT`, `dm_auto_try++`, → `S2_RETRACT` while try < 3
2. `S2_RETRACT` backs off until buffer ≤ 50.2 % **or `ks == 2`**; on `ks == 2`
   it goes to `S1_DEBOUNCE` (~`:1568`)
3. `S1_DEBOUNCE` → 100 ms of `outer` → `S1_PUSH`
4. `S1_PUSH` sees `ks == 1` → `S2_PUSH`, **setting `dm_auto_try[CHx] = 0u`**
   (~`:1419`)

Step 4 is the defect. Every pass through Stage1 zeroes the retry counter, so
the 3-strike limit that should latch `dm_fail_latch` and park the channel never
accumulates. It is only reachable if the retract happens to stop while `ks` is
still `both`, which on this geometry it does not.

The buffer filling is *correct* behaviour — an uncommanded autoload pushes
120 mm into the merger with no extruder pulling. What is wrong is that the
machine never gives up. **To stop it on a running board, withdraw the filament
until the key reads `none`** — the global sweep then forces `DM_AUTO_IDLE`.

**Fixed in tree and confirmed on hardware 2026-08-08 05:19** (§5 Test D,
`testD-fix-confirmed-0519.log`): exactly three `BUFFER_ABORT` teardowns, then
`dm_fail_latch` and `pwm=0`, and no motor drive afterwards. Withdrawal cleared
the latch via `GLOBAL_CLEAR ks=none` and the re-insertion got a fresh three —
so the fix does not overshoot into a latch that will not clear. `dm_auto_try` is now
documented and treated as a per-*insertion* budget: `src/dm_autoload_retry_policy.h`
holds the accounting (`count_abort`, saturating; `insertion_ended`, true only
for `ks == 0`), the two mid-insertion S2 entries no longer clear it, and the
two teardowns that drop to idle clear it only when the strand actually left —
`inner` used to hand back a full budget to a strand that never moved. Host
tests in `ci/dm_autoload_retry_test.cpp` replay the recorded cycle and assert it
latches on the third abort. **Caveat on those tests:** they cover the header, so
they would not catch a refund reintroduced at a call site in
`Motion_control.cpp`. The call sites are pinned by comments instead.

### 2.2 Stage1's `ks == 0` teardown has no debounce, and the sweep always wins

The first `dm_teardown` ever captured on this board, 2026-08-07 17:34, filament
hand-fed into ch3 (`ch3-autoload-teardown-1734.log`):

```
t=127.22s NOTICE state_change ONLINE_MASK  0 -> 8
t=127.42s INFO   DM_TEARDOWN ch3 state=S1_PUSH cause=GLOBAL_CLEAR ks=none held_ms=102
t=127.42s NOTICE state_change ONLINE_MASK  8 -> 0
```

The 200 ms online window decomposes as 100 ms `S1_DEBOUNCE` + 102 ms
`S1_PUSH`, which also confirms `held_ms` is time-in-state. Two consequences:

- **Stage1 entry is reachable.** The `outer`-held-100 ms debounce satisfied and
  the machine got to `S1_PUSH`. The old §5.3 claim that the entry can never
  satisfy on a moving filament is **retracted** — the transit is brief, not
  always too brief.
- **The killer is the un-debounced global sweep**, not a timeout. 102 ms of
  driving took the key from `outer` to `none`, and the sweep
  (`Motion_control.cpp` ~`:2931`) tears down on the *instant* `ks == 0`
  reading. Only `dm_fail_latch` got the `held_for` treatment in `c15193c`;
  this path did not.

### 2.3 `TEARDOWN_KS_EMPTY` from `S1_PUSH` is unreachable dead code

`S1_PUSH` has its own `ks == 0` branch reporting `TEARDOWN_KS_EMPTY`
(`Motion_control.cpp:1411`), but the sweep runs first and has already forced
`DM_AUTO_IDLE`, so from inside the machine there is no state left — the source
comment at `:2925` says as much. **Any diagnosis keyed on seeing cause=2 from
`S1_PUSH` will wait forever.**

### 2.4 `DM_AUTO_S2_FAIL_RETRACT` is unreachable, so a failed autoload leaves the buffer stuffed

Exposed by fixing §2.1, and pre-existing rather than caused by it. The
three-strike branch sets the latch and schedules the recovery retract in the
same pass (`Motion_control.cpp` ~`:1526`):

```c
if (spent) {
    dm_fail_latch[CHx] = 1u;
    dm_auto_state[CHx] = DM_AUTO_S2_FAIL_RETRACT;
}
```

but the next pass opens with (~`:1349`):

```c
if (dm_fail_latch[CHx]) { dm_autoload_x = 0; ...red... }
else { ...the entire state machine, including case DM_AUTO_S2_FAIL_RETRACT... }
```

The latch skips the state machine wholesale, so the retract that is supposed to
back the strand out and relieve the buffer never runs. `DM_AUTO_S2_FAIL_EXTRA`
is reachable only from it and is dead for the same reason. The channel parks
red with the buffer wherever the third abort left it — **observed at 100 % on
ch2, 2026-08-08**.

This was latent before §2.1 was fixed, because nothing ever reached three
strikes. Not urgent: a stuffed buffer does not block a printer-commanded load
(§6.9 covers what does), and withdrawing the filament clears it. But the
recovery it was written to perform has never once executed.

## 3. The event ring is flooded, and it is not a cosmetic problem

The 16-entry event ring is filled at roughly 8 Hz by the ignored printer long
frames (`0x0411`/`0x023C`/`0x0237`/`0x021A`), so it holds **about two seconds
of history**. The first read of it during this session contained nothing but
those frames. Every `dm_teardown` above was caught only by polling at ~10 Hz;
a 1 Hz poll sees none of them.

The previous handover listed these frames under "no harm is yet attributable".
That is no longer true — they are destroying the diagnostic channel that the
`c4ff9bc` pressure-deadband fix was written to protect. **The deadband fix
itself works**: teardown events do reach the queue and survive. Identifying
these frames should move up the list.

`/api/events.bin?after=<seq>` was also unhelpful, returning a single event; the
snapshot's 0xF1 records are the working path.

## 4. Firmware changes — both now validated on hardware

Two fixes are in the working tree, both uncommitted, **both confirmed on
hardware 2026-08-08**. Nothing in the tree is unvalidated.

**The pull-fault veto fix** (`src/motion_fault_policy.h`, the veto call in
`Motion_control.cpp`, host tests in `ci/`) passed §5 Test B at 05:41
(`testB-pullfault-confirmed-0535.log`) — and passed it against
**`PULL_NO_PROGRESS`, the incident's own fault code**, not the
`REDETECT_TIMEOUT` stand-in the procedure was written around:

```
05:35:43 ERROR  MOTION_FAULT ch0 NONE -> PULL_NO_PROGRESS
05:41:47 NOTICE MOTION_FAULT ch0 PULL_NO_PROGRESS -> NONE
05:41:49 ch0 fault=0 ams_motion=send_out pwm=597      <- gear drove
05:42:45 ch0 fault=0 ams_motion=before_on_use         <- load completed
```

No power cycle between latch and release. Earlier staged bin
`dist-2026-08-07\ams_a_060_autoload1_rgb0_pullfault-fix.bin`, sha256
`dfea374aee5d078d47841e70707a8da0baab75cbc69b3bc44aa10d7716ea62cd`.

**The Stage2 livelock fix** (§2.1) is **flashed and confirmed** — wire-verified
2026-08-08: `variant_flags=0x9601` unchanged, `build_hash` moved
`0x69EFC9EC` -> **`0x0C0C8F38`**, and Test D passed. Staged bin
`dist-2026-08-08\ams_a_060_autoload1_rgb0_livelock-fix.bin`, sha256
`e87f5d9c8bf6bc1b69e532195ac363c9b73efa485e26bef71412d0784de49120`, source tree
`git stash` object `d6d4c232b87e75c6502ae7315184c100d5d877c0`. Flash 88.0 %
(54,088 of 61,440 bytes). It also carries the pull-fault fix, so **Test B can
now be run against the board as it stands** — that is the one remaining
unvalidated change.

Build command for both:

```
BAMBU_BUS_AMS_NUM=0 AMS_RETRACT_LEN=0.60f BMCU_DM_TWO_MICROSWITCH=1 \
BMCU_ONLINE_LED_FILAMENT_RGB=0 DBMCU_P1S=0 BMCU_SOFT_LOAD=0 \
pio run -e fw
```

**Do not build this as env `ams_a_060`.** The `ams_a_*` envs set only AMS
number and retract length, leaving `BMCU_DM_TWO_MICROSWITCH` at 0; the online
key then degrades to "voltage > 1.7 V" = both switches, every loaded channel
reads offline, and the fix looks broken while being untestable. The §5 flash
check exists to catch exactly this.

Full CI is green: 247 tests, including the two new policy suites.

## 5. Verification procedures

### Flash

- Tool: the flasher in `..\BMCU-Flasher-windows-x64\`; serial details in the
  project memory note `bmcu-firmware-flashing`.
- **115200 baud** — 1 Mbaud fails mid-program and presents as a brick; the
  recovery is simply retrying at 115200.
- **No full erase** — it wipes pull/key calibration.
- **Confirm what's running** afterwards: `build_hash` must differ from the
  pre-flash value and `variant_flags` must still read `0x9601`. A cleared bit 0
  means the wrong image. Decoder one-liner in §7.

### Test A — regression: a normal exchange

Load → unload → load on a healthy slot from the printer's AMS screen. Expect no
ERROR events, `motion` walking send_out → before_on_use → on_use, normal LEDs.

### Test B — the pull-fault fix: fault → printer load → recovery without reboot

**Passed 2026-08-08** (§4). Recorded procedure notes, kept because the timing is
easy to get wrong and this test is worth re-running after any veto change.

The written repro aims for `REDETECT_TIMEOUT` (fault 3), which shares the veto
and release path with the incident's fault 1. In the run that passed, the
operator withdrew a moment earlier and got **fault 1 itself** — the stronger
result. Either code proves the release path; do not re-run just because you got
1 instead of 3.

1. From a loaded slot, command an unload **from the printer's AMS screen**;
   while the BMCU retracts, pull the filament fully out by hand so the key reads
   `none`. The printer will likely raise its own error during the next step —
   expected and irrelevant.
2. Redetect pushes toward a switch that never answers → fault latches after
   10 s. Confirm via ERROR event `MOTION_FAULT ch<n> NONE -> REDETECT_TIMEOUT`.
   If it does not latch, that is a procedure failure: the filament probably left
   during the *pull* phase, which ends cleanly. Withdraw during redetect (yellow
   LED), not during the pull.
3. Re-insert filament to the switch.
4. **Without power cycling**, command a load of that slot from the printer.

| observed | verdict |
|---|---|
| NOTICE `MOTION_FAULT ... -> NONE` + gear drives + load completes | **fix confirmed** |
| NOTICE fires but gear still doesn't drive | release worked; a *different* veto is active — check `g_on_use_jam_latch` (needs pull > 85 %) and ks |
| No clear event, no drive, STATUS stuck at `send_out` | **fix disconfirmed** — capture events + snapshot and stop |
| Fault never latched at step 2 | procedure failure — re-run |

Two things that cost time in the passing run. The redetect phase is the
**yellow** LED (`MC_STU_RGB_set_latch(i, 0xFF, 0xFF, 0x00)` in the
`filament_redetect` case) and the timeout is 10 s from when it starts *driving*,
not from entry. And while the fault is latched the motor will not turn for a
hand insertion either — `pwm=0` with the strand sitting on the key is the veto
working, not a second fault. On this board a hand-fed strand parks ch0 at
`both` (~1883 mV), which reads to the eye like "the inner position".

### Test D — the livelock fix: autoload must give up after three aborts

The repro is what happened by accident on 2026-08-08: **insert filament into a
slot by hand with the printer idle and leave it.** Autoload starts uncommanded,
Stage2 pushes into a merger nothing is pulling from, and the buffer fills.

Watch with `autoload_watch.py` (§8) — a 1 Hz poll will see none of this.

| observed | verdict |
|---|---|
| exactly **three** `BUFFER_ABORT` teardowns, then the channel goes red and the motor stops | **fix confirmed** — `dm_fail_latch` set on the third |
| aborts keep coming past three, ~25 s apart, `dmfail` never set | **fix disconfirmed** — the budget is being refunded somewhere |
| fewer than three aborts then it stops | something else ended the insertion; check `ks` did not read `none` mid-test |

Then confirm recovery: withdraw the filament until the key reads `none`, hold
it, and re-insert. The channel must get a fresh three attempts — a latch that
survives withdrawal would be a new stale-latch bug in the other direction.

### Test C — filament runout (designed, not yet run)

The three-state merger latch exists for exactly this event, and TAIL is on the
wire as **channel-flags bit 6** (`docs/bmcu_wire_layout.json:69`). All three
merger commits (`f47d2aa`, `15bb6ce`, `bd2e2e1`) are ancestors of `6f2a86f`, so
the fix is in the running build.

**A print job is not required, but a completed printer-commanded load is.**
`ams_state_set_loaded(ch)` is called only at `bambu_bus_ams.cpp:308`
(`before_on_use`/`on_use`) and `:378` (`stop_on_use`). `send_out` does **not**
acquire — its `ams_state_preempt(ch)` call fires only when someone already owns
the merger. So the filament must reach the extruder for the printer to send
`before_on_use`, or the merger is never held and there is nothing to demote.

| # | expected | meaning |
|---|---|---|
| 1 | `loaded=1 tail=0`, ks non-zero | loaded normally |
| 2 | ks → `none`, `loaded=1 tail=0` | tail passed the switch; 1500 ms timing |
| 3 | **`loaded=1 tail=1`** | LOADED → TAIL, the fix under test |
| 4 | retract accepted (`before_pull_back`) | `allow_stop` guard at `:395` |
| 5 | `loaded=0 tail=0` | merger released |

If step 3 shows `loaded=0` instead of `tail=1`, the fix is not working — that
is the old defect that made runout switchover impossible. A short test filament
that reaches the extruder while its tail clears the online key makes step 2
happen by itself; otherwise withdraw by hand past the switch and hold >1.5 s.

TAIL has deliberately **no timeout** (`Motion_control.cpp:382-390`), so it
staying held while the printer waits for a human is correct, not a hang.

## 6. Known problems and suspected causes

Ordered by how settled they are.

1. **Autoload Stage2 livelock** — **fixed and confirmed on hardware
   2026-08-08** (§2.1, §5 Test D). Fork-local. Closed.
2. **Send-out veto deadlock** — cause confirmed 2026-08-07, **fix confirmed on
   hardware 2026-08-08** against the incident's own `PULL_NO_PROGRESS` (§4).
   Fork-local. Closed.
3. **Stage1 teardown has no debounce; `KS_EMPTY` is dead code** — §2.2, §2.3.
   Neither blocked any channel, so priority is low, but both are real.

4. **`S2_FAIL_RETRACT` is unreachable** — §2.4. The recovery retract after a
   three-strike autoload failure has never executed; the buffer stays stuffed
   until someone withdraws the filament.
5. **Printer long-frames flood the event ring** — §3. Identity still unknown.
   Promoted from "no harm attributable" to "actively destroys diagnostics".
6. **Per-loader lever geometry varies more than either document assumed** — ch0
   parks at `both` (the *upstream* `dm_loaded` premise) while ch3 parked at
   `inner` (what this fork recorded as the norm), on the same board. Any
   `dm_loaded` redesign must assume both patterns coexist.
7. **Loaded-latch (merger mutex) release design** — `src/ams_merger_policy.h`,
   `docs/BMCU_LOADED_LATCH_DESIGN.md`. The TAIL work addressed the runout case
   but rows 3 and 5 are deliberately deferred: commanded retracts still release
   at command time, not completion, so the merger is occupied-but-unowned during
   every retract. Untested on hardware (§5 Test C).
8. **Motion-fault latch may fire in normal situations** — *suspected*, zero
   observed latch sequences. Catch one via the field=8 ERROR event before
   redesigning the latch condition.
9. **Path resistance can exceed what the BMCU is able to push — not urgent, but
   it will recur.** A print started 2026-08-08 with a *different filament type*
   on slot 3 failed to load, and the account is worth keeping because the
   firmware was innocent at every step and looked guilty at several.

   What happened: slot 1's unload latched `PULL_NO_PROGRESS` and left its
   strand short of home; slot 3's load then pushed into the shared path and
   went nowhere. The buffer filled to **100 %**, autoload spent its three
   strikes on `BUFFER_ABORT` and parked red (§2.1 working as intended, and
   §2.4 leaving the buffer stuffed), and the printer retried `send_out` for
   half an hour without progress. The operator finally **pushed the strand into
   the extruder by hand, with considerable force, and it went**. The load then
   completed normally: `LOADED=True`, buffer settled to 54 %.

   That last fact is the diagnosis. **The buffer decouples the motor from the
   filament tip**, so when path resistance is high the push turns into buckling
   inside the buffer instead of travel at the tip — which is exactly what
   `BUFFER_ABORT` measures. A human pushing at the loader exit bypasses the
   buffer and can apply force the BMCU structurally cannot. No firmware change
   makes the BMCU stronger here; `DM_AUTO_PWM_PUSH` is not the limit, the
   buffer is.

   **The non-urgent check:** find the resistance before the next filament
   change on that spool. Candidates, in the order they are worth eliminating —
   the filament itself (different type: diameter, surface finish, softness),
   the PTFE path (tight bend, scored bore, swarf from the earlier jam), and the
   merger entrance (chamfer or concentricity, structurally the most likely
   place for it). Ask the operator where the force was needed; that localises
   it faster than any telemetry can.

   Two loose ends from the same incident. ch0 still carries a latched
   `PULL_NO_PROGRESS` — harmless, and the next slot-1 load releases it by the
   §2 fix. And **the fix was confirmed in the field, unprompted**: ch2 latched
   `PULL_NO_PROGRESS` at 06:01:24 and was released by the printer's own
   `send_out` retry, which before 2026-08-08 would have deadlocked until a
   power cycle. Evidence `2026-08-08-autoload-livelock/`.

10. **Enclosure warp pinches the gear** — bench board slots 1 and 3,
   mechanically confirmed; fix is a reprint with clearance. Printer-side board
   shows no sign of it.

## 7. Observation points

**The two bit-4s.** Do not conflate them:

- **STATUS channel-flags bit 4 = `dm_fail_latch`** (autoload fail latch).
  STATUS carries **no motion_fault bit at all**.
- **Snapshot channel-record flags bit 4 = `motion_fault != NONE`**
  (`bmcu_link.cpp:693`), code itself in `data[14]`, with the STATUS flags byte
  shifted whole into bits 8–15 of the same word.

Channel-flags byte, authoritative in `docs/bmcu_wire_layout.json`: ks 0–1,
low 2, jam 3, dm_fail 4, **loaded 5, tail 6**.

| signal | where | notes |
|---|---|---|
| raw online-key mV + per-channel `none_thr` | snapshot record type **14** (`FULL_RECORD_DM_KEY`) | four u16 key_mv then four u16 thr_mv, little-endian. The single most useful record for anything switch-related |
| merger ownership / runout | channel-flags bits 5 and 6 | live in STATUS, no refresh needed |
| autoload teardown + excursion width | events `dm_teardown` (`held_ms`) | **poll at ~10 Hz** — the ring holds ~2 s (§3) |
| motion-fault latch/clear, live | events `state_change` field=8 | latch = ERROR, clear = NOTICE, source=safety |
| motion-fault code (1/2/3) + `motor_pwm` | `snapshot.bin?refresh=1`, channel record | refresh lands on a *following* request; compare `hw_tick32` |
| online mask changes | events `state_change` field=3 | |

Caveats that have each cost real time: **`hw_tick32` wraps every 238.6 s** at
18 MHz, and a wrap reads exactly like a reboot — check `boot_session` before
believing one. Wire `ks` is not always the switch: Buffer Gesture Load
substitutes a fake `outer` for a real `none` while armed
(`Motion_control.cpp` ~`:724`), which is why manual gesture-driven feeding works
on a loader where autoload cannot. `tools/bmcu_live_watch.py` **starts polling
at import — never import it**. Snapshot endpoints keep serving the last bytes
after a link dies (decode the 0xF0 link record first: 2 = online, 3 = stale),
and a single snapshot is not evidence of a steady state.

Link-record decoder:

```
curl -s http://192.168.1.73/api/snapshot.bin -o snap.bin
python -c "b=open('snap.bin','rb').read(); import struct; \
print(next((hex(struct.unpack_from('>H',b,o+16+16)[0]), hex(struct.unpack_from('>I',b,o+16+20)[0])) \
for o in range(0,len(b)-16,1) if b[o:o+4]==b'BSNP' and b[o+6]==0xF0))"
```

## 8. Resources

| path | what |
|---|---|
| `..\diagnostics\2026-08-08-autoload-livelock\` | the livelock log, both passing test logs, **and all four watcher tools** |
| `..\diagnostics\2026-08-08-autoload-livelock\testD-fix-confirmed-0519.log` | Test D pass: three aborts then latch, withdrawal restores the budget |
| `..\diagnostics\2026-08-08-autoload-livelock\testB-pullfault-confirmed-0535.log` | Test B pass: `PULL_NO_PROGRESS` latched then released, no reboot |
| `..\diagnostics\2026-08-08-autoload-livelock\load-failure-different-filament-0555.log` | §6.9: buffer at 100 %, the retry loop, and ch2's fault released in the field |
| `..\diagnostics\2026-08-08-autoload-livelock\autoload_watch.py` | ~10 Hz teardown catcher; filters the printer-frame flood |
| `..\diagnostics\2026-08-08-autoload-livelock\band_sweep.py` | raw key-mV trajectory; answers "can this loader reach `both`" |
| `..\diagnostics\2026-08-08-autoload-livelock\runout_watch.py` | loaded/tail/ks/motion for §5 Test C |
| `..\diagnostics\2026-08-08-autoload-livelock\cal_watch.py` | recalibration watcher; pass = all four thr == 600 |
| `..\diagnostics\2026-08-07-exchange-failure\ch3-finger-press-1813.log` | ch3 electronics exonerated: all four bands by finger |
| `..\diagnostics\2026-08-07-exchange-failure\band-sweep-slot4-slot1-1758.log` | ch0 parks at `both`, ch3 topped out at `inner` |
| `..\diagnostics\2026-08-07-exchange-failure\ch3-autoload-teardown-1734.log` | first `dm_teardown` captured on this board |
| `..\diagnostics\2026-08-07-exchange-failure\recalibration-1922.log` | all four thr back to 600; ch3 holding `both` for 5 min |
| `..\diagnostics\2026-08-07-exchange-failure\INCIDENT.md` | the 2026-08-07 pull-fault incident and its four questions |
| `..\diagnostics\2026-08-07-bench\` | band survey reference values (dmkey build, COM6) |
| project memory `bmcu-autoload-s2-livelock` | §2.1 in memory form |
| project memory `bmcu-autoload-s1-global-clear` | §2.2 / §2.3 plus the ch3 story |
| project memory `bmcu-ch2-none-thr-poisoned` | §1 ch2, and how to spot the pattern again |

Decoding references: `web/src/api/snapshot.ts` (BSNP; header big-endian, body
little-endian), `web/src/api/layout.ts` (offsets),
`docs/bmcu_wire_layout.json` (channel-flag bits, authoritative),
`pico/bmcu_link.py` `_handle_snapshot` / `_decode_event`.

## 9. Carried forward, compressed

Established earlier, evidence in `git show 6f2a86f:handover.md` and
`..\diagnostics\2026-08-04-bench\`:

- Switch band decode is correct; all four voltage bands sit mid-band with
  ~150 mV margin on every loader measured. The "voltage falls short of the
  band" theory for issue #16 is dead — and issue #16 question 2 is now
  answered outright by §1's ch2 finding.
- A channel whose motor polarity never learned is **completely dead, silently**.
  `polarity_valid` is on the wire and was True on all channels throughout.
- A momentary `ks == 0` used to erase `dm_fail_latch` (fixed `c15193c`) and
  still tears the autoload machine down — now measured, §2.2.
- The ADC chain box-cars ~4.8 ms, so sub-ms contact bounce cannot reach
  `dm_key_to_state`; observed excursions are mechanical lever float.
- Do not re-adopt the five retracted hypotheses in the old handover
  (voltage-ceiling, manual_empty_pull-swallows, gate-never-rearmed,
  chatter-survives-reassembly, bounce-justifies-hold).

Still open and easy to lose: ams-a slot 4 "swallows a few cm, `ks` never moves"
(may well have been the ch3 lever fault — recheck before treating as open);
whether AHUB mode is alive; the Pico's accumulated journal, **now ~40 MB** and
still never cleaned up; and the Bambuddy colour-hold edit (`dj-oyu/bambuddy#5`)
was never confirmed to have released the held jobs.

## 10. Addendum, 2026-08-08 afternoon — the panel, the baseline, and CI

Bridge-side work only. No firmware was flashed, no BMCU was reset, and the
printer was mid-print throughout; everything below was established over HTTP or
by deploying the Pico, neither of which touches the BMCU.

### 10.1 An OLED status ticker on the Pico bridge

`pico/oled_ticker.py` + `pico/oled_ssd1306.py`, commit `11b38df`, running on
`bmcu-monitor-a`. SSD1306 128x64 on **I2C1, GPIO6 (physical 9) SDA / GPIO7
(physical 10) SCL**, 3V3 from physical pin 36. Enabled by default; a panel that
does not answer costs one `oled` warning at boot and nothing at run time.

**The wiring trap that cost the first hour:** the panel first went to *physical*
pins 6 and 7, which are **GP4/GP5 — `bmcu-b`'s UART1 in the default link map**.
The module was fine and answered at 0x3C the whole time; main.py had simply
claimed the pins as a UART first. Symptom was `[Errno 5] EIO` at init with both
lines reading high against an internal pull-down (the module's own pull-ups).
An I2C scan on GP4/GP5 is what proved the module innocent.

Row format, `1*#oK2U 85LJ`: slot, `*` merger owner (`T` once the tail passed the
online key), `#` inserted, `o` at the online key, `K2` decoded ks (`K?` when the
firmware predates the flags byte), motion char, pull %, then latches `L`/`J`/`D`
low-pull/jam/DM-autoload and `F` motion fault. The top row is
`A? -56 U+`: per-link state (`+` online, `~` resyncing, `?` stale, `!`
incompatible, `x` offline), RSSI, and the Bambuddy uplink. The bottom row
scrolls decoded events — `dm_teardown` with its `held_ms` included, which makes
the panel the only ~10 Hz-resolution view that needs no polling script.

Two properties worth not breaking. It writes **one 128-byte page per main-loop
iteration** (~3 ms); a whole frame would stall the UART drain for ~23 ms. And
it allocates nothing in the steady state, because MicroPython has no reference
counting and main.py collects at most once a minute — the body is redrawn only
when an integer digest of the page changes. `pico/README.md` documents the
digest, the 24-bit mask it needs on a 31-bit-small-int port, and the one case
the digest does not help (during a load, `pull_pct` moves every second, so the
body really does redraw at `OLED_REFRESH_MS` and costs ~2 KB each time).

### 10.2 The link has no baseline, and every snapshot procedure depends on one

Measured 2026-08-08 afternoon, from the synthetic 0xF0 link record:

```
state_code=3 (stale)   channels_present=0     snapshot_age_ms=65535 (never)
boot_session=0         tick_hz=0              variant=0xffff  build=0x00000000
```

`boot_session=0` is the diagnosis: **the Pico has never seen a HELLO**, because
it rebooted into a BMCU that was already running. `link_state` only reaches
`online` when a full snapshot completes (`bmcu_link.py:952`), and
`GET_FULL_STATUS` is not being answered, so it retries three times and settles
back to `stale` (`:545`).

**STATUS is unaffected and correct.** Decoded live off the wire that afternoon:
`current_slot=2`, `inserted_mask=0b1111`, `online_mask=0b0101`,
`motion=[0,0,2,0]`, `pull=[45,53,52,48]`, `pressure=2621`, flags
`[ks=2] [clear] [ks=1, loaded] [clear]` — internally consistent, the loaded
channel being the one the printer selected, in `on_use`, with both switches
closed. The panel rows matched it.

What is unavailable until the BMCU reboots: **`FULL_RECORD_DM_KEY` (the
key_mv/threshold record §7 calls the most useful record for anything
switch-related), the motion-fault code, `motor_pwm`, and the firmware
identity.** Test C and any `?refresh=1` procedure need those. A BMCU power
cycle sends HELLO and restores them; it was not done because a print was
running.

Also worth knowing: `stale` makes `soft_reset_guard_error` refuse soft resets,
so the link being down fails safe rather than open.

### 10.3 CI has been red since 2026-08-03 for a toolchain reason

`host-tests` failed on every push from 2026-08-03 while `web-ui` passed.
`ci/test_web_ui_build.py::test_staged_page_matches_the_web_sources` rebuilds
the page through npm and guarded on `shutil.which("npm")` — but a runner always
has npm and never has the installed dependencies, so `npm run build` exited 127
(`vite: not found`) and the test reported a missing toolchain as a drifted
artifact. Fixed in `1f9d41d` by installing the web dependencies in that job
(the suite refuses to skip, so the toolchain belongs there) and by also
guarding on `web/node_modules`, so a developer without `npm ci` now gets a skip
that says so. **No test was ever actually failing on content** — the 235/247/288
counts quoted in this document were green locally throughout.

### 10.4 Bridge heap, measured

`heap_min_free` bottomed at **5,088 bytes** (free 58.7 KB, GC every ~60 s at
26.7 ms, zero exceptions, 307 s uptime). The same ~5 KB low-water appeared at a
similar uptime on the previous boot, which points at the HTTP endpoints rather
than the panel — `/api/snapshot.bin` assembles 17 records in one pass, and the
panel's steady-state allocation is nil. **Not confirmed.** The A/B that settles
it is `OLED_ENABLED = False` for five minutes against five minutes with it on,
with no HTTP requests during either window; it was deferred to after the print.

### 10.5 Open, in order

1. Reboot the BMCU after the print, confirm `A~` then `A+`, and check whether
   the snapshot completes — if it does not even after a HELLO, §10.2 becomes a
   firmware question rather than a startup-order one.
2. Run the heap A/B in §10.4.
3. The `.local` name resolved fine from both `curl` and `urllib` this session,
   which is not what §7's IPv6 warning predicts. One session is not enough to
   retract it; if it keeps working, drop the warning.
