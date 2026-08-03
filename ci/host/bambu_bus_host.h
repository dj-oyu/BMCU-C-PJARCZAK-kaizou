#pragma once

// Host harness for src/bambu_bus_ams.cpp: a frame-in / reply-out conversation
// fixture.
//
// What this exists to reach. bambu_bus_ams.cpp holds set_motion, the
// accept-flag table, the handler dispatch and the AMS-number routing, and none
// of it had a host test -- the policy headers did. Two defects this week were
// in the routing rather than in the state machine, and a test of the policy
// alone cannot see the wiring the policy is bolted to. So the rule here is
// that everything on the path under test is the shipped code: the real RX
// parser in _bus_hardware.h, the real CRCs from crc_bus.c, the real handlers,
// and the real merger policy from ams_merger_policy.h. What is faked is only
// what sits at the far side of a seam -- the link telemetry sinks, the motion
// controller, and the clock.
//
// A limit on what this harness can ever prove, worth knowing before writing a
// test against it: THE REPLY BYTES CANNOT CARRY A ROUTING ASSERTION. set_motion
// returns true whether it accepted a frame or dropped it on allow_any /
// allow_stop -- the refusals at :290, :315, :354 and :383 all `return true` --
// so get_package_motion builds and sends the same reply either way. A frame
// that was refused and a frame that was obeyed are byte-identical on the wire
// apart from state that had already changed for other reasons. Every assertion
// about whether the accept table took a frame therefore has to be made against
// ams[] or against the merger, never against `reply`. What the reply bytes are
// good for is the shape of the answer -- length, address, CRC, the echoed
// channel -- which is what test_motion_reply_bytes uses them for.
//
// A second limit, discovered the hard way: bambubus_init() cannot be called.
// bambubus_build_static_serial at bambu_bus_ams.cpp:1034 reads the chip UID
// through the literal address 0x1FFFF7E8, so the file touches hardware without
// naming it and a host call segfaults. reset() below says so at the point where
// the call would otherwise go. The cost is that the serial-number handler is
// uncoverable until that read moves behind a seam.
//
// The one reimplementation is the ams_state_* funnel, and it is a
// reimplementation rather than a fake: main.cpp cannot be compiled on a host
// (its Flash_saves and Motion_control dependencies are the register-heavy
// part), so the funnel body is restated here over the *real*
// src/ams_merger_policy.h, including the tail-refusal edge latch. The policy
// underneath is the shipped one, so a change to the transition table is felt
// here; a change to the funnel wrapper in main.cpp is not, and that is the
// known gap in this harness.

#include <stdint.h>

#include "bambu_bus_ams.h"

namespace host_bus
{

constexpr int kMaxRecords = 128;

// One bmcu_link_printer_transaction call, recorded verbatim. This is the
// harness's view of how a frame was classified -- the outcome/reason pair is
// what the firmware reports upstream and what the peer-addressing assertions
// below are written against.
struct Transaction
{
    uint8_t rx_class;
    uint8_t command;
    uint8_t outcome;
    uint8_t reason;
    uint16_t request_length;
    uint16_t response_length;
    uint8_t addressed_ams;
};

struct LongTransaction
{
    uint16_t type;
    uint8_t outcome;
    uint8_t reason;
    uint16_t payload_length;
    uint16_t response_length;
    uint8_t payload_hash;
};

// Merger edges, as the funnel emits them. Kind is one of the kMerger* values.
struct MergerEvent
{
    uint8_t kind;
    uint8_t channel; // claiming channel for a preempt, 0xFF otherwise
    uint32_t count;  // refusal count for a refusal, 0 otherwise
};

enum MergerEventKind : uint8_t
{
    kMergerAcquired = 0u,
    kMergerReleased = 1u,
    kMergerRefusedTail = 2u,
    kMergerPreempted = 3u,
    kMergerToTail = 4u,
    kMergerEventKinds = 5u,
};

// A sample of merger ownership, taken after every run(). The sequence of these
// is the ownership trace the routing tests assert on.
struct OwnershipSample
{
    uint8_t stage; // ams_merger::stage_*
    uint8_t owner; // 0xFF when free
};

extern Transaction transactions[kMaxRecords];
extern int transaction_count;

extern LongTransaction long_transactions[kMaxRecords];
extern int long_transaction_count;

// The detail ring, capped at kMaxRecords. A long randomised walk overruns it,
// so anything counted over a walk must go through merger_events_of, which reads
// the non-saturating totals instead.
extern MergerEvent merger_events[kMaxRecords];
extern int merger_event_count;
extern int merger_event_totals[kMergerEventKinds];

// How many times the routing offered the funnel a release, split by whether it
// named a channel. These count calls, not outcomes, so a test can tell "the
// wildcard never met a TAIL" apart from "the wildcard never ran".
extern int wildcard_release_calls;
extern int named_release_calls;

extern OwnershipSample ownership_trace[kMaxRecords];
extern int ownership_trace_count;

// Observe-only instrumentation sinks, recorded so a test can prove the
// address-match counting happens before the filters rather than after.
extern uint8_t service_frames[kMaxRecords];
extern int service_frame_count;

extern uint32_t status_change_mask;
extern int status_change_count;
extern int registration_query_count;
extern int registration_confirm_count;
extern int registration_reset_count;
extern int save_filament_count;

// The bytes handed to port_send_datas by the last run(), i.e. what the printer
// would actually have seen on the wire.
extern uint8_t reply[1280];
extern int reply_len;
extern int reply_count;

extern bambubus_package_type last_type;

// Whether run() should renew the printer heartbeat before each poll. True by
// default; clear it to drive the heartbeat-loss path.
extern bool heartbeat_alive;

// Full fixture reset: clock, AMS fixture globals, bus port, merger, records.
void reset(void);

void set_ams_online(bool online);
void set_channel_online(uint8_t ch, bool online);
void set_filament_meters(uint8_t ch, float meters);

// Feed a frame through the real RX parser one byte at a time, exactly as the
// USART interrupt would. Frames are published, not injected: a vector that
// would not survive the framer does not reach the handlers here either.
void publish(const uint8_t* frame, int length);

// Build, CRC (with the firmware's own package_add_crc) and publish a frame.
void publish_motion_short(uint8_t ams_num, uint8_t statu_flag, uint8_t channel,
                          uint8_t motion_flag);
void publish_motion_long(uint8_t ams_num, uint8_t statu_flag, uint8_t motion_flag,
                         uint8_t channel);

// One bambubus_run(), then let the 50 us response deferral elapse and drain the
// transmitter, so `reply` holds what went out. Returns reply_len.
int run(void);

// Convenience: publish + run.
int poll_motion_short(uint8_t ams_num, uint8_t statu_flag, uint8_t channel,
                      uint8_t motion_flag);
int poll_motion_long(uint8_t ams_num, uint8_t statu_flag, uint8_t motion_flag,
                     uint8_t channel);

uint8_t merger_stage(void);
uint8_t merger_owner(void);

const Transaction& last_transaction(void);
int merger_events_of(uint8_t kind);

} // namespace host_bus
