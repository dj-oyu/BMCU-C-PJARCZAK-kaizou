# Where should the tip end up? A review of key-driven transitions

2026-11-26, after AMS-B slot 3 over-retracted on every printer unload (fixed by
`src/pull_back_end_policy.h`). That bug was not a sensor misreading. The switch
reported correctly. The firmware's end condition assumed a geometry the loader
does not have. This document asks the same question of every other place where
an online-key reading (`MC_ONLINE_key_stu`, `ks`) starts or ends a motion:
**where is the tip when the motion stops, and can the BMCU drive it from
there?**

A motion that ends with the tip where nothing on the BMCU can move it again is
a hang in all but name. The motor may stop, the state may read idle, but only a
human hand gets the channel back.

## Geometry

From the spool side: **outer switch -> drive gear -> inner switch -> merger.**

| Tip position | Gear holds it | ks, lever stays pressed under the strand ("B") | ks, lever releases after the tip ("A") |
| --- | --- | --- | --- |
| past the inner switch | yes | 1 both | 3 inner |
| between gear and inner switch | yes | **2 outer** | **0 none** |
| between outer switch and gear | **no** | **2 outer** | 2 outer (under the lever) / 0 |
| upstream of the outer switch | no | 0 none | 0 none |

"B" is what AMS-B slot 3 does. "A" is the 2026-08-07 bench result on an ams-a
loader. Note that **today's live STATUS shows ks=1 (both) on every loaded
channel of both units**, ams-a included. So "B" may now describe every loader,
and the bench result may be out of date.

Two consequences hold whichever geometry applies:

1. **ks=2 is ambiguous on B.** "In the gear, just short of the inner switch"
   and "behind the gear, out of reach" read the same. The key alone cannot
   tell the firmware whether it can still drive the strand.
2. **ks=0 is ambiguous on A.** "In the gear, just short of the inner switch"
   and "gone" read the same, and every forward motion is gated on ks != 0.

So the only position that is both drivable and recognisable on both
geometries is **at the inner switch, ks 1 or 3**. Every retract that is meant
to leave the strand in the loader should be bounded so that it stops within
the gear-to-inner-switch distance after the inner switch releases. A key
reading cannot be the bound, because the readings past that point are the
ambiguous ones.

## Paths

Verdict key: OK, **HANG** (the BMCU can strand the tip where it cannot drive it
and no state recovers it), *needs hand* (stranding is the intended outcome of
a failure or a user gesture, and the operator is present).

| # | Path (Motion_control.cpp) | Ends on | Tip at the end, B | Tip at the end, A | Verdict |
| --- | --- | --- | --- | --- | --- |
| P1 | Printer pull_back sequencer, `motor_motion_filamnet_pull_back_to_online_key` | ks in {0,2} for 60 ms (since today), or travel target | in the gear, short of inner | in the gear; redetect pushes it back to inner | OK. **Travel-target exit** left: see below |
| P2 | Redetect after the pull back | key != 0, or 10 s -> `MOTION_FAULT_REDETECT_TIMEOUT` | ends at once (ks 2) | pushed back to the inner switch | OK. A strand pulled out by hand gets 10 s of push into nothing, then a fault |
| P3 | DM Stage 1: `ks==2` held 100 ms -> push until ks 1, 5 s timeout -> `S1_FAIL_RETRACT` 1.5 s pull | ks 1, or timeout | timeout leaves it where it was. The 1.5 s retract can pull a gripped tip behind the gear | same | *needs hand* (fail latch, red). The 2026-11-25 recording is this path: hand insertion stopped short of the gear |
| P4 | DM Stage 2 buffer abort -> `S2_RETRACT` until buffer <= 50.2 % or ks 2 (-> S1). At ks 0 the global ks==0 sweep ends the autoload before the state machine runs | buffer or key | ks 2 -> S1 push: OK | was: **IDLE with the tip in the gear, invisible**; send_out and autoload both need ks != 0 | **Fixed**: the sweep now hands S2_RETRACT at ks 0 to `DM_AUTO_RESEAT`, a push of at most 400 ms back to the inner switch (`pull_back_end::reseat_after_retract`) |
| P5 | DM Stage 2 out of retries -> `dm_fail_latch` set, state `S2_FAIL_RETRACT`, meant to go on to `S2_FAIL_EXTRA` (1.5 s at PWM 900) | - | **never runs**: while `dm_fail_latch` is set the autoload block drives 0 before the state switch is reached, so `S1_FAIL_RETRACT`, `S2_FAIL_RETRACT` and `S2_FAIL_EXTRA` are unreachable. The motor stops where the third abort left it, gripped, ks 1 | same | OK as it stands. The dead states would over-retract on B if they were ever made reachable |
| P6 | Auto-unload gesture: pull at 850 until the inner switch has read empty for 1.5 s, or 15 s | key, then time | 1.5 s past inner release: behind the gear, on the outer lever | was: `ks != 1` held from the start, so the unload stopped after 1.5 s. **Fixed**: the empty test is `!pull_back_end::inner_present(ks)` | *needs hand* by design: the user asked for the strand out |
| P7 | `manual_empty_pull`: ks 0 and buffer > 80 % -> pull at 700 while it holds | buffer | n/a (B reads 2) | pulls an invisible strand further back | *needs hand*, user gesture |
| P8 | Post-on_use push: idle, ks 0, within 10 s of on_use, buffer < 49 % -> push | time, buffer | n/a | OK | OK |
| P9 | send_out and every printer motion gated on `ks != 0` (`motor_motion_switch`, `Motion_control_run`) | - | OK | a tip in the gear but short of the inner switch can never be loaded by the printer | Makes P4/P5 on A a hang |
| P10 | DM Stage 1 gate: cleared only on ks 0 while idle | - | the strand body keeps ks >= 2, so after the first Stage 1 the gate stays set while any strand is in the loader | OK | OK, and it is what keeps Stage 1 from re-pushing an unloaded strand at ks 2 |
| P11 | Loaded latch -> TAIL on owner ks 0 sustained | - | tail passes outer first (3), then inner (0): OK | OK | OK |
| P12 | Strand zone key = ks 1 or 3 (`zone_sample`) | - | consistent with P1 | consistent | OK |

### P1, the travel-target exit

The pull back still ends when travel reaches `AMS_RETRACT_LEN` (600 mm). That
is right when the strand is long, but it is also the only exit when the inner
switch never releases. A switch stuck closed is a real case: ams-a ch1 reads
1033 mV, inner only, with nothing loaded, and its none threshold has been
raised to 1140 mV. When that happens the pull runs the full 600 mm, which can take the
tip out of the gear. Redetect then sees key != 0 and reports the unload as
finished.

Separately, **on AMS-B slot 3 the travel target did not end a 50 s pull**. At
60 mm/s the 600 mm target is about 10 s, and once the gear lost the strand it
spun freely. So the progress counted by `directed_progress_counts` was far
less than the gear's rotation. This is not explained yet. Until it is, the
travel target cannot be counted on as a backstop.

## What changed, and what is left

Done on 2026-11-26 (`src/pull_back_end_policy.h`, host-tested in
`ci/pull_back_end_policy_test.cpp`):

- **P1**: the printer pull back ends at inner release (ks 0 or 2), not only at
  none. Sim-tested in `sim/tests/test_firmware_sensors.py`.
- **P4**: an S2 retract that runs the key to none is followed by a reseat to
  the inner switch instead of ending the insertion.
- **P6**: auto-unload's empty test is "inner switch empty", so a loader that
  rests at inner only no longer stops after 1.5 s.

The first version of this document called P5 a hang. That was wrong: the
fail-retract states are unreachable (see the table).

Left:

1. **P1 travel target**: find out why travel accrued so slowly during the
   B-3 pull (encoder vs gear, or the speed loop holding the PWM down). Then
   consider bounding the travel after inner release separately from the
   600 mm total. A stuck-closed inner switch still runs the full 600 mm.
2. **P4 and P6 have no simulator scenario**. The simulated loader's outer lever
   stays pressed under the strand (B), so the A-geometry paths cannot be
   reproduced without a world parameter for lever release.
3. **P5 dead code**: either remove the fail-retract states or make them reachable
   with an end at inner release. Reachable as written, they would over-retract.
4. **Bench geometry**: re-check which lever behaviour the current loaders
   have. Live ks=1 on loaded ams-a channels says the 2026-08-07 "rest is
   inner" result no longer holds.
5. `docs/bmcu_wire_layout.json` names `dm_auto_state` values only up to 6.
   7 (`s2_fail_extra`) and 8 (`reseat`, the new reseat-timeout teardown) are
   unnamed. Adding them needs a layout revision bump and a schema rebuild.
