#pragma once

#include <stdint.h>

#include "signal_hold_policy.h"

// When does a low buffer reading during on_use count as a jam?
//
// pressure_ctrl_on_use used to latch g_on_use_low_latch / g_on_use_jam_latch on
// the first single sample with MC_PULL_pct_q below 40 %. Latching stops the
// motor and makes every following reply carry pressure 0xF06F, which the
// printer reads as a feed failure. The moment the extruder first grips a
// freshly loaded strand is exactly when the buffer is pulled hardest, so one
// transient sample there turned a good load into "feed failed": the printer
// spat the filament back out, the BMCU wound it in, and the load was retried
// with the same transient every time -- the endless load/unload loop seen
// during filament swaps.
//
// A real jam keeps the buffer low; a grip transient does not. So the reading
// now has to stay below the threshold for BMCU_ON_USE_JAM_HOLD_MS before it
// latches. BMCU_ON_USE_JAM_HOLD_MS=0 restores the old single-sample behaviour
// exactly.
//
// Header-only so it is host-testable; Motion_control.cpp is not.
#ifndef BMCU_ON_USE_JAM_HOLD_MS
#define BMCU_ON_USE_JAM_HOLD_MS 300u
#endif

namespace on_use_jam
{

static const int32_t kLowPctQ = 4000; // 40.00 %, unchanged

// One control pass. `since_ms` is per-channel storage, zero meaning "not
// timing". Returns true on the pass where the low reading has held long
// enough; the caller latches and stops calling until the latch clears.
inline bool low_reading_latches(uint32_t& since_ms, int32_t pct_q,
                                uint32_t now_ms, uint32_t hold_ms)
{
    const bool low = pct_q < kLowPctQ;
    if (hold_ms == 0u)
    {
        since_ms = 0u;
        return low;
    }
    return signal_hold::held_for(since_ms, low, now_ms, hold_ms);
}

// Any pass that does not evaluate the reading (key empty, latch already set,
// not in on_use) must forget a window in flight, so a stale stamp from an
// earlier session can never fire on the first sample of the next one.
inline void reset(uint32_t& since_ms)
{
    since_ms = 0u;
}

}
