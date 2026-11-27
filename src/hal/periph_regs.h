#pragma once
// Peripheral set-up by direct register writes, in place of the WCH SDK's
// *_Init(struct) functions. Those take a struct, test every field at run time
// and pull in helpers such as RCC_GetClocksFreq; every configuration in this
// firmware is fixed, so the register values are constants.
//
// Each replacement leaves the registers exactly as the SDK call did. The
// values were checked by booting the old and new images in sim/ and comparing
// the final value of every peripheral register.

#include <stdint.h>
#include "ch32v20x.h"

#ifdef __cplusplus
extern "C" {
#endif

// The clock tree SystemInit sets up (SYSCLK_FREQ_144MHz_HSI): HCLK = SYSCLK,
// APB2 = HCLK, APB1 = HCLK / 2.
#ifndef SYSCLK_FREQ_144MHz_HSI
#error "periph_regs assumes the 144 MHz HSI clock configuration"
#endif
#define PERIPH_PCLK2_HZ (SYSCLK_FREQ_144MHz_HSI)
#define PERIPH_PCLK1_HZ (SYSCLK_FREQ_144MHz_HSI / 2u)

// USART BRR exactly as the SDK's USART_Init computes it.
#define PERIPH_USART_BRR(pclk, baud)                                            \
    ((((25u * (pclk)) / (4u * (baud))) / 100u) << 4 |                           \
     (((((25u * (pclk)) / (4u * (baud))) % 100u) * 16u + 50u) / 100u & 0x0Fu))

// AFIO->PCFR1 is written whole, never read-modify-written. The SDK's
// GPIO_PinRemapConfig reads it back and, on CH32V203 parts whose word at
// 0x40022030 has bits 27:24 clear, has to shift what it reads to undo a
// readback quirk; a plain store needs neither. Bits 27:24 are written as 1s,
// as every SDK remap call writes them. PD0/PD1 are GPIO (PD0 is an AS5600 SDA).
#define PERIPH_PCFR1_BASE (0x0F000000u | AFIO_PCFR1_PD01_REMAP)

// GPIO pin configuration nibble (CNF[1:0] MODE[1:0]); outputs at 50 MHz.
enum
{
    GPIO_CFG_AIN    = 0x0u,
    GPIO_CFG_OUT_PP = 0x3u,
    GPIO_CFG_OUT_OD = 0x7u,
    GPIO_CFG_IN_PU  = 0x8u | 0x10u,   // bit 4: also drive OUTDR high (pull-up)
    GPIO_CFG_AF_PP  = 0xBu,
};

// GPIO_Init equivalent: give every pin in `pins` the configuration `cfg`.
void gpio_config(GPIO_TypeDef* port, uint32_t pins, uint32_t cfg);

// NVIC_Init equivalent under NVIC_PriorityGroup_1 with nesting enabled.
static inline void nvic_enable(IRQn_Type irq, uint32_t preempt, uint32_t sub)
{
    NVIC->IPRIOR[(uint32_t)irq] = (uint8_t)((preempt << 7) | (sub << 5));
    NVIC->IENR[(uint32_t)irq >> 5] = 1u << ((uint32_t)irq & 0x1Fu);
}

// DMA_DeInit + DMA_Init equivalent: the channel is disabled, its flags
// cleared, and it is left configured but not enabled.
static inline void dma_channel_setup(DMA_Channel_TypeDef* ch, uint32_t if_mask,
                                     uint32_t cfgr, uint32_t paddr, uint32_t maddr,
                                     uint32_t count)
{
    ch->CFGR &= (uint16_t)~DMA_CFGR1_EN;
    ch->CFGR = 0u;
    DMA1->INTFCR |= if_mask;
    ch->CFGR = cfgr;
    ch->CNTR = count;
    ch->PADDR = paddr;
    ch->MADDR = maddr;
}

#ifdef __cplusplus
}
#endif
