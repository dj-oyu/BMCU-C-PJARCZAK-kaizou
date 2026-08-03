#pragma once

#include <stdint.h>

// Ownership of the shared PTFE merger, as a three-state latch.
//
// Four channels feed one output tube, so exactly one strand may occupy it.
// g_loaded_ch is not a status flag, it is the mutex on that tube, and it is
// flash-backed because the physical occupancy survives a reboot.
//
// Two states were not enough to describe what the hardware can be doing. The
// only sensor is the online microswitch at the entry to each channel, and it
// answers "is filament resting on this switch", not "is the merger free". On a
// normal filament change the printer commands the retract while the strand is
// still on the switch, so the two questions have the same answer. On a runout
// they do not: the tail passes the switch first, and the printer's retract
// arrives afterwards. Reading key-zero as "the merger is free" therefore
// released the mutex while a metre of filament was still in the merger and the
// bowden -- and the release itself then refused the retract, because
// before_pull_back and stop_on_use are gated on still owning the mutex. That is
// why automatic switchover to a paired spool has never worked.
//
//   UNLOADED    nothing occupies the merger
//   LOADED(ch)  ch occupies the merger, and its filament still reaches the
//               online key, so the sensor can still see it
//   TAIL(ch)    ch still occupies the merger, but its tail has passed the
//               online key -- the strand lies between the switch and the
//               extruder and the sensor can no longer see any of it
//
// LOADED and TAIL are both ownership. Every predicate phrased as "does channel
// ch hold the merger" must be true in both, which is why owner is stored as a
// channel index that both states carry: the existing `loaded == ch` tests in
// bambu_bus_ams.cpp become correct in TAIL without being touched, rather than
// each site having to remember to admit the new state. A representation where
// the fix has to be repeated per consumer is a representation where one
// consumer will be missed, and being missed at exactly one consumer is the
// defect this file exists to close.
//
// Header-only, allocation-free, no loops.
namespace ams_merger
{

static const uint8_t kNoChannel = 0xFFu;
static const uint8_t kChannels = 4u;

// Stored form, one byte, which is also the flash form -- see encode/decode.
static const uint8_t kStoredFree = 0xFFu;
static const uint8_t kStoredTailFlag = 0x80u;

enum Stage : uint8_t
{
    stage_unloaded = 0u,
    stage_loaded = 1u,
    stage_tail = 2u,
};

// Invariant: tail is only ever set while owner < kChannels. Every mutator below
// preserves it and decode() re-establishes it, so the nine legal states are the
// only ones this struct can hold. There is deliberately no bit pattern spare
// for an illegal one: an owner index cannot name two channels at once, so
// "two channels hold the merger" is unrepresentable rather than merely
// detectable, and the flash record is already integrity-checked a level down
// (Flash_AMS_state_read skips any slot failing its duplicate-word XOR), so a
// decayed byte never reaches here to be misread in the first place.
struct State
{
    uint8_t owner; // kNoChannel when the merger is free
    uint8_t tail;  // 1 once owner's tail has passed the online key
};

inline void reset(State& s)
{
    s.owner = kNoChannel;
    s.tail = 0u;
}

inline bool owns(const State& s, uint8_t ch)
{
    return (ch < kChannels) && (s.owner == ch);
}

inline bool is_free(const State& s)
{
    return s.owner >= kChannels;
}

inline bool is_tail(const State& s)
{
    return s.tail != 0u;
}

inline uint8_t stage(const State& s)
{
    if (is_free(s)) return stage_unloaded;
    return s.tail ? stage_tail : stage_loaded;
}

// A printer-commanded load of ch: before_on_use or on_use.
//
// From TAIL(ch) this promotes rather than being refused. Filament reaching the
// switch again on the channel that already owns the merger is that channel
// being reloaded, not a second claimant.
//
// A load of a channel while a different one owns the merger stays refused, as
// it was before this state existed. It is unreachable from today's two callers,
// both of which sit behind an allow_any that already excludes it.
inline bool acquire(State& s, uint8_t ch)
{
    if (ch >= kChannels) return false;

    if (s.owner == ch)
    {
        if (!s.tail) return false;
        s.tail = 0u;
        return true;
    }

    if (!is_free(s)) return false;

    s.owner = ch;
    s.tail = 0u;
    return true;
}

// A printer-commanded release. `ch >= kChannels` means "whatever is held",
// which is how the send_out and idle-reset paths address it.
//
// Releases from TAIL exactly as it does from LOADED. This is the only way out
// of TAIL, and it is reached from all five existing call sites without any of
// them changing: before_pull_back, the two read_num 0xFF unload paths, the
// 0xFF idle reset, and another channel's send_out.
inline bool release(State& s, uint8_t ch)
{
    if (is_free(s)) return false;
    if (ch < kChannels && s.owner != ch) return false;

    reset(s);
    return true;
}

// The online key of the owning channel has read empty continuously for the
// debounce window: the tail has passed the switch.
//
// This is the transition that used to be a release. It is deliberately not a
// release now, and deliberately has no timeout back to UNLOADED -- see the
// commentary at the LOADED_LATCH_DROP_MS call site in Motion_control.cpp.
inline bool to_tail(State& s, uint8_t ch)
{
    if (ch >= kChannels || s.owner != ch || s.tail) return false;

    s.tail = 1u;
    return true;
}

// Flash form. The record already carries a whole byte holding 0..3 or 0xFF, so
// TAIL rides in the top bit of the same byte and no format version changes:
//
//   0x00..0x03  LOADED(ch), which is what every record written before this
//               change means, and the conservative reading of one
//   0x80..0x83  TAIL(ch)
//   anything else, including 0xFF, the merger is free
//
// Both directions of a firmware swap stay sane. New firmware reads an old
// record as LOADED, which is what it meant. Old firmware reads a new TAIL
// record as 0x80|ch, fails its own `ch < 4` guard, and treats it as free --
// that is the pre-change behaviour, not a new failure.
inline uint8_t encode(const State& s)
{
    if (is_free(s)) return kStoredFree;
    return (uint8_t)(s.owner | (s.tail ? kStoredTailFlag : 0u));
}

inline void decode(State& s, uint8_t stored)
{
    const uint8_t ch = (uint8_t)(stored & (uint8_t)~kStoredTailFlag);
    if (ch >= kChannels)
    {
        reset(s);
        return;
    }

    s.owner = ch;
    s.tail = (stored & kStoredTailFlag) ? 1u : 0u;
}

}
