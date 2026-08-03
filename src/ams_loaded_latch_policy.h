#pragma once

#include <stdint.h>

// Pure decision logic for when a key-zero may drop the loaded latch.
//
// g_loaded_ch records which channel the printer has loaded. It is flash-backed
// and asymmetric: the printer re-latches it only on a commanded load, which it
// will not send while it still believes the channel is loaded, so anything that
// clears it while the printer disagrees desynchronises the two until the next
// load. The sensor side is the weak end -- MC_ONLINE_key_stu is one microswitch
// reading, and a single pass of 0 used to clear the latch outright.
//
// This gates only the sensor-driven clear. Every printer-commanded clear stays
// immediate: those carry real information about the filament and go straight to
// ams_state_set_unloaded without passing through here.
//
// Header-only, allocation-free, no loops.
namespace ams_loaded_latch
{

static const uint8_t kNoChannel = 0xFFu;

struct State
{
    uint8_t  channel;   // channel whose key-zero is being timed, kNoChannel if none
    uint32_t since_ms;  // when that key-zero began
};

inline void reset(State& s)
{
    s.channel = kNoChannel;
    s.since_ms = 0u;
}

// One control pass. `loaded_ch` is the latch as the firmware currently holds it
// (kNoChannel or >= 4 meaning nothing latched) and `key_zero` is that channel's
// online key reading empty right now. Returns true exactly once, on the pass
// where the key has read empty continuously for `hold_ms`, meaning the caller
// should clear the latch.
//
// Any pass where the key is not empty, or where the latched channel changed
// underneath us, restarts the window -- so the hold is continuous, not
// cumulative, and a latch cleared by the printer mid-window leaves no state
// behind to catch out the next channel.
inline bool poll(State& s, uint8_t loaded_ch, bool key_zero, uint32_t now_ms,
                 uint32_t hold_ms)
{
    if (loaded_ch >= 4u || !key_zero)
    {
        reset(s);
        return false;
    }

    if (s.channel != loaded_ch)
    {
        s.channel = loaded_ch;
        s.since_ms = now_ms;
        return false;
    }

    // Unsigned difference against a monotonic millisecond counter: correct
    // across the counter's own wrap, and the window is far below 2^31 ms.
    if ((uint32_t)(now_ms - s.since_ms) < hold_ms) return false;

    reset(s);
    return true;
}

// Whether a key-zero is being timed while the latch is still held. The latch
// itself is unchanged in this window, so anything reporting the latch keeps
// reporting it as held; this is for diagnostics that want to say why.
inline bool is_pending(const State& s)
{
    return s.channel != kNoChannel;
}

}
