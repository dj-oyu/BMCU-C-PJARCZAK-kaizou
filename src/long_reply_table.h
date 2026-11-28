#pragma once

#include <stdint.h>

// Replies to printer long frames this firmware has no handler for.
//
// A printer on firmware 1.08 polls 0x0411, 0x023C and 0x0237 and has never
// been answered. Nobody knows yet what it wants, so the answer is supplied at
// run time: the Pico sends SET_LONG_REPLY (docs/BMCU_LINK_PROTOCOL_ALPHA3.md
// section 5.5) and the bus loop consults this table before it gives up on a
// frame. The table lives in RAM only. A power cycle always returns the BMCU to
// answering nothing, so a wrong reply cannot outlive the experiment.
//
// Eight entries of 44 bytes, 352 bytes in all, about 8% of the RAM left after
// .data, .bss and the stack (2026-11-28). Three frame types are unanswered
// today; the rest is room for trying more than one reply.
//
// Both BMCUs on the bus see every frame. An entry that matches a frame meant
// for the other unit makes both answer at once, so the match prefix is how a
// caller keeps each unit to its own frames.

namespace long_reply_table
{
constexpr uint8_t kEntries = 8u;
constexpr uint8_t kMaxMatch = 8u;
constexpr uint8_t kMaxReply = 32u;
constexpr uint8_t kHeaderSize = 5u;   // type:u16 | flags:u8 | match_len:u8 | reply_len:u8
constexpr uint8_t kFlagClear = 0x01u;

struct Entry
{
    uint16_t type;
    uint8_t match_len;
    uint8_t reply_len;   // 0 marks a free entry
    uint8_t match[kMaxMatch];
    uint8_t reply[kMaxReply];
};

struct Table
{
    Entry entry[kEntries];
};

enum class Result : uint8_t
{
    ok,
    bad_value,
    full,
};

inline bool same_key(const Entry& e, uint16_t type, const uint8_t* match, uint8_t match_len)
{
    if (e.reply_len == 0u || e.type != type || e.match_len != match_len) return false;
    for (uint8_t i = 0u; i < match_len; ++i)
        if (e.match[i] != match[i]) return false;
    return true;
}

// Apply one SET_LONG_REPLY payload. A set replaces the entry with the same
// type and match, or takes a free one. A clear with no match drops every entry
// of that type; with a match, only that one. Clearing nothing is still ok.
inline Result apply(Table& table, const uint8_t* payload, uint8_t length)
{
    if (payload == nullptr || length < kHeaderSize) return Result::bad_value;
    const uint16_t type = static_cast<uint16_t>(payload[0] | (payload[1] << 8));
    const uint8_t flags = payload[2];
    const uint8_t match_len = payload[3];
    const uint8_t reply_len = payload[4];
    if ((flags & static_cast<uint8_t>(~kFlagClear)) != 0u) return Result::bad_value;
    if (match_len > kMaxMatch || reply_len > kMaxReply) return Result::bad_value;
    if (length != static_cast<uint8_t>(kHeaderSize + match_len + reply_len))
        return Result::bad_value;
    const uint8_t* match = payload + kHeaderSize;
    const uint8_t* reply = match + match_len;

    if (flags & kFlagClear)
    {
        if (reply_len != 0u) return Result::bad_value;
        for (uint8_t i = 0u; i < kEntries; ++i)
        {
            Entry& e = table.entry[i];
            if (e.reply_len == 0u || e.type != type) continue;
            if (match_len == 0u || same_key(e, type, match, match_len)) e.reply_len = 0u;
        }
        return Result::ok;
    }

    if (reply_len == 0u) return Result::bad_value;
    Entry* slot = nullptr;
    for (uint8_t i = 0u; i < kEntries && slot == nullptr; ++i)
        if (same_key(table.entry[i], type, match, match_len)) slot = &table.entry[i];
    for (uint8_t i = 0u; i < kEntries && slot == nullptr; ++i)
        if (table.entry[i].reply_len == 0u) slot = &table.entry[i];
    if (slot == nullptr) return Result::full;

    slot->type = type;
    slot->match_len = match_len;
    for (uint8_t i = 0u; i < match_len; ++i) slot->match[i] = match[i];
    for (uint8_t i = 0u; i < reply_len; ++i) slot->reply[i] = reply[i];
    slot->reply_len = reply_len;
    return Result::ok;
}

// The entry for a frame, or nullptr. The longest matching prefix wins, so a
// specific entry overrides a match-everything one for the same type.
inline const Entry* find(const Table& table, uint16_t type, const uint8_t* payload,
                         uint16_t length)
{
    const Entry* best = nullptr;
    for (uint8_t i = 0u; i < kEntries; ++i)
    {
        const Entry& e = table.entry[i];
        if (e.reply_len == 0u || e.type != type || e.match_len > length) continue;
        if (e.match_len != 0u && payload == nullptr) continue;
        bool same = true;
        for (uint8_t j = 0u; j < e.match_len && same; ++j) same = e.match[j] == payload[j];
        if (same && (best == nullptr || e.match_len > best->match_len)) best = &e;
    }
    return best;
}

inline uint8_t used(const Table& table)
{
    uint8_t n = 0u;
    for (uint8_t i = 0u; i < kEntries; ++i)
        if (table.entry[i].reply_len != 0u) ++n;
    return n;
}
}  // namespace long_reply_table
