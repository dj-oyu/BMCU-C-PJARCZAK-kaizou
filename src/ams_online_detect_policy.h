#pragma once

#include <stdint.h>

// Pure decision logic for the AMS online-detect (registration) handshake.
//
// The printer broadcasts 0x05/0x00 registration queries. Historically the BMCU
// stayed silent for every such query once it had confirmed a registration
// (have_registered latched), which is correct while the printer still holds the
// registration - but if the printer forgets it without the heartbeat lapsing,
// the BMCU never answers again and the printer raises HMS 0500_409D until a
// physical power cycle.
//
// The 0x00 broadcast carries no addressee, so a "forgot us" re-query is
// byte-identical to routine discovery traffic. The discriminator has to be
// behavioural: a printer that still holds the registration keeps servicing this
// AMS (0x03 motion / 0x04 stu_motion / 0x21A MC_online, all filtered to our AMS
// number) at sub-second cadence; one that forgot it goes silent towards us and
// only broadcasts registration queries.
//
// Header-only, allocation-free, no loops - safe on the printer RX/TX path.
namespace ams_online_detect
{

enum class QueryAction : uint8_t
{
    stay_silent = 0,
    offer_first = 1,
    offer_repeat = 2,
};

struct Config
{
    uint32_t confirm_settle_ticks;  // registration must be at least this old
    uint32_t service_silence_ticks; // ... and per-AMS traffic silent this long
};

struct State
{
    uint8_t phase;            // 0=idle, 1=first offer, 2=repeat offer, 3=registered
    bool registered;
    bool confirm_settled;     // sticky: confirm older than confirm_settle_ticks
    bool service_stale;       // sticky: no per-AMS traffic for service_silence_ticks
    uint32_t last_confirm_tick;
    uint32_t last_service_tick;
};

inline void reset(State& s)
{
    s.phase = 0u;
    s.registered = false;
    s.confirm_settled = false;
    s.service_stale = false;
    s.last_confirm_tick = 0u;
    s.last_service_tick = 0u;
}

// Called from handlers already filtered to our AMS number: proof the printer is
// actively treating this unit as a registered AMS.
inline void note_service_traffic(State& s, uint32_t now)
{
    s.last_service_tick = now;
    s.service_stale = false;
}

// Runs every main-loop pass. Flags latch stickily so a 32-bit tick wrap
// (~238 s on STK_CNTL) can never alias a comparison: each unsigned diff is
// evaluated while the elapsed time is far below 2^31, and once a flag is set no
// later wrapped diff can clear it.
inline void poll(State& s, const Config& c, uint32_t now)
{
    if (!s.registered) return;

    if (!s.confirm_settled &&
        (uint32_t)(now - s.last_confirm_tick) >= c.confirm_settle_ticks)
        s.confirm_settled = true;

    if (!s.service_stale &&
        (uint32_t)(now - s.last_service_tick) >= c.service_silence_ticks)
        s.service_stale = true;
}

// A 0x05/buf[5]==0x00 registration broadcast arrived (after the caller's own
// busy / ams_num / offline guards).
inline QueryAction on_query(State& s)
{
    if (s.registered)
    {
        if (!(s.confirm_settled && s.service_stale)) return QueryAction::stay_silent;

        // The printer stopped servicing us as a registered AMS yet is still
        // broadcasting registration queries: drop the latch and restart the
        // two-step offer from the fresh-handshake prefix.
        s.registered = false;
        s.confirm_settled = false;
        s.service_stale = false;
        s.phase = 1u;
        return QueryAction::offer_first;
    }

    if (s.phase == 0u)
    {
        s.phase = 1u;
        return QueryAction::offer_first;
    }

    s.phase = 2u;
    return QueryAction::offer_repeat;
}

// Only called once the confirm packet has been fully validated by the caller.
// Seeding last_service_tick gives a service-silence grace window so the
// handshake tail cannot immediately re-arm the re-offer.
inline void latch_confirm(State& s, uint32_t now)
{
    s.registered = true;
    s.phase = 3u;
    s.confirm_settled = false;
    s.service_stale = false;
    s.last_confirm_tick = now;
    s.last_service_tick = now;
}

}
