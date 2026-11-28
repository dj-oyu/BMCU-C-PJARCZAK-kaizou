// Host test for src/long_reply_table.h.

#include "long_reply_table.h"

#define CHECK(cond, code) do { if (!(cond)) return (code); } while (0)

using long_reply_table::Result;
using long_reply_table::Table;

static Result set(Table& t, uint16_t type, const uint8_t* match, uint8_t m,
                  const uint8_t* reply, uint8_t r, uint8_t flags = 0u)
{
    uint8_t p[5 + 8 + 32 + 4] = {};
    p[0] = static_cast<uint8_t>(type);
    p[1] = static_cast<uint8_t>(type >> 8);
    p[2] = flags;
    p[3] = m;
    p[4] = r;
    for (uint8_t i = 0u; i < m; ++i) p[5 + i] = match[i];
    for (uint8_t i = 0u; i < r; ++i) p[5 + m + i] = reply[i];
    return long_reply_table::apply(t, p, static_cast<uint8_t>(5 + m + r));
}

int main(void)
{
    static_assert(sizeof(long_reply_table::Entry) == 44u, "entry size changed");
    static_assert(sizeof(Table) == 352u, "table size changed");

    const uint8_t m00[] = {0x00};
    const uint8_t m01[] = {0x01};
    const uint8_t r4[] = {0xA0, 0xA1, 0xA2, 0xA3};
    const uint8_t r1[] = {0xB0};
    const uint8_t frame00[] = {0x00, 0x00, 0x00, 0x00};
    const uint8_t frame01[] = {0x01, 0x00, 0x00, 0x00};

    // Empty table answers nothing.
    {
        Table t = {};
        CHECK(long_reply_table::find(t, 0x0411u, frame00, 4u) == nullptr, 11);
        CHECK(long_reply_table::used(t) == 0u, 12);
    }

    // A match keeps a unit to its own frames.
    {
        Table t = {};
        CHECK(set(t, 0x0411u, m00, 1u, r4, 4u) == Result::ok, 21);
        const long_reply_table::Entry* e = long_reply_table::find(t, 0x0411u, frame00, 4u);
        CHECK(e != nullptr && e->reply_len == 4u && e->reply[3] == 0xA3u, 22);
        CHECK(long_reply_table::find(t, 0x0411u, frame01, 4u) == nullptr, 23);
        CHECK(long_reply_table::find(t, 0x023Cu, frame00, 4u) == nullptr, 24);
        // A frame shorter than the match never matches.
        CHECK(long_reply_table::find(t, 0x0411u, frame00, 0u) == nullptr, 25);
    }

    // Same key replaces; the longest prefix wins over a match-everything entry.
    {
        Table t = {};
        CHECK(set(t, 0x0411u, nullptr, 0u, r1, 1u) == Result::ok, 31);
        CHECK(set(t, 0x0411u, m01, 1u, r4, 4u) == Result::ok, 32);
        CHECK(long_reply_table::find(t, 0x0411u, frame01, 4u)->reply_len == 4u, 33);
        CHECK(long_reply_table::find(t, 0x0411u, frame00, 4u)->reply_len == 1u, 34);
        CHECK(set(t, 0x0411u, m01, 1u, r1, 1u) == Result::ok, 35);
        CHECK(long_reply_table::used(t) == 2u, 36);
        CHECK(long_reply_table::find(t, 0x0411u, frame01, 4u)->reply[0] == 0xB0u, 37);
    }

    // Clear: with a match drops only that entry, without drops the type.
    {
        Table t = {};
        set(t, 0x0411u, m00, 1u, r4, 4u);
        set(t, 0x0411u, m01, 1u, r4, 4u);
        set(t, 0x023Cu, m00, 1u, r4, 4u);
        CHECK(set(t, 0x0411u, m00, 1u, nullptr, 0u, 1u) == Result::ok, 41);
        CHECK(long_reply_table::used(t) == 2u, 42);
        CHECK(long_reply_table::find(t, 0x0411u, frame01, 4u) != nullptr, 43);
        CHECK(set(t, 0x0411u, nullptr, 0u, nullptr, 0u, 1u) == Result::ok, 44);
        CHECK(long_reply_table::used(t) == 1u, 45);
        CHECK(long_reply_table::find(t, 0x023Cu, frame00, 4u) != nullptr, 46);
        // Clearing what is not there is fine.
        CHECK(set(t, 0x0237u, nullptr, 0u, nullptr, 0u, 1u) == Result::ok, 47);
    }

    // Full after eight distinct keys; a freed entry is reused.
    {
        Table t = {};
        for (uint8_t i = 0u; i < 8u; ++i)
        {
            const uint8_t m[] = {i};
            CHECK(set(t, 0x0411u, m, 1u, r1, 1u) == Result::ok, 51);
        }
        const uint8_t m9[] = {9};
        CHECK(set(t, 0x0411u, m9, 1u, r1, 1u) == Result::full, 52);
        // Replacing an existing key still works when full.
        CHECK(set(t, 0x0411u, m00, 1u, r4, 4u) == Result::ok, 53);
        CHECK(set(t, 0x0411u, m00, 1u, nullptr, 0u, 1u) == Result::ok, 54);
        CHECK(set(t, 0x0411u, m9, 1u, r1, 1u) == Result::ok, 55);
    }

    // Malformed payloads change nothing.
    {
        Table t = {};
        uint8_t big[64] = {};
        CHECK(long_reply_table::apply(t, nullptr, 5u) == Result::bad_value, 61);
        CHECK(long_reply_table::apply(t, big, 4u) == Result::bad_value, 62);
        CHECK(set(t, 0x0411u, nullptr, 0u, nullptr, 0u) == Result::bad_value, 63);   // set, no reply
        CHECK(set(t, 0x0411u, nullptr, 0u, r1, 1u, 1u) == Result::bad_value, 64);   // clear with reply
        CHECK(set(t, 0x0411u, nullptr, 0u, r1, 1u, 2u) == Result::bad_value, 65);   // unknown flag
        big[3] = 9u;                                                                // match too long
        big[4] = 1u;
        CHECK(long_reply_table::apply(t, big, 15u) == Result::bad_value, 66);
        big[3] = 0u;
        big[4] = 33u;                                                               // reply too long
        CHECK(long_reply_table::apply(t, big, 38u) == Result::bad_value, 67);
        big[4] = 4u;                                                                // length disagrees
        CHECK(long_reply_table::apply(t, big, 10u) == Result::bad_value, 68);
        CHECK(long_reply_table::used(t) == 0u, 69);
        // The largest legal request.
        big[3] = 8u;
        big[4] = 32u;
        CHECK(long_reply_table::apply(t, big, 45u) == Result::ok, 70);
    }

    return 0;
}
