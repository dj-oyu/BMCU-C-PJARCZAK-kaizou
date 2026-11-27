#include "ADC_DMA.h"
#include "hal/time_hw.h"
#include "ch32v20x_adc.h"
#include "ch32v20x_dma.h"
#include "ch32v20x_rcc.h"
#include "sensor_fixed.h"
#include "hal/periph_regs.h"
#include <stdint.h>

static constexpr uint32_t kCh      = 8;
static constexpr uint32_t kBlock   = 32;
static constexpr uint32_t kNBlocks = 4;
static constexpr uint32_t kBufLen  = (kCh * kBlock * 2);
static constexpr uint32_t kHalfLen = (kBufLen / 2);

static bool g_adc_dma_inited = false;
bool ADC_DMA_is_inited() { return g_adc_dma_inited; }

static volatile uint32_t g_dma_buf[kBufLen] __attribute__((aligned(4)));

static uint32_t g_ring_sum[kNBlocks][kCh];
static uint32_t g_acc_sum[kCh];
static uint8_t  g_ring_idx = 0;
static uint8_t  g_blocks_filled = 0;

static int32_t          g_v[2][kCh] __attribute__((aligned(4)));
static volatile uint8_t g_v_rd = 0;
static volatile uint8_t g_acc_dirty = 0;

static_assert(kBlock == 32u && kNBlocks == 4u, "sensor_fixed::kAdcMul assumes 4 blocks of 32 scans");
static uint32_t g_mul = sensor_fixed::kAdcMul[1];

static inline __attribute__((always_inline)) void adc_dma_barrier()
{
    __asm__ volatile("fence iorw, iorw" ::: "memory");
}

static inline __attribute__((always_inline)) void adc_dma_compiler_barrier()
{
    __asm__ volatile("" ::: "memory");
}

void ADC_DMA_gpio_analog()
{
    RCC->APB2PCENR |= RCC_APB2Periph_GPIOA;
    gpio_config(GPIOA, 0x00FFu, GPIO_CFG_AIN);   // PA0..PA7
}

static inline void filter_reset()
{
    g_ring_idx = 0;
    g_blocks_filled = 0;
    g_mul = sensor_fixed::kAdcMul[1];

    for (uint32_t ch = 0; ch < kCh; ch++) g_acc_sum[ch] = 0;

    for (uint32_t b = 0; b < kNBlocks; b++)
        for (uint32_t ch = 0; ch < kCh; ch++)
            g_ring_sum[b][ch] = 0;

    for (uint32_t ch = 0; ch < kCh; ch++)
    {
        g_v[0][ch] = 0;
        g_v[1][ch] = 0;
    }

    g_v_rd = 0;
    g_acc_dirty = 0;
}

static inline __attribute__((always_inline)) uint32_t adc_pair_sum(uint32_t w)
{
    const uint32_t a1 = (uint16_t)(w & 0xFFFFu);
    const uint32_t a2 = (uint16_t)(w >> 16);
    return a1 + a2;
}

static inline void process_half_update_filter(const uint32_t* p_half)
{
    uint32_t s0 = 0, s1 = 0, s2 = 0, s3 = 0, s4 = 0, s5 = 0, s6 = 0, s7 = 0;

    const uint32_t* __restrict row = p_half;
    for (uint32_t s = 0; s < kBlock; s++)
    {
        s0 += adc_pair_sum(row[0]);
        s1 += adc_pair_sum(row[1]);
        s2 += adc_pair_sum(row[2]);
        s3 += adc_pair_sum(row[3]);
        s4 += adc_pair_sum(row[4]);
        s5 += adc_pair_sum(row[5]);
        s6 += adc_pair_sum(row[6]);
        s7 += adc_pair_sum(row[7]);
        row += kCh;
    }

    const uint8_t idx = g_ring_idx;
    {
        uint32_t o;
        o = g_ring_sum[idx][0]; g_ring_sum[idx][0] = s0; g_acc_sum[0] = g_acc_sum[0] - o + s0;
        o = g_ring_sum[idx][1]; g_ring_sum[idx][1] = s1; g_acc_sum[1] = g_acc_sum[1] - o + s1;
        o = g_ring_sum[idx][2]; g_ring_sum[idx][2] = s2; g_acc_sum[2] = g_acc_sum[2] - o + s2;
        o = g_ring_sum[idx][3]; g_ring_sum[idx][3] = s3; g_acc_sum[3] = g_acc_sum[3] - o + s3;
        o = g_ring_sum[idx][4]; g_ring_sum[idx][4] = s4; g_acc_sum[4] = g_acc_sum[4] - o + s4;
        o = g_ring_sum[idx][5]; g_ring_sum[idx][5] = s5; g_acc_sum[5] = g_acc_sum[5] - o + s5;
        o = g_ring_sum[idx][6]; g_ring_sum[idx][6] = s6; g_acc_sum[6] = g_acc_sum[6] - o + s6;
        o = g_ring_sum[idx][7]; g_ring_sum[idx][7] = s7; g_acc_sum[7] = g_acc_sum[7] - o + s7;
    }

    uint8_t ni = (uint8_t)(idx + 1u);
    if (ni >= kNBlocks) ni = 0u;
    g_ring_idx = ni;

    if (g_blocks_filled < kNBlocks)
    {
        g_blocks_filled++;
        g_mul = sensor_fixed::kAdcMul[g_blocks_filled];
    }

    adc_dma_compiler_barrier();
    g_acc_dirty = 1u;
}

void ADC_DMA_poll()
{
    for (int guard = 0; guard < 4; guard++)
    {
        const uint32_t flags = DMA1->INTFR;

        if (flags & DMA1_FLAG_TE1)
        {
            DMA1->INTFCR = (DMA1_FLAG_TE1 | DMA1_FLAG_HT1 | DMA1_FLAG_TC1);
            adc_dma_barrier();
            filter_reset();
            continue;
        }

        if (flags & DMA1_FLAG_HT1)
        {
            DMA1->INTFCR = DMA1_FLAG_HT1;
            adc_dma_barrier();
            const uint32_t* p = (const uint32_t*)(const void*)&g_dma_buf[0];
            process_half_update_filter(p);
            continue;
        }

        if (flags & DMA1_FLAG_TC1)
        {
            DMA1->INTFCR = DMA1_FLAG_TC1;
            adc_dma_barrier();
            const uint32_t* p = (const uint32_t*)(const void*)&g_dma_buf[kHalfLen];
            process_half_update_filter(p);
            continue;
        }

        break;
    }
}

const int32_t *ADC_DMA_get_value()
{
    ADC_DMA_poll();

    if (g_acc_dirty)
    {
        const uint32_t mul = g_mul;
        const uint8_t wr = (uint8_t)(g_v_rd ^ 1u);
        int32_t* out = g_v[wr];

        for (uint32_t ch = 0; ch < kCh; ch++)
            out[ch] = sensor_fixed::adc_sum_to_dmv(g_acc_sum[ch], mul);

        adc_dma_compiler_barrier();
        g_v_rd = wr;
        g_acc_dirty = 0u;
    }

    return g_v[g_v_rd];
}

void ADC_DMA_filter_reset()
{
    DMA1->INTFCR = (DMA1_FLAG_GL1 | DMA1_FLAG_HT1 | DMA1_FLAG_TC1 | DMA1_FLAG_TE1);
    adc_dma_barrier();
    filter_reset();
}

bool ADC_DMA_ready()
{
    return (g_blocks_filled >= kNBlocks);
}

void ADC_DMA_wait_full()
{
    const uint32_t t0 = time_ticks32();
    const uint32_t tout = ms_to_ticks32(2000u);

    while (g_blocks_filled < kNBlocks)
    {
        ADC_DMA_poll();
        if ((uint32_t)(time_ticks32() - t0) > tout) break;
        delay(1);
    }
}

static inline void adc_calibrate(ADC_TypeDef* a)
{
    a->CTLR2 |= ADC_ADON;
    a->CTLR1 &= ~(1u << 26);            // output buffer off

    a->CTLR2 |= ADC_RSTCAL;
    while (a->CTLR2 & ADC_RSTCAL) {}
    a->CTLR2 |= ADC_CAL;
    while (a->CTLR2 & ADC_CAL) {}
}

void ADC_DMA_init()
{
    if (g_adc_dma_inited)
    {
        ADC_DMA_gpio_analog();
        ADC_DMA_filter_reset();
        ADC_DMA_wait_full();
        return;
    }

    RCC->APB2PCENR |= RCC_APB2Periph_GPIOA | RCC_APB2Periph_ADC1 | RCC_APB2Periph_ADC2;
    RCC->AHBPCENR |= RCC_AHBPeriph_DMA1;
    RCC->CFGR0 = (RCC->CFGR0 & ~RCC_ADCPRE) | RCC_PCLK2_Div8;

    ADC_DMA_gpio_analog();

    for (uint32_t i = 0; i < kBufLen; i++) g_dma_buf[i] = 0xFFFFFFFFu;
    filter_reset();

    // DMA1 channel 1: ADC1 data (ADC2 in the high half-word, regular
    // simultaneous mode) into the circular buffer, words, high priority, no
    // interrupts -- ADC_DMA_poll() reads the flags.
    constexpr uint32_t RDATAR_ADDRESS = 0x4001244Cu;
    dma_channel_setup(DMA1_Channel1, DMA1_FLAG_GL1 | DMA1_FLAG_TC1 | DMA1_FLAG_HT1 | DMA1_FLAG_TE1,
                      DMA_DIR_PeripheralSRC | DMA_MemoryInc_Enable | DMA_Mode_Circular |
                      DMA_PeripheralDataSize_Word | DMA_MemoryDataSize_Word | DMA_Priority_High,
                      RDATAR_ADDRESS, (uint32_t)g_dma_buf, kBufLen);
    DMA1->INTFCR = (DMA1_FLAG_GL1 | DMA1_FLAG_HT1 | DMA1_FLAG_TC1 | DMA1_FLAG_TE1);

    // Reset both ADCs, then: scan, continuous, software trigger, right
    // aligned, 8 conversions of channels 0..7 at 71.5 cycles each. ADC1 is
    // the master of regular simultaneous mode.
    RCC->APB2PRSTR |= RCC_APB2Periph_ADC1;
    RCC->APB2PRSTR &= ~RCC_APB2Periph_ADC1;
    RCC->APB2PRSTR |= RCC_APB2Periph_ADC2;
    RCC->APB2PRSTR &= ~RCC_APB2Periph_ADC2;

    constexpr uint32_t kCtlr2  = ADC_ExternalTrigConv_None | ADC_CONT;
    constexpr uint32_t kSmp    = ADC_SampleTime_71Cycles5;
    constexpr uint32_t kSamptr = kSmp * 0x249249u & 0xFFFFFFu;          // channels 0..7
    constexpr uint32_t kRsqr3  = 0u | 1u << 5 | 2u << 10 | 3u << 15 | 4u << 20 | 5u << 25;
    constexpr uint32_t kRsqr2  = 6u | 7u << 5;
    constexpr uint32_t kRsqr1  = (8u - 1u) << 20;
    ADC_TypeDef* const adcs[2] = {ADC1, ADC2};
    for (ADC_TypeDef* a : adcs)
    {
        a->CTLR1 = (a == ADC1 ? ADC_Mode_RegSimult : ADC_Mode_Independent) | ADC_SCAN;
        a->CTLR2 = kCtlr2;
        a->RSQR1 = kRsqr1;
        a->SAMPTR2 = kSamptr;
        a->RSQR3 = kRsqr3;
        a->RSQR2 = kRsqr2;
    }

    adc_calibrate(ADC1);
    adc_calibrate(ADC2);

    ADC1->CTLR2 |= ADC_DMA;

    DMA1_Channel1->CFGR |= DMA_CFGR1_EN;

    ADC2->CTLR2 |= ADC_EXTTRIG | ADC_SWSTART;
    ADC1->CTLR2 |= ADC_EXTTRIG | ADC_SWSTART;

    uint32_t t0 = time_ticks32();
    const uint32_t warm = ms_to_ticks32(350u);

    while ((uint32_t)(time_ticks32() - t0) < warm)
    {
        const uint32_t f = DMA1->INTFR;
        if (f & (DMA1_FLAG_HT1 | DMA1_FLAG_TC1 | DMA1_FLAG_TE1)) break;
        if (g_dma_buf[0] != 0xFFFFFFFFu) break;
        delay(1);
    }

    {
        const uint32_t f = DMA1->INTFR;
        g_adc_dma_inited =
            (g_dma_buf[0] != 0xFFFFFFFFu) ||
            (f & (DMA1_FLAG_HT1 | DMA1_FLAG_TC1));
    }

    DMA1->INTFCR = (DMA1_FLAG_GL1 | DMA1_FLAG_HT1 | DMA1_FLAG_TC1 | DMA1_FLAG_TE1);

    if (g_adc_dma_inited)
    {
        ADC_DMA_filter_reset();
        ADC_DMA_wait_full();
    }
}