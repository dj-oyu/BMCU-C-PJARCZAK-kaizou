// Host test for src/signal_hold_policy.h.
// The two call sites it replaces live in Motion_control.cpp, which does not
// compile on the host, so this is where their arithmetic gets checked.

#include "signal_hold_policy.h"

#define CHECK(cond, code) do { if (!(cond)) return (code); } while (0)

static const uint32_t kHold = 100u; // DM_LOADED_DROP_MS

// A single pass of the condition must not fire, and the window it opened must
// not survive the condition going away.
static int test_blip_does_not_fire(void)
{
    uint32_t since = 0u;

    CHECK(!signal_hold::held_for(since, false, 1000u, kHold), 11);
    CHECK(since == 0u, 12);

    CHECK(!signal_hold::held_for(since, true, 1001u, kHold), 13);
    CHECK(since == 1001u, 14);

    CHECK(!signal_hold::held_for(since, false, 1002u, kHold), 15);
    CHECK(since == 0u, 16);

    // A long quiet period afterwards must not fire a stale window.
    CHECK(!signal_hold::held_for(since, false, 60000u, kHold), 17);
    return 0;
}

// Fires exactly on the boundary, exactly once, then starts a fresh window.
static int test_fires_once_at_the_boundary(void)
{
    uint32_t since = 0u;

    CHECK(!signal_hold::held_for(since, true, 10000u, kHold), 21);
    for (uint32_t t = 10001u; t < 10100u; ++t)
        CHECK(!signal_hold::held_for(since, true, t, kHold), 22);

    CHECK(signal_hold::held_for(since, true, 10100u, kHold), 23);
    CHECK(since == 0u, 24);

    // Held continuously past the fire: a new window opens rather than the old
    // one re-firing every pass.
    CHECK(!signal_hold::held_for(since, true, 10101u, kHold), 25);
    CHECK(!signal_hold::held_for(since, true, 10200u, kHold), 26);
    CHECK(signal_hold::held_for(since, true, 10201u, kHold), 27);
    return 0;
}

// The hold is continuous, not cumulative.
static int test_hold_is_continuous_not_cumulative(void)
{
    uint32_t since = 0u;

    for (uint32_t cycle = 0u; cycle < 50u; ++cycle)
    {
        const uint32_t base = 1000u + cycle * 500u;
        for (uint32_t t = base; t < base + 90u; t += 10u)
            CHECK(!signal_hold::held_for(since, true, t, kHold), 31);
        CHECK(!signal_hold::held_for(since, false, base + 90u, kHold), 32);
    }
    return 0;
}

// now_ms is a free-running 32-bit counter; a window straddling its wrap must
// still time the full hold and neither fire early nor hang.
static int test_millisecond_counter_wrap(void)
{
    uint32_t since = 0u;

    CHECK(!signal_hold::held_for(since, true, 0xFFFFFFC0u, kHold), 41);
    CHECK(!signal_hold::held_for(since, true, 0x00000000u, kHold), 42); // 64 ms
    CHECK(!signal_hold::held_for(since, true, 0x00000023u, kHold), 43); // 99 ms
    CHECK(signal_hold::held_for(since, true, 0x00000024u, kHold), 44);  // 100 ms
    return 0;
}

// The inherited zero sentinel, asserted rather than left to be discovered. A
// stamp that would land on now_ms == 0 reads as "not timing" next pass and
// restarts, costing one pass. Preserving this exactly is what makes the
// substitution in Motion_control.cpp provably neutral.
static int test_zero_sentinel_quirk_is_preserved(void)
{
    uint32_t since = 0u;

    CHECK(!signal_hold::held_for(since, true, 0u, kHold), 51);
    CHECK(since == 0u, 52); // the stamp is indistinguishable from "idle"

    // So the window effectively begins on the following pass.
    CHECK(!signal_hold::held_for(since, true, 1u, kHold), 53);
    CHECK(since == 1u, 54);
    CHECK(!signal_hold::held_for(since, true, 100u, kHold), 55);
    CHECK(signal_hold::held_for(since, true, 101u, kHold), 56);
    return 0;
}

// The second call site uses a different limit off the same primitive.
static int test_limit_is_per_call(void)
{
    uint32_t since = 0u;
    const uint32_t empty_ms = 1500u; // AUTO_UNLOAD_EMPTY_MS

    CHECK(!signal_hold::held_for(since, true, 5000u, empty_ms), 61);
    CHECK(!signal_hold::held_for(since, true, 6499u, empty_ms), 62);
    CHECK(signal_hold::held_for(since, true, 6500u, empty_ms), 63);
    return 0;
}

int main(void)
{
    int rc = 0;
    if ((rc = test_blip_does_not_fire()) != 0) return rc;
    if ((rc = test_fires_once_at_the_boundary()) != 0) return rc;
    if ((rc = test_hold_is_continuous_not_cumulative()) != 0) return rc;
    if ((rc = test_millisecond_counter_wrap()) != 0) return rc;
    if ((rc = test_zero_sentinel_quirk_is_preserved()) != 0) return rc;
    if ((rc = test_limit_is_per_call()) != 0) return rc;
    return 0;
}
