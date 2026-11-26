#pragma once

#include <stdint.h>

// Open-outlet guard: stop pushing when the strand travels without anything
// taking it (docs/FEED_RESISTANCE_DESIGN.md §2).
//
// The PTFE tube can come off the filament hub. The BMCU then keeps feeding,
// and bare filament pours out of the gap. Nothing pushes back, so the buffer
// sits at rest -- but so does it in normal printing: AMS-B ch3, 2026-11-27,
// printing at PWM ~600 read pull 51 % against a free-travel 50 %. The buffer
// cannot tell the two apart. Travel can: a printing extruder takes at most a
// few mm/s, and the strand cannot go anywhere faster unless the outlet is
// open (or the buffer is filling, which is bounded by its stroke).
//
// So the count is the travel in excess of what the printer could have
// consumed: forward travel adds, the maximum consumption rate drains it. It
// is judged against a budget:
//
//   send_out   no consumption, no drain; the budget is the path length
//              (rest -> extruder) plus margin, the one stretch where free
//              travel is normal.
//   any other  drained at the consumption ceiling; the budget is the buffer
//              stroke plus margin, a few tens of mm.
//
// Back-pressure resets it: the buffer pressed (a coupled strand pushed), or
// tension while the motor is not pushing (the extruder pulling a coupled
// strand). Leaving send_out for anything but a retract resets it, latch
// included: the printer only moves on to before_on_use / on_use once its own
// sensor has the filament, and a send that ends any other way is the
// printer's to retry, with a fresh budget. A retract keeps the count and
// winds it back.
//
// Trip: forward drive stops on that channel (Motion_control_set_PWM brakes
// any push), retracts are still served. Nothing is raised to the printer.
// Resume: tension. The printer consumes the spilled slack, the tube is drawn
// back onto the hub, and the extruder then pulls on a strand the BMCU is not
// feeding. A retract that winds the count back to zero, or the key reading
// empty, clears it as well.
//
// The resume is exactly what the buffer-based jam detector reads as a jam
// (stretched buffer), so the jam window stays disarmed after a resume until
// the buffer has come back above the jam threshold, or for disarm_ms.
//
// No encoder, no verdict: the count needs measured travel.
//
// Header-only so it is host-testable; Motion_control.cpp is not.

namespace outlet_guard
{

enum Mode : uint8_t
{
    MODE_SEND = 0,   // filament_motion_send
    MODE_USE  = 1,   // every other motion that can feed forward
    MODE_PULL = 2,   // retract
};

struct Config
{
    int32_t  send_budget_counts;  // path + margin
    int32_t  use_budget_counts;   // buffer stroke + margin
    uint32_t consume_cps;         // consumption ceiling, counts per second
    int32_t  press_q;             // buffer at or above: pressed (0.01 %)
    int32_t  tension_q;           // buffer below, not pushing: pulled
    int32_t  jam_low_q;           // the jam detector's threshold
    uint32_t disarm_ms;           // longest jam disarm after a resume
};

struct State
{
    int64_t  excess_q8;           // counts * 256
    uint8_t  tripped;
    uint8_t  mode;
    uint32_t disarm_t0_ms;        // 0: jam detector armed
};

inline void reset(State& s)
{
    s.excess_q8 = 0;
    s.tripped = 0u;
    s.mode = MODE_USE;
    s.disarm_t0_ms = 0u;
}

inline int32_t budget_counts(const Config& c, uint8_t mode)
{
    return mode == MODE_SEND ? c.send_budget_counts : c.use_budget_counts;
}

inline bool blocks_push(const State& s)
{
    return s.tripped != 0u;
}

// One AS5600 sample. fwd_counts is forward travel since the last sample,
// dt_us the time it covers. Returns true when `tripped` changed.
inline bool sample(State& s, const Config& c, uint8_t mode, bool measured,
                   int32_t fwd_counts, uint32_t dt_us, int32_t pct_q,
                   bool pushing, bool key_present, uint32_t now_ms)
{
    const uint8_t was = s.tripped;

    if (!key_present)
    {
        s.excess_q8 = 0;
        s.tripped = 0u;
        s.mode = mode;
        s.disarm_t0_ms = 0u;
        return was != 0u;
    }

    if (mode != s.mode)
    {
        // A send that ends in anything but a retract starts over: either the
        // printer has the filament, or it gave up and will retry. A retract
        // keeps the count, so winding it back is what clears a latch.
        if (s.mode == MODE_SEND && mode != MODE_PULL)
        {
            s.excess_q8 = 0;
            s.tripped = 0u;
        }
        s.mode = mode;
    }

    // Back-pressure.
    if (pct_q >= c.press_q)
    {
        s.excess_q8 = 0;
        s.tripped = 0u;
    }
    else if (!pushing && pct_q < c.tension_q)
    {
        if (s.tripped) s.disarm_t0_ms = now_ms ? now_ms : 1u;
        s.excess_q8 = 0;
        s.tripped = 0u;
    }

    if (!measured)
    {
        // Nothing to judge. A held latch stays until contact or key empty.
        s.excess_q8 = 0;
        return was != s.tripped;
    }

    s.excess_q8 += static_cast<int64_t>(fwd_counts) * 256;
    // Draining while latched would forget what a retract has to wind back.
    if (mode != MODE_SEND && !s.tripped)
        s.excess_q8 -= (static_cast<int64_t>(c.consume_cps) * 256 * dt_us) / 1000000;
    if (s.excess_q8 < 0) s.excess_q8 = 0;

    if (s.tripped)
    {
        if (fwd_counts < 0 && s.excess_q8 == 0) s.tripped = 0u;
    }
    else if (s.excess_q8 > static_cast<int64_t>(budget_counts(c, mode)) * 256)
    {
        s.tripped = 1u;
    }
    return was != s.tripped;
}

// The jam detector asks before evaluating a low buffer. True while a resume
// has disarmed it; the disarm ends once the buffer is back above the jam
// threshold or disarm_ms has passed.
inline bool jam_disarmed(State& s, const Config& c, int32_t pct_q, uint32_t now_ms)
{
    if (s.disarm_t0_ms == 0u) return false;
    if (pct_q >= c.jam_low_q || (now_ms - s.disarm_t0_ms) >= c.disarm_ms)
    {
        s.disarm_t0_ms = 0u;
        return false;
    }
    return true;
}

}
