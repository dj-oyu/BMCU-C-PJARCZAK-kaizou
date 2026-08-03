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
//   UNLOADED    no channel owns the merger
//   LOADED(ch)  ch owns the merger, and its filament still reaches the online
//               key, so the sensor can still see it
//   TAIL(ch)    ch still owns the merger, but its tail has passed the online
//               key -- the strand lies between the switch and the extruder and
//               the sensor can no longer see any of it
//
// This is ownership, not occupancy, and the two are not yet the same thing.
// The commanded retract paths release at command time, not at completion:
// before_pull_back at bambu_bus_ams.cpp:395 and the read_num 0xFF unload at
// :425 both release the moment the command is accepted, while the strand is
// still being pulled. So on every retract, runout or not, the merger is
// occupied-but-unowned from that release until redetect finishes, and a retract
// that jams leaves it that way indefinitely. That is the pre-existing behaviour
// and this change does not alter it; closing that gap means moving those
// releases to completion, which is a separate change in the opposite direction
// to this one. Do not read UNLOADED as "the tube is empty".
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
// Two transitions in docs/BMCU_LOADED_LATCH_DESIGN.md have no code here, and
// both are absent on purpose rather than missed.
//
// Row 5, TAIL -> UNLOADED when the retract sequence terminates, is unreachable
// while row 3 is deferred. The commanded retract releases at command time --
// :395 and :425 -- so the merger is already UNLOADED long before the physical
// completion row 5 watches for. Implementing it would mean writing a transition
// out of a state the code has by then left. It becomes meaningful only if row 3
// moves those releases to completion, and it should be implemented together
// with row 3 or not at all.
//
// Row 6, TAIL -> LOADED on a debounced key-close with motion idle, is covered
// by acquire(). Filament arriving back at the key on the owning channel is
// followed by the printer's next before_on_use or on_use, and that promotes.
// The only difference is how long the wire bits lag the physical change, and
// nothing gates on the difference: allow_any and allow_stop already answer the
// same in TAIL and LOADED for the owner. Row 6 would also be actively wrong
// after a runout, where new filament at the key says nothing about the old tail
// still lying in the bowden -- the state it would leave is less accurate than
// the one it replaces.
//
// Header-only, allocation-free, no loops.
namespace ams_merger
{

static const uint8_t kNoChannel = 0xFFu;
static const uint8_t kChannels = 4u;

enum Stage : uint8_t
{
    stage_unloaded = 0u,
    stage_loaded = 1u,
    stage_tail = 2u,
};

// Invariant: tail is only ever set while owner < kChannels. Every mutator below
// preserves it and restore() re-establishes it, so the nine legal states are the
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

enum ReleaseResult : uint8_t
{
    release_none = 0u,          // nothing was held; no change
    release_done = 1u,          // the merger was released
    release_refused_tail = 2u,  // a session release met a TAIL and left it held
    release_preempted_tail = 3u // a claiming channel took the merger off a TAIL
};

// A printer-commanded release naming a channel, or -- with `ch >= kChannels` --
// ending the session outright.
//
// A session release frees LOADED but not TAIL. TAIL has to be named.
//
// This is the difference between fixing the reported symptom and not. The idle
// reset at bambu_bus_ams.cpp:458 passes 0xFF, and idle frames keep arriving
// while a printer sits paused. On a runout the sequence is: the tail clears the
// switch, TAIL is entered, the printer pauses and waits for a human, and idle
// frames flow the whole time. A session release that freed TAIL would drop the
// merger during that pause, and the retract prelude in the morning would be
// refused for want of allow_stop -- which is the original complaint, arriving
// by a new route. It is also exactly why TAIL has no timeout: an unbounded
// pause is expected, so nothing may quietly expire during one.
//
// The commanded exits are unaffected because they all name a channel:
// before_pull_back at :395, the read_num 0xFF unload at :425, and the
// statu_flags 0x01 path at :431 all pass ams_ptr->now_filament_num.
//
// The other caller that passes 0xFF is send_out at :275, and it means something
// else entirely -- see preempt() below. It does not come through here.
inline uint8_t release(State& s, uint8_t ch)
{
    if (is_free(s)) return release_none;

    if (ch >= kChannels)
    {
        if (s.tail) return release_refused_tail;
        reset(s);
        return release_done;
    }

    if (s.owner != ch) return release_none;

    reset(s);
    return release_done;
}

// Another channel is claiming the merger: bambu_bus_ams.cpp:275, on send_out.
//
// This frees TAIL, where a session release does not, and the distinction is the
// whole reason the two are separate functions. Both spell "no particular
// channel" on the wire, so before they were split they collided and one of them
// had to be wrong.
//
// Why a preempt must succeed even though TAIL means the tube is still occupied:
// the printer is master. send_out is accepted unconditionally and the printer
// will drive filament into the merger whether or not the BMCU agrees. Refusing
// does not prevent the collision, it only leaves the BMCU's idea of who owns
// the merger wrong while the collision happens -- and wrong in the direction
// that then refuses the new channel's own retract, because allow_stop would
// still name the old owner. Yielding is the lesser damage and the honest
// record.
//
// It is also the escape hatch the no-timeout decision rests on. TAIL is never
// expired by a clock, so the argument that a stale TAIL is recoverable depends
// entirely on this edge: whatever else happens, the next load of any channel
// takes the merger back. Without it TAIL would have neither a fuse nor an
// escape, which is the stale-lock failure the timeout was rejected for
// avoiding.
//
// Preempting a TAIL is reported rather than silent. It is the witness that a
// strand was still in the tube when the next one was pushed in, and that is
// worth seeing in a log after a failed print.
inline uint8_t preempt(State& s)
{
    if (is_free(s)) return release_none;

    const bool was_tail = (s.tail != 0u);
    reset(s);
    return was_tail ? release_preempted_tail : release_done;
}

// The online key of the owning channel has read empty continuously for the
// debounce window: the tail has passed the switch.
//
// This is the transition that used to be a release. It is deliberately not a
// release now, and deliberately has no timeout back to UNLOADED -- see the
// commentary at the LOADED_LATCH_DROP_MS call site in Motion_control.cpp.
//
// It names no channel, unlike every other edge here. There is only one value it
// could ever be given -- the owner, which this state already holds -- so a
// parameter would exist solely to be checked against what it was derived from,
// and would be one more thing a caller could get wrong. Dropping it measured as
// zero flash either way; it is here because the edge cannot address the wrong
// channel if it cannot address a channel at all.
inline bool to_tail(State& s)
{
    if (is_free(s) || s.tail) return false;

    s.tail = 1u;
    return true;
}

// Rebuild the state from flash at boot.
//
// The two halves arrive separately and that is deliberate: the channel keeps
// exactly the meaning it has always had in the STA record, 0..3 or 0xFF, and
// TAIL is carried by a record tag that older firmware skips entirely. See
// STA_TAIL_TAG in Flash_saves.cpp for why the split is worth a second record
// rather than a spare bit.
//
// The invariant is re-established here rather than trusted: tail without a
// valid owner is discarded, so a channel byte that fails its range check can
// never leave the merger held by nobody.
inline void restore(State& s, uint8_t owner, bool tail)
{
    if (owner >= kChannels)
    {
        reset(s);
        return;
    }

    s.owner = owner;
    s.tail = tail ? 1u : 0u;
}

}
