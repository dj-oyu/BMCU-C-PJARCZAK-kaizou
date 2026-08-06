// Host test for src/pressure_event_policy.h.
// Its one call site lives in bmcu_link.cpp, which does not compile on the host,
// so this is where the deadband arithmetic gets checked.

#include "pressure_event_policy.h"

#define CHECK(cond, code) do { if (!(cond)) return (code); } while (0)

// Motion_control.cpp:756 -- (pct - 50) * 65535 / 50, clamped at neutral.
static uint16_t encode(unsigned pct)
{
    if (pct <= 50u) return 0u;
    return static_cast<uint16_t>(((pct - 50u) * 65535u) / 50u);
}

// The bug this exists to kill: MC_PULL_pct dithers by one point forever, and
// every printer poll re-marks PRESSURE dirty. A reference that only moves when
// something was reported must stay silent through all of it -- at every
// operating point, not just the lucky ones.
static int test_one_point_dither_is_silent_everywhere(void)
{
    // Stops at 98/99 rather than 99/100 because encode(100) is 0xFFFF, which
    // the encoding already spends on "no active channel". See
    // test_full_buffer_collides_with_the_no_channel_sentinel.
    for (unsigned pct = 51u; pct < 99u; ++pct)
    {
        const uint16_t low = encode(pct);
        const uint16_t high = encode(pct + 1u);
        uint16_t reported = low;

        // Fifty polls of the buffer wobbling between two adjacent points.
        for (int poll = 0; poll < 50; ++poll)
        {
            const uint16_t sample = (poll & 1) ? high : low;
            if (pressure_event::should_report(reported, sample))
                return static_cast<int>(100u + pct);
        }
        CHECK(reported == low, 11);
    }
    return 0;
}

// The other half: a real two-point move must not be swallowed.
static int test_two_point_move_reports(void)
{
    for (unsigned pct = 51u; pct + 2u <= 100u; ++pct)
    {
        const uint16_t from = encode(pct);
        const uint16_t to = encode(pct + 2u);
        if (!pressure_event::should_report(from, to))
            return static_cast<int>(200u + pct);
    }
    return 0;
}

// A drift of one point at a time must still be reported eventually. This is
// what a deadband against the *previous sample* would lose entirely, and it is
// why the reference only moves when something was said.
static int test_slow_drift_still_reports(void)
{
    uint16_t reported = encode(51u);
    int reports = 0;

    for (unsigned pct = 52u; pct <= 70u; ++pct)
    {
        const uint16_t sample = encode(pct);
        if (pressure_event::should_report(reported, sample))
        {
            reported = sample;
            ++reports;
        }
    }

    // Nineteen points of travel at ~1.56 points per deadband: it must speak
    // repeatedly, and it must have kept up rather than stalling at the first.
    CHECK(reports >= 8, 31);
    CHECK(reported >= encode(69u), 32);
    return 0;
}

// Sentinels are not on the analogue scale. 0xFFFF and 0xFF74 differ by 139,
// well inside the 2048 deadband, so a numeric rule alone would swallow the
// transition between them.
static int test_sentinels_bypass_the_deadband(void)
{
    CHECK(pressure_event::should_report(0xFFFFu, 0xFF74u), 41);
    CHECK(pressure_event::should_report(0xFF74u, 0xFFFFu), 42);
    // Into and out of the jam code, against a normal reading.
    CHECK(pressure_event::should_report(encode(52u), 0xF06Fu), 43);
    CHECK(pressure_event::should_report(0xF06Fu, encode(52u)), 44);
    // A sentinel one count away from a normal value still reports.
    CHECK(pressure_event::should_report(0xFF73u, 0xFF74u), 45);
    return 0;
}

// An idle channel sits at 0xFFFF indefinitely and is polled the whole time.
// Equal is equal, sentinel or not.
static int test_unchanged_never_reports(void)
{
    CHECK(!pressure_event::should_report(0xFFFFu, 0xFFFFu), 51);
    CHECK(!pressure_event::should_report(0xF06Fu, 0xF06Fu), 52);
    CHECK(!pressure_event::should_report(0u, 0u), 53);
    CHECK(!pressure_event::should_report(encode(53u), encode(53u)), 54);
    return 0;
}

// The deadband is 1 << 11. Pin both sides of it so a change to the shift is a
// deliberate act rather than a silent widening.
static int test_deadband_boundary(void)
{
    CHECK(!pressure_event::should_report(10000u, 10000u + 2047u), 61);
    CHECK(pressure_event::should_report(10000u, 10000u + 2048u), 62);
    CHECK(!pressure_event::should_report(10000u, 10000u - 2047u), 63);
    CHECK(pressure_event::should_report(10000u, 10000u - 2048u), 64);
    return 0;
}

// Found by the dither sweep above, which failed at 99/100 before this was
// understood. It is a property of the encoding, not of this policy, and it
// predates the deadband:
//
//   Motion_control.cpp:756  pressure = (pct - 50) * 65535 / 50
//   at pct == 100 that is exactly 65535 == 0xFFFF
//
// and 0xFFFF is the value bmcu_pressure_class reads as "no active channel"
// (class 0). So a completely full buffer is indistinguishable on the wire from
// no filament at all. The other two sentinels are safe: 0xFF74 and 0xF06F are
// not reachable from the formula for any integer pct.
//
// Pinned rather than fixed, because the encoding is what the printer sees and
// narrowing the range is a bus-facing change. What this asserts is that the
// collision exists, so changing the formula fails here and is noticed.
static int test_full_buffer_collides_with_the_no_channel_sentinel(void)
{
    CHECK(encode(100u) == 0xFFFFu, 71);
    CHECK(pressure_event::is_sentinel(encode(100u)), 72);

    // Neither of the other sentinels is producible, so neither can be mistaken
    // for a reading.
    for (unsigned pct = 51u; pct <= 100u; ++pct)
    {
        CHECK(encode(pct) != 0xFF74u, 73);
        CHECK(encode(pct) != 0xF06Fu, 74);
    }
    return 0;
}

int main(void)
{
    int result = test_full_buffer_collides_with_the_no_channel_sentinel();
    if (result) return result;
    result = test_one_point_dither_is_silent_everywhere();
    if (result) return result;
    result = test_two_point_move_reports();
    if (result) return result;
    result = test_slow_drift_still_reports();
    if (result) return result;
    result = test_sentinels_bypass_the_deadband();
    if (result) return result;
    result = test_unchanged_never_reports();
    if (result) return result;
    result = test_deadband_boundary();
    if (result) return result;
    return 0;
}
