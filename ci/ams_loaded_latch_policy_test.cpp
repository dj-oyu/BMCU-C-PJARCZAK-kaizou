// Host test for src/ams_loaded_latch_policy.h.
// All timestamps below are milliseconds, matching the firmware's now_ms.

#include "ams_loaded_latch_policy.h"

using ams_loaded_latch::State;
using ams_loaded_latch::kNoChannel;

static const uint32_t kHold = 1500u; // LOADED_LATCH_DROP_MS

#define CHECK(cond, code) do { if (!(cond)) return (code); } while (0)

// The defect this exists to close: one pass of key-zero must not clear.
static int test_single_pass_blip_does_not_clear(void)
{
    State s;
    ams_loaded_latch::reset(s);

    CHECK(!ams_loaded_latch::poll(s, 1u, false, 1000u, kHold), 11);
    CHECK(!ams_loaded_latch::is_pending(s), 12);

    CHECK(!ams_loaded_latch::poll(s, 1u, true, 1001u, kHold), 13);
    CHECK(ams_loaded_latch::is_pending(s), 14);

    // Key comes back on the very next pass. Window is discarded.
    CHECK(!ams_loaded_latch::poll(s, 1u, false, 1002u, kHold), 15);
    CHECK(!ams_loaded_latch::is_pending(s), 16);

    // And a long quiet period afterwards must not fire a stale window.
    CHECK(!ams_loaded_latch::poll(s, 1u, false, 60000u, kHold), 17);
    return 0;
}

// A sustained key-zero must clear, or a withdrawn channel stays loaded forever.
static int test_sustained_zero_clears_once(void)
{
    State s;
    ams_loaded_latch::reset(s);

    CHECK(!ams_loaded_latch::poll(s, 2u, true, 10000u, kHold), 21);

    // Sampled at the ~1.2 ms cadence the ADC actually refreshes at.
    for (uint32_t t = 10001u; t < 11500u; t += 1u)
        CHECK(!ams_loaded_latch::poll(s, 2u, true, t, kHold), 22);

    CHECK(ams_loaded_latch::poll(s, 2u, true, 11500u, kHold), 23);

    // Exactly once: the caller has now cleared the latch, and a repeat pass
    // with the latch still reported (should not happen) must not re-fire from
    // the same window.
    CHECK(!ams_loaded_latch::poll(s, 2u, true, 11501u, kHold), 24);
    return 0;
}

// The hold is continuous, not cumulative. Chatter that never stays low for the
// full window never clears, however long it goes on.
static int test_hold_is_continuous_not_cumulative(void)
{
    State s;
    ams_loaded_latch::reset(s);

    for (uint32_t cycle = 0u; cycle < 50u; ++cycle)
    {
        const uint32_t base = 1000u + cycle * 2000u;
        for (uint32_t t = base; t < base + 1400u; t += 100u)
            CHECK(!ams_loaded_latch::poll(s, 0u, true, t, kHold), 31);
        CHECK(!ams_loaded_latch::poll(s, 0u, false, base + 1400u, kHold), 32);
    }
    return 0;
}

// A printer-commanded clear happens outside this policy: the caller simply
// stops reporting a latched channel. The pending window must not survive it and
// bite the next channel to be latched.
static int test_external_clear_discards_window(void)
{
    State s;
    ams_loaded_latch::reset(s);

    CHECK(!ams_loaded_latch::poll(s, 3u, true, 5000u, kHold), 41);
    CHECK(ams_loaded_latch::is_pending(s), 42);

    // Printer unload cleared the latch; nothing is latched now.
    CHECK(!ams_loaded_latch::poll(s, kNoChannel, false, 5100u, kHold), 43);
    CHECK(!ams_loaded_latch::is_pending(s), 44);

    // Channel 0 is loaded and immediately reads empty. It gets a full window of
    // its own, not the remains of channel 3's.
    CHECK(!ams_loaded_latch::poll(s, 0u, true, 5200u, kHold), 45);
    CHECK(!ams_loaded_latch::poll(s, 0u, true, 6600u, kHold), 46);
    CHECK(ams_loaded_latch::poll(s, 0u, true, 6700u, kHold), 47);
    return 0;
}

// The same, without an intervening unlatched pass: the latched channel changes
// straight from 3 to 0.
static int test_channel_change_restarts_window(void)
{
    State s;
    ams_loaded_latch::reset(s);

    CHECK(!ams_loaded_latch::poll(s, 3u, true, 5000u, kHold), 51);
    CHECK(!ams_loaded_latch::poll(s, 0u, true, 6400u, kHold), 52);
    CHECK(s.channel == 0u, 53);
    CHECK(!ams_loaded_latch::poll(s, 0u, true, 7800u, kHold), 54);
    CHECK(ams_loaded_latch::poll(s, 0u, true, 7900u, kHold), 55);
    return 0;
}

// now_ms is a free-running 32-bit millisecond counter; a window straddling its
// wrap must still time 1500 ms and not fire early or hang.
static int test_millisecond_counter_wrap(void)
{
    State s;
    ams_loaded_latch::reset(s);

    CHECK(!ams_loaded_latch::poll(s, 1u, true, 0xFFFFFC00u, kHold), 61);
    CHECK(!ams_loaded_latch::poll(s, 1u, true, 0x00000000u, kHold), 62); // 1024 ms
    CHECK(!ams_loaded_latch::poll(s, 1u, true, 0x000001DBu, kHold), 63); // 1499 ms
    CHECK(ams_loaded_latch::poll(s, 1u, true, 0x000001DCu, kHold), 64);  // 1500 ms
    return 0;
}

// An out-of-range latch value is treated as nothing latched, so a bad read can
// never start a window against a channel that does not exist.
static int test_out_of_range_channel_is_not_latched(void)
{
    State s;
    ams_loaded_latch::reset(s);

    CHECK(!ams_loaded_latch::poll(s, 4u, true, 1000u, kHold), 71);
    CHECK(!ams_loaded_latch::is_pending(s), 72);
    CHECK(!ams_loaded_latch::poll(s, 0xFEu, true, 9000u, kHold), 73);
    CHECK(!ams_loaded_latch::is_pending(s), 74);
    return 0;
}

int main(void)
{
    int rc = 0;
    if ((rc = test_single_pass_blip_does_not_clear()) != 0) return rc;
    if ((rc = test_sustained_zero_clears_once()) != 0) return rc;
    if ((rc = test_hold_is_continuous_not_cumulative()) != 0) return rc;
    if ((rc = test_external_clear_discards_window()) != 0) return rc;
    if ((rc = test_channel_change_restarts_window()) != 0) return rc;
    if ((rc = test_millisecond_counter_wrap()) != 0) return rc;
    if ((rc = test_out_of_range_channel_is_not_latched()) != 0) return rc;
    return 0;
}
