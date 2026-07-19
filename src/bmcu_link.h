#ifndef BMCU_LINK_H
#define BMCU_LINK_H

#include <stdint.h>

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
// Main-loop producers only; ISR code must only enqueue raw RX bytes.
void bmcu_link_status_changed(uint32_t reasons);
void bmcu_link_printer_transaction(uint8_t rx_class, uint8_t command, uint8_t outcome,
                                   uint8_t reason, uint16_t request_length,
                                   uint16_t response_length);
void bmcu_link_motion_fault(uint8_t channel, uint8_t previous_fault, uint8_t fault);
uint32_t bmcu_link_tx_drop_count(void);

#endif