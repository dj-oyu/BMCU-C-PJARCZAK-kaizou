#pragma once

#include <stdint.h>

// Where each channel's strand is, estimated from measured travel. See
// docs/strand_zone_design.md for the geometry, the rules and the assumptions;
// this header is the rules alone, header-only and allocation-free so that it
// compiles on the host and is driven there by ci/strand_zone_policy_test.cpp
// and by the Quint trace replay (ci/zone_replay_adapter.cpp).
//
// Positions are AS5600 counts of forward travel (towards the head) measured
// from the inner online switch. The firmware feeds every sample's signed
// forward delta and the debounced switch reading; everything else follows.
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
    int32_t dir_band;    // net travel that sets the direction (AS5600 jitter)
    int32_t key_back;    // front when the switch opens going back (<= 0)
    int32_t consume;     // travel past the head, while the printer feeds from
                         // this channel, that proves passage; 0: no proof
};

struct State
{
    int32_t front;        // downstream end, valid when front_known
    int32_t boot_front;   // where an unknown strand found at boot is put
    int32_t dir_acc;      // travel integrator for last_dir, in [-dir_band, dir_band]
    int32_t consumed;     // measured travel past the head while consuming
    int8_t last_dir;      // direction of the last sustained motion
    uint8_t key;          // last switch reading (1 = filament at the switch)
    uint8_t key_valid;    // key is a real reading, not a placeholder from boot
    uint8_t front_known;
    uint8_t tail_known;   // the strand's tail passed the switch going forward
    uint8_t remnant;      // an earlier tail piece may still be downstream
    uint8_t exact;        // front measured from a switch edge, not assumed
    uint8_t reached_head; // measured travel from an exact front reached the head,
                          // and the printer consumed `consume` past it
    uint8_t passage;      // reached_head just went 0 -> 1; take_passage() consumes it
    uint8_t zone;
};

inline uint8_t derive_zone(const State& s, const Config& c)
{
    if (s.remnant) return kTail;
    if (s.tail_known)
        // Any piece left past the switch will be pushed into the tube by the
        // next strand, so it counts as a tail piece from now on, and the
        // record keeps it across a reboot.
        return (s.front_known && s.front > 0) ? kTail : kUpstream;
    if (!s.front_known) return s.key ? kUpstream : kNone;
    if (s.zone == kDownstream) return (s.front < c.down_clear) ? kUpstream : kDownstream;
    return (s.front > c.down_set) ? kDownstream : kUpstream;
}

inline void reset(State& s)
{
    s.front = 0;
    s.boot_front = 0;
    s.dir_acc = 0;
    s.consumed = 0;
    s.last_dir = 0;
    s.key = 0u;
    s.key_valid = 1u;
    s.front_known = 0u;
    s.tail_known = 0u;
    s.remnant = 0u;
    s.exact = 0u;
    s.reached_head = 0u;
    s.passage = 0u;
    s.zone = kNone;
}

inline int32_t clamp(int32_t v, int32_t lo, int32_t hi)
{
    return v < lo ? lo : (v > hi ? hi : v);
}

// One sample. `fwd_delta` is signed forward travel since the last sample, or
// ignored when `measured` is false (AS5600 not good); `driving_fwd` says the
// motor is pushing forward during an unmeasured sample. `key_now` is the
// debounced switch reading. `consuming` says the printer is feeding from this
// channel (it owns the merger, in on_use): only then does travel past the head
// count as filament melted rather than a strand grinding against something.
// Returns true when the zone changed, so the caller persists and reports.
inline bool update(State& s, const Config& c, int32_t fwd_delta, bool measured,
                   bool driving_fwd, bool key_now, bool consuming)
{
    const uint8_t before = s.zone;
    const uint8_t key = key_now ? 1u : 0u;

    if (!s.key_valid)
    {
        // First real reading after a restore. A strand found at the switch is
        // put where the record says it can be at most; no edge is read into
        // it, since the switch did not change -- the placeholder did.
        s.key_valid = 1u;
        s.key = key;
        if (key && !s.front_known)
        {
            s.front_known = 1u;
            s.front = s.boot_front;
            s.exact = 0u;
        }
    }

    if (measured)
    {
        if (fwd_delta != 0)
        {
            // Direction is set by sustained travel, not by the sign of one
            // count: a count of jitter at rest must not turn a runout into a
            // float or a float into a runout.
            s.dir_acc = clamp(s.dir_acc + fwd_delta, -c.dir_band, c.dir_band);
            if (s.dir_acc >= c.dir_band) s.last_dir = 1;
            else if (s.dir_acc <= -c.dir_band) s.last_dir = -1;
            if (s.front_known)
            {
                const int32_t moved = s.front + fwd_delta;
                s.front = clamp(moved, -c.head, c.head);
                // Only measured travel to the head, from a front measured at
                // the switch, and on past it while the printer consumes,
                // counts as passage; a front placed there by restore or an
                // assumption does not.
                if (fwd_delta < 0)
                {
                    s.reached_head = 0u;
                    s.consumed = 0;
                }
                else if (s.exact && consuming && moved > c.head)
                {
                    s.consumed += moved - (moved - fwd_delta > c.head ? moved - fwd_delta : c.head);
                    if (!s.reached_head && c.consume > 0 && s.consumed >= c.consume)
                    {
                        s.reached_head = 1u;
                        s.passage = 1u;
                    }
                }
            }
        }
    }
    else
    {
        // Unmeasured: nothing measured holds any more.
        s.exact = 0u;
        s.reached_head = 0u;
        s.passage = 0u;
        s.consumed = 0;
        if (driving_fwd)
        {
            // Unseen forward travel: assume it went past the merger, by one
            // hysteresis band. Unseen backward travel never clears anything.
            const int32_t past = c.down_set + (c.down_set - c.down_clear);
            if (!s.front_known || s.front < past) s.front = past;
            s.front_known = 1u;
            s.dir_acc = c.dir_band;
            s.last_dir = 1;
        }
    }

    if (key && !s.key)
    {
        if (s.tail_known)
        {
            // Either a new strand behind the old one's tail -- whatever piece
            // was left past the switch is ahead of it and will be pushed into
            // the tube -- or a float that read as the tail passing. The switch
            // cannot tell them apart, so the front is kept where travel says
            // the old one was: an estimate ahead of a new strand over-reports,
            // one behind a strand that never left would under-report.
            if (s.front_known && s.front > 0) s.remnant = 1u;
            s.tail_known = 0u;
            s.reached_head = 0u;
            s.passage = 0u;
            s.consumed = 0;
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
            s.passage = 0u;
            s.consumed = 0;
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
        else if (s.last_dir < 0 && (!s.front_known || s.front <= 0))
        {
            // The front went past going back, and travel agrees: the estimate
            // only ever moves forward to meet it, never back.
            s.front_known = 1u;
            s.front = c.key_back;
            s.exact = 1u;
        }
        // Going back with the front still past the switch, or with no
        // motion: a lever float. Nothing moved that travel did not see.
    }
    s.key = key;

    s.zone = derive_zone(s, c);
    return s.zone != before;
}

// True once per arrival at the head: the caller then runs passage_proof. A
// front that stays at the head proves nothing new, and a proof repeated on
// every sample would undo every later estimate of the other channels.
inline bool take_passage(State& s, const Config& c)
{
    const bool fire = s.passage && s.reached_head && s.front_known && !s.tail_known &&
                      !s.remnant && s.front >= c.head;
    s.passage = 0u;
    return fire;
}

// Channel `ch`'s front reached the head. The tube is a single bore, so no
// other strand and no remnant can be in it: pull every other estimate back to
// the junction (down_set, design assumption E4). A tail piece whose tail
// passed the switch is out of the tube but may still be in its own branch, so
// it stays known: the next strand will push it in. Returns the mask of
// channels whose zone changed.
inline uint8_t passage_proof(State* all, const Config& c, uint8_t ch)
{
    uint8_t changed = 0u;
    for (uint8_t i = 0u; i < kChannels; i++)
    {
        if (i == ch) continue;
        State& s = all[i];
        const uint8_t before = s.zone;
        const bool in_tube = s.front_known && s.front > c.down_set;
        s.remnant = 0u;
        s.reached_head = 0u;
        s.passage = 0u;
        s.consumed = 0;
        if (in_tube)
        {
            // At most the junction: the tube is clear, but a strand may sit
            // anywhere in its own branch up to there.
            s.exact = 0u;
            s.front = c.down_set;
        }
        // Proven out of the tube: evaluate without the hysteresis memory.
        if (s.zone == kDownstream) s.zone = kUpstream;
        s.zone = derive_zone(s, c);
        if (s.zone != before) changed = static_cast<uint8_t>(changed | (1u << i));
    }
    return changed;
}

// The merger was acquired by this channel: owning the tube means using it.
inline bool claim(State& s, const Config& c)
{
    if (is_downstream(s.zone)) return false;
    s.front_known = 1u;
    s.front = c.head;
    s.exact = 0u;
    s.reached_head = 0u;
    s.passage = 0u;
    s.consumed = 0;
    s.tail_known = 0u;
    s.zone = derive_zone(s, c);
    return true;
}

// The zone as persisted: written ahead. A measured front moving up into the
// hysteresis band is stored as DOWNSTREAM before it reads so, which gives the
// write the band's travel to land before the strand can be past the merger.
// An assumed front (restored, or left at the junction by a proof) is already
// at or ahead of the real one and is stored as it reads.
inline uint8_t stored_zone(const State& s, const Config& c)
{
    if (s.zone == kUpstream && !s.tail_known && s.front_known && s.exact &&
        s.front > c.down_clear)
        return kDownstream;
    return s.zone;
}

// Two bits per channel, channel 0 in bits 0-1.
inline uint8_t pack(const State* all, const Config& c)
{
    uint8_t v = 0u;
    for (uint8_t i = 0u; i < kChannels; i++)
        v = static_cast<uint8_t>(v | ((stored_zone(all[i], c) & 0x03u) << (2u * i)));
    return v;
}

inline uint8_t downstream_mask(const State* all)
{
    uint8_t m = 0u;
    for (uint8_t i = 0u; i < kChannels; i++)
        if (is_downstream(all[i].zone)) m = static_cast<uint8_t>(m | (1u << i));
    return m;
}

// Rebuild one channel at boot from its stored zone. The switch has not been
// read yet: the first update() takes its reading without treating it as an
// edge. `valid` is false when no zone record survived (first boot after an
// upgrade, a page erase); a strand then found at the switch may be anywhere,
// so it is put at the head. `owner` forces DOWNSTREAM: owning the tube means
// using it.
inline void restore(State& s, const Config& c, uint8_t stored_zone, bool valid, bool owner)
{
    reset(s);
    s.key_valid = 0u;
    uint8_t z = static_cast<uint8_t>(stored_zone & 0x03u);
    if (owner && z != kTail) z = kDownstream;
    // Until then the zone reads as stored: the placeholder says a strand is
    // there for every zone but NONE.
    s.key = z != kNone ? 1u : 0u;
    // A strand known only to be short of the merger may be anywhere in its
    // branch up to the junction: an estimate behind the real front would
    // under-report its next push.
    s.boot_front = valid ? c.down_set : c.head;
    switch (z)
    {
    case kDownstream:
        s.front_known = 1u;
        s.front = c.head;
        break;
    case kTail:
        s.remnant = 1u;
        break;
    default:
        break;
    }
    s.zone = derive_zone(s, c);
}

}
