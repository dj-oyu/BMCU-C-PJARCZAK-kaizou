// Frame-in / reply-out conversation tests for src/bambu_bus_ams.cpp.
//
// The policy headers already have host tests. What had none is the wiring the
// policy is bolted to: set_motion's accept table, the handler dispatch, and the
// AMS-number routing. Both defects this week were in that wiring, and both were
// caught only by tests of the policy -- which model the routing rather than
// running it. ci/ams_merger_policy_test.cpp says so itself: its conversation
// arm copies the allow_any and allow_stop expressions "verbatim from :240-249".
// A copy cannot disagree with the original, so it cannot catch the original
// being wrong.
//
// Everything here drives real frames through the real RX framer into the real
// handlers, and reads back the real reply bytes and the real merger policy.
// See ci/host/bambu_bus_host.h for the seams.
//
// Each vector runs in its own process (the runner invokes this binary once per
// name), because bambu_bus_ams.cpp keeps file-scope state -- package_num,
// count_on_use, last_before_on_use_motion_flag, the sniff replay cursor, the
// online-detect window -- that has no reset entry point. Sharing a process
// would make vectors order-dependent in ways no assertion here would show.

#include <stdio.h>
#include <string.h>

#include "bambu_bus_host.h"

#include "ams.h"
#include "app_api.h"
#include "hal/time_hw.h"
#include "ams_merger_policy.h"
#include "bmcu_link_protocol.h"

bool package_check_crc16(uint8_t *data, int data_length);

#define CHECK(cond, code) do { if (!(cond)) { \
    printf("  FAIL %s:%d (%d): %s\n", __FILE__, __LINE__, (code), #cond); \
    return (code); } } while (0)

using namespace bmcu_link_protocol;

namespace
{

// Wire encodings of the six motion phases, from the accept table at
// bambu_bus_ams.cpp:232-236.
constexpr uint8_t kStatuSendOut = 0x03u, kFlagSendOut = 0x00u;
constexpr uint8_t kStatuBeforeOnUse = 0x09u, kFlagBeforeOnUse = 0x7Fu;
constexpr uint8_t kStatuOnUse = 0x07u, kFlagOnUse = 0x7Fu;
constexpr uint8_t kStatuStopOnUse = 0x07u, kFlagStopOnUse = 0x00u;
constexpr uint8_t kStatuBeforePullBack = 0x09u, kFlagBeforePullBack = 0x3Fu;

// The frame that means "no channel": read_num 0xFF. Two of its three forms
// matter here -- the 0x03/0x00 retract, and the catch-all reset at :440 whose
// wildcard release is the subject of the first regression vector.
constexpr uint8_t kNoChannel = 0xFFu;

constexpr uint8_t kSelf = 0u;  // BAMBU_BUS_AMS_NUM for this build
constexpr uint8_t kPeer = 1u;  // the other BMCU on the same bus

_filament_motion motion_of(uint8_t ch)
{
    return ams[0].filament[ch].motion;
}

// Drive a channel from idle to on_use the way the printer does.
void load_channel(uint8_t ch)
{
    host_bus::poll_motion_short(kSelf, kStatuSendOut, ch, kFlagSendOut);
    host_bus::poll_motion_short(kSelf, kStatuBeforeOnUse, ch, kFlagBeforeOnUse);
    host_bus::poll_motion_short(kSelf, kStatuOnUse, ch, kFlagOnUse);
}

// ---------------------------------------------------------------------------
// Family 1: this week's two routing defects, as regression vectors
// ---------------------------------------------------------------------------

// bd2e2e1: "stop an idle frame taking the merger out of a channel's tail".
//
// The runout that the whole TAIL state exists for: the strand's tail clears the
// online key, the printer pauses waiting for a human, idle frames flow for the
// whole pause, and the retract prelude arrives in the morning. It must be
// accepted, and that is what the end of this vector asserts.
//
// Read this next, because the vector does not do what its position in this file
// suggests. It does NOT catch bd2e2e1's defect. Reintroducing that defect --
// deleting the TAIL check from ams_merger::release so a wildcard reaches
// reset() -- leaves every vector in this file passing.
//
// The reason is the early return at bambu_bus_ams.cpp:440, immediately above
// the wildcard release the defect was in. The catch-all returns before
// releasing whenever the channel named by now_filament_num is in the on_use
// family, and owning the merger implies exactly that: the two acquire sites
// (:308, :378) are inside the before_on_use and on_use branches, and every
// release site is where the motion leaves that family. So :461 is unreachable
// while any channel owns the merger. See merger_ownership_invariant_walk below,
// which searches for a counterexample rather than trusting that argument, and
// finds none in forty thousand frames while the wildcard itself runs thousands
// of times.
//
// bd2e2e1 fixed a real defect in the policy, and ci/ams_merger_policy_test.cpp
// covers it. What it did not fix is the reported overnight symptom, because the
// route it names cannot be taken. That symptom is still unexplained.
//
// So the middle assertions here pin the mechanism rather than the outcome: not
// one wildcard release was even offered to the funnel. If a future change to
// the accept table starts delivering them, this fails and the defect
// bd2e2e1 guards against becomes live at the routing level for the first time.
int test_idle_frames_do_not_take_tail(void)
{
    host_bus::reset();
    load_channel(1u);

    CHECK(host_bus::merger_stage() == ams_merger::stage_loaded, 101);
    CHECK(host_bus::merger_owner() == 1u, 102);

    // The tail clears the online key. On target this edge comes from
    // Motion_control's debounce; here the test plays that part directly,
    // because Motion_control is out of scope for this harness.
    ams_state_set_tail();
    CHECK(host_bus::merger_stage() == ams_merger::stage_tail, 103);

    // The pause. Idle frames arrive continuously -- the storm that made the
    // refusal counter edge-latched in the first place.
    for (int i = 0; i < 500; ++i)
    {
        host_bus::poll_motion_short(kSelf, 0x00u, kNoChannel, 0x00u);
        host_clock_advance_ms(20u);
    }

    // The merger is still held, by the channel that holds it, in TAIL.
    CHECK(host_bus::merger_stage() == ams_merger::stage_tail, 104);
    CHECK(host_bus::merger_owner() == 1u, 105);

    // ... and no release was ever offered to the policy. The catch-all at
    // bambu_bus_ams.cpp:440 returns early whenever the channel named by
    // now_filament_num is in the on_use family, and owning the merger implies
    // exactly that: the two acquire sites (:308, :378) are inside the
    // before_on_use and on_use branches, and every release site is where the
    // motion leaves that family. So the wildcard at :461 is unreachable while
    // any channel owns the merger, and the owner-check added to
    // ams_merger::release is defence for a case this routing does not
    // currently deliver -- not the thing that saves this scenario.
    CHECK(host_bus::merger_events_of(host_bus::kMergerRefusedTail) == 0, 106);
    CHECK(host_bus::merger_events_of(host_bus::kMergerReleased) == 0, 107);
    CHECK(motion_of(1u) == _filament_motion::on_use, 108);

    // And the early return, not the policy, is what did it: five hundred idle
    // frames produced not one call into the funnel. The distinction matters --
    // "the wildcard never met a TAIL" and "the wildcard never ran" pass the
    // assertion above identically, and only one of them is a statement about
    // the routing.
    CHECK(host_bus::wildcard_release_calls == 0, 112);

    // The morning. The retract prelude is gated on allow_stop, which is true in
    // TAIL because TAIL is ownership.
    host_bus::poll_motion_short(kSelf, kStatuBeforePullBack, 1u, kFlagBeforePullBack);

    CHECK(motion_of(1u) == _filament_motion::before_pull_back, 109);
    CHECK(host_bus::merger_stage() == ams_merger::stage_unloaded, 110);
    CHECK(host_bus::merger_events_of(host_bus::kMergerReleased) == 1, 111);

    return 0;
}

// 15bb6ce: "tell a channel claiming the merger apart from the session ending".
//
// The printer abandons the runout channel and feeds another spool without
// retracting the first. send_out must take the merger off a TAIL -- the printer
// is master and the filament goes in whether the BMCU agrees or not, so the
// only choice is whether the BMCU's record of the owner stays right.
//
// Regressing :279 back to ams_state_set_unloaded(0xFF) leaves the merger held
// by channel 1, which then denies channel 2 both its load and its own retract.
// This vector fails at 205 in that case.
int test_send_out_preempts_tail(void)
{
    host_bus::reset();
    load_channel(1u);
    ams_state_set_tail();
    CHECK(host_bus::merger_stage() == ams_merger::stage_tail, 201);

    // Another channel claims the tube.
    host_bus::poll_motion_short(kSelf, kStatuSendOut, 2u, kFlagSendOut);

    CHECK(host_bus::merger_events_of(host_bus::kMergerPreempted) == 1, 202);
    CHECK(host_bus::merger_stage() == ams_merger::stage_unloaded, 203);

    // The preempt names the claiming channel, which is the whole point of it
    // being a separate edge from the session release: it is the witness that a
    // strand was still in the tube when the next one arrived.
    int preempt_index = -1;
    for (int i = 0; i < host_bus::merger_event_count; ++i)
        if (host_bus::merger_events[i].kind == host_bus::kMergerPreempted)
            preempt_index = i;
    CHECK(preempt_index >= 0, 204);
    CHECK(host_bus::merger_events[preempt_index].channel == 2u, 205);

    // The claiming channel can now load...
    host_bus::poll_motion_short(kSelf, kStatuBeforeOnUse, 2u, kFlagBeforeOnUse);
    CHECK(host_bus::merger_stage() == ams_merger::stage_loaded, 206);
    CHECK(host_bus::merger_owner() == 2u, 207);
    CHECK(motion_of(2u) == _filament_motion::before_on_use, 208);

    // ... and retract, which is the half that the blanket refusal broke.
    host_bus::poll_motion_short(kSelf, kStatuOnUse, 2u, kFlagOnUse);
    host_bus::poll_motion_short(kSelf, kStatuBeforePullBack, 2u, kFlagBeforePullBack);
    CHECK(motion_of(2u) == _filament_motion::before_pull_back, 209);
    CHECK(host_bus::merger_stage() == ams_merger::stage_unloaded, 210);

    // The abandoned channel was reset by the accept path, not left mid-motion.
    CHECK(motion_of(1u) == _filament_motion::idle, 211);

    return 0;
}

// ---------------------------------------------------------------------------
// Family 2: peer-addressed frames
// ---------------------------------------------------------------------------

// A two-unit bus carries mostly the other unit's traffic -- 26,000 such frames
// in thirty minutes of ordinary printing. Every one is silently dropped by the
// ams_num check inside the handler, and lands upstream as FAILED/NO_RESPONSE,
// indistinguishable from a genuine failure to answer until the addressed AMS
// number was captured alongside it.
//
// This vector is that distinction as a permanent assertion: the classification
// stays FAILED/NO_RESPONSE (unchanged, and this is not the place to change it),
// but addressed_ams names the peer, and nothing else moves.
int test_peer_addressed_short_frame(void)
{
    host_bus::reset();

    const int replied = host_bus::poll_motion_short(kPeer, kStatuOnUse, 0u, kFlagOnUse);

    CHECK(replied == 0, 301);
    CHECK(host_bus::reply_count == 0, 302);
    CHECK(host_bus::transaction_count == 1, 303);

    const host_bus::Transaction& t = host_bus::last_transaction();
    CHECK(t.rx_class == (uint8_t)bambubus_package_type::filament_motion_short, 304);
    CHECK(t.command == 0x03u, 305);
    CHECK(t.addressed_ams == kPeer, 306);
    CHECK(t.outcome == OUTCOME_FAILED, 307);
    CHECK(t.reason == REASON_NO_RESPONSE, 308);
    CHECK(t.response_length == 0u, 309);
    CHECK(t.request_length == 12u, 310);

    // The observe-only service counter is gated on the address match, so a peer
    // frame must not look like traffic for us -- otherwise the silence
    // measurement that HMS 0500_409D diagnosis depends on would read the peer's
    // conversation as ours.
    CHECK(host_bus::service_frame_count == 0, 311);

    // Nothing of ours moved.
    CHECK(host_bus::merger_stage() == ams_merger::stage_unloaded, 312);
    CHECK(host_bus::merger_event_count == 0, 313);
    CHECK(ams[0].now_filament_num == 0xFFu, 314);
    CHECK(motion_of(0u) == _filament_motion::idle, 315);

    // And our own next frame is answered normally: the peer frame left no
    // residue in the TX path, which is the failure mode that would turn peer
    // traffic into our own dropped replies.
    const int ours = host_bus::poll_motion_short(kSelf, kStatuSendOut, 0u, kFlagSendOut);
    CHECK(ours == 44, 316);
    CHECK(host_bus::last_transaction().addressed_ams == kSelf, 317);
    CHECK(host_bus::last_transaction().outcome == OUTCOME_REPLIED, 318);

    return 0;
}

// The long-motion frame carries its AMS number at the same offset but swaps the
// channel and motion bytes (bambu_bus_ams.cpp:696). It is routed by a different
// dispatch arm, so peer addressing is asserted separately rather than assumed
// to follow from the short frame.
int test_peer_addressed_long_frame(void)
{
    host_bus::reset();

    const int replied = host_bus::poll_motion_long(kPeer, kStatuOnUse, kFlagOnUse, 0u);

    CHECK(replied == 0, 351);
    CHECK(host_bus::transaction_count == 1, 352);

    const host_bus::Transaction& t = host_bus::last_transaction();
    CHECK(t.rx_class == (uint8_t)bambubus_package_type::filament_motion_long, 353);
    CHECK(t.command == 0x04u, 354);
    CHECK(t.addressed_ams == kPeer, 355);
    CHECK(t.outcome == OUTCOME_FAILED, 356);
    CHECK(t.reason == REASON_NO_RESPONSE, 357);
    CHECK(host_bus::service_frame_count == 0, 358);
    CHECK(host_bus::merger_event_count == 0, 359);

    // Our own long frame is answered, and its reply is the 0x3C-long form.
    const int ours = host_bus::poll_motion_long(kSelf, kStatuSendOut, kFlagSendOut, 0u);
    CHECK(ours == 60, 360);
    CHECK(host_bus::reply[2] == 0x3Cu, 361);
    CHECK(host_bus::last_transaction().addressed_ams == kSelf, 362);
    CHECK(host_bus::last_transaction().outcome == OUTCOME_REPLIED, 363);

    return 0;
}

// ---------------------------------------------------------------------------
// Family 3: funnel routing (docs/BMCU_LOADED_LATCH_DESIGN.md section 10)
// ---------------------------------------------------------------------------

// Section 10 item 2, re-run against the real set_motion rather than the policy
// alone: the healthy unload/reload from the capture, phase order
// stop_on_use -> before_pull_back -> 0xFF pull -> send_out -> before_on_use ->
// on_use, asserting the ownership trace with no spurious releases.
int test_healthy_unload_reload_trace(void)
{
    host_bus::reset();
    load_channel(1u);
    CHECK(host_bus::merger_owner() == 1u, 401);

    // stop_on_use keeps the merger: it is gated on allow_stop, and it is not an
    // exit.
    host_bus::poll_motion_short(kSelf, kStatuStopOnUse, 1u, kFlagStopOnUse);
    CHECK(motion_of(1u) == _filament_motion::stop_on_use, 402);
    CHECK(host_bus::merger_stage() == ams_merger::stage_loaded, 403);
    CHECK(host_bus::merger_events_of(host_bus::kMergerReleased) == 0, 404);

    // before_pull_back is the commanded exit, and it releases by name.
    host_bus::poll_motion_short(kSelf, kStatuBeforePullBack, 1u, kFlagBeforePullBack);
    CHECK(motion_of(1u) == _filament_motion::before_pull_back, 405);
    CHECK(host_bus::merger_stage() == ams_merger::stage_unloaded, 406);
    CHECK(host_bus::merger_events_of(host_bus::kMergerReleased) == 1, 407);

    // The 0xFF retract. The merger is already free, so this must be a no-op on
    // the policy rather than a second release event.
    host_bus::poll_motion_short(kSelf, kStatuSendOut, kNoChannel, kFlagSendOut);
    CHECK(motion_of(1u) == _filament_motion::pull_back, 408);
    CHECK(ams[0].filament_use_flag == 0x02u, 409);
    CHECK(host_bus::merger_events_of(host_bus::kMergerReleased) == 1, 410);

    // Reload the same channel.
    host_bus::poll_motion_short(kSelf, kStatuSendOut, 1u, kFlagSendOut);
    CHECK(host_bus::merger_stage() == ams_merger::stage_unloaded, 411);

    host_bus::poll_motion_short(kSelf, kStatuBeforeOnUse, 1u, kFlagBeforeOnUse);
    CHECK(host_bus::merger_stage() == ams_merger::stage_loaded, 412);
    CHECK(host_bus::merger_owner() == 1u, 413);

    host_bus::poll_motion_short(kSelf, kStatuOnUse, 1u, kFlagOnUse);
    CHECK(motion_of(1u) == _filament_motion::on_use, 414);
    CHECK(host_bus::merger_stage() == ams_merger::stage_loaded, 415);

    // Exactly one release and one preempt across the whole conversation. The
    // preempt is the reload's send_out arriving while the channel still owned
    // the merger from... nothing: it was released at :398. So there must be
    // none.
    CHECK(host_bus::merger_events_of(host_bus::kMergerReleased) == 1, 416);
    CHECK(host_bus::merger_events_of(host_bus::kMergerPreempted) == 0, 417);
    CHECK(host_bus::merger_events_of(host_bus::kMergerAcquired) == 2, 418);

    // The invariant this trace exposes, asserted so a future change to the
    // accept table has to face it: whenever the merger is owned, the owner is
    // the channel named by now_filament_num, and its motion is in the on_use
    // family. That is what makes the idle-frame wildcard unreachable while the
    // merger is held.
    CHECK(ams[0].now_filament_num == host_bus::merger_owner(), 419);

    return 0;
}

// Section 10 item 3's companion assertion, and row 6 of the transition table:
// the owner's next on_use promotes TAIL back to LOADED without passing through
// UNLOADED. If it passed through, the flash record would churn and -- worse --
// there would be a window in which allow_stop is false.
int test_owner_on_use_promotes_tail(void)
{
    host_bus::reset();
    load_channel(1u);
    ams_state_set_tail();
    CHECK(host_bus::merger_stage() == ams_merger::stage_tail, 501);

    const int releases_before = host_bus::merger_events_of(host_bus::kMergerReleased);

    host_bus::poll_motion_short(kSelf, kStatuOnUse, 1u, kFlagOnUse);

    CHECK(host_bus::merger_stage() == ams_merger::stage_loaded, 502);
    CHECK(host_bus::merger_owner() == 1u, 503);
    CHECK(host_bus::merger_events_of(host_bus::kMergerReleased) == releases_before, 504);
    CHECK(host_bus::merger_events_of(host_bus::kMergerPreempted) == 0, 505);

    // Repeated key-zero / re-latch cycling is not a release either: the flap
    // test of section 10 item 5, at the routing level.
    for (int i = 0; i < 20; ++i)
    {
        ams_state_set_tail();
        host_bus::poll_motion_short(kSelf, kStatuOnUse, 1u, kFlagOnUse);
    }
    CHECK(host_bus::merger_stage() == ams_merger::stage_loaded, 506);
    CHECK(host_bus::merger_owner() == 1u, 507);
    CHECK(host_bus::merger_events_of(host_bus::kMergerReleased) == releases_before, 508);

    return 0;
}

// Section 10 item 6: a jammed retract holds TAIL indefinitely -- there is no
// timeout -- other channels' loads are refused while it is held, and the
// send_out preempt is the only escape. This is the argument the no-timeout
// decision rests on, run through the real accept table.
int test_stale_tail_escape(void)
{
    host_bus::reset();
    load_channel(1u);
    ams_state_set_tail();

    // Time passes. A lot of it: TAIL must not expire on a clock.
    for (int i = 0; i < 200; ++i)
    {
        host_bus::poll_motion_short(kSelf, 0x00u, kNoChannel, 0x00u);
        host_clock_advance_ms(1000u);
    }
    CHECK(host_bus::merger_stage() == ams_merger::stage_tail, 601);
    CHECK(host_bus::merger_owner() == 1u, 602);

    // Another channel's load is refused while the merger is held. The accept
    // table's allow_any is false for it, so the frame is answered but does
    // nothing -- which is why the reply alone cannot be the assertion.
    const int replied = host_bus::poll_motion_short(kSelf, kStatuBeforeOnUse, 2u,
                                                    kFlagBeforeOnUse);
    CHECK(replied == 44, 603);
    CHECK(motion_of(2u) == _filament_motion::idle, 604);
    CHECK(host_bus::merger_owner() == 1u, 605);
    CHECK(ams[0].now_filament_num == 1u, 606);

    // The escape.
    host_bus::poll_motion_short(kSelf, kStatuSendOut, 2u, kFlagSendOut);
    CHECK(host_bus::merger_stage() == ams_merger::stage_unloaded, 607);
    CHECK(host_bus::merger_events_of(host_bus::kMergerPreempted) == 1, 608);

    return 0;
}

// ---------------------------------------------------------------------------
// The reply itself
// ---------------------------------------------------------------------------

// The frame-in / reply-out core: a send_out for our own channel produces a
// well-formed 0x2C motion reply carrying the state the accept table just set.
// Asserted on the bytes handed to port_send_datas, after the 50 us turnaround
// deferral, so this is what the printer would have seen.
int test_motion_reply_bytes(void)
{
    host_bus::reset();

    const int len = host_bus::poll_motion_short(kSelf, kStatuSendOut, 1u, kFlagSendOut);

    CHECK(len == 44, 701);
    CHECK(host_bus::reply_count == 1, 702);
    CHECK(host_bus::reply[0] == 0x3Du, 703);
    CHECK(host_bus::reply[1] == 0xC0u, 704);      // 0xC0 | (package_num=0 << 3)
    CHECK(host_bus::reply[2] == 0x2Cu, 705);
    CHECK(host_bus::reply[4] == 0x03u, 706);
    CHECK(host_bus::reply[5] == kSelf, 707);
    CHECK(host_bus::reply[7] == 0x02u, 708);      // filament_use_flag, send_out
    CHECK(host_bus::reply[8] == 1u, 709);         // filament_channel
    CHECK(host_bus::reply[38] == 1u, 710);        // filament_channel_2, echoed

    // pressure 0x4700, little-endian, is the send_out value from :285. Reading
    // it back off the wire is how this test knows the accept table ran rather
    // than the frame merely being answered.
    CHECK(host_bus::reply[13] == 0x00u, 711);
    CHECK(host_bus::reply[14] == 0x47u, 712);

    CHECK(package_check_crc16(host_bus::reply, len), 713);

    const host_bus::Transaction& t = host_bus::last_transaction();
    CHECK(t.outcome == OUTCOME_REPLIED, 714);
    CHECK(t.reason == REASON_OK, 715);
    CHECK(t.response_length == 44u, 716);
    CHECK(t.addressed_ams == kSelf, 717);

    CHECK(host_bus::service_frame_count == 1, 718);
    CHECK(host_bus::service_frames[0] == 0u, 719);  // kServiceMotion

    return 0;
}

// An offline AMS answers nothing at all, even for its own address. Included
// because it is the one other way a frame addressed to us produces no reply,
// and telling it apart from peer traffic is the point of family 2.
int test_offline_ams_is_silent(void)
{
    host_bus::reset();
    host_bus::set_ams_online(false);

    const int len = host_bus::poll_motion_short(kSelf, kStatuSendOut, 1u, kFlagSendOut);

    CHECK(len == 0, 801);
    const host_bus::Transaction& t = host_bus::last_transaction();
    CHECK(t.addressed_ams == kSelf, 802);
    CHECK(t.outcome == OUTCOME_FAILED, 803);
    CHECK(t.reason == REASON_NO_RESPONSE, 804);

    // The service counter is deliberately upstream of the online filter, so an
    // offline unit still records that the printer was talking to it.
    CHECK(host_bus::service_frame_count == 1, 805);

    return 0;
}

// A randomised walk over the frame alphabet, asserting after every frame that
// the merger owner is the channel named by now_filament_num and that its motion
// is in the family the idle catch-all at :440 returns early on.
//
// This is here because the claim it checks is what makes the first regression
// vector honest. Arguing it from the code is possible -- the two acquire sites
// are inside the before_on_use and on_use branches, and each release sits where
// the motion leaves that family -- but the argument has to hold across an
// accept table with five phases, two frame layouts, a wildcard channel and a
// preempt, which is exactly the kind of reasoning that produced this week's two
// defects. So it is searched rather than argued.
//
// Deterministic seed: a failure is reproducible and bisectable.
int test_merger_ownership_invariant_walk(void)
{
    host_bus::reset();

    const uint8_t channels[] = {0u, 1u, 2u, 3u, 0xFFu};
    const uint8_t statuses[] = {0x00u, 0x01u, 0x03u, 0x07u, 0x09u};
    const uint8_t flags[] = {0x00u, 0x3Fu, 0x7Fu, 0xA5u};

    uint32_t rng = 0x1BADB002u;
    auto next = [&rng](uint32_t n) {
        rng ^= rng << 13; rng ^= rng >> 17; rng ^= rng << 5;
        return rng % n;
    };

    for (int step = 0; step < 40000; ++step)
    {
        // One frame in eleven is the debounced key-zero edge that Motion_control
        // raises, rather than a printer frame.
        if (next(11u) == 0u) ams_state_set_tail();
        else if (next(6u) == 0u)
            host_bus::poll_motion_long(kSelf, statuses[next(5u)], flags[next(4u)],
                                       channels[next(5u)]);
        else
            host_bus::poll_motion_short(kSelf, statuses[next(5u)],
                                        channels[next(5u)], flags[next(4u)]);

        host_clock_advance_ms(1u + next(40u));

        const uint8_t owner = host_bus::merger_owner();
        if (owner >= 4u) continue;

        if (ams[0].now_filament_num != owner)
        {
            printf("  step %d: merger owner %u but now_filament_num %u\n",
                   step, owner, ams[0].now_filament_num);
            return 901;
        }

        const _filament_motion m = motion_of(owner);
        if (m != _filament_motion::on_use &&
            m != _filament_motion::before_on_use &&
            m != _filament_motion::stop_on_use)
        {
            printf("  step %d: merger owned by %u whose motion is %u\n",
                   step, owner, (unsigned)m);
            return 902;
        }
    }

    // The consequence: across the whole walk the wildcard release at :461 was
    // never once offered to the policy while a TAIL was held. If this ever
    // stops being true, the refusal path in ams_merger::release has become
    // load-bearing at the routing level and the first regression vector above
    // needs rewriting around the sequence that got here.
    if (host_bus::merger_events_of(host_bus::kMergerRefusedTail) != 0)
    {
        printf("  wildcard release reached a TAIL %d time(s)\n",
               host_bus::merger_events_of(host_bus::kMergerRefusedTail));
        return 903;
    }

    // The above is only a statement about the routing if the wildcard ran at
    // all. It runs constantly -- the catch-all at :440 is most of an idle bus --
    // and every one of those calls found the merger already free.
    CHECK(host_bus::wildcard_release_calls > 1000, 908);
    CHECK(host_bus::named_release_calls > 100, 909);

    // Sanity: the walk actually exercised the states it claims to cover, rather
    // than passing by never leaving UNLOADED.
    CHECK(host_bus::merger_events_of(host_bus::kMergerAcquired) > 100, 904);
    CHECK(host_bus::merger_events_of(host_bus::kMergerReleased) > 100, 905);
    CHECK(host_bus::merger_events_of(host_bus::kMergerToTail) > 100, 906);
    CHECK(host_bus::merger_events_of(host_bus::kMergerPreempted) > 10, 907);

    return 0;
}

struct Vector
{
    const char* name;
    int (*run)(void);
};

const Vector kVectors[] = {
    {"idle_frames_do_not_take_tail", test_idle_frames_do_not_take_tail},
    {"send_out_preempts_tail", test_send_out_preempts_tail},
    {"peer_addressed_short_frame", test_peer_addressed_short_frame},
    {"peer_addressed_long_frame", test_peer_addressed_long_frame},
    {"healthy_unload_reload_trace", test_healthy_unload_reload_trace},
    {"owner_on_use_promotes_tail", test_owner_on_use_promotes_tail},
    {"stale_tail_escape", test_stale_tail_escape},
    {"motion_reply_bytes", test_motion_reply_bytes},
    {"offline_ams_is_silent", test_offline_ams_is_silent},
    {"merger_ownership_invariant_walk", test_merger_ownership_invariant_walk},
};

constexpr int kVectorCount = (int)(sizeof(kVectors) / sizeof(kVectors[0]));

} // namespace

int main(int argc, char** argv)
{
    if (argc == 2 && strcmp(argv[1], "--list") == 0)
    {
        for (int i = 0; i < kVectorCount; ++i) printf("%s\n", kVectors[i].name);
        return 0;
    }

    if (argc == 2)
    {
        for (int i = 0; i < kVectorCount; ++i)
            if (strcmp(argv[1], kVectors[i].name) == 0) return kVectors[i].run();

        printf("unknown vector: %s\n", argv[1]);
        return 1;
    }

    // No argument: run them all in one process. Convenient for a local run, but
    // the runner uses one process per vector -- see the note at the top.
    for (int i = 0; i < kVectorCount; ++i)
    {
        const int code = kVectors[i].run();
        if (code) { printf("%s failed with %d\n", kVectors[i].name, code); return code; }
    }
    return 0;
}
