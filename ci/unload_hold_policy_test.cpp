// Host test for src/unload_hold_policy.h.
// The call sites live in Motion_control.cpp, which does not compile on the
// host. The scenarios follow upstream PR #155's description of the A1
// channel 1 -> 4 change. All timestamps are milliseconds.

#include "unload_hold_policy.h"

#define CHECK(cond, code) do { if (!(cond)) return (code); } while (0)

static unload_hold::Inputs parked(void)
{
    // Channel unloaded and parked: plugged in, filament at the key, not the
    // printer's current motion, no gesture.
    unload_hold::Inputs in;
    in.inserted = true;
    in.printer_driving = false;
    in.auto_unload = false;
    in.key_empty = false;
    return in;
}

// Not held: nothing is blocked, the policy is invisible.
static int test_unheld_channel_is_unaffected(void)
{
    unload_hold::State s;
    unload_hold::reset(s);

    CHECK(!unload_hold::blocks_idle(s, 1u, -1), 11);
    CHECK(!unload_hold::blocks_idle(s, 0u, 0), 12);
    CHECK(!unload_hold::blocks_autoload(s), 13);
    CHECK(!unload_hold::update(s, parked(), 1000u), 14);
    return 0;
}

// The fault itself: right after the unload, a low buffer (idle feed) and a
// stale Stage-2 (autoload) must both be held off, for as long as it takes.
static int test_hold_blocks_feeding_until_released(void)
{
    unload_hold::State s;
    unload_hold::reset(s);
    unload_hold::start(s);

    CHECK(unload_hold::blocks_autoload(s), 21);
    CHECK(unload_hold::blocks_idle(s, 1u, -1), 22);   // buffer below 30 %
    CHECK(unload_hold::blocks_idle(s, 1u, 0), 23);    // in the deadband
    CHECK(unload_hold::blocks_idle(s, 2u, -1), 24);   // outer switch only

    for (uint32_t t = 1000u; t < 600000u; t += 1000u)
        CHECK(!unload_hold::update(s, parked(), t), 25);
    CHECK(unload_hold::blocks_autoload(s), 26);
    return 0;
}

// A raised buffer is still retracted while held: pressure must be released.
static int test_raised_buffer_retract_still_runs(void)
{
    unload_hold::State s;
    unload_hold::reset(s);
    unload_hold::start(s);

    CHECK(!unload_hold::blocks_idle(s, 1u, 1), 31);
    // ...but not without filament at the key.
    CHECK(unload_hold::blocks_idle(s, 0u, 1), 32);
    // And the autoload stays blocked regardless.
    CHECK(unload_hold::blocks_autoload(s), 33);
    return 0;
}

// Another channel reaching on_use must not release this one. In the policy
// that is simply "printer_driving is only true for our own channel"; the call
// site computes it as now_filament_num == ch && motion != idle.
static int test_printer_driving_this_channel_releases(void)
{
    unload_hold::State s;
    unload_hold::reset(s);
    unload_hold::start(s);

    unload_hold::Inputs in = parked();
    CHECK(!unload_hold::update(s, in, 1000u), 41);

    in.printer_driving = true;
    CHECK(unload_hold::update(s, in, 1010u), 42);
    CHECK(!unload_hold::blocks_autoload(s), 43);
    CHECK(!unload_hold::blocks_idle(s, 1u, -1), 44);
    // Releases once, not on every pass.
    CHECK(!unload_hold::update(s, in, 1020u), 45);
    return 0;
}

static int test_auto_unload_gesture_releases(void)
{
    unload_hold::State s;
    unload_hold::reset(s);
    unload_hold::start(s);

    unload_hold::Inputs in = parked();
    in.auto_unload = true;
    CHECK(unload_hold::update(s, in, 5000u), 51);
    CHECK(!unload_hold::blocks_autoload(s), 52);
    return 0;
}

static int test_unplugged_channel_releases(void)
{
    unload_hold::State s;
    unload_hold::reset(s);
    unload_hold::start(s);

    unload_hold::Inputs in = parked();
    in.inserted = false;
    CHECK(unload_hold::update(s, in, 5000u), 61);
    return 0;
}

// Removal needs the key empty for kRemovedMs continuously; a single empty
// reading, or a gap with filament, restarts the window.
static int test_removal_needs_a_sustained_empty_key(void)
{
    unload_hold::State s;
    unload_hold::reset(s);
    unload_hold::start(s);

    unload_hold::Inputs in = parked();
    in.key_empty = true;
    CHECK(!unload_hold::update(s, in, 10000u), 71);
    CHECK(!unload_hold::update(s, in, 10500u), 72);

    in.key_empty = false;                       // filament seen again
    CHECK(!unload_hold::update(s, in, 10600u), 73);

    in.key_empty = true;
    CHECK(!unload_hold::update(s, in, 10700u), 74);
    CHECK(!unload_hold::update(s, in, 11699u), 75); // 999 ms
    CHECK(unload_hold::blocks_autoload(s), 76);
    CHECK(unload_hold::update(s, in, 11700u), 77);  // 1000 ms
    CHECK(!unload_hold::blocks_autoload(s), 78);
    return 0;
}

// A new unload restarts cleanly: no half-timed removal window survives.
static int test_restart_clears_the_removal_window(void)
{
    unload_hold::State s;
    unload_hold::reset(s);
    unload_hold::start(s);

    unload_hold::Inputs in = parked();
    in.key_empty = true;
    CHECK(!unload_hold::update(s, in, 20000u), 81);

    unload_hold::start(s);
    CHECK(s.empty_since_ms == 0u, 82);
    CHECK(!unload_hold::update(s, in, 20900u), 83); // window restarts here
    CHECK(!unload_hold::update(s, in, 21800u), 84);
    CHECK(unload_hold::update(s, in, 21900u), 85);
    return 0;
}

// The pull back's empty exit: 60 ms continuous, a reading with filament
// starts the wait over. Same primitive, upstream's limit.
static int test_pull_back_empty_debounce(void)
{
    uint32_t since = 0u;
    const uint32_t ms = unload_hold::kPullBackEmptyConfirmMs;

    CHECK(!signal_hold::held_for(since, true, 100u, ms), 91);
    CHECK(!signal_hold::held_for(since, false, 101u, ms), 92); // one bad reading
    CHECK(!signal_hold::held_for(since, true, 200u, ms), 93);
    CHECK(!signal_hold::held_for(since, true, 259u, ms), 94);
    CHECK(signal_hold::held_for(since, true, 260u, ms), 95);
    return 0;
}

int main(void)
{
    int rc = 0;
    if ((rc = test_unheld_channel_is_unaffected()) != 0) return rc;
    if ((rc = test_hold_blocks_feeding_until_released()) != 0) return rc;
    if ((rc = test_raised_buffer_retract_still_runs()) != 0) return rc;
    if ((rc = test_printer_driving_this_channel_releases()) != 0) return rc;
    if ((rc = test_auto_unload_gesture_releases()) != 0) return rc;
    if ((rc = test_unplugged_channel_releases()) != 0) return rc;
    if ((rc = test_removal_needs_a_sustained_empty_key()) != 0) return rc;
    if ((rc = test_restart_clears_the_removal_window()) != 0) return rc;
    if ((rc = test_pull_back_empty_debounce()) != 0) return rc;
    return 0;
}
