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
void bmcu_link_status_changed(uint32_t reasons);
uint32_t bmcu_link_tx_drop_count(void);

#endif