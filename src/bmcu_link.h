#ifndef BMCU_LINK_H
#define BMCU_LINK_H

#include <stdint.h>

// Per-channel motion fault/switch state, as one raw byte on the wire.
//
//   bit0-1  ks       four-valued switch reading, dm_key_to_state verbatim
//   bit2    low      g_on_use_low_latch
//   bit3    jam      g_on_use_jam_latch
//   bit4    dm_fail  dm_fail_latch
//   bit5    loaded   this channel holds the g_loaded_ch latch
//   bit6-7  reserved, transmitted as zero
//
// Deliberately sparse. Roughly 21 of the 32 combinations are reachable, which
// still needs five bits, so a dense encoding would save nothing while adding
// arithmetic on both sides and freezing today's invariants into the wire. The
// unreachable combinations are slack, not an oversight.
//
// Two invariants hold today and are recorded here as documentation only;
// nothing above depends on them, and a decoder must not assume them:
//   - jam == 1 implies low == 1. The only site that sets jam sets low in the
//     same statement, and every site that clears jam clears low with it.
//   - dm_fail is never set while ks == 0, because ks == 0 is what clears it.
//
// This union is MCU-internal: only `raw` ever crosses the wire. Bitfield
// allocation order is compiler-defined, so the Python and TypeScript decoders
// must do their own explicit shifts against the bit positions documented above
// and in docs/bmcu_wire_layout.json. They must never mirror this layout.
union BmcuChannelFlags
{
    uint8_t raw;
    struct
    {
        uint8_t ks : 2;
        uint8_t low : 1;
        uint8_t jam : 1;
        uint8_t dm_fail : 1;
        // The channel holding the printer-side loaded latch, g_loaded_ch.
        // Flash-backed, and until now visible nowhere: it is cleared the
        // instant a loaded channel's key reads 0 (Motion_control.cpp:3072) and
        // re-latched only by a printer-commanded load, so one brief switch
        // blip desynchronises it from the printer permanently -- after which
        // an unload addressed to that channel is a traceless no-op. At most
        // one channel sets this; none set means no channel is latched.
        uint8_t loaded : 1;
        uint8_t reserved : 2;
    } bits;
};
static_assert(sizeof(BmcuChannelFlags) == 1u, "BmcuChannelFlags must stay one byte");

enum bmcu_status_change : uint32_t
{
    BMCU_STATUS_CHANGE_SLOT     = 1u << 0,
    BMCU_STATUS_CHANGE_INSERTED = 1u << 1,
    BMCU_STATUS_CHANGE_ONLINE   = 1u << 2,
    BMCU_STATUS_CHANGE_MOTION   = 1u << 3,
    BMCU_STATUS_CHANGE_PRESSURE = 1u << 4,
    BMCU_STATUS_CHANGE_LED      = 1u << 5,
    BMCU_STATUS_CHANGE_ERROR    = 1u << 6,
    BMCU_STATUS_CHANGE_ALL      = 0x7Fu,
};

void bmcu_link_init(void);
void bmcu_link_service(void);
void bmcu_link_rx_isr_byte(uint8_t data);
void bmcu_link_apply_led_override(void);
void bmcu_link_set_control_error(int error);
void bmcu_link_set_calibration_busy(bool busy);
// Main-loop producers only; ISR code must only enqueue raw RX bytes.
void bmcu_link_status_changed(uint32_t reasons);
void bmcu_link_printer_transaction(uint8_t rx_class, uint8_t command, uint8_t outcome,
                                   uint8_t reason, uint16_t request_length,
                                   uint16_t response_length, uint8_t addressed_ams);
void bmcu_link_printer_long_transaction(uint16_t type, uint8_t outcome, uint8_t reason,
                                        uint16_t payload_length, uint16_t response_length,
                                        uint8_t payload_hash);
// Observe-only AMS service/registration instrumentation (issue #3 phase 1).
// These only feed diagnostic telemetry; nothing they record is ever read back
// into a printer-bus decision. Main-loop context only.
void bmcu_link_ams_service_poll(void);
void bmcu_link_ams_service_frame(uint8_t service_kind);
void bmcu_link_ams_registration_query(void);
void bmcu_link_ams_registration_confirm(void);
void bmcu_link_ams_registration_reset(void);
void bmcu_link_motion_fault(uint8_t channel, uint8_t previous_fault, uint8_t fault);
uint32_t bmcu_link_tx_drop_count(void);
bool bmcu_link_reset_pending(void);

#endif