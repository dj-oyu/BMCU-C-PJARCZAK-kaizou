#pragma once

#include <stdint.h>

// Which online-key readings end a printer pull back.
//
// Along the path the switches sit outer (spool side) -> drive gear -> inner.
// The pull back used to end only when the key read none: either the key went
// empty for kPullBackEmptyConfirmMs or the travel target ran out.
//
// On a loader whose outer lever stays pressed while the strand lies over it,
// "none" is out of reach of the motor. When the tip clears the inner switch the
// key reads outer only (ks 2). If the motor keeps pulling, the tip goes behind
// the drive gear while still sitting on the outer lever. From there the gear
// spins freely and the key never reaches none. The motor ran ~50 s until the
// printer's next command, and the next load could not catch the tip because
// the gear no longer reached it. Observed live on AMS-B slot 3, 2026-11-26.
//
// The point the pull back is after is the same on every loader: the tip has
// left the inner switch, so it is out of the shared path. On a loader whose
// outer lever releases (the 2026-08-07 bench geometry), that point reads none
// and already ended the pull back. Here it reads outer only. Ending on either
// stops the tip just upstream of the inner switch, still in front of the
// gear, where the next send_out can drive it.
//
// The redetect step that follows finishes at once when the key reads anything
// but none, so an outer-only end needs no push to find the tip again.
//
// Build with -DBMCU_PULL_BACK_END_AT_INNER_RELEASE=0 for the previous
// behaviour (end on none only).
//
// Header-only so it is host-testable; Motion_control.cpp is not.
#ifndef BMCU_PULL_BACK_END_AT_INNER_RELEASE
#define BMCU_PULL_BACK_END_AT_INNER_RELEASE 1
#endif

namespace pull_back_end
{

// ks: 0 none, 1 both, 2 outer only, 3 inner only (dm_key_to_state). Builds
// without the two-microswitch decode report 0 or 1 only, so the outer-only
// arm never fires there.
inline bool inner_released(uint8_t ks)
{
    if (ks == 0u) return true;
    return BMCU_PULL_BACK_END_AT_INNER_RELEASE && ks == 2u;
}

// Filament at the inner switch: ks 1 (both) or 3 (inner only). The one reading
// that means "the gear holds it and the firmware can see it" on every loader.
inline bool inner_present(uint8_t ks)
{
    return ks == 1u || ks == 3u;
}

// The DM autoload's buffer-abort retract (S2_RETRACT) on a loader whose outer
// lever releases behind the tip: the key reads none as soon as the tip clears
// the inner switch, while the gear still holds it. The ks == 0 sweep used to
// end the autoload there, with the tip in the gear and invisible -- send_out
// and the autoload both need a non-zero key, so nothing could move it again.
// Instead the retract is followed by a short push back to the inner switch,
// the same thing redetect does after a printer pull back.
//
// Bounded so that a strand that really left (pulled out by hand) costs no more
// than this much drive into nothing. It must stay below DM_FAIL_CLEAR_MS so a
// reseat that finds nothing ends before the sweep's "withdrawn" window does.
static constexpr uint32_t kReseatMaxMs = 400u;

inline bool reseat_after_retract(bool retracting, uint8_t ks)
{
    return retracting && ks == 0u;
}

}
