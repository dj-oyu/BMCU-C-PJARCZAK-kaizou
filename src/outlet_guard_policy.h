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
// Back-pressure resets it while unlatched: the buffer pressed (a coupled
// strand pushed) or pulled (the extruder pulling a coupled strand). Leaving send_out for on_use, directly or through retracts, resets
// it, latch included: the printer only moves on to before_on_use / on_use
// once its own sensor has the filament, and a send that ends any other way
// is the printer's to retry, with a fresh budget. Every other command keeps
// the count; a retract winds it back.
//
// Trip: forward drive stops on that channel (Motion_control_set_PWM brakes
// any push), retracts are still served; in on_use Motion_control.cpp raises
// the jam (0xF06F), so the printer stops and unloads. A detached tube does
// not come back by itself, so nothing resumes on its own: the latch holds,
// buffer readings included, until a retract winds the count back to zero
// (the printer's unload), the key reads empty, or a send ends in on_use.
// An earlier version resumed on tension, assuming consumption would draw the
// tube back onto the hub; that resume had to disarm the jam detector, and a
// slipping gear (below) used it to grind on indefinitely.
//
// No encoder, no verdict: the count needs measured travel.
//
// Gear slip. The AS5600 sits on the drive gear's shaft, so it measures the
// gear, not the strand. A gear slipping on a held strand (a spool clamped at
// its core, a tangle) reads as travel, and grinds the filament into dust
// inside the BMCU. The buffer says which. A strand held upstream while the
// extruder pulls is a stretched buffer, and real forward travel would bring
// it back within its stroke; a strand blocked downstream is a pressed one,
// and real travel cannot go on compressing it past its stroke. So forward
// travel while the buffer reads pulled or pressed is counted on its own
// (`slip`), drained at the consumption ceiling outside send_out (the extruder
// can take that much and keep the buffer where it is); past slip_budget it
// is a slip, never an open outlet. Quint found the pressed case: a send_out
// with the strand at the extruder (pressed, below the 85 % hard stop) whose
// gear then slips was reset by the pressed buffer on every sample. Before this, a slip tripped
// the open-outlet latch, the stretched buffer resumed it at once, and each
// resume disarmed the jam detector: the gear could grind on indefinitely.
//
// A slip brakes the push like an open outlet. It ends when the printer
// commands another motion, or the key reads empty. Motion_control.cpp raises the on_use jam
// for it, so the printer stops the channel and unloads.
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

enum Trip : uint8_t
{
    TRIP_NONE = 0,
    TRIP_OPEN = 1,   // travel nothing takes
    TRIP_SLIP = 2,   // gear travel the strand did not make
};

struct Config
{
    int32_t  send_budget_counts;  // path + margin
    int32_t  use_budget_counts;   // buffer stroke + margin
    int32_t  slip_budget_counts;  // forward travel allowed while pulled or pressed
    uint32_t consume_cps;         // consumption ceiling, counts per second
    int32_t  press_q;             // buffer at or above: pressed (0.01 %)
    int32_t  tension_q;           // buffer below: pulled
};

struct State
{
    int64_t  excess_q8;           // counts * 256
    int64_t  slip_q8;             // counts * 256, forward travel while pulled or pressed
    uint8_t  tripped;             // Trip
    uint8_t  mode;
    uint8_t  from_send;           // the count started in send_out (kept through retracts)
};

inline void reset(State& s)
{
    s.excess_q8 = 0;
    s.slip_q8 = 0;
    s.tripped = TRIP_NONE;
    s.mode = MODE_USE;
    s.from_send = 0u;
}

inline int32_t budget_counts(const Config& c, uint8_t mode)
{
    return mode == MODE_SEND ? c.send_budget_counts : c.use_budget_counts;
}

inline bool blocks_push(const State& s)
{
    return s.tripped != TRIP_NONE;
}

inline bool slipping(const State& s)
{
    return s.tripped == TRIP_SLIP;
}

// One AS5600 sample. fwd_counts is forward travel since the last sample,
// dt_us the time it covers. Returns true when `tripped` changed.
inline bool sample(State& s, const Config& c, uint8_t mode, bool measured,
                   int32_t fwd_counts, uint32_t dt_us, int32_t pct_q,
                   bool key_present)
{
    const uint8_t was = s.tripped;

    if (!key_present)
    {
        reset(s);
        s.mode = mode;
        return was != TRIP_NONE;
    }

    if (mode != s.mode)
    {
        // A send that ends in on_use, directly or through retracts, starts
        // over, latch included: either the printer has the filament, or it
        // gave up and will retry. A send's count carried through a pull into
        // on_use tripped a printing strand (Quint, noFalseTrip). Any other
        // change keeps the count: a repeated command buys no new budget, and
        // winding a retract back is what clears a latch.
        if (mode == MODE_USE && (s.mode == MODE_SEND || s.from_send))
        {
            s.excess_q8 = 0;
            if (s.tripped == TRIP_OPEN) s.tripped = TRIP_NONE;
        }
        if (mode == MODE_SEND) s.from_send = 1u;
        else if (mode == MODE_USE || s.mode == MODE_USE) s.from_send = 0u;
        // A slip is the printer's to handle; its next command ends it.
        if (s.tripped == TRIP_SLIP) s.tripped = TRIP_NONE;
        s.slip_q8 = 0;
        s.mode = mode;
    }

    // Back-pressure: a coupled strand, so the travel so far went somewhere.
    // A latch ignores it; only a retract undoes what went out.
    const bool deflected = pct_q < c.tension_q || pct_q >= c.press_q;
    if (deflected && s.tripped == TRIP_NONE) s.excess_q8 = 0;
    if (!deflected) s.slip_q8 = 0;

    if (!measured)
    {
        // Nothing to judge. A held latch stays, with what it has to wind back.
        if (s.tripped == TRIP_NONE) s.excess_q8 = 0;
        s.slip_q8 = 0;
        return was != s.tripped;
    }

    const int64_t drain = (mode != MODE_SEND)
        ? (static_cast<int64_t>(c.consume_cps) * 256 * dt_us) / 1000000 : 0;

    if (deflected)
    {
        // A retract still winds a latched count back, whatever the buffer
        // reads while it does.
        if (s.tripped == TRIP_OPEN && fwd_counts < 0)
        {
            s.excess_q8 += static_cast<int64_t>(fwd_counts) * 256;
            if (s.excess_q8 <= 0) { s.excess_q8 = 0; s.tripped = TRIP_NONE; }
            return was != s.tripped;
        }
        if (fwd_counts > 0) s.slip_q8 += static_cast<int64_t>(fwd_counts) * 256;
        s.slip_q8 -= drain;
        if (s.slip_q8 < 0) s.slip_q8 = 0;
        if (s.tripped == TRIP_NONE &&
            s.slip_q8 > static_cast<int64_t>(c.slip_budget_counts) * 256)
            s.tripped = TRIP_SLIP;
        return was != s.tripped;
    }

    s.excess_q8 += static_cast<int64_t>(fwd_counts) * 256;
    // Draining while latched would forget what a retract has to wind back.
    if (s.tripped == TRIP_NONE) s.excess_q8 -= drain;
    if (s.excess_q8 < 0) s.excess_q8 = 0;

    if (s.tripped == TRIP_OPEN)
    {
        if (fwd_counts < 0 && s.excess_q8 == 0) s.tripped = TRIP_NONE;
    }
    else if (s.tripped == TRIP_NONE &&
             s.excess_q8 > static_cast<int64_t>(budget_counts(c, mode)) * 256)
    {
        s.tripped = TRIP_OPEN;
    }
    return was != s.tripped;
}

}
