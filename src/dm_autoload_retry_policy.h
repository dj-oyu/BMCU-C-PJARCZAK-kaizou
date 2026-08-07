#pragma once

#include <stdint.h>

// Lifetime and accounting of the Stage2 abort budget, dm_auto_try.
//
// The budget bounds how many times autoload may push a strand into the merger,
// fill the buffer, and back off before it gives up: three strikes, then
// dm_fail_latch and the channel parks red waiting for a human. It is a
// *per-insertion* budget, and that is the whole point -- the thing being
// bounded is one filament's worth of attempts, so it has to outlive every
// transition that keeps the same strand in the loader.
//
// Until 2026-08-08 it did not. It was cleared on every entry to S2_PUSH,
// including the two that happen mid-insertion -- S1_PUSH -> S2_PUSH and the
// direct IDLE -> S2_PUSH -- and those are exactly the steps the abort path
// cycles through. Observed live on ch2 with the printer idle: S2_PUSH filled
// the buffer after ~10.4 s, aborted, retracted far enough to read `outer`, went
// round through Stage1, re-entered S2_PUSH with the counter back at zero, and
// repeated on a 25 s period driving the motor at +/-900 PWM, with dm_fail_latch
// clear the whole time. Six cycles were recorded with no sign of stopping; the
// limiter could only ever fire if a retract happened to stop while the key
// still read `both`, which on this geometry it does not. See
// diagnostics/2026-08-08-autoload-livelock.
//
// The buffer filling is not itself the fault. An uncommanded autoload pushes
// 120 mm into the merger with no extruder pulling, so aborting is correct; the
// fault is that the machine never gave up.
//
// Header-only so the accounting is host-testable; Motion_control.cpp is not.
namespace dm_autoload
{

// Three aborts and the channel parks. Matches the pre-existing `t >= 3u`.
static const uint8_t kMaxTries = 3u;

// Records one buffer abort against the budget. Returns true when it is spent
// and the caller must latch dm_fail_latch rather than schedule another retry.
//
// Saturating rather than wrapping: a budget that rolled over to zero would be
// the same livelock with a 255-cycle period, which is worse than the original
// because it would look fixed for over an hour.
inline bool count_abort(uint8_t& tries)
{
    if (tries < 255u) ++tries;
    return tries >= kMaxTries;
}

// Whether a teardown to DM_AUTO_IDLE ends the insertion, and so may clear the
// budget. Only an empty key means the strand has left.
//
// `inner` (3) is the reading this exists to exclude. Both Stage2 teardowns that
// drop to idle -- S2_PUSH on a deviated key and S2_RETRACT on an unexpected one
// -- are reached with either 0 or 3, and treating 3 as an ending hands back a
// full budget to a strand that never moved. `outer` (2) and `both` (1) never
// reach these paths; they have their own transitions.
//
// In practice the sweep in Motion_control_run has already forced idle by the
// time ks is 0, so this reads as "no" on every pass the machine actually sees.
// It is written for the reading rather than for that ordering, because the
// ordering is not this file's to depend on.
inline bool insertion_ended(uint8_t ks)
{
    return ks == 0u;
}

}
