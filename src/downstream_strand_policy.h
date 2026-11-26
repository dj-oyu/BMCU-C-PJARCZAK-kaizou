#pragma once

#include <stdint.h>

// Which channels may still have filament downstream of the merger.
//
// The merger latch (ams_merger_policy.h) records who *owns* the shared output
// tube, and it is released at command time: before_pull_back and the 0xFF
// unload free it before the strand has moved, and a send_out on another channel
// preempts it outright. So ownership cannot answer the physical question "has
// this channel's strand been pushed past the merger and not brought back yet".
// That question has two answers the firmware could not tell apart before this
// bit existed:
//
//   PARKED  idle, both switches closed, tip waiting at the merger entrance --
//           reached by a finished autoload, or by a finished retract;
//   ORPHAN  idle, both switches closed, tip still in the tool head -- reached
//           when the printer moved on to another channel (send_out preempting
//           LOADED) without retracting this one first.
//
// Every variable the firmware already keeps reads the same in both. The
// existing g_last_on_use_exit_ms comes close, but it is cleared on entering
// send, so a strand preempted while still in send (load held at the gear) reads
// as PARKED, and it lives in RAM only. The difference is history, so it is kept
// as history: one bit per channel, set where the strand leaves for the head and
// cleared where it is known to have come back.
//
//   set    the channel enters send -- the only motion that drives a strand from
//          the merger entrance towards the head;
//   clear  a printer retract finishes (redetect finds the key again), or the
//          auto-unload gesture finishes with the key empty (the strand was
//          pulled out of the loader entirely).
//
// Nothing else clears it. A motion fault, a preempt, a bus loss, TAIL and a
// reboot all leave the strand wherever it was, so the bit stays set: the
// failure direction of a wrongly-set bit is a spurious report, and of a
// wrongly-cleared one is the collision this exists to witness.
//
// Flash-backed beside the merger record, because the strand survives a power
// cycle. With no record the mask restores as zero -- the pre-change behaviour.
//
// This commit only records and reports. Nothing gates on it yet.
//
// Header-only, allocation-free, no loops, so it compiles on the host.
namespace downstream_strand
{

static const uint8_t kChannels = 4u;
static const uint8_t kAllMask = 0x0Fu;

struct State
{
    uint8_t mask; // bit ch set: ch's strand may be downstream of the merger
};

inline void reset(State& s)
{
    s.mask = 0u;
}

inline uint8_t bit(uint8_t ch)
{
    return (ch < kChannels) ? static_cast<uint8_t>(1u << ch) : 0u;
}

inline bool is_set(const State& s, uint8_t ch)
{
    return (s.mask & bit(ch)) != 0u;
}

// Channels other than `ch` that may still be downstream.
inline uint8_t others(const State& s, uint8_t ch)
{
    return static_cast<uint8_t>(s.mask & static_cast<uint8_t>(~bit(ch)) & kAllMask);
}

// The channel started driving towards the head. Returns true when the mask
// changed, so the caller knows to persist it.
inline bool mark_sent(State& s, uint8_t ch)
{
    const uint8_t b = bit(ch);
    if (b == 0u || (s.mask & b) != 0u) return false;
    s.mask = static_cast<uint8_t>(s.mask | b);
    return true;
}

// The strand is known to be back upstream of the merger. Returns true when the
// mask changed.
inline bool mark_returned(State& s, uint8_t ch)
{
    const uint8_t b = bit(ch);
    if ((s.mask & b) == 0u) return false;
    s.mask = static_cast<uint8_t>(s.mask & static_cast<uint8_t>(~b));
    return true;
}

// Rebuild from flash. Bits for channels that do not exist are discarded.
inline void restore(State& s, uint8_t stored_mask)
{
    s.mask = static_cast<uint8_t>(stored_mask & kAllMask);
}

}
