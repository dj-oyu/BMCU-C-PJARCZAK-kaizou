#pragma once

#include <stdint.h>

// Where each channel's strand is, estimated from measured travel. See
// docs/strand_zone_design.md for the geometry, the rules and the assumptions;
// this header is the rules alone, header-only and allocation-free so that it
// compiles on the host and is driven there by ci/strand_zone_policy_test.cpp
// and by the Quint trace replay (ci/quint_replay_adapter.cpp).
//
// Positions are AS5600 counts of forward travel (towards the head) measured
// from the inner online switch. The firmware feeds every sample's signed
// forward delta and the switch reading; everything else follows.
//
// Build with -DBMCU_DOWNSTREAM_STRAND=0 to turn the estimate off: the app_api
// entry points then do nothing and every zone reads NONE.
#ifndef BMCU_DOWNSTREAM_STRAND
#define BMCU_DOWNSTREAM_STRAND 1
#endif

namespace strand_zone
{

static const uint8_t kChannels = 4u;

enum Zone : uint8_t
{
    kNone = 0u,        // nothing known at or past the switch
    kUpstream = 1u,    // front known, short of the merger
    kDownstream = 2u,  // front past the merger
    kTail = 3u,        // a tail piece (or a remnant) may be past the merger
};

inline bool is_downstream(uint8_t zone)
{
    return zone == kDownstream || zone == kTail;
}

struct Config
{
    int32_t park;        // merger entrance: where autoload parks the tip
    int32_t down_set;    // front beyond this reads as past the merger
    int32_t down_clear;  // and below this as back upstream (hysteresis)
    int32_t head;        // the nozzle; travel past it is filament consumed
    int32_t slack;       // measurement drift allowed at the switch
};

struct State
{
    int32_t front;       // downstream end, valid when front_known
    int8_t last_dir;     // sign of the last non-zero delta
    uint8_t key;         // last switch reading (1 = filament at the switch)
    uint8_t front_known;
    uint8_t tail_known;  // the strand's tail passed the switch going forward
    uint8_t remnant;     // an earlier tail piece may still be downstream
    uint8_t exact;       // front measured from a switch edge, not assumed
    uint8_t reached_head; // measured travel from an exact front reached the head
    uint8_t zone;
};

inline uint8_t derive_zone(const State& s, const Config& c)
{
    if (s.remnant) return kTail;
    if (s.tail_known)
        return (s.front_known && s.front > c.down_clear) ? kTail : kUpstream;
    if (!s.front_known) return s.key ? kUpstream : kNone;
    if (s.zone == kDownstream) return (s.front < c.down_clear) ? kUpstream : kDownstream;
    return (s.front > c.down_set) ? kDownstream : kUpstream;
}

inline void reset(State& s)
{
    s.front = 0;
    s.last_dir = 0;
    s.key = 0u;
    s.front_known = 0u;
    s.tail_known = 0u;
    s.remnant = 0u;
    s.exact = 0u;
    s.reached_head = 0u;
    s.zone = kNone;
}

inline int32_t clamp(int32_t v, int32_t lo, int32_t hi)
{
    return v < lo ? lo : (v > hi ? hi : v);
}

// One sample. `fwd_delta` is signed forward travel since the last sample, or
// ignored when `measured` is false (AS5600 not good); `driving_fwd` says the
// motor is pushing forward during an unmeasured sample. Returns true when the
// zone changed, so the caller persists and reports.
inline bool update(State& s, const Config& c, int32_t fwd_delta, bool measured,
                   bool driving_fwd, bool key_now)
{
    const uint8_t before = s.zone;

    if (measured)
    {
        if (fwd_delta != 0)
        {
            s.last_dir = fwd_delta > 0 ? 1 : -1;
            if (s.front_known)
            {
                s.front = clamp(s.front + fwd_delta, -c.head, c.head);
                // Only measured travel to the head counts as passage; a
                // front placed there by restore or an assumption does not.
                if (fwd_delta < 0) s.reached_head = 0u;
                else if (s.exact && s.front >= c.head) s.reached_head = 1u;
            }
        }
    }
    else if (driving_fwd)
    {
        // Unseen forward travel: assume it went past the merger. Unseen
        // backward travel never clears anything.
        s.front_known = 1u;
        s.exact = 0u;
        s.reached_head = 0u;
        if (s.front <= c.down_set) s.front = c.down_set + 1;
        s.last_dir = 1;
    }

    const uint8_t key = key_now ? 1u : 0u;
    if (key && !s.key)
    {
        if (s.tail_known)
        {
            // Either a new strand behind the old one's tail -- a tail piece
            // that was past the merger is still there, ahead of it -- or a
            // float that read as the tail passing. The switch cannot tell
            // them apart, so the front is kept where travel says the old one
            // was: an estimate ahead of a new strand over-reports, one behind
            // a strand that never left would under-report.
            if (derive_zone(s, c) == kTail) s.remnant = 1u;
            s.tail_known = 0u;
            s.reached_head = 0u;
            if (!s.front_known || s.front <= 0)
            {
                s.front_known = 1u;
                s.front = 0;
                s.exact = 1u;
            }
            else
            {
                s.exact = 0u;
            }
        }
        else if (!s.front_known || s.front <= 0)
        {
            // The front arrived at the switch.
            s.front_known = 1u;
            s.front = 0;
            s.exact = 1u;
            s.reached_head = 0u;
        }
        // Otherwise the strand is continuous through the switch and this is
        // the end of a float the debounce let through: the front is where
        // travel says it is, never reset to the switch.
    }
    else if (!key && s.key)
    {
        if (s.last_dir > 0)
        {
            s.tail_known = 1u;            // the tail went past going forward
        }
        else if (s.last_dir < 0 && (!s.front_known || s.front <= c.slack))
        {
            s.front_known = 1u;           // the front went past going back
            if (s.front > 0) s.front = 0;
            s.exact = 1u;
        }
        // Going back with the front still well past the switch, or with no
        // motion: a lever float. Nothing moved that travel did not see.
        // last_dir == 0: a lever float with no motion; nothing moved.
    }
    s.key = key;

    s.zone = derive_zone(s, c);
    return s.zone != before;
}

// Channel `ch`'s front reached the head. The tube is a single bore, so no
// other strand and no remnant can be in it: pull every other estimate back to
// the junction (down_set, design assumption E4). Returns the mask of channels whose zone changed.
inline uint8_t passage_proof(State* all, const Config& c, uint8_t ch)
{
    uint8_t changed = 0u;
    for (uint8_t i = 0u; i < kChannels; i++)
    {
        if (i == ch) continue;
        State& s = all[i];
        const uint8_t before = s.zone;
        s.remnant = 0u;
        s.tail_known = 0u;
        s.reached_head = 0u;
        if (s.front_known && s.front > c.down_set) s.exact = 0u;
        // At most the junction: the tube is clear, but a strand may sit
        // anywhere in its own branch up to there.
        if (s.front_known && s.front > c.down_set) s.front = c.down_set;
        // Proven out of the tube: evaluate without the hysteresis memory.
        if (s.zone == kDownstream) s.zone = kUpstream;
        s.zone = derive_zone(s, c);
        if (s.zone != before) changed = static_cast<uint8_t>(changed | (1u << i));
    }
    return changed;
}

inline bool at_head(const State& s, const Config& c)
{
    return s.reached_head && s.front_known && !s.tail_known && !s.remnant && s.front >= c.head;
}

// Two bits per channel, channel 0 in bits 0-1.
inline uint8_t pack(const State* all)
{
    uint8_t v = 0u;
    for (uint8_t i = 0u; i < kChannels; i++)
        v = static_cast<uint8_t>(v | ((all[i].zone & 0x03u) << (2u * i)));
    return v;
}

inline uint8_t downstream_mask(const State* all)
{
    uint8_t m = 0u;
    for (uint8_t i = 0u; i < kChannels; i++)
        if (is_downstream(all[i].zone)) m = static_cast<uint8_t>(m | (1u << i));
    return m;
}

// Rebuild one channel from its stored zone and the switch reading at boot.
// `owner` forces DOWNSTREAM: owning the tube means using it.
inline void restore(State& s, const Config& c, uint8_t stored_zone, bool key_now, bool owner)
{
    reset(s);
    s.key = key_now ? 1u : 0u;
    uint8_t z = static_cast<uint8_t>(stored_zone & 0x03u);
    if (owner && z != kTail) z = kDownstream;
    switch (z)
    {
    case kDownstream:
        s.front_known = 1u;
        s.front = c.head;
        break;
    // A strand known only to be short of the merger may be anywhere in its
    // branch up to the junction, so it restores there, not at park: an
    // estimate behind the real front would under-report its next push.
    case kTail:
        s.remnant = 1u;
        if (key_now) { s.front_known = 1u; s.front = c.down_set; }
        break;
    default:
        if (key_now) { s.front_known = 1u; s.front = c.down_set; }
        break;
    }
    s.zone = derive_zone(s, c);
}

}
