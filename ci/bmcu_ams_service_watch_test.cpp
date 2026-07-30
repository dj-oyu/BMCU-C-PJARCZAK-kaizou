#include "bmcu_ams_service_watch.h"

using namespace bmcu_ams_service;

namespace
{
// Hardware SysTick rate; the readable sections use 1 tick per ms instead.
constexpr uint32_t kHwTpms = 18000u;

Watch fresh(uint32_t tick, uint32_t tpms)
{
    Watch watch = {};
    poll(watch, tick, tpms, nullptr); // latch the baseline only
    return watch;
}
}

int main()
{
    // Thresholds are named constants a capture can retune.
    if (kReofferConfirmAgeMs != 3000u) return 1;
    if (kReofferServiceGapMs != 1500u) return 2;
    if (kServiceGapEventMs != 5000u) return 3;
    if (kServiceKindCount != 3u) return 4;

    // (1) The first poll only latches a baseline: no accumulation, no counters.
    {
        Watch watch = {};
        Events events = {};
        poll(watch, 123456u, kHwTpms, &events);
        if (!watch.initialized || watch.last_poll_tick != 123456u) return 10;
        if (watch.gap_now_ms != 0u || watch.gap_max_ms != 0u) return 11;
        if (watch.ms_since_confirm != 0u || watch.registration_query_count != 0u) return 12;
        if (events.gap_event || events.reoffer_event) return 13;
        // A second poll one second later does accumulate.
        poll(watch, 123456u + kHwTpms * 1000u, kHwTpms, &events);
        if (watch.gap_now_ms != 1000u) return 14;
    }

    // (2) Exact remainder carry at the hardware tick rate.
    {
        Watch watch = fresh(0u, kHwTpms);
        poll(watch, 27000u, kHwTpms, nullptr);
        if (watch.gap_now_ms != 1u) return 20;
        if (watch.tick_remainder != 9000u) return 21;
        poll(watch, 54000u, kHwTpms, nullptr);
        if (watch.gap_now_ms != 3u) return 22;
        if (watch.tick_remainder != 0u) return 23;
    }

    // (3) Tick wrap is handled by unsigned subtraction.
    {
        Watch watch = fresh(0xFFFFFFF0u, 1u);
        poll(watch, 0x00000020u, 1u, nullptr);
        if (watch.gap_now_ms != 0x30u) return 30;
    }

    // (4) gap_max_ms stays put until the first service frame, then tracks gap_now.
    {
        Watch watch = fresh(0u, 1u);
        poll(watch, 9000u, 1u, nullptr);
        if (watch.gap_now_ms != 9000u) return 40;
        if (watch.gap_max_ms != 0u) return 41; // BMCU booted before the printer
        note_service(watch, kServiceMotion, 9000u, 1u, nullptr);
        if (watch.gap_now_ms != 0u || !watch.have_service) return 42;
        if (watch.gap_max_ms != 0u) return 43;
        poll(watch, 11000u, 1u, nullptr);
        if (watch.gap_max_ms != 2000u) return 44;
        note_service(watch, kServiceMotion, 11000u, 1u, nullptr);
        poll(watch, 11500u, 1u, nullptr);
        if (watch.gap_max_ms != 2000u) return 45; // monotonic
        poll(watch, 15000u, 1u, nullptr);
        if (watch.gap_max_ms != 4000u) return 46;
    }

    // (5) Per-kind counters are independent and saturate at 0xFFFF.
    {
        Watch watch = fresh(0u, 1u);
        note_service(watch, kServiceMotion, 1u, 1u, nullptr);
        note_service(watch, kServiceMcOnline, 2u, 1u, nullptr);
        note_service(watch, kServiceMcOnline, 3u, 1u, nullptr);
        if (watch.service_count[kServiceMotion] != 1u) return 50;
        if (watch.service_count[kServiceStuMotion] != 0u) return 51;
        if (watch.service_count[kServiceMcOnline] != 2u) return 52;
        note_service(watch, kServiceKindCount, 4u, 1u, nullptr); // ignored
        if (watch.service_count[kServiceMotion] != 1u) return 53;
        watch.service_count[kServiceStuMotion] = 0xFFFEu;
        note_service(watch, kServiceStuMotion, 5u, 1u, nullptr);
        if (watch.service_count[kServiceStuMotion] != 0xFFFFu) return 54;
        note_service(watch, kServiceStuMotion, 6u, 1u, nullptr);
        if (watch.service_count[kServiceStuMotion] != 0xFFFFu) return 55;
    }

    // (6) A confirm latches registration and restarts the session registers.
    {
        Watch watch = fresh(0u, 1u);
        note_service(watch, kServiceMotion, 0u, 1u, nullptr);
        poll(watch, 4000u, 1u, nullptr);
        note_confirm(watch, 4000u, 1u, nullptr);
        if (!watch.registered || !watch.confirm_seen) return 60;
        if (watch.confirm_count != 1u) return 61;
        if (watch.ms_since_confirm != 0u) return 62;
        if (watch.gap_max_since_confirm_ms != 0u) return 63;
        if (watch.gap_max_ms != 4000u) return 64; // since-boot register untouched
    }

    // (7) gap_max_since_confirm is capped by the session length.
    {
        Watch watch = fresh(0u, 1u);
        note_service(watch, kServiceMotion, 0u, 1u, nullptr);
        note_confirm(watch, 8000u, 1u, nullptr);
        poll(watch, 10000u, 1u, nullptr);
        if (watch.gap_now_ms != 10000u) return 70;
        if (watch.ms_since_confirm != 2000u) return 71;
        if (watch.gap_max_since_confirm_ms != 2000u) return 72;
        if (watch.gap_max_ms != 10000u) return 73;
    }

    // (8) note_reset counts edges only: a lapsed heartbeat calls it every loop.
    {
        Watch watch = fresh(0u, 1u);
        note_confirm(watch, 0u, 1u, nullptr);
        note_reset(watch, 10u, 1u, nullptr);
        if (watch.registered) return 80;
        if (watch.reset_count != 1u) return 81;
        note_reset(watch, 11u, 1u, nullptr);
        note_reset(watch, 12u, 1u, nullptr);
        note_reset(watch, 13u, 1u, nullptr);
        if (watch.reset_count != 1u) return 82;
        note_confirm(watch, 20u, 1u, nullptr);
        note_reset(watch, 21u, 1u, nullptr);
        if (watch.reset_count != 2u) return 83;
        if (watch.confirm_count != 2u) return 84;
    }

    // (9) Predicate truth table and would_reoffer accounting.
    {
        Watch watch = fresh(0u, 1u);
        watch.have_service = true;
        watch.gap_now_ms = 9000u;
        watch.ms_since_confirm = 9000u;
        if (reoffer_armed(watch)) return 90; // never armed while unregistered
        note_query(watch, 0u, 1u, nullptr);
        if (watch.registration_query_count != 1u) return 91;
        if (watch.would_reoffer_count != 0u) return 92;

        watch.registered = true;
        watch.confirm_seen = true;
        watch.ms_since_confirm = 2999u;
        watch.gap_now_ms = 5000u;
        if (reoffer_armed(watch)) return 93;
        watch.ms_since_confirm = 3000u;
        watch.gap_now_ms = 1499u;
        if (reoffer_armed(watch)) return 94;
        if (service_stale(watch)) return 95;
        watch.gap_now_ms = 1500u;
        if (!reoffer_armed(watch)) return 96;
        if (!service_stale(watch)) return 97;

        note_query(watch, 0u, 1u, nullptr);
        if (watch.registration_query_count != 2u) return 98;
        if (watch.would_reoffer_count != 1u) return 99;
    }

    // (10) The re-offer event fires once per arming episode.
    {
        Watch watch = fresh(0u, 1u);
        note_confirm(watch, 0u, 1u, nullptr);
        note_service(watch, kServiceMotion, 0u, 1u, nullptr);

        Events events = {};
        poll(watch, 3000u, 1u, &events);
        if (!events.reoffer_event || events.gap_event) return 100;
        Events again = {};
        poll(watch, 3100u, 1u, &again);
        if (again.reoffer_event) return 101;
        Events gap = {};
        poll(watch, 5000u, 1u, &gap);
        if (!gap.gap_event || gap.reoffer_event) return 102;

        note_service(watch, kServiceMotion, 5000u, 1u, nullptr);
        Events quiet = {};
        poll(watch, 6000u, 1u, &quiet);
        if (quiet.reoffer_event || quiet.gap_event) return 103;
        Events rearmed = {};
        poll(watch, 8000u, 1u, &rearmed);
        if (!rearmed.reoffer_event) return 104;
    }

    // (11) The gap event fires once per silence episode and re-arms after service.
    {
        Watch watch = fresh(0u, 1u);
        note_service(watch, kServiceMotion, 0u, 1u, nullptr);
        Events first = {};
        poll(watch, 5000u, 1u, &first);
        if (!first.gap_event) return 110;
        Events second = {};
        poll(watch, 20000u, 1u, &second);
        if (second.gap_event) return 111;
        note_service(watch, kServiceStuMotion, 20000u, 1u, nullptr);
        Events third = {};
        poll(watch, 24999u, 1u, &third);
        if (third.gap_event) return 112;
        Events fourth = {};
        poll(watch, 25000u, 1u, &fourth);
        if (!fourth.gap_event) return 113;
    }

    // A nullptr poll never consumes an edge a later polled emission would report.
    {
        Watch watch = fresh(0u, 1u);
        note_service(watch, kServiceMotion, 0u, 1u, nullptr);
        poll(watch, 6000u, 1u, nullptr);
        Events events = {};
        poll(watch, 6001u, 1u, &events);
        if (!events.gap_event) return 120;
    }

    // (12) Millisecond accumulators saturate instead of wrapping.
    {
        Watch watch = fresh(0u, 1u);
        note_service(watch, kServiceMotion, 0u, 1u, nullptr);
        watch.gap_now_ms = 0xFFFFFFF0u;
        watch.confirm_seen = true;
        watch.ms_since_confirm = 0xFFFFFFF0u;
        poll(watch, 1000u, 1u, nullptr);
        if (watch.gap_now_ms != 0xFFFFFFFFu) return 130;
        if (watch.ms_since_confirm != 0xFFFFFFFFu) return 131;
        if (watch.gap_max_ms != 0xFFFFFFFFu) return 132;
        if (sat_add32(0xFFFFFFFFu, 1u) != 0xFFFFFFFFu) return 133;
    }

    // A carry overflow inside the tick accumulator clamps to one honest full
    // tick wrap, not to a saturated 49.7 days.
    {
        constexpr uint32_t kWrapMs = 0xFFFFFFFFu / kHwTpms; // 238609 ms
        Watch watch = fresh(1u, kHwTpms);
        watch.tick_remainder = 5u;
        poll(watch, 0u, kHwTpms, nullptr); // elapsed == 0xFFFFFFFF
        if (watch.gap_now_ms != kWrapMs) return 140;
        if (watch.tick_remainder != 0u) return 141;

        // The same stall with have_service set must not poison the monotonic
        // since-boot maximum with a false 0xFFFFFFFF.
        Watch served = fresh(1u, kHwTpms);
        note_service(served, kServiceMotion, 1u, kHwTpms, nullptr);
        served.tick_remainder = 5u;
        poll(served, 0u, kHwTpms, nullptr);
        if (served.gap_now_ms != kWrapMs) return 142;
        if (served.gap_max_ms != kWrapMs) return 143;
        if (served.gap_max_ms == 0xFFFFFFFFu) return 144;
    }

    // (13) The sub-millisecond carry is shared clock state, not gap state: a
    // service frame must not discard it. Polls here land on a non-integer
    // number of milliseconds, so the remainder is non-zero at every step —
    // clearing it in note_service() would stop the session clock dead.
    {
        constexpr uint32_t kStep = 1500u;   // 1/12 ms at the hardware tick rate
        constexpr uint32_t kSteps = 2400u;  // 200 ms of true elapsed time
        Watch watch = fresh(0u, kHwTpms);
        note_confirm(watch, 0u, kHwTpms, nullptr);
        uint32_t tick = 0u;
        for (uint32_t step = 1u; step <= kSteps; ++step)
        {
            tick += kStep;
            if (step % 3u == 0u)
                note_service(watch, kServiceMotion, tick, kHwTpms, nullptr);
            else
                poll(watch, tick, kHwTpms, nullptr);
        }
        const uint32_t true_ms = (kSteps * kStep) / kHwTpms;
        if (watch.service_count[kServiceMotion] != kSteps / 3u) return 160;
        if (watch.ms_since_confirm > true_ms) return 161;
        if (true_ms - watch.ms_since_confirm > 1u) return 162;
        // The remainder is genuinely exercised: a run that never carries would
        // pass the bound above for the wrong reason.
        poll(watch, tick + 1u, kHwTpms, nullptr);
        if (watch.tick_remainder != 1u) return 163;
    }

    // (14) An edge report names the gap that fired it, even when the entry
    // point raising the edge goes on to clear gap_now_ms.
    {
        Watch watch = fresh(0u, 1u);
        note_service(watch, kServiceMotion, 0u, 1u, nullptr);
        Events polled = {};
        poll(watch, 7000u, 1u, &polled);
        if (!polled.gap_event || polled.gap_ms != 7000u) return 170;

        Watch arrival_watch = fresh(0u, 1u);
        note_service(arrival_watch, kServiceMotion, 0u, 1u, nullptr);
        Events arrival = {};
        note_service(arrival_watch, kServiceMotion, 9000u, 1u, &arrival);
        if (!arrival.gap_event) return 171;
        if (arrival.gap_ms != 9000u) return 172;   // not the post-reset zero
        if (arrival_watch.gap_now_ms != 0u) return 173;

        Watch armed_watch = fresh(0u, 1u);
        note_confirm(armed_watch, 0u, 1u, nullptr);
        note_service(armed_watch, kServiceMotion, 0u, 1u, nullptr);
        Events armed = {};
        note_query(armed_watch, 4000u, 1u, &armed);
        if (!armed.reoffer_event || armed.gap_event) return 174;
        if (armed.gap_ms != 4000u) return 175;
        Events quiet = {};
        poll(armed_watch, 4100u, 1u, &quiet);
        if (quiet.gap_event || quiet.reoffer_event || quiet.gap_ms != 0u) return 176;
    }

    // (15) Boot boundary: note_service() as the literal first operation on a
    // zero-initialised Watch, with no prior poll to establish the baseline.
    {
        Watch watch = {};
        Events events = {};
        note_service(watch, kServiceMotion, 555u, kHwTpms, &events);
        if (!watch.initialized) return 180;
        if (watch.last_poll_tick != 555u) return 181;
        if (watch.gap_now_ms != 0u) return 182;
        if (!watch.have_service) return 183;
        if (watch.service_count[kServiceMotion] != 1u) return 184;
        if (watch.gap_max_ms != 0u || watch.ms_since_confirm != 0u) return 185;
        if (events.gap_event || events.reoffer_event || events.gap_ms != 0u) return 186;
        // The baseline it latched is the frame tick, so the next poll measures
        // from the service frame and nothing from before boot leaks in.
        poll(watch, 555u + kHwTpms * 250u, kHwTpms, nullptr);
        if (watch.gap_now_ms != 250u) return 187;
        if (watch.gap_max_ms != 250u) return 188;
    }

    // A zero tick rate (pre-init) is inert rather than a divide by zero.
    {
        Watch watch = fresh(0u, 0u);
        note_service(watch, kServiceMotion, 100u, 0u, nullptr);
        poll(watch, 200u, 0u, nullptr);
        if (watch.gap_now_ms != 0u) return 150;
        if (watch.service_count[kServiceMotion] != 1u) return 151;
    }

    return 0;
}
