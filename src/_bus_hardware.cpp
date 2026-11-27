#include "_bus_hardware.h"

#include "ch32v20x.h"
#include "ch32v20x_rcc.h"
#include "ch32v20x_gpio.h"
#include "ch32v20x_usart.h"
#include "ch32v20x_dma.h"
#include "ch32v20x_misc.h"
#include "core_riscv.h"
#include "hal/irq_wch.h"
#include "hal/periph_regs.h"
#include "crc_bus.h"
#include "printer_rx_framer.h"

uint16_t bus_host_device_type=0x0000;

namespace
{
constexpr uint32_t kPrinterTxTimeoutMs = 25u;
volatile uint32_t g_printer_tx_started_tick = 0u;

void printer_tx_abort()
{
    USART1->CTLR3 &= ~USART_DMAReq_Tx;
    DMA1_Channel4->CFGR &= (uint16_t)(~DMA_CFGR1_EN);
    DMA1->INTFCR = DMA1_FLAG_GL4 | DMA1_FLAG_TC4 | DMA1_FLAG_HT4 | DMA1_FLAG_TE4;
    USART_ClearITPendingBit(USART1, USART_IT_TC);
    GPIOA->BCR = GPIO_Pin_12;
    bus_port_to_host.note_activity();
    bus_port_to_host.idle = true;
}
}
#if BMCU_PRINTER_RX_DMA
namespace
{
constexpr uint16_t kPrinterRxDmaSize = 2560u;
constexpr uint16_t kPrinterRxDmaHalf = kPrinterRxDmaSize / 2u;
uint8_t g_printer_rx_dma_ring[kPrinterRxDmaSize] __attribute__((aligned(4)));
volatile uint32_t g_printer_rx_dma_half_events = 0u;
uint32_t g_printer_rx_dma_consumed = 0u;
uint32_t g_printer_rx_dma_parse_cursor = 0u;
uint16_t g_printer_rx_dma_parse_position = 0u;
uint32_t g_printer_rx_dma_frame_end = 0u;
volatile bool g_printer_rx_dma_frame_retained = false;
volatile bool g_printer_rx_dma_discard_after_release = false;

uint16_t g_printer_rx_dma_frame_position = 0u;
PrinterRxFramer g_printer_rx_parser;

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

void reset_dma_parser()
{
    g_printer_rx_parser.reset();
}

uint16_t dma_ring_position(uint32_t total)
{
    return (uint16_t)(total % kPrinterRxDmaSize);
}

void set_dma_cursors(uint32_t total)
{
    g_printer_rx_dma_consumed = total;
    g_printer_rx_dma_parse_cursor = total;
    g_printer_rx_dma_parse_position = dma_ring_position(total);
    g_printer_rx_dma_frame_end = total;
    g_printer_rx_dma_frame_retained = false;
    g_printer_rx_dma_discard_after_release = false;
    reset_dma_parser();
}

void advance_dma_parser_byte()
{
    ++g_printer_rx_dma_parse_cursor;
    if (++g_printer_rx_dma_parse_position == kPrinterRxDmaSize)
        g_printer_rx_dma_parse_position = 0u;
}

void publish_dma_frame(uint16_t length, uint8_t package_type)
{
    uint8_t *frame;
    const uint32_t contiguous =
        (uint32_t)g_printer_rx_dma_frame_position + length;
    if (contiguous <= kPrinterRxDmaSize)
    {
        frame = &g_printer_rx_dma_ring[g_printer_rx_dma_frame_position];
    }
    else
    {
        frame = bus_port_to_host.rx_compat_buf();
        const uint16_t first =
            (uint16_t)(kPrinterRxDmaSize - g_printer_rx_dma_frame_position);
        memcpy(frame, &g_printer_rx_dma_ring[g_printer_rx_dma_frame_position], first);
        memcpy(frame + first, g_printer_rx_dma_ring, length - first);
        saturating_increment(bus_port_to_host.rx_metrics.rx_compat_copy);
    }

    saturating_increment(bus_port_to_host.rx_metrics.rx_frames_valid);
    g_printer_rx_dma_frame_end = g_printer_rx_dma_parse_cursor;
    g_printer_rx_dma_frame_retained = true;
    bus_port_to_host.publish_recv_frame(
        frame, length, (_bus_data_type)package_type);
}

void discard_rx_dma_pending()
{
    if (g_printer_rx_dma_frame_retained)
    {
        g_printer_rx_dma_discard_after_release = true;
        reset_dma_parser();
        return;
    }
    set_dma_cursors(rx_dma_produced());
}

void discard_rx_dma_pending_as_loss()
{
    const uint32_t produced = rx_dma_produced();
    saturating_add(bus_port_to_host.rx_metrics.rx_resync_bytes,
                   produced - g_printer_rx_dma_consumed);
    bus_port_to_host.reset_rx_parser();
    set_dma_cursors(produced);
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
    RCC->AHBPCENR |= RCC_AHBPeriph_CRC;
    bus_crc_init();
    bus_port_to_host.init(bus_port_to_host_send_func);
    bus_uart1_init();
}

void bus_uart1_init()
{
    RCC->APB2PCENR |= RCC_APB2Periph_USART1 | RCC_APB2Periph_GPIOA;
    RCC->AHBPCENR |= RCC_AHBPeriph_DMA1;

    /* USART1 TX-->A.9   RX-->A.10   DE-->A.12*/
    gpio_config(GPIOA, GPIO_Pin_9, GPIO_CFG_AF_PP);
    gpio_config(GPIOA, GPIO_Pin_10, GPIO_CFG_IN_PU);
    gpio_config(GPIOA, GPIO_Pin_12, GPIO_CFG_OUT_PP);
    GPIOA->BCR = GPIO_Pin_12;

    // 1.25 Mbaud, 9 data bits (8 + even parity), 1 stop bit, no flow control.
    USART1->CTLR2 = 0u;
    USART1->CTLR3 = 0u;
    USART1->BRR = PERIPH_USART_BRR(PERIPH_PCLK2_HZ, 1250000u);
    USART1->CTLR1 = USART_WordLength_9b | USART_Parity_Even | USART_Mode_Tx | USART_Mode_Rx |
#if !BMCU_PRINTER_RX_DMA
                    USART_CTLR1_RXNEIE |
#endif
                    USART_CTLR1_TCIE;
#if BMCU_PRINTER_RX_DMA
    USART1->CTLR3 = USART_CTLR3_EIE;
#endif

    nvic_enable(USART1_IRQn, 0u, 0u);

    // DMA1 channel 4: USART1 TX, memory to peripheral, bytes, very high priority.
    // bus_uart1_dma_send() fills in the address and count.
    dma_channel_setup(DMA1_Channel4, DMA1_FLAG_GL4 | DMA1_FLAG_TC4 | DMA1_FLAG_HT4 | DMA1_FLAG_TE4,
                      DMA_DIR_PeripheralDST | DMA_MemoryInc_Enable | DMA_Priority_VeryHigh,
                      (uint32_t)&USART1->DATAR, 0u, 0u);

#if BMCU_PRINTER_RX_DMA
    // DMA1 channel 5: USART1 RX into the circular ring, with half/full/error
    // interrupts.
    dma_channel_setup(DMA1_Channel5, DMA1_FLAG_GL5 | DMA1_FLAG_TC5 | DMA1_FLAG_HT5 | DMA1_FLAG_TE5,
                      DMA_DIR_PeripheralSRC | DMA_MemoryInc_Enable | DMA_Mode_Circular |
                      DMA_Priority_VeryHigh | DMA_IT_HT | DMA_IT_TC | DMA_IT_TE,
                      (uint32_t)&USART1->DATAR, (uint32_t)g_printer_rx_dma_ring, kPrinterRxDmaSize);
    DMA1->INTFCR = DMA1_FLAG_GL5 | DMA1_FLAG_HT5 | DMA1_FLAG_TC5 | DMA1_FLAG_TE5;

    nvic_enable(DMA1_Channel5_IRQn, 0u, 1u);

    g_printer_rx_dma_half_events = 0u;
    set_dma_cursors(0u);
    DMA1_Channel5->CFGR |= DMA_CFGR1_EN;
    USART1->CTLR3 |= USART_DMAReq_Rx;
#endif

    USART1->CTLR1 |= USART_CTLR1_UE;
}

bool bus_uart1_rx_transport_quiet()
{
#if BMCU_PRINTER_RX_DMA
    return !g_printer_rx_parser.active() &&
           !g_printer_rx_dma_frame_retained &&
           rx_dma_produced() == g_printer_rx_dma_parse_cursor;
#else
    return true;
#endif
}

bool bus_uart1_reset_transport_quiescent()
{
    if (!bus_uart1_rx_transport_quiet()) return false;
    if ((DMA1_Channel4->CFGR & DMA_CFGR1_EN) != 0u) return false;
    if ((DMA1->INTFR & (DMA1_FLAG_TE4 | DMA1_FLAG_TE5)) != 0u) return false;
    if ((USART1->STATR & (USART_FLAG_ORE | USART_FLAG_NE | USART_FLAG_FE)) != 0u)
        return false;
    // Not USART_STATR_TC. USART1_IRQHandler clears TC on every completed
    // printer reply (USART_ClearITPendingBit(USART1, USART_IT_TC) writes the
    // TC bit of STATR to zero), so at rest after the first reply TC reads 0
    // forever and this predicate could never pass. That silently disabled
    // every flash persistence write (filament info and the merger state were
    // lost on each power cycle) and the BMCU-side soft-reset gate.
    // "Transmission complete" is still required: bus_port_to_host.idle only
    // becomes true in that same TC interrupt (or the TX abort path), and
    // quiet_for_us() checks idle before calling this; TX DMA disabled is
    // checked just above.
    if ((GPIOA->OUTDR & GPIO_Pin_12) != 0u) return false;
    return true;
}

void bus_uart1_rx_poll()
{
#if BMCU_PRINTER_RX_DMA
    if (!bus_port_to_host.idle)
    {
        discard_rx_dma_pending();
        return;
    }
    if (g_printer_rx_dma_frame_retained) return;

    const uint32_t produced = rx_dma_produced();
    uint32_t held = produced - g_printer_rx_dma_consumed;
    if (held > kPrinterRxDmaSize)
    {
        saturating_increment(bus_port_to_host.rx_metrics.rx_dma_overrun);
        saturating_add(bus_port_to_host.rx_metrics.rx_resync_bytes,
                       held - kPrinterRxDmaSize);
        set_dma_cursors(produced - kPrinterRxDmaSize);
        held = kPrinterRxDmaSize;
    }
    if (held > bus_port_to_host.rx_metrics.rx_dma_max_pending)
        bus_port_to_host.rx_metrics.rx_dma_max_pending = held;

    uint32_t available = produced - g_printer_rx_dma_parse_cursor;
    while (available != 0u && !g_printer_rx_dma_frame_retained)
    {
        const uint32_t byte_cursor = g_printer_rx_dma_parse_cursor;
        const uint16_t byte_position = g_printer_rx_dma_parse_position;
        const uint8_t value = g_printer_rx_dma_ring[byte_position];
        advance_dma_parser_byte();
        --available;
        bus_port_to_host.note_activity();
        saturating_increment(bus_port_to_host.rx_metrics.rx_bytes);

        const bool was_active = g_printer_rx_parser.active();
        const PrinterRxFramerResult result = g_printer_rx_parser.push(value);
        switch (result.event)
        {
        case PrinterRxFramerEvent::frame_started:
            g_printer_rx_dma_frame_position = byte_position;
            g_printer_rx_dma_consumed = byte_cursor;
            break;

        case PrinterRxFramerEvent::bad_length:
            saturating_increment(bus_port_to_host.rx_metrics.rx_bad_length);
            g_printer_rx_dma_consumed = g_printer_rx_dma_parse_cursor;
            break;

        case PrinterRxFramerEvent::header_crc_error:
            saturating_increment(bus_port_to_host.rx_metrics.rx_header_crc_error);
            g_printer_rx_dma_consumed = g_printer_rx_dma_parse_cursor;
            break;

        case PrinterRxFramerEvent::heartbeat_complete:
            g_printer_rx_dma_consumed = g_printer_rx_dma_parse_cursor;
            bambubus_heartbeat_seen_fast();
            break;

        case PrinterRxFramerEvent::frame_complete:
            publish_dma_frame(result.frame_length, result.package_type);
            break;

        case PrinterRxFramerEvent::none:
        default:
            if (!was_active)
            {
                saturating_increment(bus_port_to_host.rx_metrics.rx_resync_bytes);
                g_printer_rx_dma_consumed = g_printer_rx_dma_parse_cursor;
            }
            break;
        }
    }
#endif
}

void bus_uart1_rx_release_frame()
{
#if BMCU_PRINTER_RX_DMA
    if (g_printer_rx_dma_frame_retained)
    {
        const uint32_t produced = rx_dma_produced();
        const uint32_t held = produced - g_printer_rx_dma_consumed;
        if (held > kPrinterRxDmaSize)
        {
            saturating_increment(bus_port_to_host.rx_metrics.rx_dma_overrun);
            saturating_add(bus_port_to_host.rx_metrics.rx_resync_bytes,
                           held - kPrinterRxDmaSize);
            set_dma_cursors(produced);
            return;
        }

        g_printer_rx_dma_consumed = g_printer_rx_dma_frame_end;
        g_printer_rx_dma_frame_retained = false;
        if (g_printer_rx_dma_discard_after_release)
        {
            saturating_add(bus_port_to_host.rx_metrics.rx_resync_bytes,
                           produced - g_printer_rx_dma_frame_end);
            set_dma_cursors(produced);
        }
    }
#endif
}

void bus_uart1_tx_poll()
{
    if (bus_port_to_host.idle) return;

    const uint32_t flags = DMA1->INTFR;
    if ((flags & DMA1_FLAG_TE4) != 0u)
    {
        bus_port_to_host.report_tx_fault(bus_tx_fault::dma_error);
        printer_tx_abort();
        return;
    }

    const uint32_t timeout_ticks = time_hw_tpms * kPrinterTxTimeoutMs;
    if (timeout_ticks != 0u &&
        static_cast<uint32_t>(time_ticks32() - g_printer_tx_started_tick) >= timeout_ticks)
    {
        bus_port_to_host.report_tx_fault(bus_tx_fault::timeout);
        printer_tx_abort();
    }
}

void bus_uart1_dma_send(unsigned char *data, uint16_t length)
{
    if (!bus_port_to_host.idle) return;

#if BMCU_PRINTER_RX_DMA
    discard_rx_dma_pending_as_loss();
#endif
    bus_port_to_host.idle = false;

    DMA1_Channel4->CFGR &= (uint16_t)(~DMA_CFGR1_EN);
    DMA1->INTFCR = DMA1_FLAG_GL4 | DMA1_FLAG_TC4 | DMA1_FLAG_HT4 | DMA1_FLAG_TE4;

    DMA1_Channel4->MADDR = (uint32_t)data;
    DMA1_Channel4->CNTR  = length;

    // DE = TX
    GPIOA->BSHR = GPIO_Pin_12;

    // wyczyść TC
    USART_ClearITPendingBit(USART1, USART_IT_TC);

    g_printer_tx_started_tick = time_ticks32();
    USART1->CTLR3 |= USART_DMAReq_Tx;
    DMA1_Channel4->CFGR |= DMA_CFGR1_EN;
    if (bus_port_to_host.tx_metrics.tx_started != 0xFFFFFFFFu)
        ++bus_port_to_host.tx_metrics.tx_started;
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
        const bool tx_was_active = !bus_port_to_host.idle;
        if (tx_was_active && (DMA1->INTFR & DMA1_FLAG_TE4) != 0u)
        {
            bus_port_to_host.report_tx_fault(bus_tx_fault::dma_error);
            printer_tx_abort();
            return;
        }
        USART_ClearITPendingBit(USART1, USART_IT_TC);
        USART1->CTLR3 &= ~USART_DMAReq_Tx;
        DMA1_Channel4->CFGR &= (uint16_t)(~DMA_CFGR1_EN);

        // DE = RX
        GPIOA->BCR = GPIO_Pin_12;

        // TX done
        bus_port_to_host.note_activity();
        bus_port_to_host.idle = true;
        if (tx_was_active && bus_port_to_host.tx_metrics.tx_completed != 0xFFFFFFFFu)
            ++bus_port_to_host.tx_metrics.tx_completed;
    }
}
#if BMCU_PRINTER_RX_DMA
extern "C" void DMA1_Channel5_IRQHandler(void) __attribute__((interrupt("WCH-Interrupt-fast")));
void DMA1_Channel5_IRQHandler(void)
{
    account_rx_dma_flags();
}
#endif
