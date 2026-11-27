// Host test for src/outlet_guard_policy.h.

#include "outlet_guard_policy.h"

#define CHECK(cond, code) do { if (!(cond)) return (code); } while (0)

using namespace outlet_guard;

// 1 count = 1 mm here, to keep the vectors readable.
static const Config kCfg = {
    870,    // send budget
    40,     // use budget
    40,     // slip budget
    12,     // consumption ceiling: 12 counts/s
    5500, 4700,
};

static const uint32_t kDt = 100000u;  // 100 ms samples

// Feed `n` samples of `fwd` counts each. `pushing` only documents the
// vector: the guard does not look at the motor.
static bool run(State& s, uint8_t mode, int n, int32_t fwd, int32_t pct, bool pushing,
                uint32_t& t)
{
    (void)pushing;
    bool changed = false;
    for (int i = 0; i < n; i++)
    {
        t += 100u;
        changed |= sample(s, kCfg, mode, true, fwd, kDt, pct, true);
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

// Tube off mid-print: fast free travel trips within the budget, and the
// latch holds whatever the buffer reads: a detached tube does not come back by
// itself. The printer's unload winds it back.
static int test_detach_trips_and_holds(void)
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
    run(s, MODE_USE, 100, 0, 5000, false, t);       // braked, buffer at rest
    CHECK(blocks_push(s), 23);                      // the drain does not release it
    run(s, MODE_USE, 100, 0, 4000, false, t);       // tension does not resume it
    CHECK(blocks_push(s), 24);
    run(s, MODE_USE, 10, 0, 6000, false, t);        // nor does a pressed buffer
    CHECK(blocks_push(s), 25);
    run(s, MODE_PULL, 4, -5, 5000, false, t);       // a partial unload does not
    CHECK(blocks_push(s), 26);
    run(s, MODE_PULL, 20, -5, 4000, false, t);      // the full one does, buffer pulled
    CHECK(!blocks_push(s), 27);
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
    CHECK(sample(s, kCfg, MODE_USE, true, 0, kDt, 5000, false), 92);
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
    State s; reset(s);
    for (int i = 0; i < 1000; i++)
        sample(s, kCfg, MODE_USE, false, 0, kDt, 5000, true);
    CHECK(!blocks_push(s), 111);
    return 0;
}

// Gear slip on a held strand (spool clamped): the gear turns, the extruder
// keeps pulling, the buffer stays stretched. A slip, braked, raised to the
// printer, held until the printer's next command.
static int test_slip_trips_and_holds(void)
{
    State s; reset(s); uint32_t t = 1000u;
    int turned = 0;
    while (!blocks_push(s) && turned < 200)
    {
        run(s, MODE_USE, 1, 5, 3000, true, t);      // 50 mm/s of gear, buffer at 30 %
        turned += 5;
    }
    CHECK(slipping(s), 121);
    CHECK(turned <= 60, 122);
    run(s, MODE_USE, 100, 0, 3000, false, t);       // braked, still pulled
    CHECK(slipping(s), 123);
    run(s, MODE_USE, 10, -5, 3000, false, t);       // a retract does not end it
    CHECK(slipping(s), 124);
    run(s, MODE_PULL, 1, -5, 3000, false, t);       // the printer's unload does
    CHECK(!blocks_push(s), 125);
    return 0;
}

// A stretched buffer refilled by real travel is not a slip: the buffer comes
// back within its stroke.
static int test_refill_is_not_slip(void)
{
    State s; reset(s); uint32_t t = 1000u;
    for (int k = 0; k < 50; k++)
    {
        run(s, MODE_USE, 3, 8, 3500, true, t);      // 24 mm with the buffer pulled
        run(s, MODE_USE, 20, 1, 5100, true, t);     // back in band, printing
    }
    CHECK(!blocks_push(s), 131);
    return 0;
}

// An extruder taking as much as the BMCU can feed keeps the buffer pulled:
// travel within the consumption ceiling is not a slip.
static int test_feed_limited_is_not_slip(void)
{
    State s; reset(s); uint32_t t = 1000u;
    run(s, MODE_USE, 6000, 1, 4000, true, t);       // 10 mm/s pulled, 10 minutes
    CHECK(!blocks_push(s), 141);
    return 0;
}

// The old loop: an open-outlet latch resumed by the stretched buffer of a
// slip. A pulled buffer never builds an open-outlet excess now.
static int test_slip_is_not_open(void)
{
    State s; reset(s); uint32_t t = 1000u;
    run(s, MODE_USE, 30, 5, 3000, true, t);
    CHECK(slipping(s), 151);
    CHECK(s.excess_q8 == 0, 152);
    return 0;
}

// A send_out ends a slip: the printer feeding again is its decision.
static int test_send_ends_slip(void)
{
    State s; reset(s); uint32_t t = 1000u;
    run(s, MODE_USE, 30, 5, 3000, true, t);
    CHECK(slipping(s), 161);
    run(s, MODE_SEND, 1, 0, 5000, true, t);
    CHECK(!blocks_push(s), 162);
    return 0;
}

// A send that ends in on_use through a short retract starts over too: the
// send's whole-path count must not trip the printing strand (Quint,
// noFalseTrip).
static int test_send_through_pull_resets(void)
{
    State s; reset(s); uint32_t t = 1000u;
    run(s, MODE_SEND, 80, 10, 5000, true, t);       // 800 mm, at the extruder
    run(s, MODE_PULL, 2, -5, 5000, false, t);       // 10 mm back
    run(s, MODE_USE, 1, 1, 5000, true, t);
    CHECK(s.excess_q8 <= 256, 171);
    run(s, MODE_USE, 6, 5, 5000, true, t);          // a stroke-sized refill
    CHECK(!blocks_push(s), 172);
    return 0;
}

// Commands that do not come from a send buy no new budget: on_use / pull
// cycles over an open outlet still trip (Quint, spillBounded).
static int test_use_pull_cycles_keep_count(void)
{
    State s; reset(s); uint32_t t = 1000u;
    for (int i = 0; i < 12 && !blocks_push(s); i++)
    {
        run(s, MODE_USE, 2, 5, 5000, true, t);      // 10 mm free
        run(s, MODE_PULL, 1, 0, 5000, false, t);    // a pull that moves nothing
    }
    CHECK(blocks_push(s), 181);
    return 0;
}

int main(void)
{
    int r;
    if ((r = test_printing_never_trips())) return r;
    if ((r = test_detach_trips_and_holds())) return r;
    if ((r = test_send_budget())) return r;
    if ((r = test_leaving_send_resets())) return r;
    if ((r = test_repeated_send_keeps_count())) return r;
    if ((r = test_press_resets())) return r;
    if ((r = test_retract_clears())) return r;
    if ((r = test_key_empty_clears())) return r;
    if ((r = test_jam_is_not_open())) return r;
    if ((r = test_unmeasured_never_trips())) return r;
    if ((r = test_slip_trips_and_holds())) return r;
    if ((r = test_refill_is_not_slip())) return r;
    if ((r = test_feed_limited_is_not_slip())) return r;
    if ((r = test_slip_is_not_open())) return r;
    if ((r = test_send_ends_slip())) return r;
    if ((r = test_send_through_pull_resets())) return r;
    if ((r = test_use_pull_cycles_keep_count())) return r;
    return 0;
}
