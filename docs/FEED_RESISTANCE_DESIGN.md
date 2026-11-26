# Feed resistance: jams and open outlets

Two failures of the same measurement. The BMCU drives its motor forward and
the AS5600 measures how far the strand moves; the buffer tells whether
anything downstream pushes back. A **jam** is drive that produces no travel.
An **open outlet** (a PTFE tube off the filament hub, or a load the extruder
never took) is travel that meets nothing. They sit in opposite corners of the
same plane, and they must be told apart without one triggering the other.

Status:

| part | state |
|---|---|
| jam latch release (§1.2) | implemented, `346783f`, sim-verified, not yet flashed |
| jam detection by resistance (§1.3) | design, not implemented |
| open outlet guard (§2) | design, not implemented |
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
pulls faster than the BMCU feeds, which includes every resume after the BMCU
stopped feeding on purpose (§2.5).

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
  evaluate (the open-outlet guard takes over its budget by time, §2.3).
- The buffer reading stays as a supplementary signal for the extruder side
  only (demand), not as the jam criterion.

Open: the thresholds (dead band, fraction, window) need the recordings of
normal on_use and of the 11-27 event fitted; the 20 s high-PWM LOW latch
should become a special case of this rule rather than a second one.

## 2. Open outlet (design)

### 2.1 The failure

The PTFE tube can come off the filament hub by accident. The BMCU does not
know: it keeps feeding as if loaded, and bare filament comes out of the open
end. The same thing, from the firmware's side, happens when a load is never
taken by the extruder: forward travel with nothing pushing back. The guard
does not try to tell the two apart.

### 2.2 Where free travel is normal

Exactly one stretch: the send path, from the channel's rest position to the
extruder. It has a known length, `AMS_RETRACT_LEN` (0.60 m in this build;
`BMCU_KEY_TO_HEAD_MM` in `src/Motion_control.cpp`, line ~532, derives the
zone head from it, `kZoneConfig.head`). On the night of 2026-11-27 the loads
took 34, 35, 41 and 41 s from send_out to the tip arriving.

Everywhere else (on_use, before_on_use's hold_load, the idle feed, the DM
Stage-2 re-push) a strand that is coupled to the extruder raises the buffer
within a few mm of push. Free travel there is a fault.

### 2.3 The count

Per channel, `free`: forward travel since the last back-pressure.

- Measured by the AS5600 forward delta. With the AS5600 bad, by time ×
  commanded speed, which is at least the real travel: the guard then trips
  early, never late.
- **Back-pressure** (contact) is the buffer leaving its free band around the
  channel's rest reading, either way: up (the BMCU pushing a coupled strand)
  or down (the extruder pulling one). On the send path the buffer sits in
  the band; that is what "free" means. The band is learned from the idle
  reading; the width needs fitting to recordings.
- Contact sets `free = 0`. Backward travel subtracts from it (floor 0): a
  retract pulls back what it fed, including filament that came out of an
  open outlet.
- Mode changes do not reset it. A repeated send_out must not buy another
  budget; only contact or a retract does.

### 2.4 The budget

| mode | budget |
|---|---|
| send_out | `AMS_RETRACT_LEN` + margin (margin order of 100-150 mm, fitted to the 34-41 s loads) |
| on_use, before_on_use, any other forward motion | a few tens of mm (order of 30-40 mm) |

The budget is judged on every sample and on every mode change. A
before_on_use that arrives with no contact since the send (the load was not
taken) trips at once, which is intended. A tie-in with the strand zone is
possible later (budget = `head − front` + margin when the front is exact), but
the plain path length is enough and does not depend on the estimate.

### 2.5 Trip, hold, resume

- **Trip:** `free > budget(mode)`. Latch `outlet_open[ch]`; stop forward
  drive on that channel. Retracts are still served. No human action is
  needed and none is asked for. Report it (a diag counter and a wire flag
  bit); whether to raise anything to the printer is open: `0xF06F` would
  start the printer's unload, which is not what a reseating tube needs.
- **Hold:** while latched nothing pushes, so `free` cannot grow (at most one
  sample past the budget).
- **Resume:** when the printer consumes, the filament in the tube draws the
  tube back onto the hub and the BMCU feels back-pressure (torque) again,
  here as tension: the extruder pulling a strand the BMCU is not feeding.
  That contact clears the latch and `free`, and feeding resumes.
- **Retract:** a retract that winds `free` back to 0 also clears it: the
  strand is back where the count started (the send-path case, where nothing
  is consumed and a human has to put the tube back).
- Key empty clears it, as it does every other per-strand latch.

### 2.6 Interaction with jam detection

- **The resume looks like a jam to the buffer.** When the guard has stopped
  the motor and the printer consumes, the buffer is stretched at the moment
  contact returns. Today's detector (below 40 % for 300 ms) would latch a jam
  right there, raise `0xF06F` and send the printer into its unload cycle. So:
  - with today's detector, the resume must reset `on_use_jam` and keep it
    disarmed until the buffer has come back above the low threshold once;
  - with the resistance detector (§1.3) the hazard is gone by construction:
    nothing is driven while stopped, and the first drive after the resume
    moves freely against a pulling extruder, which is the opposite of a jam.
- **The latches are disjoint.** A jam is drive without travel, so it never
  adds to `free`; an open outlet is travel without resistance, so it never
  looks like a jam. While either latch holds the motor, the other is not
  evaluated (the guard does not judge a mode change while a jam holds; the
  count is judged again after the printer's recovery clears the jam).
- With the AS5600 bad, a held strand makes the time-based count grow and the
  guard trips: the wrong reason, the right action (stop).

Spec (`spec/strand_zone.qnt`, module `outlet_guard`, one channel; separate
from `strand_zone` so the replayed zone traces do not change):

- `freeBounded`: unlatched in a forward mode, `free ≤ budget(mode)`; always
  `free ≤ max budget + 1`. "Free feed never grows past the budget."
- `spillCounted`: filament pushed out of the open outlet ≤ `free`, so the
  spill is bounded by the same budget.
- `latchesDisjoint`, `jamSound` (a jam latches only on a held strand, in
  particular not at the resume), `jamReleasedByCommand` (send_out / pull
  release the jam), `resumeOnContact` (contact always clears the guard).
- Witnesses kept reachable: the guard trips, it resumes, the buffer is
  stretched at the resume (where a buffer-based detector would misfire), a
  real jam still latches, the send-path spill exceeds the on_use budget.
- Scenarios: `detachMidPrintTest`, `notTakenTest`, `jamIsNotOpenTest`.

```
quint test spec/strand_zone.qnt --main=outlet_guard
quint run  spec/strand_zone.qnt --main=outlet_guard --invariant=safety
```

These are not yet in `.github/workflows/quint.yml`, which checks the default
module only.

### 2.7 Not covered

- The guard does not know where the outlet came off; on the send path up to
  the whole budget can come out before it trips. That is the price of the one
  stretch where free travel is normal.
- A tube that comes off and never reseats leaves the channel latched until a
  retract or a key-empty; the printer's own runout/clog handling then decides.
- Thresholds (band, margin, on_use budget) are placeholders until fitted to
  recordings.
