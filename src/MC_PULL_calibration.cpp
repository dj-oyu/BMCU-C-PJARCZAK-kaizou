#include "MC_PULL_calibration.h"
#include "Motion_control.h"
#include "ADC_DMA.h"
#include "Flash_saves.h"
#include "hal/time_hw.h"
#include "Debug_log.h"
#include "app_api.h"
#include "bmcu_link.h"
#include "sensor_fixed.h"

using sensor_fixed::dmv;

extern void RGB_update();

namespace
{
struct CalibrationBusyGuard
{
    CalibrationBusyGuard() { bmcu_link_set_calibration_busy(true); }
    ~CalibrationBusyGuard() { bmcu_link_set_calibration_busy(false); }
};

void calibration_wait(uint32_t milliseconds)
{
    const uint32_t ticks_per_ms = time_hw_ticks_per_ms();
    const uint32_t started = time_ticks32();
    const uint32_t duration = milliseconds * ticks_per_ms;
    do
    {
        ADC_DMA_poll();
        bmcu_link_service();
        RGB_update();
        delay(1);
    }
    while (static_cast<uint32_t>(time_ticks32() - started) < duration);
}
}

// All voltages below are in 0.1 mV; see sensor_fixed.h.
static inline int32_t adc_pull_raw_ch(int ch, const int32_t *v8)
{
    switch (ch)
    {
    case 0: return v8[6];
    case 1: return v8[4];
    case 2: return v8[2];
    default:return v8[0];
    }
}

static inline int32_t adc_key_raw_ch(int ch, const int32_t *v8)
{
    switch (ch)
    {
    case 0: return v8[7];
    case 1: return v8[5];
    case 2: return v8[3];
    default:return v8[1];
    }
}

static inline int32_t adc_pull_v_centered(int ch)
{
    const int32_t *d = ADC_DMA_get_value();
    return adc_pull_raw_ch(ch, d) + MC_PULL_V_OFFSET[ch];
}

static inline int32_t cal_apply_polarity(int32_t v, int8_t pol)
{
    return (pol < 0) ? (sensor_fixed::kFullScale - v) : v;
}

static inline int32_t abs_dmv(int32_t v) { return v < 0 ? -v : v; }

static constexpr int32_t CAL_PRESS_DELTA_V = dmv(0.1);
static constexpr int32_t CAL_CENTER_EPS_V  = dmv(0.02);
static const uint32_t CAL_STABLE_MS     = 200;
static const uint32_t CAL_TIMEOUT_MS    = 30000;

static void blink_all(uint8_t r, uint8_t g, uint8_t b, int times = 4, int on_ms = 60, int off_ms = 60)
{
    for (int k = 0; k < times; k++)
    {
        for (int ch = 0; ch < 4; ch++) MC_PULL_ONLINE_RGB_set(ch, r, g, b);
        RGB_update(); calibration_wait(on_ms);
        for (int ch = 0; ch < 4; ch++) MC_PULL_ONLINE_RGB_set(ch, 0, 0, 0);
        RGB_update(); calibration_wait(off_ms);
    }
}

static void blink_one(int ch, uint8_t r, uint8_t g, uint8_t b, int times = 3, int on_ms = 70, int off_ms = 70)
{
    for (int k = 0; k < times; k++)
    {
        MC_PULL_ONLINE_RGB_set(ch, r, g, b);
        RGB_update(); calibration_wait(on_ms);
        MC_PULL_ONLINE_RGB_set(ch, 0, 0, 0);
        RGB_update(); calibration_wait(off_ms);
    }
}

static void show_diag_step(uint8_t r, uint8_t g, uint8_t b, int on_ms = 360, int off_ms = 180)
{
    for (int ch = 0; ch < 4; ch++) MC_PULL_ONLINE_RGB_set(ch, r, g, b);
    RGB_update();
    calibration_wait(on_ms);
    for (int ch = 0; ch < 4; ch++) MC_PULL_ONLINE_RGB_set(ch, 0, 0, 0);
    RGB_update();
    calibration_wait(off_ms);
}

static bool capture_first_extreme_wait_release(int ch, int32_t center_v, int32_t &out_best_norm, int8_t &out_pol)
{
    const uint32_t tpm = time_hw_ticks_per_ms();
    const uint32_t t0  = time_ticks32();
    const uint32_t dt  = (uint32_t)CAL_TIMEOUT_MS * tpm;

    bool pressed = false;
    int32_t raw_min = center_v;
    int32_t raw_max = center_v;
    uint32_t stable_t0 = 0u;

    out_best_norm = center_v;
    out_pol = 1;

    for (;;)
    {
        const uint32_t now_t = time_ticks32();
        if ((uint32_t)(now_t - t0) >= dt) break;

        const int32_t v = adc_pull_v_centered(ch);

        const uint32_t elapsed_ms = (uint32_t)((now_t - t0) / tpm);
        if (((elapsed_ms / 200u) & 1u) == 0u) MC_PULL_ONLINE_RGB_set(ch, 0x00, 0x00, 0x10);
        else                                  MC_PULL_ONLINE_RGB_set(ch, 0x00, 0x00, 0x00);
        RGB_update();

        if (!pressed)
        {
            if (abs_dmv(v - center_v) >= CAL_PRESS_DELTA_V)
            {
                pressed = true;
                raw_min = v;
                raw_max = v;
            }
        }
        else
        {
            if (v < raw_min) raw_min = v;
            if (v > raw_max) raw_max = v;

            if (abs_dmv(v - center_v) <= CAL_CENTER_EPS_V)
            {
                if (stable_t0 == 0u) stable_t0 = now_t;
                if ((uint32_t)(now_t - stable_t0) >= (uint32_t)CAL_STABLE_MS * tpm)
                {
                    const int32_t dmin = center_v - raw_min;
                    const int32_t dmax = raw_max - center_v;

                    if (dmax > dmin)
                    {
                        out_pol = -1;
                        out_best_norm = cal_apply_polarity(raw_max, out_pol);
                    }
                    else
                    {
                        out_pol = 1;
                        out_best_norm = cal_apply_polarity(raw_min, out_pol);
                    }

                    MC_PULL_ONLINE_RGB_set(ch, 0, 0, 0);
                    RGB_update();
                    return true;
                }
            }
            else
            {
                stable_t0 = 0u;
            }
        }

        calibration_wait(15u);
    }

    out_best_norm = center_v;
    out_pol = 1;
    MC_PULL_ONLINE_RGB_set(ch, 0, 0, 0);
    RGB_update();
    return false;
}

static bool capture_second_extreme_wait_release(int ch, int32_t center_v, int8_t pol, int32_t &out_best_norm)
{
    const uint32_t tpm = time_hw_ticks_per_ms();
    const uint32_t t0  = time_ticks32();
    const uint32_t dt  = (uint32_t)CAL_TIMEOUT_MS * tpm;

    const bool want_raw_min = (pol < 0);

    bool pressed = false;
    int32_t best_raw = center_v;
    uint32_t stable_t0 = 0u;

    for (;;)
    {
        const uint32_t now_t = time_ticks32();
        if ((uint32_t)(now_t - t0) >= dt) break;

        const int32_t v = adc_pull_v_centered(ch);

        const uint32_t elapsed_ms = (uint32_t)((now_t - t0) / tpm);
        if (((elapsed_ms / 200u) & 1u) == 0u) MC_PULL_ONLINE_RGB_set(ch, 0x10, 0x00, 0x00);
        else                                  MC_PULL_ONLINE_RGB_set(ch, 0x00, 0x00, 0x00);
        RGB_update();

        if (!pressed)
        {
            if (want_raw_min)
            {
                if (v < (center_v - CAL_PRESS_DELTA_V))
                {
                    pressed = true;
                    best_raw = v;
                }
            }
            else
            {
                if (v > (center_v + CAL_PRESS_DELTA_V))
                {
                    pressed = true;
                    best_raw = v;
                }
            }
        }
        else
        {
            if (want_raw_min)
            {
                if (v < best_raw) best_raw = v;
            }
            else
            {
                if (v > best_raw) best_raw = v;
            }

            if (abs_dmv(v - center_v) <= CAL_CENTER_EPS_V)
            {
                if (stable_t0 == 0u) stable_t0 = now_t;
                if ((uint32_t)(now_t - stable_t0) >= (uint32_t)CAL_STABLE_MS * tpm)
                {
                    out_best_norm = cal_apply_polarity(best_raw, pol);
                    MC_PULL_ONLINE_RGB_set(ch, 0, 0, 0);
                    RGB_update();
                    return true;
                }
            }
            else
            {
                stable_t0 = 0u;
            }
        }

        calibration_wait(15u);
    }

    out_best_norm = center_v;
    MC_PULL_ONLINE_RGB_set(ch, 0, 0, 0);
    RGB_update();
    return false;
}

static void capture_minmax_one_ch_event(int ch, int32_t center_v, int32_t &out_min, int32_t &out_max, int8_t &out_pol)
{
    int32_t vmin = center_v;
    int32_t vmax = center_v;
    int8_t pol = 1;

    bool ok_min = capture_first_extreme_wait_release(ch, center_v, vmin, pol);
    if (ok_min) blink_one(ch, 0x10, 0x10, 0x00, 2, 60, 60);

    bool ok_max = capture_second_extreme_wait_release(ch, center_v, pol, vmax);
    if (ok_max) blink_one(ch, 0x10, 0x10, 0x00, 2, 60, 60);

    if (vmin > (center_v - dmv(0.050))) vmin = (center_v - dmv(0.050));
    if (vmax < (center_v + dmv(0.050))) vmax = (center_v + dmv(0.050));
    if (vmax <= vmin + dmv(0.10)) { vmin = center_v - dmv(0.10); vmax = center_v + dmv(0.10); }

    out_min = vmin;
    out_max = vmax;
    out_pol = pol;
}

void MC_PULL_calibration_clear()
{
    Flash_NVM_full_clear();
}

void MC_PULL_calibration_boot()
{
    CalibrationBusyGuard busy_guard;
    for (int i = 0; i < 6; i++) calibration_wait(20u);

    MC_PULL_detect_channels_inserted();

    int32_t offs[4], vmin[4], vmax[4];
    int8_t pol[4];
    if (Flash_MC_PULL_cal_read(offs, vmin, vmax, pol))
    {
        for (int ch = 0; ch < 4; ch++)
        {
            MC_PULL_V_OFFSET[ch] = offs[ch];
            MC_PULL_V_MIN[ch]    = vmin[ch];
            MC_PULL_V_MAX[ch]    = vmax[ch];
            MC_PULL_POLARITY[ch] = (pol[ch] < 0) ? -1 : 1;
        }
        return;
    }

    const bool ok_wipe = Flash_NVM_full_clear();

    // 90 samples of at most 33000 each stay far inside int32.
    int32_t sum_raw[4] = {0, 0, 0, 0};
    int32_t sum_key[4] = {0, 0, 0, 0};
    const int N = 90;
    const uint32_t tpm = time_hw_ticks_per_ms();
    const uint32_t t0  = time_ticks32();

    for (int k = 0; k < N; k++)
    {
        const int32_t *v = ADC_DMA_get_value();

        for (int ch = 0; ch < 4; ch++)
        {
            if (!filament_channel_inserted[ch]) continue;
            sum_raw[ch] += adc_pull_raw_ch(ch, v);
            sum_key[ch] += adc_key_raw_ch(ch, v);
        }

        const uint32_t now_t = time_ticks32();
        const uint32_t elapsed_ms = (uint32_t)((now_t - t0) / tpm);
        bool on = (((elapsed_ms / 200u) & 1u) == 0u);
        for (int ch = 0; ch < 4; ch++)
            MC_PULL_ONLINE_RGB_set(ch, on ? 0x10 : 0, on ? 0x10 : 0, 0x00);
        RGB_update();
        calibration_wait(15u);
    }

    int32_t center_raw[4] = {sensor_fixed::kCenter, sensor_fixed::kCenter,
                             sensor_fixed::kCenter, sensor_fixed::kCenter};
    for (int ch = 0; ch < 4; ch++)
    {
        if (!filament_channel_inserted[ch]) continue;
        center_raw[ch] = (sum_raw[ch] + N / 2) / N;
    }

    for (int ch = 0; ch < 4; ch++)
    {
        if (!filament_channel_inserted[ch])
        {
            MC_PULL_V_OFFSET[ch] = 0;
            MC_PULL_POLARITY[ch] = 1;
            MC_DM_KEY_NONE_THRESH[ch] = dmv(0.60);
            continue;
        }

        MC_PULL_V_OFFSET[ch] = sensor_fixed::kCenter - center_raw[ch];
        MC_PULL_POLARITY[ch] = 1;
        MC_DM_KEY_NONE_THRESH[ch] =
            sensor_fixed::none_threshold_from_idle((sum_key[ch] + N / 2) / N);
    }

    int32_t center_v_ref[4] = {sensor_fixed::kCenter, sensor_fixed::kCenter,
                               sensor_fixed::kCenter, sensor_fixed::kCenter};
    for (int ch = 0; ch < 4; ch++)
    {
        if (!filament_channel_inserted[ch]) continue;
        center_v_ref[ch] = center_raw[ch] + MC_PULL_V_OFFSET[ch];
    }

    blink_all(0x10, 0x10, 0x00, 3, 60, 60);

    for (int ch = 0; ch < 4; ch++)
    {
        if (!filament_channel_inserted[ch])
        {
            MC_PULL_V_MIN[ch] = dmv(1.55);
            MC_PULL_V_MAX[ch] = dmv(1.75);
            MC_PULL_POLARITY[ch] = 1;
            continue;
        }

        int32_t mn, mx;
        int8_t p;
        capture_minmax_one_ch_event(ch, center_v_ref[ch], mn, mx, p);

        MC_PULL_V_MIN[ch] = mn;
        MC_PULL_V_MAX[ch] = mx;
        MC_PULL_POLARITY[ch] = (p < 0) ? -1 : 1;

        MC_PULL_ONLINE_RGB_set(ch, 0, 0, 0);
        RGB_update();
        calibration_wait(80u);
    }

    const bool ok_cal = Flash_MC_PULL_cal_write_all(MC_PULL_V_OFFSET, MC_PULL_V_MIN, MC_PULL_V_MAX, MC_PULL_POLARITY);
    const bool ok_mot = Motion_control_save_dm_key_none_thresholds();
    const bool ok = ok_wipe && ok_cal && ok_mot;

    if (ok_wipe) show_diag_step(0x00, 0x10, 0x10);
    else         show_diag_step(0x10, 0x00, 0x00, 460, 220);

    if (ok_cal) show_diag_step(0x10, 0x10, 0x00);
    else        show_diag_step(0x10, 0x00, 0x10, 460, 220);

    if (ok_mot) show_diag_step(0x00, 0x00, 0x10);
    else        show_diag_step(0x10, 0x10, 0x10, 460, 220);

    if (ok) blink_all(0x00, 0x10, 0x00, 2, 220, 220);
    else    blink_all(0x10, 0x00, 0x00, 2, 260, 260);

    calibration_wait(200u);
}