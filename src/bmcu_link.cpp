#include "bmcu_link.h"
#include "bmcu_link_protocol.h"

#include "Motion_control.h"
#include "ams.h"
#include "ws2812.h"
#include "ch32v20x_dma.h"
#include "ch32v20x_gpio.h"
#include "ch32v20x_misc.h"
#include "ch32v20x_rcc.h"
#include "ch32v20x_usart.h"
#include "hal/time_hw.h"
#include "hal/irq_wch.h"

extern WS2812_class SYS_RGB;

namespace
{
using namespace bmcu_link_protocol;

constexpr uint8_t kMaxDecoded = 64u;
constexpr uint8_t kMaxWire = 66u;
constexpr uint8_t kTxSlots = 8u;
constexpr uint8_t kRxRingSize = 128u;

struct TxSlot
{
    uint8_t length;
    uint8_t data[kMaxWire];
};

TxSlot g_tx[kTxSlots];
uint8_t g_tx_read = 0u;
uint8_t g_tx_write = 0u;
uint8_t g_tx_active = 0u;
uint16_t g_sequence = 0u;
uint32_t g_tx_drop = 0u;

volatile uint8_t g_rx[kRxRingSize];
volatile uint8_t g_rx_read = 0u;
volatile uint8_t g_rx_write = 0u;
volatile uint32_t g_rx_drop = 0u;
uint32_t g_rx_crc_error = 0u;
uint32_t g_rx_frame_error = 0u;
uint8_t g_rx_frame[kMaxWire];
uint8_t g_rx_frame_length = 0u;
uint8_t g_rx_discard = 0u;

uint32_t g_last_led_tick = 0u;
volatile uint32_t g_status_dirty = 0u;
uint8_t g_led_mode = 0u;
uint16_t g_led_remaining_s = 0u;
uint8_t g_led_restore_pending = 0u;
int g_control_error = 0;

enum LinkDiag : uint8_t
{
    kDiagOff = 0u,
    kDiagInit = 1u,
    kDiagTxPending = 2u,
    kDiagTxOk = 3u,
    kDiagTxTimeout = 4u,
};
uint8_t g_link_diag = kDiagOff;
uint8_t g_tx_fault = 0u;
uint32_t g_tx_started_tick = 0u;
uint32_t g_diag_until_tick = 0u;
uint32_t g_rx_diag_until_tick = 0u;


uint16_t crc16(const uint8_t* data, uint8_t length)
{
    uint16_t crc = 0xFFFFu;
    while (length--)
    {
        crc ^= static_cast<uint16_t>(*data++) << 8;
        for (uint8_t bit = 0u; bit < 8u; ++bit)
            crc = (crc & 0x8000u) ? static_cast<uint16_t>((crc << 1) ^ 0x1021u)
                                  : static_cast<uint16_t>(crc << 1);
    }
    return crc;
}

uint8_t cobs_encode(const uint8_t* input, uint8_t length, uint8_t* output)
{
    uint8_t code_index = 0u;
    uint8_t write_index = 1u;
    uint8_t code = 1u;
    output[0] = 0u;

    while (length--)
    {
        const uint8_t value = *input++;
        if (value == 0u)
        {
            output[code_index] = code;
            code_index = write_index++;
            code = 1u;
        }
        else
        {
            output[write_index++] = value;
            if (++code == 0xFFu)
            {
                output[code_index] = code;
                code_index = write_index++;
                code = 1u;
            }
        }
    }
    output[code_index] = code;
    return write_index;
}

uint8_t cobs_decode(const uint8_t* input, uint8_t length, uint8_t* output)
{
    uint8_t read_index = 0u;
    uint8_t write_index = 0u;
    while (read_index < length)
    {
        const uint8_t code = input[read_index++];
        if (code == 0u) return 0u;
        const uint8_t copy = static_cast<uint8_t>(code - 1u);
        if (static_cast<uint16_t>(read_index) + copy > length) return 0u;
        if (static_cast<uint16_t>(write_index) + copy > kMaxDecoded) return 0u;
        for (uint8_t i = 0u; i < copy; ++i) output[write_index++] = input[read_index++];
        if (code != 0xFFu && read_index < length)
        {
            if (write_index >= kMaxDecoded) return 0u;
            output[write_index++] = 0u;
        }
    }
    return write_index;
}

void tx_start_if_idle()
{
    if (g_tx_active || g_tx_read == g_tx_write) return;
    TxSlot& slot = g_tx[g_tx_read];
    DMA_Cmd(DMA1_Channel2, DISABLE);
    DMA_ClearFlag(DMA1_FLAG_TC2 | DMA1_FLAG_GL2);
    USART_ClearFlag(USART3, USART_FLAG_TC);
    DMA1_Channel2->MADDR = reinterpret_cast<uint32_t>(slot.data);
    DMA1_Channel2->CNTR = slot.length;
    DMA_Cmd(DMA1_Channel2, ENABLE);
    g_tx_active = 1u;
    g_tx_started_tick = time_ticks32();
    g_link_diag = kDiagTxPending;
}

void tx_finish_if_complete()
{
    if (!g_tx_active) return;
    if (DMA_GetFlagStatus(DMA1_FLAG_TC2) == RESET) return;
    if (USART_GetFlagStatus(USART3, USART_FLAG_TC) == RESET) return;
    DMA_Cmd(DMA1_Channel2, DISABLE);
    DMA_ClearFlag(DMA1_FLAG_TC2 | DMA1_FLAG_GL2);
    g_tx_active = 0u;
    g_tx_read = static_cast<uint8_t>((g_tx_read + 1u) % kTxSlots);
    g_link_diag = kDiagTxOk;
    g_diag_until_tick = time_ticks32() + time_hw_tpms * 5000u;
    tx_start_if_idle();
}

void tx_fail_if_needed(uint32_t now)
{
    if (!g_tx_active) return;
    const uint32_t timeout_ticks = time_hw_tpms * 250u;
    const bool timed_out = timeout_ticks != 0u &&
                           static_cast<uint32_t>(now - g_tx_started_tick) >= timeout_ticks;
    const bool transfer_error = DMA_GetFlagStatus(DMA1_FLAG_TE2) != RESET;
    if (!timed_out && !transfer_error) return;

    DMA_Cmd(DMA1_Channel2, DISABLE);
    DMA_ClearFlag(DMA1_FLAG_GL2 | DMA1_FLAG_TC2 | DMA1_FLAG_HT2 | DMA1_FLAG_TE2);
    g_tx_active = 0u;
    g_tx_fault = 1u;
    g_tx_read = g_tx_write;
    ++g_tx_drop;
    g_link_diag = kDiagTxTimeout;
}

bool enqueue(uint8_t kind, uint16_t sequence, const uint8_t* payload, uint8_t payload_length)
{
    if (g_tx_fault)
    {
        ++g_tx_drop;
        return false;
    }
    if (payload_length > static_cast<uint8_t>(kMaxDecoded - 7u)) return false;
    const uint8_t next = static_cast<uint8_t>((g_tx_write + 1u) % kTxSlots);
    if (next == g_tx_read)
    {
        ++g_tx_drop;
        return false;
    }

    uint8_t raw[kMaxDecoded];
    raw[0] = VERSION;
    raw[1] = kind;
    raw[2] = static_cast<uint8_t>(sequence);
    raw[3] = static_cast<uint8_t>(sequence >> 8);
    raw[4] = payload_length;
    for (uint8_t i = 0u; i < payload_length; ++i) raw[5u + i] = payload[i];
    const uint8_t crc_length = static_cast<uint8_t>(5u + payload_length);
    const uint16_t crc = crc16(raw, crc_length);
    raw[crc_length] = static_cast<uint8_t>(crc);
    raw[crc_length + 1u] = static_cast<uint8_t>(crc >> 8);

    TxSlot& slot = g_tx[g_tx_write];
    slot.length = cobs_encode(raw, static_cast<uint8_t>(crc_length + 2u), slot.data);
    slot.data[slot.length++] = 0u;
    g_tx_write = next;
    tx_start_if_idle();
    return true;
}

void put16(uint8_t* output, uint16_t value)
{
    output[0] = static_cast<uint8_t>(value);
    output[1] = static_cast<uint8_t>(value >> 8);
}

void put32(uint8_t* output, uint32_t value)
{
    output[0] = static_cast<uint8_t>(value);
    output[1] = static_cast<uint8_t>(value >> 8);
    output[2] = static_cast<uint8_t>(value >> 16);
    output[3] = static_cast<uint8_t>(value >> 24);
}

void send_hello()
{
    uint8_t payload[9];
    payload[0] = VERSION;
    put16(&payload[1], CAP_STATUS_EVENTS | CAP_LED_OVERRIDE | CAP_PING_PONG | CAP_RAW_HW_TICK);
    payload[3] = 1u;
    payload[4] = 1u;
    put32(&payload[5], time_hw_tpus * 1000000u);
    enqueue(KIND_HELLO, g_sequence++, payload, sizeof(payload));
}

void build_status_payload(uint8_t payload[27])
{
    const _ams& state = ams[BAMBU_BUS_AMS_NUM];
    put32(&payload[0], time_ticks32());
    put16(&payload[4], static_cast<uint16_t>(g_tx_drop));
    put16(&payload[6], static_cast<uint16_t>(g_rx_drop));
    put16(&payload[8], static_cast<uint16_t>(g_rx_crc_error));
    put16(&payload[10], static_cast<uint16_t>(g_rx_frame_error));
    payload[12] = state.now_filament_num;
    payload[13] = 0u;
    payload[14] = 0u;
    for (uint8_t ch = 0u; ch < 4u; ++ch)
    {
        if (filament_channel_inserted[ch]) payload[13] |= static_cast<uint8_t>(1u << ch);
        if (state.filament[ch].online) payload[14] |= static_cast<uint8_t>(1u << ch);
        payload[15u + ch] = static_cast<uint8_t>(state.filament[ch].motion);
        payload[19u + ch] = MC_PULL_pct[ch];
    }
    put16(&payload[23], state.pressure);
    payload[25] = g_led_mode;
    payload[26] = g_control_error ? 1u : 0u;
}

uint32_t take_status_changes()
{
    const uint32_t irq = irq_save_wch();
    const uint32_t pending = g_status_dirty;
    g_status_dirty = 0u;
    irq_restore_wch(irq);
    return pending;
}

void restore_status_changes(uint32_t reasons)
{
    if (reasons == 0u) return;
    const uint32_t irq = irq_save_wch();
    g_status_dirty |= reasons;
    irq_restore_wch(irq);
}

bool send_status(uint16_t sequence)
{
    uint8_t payload[27];
    build_status_payload(payload);
    return enqueue(KIND_STATUS, sequence, payload, sizeof(payload));
}

void send_ack(uint16_t sequence, uint8_t request_kind, AckResult result)
{
    const uint8_t payload[2] = {request_kind, static_cast<uint8_t>(result)};
    enqueue(KIND_ACK, sequence, payload, sizeof(payload));
}

void send_pong(uint16_t sequence, const uint8_t token[4])
{
    uint8_t payload[8];
    for (uint8_t i = 0u; i < 4u; ++i) payload[i] = token[i];
    put32(&payload[4], time_ticks32());
    enqueue(KIND_PONG, sequence, payload, sizeof(payload));
}

void handle_frame(const uint8_t* encoded, uint8_t encoded_length)
{
    uint8_t raw[kMaxDecoded];
    const uint8_t length = cobs_decode(encoded, encoded_length, raw);
    if (length < 7u)
    {
        ++g_rx_frame_error;
        return;
    }
    const uint8_t payload_length = raw[4];
    if (raw[0] != VERSION || static_cast<uint16_t>(payload_length) + 7u != length)
    {
        ++g_rx_frame_error;
        return;
    }
    const uint16_t expected = static_cast<uint16_t>(raw[length - 2u]) |
                              (static_cast<uint16_t>(raw[length - 1u]) << 8);
    if (crc16(raw, static_cast<uint8_t>(length - 2u)) != expected)
    {
        ++g_rx_crc_error;
        return;
    }

    // A fully decoded, CRC-valid command proves the physical USART3 RX path.
    g_rx_diag_until_tick = time_ticks32() + time_hw_tpms * 5000u;

    const uint8_t kind = raw[1];
    const uint16_t sequence = static_cast<uint16_t>(raw[2]) |
                              (static_cast<uint16_t>(raw[3]) << 8);
    const uint8_t* payload = &raw[5];

    if (kind == KIND_GET_STATUS)
    {
        if (payload_length != 0u)
        {
            send_ack(sequence, kind, ACK_BAD_VALUE);
        }
        else
        {
            const uint32_t pending = take_status_changes();
            if (!send_status(sequence)) restore_status_changes(pending);
        }
        return;
    }
    if (kind == KIND_PING)
    {
        if (payload_length != 4u) send_ack(sequence, kind, ACK_BAD_VALUE);
        else send_pong(sequence, payload);
        return;
    }
    if (kind == KIND_SET_LED_MODE)
    {
        if (payload_length != 3u || payload[0] > 3u)
        {
            send_ack(sequence, kind, ACK_BAD_VALUE);
            return;
        }
        const uint16_t timeout = static_cast<uint16_t>(payload[1]) |
                                 (static_cast<uint16_t>(payload[2]) << 8);
        const uint8_t previous_led_mode = g_led_mode;
        if (timeout == 0u || payload[0] == 0u)
        {
            g_led_mode = 0u;
            g_led_remaining_s = 0u;
            g_led_restore_pending = 1u;
        }
        else
        {
            g_led_mode = payload[0];
            g_led_remaining_s = timeout;
        }
        if (g_led_mode != previous_led_mode)
            bmcu_link_status_changed(BMCU_STATUS_CHANGE_LED);
        send_ack(sequence, kind, ACK_OK);
        return;
    }
    send_ack(sequence, kind, ACK_UNSUPPORTED);
}

void process_one_rx_frame()
{
    while (g_rx_read != g_rx_write)
    {
        const uint8_t value = g_rx[g_rx_read];
        g_rx_read = static_cast<uint8_t>((g_rx_read + 1u) & (kRxRingSize - 1u));
        if (value == 0u)
        {
            if (!g_rx_discard && g_rx_frame_length != 0u)
                handle_frame(g_rx_frame, g_rx_frame_length);
            g_rx_frame_length = 0u;
            g_rx_discard = 0u;
            return;
        }
        if (g_rx_discard) continue;
        if (g_rx_frame_length >= sizeof(g_rx_frame))
        {
            g_rx_discard = 1u;
            ++g_rx_frame_error;
        }
        else
        {
            g_rx_frame[g_rx_frame_length++] = value;
        }
    }
}
}

void bmcu_link_init(void)
{
    GPIO_InitTypeDef gpio = {0};
    USART_InitTypeDef uart = {0};
    DMA_InitTypeDef dma = {0};
    NVIC_InitTypeDef nvic = {0};

    RCC_APB1PeriphClockCmd(RCC_APB1Periph_USART3, ENABLE);
    RCC_APB2PeriphClockCmd(RCC_APB2Periph_GPIOB, ENABLE);
    RCC_AHBPeriphClockCmd(RCC_AHBPeriph_DMA1, ENABLE);

    gpio.GPIO_Pin = GPIO_Pin_10;
    gpio.GPIO_Speed = GPIO_Speed_50MHz;
    gpio.GPIO_Mode = GPIO_Mode_AF_PP;
    GPIO_Init(GPIOB, &gpio);
    gpio.GPIO_Pin = GPIO_Pin_11;
    gpio.GPIO_Mode = GPIO_Mode_IPU;
    GPIO_Init(GPIOB, &gpio);

    uart.USART_BaudRate = 115200u;
    uart.USART_WordLength = USART_WordLength_9b;
    uart.USART_StopBits = USART_StopBits_1;
    uart.USART_Parity = USART_Parity_Even;
    uart.USART_HardwareFlowControl = USART_HardwareFlowControl_None;
    uart.USART_Mode = USART_Mode_Tx | USART_Mode_Rx;
    USART_Init(USART3, &uart);
    USART_ITConfig(USART3, USART_IT_RXNE, ENABLE);

    nvic.NVIC_IRQChannel = USART3_IRQn;
    nvic.NVIC_IRQChannelPreemptionPriority = 1u;
    nvic.NVIC_IRQChannelSubPriority = 1u;
    nvic.NVIC_IRQChannelCmd = ENABLE;
    NVIC_Init(&nvic);

    dma.DMA_PeripheralBaseAddr = reinterpret_cast<uint32_t>(&USART3->DATAR);
    dma.DMA_MemoryBaseAddr = reinterpret_cast<uint32_t>(g_tx[0].data);
    dma.DMA_DIR = DMA_DIR_PeripheralDST;
    dma.DMA_BufferSize = 1u;
    dma.DMA_PeripheralInc = DMA_PeripheralInc_Disable;
    dma.DMA_MemoryInc = DMA_MemoryInc_Enable;
    dma.DMA_PeripheralDataSize = DMA_PeripheralDataSize_Byte;
    dma.DMA_MemoryDataSize = DMA_MemoryDataSize_Byte;
    dma.DMA_Mode = DMA_Mode_Normal;
    dma.DMA_Priority = DMA_Priority_Low;
    dma.DMA_M2M = DMA_M2M_Disable;
    DMA_Cmd(DMA1_Channel2, DISABLE);
    DMA_DeInit(DMA1_Channel2);
    DMA_Init(DMA1_Channel2, &dma);

    USART_Cmd(USART3, ENABLE);
    USART_DMACmd(USART3, USART_DMAReq_Tx, ENABLE);
    g_link_diag = kDiagInit;
    g_last_led_tick = time_ticks32();
    g_status_dirty = BMCU_STATUS_CHANGE_ALL;
    send_hello();
}

void bmcu_link_rx_isr_byte(uint8_t data)
{
    const uint8_t next = static_cast<uint8_t>((g_rx_write + 1u) & (kRxRingSize - 1u));
    if (next == g_rx_read)
    {
        ++g_rx_drop;
        return;
    }
    g_rx[g_rx_write] = data;
    g_rx_write = next;
}

void bmcu_link_service(void)
{
    tx_finish_if_complete();
    process_one_rx_frame();

    const uint32_t now = time_ticks32();
    tx_fail_if_needed(now);

    const uint32_t one_second = time_hw_tpms * 1000u;
    if (one_second != 0u && static_cast<uint32_t>(now - g_last_led_tick) >= one_second)
    {
        g_last_led_tick = now;
        if (g_led_remaining_s != 0u && --g_led_remaining_s == 0u)
        {
            g_led_mode = 0u;
            g_led_restore_pending = 1u;
            bmcu_link_status_changed(BMCU_STATUS_CHANGE_LED);
        }
    }

    if (g_status_dirty != 0u)
    {
        const uint32_t pending = take_status_changes();
        if (pending != 0u)
        {
            if (send_status(g_sequence)) ++g_sequence;
            else restore_status_changes(pending);
        }
    }

    tx_start_if_idle();
}

void bmcu_link_apply_led_override(void)
{
    const uint32_t now = time_ticks32();
    if (static_cast<int32_t>(g_rx_diag_until_tick - now) > 0)
    {
        // Cyan: a complete command arrived from the host and passed CRC.
        SYS_RGB.set_RGB(0x00u, 0x30u, 0x30u, 0u);
        return;
    }
    if (g_link_diag == kDiagInit || g_link_diag == kDiagTxPending)
    {
        SYS_RGB.set_RGB(0x00u, 0x00u, 0x30u, 0u);
        return;
    }
    if (g_link_diag == kDiagTxOk)
    {
        if (static_cast<int32_t>(g_diag_until_tick - now) > 0)
        {
            SYS_RGB.set_RGB(0x00u, 0x30u, 0x00u, 0u);
            return;
        }
        g_link_diag = kDiagOff;
    }
    if (g_link_diag == kDiagTxTimeout)
    {
        SYS_RGB.set_RGB(0x30u, 0x00u, 0x30u, 0u);
        return;
    }

    if (g_led_mode == 1u) SYS_RGB.set_RGB(0x00u, 0x00u, 0x30u, 0u);
    else if (g_led_mode == 2u) SYS_RGB.set_RGB(0x00u, 0x30u, 0x00u, 0u);
    else if (g_led_mode == 3u) SYS_RGB.set_RGB(0x30u, 0x00u, 0x30u, 0u);
    else if (g_led_restore_pending)
    {
        if (g_control_error) SYS_RGB.set_RGB(0x10u, 0x00u, 0x00u, 0u);
        else SYS_RGB.set_RGB(0x38u, 0x35u, 0x32u, 0u);
        g_led_restore_pending = 0u;
    }
}

void bmcu_link_set_control_error(int error)
{
    if (g_control_error == error) return;
    g_control_error = error;
    bmcu_link_status_changed(BMCU_STATUS_CHANGE_ERROR);
}

void bmcu_link_status_changed(uint32_t reasons)
{
    if (reasons == 0u) return;
    const uint32_t irq = irq_save_wch();
    g_status_dirty |= reasons;
    irq_restore_wch(irq);
}

uint32_t bmcu_link_tx_drop_count(void)
{
    return g_tx_drop;
}