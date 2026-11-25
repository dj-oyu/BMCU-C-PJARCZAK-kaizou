#pragma once

#include <stdint.h>

// Records the motion commands the printer puts on the bus, for every AMS this
// BMCU can hear -- not only its own.
//
// The printer and all AMS units share one bus, so every BMCU receives the
// motion and stu_motion frames addressed to the other units too, and until now
// dropped them on the address check. On a two-BMCU setup where only one board
// has a monitor wired to it (the newer USB-C boards have no UART pads), that
// left the unwired unit completely dark: on 2026-11-25 an unload on AMS B
// failed with "Failed to pull out the filament from the extruder" and there was
// no way to tell whether the printer ever sent the pull_back, or which
// before_pull_back preceded it.
//
// One record is emitted when the (channel, statu_flag, motion_flag) triple
// changes, tracked per AMS and per frame kind (motion / stu_motion, which the
// printer interleaves), not per frame: the printer repeats the same command
// several times a second, and the transitions are what the diagnosis needs.
// The record is a DIAG_COUNTER_PRINTER_MOTION_CMD whose value packs
//   bit  31      frame kind (0 = motion, 1 = stu_motion)
//   bits 30..24  ams_num as addressed on the wire
//   bits 23..16  filament channel (0xFF = AMS-wide)
//   bits 15..8   statu_flag
//   bits  7..0   motion_flag
// This is observe-only: it runs before any filter and changes no reply.
// Build with -DBMCU_LOG_PRINTER_MOTION=0 to turn it off.
#ifndef BMCU_LOG_PRINTER_MOTION
#define BMCU_LOG_PRINTER_MOTION 1
#endif

namespace printer_motion_watch
{
constexpr uint8_t kAmsSlots = 4u;
constexpr uint8_t kKindMotion = 0u;
constexpr uint8_t kKindStuMotion = 1u;
constexpr uint8_t kSlots = kAmsSlots * 2u;

struct State
{
    uint32_t last[kSlots];
    uint8_t seen_mask;
};

inline uint32_t pack(uint8_t kind, uint8_t ams_num, uint8_t channel,
                     uint8_t statu_flag, uint8_t motion_flag)
{
    return (static_cast<uint32_t>(kind & 1u) << 31) |
           (static_cast<uint32_t>(ams_num & 0x7Fu) << 24) |
           (static_cast<uint32_t>(channel) << 16) |
           (static_cast<uint32_t>(statu_flag) << 8) |
           static_cast<uint32_t>(motion_flag);
}

inline void reset(State& s)
{
    for (uint8_t i = 0; i < kSlots; ++i) s.last[i] = 0u;
    s.seen_mask = 0u;
}

// True when this command differs from the last one seen for the same AMS and
// frame kind (or is the first one), i.e. when a record should be emitted. An
// ams_num outside 0..3 is never recorded.
inline bool note(State& s, uint8_t kind, uint8_t ams_num, uint32_t packed)
{
    if (ams_num >= kAmsSlots) return false;
    const uint8_t slot = static_cast<uint8_t>(ams_num * 2u + (kind & 1u));
    const uint8_t bit = static_cast<uint8_t>(1u << slot);
    if ((s.seen_mask & bit) != 0u && s.last[slot] == packed) return false;
    s.seen_mask = static_cast<uint8_t>(s.seen_mask | bit);
    s.last[slot] = packed;
    return true;
}
}  // namespace printer_motion_watch
