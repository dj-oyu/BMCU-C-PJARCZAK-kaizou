// Host test for src/on_use_jam_policy.h.

#include "on_use_jam_policy.h"

#define CHECK(cond, code) do { if (!(cond)) return (code); } while (0)

static const uint32_t kHold = 300u;

// The grip transient: a few low samples, then recovery. Must never latch.
static int test_transient_does_not_latch(void)
{
    uint32_t since = 0u;
    for (uint32_t t = 1000u; t < 1250u; t += 5u)
        CHECK(!on_use_jam::low_reading_latches(since, 1500, t, kHold), 11);
    CHECK(!on_use_jam::low_reading_latches(since, 5000, 1250u, kHold), 12);
    CHECK(since == 0u, 13);
    // A fresh dip starts a fresh window rather than continuing the old one.
    CHECK(!on_use_jam::low_reading_latches(since, 1500, 1260u, kHold), 14);
    CHECK(!on_use_jam::low_reading_latches(since, 1500, 1500u, kHold), 15);
    return 0;
}

// A sustained low reading is a jam and latches once the hold elapses.
static int test_sustained_low_latches(void)
{
    uint32_t since = 0u;
    CHECK(!on_use_jam::low_reading_latches(since, 3999, 2000u, kHold), 21);
    CHECK(!on_use_jam::low_reading_latches(since, 3999, 2299u, kHold), 22);
    CHECK(on_use_jam::low_reading_latches(since, 3999, 2300u, kHold), 23);
    return 0;
}

// The threshold itself is unchanged: 40.00 % is not low.
static int test_threshold_boundary(void)
{
    uint32_t since = 0u;
    CHECK(!on_use_jam::low_reading_latches(since, 4000, 1u, 0u), 31);
    CHECK(on_use_jam::low_reading_latches(since, 3999, 1u, 0u), 32);
    return 0;
}

// hold 0 is the old behaviour: the first low sample latches.
static int test_zero_hold_is_legacy(void)
{
    uint32_t since = 0u;
    CHECK(on_use_jam::low_reading_latches(since, 100, 5000u, 0u), 41);
    CHECK(since == 0u, 42);
    return 0;
}

// A window interrupted by a reset (key empty / left on_use) must not survive.
static int test_reset_discards_window(void)
{
    uint32_t since = 0u;
    CHECK(!on_use_jam::low_reading_latches(since, 100, 7000u, kHold), 51);
    on_use_jam::reset(since);
    CHECK(!on_use_jam::low_reading_latches(since, 100, 7400u, kHold), 52);
    CHECK(!on_use_jam::low_reading_latches(since, 100, 7699u, kHold), 53);
    CHECK(on_use_jam::low_reading_latches(since, 100, 7700u, kHold), 54);
    return 0;
}

int main(void)
{
    int rc;
    if ((rc = test_transient_does_not_latch()) != 0) return rc;
    if ((rc = test_sustained_low_latches()) != 0) return rc;
    if ((rc = test_threshold_boundary()) != 0) return rc;
    if ((rc = test_zero_hold_is_legacy()) != 0) return rc;
    if ((rc = test_reset_discards_window()) != 0) return rc;
    return 0;
}
