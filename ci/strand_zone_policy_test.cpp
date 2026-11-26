// Host test for src/strand_zone_policy.h. Units are millimetres here (the
// policy only compares and adds), with the firmware's default geometry.

#include "strand_zone_policy.h"

#define CHECK(cond, code) do { if (!(cond)) return (code); } while (0)

using namespace strand_zone;

static const Config C = { 120, 150, 130, 720, 30 }; // park, set, clear, head (0.6 m retract), slack

static bool step(State& s, int32_t d, bool key) { return update(s, C, d, true, false, key); }

// Drive `mm` forward or back in samples of at most 10 mm, switch held.
static void travel(State& s, int32_t mm, bool key)
{
    while (mm != 0)
    {
        const int32_t inc = mm > 10 ? 10 : (mm < -10 ? -10 : mm);
        step(s, inc, key);
        mm -= inc;
    }
}

static int test_autoload_parks_upstream(void)
{
    State s; reset(s);
    CHECK(s.zone == kNone, 11);
    step(s, 5, true);                    // front reaches the switch
    CHECK(s.front_known && s.front == 0, 12);
    travel(s, 120, true);                // Stage 2
    CHECK(s.zone == kUpstream, 13);
    return 0;
}

static int test_send_then_full_retract(void)
{
    State s; reset(s);
    step(s, 0, true);
    travel(s, 900, true);                // send to the head and print
    CHECK(s.zone == kDownstream && s.front == C.head, 21);
    CHECK(at_head(s, C), 22);
    travel(s, -600, true);               // the build's retract
    CHECK(s.zone == kUpstream, 23);
    return 0;
}

// The jam-latched unload pulls 100 mm only: still past the merger.
static int test_jam_pull_stays_downstream(void)
{
    State s; reset(s);
    step(s, 0, true);
    travel(s, 900, true);
    travel(s, -100, true);
    CHECK(s.zone == kDownstream, 31);
    return 0;
}

// hold_load / Stage-2 re-push / idle feed: no send needed to get past.
static int test_any_forward_travel_sets(void)
{
    State s; reset(s);
    step(s, 0, true);
    travel(s, 120, true);
    CHECK(s.zone == kUpstream, 41);
    travel(s, 40, true);
    CHECK(s.zone == kDownstream, 42);
    travel(s, -15, true);                // inside the hysteresis band
    CHECK(s.zone == kDownstream, 43);
    travel(s, -20, true);                // below down_clear
    CHECK(s.zone == kUpstream, 44);
    return 0;
}

// Runout: the tail passes the switch going forward and leaves a tail piece.
static int test_runout_leaves_tail(void)
{
    State s; reset(s);
    step(s, 0, true);
    travel(s, 900, true);
    step(s, 10, false);                  // falling edge while moving forward
    CHECK(s.tail_known && s.zone == kTail, 51);
    travel(s, 300, false);               // extruder keeps pulling: still a tail
    CHECK(s.zone == kTail, 52);
    // A new strand behind the old tail piece: the remnant survives.
    step(s, 10, true);
    CHECK(s.remnant && s.zone == kTail, 53);
    return 0;
}

// Hand removal: measured backward travel past the switch.
static int test_hand_removal_clears(void)
{
    State s; reset(s);
    step(s, 0, true);
    travel(s, 120, true);
    travel(s, -130, true);
    step(s, -10, false);                 // falling edge while moving back
    CHECK(s.zone == kUpstream || s.zone == kNone, 61);
    CHECK(!is_downstream(s.zone), 62);
    return 0;
}

// A lever float with no motion changes nothing, at the head or parked.
static int test_float_without_motion_is_ignored(void)
{
    State s; reset(s);
    step(s, 0, true);
    travel(s, 900, true);
    step(s, 0, true);                    // last_dir is +1 from travel
    s.last_dir = 0;
    step(s, 0, false);
    step(s, 0, true);
    CHECK(s.zone == kDownstream && !s.tail_known && !s.remnant, 71);
    return 0;
}

// A rising edge while the front is known past the switch keeps the front.
static int test_rising_edge_never_resets_a_downstream_front(void)
{
    State s; reset(s);
    step(s, 0, true);
    travel(s, 900, true);
    s.key = 0;                           // missed falling edge (debounce)
    s.last_dir = 0;
    step(s, 0, true);
    CHECK(s.zone == kDownstream, 81);
    return 0;
}

// Unmeasured forward drive is assumed to have gone past the merger;
// unmeasured backward drive clears nothing.
static int test_unmeasured_drive(void)
{
    State s; reset(s);
    step(s, 0, true);
    travel(s, 120, true);
    update(s, C, 0, false, true, true);
    CHECK(s.zone == kDownstream, 91);
    update(s, C, 0, false, false, true);
    CHECK(s.zone == kDownstream, 92);
    return 0;
}

static int test_passage_proof(void)
{
    State all[4];
    for (int i = 0; i < 4; i++) reset(all[i]);
    // 0: orphan in the head. 1: runout tail. 2: parked. 3: loads to the head.
    step(all[0], 0, true); travel(all[0], 900, true);
    step(all[1], 0, true); travel(all[1], 900, true); step(all[1], 10, false);
    step(all[2], 0, true); travel(all[2], 120, true);
    step(all[3], 0, true); travel(all[3], 900, true);
    CHECK(at_head(all[3], C), 101);
    const uint8_t changed = passage_proof(all, C, 3);
    CHECK(changed == 0x03u, 102);
    CHECK(all[0].zone == kUpstream && all[1].zone != kTail, 103);
    CHECK(all[2].zone == kUpstream && all[3].zone == kDownstream, 104);
    CHECK(downstream_mask(all) == 0x08u, 105);
    return 0;
}

static int test_pack_and_restore(void)
{
    State all[4];
    for (int i = 0; i < 4; i++) reset(all[i]);
    all[0].zone = kNone; all[1].zone = kUpstream; all[2].zone = kDownstream; all[3].zone = kTail;
    const uint8_t p = pack(all);
    CHECK(p == 0xE4u, 111);
    State r;
    restore(r, C, kDownstream, false, false);
    CHECK(r.zone == kDownstream && r.front == C.head, 112);
    CHECK(!at_head(r, C), 117);          // a restored front proves nothing
    restore(r, C, kTail, true, false);
    CHECK(r.zone == kTail && r.remnant, 113);
    restore(r, C, kUpstream, true, true);  // owner forces downstream
    CHECK(r.zone == kDownstream, 114);
    restore(r, C, kNone, false, false);
    CHECK(r.zone == kNone, 115);
    // Stored downstream, switch reads empty at the first sample: no motion,
    // so nothing changes.
    restore(r, C, kDownstream, true, false);
    step(r, 0, false);
    CHECK(r.zone == kDownstream, 116);
    return 0;
}

int main(void)
{
    int rc = 0;
    if ((rc = test_autoload_parks_upstream()) != 0) return rc;
    if ((rc = test_send_then_full_retract()) != 0) return rc;
    if ((rc = test_jam_pull_stays_downstream()) != 0) return rc;
    if ((rc = test_any_forward_travel_sets()) != 0) return rc;
    if ((rc = test_runout_leaves_tail()) != 0) return rc;
    if ((rc = test_hand_removal_clears()) != 0) return rc;
    if ((rc = test_float_without_motion_is_ignored()) != 0) return rc;
    if ((rc = test_rising_edge_never_resets_a_downstream_front()) != 0) return rc;
    if ((rc = test_unmeasured_drive()) != 0) return rc;
    if ((rc = test_passage_proof()) != 0) return rc;
    if ((rc = test_pack_and_restore()) != 0) return rc;
    return 0;
}
