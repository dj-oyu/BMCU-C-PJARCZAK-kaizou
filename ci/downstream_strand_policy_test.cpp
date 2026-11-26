// Host test for src/downstream_strand_policy.h.
// The call sites live in Motion_control.cpp and main.cpp, which do not compile
// on the host. Scenarios follow the flattened state map: PARKED and ORPHAN read
// the same in every other variable, and this mask is what tells them apart.

#include "downstream_strand_policy.h"

#define CHECK(cond, code) do { if (!(cond)) return (code); } while (0)

using namespace downstream_strand;

// Fresh state, and no flash record: nothing is downstream.
static int test_reset_is_empty(void)
{
    State s;
    reset(s);
    for (uint8_t ch = 0u; ch < kChannels; ch++)
    {
        CHECK(!is_set(s, ch), 11);
        CHECK(others(s, ch) == 0u, 12);
    }
    return 0;
}

// Load, then retract: the ordinary cycle ends where it began.
static int test_send_then_retract_returns_to_parked(void)
{
    State s;
    reset(s);
    CHECK(mark_sent(s, 1u), 21);
    CHECK(is_set(s, 1u), 22);
    CHECK(!mark_sent(s, 1u), 23); // already set: nothing to persist
    CHECK(mark_returned(s, 1u), 24);
    CHECK(!is_set(s, 1u), 25);
    CHECK(!mark_returned(s, 1u), 26); // already clear: nothing to persist
    CHECK(s.mask == 0u, 27);
    return 0;
}

// The printer moves on to channel 2 without retracting 0: ORPHAN. The send on
// 2 sees 0 still marked, which is the collision report, and 0 stays marked.
static int test_preempting_send_leaves_the_orphan_marked(void)
{
    State s;
    reset(s);
    CHECK(mark_sent(s, 0u), 31);
    CHECK(others(s, 2u) == 0x01u, 32);
    CHECK(mark_sent(s, 2u), 33);
    CHECK(is_set(s, 0u) && is_set(s, 2u), 34);
    CHECK(others(s, 2u) == 0x01u, 35);
    CHECK(others(s, 0u) == 0x04u, 36);
    // Only channel 2 comes back; the orphan on 0 is still witnessed.
    CHECK(mark_returned(s, 2u), 37);
    CHECK(s.mask == 0x01u, 38);
    return 0;
}

// A channel never counts against itself.
static int test_others_excludes_self(void)
{
    State s;
    reset(s);
    mark_sent(s, 3u);
    CHECK(others(s, 3u) == 0u, 41);
    CHECK(others(s, 1u) == 0x08u, 42);
    return 0;
}

// Out-of-range channels are inert in every operation.
static int test_out_of_range_channel_is_ignored(void)
{
    State s;
    reset(s);
    CHECK(!mark_sent(s, 4u), 51);
    CHECK(!mark_sent(s, 0xFFu), 52);
    CHECK(s.mask == 0u, 53);
    mark_sent(s, 0u);
    CHECK(!mark_returned(s, 0xFFu), 54);
    CHECK(s.mask == 0x01u, 55);
    CHECK(!is_set(s, 7u), 56);
    CHECK(others(s, 0xFFu) == 0x01u, 57);
    return 0;
}

// Flash restore keeps channel bits and discards the rest.
static int test_restore_masks_to_four_channels(void)
{
    State s;
    reset(s);
    restore(s, 0xA5u);
    CHECK(s.mask == 0x05u, 61);
    CHECK(is_set(s, 0u) && is_set(s, 2u), 62);
    CHECK(!is_set(s, 1u) && !is_set(s, 3u), 63);
    restore(s, 0u);
    CHECK(s.mask == 0u, 64);
    return 0;
}

int main(void)
{
    int rc = 0;
    if ((rc = test_reset_is_empty()) != 0) return rc;
    if ((rc = test_send_then_retract_returns_to_parked()) != 0) return rc;
    if ((rc = test_preempting_send_leaves_the_orphan_marked()) != 0) return rc;
    if ((rc = test_others_excludes_self()) != 0) return rc;
    if ((rc = test_out_of_range_channel_is_ignored()) != 0) return rc;
    if ((rc = test_restore_masks_to_four_channels()) != 0) return rc;
    return 0;
}
