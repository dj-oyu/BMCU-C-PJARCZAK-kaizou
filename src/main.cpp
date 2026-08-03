#include "MC_PULL_calibration.h"
#include "ws2812.h"

#include "Flash_saves.h"
#include "Motion_control.h"
#include "_bus_hardware.h"
#include "ams.h"
#include "ahub_bus.h"
#include "bambu_bus_ams.h"
#include "ADC_DMA.h"
#include "Debug_log.h"
#include "bmcu_link.h"
#include "ams_merger_policy.h"
#include <string.h>

WS2812_class SYS_RGB;
WS2812_class RGBOUT[4];

void RGB_init()
{
    SYS_RGB.init(1, GPIOD, GPIO_Pin_1);
    RGBOUT[0].init(2, GPIOA, GPIO_Pin_11);
    RGBOUT[1].init(2, GPIOA, GPIO_Pin_8);
    RGBOUT[2].init(2, GPIOB, GPIO_Pin_1);
    RGBOUT[3].init(2, GPIOB, GPIO_Pin_0);
}

void RGB_update()
{
    bmcu_link_apply_led_override();
    if (!(SYS_RGB.is_dirty() ||
          RGBOUT[0].is_dirty() || RGBOUT[1].is_dirty() ||
          RGBOUT[2].is_dirty() || RGBOUT[3].is_dirty()))
        return;

    static uint32_t last = 0u;
    static uint8_t next_strip = 0u;

    uint32_t min_gap = time_hw_tpms;
    if (!min_gap) min_gap = 1u;

    const uint32_t now = time_ticks32();
    if (last != 0u && static_cast<uint32_t>(now - last) < min_gap)
        return;

    for (uint8_t attempt = 0u; attempt < 5u; ++attempt)
    {
        const uint8_t strip = next_strip;
        if (++next_strip >= 5u) next_strip = 0u;

        switch (strip)
        {
        case 0u:
            if (SYS_RGB.is_dirty()) { SYS_RGB.updata(); last = now; return; }
            break;
        case 1u:
            if (RGBOUT[0].is_dirty()) { RGBOUT[0].updata(); last = now; return; }
            break;
        case 2u:
            if (RGBOUT[1].is_dirty()) { RGBOUT[1].updata(); last = now; return; }
            break;
        case 3u:
            if (RGBOUT[2].is_dirty()) { RGBOUT[2].updata(); last = now; return; }
            break;
        default:
            if (RGBOUT[3].is_dirty()) { RGBOUT[3].updata(); last = now; return; }
            break;
        }
    }
}
static uint8_t g_fil_dirty = 0;
static ams_merger::State g_merger = { ams_merger::kNoChannel, 0u };
static uint8_t g_state_dirty = 0;

static inline void ram_to_flashinfo(uint8_t fil, Flash_FilamentInfo* o)
{
    const _filament* f = &ams[BAMBU_BUS_AMS_NUM].filament[fil];

    memcpy(o->bambubus_filament_id, f->bambubus_filament_id, sizeof(o->bambubus_filament_id));
    o->color_R = f->color_R;
    o->color_G = f->color_G;
    o->color_B = f->color_B;
    o->color_A = f->color_A;
    o->temperature_min = f->temperature_min;
    o->temperature_max = f->temperature_max;
    memcpy(o->name, f->name, sizeof(o->name));
}

static inline void flashinfo_to_ram(uint8_t fil, const Flash_FilamentInfo* i)
{
    _filament* f = &ams[BAMBU_BUS_AMS_NUM].filament[fil];

    memcpy(f->bambubus_filament_id, i->bambubus_filament_id, sizeof(i->bambubus_filament_id));
    f->color_R = i->color_R;
    f->color_G = i->color_G;
    f->color_B = i->color_B;
    f->color_A = i->color_A;
    f->temperature_min = i->temperature_min;
    f->temperature_max = i->temperature_max;

    memset(f->name, 0, sizeof(f->name));
    memcpy(f->name, i->name, sizeof(i->name));
    f->name[sizeof(f->name) - 1u] = 0;
}

bool ams_datas_read()
{
    bool any = false;

    for (uint8_t fil = 0; fil < 4u; fil++)
    {
        Flash_FilamentInfo fi;
        if (Flash_AMS_filament_read(fil, &fi))
        {
            flashinfo_to_ram(fil, &fi);
            any = true;
        }
    }

    return any;
}

void ams_datas_set_need_to_save()
{
    g_fil_dirty = 0x0Fu;
}

void ams_datas_set_need_to_save_filament(uint8_t filament_idx)
{
    if (filament_idx >= 4u) return;
    g_fil_dirty |= (uint8_t)(1u << filament_idx);
}

// ams_merger::acquire keeps a refusal that reads like it could silently lose a
// load, but it cannot reach that case. Both callers -- bambu_bus_ams.cpp on
// before_on_use and on on_use -- sit behind `if (!allow_any) return true;`, and
// allow_any is (loaded == 0xFF || loaded == ch) sampled earlier in the same call
// with nothing in between that writes the latch. So on arrival here the merger
// is either free or already owned by this very channel, and the refusal only
// ever suppresses a redundant re-acquire. It stays as defence for a caller that
// does not yet exist; if one is ever added, a merger owned by a different
// channel is a genuine conflict and would deserve a MOTION_FAULT_* report
// rather than this silence.
void ams_state_set_loaded(uint8_t filament_ch)
{
    if (ams_merger::acquire(g_merger, filament_ch)) g_state_dirty = 1u;
}

// Refused wildcard releases, saturating, and whether this TAIL episode has
// already said so. TAIL has no timeout, so a merger that is wedged is never
// expired by anything -- the refusal count is the fuse's replacement, and it
// has to be visible without being a storm. Idle frames arrive continuously
// while a printer is paused, which is precisely when TAIL is held, so the event
// is edge-latched to the first refusal of each episode and the count keeps
// running underneath it.
static uint16_t g_tail_refused_count = 0u;
static uint8_t  g_tail_refused_reported = 0u;

void ams_state_set_unloaded(uint8_t filament_ch)
{
    switch (ams_merger::release(g_merger, filament_ch))
    {
    case ams_merger::release_done:
        g_state_dirty = 1u;
        g_tail_refused_reported = 0u;
        break;

    case ams_merger::release_refused_tail:
        if (g_tail_refused_count != 0xFFFFu) ++g_tail_refused_count;
        if (!g_tail_refused_reported)
        {
            g_tail_refused_reported = 1u;
            bmcu_link_merger_tail_refused(g_tail_refused_count);
        }
        break;

    default:
        break;
    }
}

// LOADED(ch) -> TAIL(ch): the owning channel's key has read empty for the whole
// debounce window, so the tail is past the switch. The merger is still occupied
// and the printer's retract must still be accepted, which is exactly what not
// releasing here buys.
void ams_state_preempt(uint8_t claiming_ch)
{
    const uint8_t result = ams_merger::preempt(g_merger);
    if (result == ams_merger::release_none) return;

    g_state_dirty = 1u;
    g_tail_refused_reported = 0u;

    if (result == ams_merger::release_preempted_tail)
        bmcu_link_merger_tail_preempted(claiming_ch);
}

void ams_state_set_tail(void)
{
    if (!ams_merger::to_tail(g_merger)) return;

    g_state_dirty = 1u;
    // A fresh TAIL episode gets its own first refusal reported.
    g_tail_refused_reported = 0u;
}

uint8_t ams_state_get_loaded(void)
{
    return g_merger.owner;
}

bool ams_state_is_tail(void)
{
    return ams_merger::is_tail(g_merger);
}

static void ams_state_save_run()
{
    if (!g_state_dirty) return;

    if (Flash_AMS_state_write(g_merger.owner, ams_merger::is_tail(g_merger)))
        g_state_dirty = 0u;
}

void ams_datas_save_run()
{
    if (!g_fil_dirty) return;

    uint8_t fil = 0xFFu;
    for (uint8_t i = 0; i < 4u; i++)
    {
        if (g_fil_dirty & (uint8_t)(1u << i))
        {
            fil = i;
            break;
        }
    }

    if (fil == 0xFFu) return;

    Flash_FilamentInfo now;
    ram_to_flashinfo(fil, &now);

    if (Flash_AMS_filament_write(fil, &now))
        g_fil_dirty &= (uint8_t)~(1u << fil);
}

static void persistence_save_run()
{
    if (!g_state_dirty && !g_fil_dirty) return;
    if (!bus_port_to_host.quiet_for_us(5000u)) return;

    static uint32_t last_save_tick = 0u;
    const uint32_t now = time_ticks32();
    const uint32_t min_gap = time_hw_tpms * 10u;
    if (last_save_tick != 0u && static_cast<uint32_t>(now - last_save_tick) < min_gap) return;

    if (g_state_dirty) ams_state_save_run();
    else ams_datas_save_run();
    last_save_tick = time_ticks32();
}
int main(void)
{
    SystemInit();
    SystemCoreClockUpdate();
    time_hw_init();

    __enable_irq();

    WWDG_DeInit();
    RCC_APB1PeriphClockCmd(RCC_APB1Periph_WWDG, DISABLE);
    RCC_APB2PeriphClockCmd(RCC_APB2Periph_AFIO, ENABLE);

    NVIC_PriorityGroupConfig(NVIC_PriorityGroup_1);
    GPIO_PinRemapConfig(GPIO_Remap_PD01, ENABLE);

    // Start the dedicated monitor UART before any sensor/calibration waits.
    bmcu_link_init();

    RGB_init();
    delay(10);

    SYS_RGB.set_RGB(0x10, 0x00, 0x00, 0);
    for (int i = 0; i < 4; i++) RGBOUT[i].set_RGB(0, 0, 0, 0);
    RGB_update();
    delay(50);

    ams_init();
    Flash_saves_init();

    ADC_DMA_init();
    ADC_DMA_wait_full();

    MC_PULL_calibration_boot();
    ams_datas_read();

    // TAIL survives the reboot, and restores the same session as LOADED.
    //
    // Surviving is the whole reason this state is flash-backed: a strand lying
    // between the online key and the extruder is still there after a power cut,
    // and coming back as UNLOADED would refuse the retract that clears it --
    // the exact failure this state exists to prevent, just re-armed by a
    // reboot.
    //
    // Note that the channel byte is assigned to the merger before its range is
    // checked, exactly as it always was. That is why TAIL must never be encoded
    // into that byte: a value this guard rejects still reaches every gate that
    // compares against it, and 0x80|ch matches no channel and is not 0xFF, so
    // it would refuse allow_any and allow_stop for all four channels at once.
    // ams_merger::restore is what makes the assignment safe now -- it rejects
    // an out-of-range owner into UNLOADED instead of storing it.
    //
    // The session is reconstructed identically because the retract needs it to
    // be. before_pull_back only becomes motion in bambu_bus_ams.cpp if the
    // channel's prior motion is on_use, before_on_use or stop_on_use, so a
    // restore that stopped short of on_use would leave the command accepted but
    // inert -- silent in a different place. What TAIL changes is the merger
    // state alone, and this makes no difference to Motion_control_is_reset_safe,
    // which already saw a restored LOADED session as not reset-safe for exactly
    // the same reason: filament[ch].motion is not idle.
    {
        uint8_t stored = 0xFFu;
        bool stored_tail = false;
        if (Flash_AMS_state_read(&stored, &stored_tail))
        {
            ams_merger::restore(g_merger, stored, stored_tail);
            const uint8_t ch = g_merger.owner;

            if (ch < 4u)
            {
                _ams* a = &ams[BAMBU_BUS_AMS_NUM];

                a->now_filament_num  = ch;
                a->filament_use_flag = 0x04;
                a->pressure          = 0x2B00;

                for (uint8_t i = 0; i < 4u; i++)
                    a->filament[i].motion = _filament_motion::idle;

                a->filament[ch].motion = _filament_motion::on_use;
            }
        }
    }

    Motion_control_init();
    bambubus_init();
    bus_init();

    DEBUG("START\n");

    while (1)
    {
        bus_uart1_tx_poll();
        bus_uart1_rx_poll();
        const ahubus_package_type   ahub_stu     = ahubus_run();
        const bambubus_package_type bambubus_stu = bambubus_run();
        bus_port_to_host.send_package();

        static int error = 0;

        if ((ahub_stu != ahubus_package_type::none) || (bambubus_stu != bambubus_package_type::none))
        {
            if ((ahub_stu != ahubus_package_type::error) || (bambubus_stu != bambubus_package_type::error))
            {
                error = 0;

                if (bambubus_stu == bambubus_package_type::heartbeat)
                {
                    SYS_RGB.set_RGB(0x38, 0x35, 0x32, 0);
                    bus_host_device_type = host_device_type_ams;
                }

                if (ahub_stu == ahubus_package_type::heartbeat)
                    bus_host_device_type = host_device_type_ahub;
            }
            else
            {
                error = -1;
                SYS_RGB.set_RGB(0x10, 0x00, 0x00, 0);
            }
        }

        Motion_control_run(error);
        bmcu_link_set_control_error(error);
        bmcu_link_service();
        RGB_update();
        if (!bmcu_link_reset_pending()) persistence_save_run();
    }
}
