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
// strand). Leaving send_out for on_use, directly or through retracts, resets
// it, latch included: the printer only moves on to before_on_use / on_use
// once its own sensor has the filament, and a send that ends any other way
// is the printer's to retry, with a fresh budget. Every other command keeps
// the count; a retract winds it back.
//
// Trip: forward drive stops on that channel (Motion_control_set_PWM brakes
// any push), retracts are still served. Nothing is raised to the printer:
// this one recovers by itself. With the motor stopped, the extruder takes up
// the spilled slack, the strand draws the tube back onto the hub, and the
// extruder then pulls on a strand the BMCU is not feeding. That tension is
// the resume. A retract that winds the count back to zero, or the key
// reading empty, clears it as well.
//
// The resume is exactly what the buffer-based jam detector reads as a jam
// (stretched buffer), so the jam window stays disarmed after a resume until
// the buffer has come back above the jam threshold, or for disarm_ms. A
// held strand also reads as tension; if the resume was one, the push that
// follows either moves the gear with the buffer still pulled (a slip, below)
// or does not move it (the jam detector, once the disarm runs out).
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
// gear then slips was reset by the pressed buffer on every sample. Before
// this, a slip tripped the open-outlet latch, the stretched buffer resumed
// it at once, and each resume disarmed the jam detector: the gear could
// grind on indefinitely.
//
// A slip stops the push like an open outlet, but is not resumed by tension
// (tension is what it looks like). What breaks the loop it would otherwise
// be in -- the pressure controller pushing harder on a buffer the push
// cannot restore -- is that nothing pushes while it is latched.
//
// It may still clear by itself: a tangle the extruder's pull works loose.
// The one reading that proves the strand moves is travel with the motor
// unpowered. Motion_control.cpp coasts the motor while a slip is latched
// (both bridge inputs low, not the brake), so the extruder can draw the
// strand through and the gear turns only because the strand does. Past
// settle_ms (the motor spinning down) forward travel without a push counts
// toward recover_counts; once it passes, the slip clears and the push
// resumes from a cleared controller, with the jam window disarmed as after
// an open-outlet resume (the buffer is still pulled). A slip that comes back
// is allowed max_resumes such recoveries per printer command; after that, or
// once slip_wait_ms pass with no recovery, it is escalated: Motion_control
// brakes and raises the on_use jam (0xF06F) for a human. It ends when the
// printer commands another motion, or the key reads empty.
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
    int32_t  tension_q;           // buffer below, not pushing: pulled
    int32_t  jam_low_q;           // the jam detector's threshold
    uint32_t disarm_ms;           // longest jam disarm after a resume
    int32_t  recover_counts;      // unpowered forward travel that clears a slip
    uint32_t settle_ms;           // after a slip trip, travel that is the motor spinning down
    uint32_t slip_wait_ms;        // a slip not recovered by then is escalated
    uint8_t  max_resumes;         // recoveries allowed per printer command
};

struct State
{
    int64_t  excess_q8;           // counts * 256
    int64_t  slip_q8;             // counts * 256, forward travel while pulled or pressed
    uint8_t  tripped;             // Trip
    uint8_t  mode;
    uint8_t  from_send;           // the count started in send_out (kept through retracts)
    uint8_t  resumes;             // slip recoveries since the last command
    uint32_t disarm_t0_ms;        // 0: jam detector armed
    uint32_t slip_t0_ms;          // when the slip latched
    int64_t  recover_q8;          // counts * 256, unpowered forward travel while slipped
};

inline void reset(State& s)
{
    s.excess_q8 = 0;
    s.slip_q8 = 0;
    s.tripped = TRIP_NONE;
    s.mode = MODE_USE;
    s.from_send = 0u;
    s.resumes = 0u;
    s.disarm_t0_ms = 0u;
    s.slip_t0_ms = 0u;
    s.recover_q8 = 0;
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

// A slip that will not clear by itself: the printer and a human have it.
inline bool slip_escalated(const State& s, const Config& c, uint32_t now_ms)
{
    return s.tripped == TRIP_SLIP &&
           (s.resumes >= c.max_resumes || (now_ms - s.slip_t0_ms) >= c.slip_wait_ms);
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
        s.recover_q8 = 0;
        s.resumes = 0u;
        s.mode = mode;
    }

    // Back-pressure. A pulled buffer is a coupled strand, so whatever the
    // excess was it is not an open outlet; it may be a slip. Tension on a
    // latched open outlet is the tube back on the hub: the resume.
    const bool pulled = pct_q < c.tension_q;
    if (pct_q >= c.press_q || pulled)
    {
        if (s.tripped == TRIP_OPEN && (!pulled || !pushing))
        {
            if (pulled) s.disarm_t0_ms = now_ms ? now_ms : 1u;
            s.tripped = TRIP_NONE;
        }
        s.excess_q8 = 0;
    }
    const bool deflected = pulled || pct_q >= c.press_q;
    if (!deflected) s.slip_q8 = 0;

    if (!measured)
    {
        // Nothing to judge. A held latch stays until contact or key empty.
        s.excess_q8 = 0;
        s.slip_q8 = 0;
        return was != s.tripped;
    }

    const int64_t drain = (mode != MODE_SEND)
        ? (static_cast<int64_t>(c.consume_cps) * 256 * dt_us) / 1000000 : 0;

    if (s.tripped == TRIP_SLIP)
    {
        // Travel with the motor unpowered is the strand's own.
        if (!pushing && fwd_counts > 0 && (now_ms - s.slip_t0_ms) >= c.settle_ms &&
            !slip_escalated(s, c, now_ms))
        {
            s.recover_q8 += static_cast<int64_t>(fwd_counts) * 256;
            if (s.recover_q8 > static_cast<int64_t>(c.recover_counts) * 256)
            {
                s.tripped = TRIP_NONE;
                s.resumes++;
                s.slip_q8 = 0;
                s.recover_q8 = 0;
                s.excess_q8 = 0;
                s.disarm_t0_ms = now_ms ? now_ms : 1u;
            }
        }
        return was != s.tripped;
    }

    if (deflected)
    {
        if (fwd_counts > 0) s.slip_q8 += static_cast<int64_t>(fwd_counts) * 256;
        s.slip_q8 -= drain;
        if (s.slip_q8 < 0) s.slip_q8 = 0;
        if (s.tripped == TRIP_NONE &&
            s.slip_q8 > static_cast<int64_t>(c.slip_budget_counts) * 256)
        {
            s.tripped = TRIP_SLIP;
            s.slip_t0_ms = now_ms;
            s.recover_q8 = 0;
        }
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
