# Feed resistance: jams, open outlets and gear slip

Two failures of the same measurement. The BMCU drives its motor forward and
the AS5600 measures how far the strand moves; the buffer tells whether
anything downstream pushes back. A **jam** is drive that produces no travel.
An **open outlet** (a PTFE tube off the filament hub, or a load the extruder
never took) is travel that nothing takes. They sit in opposite corners of the
same plane, and they must be told apart without one triggering the other.

Status:

| part | state |
|---|---|
| jam latch release (§1.2) | implemented, `346783f`, sim-verified, not yet flashed |
| jam detection by resistance (§1.3) | design, not implemented |
| open outlet guard (§2) | implemented, `4b552c9`, sim-verified, not checked on hardware |
| gear slip (§3) | implemented, sim-verified, not checked on hardware |
| specs | `spec/bmcu_merger.qnt` (latch release), `spec/strand_zone.qnt` module `outlet_guard` (guard + jam interaction) |

## 1. Jam

### 1.1 The event (AMS-A ch3, 2026-11-27)

`sim/recordings/2026-11-27_ams_a_ch3_anchored_spool_jam.log`. The spool was
clamped too tight and its end held at the core during a print. The extruder
stretched the buffer (`p 8`, then `p 2`), LOW+JAM latched (`[LJOD]`), and the
printer stopped the channel and unloaded it, which is the correct response.
Once the snag was cleared the printer reloaded, and every reload failed: the
printer looped on its cutter for ten minutes (02:31-02:41) until the latch
happened to clear.

Reproduced in the simulator by `AnchoredSpoolJam` in
`sim/tests/test_firmware_sensors.py` (scenario
`sim/scenarios/anchored_spool_jam_ch3.json`): the pre-fix build fails the same
three ways the device did; the fixed build passes. Not reproduced: the DM_FAIL
latch seen at 02:32:03.

### 1.2 Latch release (implemented, `346783f`)

`g_on_use_low_latch` / `g_on_use_jam_latch` stop the motor in on_use and make
every reply carry pressure `0xF06F`. Their job ends when the printer has acted
on them. Three defects in `src/Motion_control.cpp` kept them past that point:

1. **before_pull_back did not clear them.** The latch survived into the
   printer's unload, and a latched pull_back is cut to 100 mm
   (`target_counts = distance_m_to_counts(0.100f)`), which left the strand in
   the path. Now `before_pull_back` on the channel, on the transition into
   `filament_before_pull_back`, clears `g_on_use_low_latch`,
   `g_on_use_jam_latch` and `g_on_use_hi_pwm_us`.
2. **The pull_back sequencer held the channel.** `filament_pulling_back` in
   `motor_motion_filamnet_pull_back_to_online_key` ran its full target and
   skipped the dispatch, so a send_out that arrived mid-retract went
   unanswered until the 0.6 m had run. Now, when the printer has this channel
   selected (`now_filament_num == i`) and commands `send_out`,
   `before_on_use` or `on_use`, the sequencer stops the motor, resets its
   pull state and returns the channel to `filament_idle`; the dispatch handles
   the command in the same pass. Idle and session frames do not end it: after
   a pull_back command the printer expects the retract to finish.
3. **send_out waited for pull > 85 %.** A latched send_out was vetoed (motor
   braked) unless the buffer read above 85 %. A print sits near 55 %, and a
   hand push cannot get past a braked gear, so only pinching the buffer
   itself during the send_out window opened it. Now send_out clears the
   latches unconditionally: the printer deciding to feed again is the
   confirmation.

A jam that is still there latches again in the next on_use. The key reading
empty (the strand withdrawn) still clears the latch, as before.

Invariants (`spec/bmcu_merger.qnt`, in `safety`):

- `assistNotJammed`: the pull the printer prepares with before_pull_back is
  never the jam-shortened one. A 0xFF pull reached without before_pull_back
  can still be (`witnessNoJamPull` keeps that reachable).
- `sendNotJammed`: an accepted send is not held back by a latch.
- `jamEscapable`: wherever a latched channel is, send_out is accepted there,
  or the phase moves on by itself (a pull or redetect ends, an unload gesture
  settles) to one where it is. So a send_out or on_use cycle from the printer
  can never be blocked for good by the latch.
- `anchoredSpoolJamTest` replays the event; `jamPullThenSendTest` covers the
  short pull.

### 1.3 Detection by resistance (design)

Today a jam is "buffer below 40 % for `BMCU_ON_USE_JAM_HOLD_MS`"
(`on_use_jam_policy.h`), plus a 20 s high-PWM timer that sets LOW without JAM.
Both read the buffer, which measures the extruder's demand, not the strand's
resistance. A stretched buffer is also what the printer produces whenever it
pulls faster than the BMCU feeds.

The design judges resistance directly: compare the motor's forward drive
(logical PWM) with the travel the AS5600 measures, against the relation
learned for this channel in normal feeding.

- **Learn.** Per channel, during on_use with the buffer in its normal band,
  keep an EWMA of travel per unit of drive above the motor's dead band
  (`travel_rate ≈ k · (pwm − pwm0)`). Seed `k` from a build default; hold the
  learned value in RAM (persisting it is a later choice).
- **Judge.** Over a window (order of 300 ms, scaled so that it covers a few
  mm of expected travel), a jam is drive above the dead band with measured
  travel below a fraction (say ¼) of what `k` predicts, sustained over
  `JAM_N` consecutive windows. Zero drive is never evidence: a controller
  that is not pushing proves nothing.
- **Latch and release** as in §1.2: stop forward drive, raise `0xF06F`, clear
  on before_pull_back, send_out or key empty.
- **No encoder, no verdict.** With the AS5600 bad the detector does not
  evaluate.
- The buffer reading stays as a supplementary signal for the extruder side
  only (demand), not as the jam criterion.

Open: the thresholds (dead band, fraction, window) need the recordings of
normal on_use and of the 11-27 event fitted; the 20 s high-PWM LOW latch
should become a special case of this rule rather than a second one.

## 2. Open outlet (implemented, `4b552c9`)

Code: `src/outlet_guard_policy.h` (header-only, host-tested by
`ci/outlet_guard_policy_test.cpp`), wired in `src/Motion_control.cpp`
(`outlet_sample`, `Motion_control_set_PWM`). Sim: `OpenOutlet` in
`sim/tests/test_firmware_sensors.py`, about 50 mm out of the gap before the
trip. Not yet checked on hardware.

### 2.1 The failure

The PTFE tube can come off the filament hub by accident. The BMCU does not
know: it keeps feeding as if loaded, and bare filament comes out of the gap.
A load the extruder never takes looks the same from the firmware's side:
forward travel that nothing takes. The guard does not tell the two apart.

### 2.2 Why not the buffer

The first version of this design (`31fb2cd`) took back-pressure from the
buffer leaving a band around its rest reading. The data says no: AMS-B ch3,
printing on 2026-11-27, read PWM ~600, encoder 1.1 mm/s and pull 51 %
(FULL_STATUS), against 50 % for free travel. A printing strand does not press
the buffer, so in on_use the band cannot mean contact.

What does tell them apart is speed. A printing extruder takes at most a few
mm/s (an A1 mini hotend tops out near 25-28 mm³/s, about 11 mm/s of 1.75 mm
filament), and a strand cannot travel faster than that for long unless
something other than the extruder takes it: the buffer filling (bounded by
its stroke) or an open outlet.

### 2.3 The count

Per channel, `excess`: forward travel in excess of what the printer could
have consumed.

- Each AS5600 sample adds its forward travel (negative for a retract).
- Outside send_out, and only while not latched, it is drained by the
  consumption ceiling × dt (`BMCU_OUTLET_CONSUME_MM_S`, 12 mm/s). It never
  goes below zero.
- In send_out nothing drains: the strand is not in the extruder yet.
- **Reset by back-pressure** while unlatched, either of:
  - the buffer pressed, pull ≥ 55 % (a coupled strand pushed);
  - the buffer pulled, pull < 47 % (the extruder pulling a coupled strand).

  Latched, the buffer is ignored (§2.5).

  In on_use the drain, not the buffer, is what accounts for a printing
  strand.
- **Reset by leaving send_out for on_use**, directly or through retracts
  (count and latch both). In the recordings the printer goes from send_out (p 50) to
  before_on_use (p 53) before the buffer presses, so judging the budget at
  that mode change, as the first design did, would have stopped every good
  load. The printer moves on only once its own sensor has the filament, so
  that is the arrival. A send that ends some other way (idle) is the
  printer's to retry, with a new budget.
- **Kept through a retract**: send_out → pull keeps the count, and the pull
  winds it back. If the printer then goes on to on_use, the count starts
  over as on a direct arrival: a send's whole-path count carried into on_use
  tripped a printing strand (Quint, `noFalseTrip`).
- **Kept through every other command.** on_use ↔ pull cycles over an open
  outlet buy no new budget (Quint, `spillBounded`, found when every mode
  change reset the count).
- The mode is the motor's (`outlet_sample`): send, pull, and every other
  motion that can push (on_use, before_on_use, the idle feed, DM autoload).
- **No encoder, no verdict.** With the AS5600 bad the count is held at zero
  and nothing trips; a latch already set stays until contact or key empty.
  The time × commanded-speed fallback of the first design is not
  implemented: PWM is not a speed command.

### 2.4 The budget

| mode | budget | macro |
|---|---|---|
| send_out | `BMCU_KEY_TO_HEAD_MM` (720) + 150 mm | `BMCU_OUTLET_SEND_MARGIN_MM` |
| any other forward motion | 40 mm (buffer stroke + margin) | `BMCU_OUTLET_USE_MM` |

A send starts with the strand at or past the key switch, so the
switch-to-head distance, not `AMS_RETRACT_LEN`, bounds the free stretch.
The whole guard is `BMCU_OUTLET_GUARD` (default on); the ceiling is
`BMCU_OUTLET_CONSUME_MM_S`. Thresholds in `kOutletConfig`: pressed 55 %,
pulled 47 %.

On the night of 2026-11-27 the loads took 34, 35, 41 and 41 s from send_out
to the tip arriving.

### 2.5 Trip and hold

A detached tube does not come back by itself, so the guard stops and tells
the printer; nothing resumes on its own. Out of the normal range is a stop.

- **Trip:** `excess > budget(mode)`. Latch per channel. While latched,
  `Motion_control_set_PWM` turns every logical push (logical PWM < 0) on that
  channel into a brake, whatever path asked for it (the pressure controller,
  DM autoload, the idle feed). Retracts pass.
- **Raised:** in on_use `outlet_sample` sets the jam (`g_on_use_low_latch`,
  `g_on_use_jam_latch`, `0xF06F`), so the printer stops the channel and
  unloads.
- **Hold:** latched, nothing pushes, `excess` is not drained, and no buffer
  reading releases it, tension included.
- **Release:** a retract that winds the count back to zero (the printer's
  unload; it counts whatever the buffer reads), the key reading empty, or a
  send ending in on_use (§2.3).

The first version resumed on tension, assuming the extruder would draw the
tube back onto the hub once it had consumed the spill. That resume read as a
jam, so it disarmed the jam detector for up to 3 s, and a slipping gear
(§3) tripped the latch, was resumed at once by its own stretched buffer, and
ground on with the jam detector disarmed.

### 2.6 Interaction with jam detection

The jam detector is still the buffer one (below 40 % for
`BMCU_ON_USE_JAM_HOLD_MS`) plus the 20 s high-PWM timer; §1.3 is still a
design.

- **While latched** both are held off (the jam window and the high-PWM timer
  are reset as on key empty): the push is braked, so a low buffer and a long
  high PWM say nothing about a jam. The guard has raised the jam itself.
- **The causes are disjoint:** a jam is drive without travel, so it adds
  nothing to `excess`; an open outlet is travel with nothing taking it.

### 2.7 Spec (module `outlet_guard`)

`spec/strand_zone.qnt`, module `outlet_guard`, one channel: a transcription
of `outlet_guard::sample` and the on_use jam check, against a physical model
(strand position, spill out of the gap, buffer compression and stretch, a
consumption ceiling per sample). It is a separate module so the replayed
`strand_zone` traces do not change; nothing replays this one against the
header yet.

- `freeBounded`: once judged in its mode and unlatched, the count is within
  that mode's budget; it is always ≤ the largest budget + one sample.
- `spillBounded`: filament out of the gap since the count was last zero is
  bounded (travel outpaces the drain by feed − ceiling per sample).
- `noFalseTrip`: the guard trips only if, since the count was last zero,
  travel went uncoupled outside send_out or out of the gap. Printing at the
  consumption ceiling, and a send that reaches the extruder before the
  buffer presses, never trip.
- `grindBounded`, `slipNotOpen`: §3.
- `latchBrakes`, `faultRaised` (a latch in on_use has raised the jam once
  sampled), `jamSound` (a jam latches only on a held strand or an
  open-outlet trip), `jamReleasedByCommand` (send_out / pull release a jam).
- Witnesses kept reachable: trip, unwind, a latch still held with the tube
  drawn back on, a real jam, a send-path spill past the on_use budget, an
  unbounded spill with the encoder bad (no encoder, no verdict), a slip
  trip.
- Scenarios: `arrivalBeforePressTest`, `detachMidPrintTest`,
  `sendNotTakenTest` (trip, retry with a fresh budget, unwind),
  `jamIsNotOpenTest`.

```
quint test spec/strand_zone.qnt --main=outlet_guard
quint run  spec/strand_zone.qnt --main=outlet_guard --invariant=safety
```

`.github/workflows/quint.yml` runs the tests, `safety` and the witnesses
of this module too (since `1d8ddee`).

### 2.8 Not covered

- The guard does not know where the outlet came off; on the send path up to
  the whole budget (870 mm) can come out before it trips. That is the price
  of the one stretch where free travel is normal.
- A send the printer abandons and retries gets a fresh budget each time, so
  repeated retries into an open outlet each let out up to a budget unless a
  retract comes between them.
- With the AS5600 bad there is no guard at all.
- In send_out a trip only brakes: there is no error to raise there, so the
  printer's own load timeout has to notice the filament never arrived.
- Reseating the tube is a human step. The latch ends with the printer's
  unload, after which a fresh load has to be started.
- The thresholds come from one printing reading and the simulator; they are
  not yet checked on hardware.

## 3. Gear slip

### 3.1 The failure

The drive gear slips on a strand that is held (a spool clamped at its core,
a tangle). Left running it grinds the filament into dust that spreads inside
the BMCU.

The AS5600 sits on the drive gear's shaft (`sim/world.py`: "drive gear on
the AS5600 shaft"), so it measures the gear, not the strand. A slipping gear
reads as travel. "Drive without travel", the obvious slip test, cannot see
it, and §1.3 (resistance) cannot either: a slipping gear turns freely.

Before this, a slip in on_use looked like an open outlet: travel nothing
takes. It tripped the §2 latch, which then resumed on tension (§2.5): the
stretched buffer resumed it at once, and each resume disarmed the jam
detector. In the simulator the gear ground 617 mm and never stopped
(`406d143`).

### 3.2 The signature

The buffer says which. Real forward travel restores a deflected buffer
within its stroke:

- **pulled** (< 47 %): the strand is held upstream while the extruder pulls.
  Travel that reached the buffer would bring it back to rest.
- **pressed** (≥ 55 %): the strand is blocked downstream. Travel cannot go on
  compressing a buffer past its stroke.

So gear travel while the buffer reads pulled or pressed is counted on its
own (`slip`). Outside send_out it is drained at the consumption ceiling,
since the extruder can take that much with the buffer where it is. Past
`BMCU_OUTLET_SLIP_MM` (40 mm) it is a slip. The count resets whenever the
buffer is at rest, on a mode change and on key empty.

Quint found the pressed case. A send_out with the strand at the extruder
(pressed, below the 85 % hard stop) whose gear then slips was reset by the
pressed buffer on every sample, so the first version, which counted only a
pulled buffer, never tripped.

### 3.3 Trip and release

- A slip brakes every push and raises the on_use jam like an open outlet
  (§2.5), but it is never an open-outlet trip.
- The printer's next command (pull, send_out) ends it, as it ends the jam
  latch (§1.2); key empty does as well.

### 3.4 Checked

- Host: `ci/outlet_guard_policy_test.cpp` (slip trips and holds, a refill
  is not a slip, feed-limited printing is not a slip, a slip is not an open
  outlet, send_out ends it).
- Simulator: `GearSlip` in `sim/tests/test_firmware_sensors.py`, extruder at
  1 mm/s, 30 mm stroke, the gear slipping from t = 8 s:

  | image | ground | jam raised |
  |---|---|---|
  | `64b5c7c` (deployed) | 166 mm | 11.2 s |
  | `406d143` | 617 mm | never |
  | this | 55 mm | 9.4 s |

- Quint: `grindBounded`: gear travel into a held strand, net of the
  consumption ceiling, is bounded per fault and printer command. At most it
  covers three stretches: pressed (a slip count short of its trip), at rest
  (the open-outlet count), and pulled (a slip count to its trip).
  `slipNotOpen`: a slip never sets the open-outlet latch. The witness
  `witnessNoSlip` keeps a slip trip reachable.

### 3.5 Not covered

- A slip while the buffer reads at rest is only caught by the open-outlet
  count: in send_out that is the whole send budget (870 mm) before it trips.
- A gear that slips at under the consumption ceiling's duty (on/off drive,
  grinding no faster than the extruder could take filament) looks like
  normal feeding to these sensors. The spec counts grinding net of the
  ceiling for that reason.
- The 40 mm budget, the 47 / 55 % thresholds and the buffer's real stroke
  are from the simulator and one printing reading, not hardware.
