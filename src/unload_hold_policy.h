#pragma once

#include <stdint.h>

#include "signal_hold_policy.h"

// Unload hold: once the printer has unloaded a channel, the idle control must
// not feed that channel forward until the channel is used again.
//
// Ported from upstream jarczakpawel/BMCU-C-PJARCZAK PR #155. The fault it
// answers, seen on an A1 + BMCU DM: the printer unloads channel N, the BMCU
// pulls it back to its retract position, and within half a second feeds it
// forward again to the tool head. Neither side notices, the next channel is
// fed, and the two filaments meet in the tool head. Two idle paths can feed
// after redetect -> idle:
//
//  - on DM boards a Stage-2 autoload re-armed while the channel was in use
//    (dm_loaded cleared by DM_LOADED_DROP_MS away from "both") pushes the
//    Stage-2 distance as soon as the channel is idle -- the likely cause, it
//    starts right after the unload and is long enough to reach the tool head;
//  - the idle buffer control feeds while the buffer reads below 30 %.
//
// The hold does not depend on which of these would have fired. A channel the
// printer has just unloaded has no reason to move forward by itself.
//
// While held, the idle branch does not feed. The idle retract of a raised
// buffer (filament present and the buffer above the high deadband) still runs,
// so pressure in the buffer is still released.
//
// The hold ends when
//  - the printer drives this channel again (it is the current channel and its
//    AMS-side motion is anything other than idle);
//  - the user starts the auto-unload gesture on it (pull the buffer up);
//  - the filament is taken out (key reads empty for kRemovedMs);
//  - the channel is unplugged.
// Another channel reaching on_use deliberately does not end it: set_motion()
// reports on_use before that channel's filament has reached the extruder, and
// upstream's field test caught channel 1 feeding again while channel 4 was
// still on its way when the hold was released there.
//
// RAM only. After a power cycle no channel is held, as before this policy.
//
// Header-only, allocation-free, no loops, so it compiles on the host.
namespace unload_hold
{

// How long the key must read empty, continuously, before a held channel counts
// as emptied by hand. Upstream's value.
static constexpr uint32_t kRemovedMs = 1000u;

// A pull back only ends early on "no filament" once the key has read empty
// this long. One bad reading would otherwise end the unload with the tip still
// in the shared splitter, redetect would push it back until the key reads
// filament again, and the printer would be told the unload finished. 60 ms is
// about 3.6 mm at 60 mm/s. Upstream's value.
static constexpr uint32_t kPullBackEmptyConfirmMs = 60u;

struct State
{
    uint8_t  held;
    uint32_t empty_since_ms; // signal_hold storage, 0 = not timing
};

inline void reset(State& s)
{
    s.held = 0u;
    s.empty_since_ms = 0u;
}

inline void start(State& s)
{
    s.held = 1u;
    s.empty_since_ms = 0u;
}

// Everything update() needs about one channel on one control pass.
struct Inputs
{
    bool inserted;         // filament_channel_inserted[ch]
    bool printer_driving;  // now_filament_num == ch && AMS motion != idle
    bool auto_unload;      // auto_unload_active[ch]
    bool key_empty;        // MC_ONLINE_key_stu[ch] == 0
};

// One control pass. Returns true on the pass that releases the hold.
inline bool update(State& s, const Inputs& in, uint32_t now_ms)
{
    if (!s.held)
    {
        s.empty_since_ms = 0u;
        return false;
    }

    // Evaluated on every held pass so the window restarts on any pass with
    // filament, exactly like the other key debounces in Motion_control.cpp.
    const bool removed =
        signal_hold::held_for(s.empty_since_ms, in.key_empty, now_ms, kRemovedMs);

    if (!in.inserted || in.printer_driving || in.auto_unload || removed)
    {
        reset(s);
        return true;
    }
    return false;
}

// Whether the idle branch must hold the motor at zero this pass. `ks` is
// MC_ONLINE_key_stu, `pull_stu` is MC_PULL_stu (+1 above the high deadband,
// i.e. a raised buffer that the idle control retracts).
inline bool blocks_idle(const State& s, uint8_t ks, int8_t pull_stu)
{
    if (!s.held) return false;
    const bool raised_retract = (ks != 0u) && (pull_stu > 0);
    return !raised_retract;
}

// Whether the DM autoload may run this pass. Separate from blocks_idle: the
// raised-buffer retract is allowed through, but autoload never is.
inline bool blocks_autoload(const State& s)
{
    return s.held != 0u;
}

}
