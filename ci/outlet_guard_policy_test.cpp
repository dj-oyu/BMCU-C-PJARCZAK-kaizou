// Host test for src/outlet_guard_policy.h.

#include "outlet_guard_policy.h"

#define CHECK(cond, code) do { if (!(cond)) return (code); } while (0)

using namespace outlet_guard;

// 1 count = 1 mm here, to keep the vectors readable.
static const Config kCfg = {
    870,    // send budget
    40,     // use budget
    12,     // consumption ceiling: 12 counts/s
    5500, 4700, 4000, 3000u,
};

static const uint32_t kDt = 100000u;  // 100 ms samples

// Feed `n` samples of `fwd` counts each.
static bool run(State& s, uint8_t mode, int n, int32_t fwd, int32_t pct, bool pushing,
                uint32_t& t)
{
    bool changed = false;
    for (int i = 0; i < n; i++)
    {
        t += 100u;
        changed |= sample(s, kCfg, mode, true, fwd, kDt, pct, pushing, true, t);
    }
    return changed;
}

// Printing: travel at or below the consumption ceiling never adds up.
static int test_printing_never_trips(void)
{
    State s; reset(s); uint32_t t = 1000u;
    run(s, MODE_USE, 6000, 1, 5100, true, t);       // 10 mm/s for 10 minutes
    CHECK(!blocks_push(s), 11);
    // A buffer refill burst, stroke-sized, is absorbed by the budget.
    run(s, MODE_USE, 6, 5, 5000, true, t);          // 30 mm in 0.6 s
    CHECK(!blocks_push(s), 12);
    return 0;
}

// Tube off mid-print: fast free travel trips within the budget.
static int test_detach_trips_and_resumes_on_tension(void)
{
    State s; reset(s); uint32_t t = 1000u;
    int spilled = 0;
    while (!blocks_push(s) && spilled < 200)
    {
        run(s, MODE_USE, 1, 5, 5000, true, t);      // 50 mm/s free
        spilled += 5;
    }
    CHECK(blocks_push(s), 21);
    CHECK(spilled <= 60, 22);                       // 40 + drain over the trip time
    // Braked, the printer eats the slack: nothing moves, buffer at rest.
    run(s, MODE_USE, 100, 0, 5000, false, t);
    CHECK(blocks_push(s), 23);                      // the drain does not release it
    // Tube reseated: the extruder pulls on the strand.
    run(s, MODE_USE, 1, 0, 4600, false, t);
    CHECK(!blocks_push(s), 24);
    // The jam detector is disarmed while the buffer is still low ...
    CHECK(jam_disarmed(s, kCfg, 3500, t + 10u), 25);
    // ... until it is back above the jam threshold.
    CHECK(!jam_disarmed(s, kCfg, 4100, t + 20u), 26);
    CHECK(!jam_disarmed(s, kCfg, 3500, t + 30u), 27);
    return 0;
}

// The disarm is bounded in time: a real jam right after a resume still latches.
static int test_disarm_times_out(void)
{
    State s; reset(s); uint32_t t = 1000u;
    run(s, MODE_USE, 20, 5, 5000, true, t);
    CHECK(blocks_push(s), 31);
    run(s, MODE_USE, 1, 0, 4600, false, t);
    CHECK(!blocks_push(s), 32);
    CHECK(jam_disarmed(s, kCfg, 3000, t + 2999u), 33);
    CHECK(!jam_disarmed(s, kCfg, 3000, t + 3000u), 34);
    return 0;
}

// send_out: the path is free, its whole length is allowed; past it, not.
static int test_send_budget(void)
{
    State s; reset(s); uint32_t t = 1000u;
    run(s, MODE_SEND, 85, 10, 5000, true, t);       // 850 mm
    CHECK(!blocks_push(s), 41);
    run(s, MODE_SEND, 3, 10, 5000, true, t);        // 880 mm
    CHECK(blocks_push(s), 42);
    return 0;
}

// The printer's own arrival (before_on_use / on_use) ends the send stretch.
static int test_leaving_send_resets(void)
{
    State s; reset(s); uint32_t t = 1000u;
    run(s, MODE_SEND, 60, 10, 5000, true, t);       // 600 mm, not yet pressed
    run(s, MODE_USE, 1, 1, 5300, true, t);
    CHECK(!blocks_push(s), 51);
    CHECK(s.excess_q8 <= 256, 52);
    return 0;
}

// A repeated send_out does not buy another budget.
static int test_repeated_send_keeps_count(void)
{
    State s; reset(s); uint32_t t = 1000u;
    run(s, MODE_SEND, 60, 10, 5000, true, t);
    run(s, MODE_SEND, 30, 10, 5000, true, t);
    CHECK(blocks_push(s), 61);
    return 0;
}

// Pressed buffer is contact: the count restarts.
static int test_press_resets(void)
{
    State s; reset(s); uint32_t t = 1000u;
    run(s, MODE_USE, 6, 5, 5000, true, t);          // 30 mm
    run(s, MODE_USE, 1, 1, 5600, true, t);
    CHECK(s.excess_q8 == 0, 71);
    run(s, MODE_USE, 6, 5, 5000, true, t);
    CHECK(!blocks_push(s), 72);
    return 0;
}

// A retract that winds the spill back clears the latch; a partial one does not.
static int test_retract_clears(void)
{
    State s; reset(s); uint32_t t = 1000u;
    run(s, MODE_SEND, 88, 10, 5000, true, t);
    CHECK(blocks_push(s), 81);
    run(s, MODE_PULL, 40, -10, 5000, false, t);
    CHECK(blocks_push(s), 82);
    run(s, MODE_PULL, 60, -10, 5000, false, t);
    CHECK(!blocks_push(s), 83);
    return 0;
}

// Key empty clears everything.
static int test_key_empty_clears(void)
{
    State s; reset(s); uint32_t t = 1000u;
    run(s, MODE_USE, 20, 5, 5000, true, t);
    CHECK(blocks_push(s), 91);
    CHECK(sample(s, kCfg, MODE_USE, true, 0, kDt, 5000, false, false, t + 100u), 92);
    CHECK(!blocks_push(s), 93);
    return 0;
}

// A jam (drive, no travel) never adds to the count.
static int test_jam_is_not_open(void)
{
    State s; reset(s); uint32_t t = 1000u;
    run(s, MODE_USE, 600, 0, 3000, true, t);
    CHECK(!blocks_push(s), 101);
    CHECK(s.excess_q8 == 0, 102);
    return 0;
}

// No encoder, no verdict.
static int test_unmeasured_never_trips(void)
{
    State s; reset(s); uint32_t t = 1000u;
    for (int i = 0; i < 1000; i++)
    {
        t += 100u;
        sample(s, kCfg, MODE_USE, false, 0, kDt, 5000, true, true, t);
    }
    CHECK(!blocks_push(s), 111);
    return 0;
}

int main(void)
{
    int r;
    if ((r = test_printing_never_trips())) return r;
    if ((r = test_detach_trips_and_resumes_on_tension())) return r;
    if ((r = test_disarm_times_out())) return r;
    if ((r = test_send_budget())) return r;
    if ((r = test_leaving_send_resets())) return r;
    if ((r = test_repeated_send_keeps_count())) return r;
    if ((r = test_press_resets())) return r;
    if ((r = test_retract_clears())) return r;
    if ((r = test_key_empty_clears())) return r;
    if ((r = test_jam_is_not_open())) return r;
    if ((r = test_unmeasured_never_trips())) return r;
    return 0;
}
