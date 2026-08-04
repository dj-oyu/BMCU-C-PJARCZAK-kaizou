// Host test for src/ams_merger_policy.h: the merger mutex transition table.

#include "ams_merger_policy.h"
#include "ams_loaded_latch_policy.h"

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
    CHECK(ams_merger::release(s, 2u) == ams_merger::release_none, 14);
    CHECK(ams_merger::release(s, kNoChannel) == ams_merger::release_none, 15);
    CHECK(is(s, ams_merger::stage_unloaded, kNoChannel), 16);

    // key-zero cannot invent an owner
    CHECK(!ams_merger::to_tail(s), 17);
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
    CHECK(ams_merger::to_tail(s), 25);
    CHECK(is(s, ams_merger::stage_tail, 2u), 26);

    // release by the owner, and release addressed to nobody in particular
    ams_merger::reset(s);
    ams_merger::acquire(s, 2u);
    CHECK(ams_merger::release(s, 2u) == ams_merger::release_done, 29);
    CHECK(is(s, ams_merger::stage_unloaded, kNoChannel), 30);

    ams_merger::reset(s);
    ams_merger::acquire(s, 2u);
    CHECK(ams_merger::release(s, kNoChannel) == ams_merger::release_done, 31);
    CHECK(is(s, ams_merger::stage_unloaded, kNoChannel), 32);

    // release addressed to a channel that does not own leaves the owner alone
    ams_merger::reset(s);
    ams_merger::acquire(s, 2u);
    CHECK(ams_merger::release(s, 1u) == ams_merger::release_none, 33);
    CHECK(is(s, ams_merger::stage_loaded, 2u), 34);

    // --- from TAIL(2) ---
    ams_merger::reset(s);
    ams_merger::acquire(s, 2u);
    ams_merger::to_tail(s);

    // a repeat key-zero is idle: TAIL is where key-zero is the normal reading
    CHECK(!ams_merger::to_tail(s), 41);
    CHECK(is(s, ams_merger::stage_tail, 2u), 42);

    // the owner reloading promotes back to LOADED
    CHECK(ams_merger::acquire(s, 2u), 43);
    CHECK(is(s, ams_merger::stage_loaded, 2u), 44);

    // another channel still cannot take a merger held in TAIL
    ams_merger::reset(s);
    ams_merger::acquire(s, 2u);
    ams_merger::to_tail(s);
    CHECK(!ams_merger::acquire(s, 3u), 45);
    CHECK(is(s, ams_merger::stage_tail, 2u), 46);

    // the only exit is a release that names the owner
    CHECK(ams_merger::release(s, 2u) == ams_merger::release_done, 47);
    CHECK(is(s, ams_merger::stage_unloaded, kNoChannel), 48);

    // A wildcard release must NOT free a TAIL. This is the idle-frame reset at
    // bambu_bus_ams.cpp:458, which keeps arriving while a printer sits paused
    // mid-runout. Letting it through loses the merger during the pause, and
    // the morning's retract prelude is then refused for want of allow_stop.
    ams_merger::reset(s);
    ams_merger::acquire(s, 2u);
    ams_merger::to_tail(s);
    CHECK(ams_merger::release(s, kNoChannel) == ams_merger::release_refused_tail, 49);
    CHECK(is(s, ams_merger::stage_tail, 2u), 50);

    // A session release still frees a plain LOADED, which is what it is for.
    ams_merger::reset(s);
    ams_merger::acquire(s, 2u);
    CHECK(ams_merger::release(s, kNoChannel) == ams_merger::release_done, 55);
    CHECK(is(s, ams_merger::stage_unloaded, kNoChannel), 56);

    // A preempt frees a TAIL where a session release does not. This is the
    // only escape from a stale TAIL, and the reason TAIL may have no timeout.
    ams_merger::reset(s);
    ams_merger::acquire(s, 2u);
    ams_merger::to_tail(s);
    CHECK(ams_merger::preempt(s) == ams_merger::release_preempted_tail, 57);
    CHECK(is(s, ams_merger::stage_unloaded, kNoChannel), 58);

    // Preempting a plain LOADED is an ordinary release, not a collision.
    ams_merger::reset(s);
    ams_merger::acquire(s, 2u);
    CHECK(ams_merger::preempt(s) == ams_merger::release_done, 59);
    // Preempting a free merger is a no-op.
    CHECK(ams_merger::preempt(s) == ams_merger::release_none, 60);

    // a release addressed elsewhere does not free a TAIL either
    ams_merger::reset(s);
    ams_merger::acquire(s, 2u);
    ams_merger::to_tail(s);
    CHECK(ams_merger::release(s, 0u) == ams_merger::release_none, 51);
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
    ams_merger::to_tail(s);

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

// Out-of-range inputs must never produce an owner.
static int test_out_of_range_inputs(void)
{
    State s;

    ams_merger::reset(s);
    CHECK(!ams_merger::acquire(s, 4u), 71);
    CHECK(!ams_merger::acquire(s, 0xFFu), 72);
    CHECK(is(s, ams_merger::stage_unloaded, kNoChannel), 73);

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
    CHECK(ams_merger::to_tail(s), 103);

    // The printer's retract for channel 0 arrives afterwards. allow_stop must
    // still be true or before_pull_back is refused and nothing moves.
    CHECK(ams_merger::owns(s, 0u), 104);

    // before_pull_back releases the merger, as it always did.
    CHECK(ams_merger::release(s, 0u) == ams_merger::release_done, 105);
    CHECK(ams_merger::is_free(s), 106);

    // The paired spool on channel 1 loads.
    CHECK(ams_merger::acquire(s, 1u), 107);
    CHECK(ams_merger::owns(s, 1u), 108);

    return 0;
}

// ---------------------------------------------------------------------------
// Conversation level.
//
// The transition tests above check edges one at a time, which is not enough:
// the defect this file exists to close was a routing mistake that every
// individual edge passed. What follows replays the bus conversation for a
// runout the way bambu_bus_ams.cpp would drive this state machine, so a wrong
// route shows up here rather than on a printer.
//
// The gates below are copied from bambu_bus_ams.cpp:240-249 deliberately. That
// file is untouched by this change, so these three lines are the whole
// interface between it and the merger, and asserting them here is what makes
// "untouched" a property rather than a hope.

// The debounce window, and the exact routing Motion_control_run performs: the
// loaded-latch policy times the key-zero, and only its firing edge calls
// to_tail. Driving the real policy here rather than calling to_tail directly is
// the point -- the defect being guarded against is a routing mistake, and a
// test that skips the routing cannot see one.
static const uint32_t kHold = 1500u; // LOADED_LATCH_DROP_MS

static bool key_empty_through_the_window(State& s, ams_loaded_latch::State& d,
                                         uint32_t& t)
{
    bool entered = false;
    for (uint32_t i = 0u; i <= kHold; ++i, ++t)
    {
        const uint8_t owner = s.owner;
        const uint8_t timing =
            ams_merger::is_tail(s) ? ams_loaded_latch::kNoChannel : owner;
        if (ams_loaded_latch::poll(d, timing, owner < 4u, t, kHold))
            if (ams_merger::to_tail(s)) entered = true;
    }
    return entered;
}

struct Gates
{
    bool allow_any;  // before_on_use, on_use
    bool allow_stop; // stop_on_use, before_pull_back
};

static Gates gates_for(const State& s, uint8_t ch)
{
    const uint8_t loaded = s.owner; // ams_state_get_loaded()
    Gates g;
    g.allow_any = (loaded == 0xFFu) || (loaded == ch);
    g.allow_stop = (loaded == ch);
    return g;
}

// The printer feeding another spool without retracting the old one first.
//
// send_out routes to preempt, not to a session release, and it must succeed
// even though TAIL means the tube is still occupied. The printer is master: it
// drives the filament in regardless, so refusing would not prevent the
// collision, only leave the BMCU naming the wrong owner while it happens -- and
// then refusing the new channel's own retract, because allow_stop would still
// point at the old one.
static int test_send_out_preempts_a_held_tail(void)
{
    State s;
    ams_merger::reset(s);
    ams_merger::acquire(s, 3u);
    ams_merger::to_tail(s);

    // Reported, because a strand was still in the tube when the next one came.
    CHECK(ams_merger::preempt(s) == ams_merger::release_preempted_tail, 111);
    CHECK(ams_merger::is_free(s), 112);

    // The claiming channel then loads and owns the merger.
    CHECK(ams_merger::acquire(s, 1u), 113);
    CHECK(ams_merger::owns(s, 1u), 114);
    CHECK(gates_for(s, 1u).allow_stop, 115);

    return 0;
}

// Runout, then the printer gives up on the channel and loads another instead.
//
// This is the arm that separates a preempt from a session release. Both spell
// "no particular channel" at the funnel, so before they were split one of them
// was wrong -- and the wrong one was this: send_out was refused, the merger
// stayed with the abandoned channel, and every gate afterwards named the wrong
// owner. It also matters more than an ordinary divergence, because this edge is
// the only escape from a stale TAIL and therefore the reason TAIL is allowed to
// have no timeout at all.
static int test_runout_then_printer_moves_to_another_channel(void)
{
    State s;
    ams_loaded_latch::State d;
    uint32_t t = 1000u;
    ams_merger::reset(s);
    ams_loaded_latch::reset(d);

    // Printing from channel 0, which then runs out.
    CHECK(ams_merger::acquire(s, 0u), 241);
    CHECK(key_empty_through_the_window(s, d, t), 242);
    CHECK(is(s, ams_merger::stage_tail, 0u), 243);

    // A pause, with idle frames that must not take the merger.
    for (uint32_t frame = 0u; frame < 100u; ++frame)
        CHECK(ams_merger::release(s, kNoChannel) == ams_merger::release_refused_tail, 244);
    CHECK(is(s, ams_merger::stage_tail, 0u), 245);

    // The printer does not retract channel 0. It feeds channel 1 instead.
    // send_out is accepted unconditionally on the bus, so the merger must yield
    // -- the filament is coming in whether the BMCU agrees or not.
    CHECK(ams_merger::preempt(s) == ams_merger::release_preempted_tail, 246);
    CHECK(ams_merger::is_free(s), 247);

    // Channel 1's own load then works end to end, gates included. Had the
    // preempt been refused, allow_any here would still name channel 0 and the
    // load would be rejected.
    CHECK(gates_for(s, 1u).allow_any, 248);
    CHECK(ams_merger::acquire(s, 1u), 249);
    CHECK(is(s, ams_merger::stage_loaded, 1u), 250);

    // And channel 1 can be retracted afterwards, which is what a merger left
    // naming channel 0 would have made impossible.
    CHECK(gates_for(s, 1u).allow_stop, 251);
    CHECK(ams_merger::release(s, 1u) == ams_merger::release_done, 252);

    return 0;
}

// Runout, an overnight pause, and the retract in the morning.
//
// This is the user's reported symptom end to end. The printer is mid-print on
// channel 0 when the spool runs out; the tail leaves the online key first, the
// printer keeps polling on_use for a while, then pauses and waits for a human.
// Idle frames arrive throughout the pause. In the morning the human resumes and
// the printer sends the retract prelude.
//
// Every step asserts the gate that step depends on, because a merger lost at
// any point during the pause makes the final assertion fail -- and the final
// assertion failing is exactly the bug being fixed.
static int test_runout_pause_and_morning_retract(void)
{
    State s;
    ams_loaded_latch::State d;
    uint32_t t = 1000u;
    ams_merger::reset(s);
    ams_loaded_latch::reset(d);

    // Printing from channel 0.
    CHECK(ams_merger::acquire(s, 0u), 201);
    CHECK(gates_for(s, 0u).allow_any, 202);

    // The spool runs out. The tail passes the key, and the debounce holds.
    CHECK(key_empty_through_the_window(s, d, t), 203);
    CHECK(is(s, ams_merger::stage_tail, 0u), 204);

    // The printer has not noticed yet and keeps polling on_use on channel 0.
    // Each one re-acquires, which must promote TAIL back to LOADED rather than
    // being refused, and must not route through UNLOADED.
    for (int poll = 0; poll < 5; ++poll)
    {
        CHECK(gates_for(s, 0u).allow_any, 205);
        ams_merger::acquire(s, 0u);
        CHECK(is(s, ams_merger::stage_loaded, 0u), 206);

        // The key is still empty, so the debounce re-enters TAIL.
        CHECK(key_empty_through_the_window(s, d, t), 207);
        CHECK(is(s, ams_merger::stage_tail, 0u), 208);
    }

    // The printer pauses and waits for a human. Idle frames arrive for hours.
    // Each is a wildcard release. None of them may take the merger.
    for (uint32_t frame = 0u; frame < 20000u; ++frame)
    {
        CHECK(ams_merger::release(s, kNoChannel) == ams_merger::release_refused_tail, 209);

        // The key stays empty for the whole pause. Already in TAIL, the
        // debounce is parked and must not produce a second edge.
        CHECK(!ams_loaded_latch::poll(d, ams_loaded_latch::kNoChannel, true, t, kHold), 210);
        t += 500u;
    }
    CHECK(is(s, ams_merger::stage_tail, 0u), 211);

    // Morning. The human resumes and the printer sends the retract prelude for
    // channel 0. This is the assertion the whole change exists to make true.
    CHECK(gates_for(s, 0u).allow_stop, 212);

    // before_pull_back is accepted and releases the merger by name.
    CHECK(ams_merger::release(s, 0u) == ams_merger::release_done, 213);
    CHECK(ams_merger::is_free(s), 214);

    // The paired spool then loads normally.
    CHECK(gates_for(s, 1u).allow_any, 215);
    CHECK(ams_merger::acquire(s, 1u), 216);
    CHECK(is(s, ams_merger::stage_loaded, 1u), 217);

    return 0;
}

// The same conversation on the pre-TAIL behaviour, as a guard against the fix
// being quietly undone. If a future change makes the debounced key-zero release
// the merger again -- or lets a wildcard take a TAIL -- the morning gate goes
// false, and this records what that looks like so the failure is recognisable.
static int test_the_symptom_is_a_lost_merger(void)
{
    State s;
    ams_merger::reset(s);
    ams_merger::acquire(s, 0u);
    ams_merger::to_tail(s);

    // Pretend a wildcard got through, the way it did before this change.
    ams_merger::reset(s);

    // This is the reported symptom: the retract is refused and nothing moves.
    CHECK(!gates_for(s, 0u).allow_stop, 221);

    return 0;
}

// stop_on_use during TAIL is a pause, not a retract, and must not release.
static int test_stop_on_use_in_tail_does_not_release(void)
{
    State s;
    ams_merger::reset(s);
    ams_merger::acquire(s, 2u);
    ams_merger::to_tail(s);

    // The gate lets it through ...
    CHECK(gates_for(s, 2u).allow_stop, 231);
    // ... and bambu_bus_ams.cpp's stop_on_use branch calls no release, so the
    // merger is still held afterwards.
    CHECK(is(s, ams_merger::stage_tail, 2u), 232);

    return 0;
}

// release_session: the session-idle edge, alongside release and preempt.
//
// The distinction that matters is the named form. release(s, ch) honours a
// named release and takes TAIL with it -- that is the commanded retract, and it
// must. release_session(s, ch) refuses the same call, because a printer going
// quiet is not a printer pulling filament out. Before this edge existed, :435
// spelled the first and meant the second, and was saved only by an unrelated
// use_flag guard upstream.
static int test_release_session_refuses_tail(void)
{
    State s;

    // Named, from TAIL(2): refused, and the merger is untouched.
    ams_merger::reset(s);
    ams_merger::acquire(s, 2u);
    ams_merger::to_tail(s);
    CHECK(ams_merger::release_session(s, 2u) == ams_merger::release_refused_tail, 241);
    CHECK(is(s, ams_merger::stage_tail, 2u), 242);

    // The same call through release() does let go. This pair is the whole
    // reason the two functions exist separately.
    CHECK(ams_merger::release(s, 2u) == ams_merger::release_done, 243);
    CHECK(is(s, ams_merger::stage_unloaded, kNoChannel), 244);

    // Wildcard, from TAIL(2): refused as well. Unchanged behaviour -- this is
    // what release(s, 0xFF) did, and :461 now spells it here instead.
    ams_merger::reset(s);
    ams_merger::acquire(s, 2u);
    ams_merger::to_tail(s);
    CHECK(ams_merger::release_session(s, kNoChannel) == ams_merger::release_refused_tail, 245);
    CHECK(is(s, ams_merger::stage_tail, 2u), 246);

    // From LOADED, both forms release: an idle session with the strand still at
    // the key is the ordinary end of a print, and row 9 keeps it.
    ams_merger::reset(s);
    ams_merger::acquire(s, 2u);
    CHECK(ams_merger::release_session(s, 2u) == ams_merger::release_done, 247);
    CHECK(is(s, ams_merger::stage_unloaded, kNoChannel), 248);

    ams_merger::reset(s);
    ams_merger::acquire(s, 2u);
    CHECK(ams_merger::release_session(s, kNoChannel) == ams_merger::release_done, 249);
    CHECK(is(s, ams_merger::stage_unloaded, kNoChannel), 250);

    // A named session release for a channel that does not own the merger is
    // ignored, so a stale now_filament_num cannot release someone else's.
    ams_merger::reset(s);
    ams_merger::acquire(s, 2u);
    CHECK(ams_merger::release_session(s, 0u) == ams_merger::release_none, 251);
    CHECK(is(s, ams_merger::stage_loaded, 2u), 252);

    // ... including when the owner is in TAIL: not-the-owner is answered before
    // the TAIL refusal, exactly as release() orders them.
    ams_merger::to_tail(s);
    CHECK(ams_merger::release_session(s, 0u) == ams_merger::release_none, 253);
    CHECK(is(s, ams_merger::stage_tail, 2u), 254);

    // Nothing held: no change, no error, either form.
    ams_merger::reset(s);
    CHECK(ams_merger::release_session(s, 2u) == ams_merger::release_none, 255);
    CHECK(ams_merger::release_session(s, kNoChannel) == ams_merger::release_none, 256);
    CHECK(is(s, ams_merger::stage_unloaded, kNoChannel), 257);

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
    if ((rc = test_send_out_preempts_a_held_tail()) != 0) return rc;
    if ((rc = test_runout_then_printer_moves_to_another_channel()) != 0) return rc;
    if ((rc = test_runout_pause_and_morning_retract()) != 0) return rc;
    if ((rc = test_the_symptom_is_a_lost_merger()) != 0) return rc;
    if ((rc = test_stop_on_use_in_tail_does_not_release()) != 0) return rc;
    if ((rc = test_release_session_refuses_tail()) != 0) return rc;
    return 0;
}
