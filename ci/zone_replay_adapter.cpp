// Replays Quint traces of spec/strand_zone.qnt against src/strand_zone_policy.h
// and fails on the first step where the firmware's estimate differs from the
// spec's.
//
// The spec carries both the physical strands and a transcription of the
// estimate; its invariant `sound` checks the transcription against physics.
// This program checks the transcription against the real header: it feeds
// each step's samples to strand_zone::update / passage_proof / restore the way
// Motion_control.cpp's zone_sample / Motion_control_zone_claimed /
// Motion_control_zone_restore do, and compares every field of every channel.
//
// Not modelled: the 50 ms switch debounce in zone_sample (the spec's switch
// edges are already debounced), and channel 3 (the spec has three channels;
// the header's fourth stays empty, which passage_proof leaves alone).
//
// Units: one spec unit is kScale counts here. The spec moves whole units and
// takes the sign of the last move as the direction; with dir_band 1 below a
// unit, the header's travel integrator agrees with it on every move.
//
// Input, one event per line (whitespace separated):
//   <trace> <step> <action> <ch> <delta> <measured> <driving> <key> <owner>
//   then, for each spec channel:
//   <zone> <front> <known> <tail_known> <remnant> <exact> <reached_head>
//   <last_dir> <key> <key_valid> <claimed>
// `key` after <driving> is the switch reading of <ch> after the step. A line
// "reset <trace>" starts a new trace. ci/quint_replay.py writes this format.

#include "strand_zone_policy.h"

#include <stdio.h>
#include <string.h>

namespace
{

const int kSpecChannels = 3;
const int32_t kScale = 4;

// spec/strand_zone.qnt geometry: PARK 2, SET 4, CLEAR 3, HEAD 7, KEY_BACK -1,
// CONSUME 1.
const strand_zone::Config kConfig = {2 * kScale, 4 * kScale, 3 * kScale, 7 * kScale, 1,
                                     -1 * kScale, 1 * kScale};

struct Machine
{
    strand_zone::State zone[strand_zone::kChannels];
    uint8_t stored;  // packed zones as last persisted (spec assumption A5)
    bool stored_valid;
};

void reset(Machine& m)
{
    for (uint8_t i = 0u; i < strand_zone::kChannels; i++) strand_zone::reset(m.zone[i]);
    m.stored = 0u;
    m.stored_valid = true;
}

// Motion_control.cpp zone_sample, without the debounce.
// `consuming`: the channel owns the merger (the firmware also asks for on_use,
// which the spec does not model apart from ownership).
void sample(Machine& m, uint8_t ch, int delta, bool measured, bool driving, bool key,
            bool consuming)
{
    strand_zone::update(m.zone[ch], kConfig, delta * kScale, measured, driving, key,
                        consuming);
    if (strand_zone::take_passage(m.zone[ch], kConfig))
        strand_zone::passage_proof(m.zone, kConfig, ch);
}

// Returns false for an action name this adapter does not know.
bool apply(Machine& m, const char* action, int ch, int delta, bool measured, bool driving,
           bool key, int owner, const bool* boot_keys)
{
    const uint8_t c = static_cast<uint8_t>(ch < 0 ? 0 : ch);

    if (!strcmp(action, "fwd") || !strcmp(action, "back") || !strcmp(action, "runout"))
    {
        // The AS5600 loop: one sample with the measured delta, or an
        // unmeasured one while the motor drives forward.
        sample(m, c, delta, measured, driving, key, owner == ch);
        return true;
    }
    if (!strcmp(action, "float"))
    {
        // The switch reads empty for one sample and back, with no travel.
        sample(m, c, 0, true, false, false, owner == ch);
        sample(m, c, 0, true, false, true, owner == ch);
        return true;
    }
    if (!strcmp(action, "claim"))
    {
        // Motion_control_zone_claimed.
        strand_zone::claim(m.zone[c], kConfig);
        return true;
    }
    if (!strcmp(action, "reboot"))
    {
        // Motion_control_zone_restore, then zone_sample's first settled
        // reading of each switch.
        for (uint8_t i = 0u; i < strand_zone::kChannels; i++)
        {
            const uint8_t z = static_cast<uint8_t>((m.stored >> (2u * i)) & 0x03u);
            strand_zone::restore(m.zone[i], kConfig, z, m.stored_valid,
                                 owner == static_cast<int>(i));
            const bool k = i < kSpecChannels ? boot_keys[i] : false;
            strand_zone::update(m.zone[i], kConfig, 0, true, false, k, false);
        }
        return true;
    }
    if (!strcmp(action, "loseRecord"))
    {
        m.stored = 0u;
        m.stored_valid = false;
        return true;
    }

    // Events the BMCU cannot see.
    static const char* const kInert[] = {
        "init", "insert", "consume", "asFail", "asRecover", "release",
    };
    for (size_t i = 0; i < sizeof(kInert) / sizeof(kInert[0]); i++)
        if (!strcmp(action, kInert[i])) return true;
    return false;
}

struct Expect
{
    int zone, front, known, tail_known, remnant, exact, reached_head, last_dir, key, key_valid,
        claimed;
};

bool same(const strand_zone::State& s, const Expect& e)
{
    // The front is meaningful only once known; the spec keeps it at 0 before.
    return s.zone == e.zone && s.front_known == e.known &&
           (!e.known || s.front == e.front * kScale) && s.tail_known == e.tail_known &&
           s.remnant == e.remnant && s.exact == e.exact &&
           s.reached_head == e.reached_head && s.last_dir == e.last_dir && s.key == e.key &&
           s.key_valid == e.key_valid && s.claimed == e.claimed;
}

void print_state(const char* who, int ch, const strand_zone::State& s)
{
    fprintf(stderr,
            "  %-8s ch%d zone=%d front=%d known=%d tail=%d remnant=%d exact=%d head=%d "
            "dir=%d key=%d key_valid=%d claimed=%d (front in counts, %d per spec unit)\n",
            who, ch, s.zone, static_cast<int>(s.front), s.front_known, s.tail_known, s.remnant,
            s.exact, s.reached_head, s.last_dir, s.key, s.key_valid, s.claimed,
            static_cast<int>(kScale));
}

}

int main(void)
{
    Machine m;
    reset(m);

    char line[1024];
    unsigned long events = 0;
    while (fgets(line, sizeof(line), stdin))
    {
        char trace[128];
        if (sscanf(line, "reset %127s", trace) == 1)
        {
            reset(m);
            continue;
        }

        char action[64];
        int step, ch, delta, measured, driving, key, owner, used;
        if (sscanf(line, "%127s %d %63s %d %d %d %d %d %d%n", trace, &step, action, &ch, &delta,
                   &measured, &driving, &key, &owner, &used) != 9)
        {
            fprintf(stderr, "bad line: %s", line);
            return 2;
        }
        Expect exp[kSpecChannels];
        const char* p = line + used;
        for (int i = 0; i < kSpecChannels; i++)
        {
            Expect& e = exp[i];
            int n = 0;
            if (sscanf(p, "%d %d %d %d %d %d %d %d %d %d %d%n", &e.zone, &e.front, &e.known,
                       &e.tail_known, &e.remnant, &e.exact, &e.reached_head, &e.last_dir,
                       &e.key, &e.key_valid, &e.claimed, &n) != 11)
            {
                fprintf(stderr, "bad line: %s", line);
                return 2;
            }
            p += n;
        }

        bool boot_keys[kSpecChannels];
        for (int i = 0; i < kSpecChannels; i++) boot_keys[i] = exp[i].key != 0;
        if (!apply(m, action, ch, delta, measured != 0, driving != 0, key != 0, owner,
                   boot_keys))
        {
            fprintf(stderr, "%s step %d: unknown action %s\n", trace, step, action);
            return 2;
        }
        // main.cpp persists on any zone change (A5: immediately), and every
        // write re-creates a lost record. The spec persists after every
        // sample, claim and reboot; a write of unchanged zones is a no-op.
        static const char* const kPersists[] = {"fwd", "back", "runout", "float", "claim",
                                                "reboot"};
        bool persists = false;
        for (size_t i = 0; i < sizeof(kPersists) / sizeof(kPersists[0]); i++)
            if (!strcmp(action, kPersists[i])) persists = true;
        if (persists)
        {
            m.stored = strand_zone::pack(m.zone, kConfig);
            m.stored_valid = true;
        }
        events++;

        for (int i = 0; i < kSpecChannels; i++)
        {
            if (same(m.zone[i], exp[i])) continue;
            fprintf(stderr, "%s step %d: %s(ch=%d delta=%d measured=%d driving=%d) diverged\n",
                    trace, step, action, ch, delta, measured, driving);
            strand_zone::State s;
            strand_zone::reset(s);
            s.zone = static_cast<uint8_t>(exp[i].zone);
            s.front = exp[i].front * kScale;
            s.front_known = static_cast<uint8_t>(exp[i].known);
            s.tail_known = static_cast<uint8_t>(exp[i].tail_known);
            s.remnant = static_cast<uint8_t>(exp[i].remnant);
            s.exact = static_cast<uint8_t>(exp[i].exact);
            s.reached_head = static_cast<uint8_t>(exp[i].reached_head);
            s.last_dir = static_cast<int8_t>(exp[i].last_dir);
            s.key = static_cast<uint8_t>(exp[i].key);
            s.key_valid = static_cast<uint8_t>(exp[i].key_valid);
            s.claimed = static_cast<uint8_t>(exp[i].claimed);
            print_state("spec:", i, s);
            print_state("firmware:", i, m.zone[i]);
            return 1;
        }
    }

    printf("replayed %lu zone events, no divergence\n", events);
    return 0;
}
