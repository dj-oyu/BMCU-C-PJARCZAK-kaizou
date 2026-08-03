#pragma once

#include <stdint.h>

// "Has this condition been true continuously for long enough."
//
// Motion_control.cpp measures several windows off the online key, and two of
// them are the same shape written twice: stamp a timestamp on the first pass
// the condition holds, fire once when the elapsed time reaches the limit, and
// throw the timestamp away the moment the condition stops holding. The
// arithmetic is three lines and it was three lines in two places, in a file
// that cannot be compiled on the host and therefore cannot be tested at all.
// Here it can.
//
// Deliberately not a general timer. Windows that time an *operation* rather
// than a signal do not belong here and are not folded in -- REDETECT_TIMEOUT_MS
// counts driving time rather than time in state, AUTO_UNLOAD_MAX_MS bounds an
// unload attempt, and AUTO_UNLOAD_ARM_MS bounds a two-edge gesture rather than
// a held level. Nor does the DM autoload state machine, whose dm_auto_t0_ms is
// a state-entry stamp shared by four windows at once. Folding any of those in
// would change what they measure.
//
// Header-only, allocation-free, no loops.
namespace signal_hold
{

// One control pass. `since_ms` is the caller's own per-channel storage, zero
// meaning "not currently timing". Returns true on the single pass where `value`
// has been true continuously for `hold_ms`, and resets so the caller gets one
// edge rather than a level.
//
// The zero sentinel is inherited rather than chosen. Both call sites used it,
// and it has one visible quirk worth naming: a window that would have been
// stamped at exactly now_ms == 0 reads as "not timing" on the next pass and so
// restarts, costing one extra pass roughly once every 49.7 days of uptime. That
// is preserved exactly, because the point of this change is that it decides
// nothing differently. A `bool timing` field would remove the quirk and cost a
// byte per channel; it is not this commit's call to make.
inline bool held_for(uint32_t& since_ms, bool value, uint32_t now_ms,
                     uint32_t hold_ms)
{
    if (!value)
    {
        since_ms = 0u;
        return false;
    }

    if (since_ms == 0u)
    {
        since_ms = now_ms;
        return false;
    }

    // Unsigned difference against a monotonic millisecond counter: correct
    // across the counter's own wrap, and these windows are far below 2^31 ms.
    if ((uint32_t)(now_ms - since_ms) < hold_ms) return false;

    since_ms = 0u;
    return true;
}

}
