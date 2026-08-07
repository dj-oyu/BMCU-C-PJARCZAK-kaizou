// Host test for src/dm_autoload_retry_policy.h.
// The call sites live in the DM autoload state machine in Motion_control.cpp,
// which does not compile on the host, so the budget accounting is checked here
// and the livelock cycle is replayed against it.

#include "dm_autoload_retry_policy.h"

#define CHECK(cond, code) do { if (!(cond)) return (code); } while (0)

// ks encodings, from dm_key_to_state.
enum { KS_NONE = 0u, KS_BOTH = 1u, KS_OUTER = 2u, KS_INNER = 3u };

// Three aborts spend the budget, and the third is the one that reports it.
static int test_budget_spends_on_third_abort(void)
{
    uint8_t tries = 0u;
    CHECK(!dm_autoload::count_abort(tries), 11);
    CHECK(tries == 1u, 12);
    CHECK(!dm_autoload::count_abort(tries), 13);
    CHECK(tries == 2u, 14);
    CHECK(dm_autoload::count_abort(tries), 15);
    CHECK(tries == 3u, 16);
    return 0;
}

// Saturating, not wrapping. A budget that rolled back to zero would be the same
// livelock with a 255-cycle period, and it would stay spent-reporting anyway.
static int test_budget_saturates(void)
{
    uint8_t tries = 254u;
    CHECK(dm_autoload::count_abort(tries), 21);
    CHECK(tries == 255u, 22);
    CHECK(dm_autoload::count_abort(tries), 23);
    CHECK(tries == 255u, 24);
    return 0;
}

// Only an empty key ends the insertion. `inner` is the reading that used to
// hand a full budget back to a strand that never moved.
static int test_only_empty_key_ends_the_insertion(void)
{
    CHECK(dm_autoload::insertion_ended(KS_NONE), 31);
    CHECK(!dm_autoload::insertion_ended(KS_INNER), 32);
    CHECK(!dm_autoload::insertion_ended(KS_OUTER), 33);
    CHECK(!dm_autoload::insertion_ended(KS_BOTH), 34);
    return 0;
}

// The 2026-08-08 livelock, replayed. Each round is what was recorded on ch2:
// S2_PUSH fills the buffer and aborts, S2_RETRACT backs off until the key reads
// `outer`, Stage1 debounces and pushes, the key reads `both` and Stage2 is
// re-entered. The re-entry must NOT refund the budget, so the cycle has to
// terminate. Before the fix this loop never exited.
static int test_livelock_cycle_terminates(void)
{
    uint8_t tries = 0u;

    for (int round = 1; round <= 8; ++round)
    {
        const bool spent = dm_autoload::count_abort(tries);   // S2_PUSH aborts
        if (spent)
        {
            // dm_fail_latch is set and the channel parks. Three rounds is the
            // budget, so a later exit means the refund crept back in.
            CHECK(round == 3, 40 + round);
            return 0;
        }

        // S2_RETRACT stops on `outer`, so it goes round through Stage1 rather
        // than straight back to S2_PUSH. Neither hop ends the insertion.
        CHECK(!dm_autoload::insertion_ended(KS_OUTER), 51);   // -> S1_DEBOUNCE
        CHECK(!dm_autoload::insertion_ended(KS_BOTH), 52);    // S1_PUSH -> S2_PUSH
    }

    return 53;   // never latched: the livelock is back
}

// Withdrawing the filament is what genuinely resets the budget, so the next
// insertion gets its own three attempts rather than inheriting a spent one.
static int test_withdrawal_restores_the_budget(void)
{
    uint8_t tries = 0u;
    while (!dm_autoload::count_abort(tries)) { }
    CHECK(tries == dm_autoload::kMaxTries, 61);

    CHECK(dm_autoload::insertion_ended(KS_NONE), 62);
    tries = 0u;

    CHECK(!dm_autoload::count_abort(tries), 63);
    CHECK(tries == 1u, 64);
    return 0;
}

int main(void)
{
    int code = 0;
    if ((code = test_budget_spends_on_third_abort()) != 0) return code;
    if ((code = test_budget_saturates()) != 0) return code;
    if ((code = test_only_empty_key_ends_the_insertion()) != 0) return code;
    if ((code = test_livelock_cycle_terminates()) != 0) return code;
    if ((code = test_withdrawal_restores_the_budget()) != 0) return code;
    return 0;
}
