# Strand zone: where each channel's filament is, from travel

Replaces the one-bit "downstream" history of the first version of this change.
That bit was set on a motor-mode edge (entering send) and cleared on
"redetect found the key", and a design review found both keyed to the wrong
evidence:

- other motions push a parked strand past the merger without entering send:
  hold_load under before_on_use, the DM Stage-2 re-push, the idle feed;
- a jam-latched unload pulls 100 mm and still reaches redetect, which clears;
- after a full-length pull the key never went empty, so "found the key" is not
  evidence of anything;
- nothing clears the bit for a strand a human pulled out, or a tail piece the
  printer consumed, so it reports forever.

The physical fact is how far the strand has travelled, and the AS5600 measures
exactly that. So the firmware keeps an estimate of the strand's position and
derives the zone from it. The rule throughout is to **over-report rather than
under-report**: where the estimate cannot know, it assumes the strand is
further downstream.

## Geometry

Positions are counts of forward travel (towards the head), measured from the
inner online switch (`ks` 1 or 3; the only switch on single-switch builds).

| name | value (default) | meaning |
|---|---|---|
| `0` | | inner switch |
| `park` | `DM_AUTO_S2_TARGET_COUNTS` (120 mm) | merger entrance, where autoload parks |
| `down_set` | `park + 30 mm` | front beyond this: past the merger (the junction, E4) |
| `down_clear` | `park + 10 mm` | front below this: back upstream (hysteresis) |
| `head` | `park + AMS_RETRACT_LEN` | the nozzle; travel beyond it is filament consumed |
| `dir_band` | 1 mm | net travel that decides a direction |
| `consume` | 10 mm, or 0 | travel past `head`, while the printer feeds, that proves passage |

`head` is derived from the retract length because that setting is what a user
tunes so that an unload brings the tip back to the merger. That makes the
default circular (a nominal retract always lands on `park`), so it is a guess:
`-DBMCU_KEY_TO_HEAD_MM` gives the real distance, and only then is the passage
proof on (`consume` = 10 mm; `-DBMCU_ZONE_PROOF=1` accepts the guess).

## Estimate per channel

- `front`: position of the strand's downstream end. Saturates at `head`: once
  the tip is in the nozzle, more travel is filament being melted, not the tip
  moving. Valid once known (`front_known`); `exact` when it was measured from a
  switch edge rather than assumed.
- `tail_known`: the strand's upstream end has passed the switch going forward,
  so the spool ran out and what is left will leave the BMCU. A piece left past
  the switch, even short of the merger, is pushed into the tube by the next
  strand, so it counts as a **tail piece** from then on; once the next strand
  arrives behind it, it is a **remnant** until proven gone.
- `last_dir`: the direction of the last sustained motion. A travel integrator
  clamped to `±dir_band` flips it only after `2 × dir_band` of net travel, so a
  count of AS5600 jitter at rest never decides an edge.

Updated every AS5600 sample with the signed forward delta and the debounced
(50 ms) switch reading:

1. `front += delta`, clamped to `[-head, head]`.
2. Switch rising edge with the front at or behind the switch: the front
   arrived, `front = 0`, exact. With a tail known, a piece left past the switch
   becomes a remnant. With the front known past the switch: the end of a float;
   nothing moves.
3. Switch falling edge, last motion forward: the tail passed, `tail_known = 1`.
   Last motion backward and the front at or behind the switch: the front passed
   going back, `front = key_back` (0), exact. Otherwise a lever float: nothing
   changes.
4. AS5600 not good: nothing measured holds (`exact = 0`, no passage). If the
   motor drives forward the travel is unseen, so the front is put one
   hysteresis band past the merger. Unseen backward travel never clears.

## Zone

| zone | when | downstream? |
|---|---|---|
| `NONE` | no front known, switch empty | no |
| `UPSTREAM` | front known, short of the merger | no |
| `DOWNSTREAM` | front past `down_set` (hysteresis to `down_clear`), or owning the merger | yes |
| `TAIL` | a tail piece past the switch, or a remnant | yes |

The wire flag (BmcuChannelFlags bit 7) is `zone ∈ {DOWNSTREAM, TAIL}`.

## Clearing a strand the BMCU cannot see: proof by passage

The output tube is a single bore. If channel Y's strand has gone through the
nozzle, Y's strand fills the tube from the merger to the nozzle, so no other
strand and no remnant is anywhere in it.

Passage is accepted only when all of these hold:

- Y's front was measured from a switch edge (`exact`) and travelled, measured,
  to `head`;
- it then travelled `consume` further **while the printer feeds from Y** (Y owns
  the merger, in on_use). A push against a blocked junction can count travel
  too if the wheel slips (E1); the printer pulling filament through cannot;
- no backward or unmeasured sample in between.

The proof fires **once**, on that arrival, never again while the front stays at
the head. A front left at the head by an unmeasured pull would otherwise keep
clearing every other channel on every sample, and the zones flipping would
write flash at the save loop's rate.

When it fires, every other channel's estimate is pulled back to the junction:
`front = min(front, down_set)`, not exact, remnant cleared. Not to `park`: a
strand may sit anywhere in its own branch up to the junction, and pulling the
estimate behind the real front would under-report it later. A known tail piece
stays known: it is out of the tube but may still be in its own branch.

## Collision report

A send (or a load acquiring the merger) on channel X while another channel's
zone is downstream raises `DIAG_COUNTER_DOWNSTREAM_OCCUPIED` with
`(others_mask << 8) | X`. The report is latched per `(X, others)` pair and
re-armed when any zone changes, so a send edge that repeats every pass (bad
AS5600, bus error) reports once.

## Persistence

Zones are 2 bits per channel, and 4 channels fit the low byte of the existing
0xA7 record in the merger ring. A record is written only when it changes, a
few times per load/unload, and it is **written ahead**: a measured front
climbing into the hysteresis band (above `down_clear`) is stored as
`DOWNSTREAM` before it reads so. The write (which waits for bus quiet) then has
the band's 20 mm of travel to land before the strand can be past the merger. A
ring-page erase re-marks the record dirty, so it is rewritten at once.

On boot each switch is read only once it has read the same for 200 ms (the
ADC filter starts at 0); that first reading is taken as it is, never as an
edge. Then:

- `DOWNSTREAM`: `front = head`, not exact.
- `TAIL`: the channel is a remnant.
- `UPSTREAM` / `NONE` with a strand at the switch: `front = down_set`, the
  furthest an upstream strand can be.
- **No record** (first boot of this firmware, a downgrade round trip, a
  page erase that lost it) with a strand at the switch: `front = head`. The
  strand may be anywhere.
- The merger owner is forced to `DOWNSTREAM`, since owning the tube means
  using it.

## Assumptions

Each one fails in a direction; the ones that can under-report are marked.

- **E1.** The AS5600 measures filament travel: no slip between the measuring
  wheel and the strand. If the wheel can turn on a strand the extruder holds,
  a measured retract can be false and the zone clears early
  (**under-reports**). The consumption condition on the passage proof is there
  because of this. Not verified on hardware.
- **E2.** `head` is right. Too long: the proof needs more travel to fire (it may
  never fire; over-reports). Too short: the proof fires while a remnant is
  still in the tube, and a retract shorter than the real tube reads as upstream
  after every unload (**under-reports**). Hence no proof without a measured
  `BMCU_KEY_TO_HEAD_MM`.
- **E3.** The tube is a single bore with no bypass, so passage is proof.
- **E4.** The junction is at `down_set`. A junction further than that: a
  channel that parks or rests between them reads as downstream (over-reports).
  A junction closer: a strand past it but short of `down_set` reads as upstream
  (**under-reports**). The 50 ms debounce lag on a rising edge (a few mm) eats
  into the same margin.
- **A5.** A record is written before power can be lost. Written ahead for
  travel into the merger, so only a power loss within one save of a runout or a
  claim loses a zone change (**under-reports** that change).
- **A6.** A lost record loses any tail piece it held: with the switch empty
  there is nothing to find it by (**under-reports**).

## Not covered

- AS5600 bad and the strand moving without the motor (the extruder pulling, a
  hand pushing) is unseen forward travel that is not assumed. The owner is
  covered by the claim; other channels are not.
- Travel during the 200 ms settle after boot is not counted. Nothing moves
  before the printer talks.

## What this does not do

It is still observe-only: nothing gates on the zone. A future consumer, such as
accepting the printer's unload for a channel whose zone is downstream but
whose AMS motion is idle, must bear in mind that `DOWNSTREAM` often means
"position unknown": claim, restore and the unmeasured guess all put the front
at the head or just past the merger. Such a consumer must bound its pull by the
switch and by measured travel, never by the zone alone, and would want an
"assumed vs measured" bit on the wire.
