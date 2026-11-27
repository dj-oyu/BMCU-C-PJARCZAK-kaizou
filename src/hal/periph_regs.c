#include "hal/periph_regs.h"

void gpio_config(GPIO_TypeDef* port, uint32_t pins, uint32_t cfg)
{
    // Kept from the SDK's GPIO_Init: on CH32V203 parts whose word at
    // 0x40022030 has bits 27:24 clear, the SDK moves GPIOC pins 13..15 down to
    // 0..2 before configuring them. Which parts those are is not documented;
    // the behaviour is copied as it is.
    if (port == GPIOC && ((*(volatile uint32_t*)0x40022030u) & 0x0F000000u) == 0u)
        pins >>= 13;

    if (cfg & 0x10u) port->BSHR = pins;

    volatile uint32_t* reg = &port->CFGLR;   // CFGLR, then CFGHR
    for (uint32_t half = 0u; half < 2u; half++, reg++)
    {
        const uint32_t m = (pins >> (8u * half)) & 0xFFu;
        if (m == 0u) continue;
        uint32_t v = *reg;
        for (uint32_t i = 0u; i < 8u; i++)
        {
            if (m & (1u << i))
                v = (v & ~(0xFu << (4u * i))) | ((cfg & 0xFu) << (4u * i));
        }
        *reg = v;
    }
}
