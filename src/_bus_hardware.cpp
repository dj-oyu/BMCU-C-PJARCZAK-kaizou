#include "_bus_hardware.h"

#include "ch32v20x.h"
#include "ch32v20x_rcc.h"
#include "ch32v20x_gpio.h"
#include "ch32v20x_usart.h"
#include "ch32v20x_dma.h"
#include "ch32v20x_misc.h"
#include "core_riscv.h"
#include "hal/irq_wch.h"
#include "crc_bus.h"

uint16_t bus_host_device_type=0x0000;

DMA_InitTypeDef bus_uart1_dma_init_structure;
#if BMCU_PRINTER_RX_DMA
namespace
{
constexpr uint16_t kPrinterRxDmaSize = 2560u;
constexpr uint16_t kPrinterRxDmaHalf = kPrinterRxDmaSize / 2u;
uint8_t g_printer_rx_dma_ring[kPrinterRxDmaSize] __attribute__((aligned(4)));
volatile uint32_t g_printer_rx_dma_half_events = 0u;
uint32_t g_printer_rx_dma_consumed = 0u;

inline void saturating_increment(volatile uint32_t& value)
{
    if (value != 0xFFFFFFFFu) ++value;
}

void saturating_add(volatile uint32_t& value, uint32_t add)
{
    if (0xFFFFFFFFu - value < add) value = 0xFFFFFFFFu;
    else value += add;
}

void account_rx_dma_flags()
{
    const uint32_t flags = DMA1->INTFR;
    uint32_t clear = 0u;
    if (flags & DMA1_FLAG_HT5)
    {
        ++g_printer_rx_dma_half_events;
        clear |= DMA1_FLAG_HT5;
    }
    if (flags & DMA1_FLAG_TC5)
    {
        ++g_printer_rx_dma_half_events;
        clear |= DMA1_FLAG_TC5;
        saturating_increment(bus_port_to_host.rx_metrics.rx_dma_wrap);
    }
    if (flags & DMA1_FLAG_TE5)
    {
        clear |= DMA1_FLAG_TE5;
        saturating_increment(bus_port_to_host.rx_metrics.rx_dma_error);
    }
    if (clear != 0u) DMA1->INTFCR = clear;
}

uint32_t rx_dma_produced()
{
    const uint32_t irq_state = irq_save_wch();
    uint32_t events_before;
    uint32_t events_after;
    uint16_t remaining_a;
    uint16_t remaining_b;
    do
    {
        account_rx_dma_flags();
        events_before = g_printer_rx_dma_half_events;
        remaining_a = (uint16_t)DMA1_Channel5->CNTR;
        remaining_b = (uint16_t)DMA1_Channel5->CNTR;
        account_rx_dma_flags();
        events_after = g_printer_rx_dma_half_events;
    } while (events_before != events_after || remaining_a != remaining_b);

    const uint16_t position = (uint16_t)(kPrinterRxDmaSize - remaining_a);
    const uint16_t within_half = position >= kPrinterRxDmaHalf
        ? (uint16_t)(position - kPrinterRxDmaHalf) : position;
    __asm volatile ("" ::: "memory");
    const uint32_t produced =
        events_after * (uint32_t)kPrinterRxDmaHalf + within_half;
    irq_restore_wch(irq_state);
    return produced;
}

void discard_rx_dma_pending()
{
    g_printer_rx_dma_consumed = rx_dma_produced();
}

void discard_rx_dma_pending_as_loss()
{
    const uint32_t produced = rx_dma_produced();
    saturating_add(bus_port_to_host.rx_metrics.rx_resync_bytes,
                   produced - g_printer_rx_dma_consumed);
    bus_port_to_host.reset_rx_parser();
    g_printer_rx_dma_consumed = produced;
}
}
#endif
void bus_uart1_init();
void bus_uart1_dma_send(uint8_t *data, uint16_t length);


_bus_port_deal bus_port_to_host;

#define uart1_port_irq(data) bus_port_to_host.irq(data)
#define uart1_port_idle bus_port_to_host.idle
#define bus_port_to_host_send_func bus_uart1_dma_send

void bus_init()
{
    RCC_AHBPeriphClockCmd(RCC_AHBPeriph_CRC, ENABLE);
    bus_crc_init();
    bus_port_to_host.init(bus_port_to_host_send_func);
    bus_uart1_init();
}

void bus_uart1_init()
{
    GPIO_InitTypeDef GPIO_InitStructure = {0};
    USART_InitTypeDef USART_InitStructure = {0};
    NVIC_InitTypeDef NVIC_InitStructure = {0};

    RCC_APB2PeriphClockCmd(RCC_APB2Periph_USART1, ENABLE);
    RCC_APB2PeriphClockCmd(RCC_APB2Periph_GPIOA, ENABLE);
    RCC_AHBPeriphClockCmd(RCC_AHBPeriph_DMA1, ENABLE);

    /* USART1 TX-->A.9   RX-->A.10   DE-->A.12*/
    GPIO_InitStructure.GPIO_Pin = GPIO_Pin_9; // TX
    GPIO_InitStructure.GPIO_Speed = GPIO_Speed_50MHz;
    GPIO_InitStructure.GPIO_Mode = GPIO_Mode_AF_PP;
    GPIO_Init(GPIOA, &GPIO_InitStructure);
    GPIO_InitStructure.GPIO_Pin = GPIO_Pin_10; // RX
    GPIO_InitStructure.GPIO_Mode = GPIO_Mode_IPU;
    GPIO_Init(GPIOA, &GPIO_InitStructure);
    GPIO_InitStructure.GPIO_Pin = GPIO_Pin_12; // DE
    GPIO_InitStructure.GPIO_Speed = GPIO_Speed_50MHz;
    GPIO_InitStructure.GPIO_Mode = GPIO_Mode_Out_PP;
    GPIO_Init(GPIOA, &GPIO_InitStructure);
    GPIOA->BCR = GPIO_Pin_12;

    USART_InitStructure.USART_BaudRate = 1250000;
    USART_InitStructure.USART_WordLength = USART_WordLength_9b;
    USART_InitStructure.USART_StopBits = USART_StopBits_1;
    USART_InitStructure.USART_Parity = USART_Parity_Even;
    USART_InitStructure.USART_HardwareFlowControl = USART_HardwareFlowControl_None;
    USART_InitStructure.USART_Mode = USART_Mode_Tx | USART_Mode_Rx;

    USART_Init(USART1, &USART_InitStructure);
#if BMCU_PRINTER_RX_DMA
    USART_ITConfig(USART1, USART_IT_RXNE, DISABLE);
#else
    USART_ITConfig(USART1, USART_IT_RXNE, ENABLE);
#endif
    USART_ITConfig(USART1, USART_IT_TC, ENABLE);
#if BMCU_PRINTER_RX_DMA
    USART_ITConfig(USART1, USART_IT_ERR, ENABLE);
#endif

    NVIC_InitStructure.NVIC_IRQChannel = USART1_IRQn;
    NVIC_InitStructure.NVIC_IRQChannelPreemptionPriority = 0;
    NVIC_InitStructure.NVIC_IRQChannelSubPriority = 0;
    NVIC_InitStructure.NVIC_IRQChannelCmd = ENABLE;
    NVIC_Init(&NVIC_InitStructure);

    // Configure DMA1 channel 4 for USART1 TX
    bus_uart1_dma_init_structure.DMA_PeripheralBaseAddr = (uint32_t)&USART1->DATAR;
    bus_uart1_dma_init_structure.DMA_MemoryBaseAddr = (uint32_t)0;
    bus_uart1_dma_init_structure.DMA_DIR = DMA_DIR_PeripheralDST;
    bus_uart1_dma_init_structure.DMA_Mode = DMA_Mode_Normal;
    bus_uart1_dma_init_structure.DMA_PeripheralInc = DMA_PeripheralInc_Disable;
    bus_uart1_dma_init_structure.DMA_MemoryInc = DMA_MemoryInc_Enable;
    bus_uart1_dma_init_structure.DMA_Priority = DMA_Priority_VeryHigh;
    bus_uart1_dma_init_structure.DMA_M2M = DMA_M2M_Disable;
    bus_uart1_dma_init_structure.DMA_MemoryDataSize = DMA_MemoryDataSize_Byte;
    bus_uart1_dma_init_structure.DMA_PeripheralDataSize = DMA_PeripheralDataSize_Byte;
    bus_uart1_dma_init_structure.DMA_BufferSize = 0;
    DMA_Init(DMA1_Channel4, &bus_uart1_dma_init_structure);

#if BMCU_PRINTER_RX_DMA
    DMA_InitTypeDef rx_dma = {0};
    rx_dma.DMA_PeripheralBaseAddr = (uint32_t)&USART1->DATAR;
    rx_dma.DMA_MemoryBaseAddr = (uint32_t)g_printer_rx_dma_ring;
    rx_dma.DMA_DIR = DMA_DIR_PeripheralSRC;
    rx_dma.DMA_BufferSize = kPrinterRxDmaSize;
    rx_dma.DMA_PeripheralInc = DMA_PeripheralInc_Disable;
    rx_dma.DMA_MemoryInc = DMA_MemoryInc_Enable;
    rx_dma.DMA_PeripheralDataSize = DMA_PeripheralDataSize_Byte;
    rx_dma.DMA_MemoryDataSize = DMA_MemoryDataSize_Byte;
    rx_dma.DMA_Mode = DMA_Mode_Circular;
    rx_dma.DMA_Priority = DMA_Priority_VeryHigh;
    rx_dma.DMA_M2M = DMA_M2M_Disable;
    DMA_DeInit(DMA1_Channel5);
    DMA_Init(DMA1_Channel5, &rx_dma);
    DMA1->INTFCR = DMA1_FLAG_GL5 | DMA1_FLAG_HT5 | DMA1_FLAG_TC5 | DMA1_FLAG_TE5;
    DMA_ITConfig(DMA1_Channel5, DMA_IT_HT | DMA_IT_TC | DMA_IT_TE, ENABLE);

    NVIC_InitStructure.NVIC_IRQChannel = DMA1_Channel5_IRQn;
    NVIC_InitStructure.NVIC_IRQChannelPreemptionPriority = 0;
    NVIC_InitStructure.NVIC_IRQChannelSubPriority = 1;
    NVIC_InitStructure.NVIC_IRQChannelCmd = ENABLE;
    NVIC_Init(&NVIC_InitStructure);

    g_printer_rx_dma_half_events = 0u;
    g_printer_rx_dma_consumed = 0u;
    DMA_Cmd(DMA1_Channel5, ENABLE);
    USART_DMACmd(USART1, USART_DMAReq_Rx, ENABLE);
#endif

    USART_Cmd(USART1, ENABLE);
}

bool bus_uart1_rx_transport_quiet()
{
#if BMCU_PRINTER_RX_DMA
    return rx_dma_produced() == g_printer_rx_dma_consumed;
#else
    return true;
#endif
}

void bus_uart1_rx_poll()
{
#if BMCU_PRINTER_RX_DMA
    if (!bus_port_to_host.idle)
    {
        discard_rx_dma_pending();
        return;
    }

    const uint32_t produced = rx_dma_produced();
    uint32_t available = produced - g_printer_rx_dma_consumed;
    if (available > kPrinterRxDmaSize)
    {
        saturating_increment(bus_port_to_host.rx_metrics.rx_dma_overrun);
        bus_port_to_host.reset_rx_parser();
        saturating_add(bus_port_to_host.rx_metrics.rx_resync_bytes,
                       available - kPrinterRxDmaSize);
        g_printer_rx_dma_consumed = produced - kPrinterRxDmaSize;
        available = kPrinterRxDmaSize;
    }
    if (available > bus_port_to_host.rx_metrics.rx_dma_max_pending)
        bus_port_to_host.rx_metrics.rx_dma_max_pending = available;

    while (available-- != 0u)
    {
        uint32_t index = g_printer_rx_dma_consumed;
        while (index >= kPrinterRxDmaSize) index -= kPrinterRxDmaSize;
        const uint8_t value = g_printer_rx_dma_ring[index];
        ++g_printer_rx_dma_consumed;
        bus_port_to_host.irq(value);
        if (bus_port_to_host.recv_data_len != 0) break;
    }
#endif
}

void bus_uart1_dma_send(unsigned char *data, uint16_t length)
{
    if (!bus_port_to_host.idle) return;

#if BMCU_PRINTER_RX_DMA
    discard_rx_dma_pending_as_loss();
#endif
    bus_port_to_host.idle = false;

    DMA1_Channel4->CFGR &= (uint16_t)(~DMA_CFGR1_EN);

    DMA1_Channel4->MADDR = (uint32_t)data;
    DMA1_Channel4->CNTR  = length;

    // DE = TX
    GPIOA->BSHR = GPIO_Pin_12;

    // wyczyść TC
    USART_ClearITPendingBit(USART1, USART_IT_TC);

    USART1->CTLR3 |= USART_DMAReq_Tx;
    DMA1_Channel4->CFGR |= DMA_CFGR1_EN;
}

extern "C" void USART1_IRQHandler(void) __attribute__((interrupt("WCH-Interrupt-fast")));
void USART1_IRQHandler(void)
{
#if BMCU_PRINTER_RX_DMA
    const uint16_t error = (uint16_t)(USART1->STATR &
        (USART_FLAG_ORE | USART_FLAG_NE | USART_FLAG_FE));
    if (error != 0u)
    {
        const volatile uint32_t data = USART1->DATAR;
        (void)data;
        if (error & USART_FLAG_ORE)
            saturating_increment(bus_port_to_host.rx_metrics.rx_usart_overrun);
        else
            saturating_increment(bus_port_to_host.rx_metrics.rx_resync_bytes);
        bus_port_to_host.reset_rx_parser();
        discard_rx_dma_pending();
    }
#else
    if (USART_GetITStatus(USART1, USART_IT_RXNE) != RESET)
    {
        const uint8_t d = (uint8_t)USART_ReceiveData(USART1);
        if (bus_port_to_host.idle) uart1_port_irq(d);
    }
#endif
    if (USART_GetITStatus(USART1, USART_IT_TC) != RESET)
    {
        USART_ClearITPendingBit(USART1, USART_IT_TC);
        USART1->CTLR3 &= ~USART_DMAReq_Tx;
        DMA1_Channel4->CFGR &= (uint16_t)(~DMA_CFGR1_EN);

        // DE = RX
        GPIOA->BCR = GPIO_Pin_12;

        // TX done
        bus_port_to_host.note_activity();
        bus_port_to_host.idle = true;
    }
}
#if BMCU_PRINTER_RX_DMA
extern "C" void DMA1_Channel5_IRQHandler(void) __attribute__((interrupt("WCH-Interrupt-fast")));
void DMA1_Channel5_IRQHandler(void)
{
    account_rx_dma_flags();
}
#endif