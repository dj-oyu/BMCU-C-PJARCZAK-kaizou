#pragma once

#include <stdint.h>

// "Has the buffer pressure moved enough to be worth saying out loud."
//
// Pressure is the one analogue value in a mechanism built for discrete state.
// Motion_control reports (MC_PULL_pct - 50) * 65535/50, so one percentage point
// of buffer travel is 1311 counts -- and MC_PULL_pct dithers by a point forever.
// The notify guard in bambu_bus_ams.cpp marks PRESSURE dirty on every
// set_motion, which is every printer poll, and because the value really had
// changed each time, push_state_event's equality check never fired. Measured on
// the bench: 12.5 events/s, 448 B/s into the bridge's journal, 38 MB/day, all of
// it a 1% wobble.
//
// The comparison is against the last value an event REPORTED, not against the
// previous sample. That is the mechanism, not an implementation detail:
//
//   - against the previous sample, any rule without memory chatters wherever
//     the signal straddles its own boundary. Masking the low bits was measured
//     across the pressure scale: a 4096 bucket still leaves 15 of 50 operating
//     points sitting on a boundary, where the 1-point dither flips the bucket
//     every poll and emits exactly as before. Widening to 8192 costs 6.2 points
//     of resolution and still leaves 7.
//   - against the last reported value, the reference only moves when something
//     was actually said, which is hysteresis. A 1-point dither emits at zero
//     operating points, and a slow drift still accumulates against a fixed
//     reference until it crosses -- so nothing is lost, only the wobble.
//
// Header-only, allocation-free, no loops. The caller owns the reference.
namespace pressure_event
{

// Deadband of 1 << kDeadbandShift = 2048: above one percentage point (1311),
// below two (2622).
constexpr uint8_t kDeadbandShift = 11u;

// Values that are not on the analogue scale, so the deadband must not apply.
// 0xFFFF means no active channel and 0xF06F is the jam code the printer raises
// HMS on. 0xFFFF and 0xFF74 differ by 139, well inside the deadband: without
// this exemption a real transition between them would be silently swallowed.
inline bool is_sentinel(uint16_t value)
{
    return value == 0xFFFFu || value == 0xFF74u || value == 0xF06Fu;
}

// True when `value` should be reported, given the last value reported. The
// caller assigns `value` to its reference only when this returns true, which is
// what makes the reference hysteretic rather than a moving average.
//
// Equal values are never worth reporting, sentinel or not -- an idle channel
// sits at 0xFFFF indefinitely and has nothing to say each time it is polled.
inline bool should_report(uint16_t reported, uint16_t value)
{
    if (value == reported) return false;
    if (is_sentinel(value) || is_sentinel(reported)) return true;

    const uint16_t delta = (value > reported)
                               ? static_cast<uint16_t>(value - reported)
                               : static_cast<uint16_t>(reported - value);
    return (delta >> kDeadbandShift) != 0u;
}

}
