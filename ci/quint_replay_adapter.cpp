// Replays Quint traces of spec/bmcu_merger.qnt against the firmware's
// merger policy, and fails on the first step where they disagree.
//
// The spec says what owner and TAIL must be after every event; this program
// applies the same event to ams_merger_policy.h the way the firmware's call
// sites do, and compares. (Strand zones have their own spec and adapter:
// spec/strand_zone.qnt, ci/zone_replay_adapter.cpp.) ci/quint_replay.py turns ITF traces into the line format below.
//
// What this checks: the policy plus the call-site mapping in apply() agree
// with the spec. What it does not: that apply() mirrors the firmware call
// sites. Each case names the site it mirrors; keep them in step by hand. The
// call sites live in bambu_bus_ams.cpp, Motion_control.cpp and main.cpp, none
// of which compile on the host.
//
// Input, one event per line (whitespace separated):
//   <trace> <step> <action> <ch> <owner_in_use> <exp_owner> <exp_tail>
// ch, exp_owner: -1 for none. owner_in_use: 1 when the merger owner's phase
// before the event keeps its AMS motion in on_use/before_on_use/stop_on_use
// (needed by the 0xFF idle path). A line "reset <trace>" starts a new trace.

#include "ams_merger_policy.h"

#include <stdio.h>
#include <string.h>

namespace
{

struct Machine
{
    ams_merger::State merger;
};

void reset(Machine& m)
{
    ams_merger::reset(m.merger);
}

uint8_t ch8(int ch)
{
    return ch < 0 ? 0xFFu : static_cast<uint8_t>(ch);
}

// Returns false for an action name this adapter does not know.
bool apply(Machine& m, const char* action, int ch, bool owner_in_use)
{
    const uint8_t c = ch8(ch);

    if (!strcmp(action, "sendOut"))
    {
        // bambu_bus_ams.cpp send_out: preempt when the merger is held.
        if (!ams_merger::is_free(m.merger)) ams_merger::preempt(m.merger);
        return true;
    }
    if (!strcmp(action, "beforeOnUse") || !strcmp(action, "onUse") ||
        !strcmp(action, "stopOnUse"))
    {
        // bambu_bus_ams.cpp before_on_use / stop_on_use(=on_use) handlers:
        // ams_state_set_loaded.
        ams_merger::acquire(m.merger, c);
        return true;
    }
    if (!strcmp(action, "beforePullBack") || !strcmp(action, "pullBack") ||
        !strcmp(action, "jamPullBack"))
    {
        // before_pull_back and 0xFF 03/00: ams_state_set_unloaded(ch).
        ams_merger::release(m.merger, c);
        return true;
    }
    if (!strcmp(action, "idleAll"))
    {
        // 0xFF other: returns early while the current channel is in use,
        // otherwise ams_state_session_idle(0xFF).
        if (!owner_in_use) ams_merger::release_session(m.merger, 0xFFu);
        return true;
    }
    if (!strcmp(action, "toTail"))
    {
        // Motion_control.cpp loaded-latch poll -> ams_state_set_tail.
        ams_merger::to_tail(m.merger);
        return true;
    }
    if (!strcmp(action, "reboot"))
    {
        // main.cpp boot: restore from flash (spec assumption A5).
        const uint8_t owner = m.merger.owner;
        const bool tail = ams_merger::is_tail(m.merger);
        ams_merger::restore(m.merger, owner, tail);
        return true;
    }

    // Events that touch the merger not at all.
    static const char* const kInert[] = {
        "sendHold", "sendAbort", "pullDone", "redetectFound", "redetectFoundShort",
        "redetectTimeout", "pullFault", "busLost", "busBack", "keyEmptyWhilePrinting",
        "keyBack", "autoload", "jam", "autoUnloadStart", "autoUnloadEmpty",
        "autoUnloadGiveUp", "handRemoved", "remnantCleared",
    };
    for (size_t i = 0; i < sizeof(kInert) / sizeof(kInert[0]); i++)
        if (!strcmp(action, kInert[i])) return true;
    return false;
}

}

int main(void)
{
    Machine m;
    reset(m);

    char line[256];
    unsigned long events = 0;
    while (fgets(line, sizeof(line), stdin))
    {
        char trace[128];
        if (sscanf(line, "reset %127s", trace) == 1)
        {
            reset(m);
            continue;
        }

        char action[64];
        int step, ch, in_use, exp_owner, exp_tail;
        if (sscanf(line, "%127s %d %63s %d %d %d %d", trace, &step, action, &ch,
                   &in_use, &exp_owner, &exp_tail) != 7)
        {
            fprintf(stderr, "bad line: %s", line);
            return 2;
        }

        if (!apply(m, action, ch, in_use != 0))
        {
            fprintf(stderr, "%s step %d: unknown action %s\n", trace, step, action);
            return 2;
        }
        events++;

        const int owner = ams_merger::is_free(m.merger) ? -1 : m.merger.owner;
        const int tail = ams_merger::is_tail(m.merger) ? 1 : 0;
        if (owner != exp_owner || tail != exp_tail)
        {
            fprintf(stderr,
                    "%s step %d: %s(ch=%d) diverged\n"
                    "  spec:     owner=%d tail=%d\n"
                    "  firmware: owner=%d tail=%d\n",
                    trace, step, action, ch, exp_owner, exp_tail, owner, tail);
            return 1;
        }
    }

    printf("replayed %lu events, no divergence\n", events);
    return 0;
}
