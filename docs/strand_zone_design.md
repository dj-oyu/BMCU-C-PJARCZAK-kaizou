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
derives the zone from it.

## Geometry

Positions are counts of forward travel (towards the head), measured from the
inner online switch (`ks` 1 or 3; the only switch on single-switch builds).

| name | value (default) | meaning |
|---|---|---|
| `0` | | inner switch |
| `park` | `DM_AUTO_S2_TARGET_COUNTS` (120 mm) | merger entrance, where autoload parks |
| `down_set` | `park + 30 mm` | front beyond this: past the merger |
| `down_clear` | `park + 10 mm` | front below this: back upstream (hysteresis) |
| `head` | `park + AMS_RETRACT_LEN` | the nozzle; travel beyond it is filament consumed |

`head` is derived from the retract length because that setting is what a user
tunes so that an unload brings the tip back to the merger. `-DBMCU_KEY_TO_HEAD_MM`
overrides it where the real geometry is known.

## Estimate per channel

- `front`: position of the strand's downstream end. Saturates at `head`: once
  the tip is in the nozzle, more travel is filament being melted, not the tip
  moving. Valid once the front has crossed the switch at least once
  (`front_known`).
- `tail_known`: the strand's upstream end has passed the switch going forward,
  so the spool ran out and what is left will leave the BMCU. Once the tail
  clears the drive gear nothing more is measured, so a tail piece past the
  merger is a **remnant** until proven gone.

Updated every AS5600 sample with the signed forward delta:

1. `front += delta`, clamped to `[-head, head]`.
2. Switch rising edge: the front is at the switch, so `front = 0`. If a tail
   piece was downstream, it becomes a remnant (a new strand behind an old tail).
3. Switch falling edge, last motion forward: the tail passed the switch, so
   `tail_known = 1`.
   Last motion backward: the front passed the switch going upstream, so
   `front = min(front, 0)`.
   No motion: a lever float; nothing changes.
4. AS5600 not good while the motor drives forward: the travel is unseen, so
   `front = max(front, down_set + 1)`. Unseen backward travel never clears.

## Zone

| zone | when | downstream? |
|---|---|---|
| `NONE` | no front known, switch empty | no |
| `UPSTREAM` | front known, below the band | no |
| `DOWNSTREAM` | front past `down_set` (hysteresis to `down_clear`) | yes |
| `TAIL` | tail passed the switch, front was past the merger, or remnant set | yes |

The wire flag (BmcuChannelFlags bit 7) is `zone ∈ {DOWNSTREAM, TAIL}`.

## Clearing a strand the BMCU cannot see: proof by passage

The output tube is a single bore. If channel Y's front has travelled to `head`,
Y's strand fills the tube from the merger to the nozzle, so no other strand and
no remnant is anywhere in it. Pushing a remnant ahead is fine too: Y reaching
the head means it went through the nozzle first.

So when Y's front reaches `head` by measured travel (a front restored there at
boot does not count), every other channel's estimate is pulled back to the
junction: `front = min(front, down_set)`, `tail_known = 0`, remnant cleared.
Not to `park`: a strand may sit anywhere in its own branch up to the junction,
and pulling the estimate behind the real front would under-report it later.
This is the only way a remnant or a hand-removed strand gets cleared, and it
happens on the next ordinary load of any channel.

## Collision report

A send (or a load acquiring the merger) on channel X while another channel's
zone is downstream raises `DIAG_COUNTER_DOWNSTREAM_OCCUPIED` with
`(others_mask << 8) | X`. The report is latched per `(X, others)` pair and
re-armed when any zone changes, so a send edge that repeats every pass (bad
AS5600, bus error) reports once.

## Persistence

Zones are 2 bits per channel, and 4 channels fit the low byte of the existing
0xA7 record in the merger ring. A record is written only when a zone changes,
which is a few times per load/unload, not per sample. On boot:

- `DOWNSTREAM`: `front = head`. The worst case is a needless retract later.
- `TAIL`: `tail_known = 1`, so the channel is treated as a remnant.
- `UPSTREAM` / `NONE`: `front = down_set` (the junction, the furthest an
  upstream strand can be) if the switch reads filament, else unknown.
- The merger owner is forced to `DOWNSTREAM`, since owning the tube means
  using it.

## Assumptions

- **E1.** The AS5600 measures filament travel, not just gear rotation: no slip
  between the measuring wheel and the strand. If the wheel can turn on a
  strand the extruder is holding, a measured retract can be false and the zone
  can clear early. Not verified on hardware.
- **E2.** `head` is right. A wrong value moves the clear point by the error.
- **E3.** The tube is a single bore with no bypass, so passage is proof.
- **E4.** `down_set` sits above every parking position. A channel that parks
  deeper than `park + 30 mm` reads as downstream: a spurious report, not a
  missed one.

## What this does not do

It is still observe-only: nothing gates on the zone. The first consumer would
be accepting the printer's unload for a channel whose zone is downstream but
whose AMS motion is idle (an orphan or a faulted pull), which the firmware
refuses today.
