// Host test for src/ams_merger_policy.h: the merger mutex transition table.

#include "ams_merger_policy.h"

using ams_merger::State;
using ams_merger::kNoChannel;

#define CHECK(cond, code) do { if (!(cond)) return (code); } while (0)

static bool is(const State& s, uint8_t stage, uint8_t owner)
{
    return ams_merger::stage(s) == stage && s.owner == owner;
}

// The full table, one edge per assertion. Rows are the state, columns are the
// three inputs: a printer load, a printer release, and the debounced key-zero.
static int test_transition_table(void)
{
    State s;

    // --- from UNLOADED ---
    ams_merger::reset(s);
    CHECK(is(s, ams_merger::stage_unloaded, kNoChannel), 11);

    // load -> LOADED(ch)
    CHECK(ams_merger::acquire(s, 2u), 12);
    CHECK(is(s, ams_merger::stage_loaded, 2u), 13);

    // release from UNLOADED is a no-op, not an error
    ams_merger::reset(s);
    CHECK(!ams_merger::release(s, 2u), 14);
    CHECK(!ams_merger::release(s, kNoChannel), 15);
    CHECK(is(s, ams_merger::stage_unloaded, kNoChannel), 16);

    // key-zero cannot invent an owner
    CHECK(!ams_merger::to_tail(s, 2u), 17);
    CHECK(is(s, ams_merger::stage_unloaded, kNoChannel), 18);

    // --- from LOADED(2) ---
    ams_merger::reset(s);
    ams_merger::acquire(s, 2u);

    // re-load of the owner changes nothing
    CHECK(!ams_merger::acquire(s, 2u), 21);
    CHECK(is(s, ams_merger::stage_loaded, 2u), 22);

    // load of a different channel is refused, as before this state existed
    CHECK(!ams_merger::acquire(s, 0u), 23);
    CHECK(is(s, ams_merger::stage_loaded, 2u), 24);

    // key-zero -> TAIL(2), NOT UNLOADED. This is the defect.
    CHECK(ams_merger::to_tail(s, 2u), 25);
    CHECK(is(s, ams_merger::stage_tail, 2u), 26);

    // key-zero named at a channel that does not own is ignored
    ams_merger::reset(s);
    ams_merger::acquire(s, 2u);
    CHECK(!ams_merger::to_tail(s, 1u), 27);
    CHECK(is(s, ams_merger::stage_loaded, 2u), 28);

    // release by the owner, and release addressed to nobody in particular
    ams_merger::reset(s);
    ams_merger::acquire(s, 2u);
    CHECK(ams_merger::release(s, 2u), 29);
    CHECK(is(s, ams_merger::stage_unloaded, kNoChannel), 30);

    ams_merger::reset(s);
    ams_merger::acquire(s, 2u);
    CHECK(ams_merger::release(s, kNoChannel), 31);
    CHECK(is(s, ams_merger::stage_unloaded, kNoChannel), 32);

    // release addressed to a channel that does not own leaves the owner alone
    ams_merger::reset(s);
    ams_merger::acquire(s, 2u);
    CHECK(!ams_merger::release(s, 1u), 33);
    CHECK(is(s, ams_merger::stage_loaded, 2u), 34);

    // --- from TAIL(2) ---
    ams_merger::reset(s);
    ams_merger::acquire(s, 2u);
    ams_merger::to_tail(s, 2u);

    // a repeat key-zero is idle: TAIL is where key-zero is the normal reading
    CHECK(!ams_merger::to_tail(s, 2u), 41);
    CHECK(is(s, ams_merger::stage_tail, 2u), 42);

    // the owner reloading promotes back to LOADED
    CHECK(ams_merger::acquire(s, 2u), 43);
    CHECK(is(s, ams_merger::stage_loaded, 2u), 44);

    // another channel still cannot take a merger held in TAIL
    ams_merger::reset(s);
    ams_merger::acquire(s, 2u);
    ams_merger::to_tail(s, 2u);
    CHECK(!ams_merger::acquire(s, 3u), 45);
    CHECK(is(s, ams_merger::stage_tail, 2u), 46);

    // and the only exits: a release by the owner, or an unaddressed release
    CHECK(ams_merger::release(s, 2u), 47);
    CHECK(is(s, ams_merger::stage_unloaded, kNoChannel), 48);

    ams_merger::reset(s);
    ams_merger::acquire(s, 2u);
    ams_merger::to_tail(s, 2u);
    CHECK(ams_merger::release(s, kNoChannel), 49);
    CHECK(is(s, ams_merger::stage_unloaded, kNoChannel), 50);

    // a release addressed elsewhere does not free a TAIL either
    ams_merger::reset(s);
    ams_merger::acquire(s, 2u);
    ams_merger::to_tail(s, 2u);
    CHECK(!ams_merger::release(s, 0u), 51);
    CHECK(is(s, ams_merger::stage_tail, 2u), 52);

    return 0;
}

// The property the whole change turns on: every "does ch hold the merger" test
// in bambu_bus_ams.cpp is `ams_state_get_loaded() == ch`, and it must answer
// yes in TAIL, or the runout retract is refused exactly as it is today.
static int test_ownership_survives_the_tail(void)
{
    State s;
    ams_merger::reset(s);
    ams_merger::acquire(s, 1u);
    ams_merger::to_tail(s, 1u);

    // allow_stop
    CHECK(s.owner == 1u, 61);
    CHECK(ams_merger::owns(s, 1u), 62);
    // allow_any, for the owner and for anyone else
    CHECK(!ams_merger::is_free(s), 63);
    CHECK(!ams_merger::owns(s, 0u), 64);
    CHECK(!ams_merger::owns(s, 2u), 65);
    CHECK(!ams_merger::owns(s, 3u), 66);

    return 0;
}

// Out-of-range inputs must never produce an owner, in either mutator.
static int test_out_of_range_inputs(void)
{
    State s;

    ams_merger::reset(s);
    CHECK(!ams_merger::acquire(s, 4u), 71);
    CHECK(!ams_merger::acquire(s, 0xFFu), 72);
    CHECK(is(s, ams_merger::stage_unloaded, kNoChannel), 73);

    ams_merger::reset(s);
    ams_merger::acquire(s, 0u);
    CHECK(!ams_merger::to_tail(s, 4u), 74);
    CHECK(!ams_merger::to_tail(s, 0xFFu), 75);
    CHECK(is(s, ams_merger::stage_loaded, 0u), 76);

    return 0;
}

// Boot restore. The two halves come from flash separately -- the channel from
// the STA record, which keeps the meaning it has always had, and the tail from
// a record tag older firmware skips.
static int test_boot_restore(void)
{
    State s;

    for (uint8_t ch = 0u; ch < 4u; ++ch)
    {
        ams_merger::restore(s, ch, false);
        CHECK(is(s, ams_merger::stage_loaded, ch), 81);

        ams_merger::restore(s, ch, true);
        CHECK(is(s, ams_merger::stage_tail, ch), 82);
        // and the point of restoring it at all: the retract is still accepted.
        CHECK(ams_merger::owns(s, ch), 83);
    }

    // The free record, and every other byte the channel field could hold. None
    // of them may leave the merger owned, and in particular a tail flag with a
    // rejected channel must not survive as an owner nobody can release --
    // main.cpp assigns this byte before it range-checks it.
    for (uint32_t raw = 4u; raw <= 0xFFu; ++raw)
    {
        ams_merger::restore(s, (uint8_t)raw, false);
        CHECK(is(s, ams_merger::stage_unloaded, kNoChannel), 84);

        ams_merger::restore(s, (uint8_t)raw, true);
        CHECK(is(s, ams_merger::stage_unloaded, kNoChannel), 85);
        CHECK(!ams_merger::is_tail(s), 86);

        // No byte outside 0..3 may match a channel gate.
        for (uint8_t ch = 0u; ch < 4u; ++ch)
            CHECK(!ams_merger::owns(s, ch), 87);
    }

    return 0;
}

// The runout sequence end to end, as a state trace. This is the user-visible
// failure: automatic switchover to a paired spool.
static int test_runout_sequence(void)
{
    State s;
    ams_merger::reset(s);

    // Printer loads channel 0 and prints from it.
    CHECK(ams_merger::acquire(s, 0u), 101);
    CHECK(ams_merger::owns(s, 0u), 102);

    // The spool runs out: the tail passes the online key first.
    CHECK(ams_merger::to_tail(s, 0u), 103);

    // The printer's retract for channel 0 arrives afterwards. allow_stop must
    // still be true or before_pull_back is refused and nothing moves.
    CHECK(ams_merger::owns(s, 0u), 104);

    // before_pull_back releases the merger, as it always did.
    CHECK(ams_merger::release(s, 0u), 105);
    CHECK(ams_merger::is_free(s), 106);

    // The paired spool on channel 1 loads.
    CHECK(ams_merger::acquire(s, 1u), 107);
    CHECK(ams_merger::owns(s, 1u), 108);

    return 0;
}

// The same, but the printer switches spools without retracting first: send_out
// on another channel is accepted unconditionally and releases whatever is held.
// This is the escape hatch that stops a wrongly-entered TAIL being permanent.
static int test_send_out_frees_a_held_tail(void)
{
    State s;
    ams_merger::reset(s);
    ams_merger::acquire(s, 3u);
    ams_merger::to_tail(s, 3u);

    // send_out's release is addressed to nobody: ams_state_set_unloaded(0xFF).
    CHECK(ams_merger::release(s, kNoChannel), 111);
    CHECK(ams_merger::is_free(s), 112);
    CHECK(ams_merger::acquire(s, 1u), 113);

    return 0;
}

int main(void)
{
    int rc = 0;
    if ((rc = test_transition_table()) != 0) return rc;
    if ((rc = test_ownership_survives_the_tail()) != 0) return rc;
    if ((rc = test_out_of_range_inputs()) != 0) return rc;
    if ((rc = test_boot_restore()) != 0) return rc;
    if ((rc = test_runout_sequence()) != 0) return rc;
    if ((rc = test_send_out_frees_a_held_tail()) != 0) return rc;
    return 0;
}
