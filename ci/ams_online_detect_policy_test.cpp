// Host test for src/ams_online_detect_policy.h.
// 1 tick == 1 ms in all cases below.

#include "ams_online_detect_policy.h"

using ams_online_detect::Config;
using ams_online_detect::QueryAction;
using ams_online_detect::State;

static const Config kCfg = {3000u, 1500u};

#define CHECK(cond, code) do { if (!(cond)) return (code); } while (0)

static int test_fresh_handshake_unchanged(void)
{
    State s;
    ams_online_detect::reset(s);

    CHECK(ams_online_detect::on_query(s) == QueryAction::offer_first, 11);
    CHECK(s.phase == 1u, 12);
    CHECK(ams_online_detect::on_query(s) == QueryAction::offer_repeat, 13);
    CHECK(s.phase == 2u, 14);
    CHECK(ams_online_detect::on_query(s) == QueryAction::offer_repeat, 15);
    CHECK(s.phase == 2u, 16);

    ams_online_detect::latch_confirm(s, 1000u);
    CHECK(s.registered, 17);
    CHECK(s.phase == 3u, 18);
    CHECK(ams_online_detect::on_query(s) == QueryAction::stay_silent, 19);
    return 0;
}

static int test_live_service_never_reoffers(void)
{
    State s;
    ams_online_detect::reset(s);
    ams_online_detect::latch_confirm(s, 0u);

    for (uint32_t t = 100u; t <= 60000u; t += 100u)
    {
        ams_online_detect::note_service_traffic(s, t);
        ams_online_detect::poll(s, kCfg, t);
        if ((t % 5000u) == 0u)
        {
            CHECK(ams_online_detect::on_query(s) == QueryAction::stay_silent, 21);
            CHECK(s.registered, 22);
            CHECK(s.phase == 3u, 23);
        }
    }
    return 0;
}

static int test_forget_then_recover(void)
{
    State s;
    ams_online_detect::reset(s);
    ams_online_detect::latch_confirm(s, 0u);

    for (uint32_t t = 100u; t <= 10000u; t += 100u)
    {
        ams_online_detect::note_service_traffic(s, t);
        ams_online_detect::poll(s, kCfg, t);
    }

    // 1400 ms of silence: not yet stale.
    ams_online_detect::poll(s, kCfg, 11400u);
    CHECK(!s.service_stale, 31);
    CHECK(s.confirm_settled, 32);
    CHECK(ams_online_detect::on_query(s) == QueryAction::stay_silent, 33);

    // 1500 ms of silence: re-offer.
    ams_online_detect::poll(s, kCfg, 11500u);
    CHECK(s.service_stale, 34);
    CHECK(ams_online_detect::on_query(s) == QueryAction::offer_first, 35);
    CHECK(!s.registered, 36);
    CHECK(s.phase == 1u, 37);
    CHECK(ams_online_detect::on_query(s) == QueryAction::offer_repeat, 38);

    ams_online_detect::latch_confirm(s, 12000u);
    CHECK(s.registered, 39);
    CHECK(s.phase == 3u, 40);
    CHECK(ams_online_detect::on_query(s) == QueryAction::stay_silent, 41);
    return 0;
}

static int test_both_conditions_required(void)
{
    // (a) service stale but confirm not settled.
    State a;
    ams_online_detect::reset(a);
    ams_online_detect::latch_confirm(a, 0u);
    ams_online_detect::poll(a, kCfg, 1600u);
    CHECK(a.service_stale, 51);
    CHECK(!a.confirm_settled, 52);
    CHECK(ams_online_detect::on_query(a) == QueryAction::stay_silent, 53);

    // T5: same state, past the confirm settle window -> re-offer.
    ams_online_detect::poll(a, kCfg, 3100u);
    CHECK(a.confirm_settled, 54);
    CHECK(a.service_stale, 55);
    CHECK(ams_online_detect::on_query(a) == QueryAction::offer_first, 56);

    // (b) confirm settled but service fresh.
    State b;
    ams_online_detect::reset(b);
    ams_online_detect::latch_confirm(b, 0u);
    ams_online_detect::note_service_traffic(b, 2900u);
    ams_online_detect::poll(b, kCfg, 3100u);
    CHECK(b.confirm_settled, 57);
    CHECK(!b.service_stale, 58);
    CHECK(ams_online_detect::on_query(b) == QueryAction::stay_silent, 59);
    return 0;
}

static int test_reset_clears_sticky_state(void)
{
    State s;
    ams_online_detect::reset(s);
    ams_online_detect::latch_confirm(s, 0u);
    ams_online_detect::poll(s, kCfg, 5000u);
    CHECK(s.confirm_settled && s.service_stale, 61);

    ams_online_detect::reset(s);
    CHECK(!s.registered, 62);
    CHECK(!s.confirm_settled && !s.service_stale, 63);
    CHECK(s.phase == 0u, 64);
    CHECK(ams_online_detect::on_query(s) == QueryAction::offer_first, 65);
    CHECK(s.phase == 1u, 66);
    return 0;
}

static int test_tick_wrap(void)
{
    State s;
    ams_online_detect::reset(s);
    ams_online_detect::latch_confirm(s, 0xFFFFF000u);
    ams_online_detect::note_service_traffic(s, 0xFFFFF000u);

    ams_online_detect::poll(s, kCfg, 0x00000C00u); // elapsed 7168 ticks
    CHECK(s.confirm_settled, 71);
    CHECK(s.service_stale, 72);
    CHECK(ams_online_detect::on_query(s) == QueryAction::offer_first, 73);
    return 0;
}

static int test_poll_before_registration_is_noop(void)
{
    State s;
    ams_online_detect::reset(s);
    ams_online_detect::poll(s, kCfg, 100000u);
    CHECK(!s.confirm_settled, 81);
    CHECK(!s.service_stale, 82);
    CHECK(s.phase == 0u, 83);
    CHECK(ams_online_detect::on_query(s) == QueryAction::offer_first, 84);
    return 0;
}

static int test_stay_silent_leaves_timestamps(void)
{
    State s;
    ams_online_detect::reset(s);
    ams_online_detect::latch_confirm(s, 4000u);
    ams_online_detect::note_service_traffic(s, 4200u);
    ams_online_detect::poll(s, kCfg, 4300u);

    const uint32_t confirm = s.last_confirm_tick;
    const uint32_t service = s.last_service_tick;
    const uint8_t phase = s.phase;

    CHECK(ams_online_detect::on_query(s) == QueryAction::stay_silent, 91);
    CHECK(s.last_confirm_tick == confirm, 92);
    CHECK(s.last_service_tick == service, 93);
    CHECK(s.phase == phase, 94);
    CHECK(s.registered, 95);
    return 0;
}

int main(void)
{
    int rc = 0;
    if ((rc = test_fresh_handshake_unchanged()) != 0) return rc;
    if ((rc = test_live_service_never_reoffers()) != 0) return rc;
    if ((rc = test_forget_then_recover()) != 0) return rc;
    if ((rc = test_both_conditions_required()) != 0) return rc;
    if ((rc = test_reset_clears_sticky_state()) != 0) return rc;
    if ((rc = test_tick_wrap()) != 0) return rc;
    if ((rc = test_poll_before_registration_is_noop()) != 0) return rc;
    if ((rc = test_stay_silent_leaves_timestamps()) != 0) return rc;
    return 0;
}
