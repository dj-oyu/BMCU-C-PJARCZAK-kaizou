#pragma once

// Observe-only instrumentation for HMS 0500_409D (issue #3, phase 1).
//
// Measures the inter-arrival gap of printer frames addressed to THIS AMS and
// mirrors the online_detect registration latch, so a hardware capture can tell
// a BMCU-side session latch (H1) apart from a printer-side authorization lock
// (H2). Nothing here is ever read back into a printer-bus decision: the module
// only measures, and the candidate re-offer predicate is evaluated but never
// acted on.
//
// Pure, header-only, allocation-free and loop-free: every entry point is O(1)
// with a single 32-bit divide, safe on the timing-critical main loop. Not
// ISR-safe (no atomics); call from main-loop context only.

#include <stdint.h>

namespace bmcu_ams_service
{
// Candidate re-offer predicate thresholds. Named so a capture can retune them.
constexpr uint32_t kReofferConfirmAgeMs = 3000u;
constexpr uint32_t kReofferServiceGapMs = 1500u;
// First crossing of this gap in one silence episode raises a bounded event.
constexpr uint32_t kServiceGapEventMs = 5000u;

enum ServiceKind : uint8_t
{
    kServiceMotion = 0u,     // 0x03 filament_motion_short addressed to this AMS
    kServiceStuMotion = 1u,  // 0x04 filament_motion_long addressed to this AMS
    kServiceMcOnline = 2u,   // 0x21A MC_online long frame naming this AMS
    kServiceKindCount = 3u,
};

// Edge reports produced by an entry point. Initialise with {} at every call.
struct Events
{
    bool gap_event;
    bool reoffer_event;
    // The gap that triggered the edge above, latched at the moment the edge is
    // raised. An entry point may zero gap_now_ms after raising an edge (a
    // service frame both ends the silence and is the reason the edge fires), so
    // a caller that reads the live register would report 0 instead of the value
    // that fired the event.
    uint32_t gap_ms;
};

struct Watch
{
    uint32_t last_poll_tick;
    uint32_t tick_remainder;
    uint32_t gap_now_ms;
    uint32_t gap_max_ms;
    uint32_t gap_max_since_confirm_ms;
    uint32_t ms_since_confirm;
    uint16_t service_count[kServiceKindCount];
    uint16_t registration_query_count;
    uint16_t would_reoffer_count;
    uint16_t confirm_count;
    uint16_t reset_count;
    bool initialized;
    bool have_service;
    bool registered;
    bool confirm_seen;
    bool gap_event_latched;
    bool reoffer_event_latched;
};

inline uint32_t sat_add32(uint32_t base, uint32_t delta)
{
    const uint32_t sum = base + delta;
    return sum < base ? 0xFFFFFFFFu : sum;
}

inline void sat_inc16(uint16_t& counter)
{
    if (counter != 0xFFFFu) ++counter;
}

// Candidate predicate. Computed for telemetry only; never gates a bus decision.
inline bool reoffer_armed(const Watch& watch)
{
    return watch.registered && watch.confirm_seen &&
           watch.ms_since_confirm >= kReofferConfirmAgeMs &&
           watch.gap_now_ms >= kReofferServiceGapMs;
}

inline bool service_stale(const Watch& watch)
{
    return watch.gap_now_ms >= kReofferServiceGapMs;
}

// Advances the millisecond accumulators. `events` may be nullptr, in which case
// edges are neither reported nor latched, so a snapshot-time poll can never
// swallow an event that a later poll would have emitted.
inline void poll(Watch& watch, uint32_t now_tick, uint32_t ticks_per_ms, Events* events)
{
    if (!watch.initialized)
    {
        // First call only latches a baseline: no accumulation, so the module is
        // immune to init ordering against the rest of the link.
        watch.initialized = true;
        watch.last_poll_tick = now_tick;
        return;
    }
    if (ticks_per_ms == 0u) return;

    const uint32_t elapsed = static_cast<uint32_t>(now_tick - watch.last_poll_tick);
    watch.last_poll_tick = now_tick;
    const uint32_t total = elapsed + watch.tick_remainder;
    uint32_t elapsed_ms;
    if (total < elapsed)
    {
        // Carry overflow can only happen after a pathological poll gap; clamp
        // rather than fold the measurement back around zero. The honest bound
        // is one full tick wrap, not 0xFFFFFFFF milliseconds: a saturated
        // elapsed_ms would pin gap_now_ms and latch a false 49.7-day maximum
        // into the never-resetting gap_max_ms register.
        elapsed_ms = 0xFFFFFFFFu / ticks_per_ms;
        watch.tick_remainder = 0u;
    }
    else
    {
        elapsed_ms = total / ticks_per_ms;
        watch.tick_remainder = total % ticks_per_ms;
    }

    watch.gap_now_ms = sat_add32(watch.gap_now_ms, elapsed_ms);
    if (watch.confirm_seen)
        watch.ms_since_confirm = sat_add32(watch.ms_since_confirm, elapsed_ms);

    // Monotonic max registers: a slow or lossy readout link can delay a read
    // but never corrupt the value.
    if (watch.have_service && watch.gap_now_ms > watch.gap_max_ms)
        watch.gap_max_ms = watch.gap_now_ms;
    if (watch.confirm_seen)
    {
        // A gap that started before the confirm is capped at the session length.
        const uint32_t capped = watch.gap_now_ms < watch.ms_since_confirm
            ? watch.gap_now_ms : watch.ms_since_confirm;
        if (capped > watch.gap_max_since_confirm_ms)
            watch.gap_max_since_confirm_ms = capped;
    }

    const bool armed = reoffer_armed(watch);
    if (!armed) watch.reoffer_event_latched = false;
    if (events == nullptr) return;
    if (watch.have_service && watch.gap_now_ms >= kServiceGapEventMs && !watch.gap_event_latched)
    {
        watch.gap_event_latched = true;
        events->gap_event = true;
        events->gap_ms = watch.gap_now_ms;
    }
    if (armed && !watch.reoffer_event_latched)
    {
        watch.reoffer_event_latched = true;
        events->reoffer_event = true;
        events->gap_ms = watch.gap_now_ms;
    }
}

// A valid printer frame of `kind` carried our AMS number. Deliberately counted
// on address match alone, i.e. a superset of "the BMCU handled it": what
// discriminates H1 from H2 is whether the printer still addresses us.
inline void note_service(Watch& watch, uint8_t kind, uint32_t now_tick,
                         uint32_t ticks_per_ms, Events* events)
{
    if (kind >= kServiceKindCount) return;
    poll(watch, now_tick, ticks_per_ms, events);
    sat_inc16(watch.service_count[kind]);
    watch.gap_now_ms = 0u;
    // tick_remainder is deliberately NOT cleared: it is the shared sub-
    // millisecond carry of the tick->ms conversion, not gap state. Zeroing it
    // here discards up to one millisecond of real time per service frame, which
    // at the printer's 0x03/0x04 rate makes ms_since_confirm (and every
    // predicate built on it) run systematically slow.
    watch.gap_event_latched = false;
    watch.have_service = true;
}

// A 0x05 online_detect frame with subtype 0x00 (registration query) was seen,
// counted even when the have_registered latch swallows it without answering.
inline void note_query(Watch& watch, uint32_t now_tick, uint32_t ticks_per_ms, Events* events)
{
    poll(watch, now_tick, ticks_per_ms, events);
    sat_inc16(watch.registration_query_count);
    if (reoffer_armed(watch)) sat_inc16(watch.would_reoffer_count);
}

inline void note_confirm(Watch& watch, uint32_t now_tick, uint32_t ticks_per_ms, Events* events)
{
    poll(watch, now_tick, ticks_per_ms, events);
    watch.registered = true;
    watch.confirm_seen = true;
    sat_inc16(watch.confirm_count);
    watch.ms_since_confirm = 0u;
    watch.gap_max_since_confirm_ms = 0u;
    watch.reoffer_event_latched = false;
}

// Edge-guarded: online_detect_reset() fires on every loop iteration while the
// heartbeat is lapsed and on every online_detect frame while the AMS is
// offline, so only registered -> unregistered transitions are counted.
inline void note_reset(Watch& watch, uint32_t now_tick, uint32_t ticks_per_ms, Events* events)
{
    poll(watch, now_tick, ticks_per_ms, events);
    if (!watch.registered) return;
    watch.registered = false;
    sat_inc16(watch.reset_count);
}
}
