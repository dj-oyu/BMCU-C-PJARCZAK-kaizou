// Host harness implementation. See bambu_bus_host.h for what is real here and
// what is faked.

#include "bambu_bus_host.h"

#include <string.h>

#include "ams.h"
#include "_bus_hardware.h"
#include "app_api.h"
#include "hal/time_hw.h"
#include "bmcu_link.h"
#include "ams_merger_policy.h"
#include "Motion_control.h"

// The firmware's own CRC stamper, used to build vectors. Building frames with
// the shipped function rather than a hand-rolled copy means a vector cannot be
// wrong in a way that a real printer's frame would not also be wrong.
void package_add_crc(uint8_t* data, int send_data_length);

// ---------------------------------------------------------------------------
// The clock
// ---------------------------------------------------------------------------
// Initialisers match src/hal/time_hw.c exactly, so one tick is one microsecond
// before time_hw_init would have run. The firmware's own pre-init values.
extern "C" {
uint32_t time_hw_tpus = 1u;
uint32_t time_hw_tpms = 1000u;
uint32_t host_clock_ticks = 0u;

void time_hw_init(void) {}
uint32_t time_hw_ticks_per_us(void) { return time_hw_tpus; }
uint32_t time_hw_ticks_per_ms(void) { return time_hw_tpms; }
uint64_t time_ticks64(void) { return host_clock_ticks; }
uint64_t time_us64(void) { return host_clock_ticks / time_hw_tpus; }
uint64_t time_ms64(void) { return host_clock_ticks / time_hw_tpms; }
void delay_us(uint32_t us) { host_clock_advance_us(us); }
void delay(uint32_t ms) { host_clock_advance_ms(ms); }
}

// ---------------------------------------------------------------------------
// Fixture globals the firmware expects to exist
// ---------------------------------------------------------------------------
_ams ams[ams_max_number];
uint8_t bus_now_ams_num = 0u;
uint16_t bus_host_device_type = host_device_type_ams;
_bus_port_deal bus_port_to_host;

void ams_init(void)
{
    for (uint8_t i = 0u; i < ams_max_number; ++i) ams[i].init();
}

void bus_init(void) {}

// The UART transport. Nothing here is under test: the harness drives the
// parser directly, so these only have to be quiescent and honest.
bool bus_uart1_rx_transport_quiet(void) { return true; }
bool bus_uart1_reset_transport_quiescent(void) { return true; }
void bus_uart1_rx_poll(void) {}
void bus_uart1_rx_release_frame(void) {}
void bus_uart1_tx_poll(void) {}

static float g_meters[4] = {1.0f, 1.0f, 1.0f, 1.0f};

float Motion_control_get_filament_meters(uint8_t channel)
{
    return (channel < 4u) ? g_meters[channel] : 0.0f;
}

// ---------------------------------------------------------------------------
// Recording fakes
// ---------------------------------------------------------------------------
namespace host_bus
{
Transaction transactions[kMaxRecords];
int transaction_count = 0;

LongTransaction long_transactions[kMaxRecords];
int long_transaction_count = 0;

MergerEvent merger_events[kMaxRecords];
int merger_event_count = 0;

OwnershipSample ownership_trace[kMaxRecords];
int ownership_trace_count = 0;

uint8_t service_frames[kMaxRecords];
int service_frame_count = 0;

uint32_t status_change_mask = 0u;
int status_change_count = 0;
int registration_query_count = 0;
int registration_confirm_count = 0;
int registration_reset_count = 0;
int save_filament_count = 0;

uint8_t reply[1280];
int reply_len = 0;
int reply_count = 0;

bambubus_package_type last_type = bambubus_package_type::none;

int merger_event_totals[kMergerEventKinds] = {};
int wildcard_release_calls = 0;
int named_release_calls = 0;

static void record_merger(uint8_t kind, uint8_t channel, uint32_t count)
{
    // The totals never saturate. The detail ring does, and a long randomised
    // walk overruns it in a few hundred frames -- so anything asserted over a
    // walk has to be counted here rather than by scanning the ring.
    if (kind < kMergerEventKinds) ++merger_event_totals[kind];

    if (merger_event_count >= kMaxRecords) return;
    MergerEvent& e = merger_events[merger_event_count++];
    e.kind = kind;
    e.channel = channel;
    e.count = count;
}

const Transaction& last_transaction(void)
{
    static const Transaction empty = {};
    return transaction_count ? transactions[transaction_count - 1] : empty;
}

int merger_events_of(uint8_t kind)
{
    return (kind < kMergerEventKinds) ? merger_event_totals[kind] : 0;
}
} // namespace host_bus

void bmcu_link_status_changed(uint32_t reasons)
{
    host_bus::status_change_mask |= reasons;
    ++host_bus::status_change_count;
}

void bmcu_link_printer_transaction(uint8_t rx_class, uint8_t command, uint8_t outcome,
                                   uint8_t reason, uint16_t request_length,
                                   uint16_t response_length, uint8_t addressed_ams)
{
    if (host_bus::transaction_count >= host_bus::kMaxRecords) return;
    host_bus::Transaction& t = host_bus::transactions[host_bus::transaction_count++];
    t.rx_class = rx_class;
    t.command = command;
    t.outcome = outcome;
    t.reason = reason;
    t.request_length = request_length;
    t.response_length = response_length;
    t.addressed_ams = addressed_ams;
}

void bmcu_link_printer_long_transaction(uint16_t type, uint8_t outcome, uint8_t reason,
                                        uint16_t payload_length, uint16_t response_length,
                                        uint8_t payload_hash)
{
    if (host_bus::long_transaction_count >= host_bus::kMaxRecords) return;
    host_bus::LongTransaction& t =
        host_bus::long_transactions[host_bus::long_transaction_count++];
    t.type = type;
    t.outcome = outcome;
    t.reason = reason;
    t.payload_length = payload_length;
    t.response_length = response_length;
    t.payload_hash = payload_hash;
}

void bmcu_link_ams_service_poll(void) {}

void bmcu_link_ams_service_frame(uint8_t service_kind)
{
    if (host_bus::service_frame_count >= host_bus::kMaxRecords) return;
    host_bus::service_frames[host_bus::service_frame_count++] = service_kind;
}

void bmcu_link_ams_registration_query(void) { ++host_bus::registration_query_count; }
void bmcu_link_ams_registration_confirm(void) { ++host_bus::registration_confirm_count; }
void bmcu_link_ams_registration_reset(void) { ++host_bus::registration_reset_count; }

void bmcu_link_motion_fault(uint8_t, uint8_t, uint8_t) {}

void ams_datas_set_need_to_save_filament(uint8_t filament_idx)
{
    (void)filament_idx;
    ++host_bus::save_filament_count;
}

// ---------------------------------------------------------------------------
// The merger funnel, restated over the real policy
// ---------------------------------------------------------------------------
// This mirrors main.cpp:144-215. It is not a model of the state machine: every
// decision below is delegated to src/ams_merger_policy.h, the same header the
// firmware links. What is restated is only the wrapper -- the dirty flag
// (dropped, there is no flash here), the refusal counter, and the edge latch
// that keeps a paused printer's idle frames from emitting one event per frame.
static ams_merger::State g_merger = {ams_merger::kNoChannel, 0u};
static uint16_t g_tail_refused_count = 0u;
static uint8_t g_tail_refused_reported = 0u;

void ams_state_set_loaded(uint8_t filament_ch)
{
    if (ams_merger::acquire(g_merger, filament_ch))
        host_bus::record_merger(host_bus::kMergerAcquired, filament_ch, 0u);
}

void ams_state_set_unloaded(uint8_t filament_ch)
{
    // Counted before the policy sees it, and counted whatever the answer is.
    // Without this a test asserting "no wildcard release ever met a TAIL" could
    // be passing because no wildcard release ever happened at all.
    if (filament_ch >= ams_merger::kChannels) ++host_bus::wildcard_release_calls;
    else ++host_bus::named_release_calls;

    switch (ams_merger::release(g_merger, filament_ch))
    {
    case ams_merger::release_done:
        g_tail_refused_reported = 0u;
        host_bus::record_merger(host_bus::kMergerReleased, filament_ch, 0u);
        break;

    case ams_merger::release_refused_tail:
        if (g_tail_refused_count != 0xFFFFu) ++g_tail_refused_count;
        if (!g_tail_refused_reported)
        {
            g_tail_refused_reported = 1u;
            host_bus::record_merger(host_bus::kMergerRefusedTail, 0xFFu,
                                    g_tail_refused_count);
        }
        break;

    default:
        break;
    }
}

void ams_state_preempt(uint8_t claiming_ch)
{
    const uint8_t result = ams_merger::preempt(g_merger);
    if (result == ams_merger::release_none) return;

    g_tail_refused_reported = 0u;

    if (result == ams_merger::release_preempted_tail)
        host_bus::record_merger(host_bus::kMergerPreempted, claiming_ch, 0u);
    else
        host_bus::record_merger(host_bus::kMergerReleased, claiming_ch, 0u);
}

void ams_state_set_tail(void)
{
    if (!ams_merger::to_tail(g_merger)) return;

    g_tail_refused_reported = 0u;
    host_bus::record_merger(host_bus::kMergerToTail, g_merger.owner, 0u);
}

uint8_t ams_state_get_loaded(void) { return g_merger.owner; }
bool ams_state_is_tail(void) { return ams_merger::is_tail(g_merger); }

// ---------------------------------------------------------------------------
// Transmit capture
// ---------------------------------------------------------------------------
static void host_port_send_datas(uint8_t* data, uint16_t len)
{
    if (len > sizeof(host_bus::reply)) len = (uint16_t)sizeof(host_bus::reply);
    memcpy(host_bus::reply, data, len);
    host_bus::reply_len = (int)len;
    ++host_bus::reply_count;
}

// ---------------------------------------------------------------------------
// Fixture API
// ---------------------------------------------------------------------------
namespace host_bus
{

void reset(void)
{
    host_clock_set(0u);
    time_hw_tpus = 1u;
    time_hw_tpms = 1000u;

    ams_init();
    for (uint8_t i = 0u; i < 4u; ++i) g_meters[i] = 1.0f;

    bus_port_to_host.init(host_port_send_datas);

    // bambubus_init() is deliberately NOT called, and this is the one blocker
    // that is inside bambu_bus_ams.cpp rather than behind an include.
    // bambubus_build_static_serial (:1034) reads the chip UID through a
    // literal absolute address -- `(volatile const uint8_t *)0x1FFFF7E8` -- so
    // the file does touch hardware, just not by any name a register grep would
    // match. Calling init here segfaults on the host.
    //
    // Everything this harness drives is reachable without it: init only builds
    // the serial-number reply and clears the heartbeat deadline, which starts
    // zeroed anyway. The cost is that get_package_long_packge_serial_number is
    // out of reach for these tests, and would need the UID read lifted behind a
    // seam before it could be covered.

    ams_merger::reset(g_merger);
    g_tail_refused_count = 0u;
    g_tail_refused_reported = 0u;

    transaction_count = 0;
    long_transaction_count = 0;
    merger_event_count = 0;
    for (int i = 0; i < kMergerEventKinds; ++i) merger_event_totals[i] = 0;
    wildcard_release_calls = 0;
    named_release_calls = 0;
    ownership_trace_count = 0;
    service_frame_count = 0;
    status_change_mask = 0u;
    status_change_count = 0;
    registration_query_count = 0;
    registration_confirm_count = 0;
    registration_reset_count = 0;
    save_filament_count = 0;
    reply_len = 0;
    reply_count = 0;
    last_type = bambubus_package_type::none;
    heartbeat_alive = true;

    set_ams_online(true);
    for (uint8_t i = 0u; i < 4u; ++i) set_channel_online(i, true);
}

void set_ams_online(bool online)
{
    ams[0].online = online;
}

void set_channel_online(uint8_t ch, bool online)
{
    if (ch < 4u) ams[0].filament[ch].online = online;
}

void set_filament_meters(uint8_t ch, float meters)
{
    if (ch < 4u) g_meters[ch] = meters;
}

void publish(const uint8_t* frame, int length)
{
    for (int i = 0; i < length; ++i) bus_port_to_host.irq(frame[i]);
}

// 3D C5 0C C8 03 <ams> <statu> <chan> <motion> 02 crc16
// Layout from bambubus_printer_motion_package_struct, bambu_bus_ams.cpp:464.
void publish_motion_short(uint8_t ams_num, uint8_t statu_flag, uint8_t channel,
                          uint8_t motion_flag)
{
    uint8_t f[12];
    memset(f, 0, sizeof(f));
    f[0] = 0x3Du;
    f[1] = 0xC5u;
    f[2] = 0x0Cu;
    f[4] = 0x03u;
    f[5] = ams_num;
    f[6] = statu_flag;
    f[7] = channel;
    f[8] = motion_flag;
    f[9] = 0x02u;
    package_add_crc(f, (int)sizeof(f));
    publish(f, (int)sizeof(f));
}

// 3D C5 0D F1 04 <ams> <statu> <motion> 03 <chan> 00 crc16
// Layout from bambubus_printer_stu_motion_package_struct, bambu_bus_ams.cpp:696.
// Note the channel and motion bytes swap places relative to the short frame.
void publish_motion_long(uint8_t ams_num, uint8_t statu_flag, uint8_t motion_flag,
                         uint8_t channel)
{
    uint8_t f[13];
    memset(f, 0, sizeof(f));
    f[0] = 0x3Du;
    f[1] = 0xC5u;
    f[2] = 0x0Du;
    f[4] = 0x04u;
    f[5] = ams_num;
    f[6] = statu_flag;
    f[7] = motion_flag;
    f[8] = 0x03u;
    f[9] = channel;
    f[10] = 0x00u;
    package_add_crc(f, (int)sizeof(f));
    publish(f, (int)sizeof(f));
}

bool heartbeat_alive = true;

int run(void)
{
    reply_len = 0;

    // Stand in for the printer's 0x20 heartbeat frames. Without this the
    // deadline stays at zero, every run past tick zero reads as heartbeat loss,
    // and bambubus_run returns `error` and re-arms online detection each time
    // -- true to the firmware, but it would mean every conversation vector was
    // implicitly also a bus-loss vector. Tests that want the loss path clear
    // heartbeat_alive.
    if (heartbeat_alive) bambubus_heartbeat_seen_fast();

    last_type = bambubus_run();

    // bambubus_run defers a response by 50 us so the printer's line has turned
    // around. Step past it and drain, so `reply` is what the wire carried
    // rather than what the build buffer happened to hold.
    host_clock_advance_us(200u);
    bus_port_to_host.send_package();

    if (ownership_trace_count < kMaxRecords)
    {
        OwnershipSample& s = ownership_trace[ownership_trace_count++];
        s.stage = ams_merger::stage(g_merger);
        s.owner = g_merger.owner;
    }

    return reply_len;
}

int poll_motion_short(uint8_t ams_num, uint8_t statu_flag, uint8_t channel,
                      uint8_t motion_flag)
{
    publish_motion_short(ams_num, statu_flag, channel, motion_flag);
    return run();
}

int poll_motion_long(uint8_t ams_num, uint8_t statu_flag, uint8_t motion_flag,
                     uint8_t channel)
{
    publish_motion_long(ams_num, statu_flag, motion_flag, channel);
    return run();
}

uint8_t merger_stage(void) { return ams_merger::stage(g_merger); }
uint8_t merger_owner(void) { return g_merger.owner; }

} // namespace host_bus
