// Replays Quint traces of spec/bmcu_merger.qnt against the firmware's own
// header-only policies, and fails on the first step where they disagree.
//
// The spec says what owner, TAIL and the downstream mask must be after every
// event; this program applies the same event to ams_merger_policy.h and
// downstream_strand_policy.h the way the firmware's call sites do, and
// compares. ci/quint_replay.py turns ITF traces into the line format below.
//
// What this checks: the policies plus the call-site mapping in apply() agree
// with the spec. What it does not: that apply() mirrors the firmware call
// sites. Each case names the site it mirrors; keep them in step by hand. The
// call sites live in bambu_bus_ams.cpp, Motion_control.cpp and main.cpp, none
// of which compile on the host.
//
// Input, one event per line (whitespace separated):
//   <trace> <step> <action> <ch> <owner_in_use> <exp_owner> <exp_tail> <exp_mask> <exp_occupied>
// ch, exp_owner: -1 for none. owner_in_use: 1 when the merger owner's phase
// before the event keeps its AMS motion in on_use/before_on_use/stop_on_use
// (needed by the 0xFF idle path). exp_occupied: 1 when this event must have
// raised DOWNSTREAM_OCCUPIED. A line "reset <trace>" starts a new trace.

#include "ams_merger_policy.h"
#include "downstream_strand_policy.h"

#include <stdio.h>
#include <string.h>

namespace
{

struct Machine
{
    ams_merger::State merger;
    downstream_strand::State down;
    bool occupied; // DOWNSTREAM_OCCUPIED raised by the last event
};

void reset(Machine& m)
{
    ams_merger::reset(m.merger);
    downstream_strand::reset(m.down);
    m.occupied = false;
}

uint8_t ch8(int ch)
{
    return ch < 0 ? 0xFFu : static_cast<uint8_t>(ch);
}

// Returns false for an action name this adapter does not know.
bool apply(Machine& m, const char* action, int ch, bool owner_in_use)
{
    const uint8_t c = ch8(ch);
    m.occupied = false;

    if (!strcmp(action, "sendOut"))
    {
        // bambu_bus_ams.cpp send_out: preempt when the merger is held.
        if (!ams_merger::is_free(m.merger)) ams_merger::preempt(m.merger);
        // Motion_control.cpp set_motion entering send -> main.cpp
        // ams_downstream_mark_sent: report others, then mark.
        m.occupied = downstream_strand::others(m.down, c) != 0u;
        downstream_strand::mark_sent(m.down, c);
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
    if (!strcmp(action, "beforePullBack") || !strcmp(action, "pullBack"))
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
    if (!strcmp(action, "redetectFound") || !strcmp(action, "autoUnloadEmpty"))
    {
        // Motion_control.cpp redetect success / auto-unload empty_held.
        downstream_strand::mark_returned(m.down, c);
        return true;
    }
    if (!strcmp(action, "reboot"))
    {
        // main.cpp boot: restore from flash. Persistence is taken as
        // immediate here (spec assumption A5).
        const uint8_t owner = m.merger.owner;
        const bool tail = ams_merger::is_tail(m.merger);
        const uint8_t mask = m.down.mask;
        ams_merger::restore(m.merger, owner, tail);
        downstream_strand::restore(m.down, mask);
        return true;
    }

    // Events that touch neither policy.
    static const char* const kInert[] = {
        "sendHold", "sendAbort", "pullDone", "redetectTimeout", "pullFault",
        "busLost", "busBack", "keyEmptyWhilePrinting", "keyBack", "autoload",
        "autoUnloadStart", "autoUnloadGiveUp", "handRemoved", "remnantCleared",
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
        int step, ch, in_use, exp_owner, exp_tail, exp_mask, exp_occ;
        if (sscanf(line, "%127s %d %63s %d %d %d %d %d %d", trace, &step, action, &ch,
                   &in_use, &exp_owner, &exp_tail, &exp_mask, &exp_occ) != 9)
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
        const int mask = m.down.mask;
        const int occ = m.occupied ? 1 : 0;
        if (owner != exp_owner || tail != exp_tail || mask != exp_mask || occ != exp_occ)
        {
            fprintf(stderr,
                    "%s step %d: %s(ch=%d) diverged\n"
                    "  spec:     owner=%d tail=%d mask=0x%X occupied=%d\n"
                    "  firmware: owner=%d tail=%d mask=0x%X occupied=%d\n",
                    trace, step, action, ch, exp_owner, exp_tail, exp_mask, exp_occ,
                    owner, tail, mask, occ);
            return 1;
        }
    }

    printf("replayed %lu events, no divergence\n", events);
    return 0;
}
