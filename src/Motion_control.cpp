#include "Motion_control.h"
#include "ams.h"
#include "ADC_DMA.h"
#include "Flash_saves.h"
#include "_bus_hardware.h"
#include "many_soft_AS5600.h"
#include "app_api.h"
#include "hal/time_hw.h"
#include "bmcu_link.h"
#include "ams_loaded_latch_policy.h"
#include "signal_hold_policy.h"

static inline uint8_t bmcu_pressure_class(uint16_t pressure)
{
    if (pressure == 0xF06Fu) return 2u;
    if (pressure == 0xFFFFu || pressure == 0xFF74u) return 0u;
    return 1u;
}

static uint8_t g_bmcu_reported_pressure_class = 0xFFu;

static inline void bmcu_set_pressure(_ams& state, uint16_t pressure)
{
    state.pressure = pressure;
}

static inline uint8_t inserted_mask()
{
    uint8_t mask = 0u;
    for (uint8_t ch = 0u; ch < 4u; ++ch)
        if (filament_channel_inserted[ch]) mask |= static_cast<uint8_t>(1u << ch);
    return mask;
}

static inline float absf(float x) { return (x < 0.0f) ? -x : x; }
static inline float clampf(float x, float a, float b)
{
    if (x < a) return a;
    if (x > b) return b;
    return x;
}

static inline uint8_t dm_key_v_to_centi_ceil(float v)
{
    if (v <= 0.0f) return 0u;

    float x = v * 100.0f - 0.0001f;
    int iv = (int)x;
    if ((float)iv < x) iv++;

    if (iv < 0) iv = 0;
    if (iv > 255) iv = 255;
    return (uint8_t)iv;
}

static inline float dm_key_centi_to_v(uint8_t cv)
{
    return 0.01f * (float)cv;
}

static uint32_t g_time_last_ticks32 = 0u;
static uint32_t g_time_rem_ticks32 = 0u;
static uint32_t g_time_ms32 = 0u;
static uint32_t g_time_tpm_last = 0u;
static uint8_t g_time_inited = 0u;

static inline __attribute__((always_inline)) uint32_t
time_ms_fast_from_ticks32(uint32_t now_ticks)
{
    uint32_t tpm = time_hw_tpms;
    if (tpm == 0u) tpm = 1u;

    if (!g_time_inited || tpm != g_time_tpm_last)
    {
        g_time_inited = 1u;
        g_time_tpm_last = tpm;
        g_time_last_ticks32 = now_ticks;
        g_time_ms32 = now_ticks / tpm;
        g_time_rem_ticks32 = now_ticks - g_time_ms32 * tpm;
        return g_time_ms32;
    }

    const uint32_t dt = now_ticks - g_time_last_ticks32;
    g_time_last_ticks32 = now_ticks;

    if (tpm == 1u)
    {
        g_time_ms32 += dt;
        return g_time_ms32;
    }

    const uint32_t whole_ms = dt / tpm;
    uint32_t remainder = g_time_rem_ticks32 + (dt - whole_ms * tpm);
    uint32_t increment = whole_ms;
    if (remainder >= tpm)
    {
        remainder -= tpm;
        ++increment;
    }

    g_time_rem_ticks32 = remainder;
    g_time_ms32 += increment;
    return g_time_ms32;
}

static inline __attribute__((always_inline)) uint32_t time_ms_fast(void)
{
    return time_ms_fast_from_ticks32(time_ticks32());
}

static inline int32_t retract_mag_from_err_q(int32_t err_q, int32_t mag_max)
{
    constexpr int32_t e0_q = 10;
    constexpr int32_t e1_q = 35;
    constexpr int32_t e2_q = 235;
    if (err_q <= e0_q) return 0;

    int32_t mag;
    if (err_q < e1_q)
        mag = 450 + 4 * (err_q - e0_q);
    else
    {
        int32_t span_q = err_q - e1_q;
        if (span_q > e2_q - e1_q) span_q = e2_q - e1_q;
        mag = 550 + ((3 * span_q) >> 1);
    }
    return mag > mag_max ? mag_max : mag;
}

static inline uint8_t hyst_q(uint8_t active, int32_t value_q,
                             int32_t start_q, int32_t stop_q)
{
    if (active)
    {
        if (value_q <= stop_q) active = 0u;
    }
    else if (value_q >= start_q)
    {
        active = 1u;
    }
    return active;
}


static constexpr uint8_t  kChCount = 4;
static constexpr int      PWM_lim  = 1000;
static constexpr float    kAS5600_PI = 3.14159265358979323846f;

// stała do przeliczenia AS5600 - liczona raz
static constexpr float kAS5600_MM_PER_CNT = -(kAS5600_PI * 7.5f) / 4096.0f;
static constexpr float kAS5600_COUNTS_PER_M = 1000.0f / (-kAS5600_MM_PER_CNT);
static constexpr float kAS5600_M_PER_CNT = (-kAS5600_MM_PER_CNT) * 0.001f;

static constexpr int32_t distance_m_to_counts(float meters)
{
    return static_cast<int32_t>(meters * kAS5600_COUNTS_PER_M + 0.5f);
}

static inline uint32_t delta_magnitude_counts(int32_t delta)
{
    const int32_t mask = delta >> 31;
    return static_cast<uint32_t>((delta ^ mask) - mask);
}

static inline __attribute__((always_inline)) int32_t
apply_integer_sign_mask(int32_t value, int32_t negate_mask)
{
    return (value ^ negate_mask) - negate_mask;
}

static constexpr int32_t motor_polarity_to_negate_mask(int polarity)
{
    return polarity < 0 ? -1 : 0;
}

static inline int32_t abs_i32(int32_t value)
{
    const int32_t mask = value >> 31;
    return (value ^ mask) - mask;
}

static constexpr int32_t motion_progress_negate_mask(bool send_out)
{
    return send_out ? -1 : 0;
}

// ===== AS5600 =====
AS5600_soft_IIC_many MC_AS5600;
static GPIO_TypeDef* const AS5600_SCL_PORT[4] = { GPIOB, GPIOB, GPIOB, GPIOB };
static const uint16_t      AS5600_SCL_PIN [4] = { GPIO_Pin_15, GPIO_Pin_14, GPIO_Pin_13, GPIO_Pin_12 };
static GPIO_TypeDef* const AS5600_SDA_PORT[4] = { GPIOD, GPIOC, GPIOC, GPIOC };
static const uint16_t      AS5600_SDA_PIN [4] = { GPIO_Pin_0, GPIO_Pin_15, GPIO_Pin_14, GPIO_Pin_13 };

// AS5600 speed in 0.001 mm/s. Integer form keeps the control hot path
// away from software floating-point arithmetic.
static int32_t speed_as5600_q[4] = {0, 0, 0, 0};
// ===== AS5600 health gate (anti-runaway) =====
static uint8_t g_as5600_good[4]     = {0,0,0,0};
static uint8_t g_as5600_fail[4]     = {0,0,0,0};
static uint8_t g_as5600_okstreak[4] = {0,0,0,0};
static int16_t g_as5600_last_delta[4] = {0,0,0,0};
static int32_t g_filament_position_counts[4] = {0,0,0,0};
static int16_t g_motor_pwm[4] = {0,0,0,0};
static constexpr uint8_t kAS5600_FAIL_TRIP   = 3;
static constexpr uint8_t kAS5600_OK_RECOVER  = 2;
static inline bool AS5600_is_good(uint8_t ch) { return g_as5600_good[ch] != 0; }

// Preconditions: hi >= lo and all operands stay within the calibrated sensor range.
static inline __attribute__((always_inline)) bool in_closed_range_i32(
    int32_t value, int32_t lo, int32_t hi)
{
    return static_cast<uint32_t>(value - lo) <=
           static_cast<uint32_t>(hi - lo);
}

// ---- liniowe zwalnianie końcówki + minimalny PWM ----
static constexpr float PULL_RAMP_M = 0.015f; // 15 mm braking zone
static constexpr int32_t PULL_PWM_MIN = 400; // minimum pull-back PWM
static constexpr int32_t PULL_V_FAST_Q = 60000; // 0.001 mm/s
static constexpr int32_t PULL_V_END_Q  = 12000; // 0.001 mm/s

static int32_t g_pull_remain_counts[4] = {0,0,0,0};
static int32_t g_pull_speed_set_q[4] = {
    -PULL_V_FAST_Q, -PULL_V_FAST_Q, -PULL_V_FAST_Q, -PULL_V_FAST_Q
};
static int16_t g_pull_pwm_floor[4] = {500,500,500,500};

float MC_PULL_V_OFFSET[4]      = {0.0f, 0.0f, 0.0f, 0.0f};
float MC_PULL_V_MIN[4]         = {1.00f, 1.00f, 1.00f, 1.00f};
float MC_PULL_V_MAX[4]         = {2.00f, 2.00f, 2.00f, 2.00f};
int8_t MC_PULL_POLARITY[4]     = {1, 1, 1, 1};
float MC_DM_KEY_NONE_THRESH[4] = {0.60f, 0.60f, 0.60f, 0.60f};

uint8_t MC_PULL_pct[4]        = {50, 50, 50, 50};
static int16_t MC_PULL_pct_q[4] = {5000, 5000, 5000, 5000}; // 0.01 percent

static float  MC_PULL_stu_raw[4]        = {1.65f, 1.65f, 1.65f, 1.65f};
static int8_t MC_PULL_stu[4]            = {0, 0, 0, 0};
static float  g_pull_low_scale[4]       = {0,0,0,0};
static float  g_pull_high_scale[4]      = {0,0,0,0};
static float  g_pull_cached_vmin[4]     = {0,0,0,0};
static float  g_pull_cached_vmax[4]     = {0,0,0,0};
static uint8_t g_pull_scale_valid_mask  = 0u;

static uint8_t  MC_ONLINE_key_stu[4]    = {0, 0, 0, 0};
static uint8_t  g_on_use_low_latch[4]   = {0, 0, 0, 0};   // 1=stop motor latch
static uint8_t  g_on_use_jam_latch[4]   = {0, 0, 0, 0};   // 1=real jam -> 0xF06F
static uint32_t g_on_use_hi_pwm_us[4]   = {0u, 0u, 0u, 0u};

static inline __attribute__((always_inline)) void MC_STU_RGB_set_latch(
    uint8_t ch, uint8_t r, uint8_t g, uint8_t b, uint32_t now_ms, uint8_t blink)
{
    if (!g_on_use_low_latch[ch]) { MC_STU_RGB_set(ch, r, g, b); return; }

    if (!blink || (((now_ms / 1000u) & 1u) != 0u))
        MC_STU_RGB_set(ch, 0xFFu, 0x00u, 0x00u);
    else
        MC_STU_RGB_set(ch, r, g, b);
}

#if BMCU_DM_TWO_MICROSWITCH
static inline uint8_t dm_key_to_state(uint8_t ch, float v)
{
    const float none_thr = MC_DM_KEY_NONE_THRESH[ch];

    if (v < none_thr) return 0u;   // none
    if (v > 1.7f)     return 1u;   // both
    if (v > 1.4f)     return 2u;   // external only
    return 3u;
}

// ---- DM autoload (two microswitch) ----
static constexpr uint32_t DM_AUTO_S1_DEBOUNCE_MS       = 100u;   // 0.1s
static constexpr uint32_t DM_AUTO_S1_TIMEOUT_MS        = 5000u;  // 5s
static constexpr uint32_t DM_AUTO_S1_FAIL_RETRACT_MS   = 1500u;  // 1.5s

static constexpr int32_t  DM_AUTO_S2_TARGET_COUNTS     = distance_m_to_counts(0.120f); // 120mm
static constexpr float    DM_AUTO_BUF_ABORT_PCT        = 75.0f;    // abort push
static constexpr float    DM_AUTO_BUF_RECOVER_PCT      = 50.2f;    // retract-to (try 1/2)
static constexpr uint32_t DM_AUTO_FAIL_EXTRA_MS        = 1500u;  // extra retract after fail
static constexpr int32_t  DM_AUTO_PWM_PUSH             = 900;   // push strength
static constexpr int32_t  DM_AUTO_PWM_PULL             = 900;   // retract strength
static constexpr int32_t  DM_AUTO_IDLE_LIM             = 950;   // clamp only during autoload

enum : uint8_t
{
    DM_AUTO_IDLE = 0,
    DM_AUTO_S1_DEBOUNCE,
    DM_AUTO_S1_PUSH,
    DM_AUTO_S1_FAIL_RETRACT,
    DM_AUTO_S2_PUSH,
    DM_AUTO_S2_RETRACT,
    DM_AUTO_S2_FAIL_RETRACT,
    DM_AUTO_S2_FAIL_EXTRA,
};

static uint8_t  dm_loaded[4]            = {1,1,1,1};   // 1=loaded (after stage2 success)
static uint8_t  dm_fail_latch[4]        = {0,0,0,0};   // latch until ks==0 (<0.6V)
static uint8_t  dm_auto_state[4]        = {0,0,0,0};
static uint8_t  dm_autoload_gate[4]     = {0,0,0,0}; // 0=allow Stage1, 1=block Stage1 until idle+ks==0
static uint8_t  dm_auto_try[4]          = {0,0,0,0};   // abort count (stage2)
static uint32_t dm_auto_t0_ms[4] = {0u,0u,0u,0u};
static int32_t  dm_auto_remain_counts[4] = {0,0,0,0};
static int32_t  dm_auto_last_counts[4]   = {0,0,0,0};

// How long the key must read anything other than "both switches" before a
// confirmed DM load is given up. This was an unnamed 100 at its one use site;
// it is the same number as DM_AUTO_S1_DEBOUNCE_MS but a different question --
// that one times a state the autoload machine is sitting in, this one times a
// level on the key -- so they are named apart rather than shared.
static constexpr uint32_t DM_LOADED_DROP_MS = 100u;
static uint32_t dm_loaded_drop_t0_ms[4] = {0u,0u,0u,0u};
#endif

// How long the owning channel's online key must read empty, continuously,
// before Motion_control_run moves the merger from LOADED to TAIL. See
// src/ams_merger_policy.h for the states and src/ams_loaded_latch_policy.h for
// the debounce itself.
//
// On the sampling rate this debounce is measured against: MC_PULL_ONLINE_read
// runs once per Motion_control_run, which is once per main-loop pass, but the
// key signal underneath it is much slower than the loop. ADCCLK is PCLK2/8 =
// 18 MHz, a conversion is 71.5 + 12.5 = 84 cycles, and the scan is 8 channels,
// so one scan takes 37.3 us; the DMA half-buffer holds 32 scans and therefore
// completes every ~1.2 ms, and ADC_DMA_get_value hands back a boxcar over the
// last four of those (~4.8 ms). Passes falling inside one of those windows read
// a byte-identical key. So the smallest transient that can reach this code is
// already several milliseconds wide, and the window below is resolved to about
// 1.2 ms regardless of how fast the loop happens to run.
//
// 1500 ms is AUTO_UNLOAD_EMPTY_MS, which this firmware already uses to answer
// the same physical question off the same switch: how long must the online key
// read empty before the filament is really out of the channel. Reusing that
// number keeps one definition of "really gone" rather than introducing a second
// one that would drift from it. It is named separately because the two uses are
// independent -- auto-unload may retune without dragging the latch with it.
//
// Both directions of a wrong window are now cheap, which they were not before
// TAIL existed. Erring long delays the wire bit and nothing else, because a
// printer command arriving mid-window is helped rather than hurt: allow_stop is
// true throughout LOADED, and those branches release the merger themselves,
// immediately and outside this debounce. Erring short no longer releases
// anything -- it enters TAIL, where allow_stop is still true and the retract is
// still accepted. What used to be the expensive direction is now a mislabelled
// wire bit for as long as the merger stays held.
//
// On why there is no timeout from TAIL back to UNLOADED. Any finite one is this
// same defect with a longer fuse: the pre-change firmware released outright at
// this window, which is a 1500 ms timeout, and the bug is precisely that the
// printer's retract had not arrived yet. Sizing a timeout means bounding how
// long the printer may take, and on the runout that motivates all of this the
// printer pauses and can wait for a human, so there is no bound to pick. A
// number large enough to be safe is not a backstop, and a number small enough
// to be a backstop is the bug.
//
// The stale-lock worry a permanent TAIL raises is real but is not the failure
// it replaces. The old stale latch was unrecoverable in practice: the printer
// believed the channel loaded, so it would never send the load that re-latches
// it. TAIL is released by every printer command that touches the merger, and in
// particular by send_out, which opens every load and is accepted
// unconditionally -- so a TAIL held wrongly costs a mislabelled bit 5 and 6, and
// the refusal of a bare before_on_use or on_use for a *different* channel that
// arrives without its send_out. The next load clears it. That escape exists;
// the old one did not.
static constexpr uint32_t LOADED_LATCH_DROP_MS = 1500u;
static ams_loaded_latch::State g_loaded_latch_drop = { ams_loaded_latch::kNoChannel, 0u };

static constexpr float    AUTO_UNLOAD_START_PCT      = 80.0f;
static constexpr float    AUTO_UNLOAD_NEUTRAL_LO_PCT = 45.0f;
static constexpr float    AUTO_UNLOAD_NEUTRAL_HI_PCT = 55.0f;
static constexpr float    AUTO_UNLOAD_ABORT_PCT      = 35.0f;
static constexpr uint32_t AUTO_UNLOAD_ARM_MS = 1000u;
static constexpr uint32_t AUTO_UNLOAD_MAX_MS = 15000u;
static constexpr uint32_t AUTO_UNLOAD_EMPTY_MS = 1500u;
static constexpr int32_t  AUTO_UNLOAD_PWM_PULL = 850;

static uint8_t  auto_unload_arm[4]          = {0,0,0,0};
static uint8_t  auto_unload_active[4]       = {0,0,0,0};
static uint8_t  auto_unload_blocked[4]      = {0,0,0,0};
static uint32_t auto_unload_arm_t0_ms[4] = {0u,0u,0u,0u};
static uint32_t auto_unload_active_t0_ms[4] = {0u,0u,0u,0u};
static uint32_t auto_unload_empty_t0_ms[4] = {0u,0u,0u,0u};

bool filament_channel_inserted[4]       = {false, false, false, false}; // czy kanał fizycznie wpięty



static constexpr int MC_PULL_DEADBAND_PCT_LOW  = 30;
static constexpr int MC_PULL_DEADBAND_PCT_HIGH = 70;

// ================ LOAD CONTROL ======================
#if BMCU_SOFT_LOAD
    // Stage1
    static constexpr int   MC_LOAD_S1_FAST_PCT       = 75;
    static constexpr int   MC_LOAD_S1_HARD_STOP_PCT  = 90;  // bezpiecznik
    static constexpr int   MC_LOAD_S1_HARD_HYS       = 2;   // wróć dopiero < (HARD_STOP - HYS)
    // Stage2 (hold_load)
    static constexpr float MC_LOAD_S2_HOLD_TARGET_PCT    = 75.0f;
    static constexpr float MC_LOAD_S2_HOLD_BAND_LO_DELTA = 0.3f;   // push_hi = hold_target - delta
    static constexpr float MC_LOAD_S2_PUSH_START_PCT     = 55.0f;  // start push PWM
    static constexpr float MC_LOAD_S2_PWM_HI             = 480.0f;
    static constexpr float MC_LOAD_S2_PWM_LO             = 1000.0f;
    // ===== ON_USE CONTROL =====
    static constexpr float MC_ON_USE_TARGET_PCT    = 52.0f;
    static constexpr float MC_ON_USE_BAND_LO_DELTA = 0.2f;  // band_lo = target - delta
    static constexpr float MC_ON_USE_BAND_HI_PCT   = 60.0f;
#elif BMCU_P1S  // P1S
    // Stage1
    static constexpr int   MC_LOAD_S1_FAST_PCT       = 88;
    static constexpr int   MC_LOAD_S1_HARD_STOP_PCT  = 97;  // bezpiecznik
    static constexpr int   MC_LOAD_S1_HARD_HYS       = 2;   // wróć dopiero < (HARD_STOP - HYS)
    // Stage2 (hold_load)
    static constexpr float MC_LOAD_S2_HOLD_TARGET_PCT    = 95.0f;
    static constexpr float MC_LOAD_S2_HOLD_BAND_LO_DELTA = 1.0f;   // push_hi = hold_target - delta
    static constexpr float MC_LOAD_S2_PUSH_START_PCT     = 88.0f;  // start push PWM
    static constexpr float MC_LOAD_S2_PWM_HI             = 550.0f;
    static constexpr float MC_LOAD_S2_PWM_LO             = 1000.0f;
    // ===== ON_USE CONTROL =====
    static constexpr float MC_ON_USE_TARGET_PCT    = 54.0f;
    static constexpr float MC_ON_USE_BAND_LO_DELTA = 0.2f;  // band_lo = target - delta
    static constexpr float MC_ON_USE_BAND_HI_PCT   = 65.0f;
#else        // A1
    // Stage1
    static constexpr int   MC_LOAD_S1_FAST_PCT       = 85;
    static constexpr int   MC_LOAD_S1_HARD_STOP_PCT  = 95;  // bezpiecznik
    static constexpr int   MC_LOAD_S1_HARD_HYS       = 2;   // wróć dopiero < (HARD_STOP - HYS)
    // Stage2 (hold_load)
    static constexpr float MC_LOAD_S2_HOLD_TARGET_PCT    = 90.0f;
    static constexpr float MC_LOAD_S2_HOLD_BAND_LO_DELTA = 0.3f;   // push_hi = hold_target - delta
    static constexpr float MC_LOAD_S2_PUSH_START_PCT     = 80.0f;  // start push PWM
    static constexpr float MC_LOAD_S2_PWM_HI             = 480.0f;
    static constexpr float MC_LOAD_S2_PWM_LO             = 1000.0f;
    // ===== ON_USE CONTROL =====
    static constexpr float MC_ON_USE_TARGET_PCT    = 52.0f;
    static constexpr float MC_ON_USE_BAND_LO_DELTA = 0.2f;  // band_lo = target - delta
    static constexpr float MC_ON_USE_BAND_HI_PCT   = 60.0f;
#endif
// ====================================================

static constexpr uint32_t CAL_RESET_HOLD_MS     = 5000;
static constexpr int      CAL_RESET_PCT_THRESH  = 15;
static constexpr float    CAL_RESET_V_DELTA     = 0.10f;
static constexpr float    CAL_RESET_NEAR_MIN    = 0.03f;

static int      g_hold_ch = -1;
static uint32_t g_hold_t0_ticks = 0;

// kiedy kanał OSTATNIO wyszedł z on_use (0 = nigdy, 1 = marker "był kiedykolwiek") (patch do wersji BMCU DM przy automatycznej zmianie filamentu gdy się skończy, żeby ekstruder nie trzymał filamentu)
static uint32_t g_last_on_use_exit_ms[4] = {0u,0u,0u,0u};

extern void RGB_update();

static inline bool all_no_filament()
{
    return ((MC_ONLINE_key_stu[0] | MC_ONLINE_key_stu[1] | MC_ONLINE_key_stu[2] | MC_ONLINE_key_stu[3]) == 0);
}

static void blink_all_blue_3s()
{
    const uint32_t tpm = time_hw_ticks_per_ms();
    const uint32_t t0  = time_ticks32();
    const uint32_t dt  = 3000u * tpm;

    while ((uint32_t)(time_ticks32() - t0) < dt)
    {
        const uint32_t now_t = time_ticks32();
        const uint32_t elapsed_ms = (uint32_t)((now_t - t0) / tpm);

        const bool on = (((elapsed_ms / 150u) & 1u) == 0u);
        for (uint8_t ch = 0; ch < kChCount; ch++)
            MC_PULL_ONLINE_RGB_set(ch, 0, 0, on ? 0x10 : 0);

        RGB_update();
        delay(20);
    }

    for (uint8_t ch = 0; ch < kChCount; ch++)
        MC_PULL_ONLINE_RGB_set(ch, 0, 0, 0);
    RGB_update();
}

static __attribute__((noinline, cold)) void calibration_reset_and_reboot()
{
    for (uint8_t i = 0; i < kChCount; i++) Motion_control_set_PWM(i, 0);

    blink_all_blue_3s();

    Flash_NVM_full_clear();

    NVIC_SystemReset();
}

static float pull_v_to_percent_f(uint8_t ch, float v)
{
    constexpr float c = 1.65f;

    float vmin = MC_PULL_V_MIN[ch];
    float vmax = MC_PULL_V_MAX[ch];

    if (vmin > 1.60f) vmin = 1.60f;
    if (vmax < 1.70f) vmax = 1.70f;
    if (vmax <= (vmin + 0.10f)) { vmin = 1.55f; vmax = 1.75f; }

    const uint8_t bit = static_cast<uint8_t>(1u << ch);
    if ((g_pull_scale_valid_mask & bit) == 0u ||
        g_pull_cached_vmin[ch] != vmin || g_pull_cached_vmax[ch] != vmax)
    {
        float low_den = c - vmin;
        float high_den = vmax - c;
        if (low_den < 0.05f) low_den = 0.05f;
        if (high_den < 0.05f) high_den = 0.05f;
        g_pull_low_scale[ch] = 0.5f / low_den;
        g_pull_high_scale[ch] = 0.5f / high_den;
        g_pull_cached_vmin[ch] = vmin;
        g_pull_cached_vmax[ch] = vmax;
        g_pull_scale_valid_mask |= bit;
    }

    if (v <= c)
        return clampf((v - vmin) * g_pull_low_scale[ch], 0.0f, 1.0f) * 100.0f;

    return clampf(0.5f + (v - c) * g_pull_high_scale[ch], 0.0f, 1.0f) * 100.0f;
}

static inline float pull_v_apply_polarity(uint8_t ch, float v)
{
    if (MC_PULL_POLARITY[ch] < 0) return 3.30f - v;
    return v;
}

void MC_PULL_detect_channels_inserted()
{
    const uint8_t before_mask = inserted_mask();
    if (!ADC_DMA_is_inited())
    {
        for (uint8_t ch = 0; ch < kChCount; ch++) filament_channel_inserted[ch] = false;
        if (before_mask != inserted_mask())
            bmcu_link_status_changed(BMCU_STATUS_CHANGE_INSERTED);
        return;
    }

    ADC_DMA_gpio_analog();
    ADC_DMA_filter_reset();
    (void)ADC_DMA_wait_full();

    constexpr uint8_t idx[kChCount] = {6,4,2,0};
    constexpr int N = 16;
    float s[kChCount] = {0,0,0,0};

    for (int i = 0; i < N; i++)
    {
        const float *v = ADC_DMA_get_value();
        for (uint8_t ch = 0; ch < kChCount; ch++) s[ch] += v[idx[ch]];
        delay(2);
    }

    constexpr float VMIN = 0.30f;
    constexpr float VMAX = 3.00f;
    constexpr float invN = 1.0f / (float)N;

    for (uint8_t ch = 0; ch < kChCount; ch++)
    {
        const float a = s[ch] * invN;
        filament_channel_inserted[ch] = (a > VMIN) && (a < VMAX);
    }
    if (before_mask != inserted_mask())
        bmcu_link_status_changed(BMCU_STATUS_CHANGE_INSERTED);
}

static inline void MC_PULL_ONLINE_init()
{
    MC_PULL_detect_channels_inserted();
}

static inline void MC_PULL_ONLINE_read(uint32_t now_ticks)
{
    const float *data = ADC_DMA_get_value();

    // mapowanie ADC -> kanały
    MC_PULL_stu_raw[3] = pull_v_apply_polarity(3u, data[0] + MC_PULL_V_OFFSET[3]);
    const float key3   = data[1];

    MC_PULL_stu_raw[2] = pull_v_apply_polarity(2u, data[2] + MC_PULL_V_OFFSET[2]);
    const float key2   = data[3];

    MC_PULL_stu_raw[1] = pull_v_apply_polarity(1u, data[4] + MC_PULL_V_OFFSET[1]);
    const float key1   = data[5];

    MC_PULL_stu_raw[0] = pull_v_apply_polarity(0u, data[6] + MC_PULL_V_OFFSET[0]);
    const float key0   = data[7];

    // Quantize the calibrated ADC value once per channel. All gesture and
    // control logic below consumes the cached 0.01% representation.
    for (uint8_t i = 0; i < kChCount; ++i)
    {
        if (!filament_channel_inserted[i])
        {
            MC_PULL_pct_q[i] = 5000;
            continue;
        }

        const float pct_f = pull_v_to_percent_f(i, MC_PULL_stu_raw[i]);
        int32_t pct_q = static_cast<int32_t>(pct_f * 100.0f + 0.5f);
        if (pct_q < 0) pct_q = 0;
        if (pct_q > 10000) pct_q = 10000;
        MC_PULL_pct_q[i] = static_cast<int16_t>(pct_q);
    }

#if BMCU_DM_TWO_MICROSWITCH
    const float keyv[4] = { key0, key1, key2, key3 };

    // --- Buffer Gesture Load  ---
    static uint32_t gst_t0_ticks[4]     = {0,0,0,0};
    static uint8_t  gst_step[4]         = {0,0,0,0};      // 0=idle, 1=wait_low, 2=wait_return
    static bool     gst_active[4]       = {false,false,false,false};
    static uint32_t gst_act_t0_ticks[4] = {0,0,0,0};

    uint32_t tpm = time_hw_tpms;
    if (!tpm) tpm = 1u;

    const uint32_t T100  = 100u  * tpm;
    const uint32_t T2000 = 2000u * tpm;
    const uint32_t T5500 = 5500u * tpm;

    for (uint8_t i = 0; i < kChCount; i++)
    {
        if (!filament_channel_inserted[i])
        {
            gst_step[i] = 0;
            gst_active[i] = false;
            gst_t0_ticks[i] = 0;
            gst_act_t0_ticks[i] = 0;
            MC_ONLINE_key_stu[i] = 0u;
            continue;
        }

        if (dm_fail_latch[i])
        {
            gst_step[i] = 0;
            gst_active[i] = false;
        }

        if (!gst_active[i])
        {
            const int32_t pct_q = MC_PULL_pct_q[i];

            if (gst_step[i] == 0)
            {
                if (pct_q < 1000) { gst_step[i] = 1; gst_t0_ticks[i] = now_ticks; }
            }
            else if (gst_step[i] == 1)
            {
                if (pct_q > 1500) { gst_step[i] = 0; }
                else if ((uint32_t)(now_ticks - gst_t0_ticks[i]) >= T100)
                {
                    gst_step[i] = 2;
                }
            }
            else
            {
                if ((uint32_t)(now_ticks - gst_t0_ticks[i]) > T2000)
                {
                    gst_step[i] = 0;
                }
                else if (in_closed_range_i32(pct_q, 4500, 5500))
                {
                    gst_active[i] = true;
                    gst_act_t0_ticks[i] = now_ticks;
                    gst_step[i] = 0;
                }
            }
        }

        if (gst_active[i])
        {
            if (keyv[i] > 1.7f) gst_active[i] = false;
            else if ((uint32_t)(now_ticks - gst_act_t0_ticks[i]) > T5500) gst_active[i] = false;
        }

        const uint8_t phys = dm_key_to_state(i, keyv[i]);
        uint8_t state = phys;

        if (gst_active[i] && (phys == 0u)) state = 2u;

        MC_ONLINE_key_stu[i] = state;
    }
    // --- End Buffer Gesture Load  ---
#else
    // online key: tylko jeśli kanał fizycznie wpięty
    MC_ONLINE_key_stu[3] = (filament_channel_inserted[3] && (key3 > 1.7f)) ? 1u : 0u;
    MC_ONLINE_key_stu[2] = (filament_channel_inserted[2] && (key2 > 1.7f)) ? 1u : 0u;
    MC_ONLINE_key_stu[1] = (filament_channel_inserted[1] && (key1 > 1.7f)) ? 1u : 0u;
    MC_ONLINE_key_stu[0] = (filament_channel_inserted[0] && (key0 > 1.7f)) ? 1u : 0u;
#endif


    for (uint8_t i = 0; i < kChCount; i++)
    {
        const bool ins = filament_channel_inserted[i];

        // jeśli kanał nie jest wpięty -> neutral
        if (!ins)
        {
            MC_ONLINE_key_stu[i] = 0;
            MC_PULL_pct_q[i] = 5000;
            MC_PULL_pct[i]   = 50;
            MC_PULL_stu[i]   = 0;
            continue;
        }

        const int32_t pct_q = MC_PULL_pct_q[i];
        const int pct = (pct_q + 50) / 100;
        MC_PULL_pct[i] = static_cast<uint8_t>(pct);

        if      (pct > MC_PULL_DEADBAND_PCT_HIGH) MC_PULL_stu[i] = 1;
        else if (pct < MC_PULL_DEADBAND_PCT_LOW)  MC_PULL_stu[i] = -1;
        else                                      MC_PULL_stu[i] = 0;
    }

    // pressure do hosta (tylko dla aktywnego kanału)
    auto &A = ams[motion_control_ams_num];
    const uint8_t num = A.now_filament_num;

    if ((num != 0xFF) && (num < kChCount) && filament_channel_inserted[num])
    {
        const uint8_t pct = MC_PULL_pct[num];
            const uint32_t hi = (pct > 50u) ? (uint32_t)(pct - 50u) : 0u;
            bmcu_set_pressure(A, static_cast<uint16_t>((hi * 65535u) / 50u));
    }
    else
    {
        bmcu_set_pressure(A, 0xFFFFu);
    }
}

// ===== zapis kierunku silników + progow DM key =====
struct alignas(4) Motion_control_save_struct
{
    int motor_polarity[4];
    uint32_t check;
    uint8_t dm_key_none_cv[4];
} Motion_control_data_save;

static inline void Motion_control_defaults()
{
    for (uint8_t i = 0; i < kChCount; i++)
    {
        Motion_control_data_save.motor_polarity[i] = 0;
        Motion_control_data_save.dm_key_none_cv[i] = 60u;
    }

    Motion_control_data_save.check = 0x40614061u;
}

static inline void Motion_control_apply_saved()
{
    for (uint8_t i = 0; i < kChCount; i++)
    {
        if (Motion_control_data_save.dm_key_none_cv[i] < 60u)
            Motion_control_data_save.dm_key_none_cv[i] = 60u;

        MC_DM_KEY_NONE_THRESH[i] = dm_key_centi_to_v(Motion_control_data_save.dm_key_none_cv[i]);
    }
}

static inline bool Motion_control_read()
{
    Motion_control_defaults();

    if (!Flash_Motion_read(&Motion_control_data_save, (uint16_t)sizeof(Motion_control_save_struct)))
    {
        Motion_control_apply_saved();
        return false;
    }

    if (Motion_control_data_save.check != 0x40614061u)
    {
        Motion_control_defaults();
        Motion_control_apply_saved();
        return false;
    }

    Motion_control_apply_saved();
    return true;
}

static inline bool Motion_control_save()
{
    Motion_control_data_save.check = 0x40614061u;

    for (uint8_t i = 0; i < kChCount; i++)
    {
        uint8_t cv = dm_key_v_to_centi_ceil(MC_DM_KEY_NONE_THRESH[i]);
        if (cv < 60u) cv = 60u;
        Motion_control_data_save.dm_key_none_cv[i] = cv;
    }

    return Flash_Motion_write(&Motion_control_data_save, (uint16_t)sizeof(Motion_control_save_struct));
}

bool Motion_control_save_dm_key_none_thresholds(void)
{
    float thr[4];
    for (uint8_t i = 0; i < kChCount; i++)
        thr[i] = MC_DM_KEY_NONE_THRESH[i];

    (void)Motion_control_read();

    for (uint8_t i = 0; i < kChCount; i++)
        MC_DM_KEY_NONE_THRESH[i] = thr[i];

    return Motion_control_save();
}
// ===== fixed-point PI =====
// Scale and gains are template parameters so unused integral paths disappear
// and gain-dependent divisions are folded at compile time.
template<int32_t ErrorScale, int32_t PGain, int32_t IGain>
class MOTOR_PI
{
    int32_t i_save_q8 = 0;

public:
    int32_t calculate(int32_t error_q, uint32_t dt_us)
    {
        constexpr int32_t p_abs = PGain < 0 ? -PGain : PGain;
        if constexpr (p_abs != 0)
        {
            constexpr int32_t max_error_q = (PWM_lim * ErrorScale) / p_abs;
            if (error_q > max_error_q) error_q = max_error_q;
            if (error_q < -max_error_q) error_q = -max_error_q;
        }

        const int32_t p_q8 = (PGain * error_q * 256) / ErrorScale;

        if constexpr (IGain != 0)
        {
            if (dt_us != 0u)
            {
                // The control loop is millisecond-paced. Integrating in rounded
                // milliseconds preserves the prior Q8 accumulator resolution.
                const uint32_t dt_ms = (dt_us + 500u) / 1000u;
                const int32_t rate_pwm_s = (IGain * error_q) / ErrorScale;
                i_save_q8 += static_cast<int32_t>(
                    (rate_pwm_s * static_cast<int32_t>(dt_ms) * 256) / 1000);
                constexpr int32_t i_limit_q8 = (PWM_lim / 2) * 256;
                if (i_save_q8 > i_limit_q8) i_save_q8 = i_limit_q8;
                if (i_save_q8 < -i_limit_q8) i_save_q8 = -i_limit_q8;
            }
        }

        int32_t out_q8 = p_q8 + i_save_q8;
        constexpr int32_t out_limit_q8 = PWM_lim * 256;
        if (out_q8 > out_limit_q8) out_q8 = out_limit_q8;
        if (out_q8 < -out_limit_q8) out_q8 = -out_limit_q8;
        return out_q8 >= 0 ? (out_q8 + 128) >> 8
                           : -((-out_q8 + 128) >> 8);
    }

    void clear() { i_save_q8 = 0; }
};

enum class filament_motion_enum
{
    filament_motion_send = 0,
    filament_motion_redetect = 1,
    filament_motion_pull = 2,
    filament_motion_stop = 3,
    filament_motion_before_on_use = 4,
    filament_motion_stop_on_use = 5,
    filament_motion_pressure_ctrl_on_use = 6,
    filament_motion_pressure_ctrl_idle = 7,
    filament_motion_before_pull_back = 8,
};

static constexpr uint16_t motion_bit(filament_motion_enum value)
{
    return static_cast<uint16_t>(1u << static_cast<uint8_t>(value));
}

static constexpr uint16_t kMotionSend =
    motion_bit(filament_motion_enum::filament_motion_send);
static constexpr uint16_t kMotionRedetect =
    motion_bit(filament_motion_enum::filament_motion_redetect);
static constexpr uint16_t kMotionPull =
    motion_bit(filament_motion_enum::filament_motion_pull);
static constexpr uint16_t kMotionStop =
    motion_bit(filament_motion_enum::filament_motion_stop);
static constexpr uint16_t kMotionBeforeOnUse =
    motion_bit(filament_motion_enum::filament_motion_before_on_use);
static constexpr uint16_t kMotionStopOnUse =
    motion_bit(filament_motion_enum::filament_motion_stop_on_use);
static constexpr uint16_t kMotionPressureOnUse =
    motion_bit(filament_motion_enum::filament_motion_pressure_ctrl_on_use);
static constexpr uint16_t kMotionPressureIdle =
    motion_bit(filament_motion_enum::filament_motion_pressure_ctrl_idle);
static constexpr uint16_t kMotionBeforePullBack =
    motion_bit(filament_motion_enum::filament_motion_before_pull_back);
static constexpr uint16_t kMotionOnUseLike =
    kMotionPressureOnUse | kMotionBeforeOnUse | kMotionStopOnUse;
static constexpr uint16_t kMotionHold =
    kMotionPressureIdle | kMotionPressureOnUse |
    kMotionBeforeOnUse | kMotionStopOnUse;

// The two states motor_motion_switch converges a channel to once the bus goes
// quiet: stop for an unloaded, jammed or faulted channel, pressure_ctrl_idle
// for one holding filament (:2441-2448, :2586-2598, :2611-2614). Everything
// else is either printing or a transition into it:
//
//   send (0)                  drives, ramped          printer-commanded feed
//   redetect (1)              drives                  local re-detect, no bus command
//   pull (2)                  drives                  retract
//   stop (3)                  run() commands nothing  parked
//   before_on_use (4)         drives                  printing
//   stop_on_use (5)           drives                  printing
//   pressure_ctrl_on_use (6)  drives at full PWM_lim  printing
//   pressure_ctrl_idle (7)    drives, clamped +-800   parked
//   before_pull_back (8)      drives                  transition
//
// Parked does not mean the motor cannot move, and the column above describes
// only what motor.run() commands. Two overrides bypass run() and write PWM
// directly without consulting this enum at all: auto-unload, and the manual
// empty pull, whose predicate is inserted && ks == 0 && pull above 80%. Either
// can drive a channel whose phase reads stop. DM autoload likewise drives, and
// only from pressure_ctrl_idle.
//
// So parked means no operation is in flight, not that the output is idle --
// which is why Motion_control_is_reset_safe pairs this mask with the
// instantaneous g_motor_pwm rather than trusting either alone.
static constexpr uint16_t kMotionParked = kMotionStop | kMotionPressureIdle;



// ===== Motor control =====
class _MOTOR_CONTROL
{
public:
    filament_motion_enum motion = filament_motion_enum::filament_motion_stop;
    int CHx = 0;

    uint8_t pwm_zeroed = 1;
    uint16_t motion_mask = kMotionStop;

    uint32_t motor_stop_time = 0u;

    int16_t post_sendout_retract_thresh_q = -1;
    uint8_t retract_hys_active = 0;
    int16_t on_use_hi_gate_q = -1;
    uint32_t on_use_hi_gate_t0_ms = 0u;

    uint32_t send_start_ms = 0u;
    uint8_t  send_len_abort = 0;

    uint32_t pull_start_ms = 0u;
    int32_t pull_progress_checkpoint_counts = 0;
    uint32_t pull_progress_checkpoint_ms = 0u;
    uint8_t motion_fault = MOTION_FAULT_NONE;

    int32_t  directed_progress_counts = 0;
    uint32_t travel_budget_counts = 0;
    int32_t  progress_negate_mask = 0; // 0: keep raw delta, -1: negate raw delta

    bool send_stop_latch = false;

    MOTOR_PI<1000, 2, 20> PID_speed;
    MOTOR_PI<100, 25, 0> PID_pressure;

    int16_t pwm_zero = 500;
    int32_t motor_polarity_negate_mask = 0;
    uint8_t motor_polarity_valid = 0u;

    static int32_t x_prev[4];

    bool  send_hard = false;

    _MOTOR_CONTROL(int _CHx) : CHx(_CHx) {}

    void set_pwm_zero(int16_t value) { pwm_zero = value; }

    inline __attribute__((always_inline)) int32_t apply_motor_polarity(int32_t value) const
    {
        return apply_integer_sign_mask(value, motor_polarity_negate_mask);
    }

    inline __attribute__((always_inline)) int32_t to_logical_pwm(int32_t value) const
    {
        return apply_integer_sign_mask(value, motor_polarity_negate_mask);
    }

    void set_motion(filament_motion_enum _motion, uint32_t over_time)
    {
        set_motion(_motion, over_time, time_ms_fast());
    }

    void set_motion(filament_motion_enum _motion, uint32_t over_time, uint32_t time_now)
    {
        motor_stop_time = (_motion == filament_motion_enum::filament_motion_stop) ? 0 : (time_now + over_time);

        if (motion == _motion) return;

        const filament_motion_enum prev = motion;
        motion = _motion;
        motion_mask = motion_bit(_motion);

        if ((_motion != filament_motion_enum::filament_motion_pressure_ctrl_on_use) &&
            g_on_use_low_latch[CHx] && !g_on_use_jam_latch[CHx])
        {
            g_on_use_low_latch[CHx] = 0u;
            g_on_use_hi_pwm_us[CHx] = 0u;
        }

        pwm_zeroed = 0;

        if (_motion == filament_motion_enum::filament_motion_send) {
            send_start_ms = time_now;
            send_stop_latch = false;
            send_len_abort = 0;
            directed_progress_counts = 0;
            travel_budget_counts = 0;
            progress_negate_mask = motion_progress_negate_mask(true);
        }

        if (_motion == filament_motion_enum::filament_motion_pull) {
            pull_start_ms = time_now;
            directed_progress_counts = 0;
            travel_budget_counts = 0;
            progress_negate_mask = motion_progress_negate_mask(false);
            pull_progress_checkpoint_counts = 0;
            pull_progress_checkpoint_ms = time_now;
            motion_fault = MOTION_FAULT_NONE;
        }

        if (prev == filament_motion_enum::filament_motion_send &&
            _motion != filament_motion_enum::filament_motion_send)
        {
            send_start_ms = 0;
            send_stop_latch = false;
            send_len_abort = 0;
        }

        if (prev == filament_motion_enum::filament_motion_pull &&
            _motion != filament_motion_enum::filament_motion_pull)
        {
            pull_start_ms = 0;
        }

        if (_motion == filament_motion_enum::filament_motion_pressure_ctrl_on_use)
        {
            if (g_last_on_use_exit_ms[CHx] == 0) g_last_on_use_exit_ms[CHx] = 1;
        }

        if (prev == filament_motion_enum::filament_motion_pressure_ctrl_on_use &&
            _motion != filament_motion_enum::filament_motion_pressure_ctrl_on_use)
        {
            g_last_on_use_exit_ms[CHx] = time_now;
        }

        if (_motion == filament_motion_enum::filament_motion_send ||
            _motion == filament_motion_enum::filament_motion_pull)
        {
            g_last_on_use_exit_ms[CHx] = 0;
        }

        if (_motion == filament_motion_enum::filament_motion_send)
        {
            send_hard = false;
        }

        if (prev == filament_motion_enum::filament_motion_send &&
            _motion != filament_motion_enum::filament_motion_send)
        {
            send_hard = false;
        }

        PID_speed.clear();
        PID_pressure.clear();

        const bool keep_pwm =
            (prev == filament_motion_enum::filament_motion_send) &&
            (_motion == filament_motion_enum::filament_motion_pressure_ctrl_on_use);

        if (_motion == filament_motion_enum::filament_motion_send)
        {
            post_sendout_retract_thresh_q = -1;
            retract_hys_active = 0;
        }

        if (_motion == filament_motion_enum::filament_motion_before_on_use || _motion == filament_motion_enum::filament_motion_stop_on_use)
        {
            post_sendout_retract_thresh_q = MC_PULL_pct_q[CHx];
            retract_hys_active = 0;
        }

        if (_motion == filament_motion_enum::filament_motion_pressure_ctrl_on_use)
        {
            retract_hys_active = 0;
            const int16_t p_q = MC_PULL_pct_q[CHx];
            post_sendout_retract_thresh_q = p_q;

            if (prev == filament_motion_enum::filament_motion_before_on_use ||
                prev == filament_motion_enum::filament_motion_stop_on_use)
            {
                on_use_hi_gate_q = p_q;
                on_use_hi_gate_t0_ms = time_now;
            }
            else
            {
                on_use_hi_gate_q = -1;
                on_use_hi_gate_t0_ms = 0u;
            }
        }
        else if (prev == filament_motion_enum::filament_motion_pressure_ctrl_on_use)
        {
            post_sendout_retract_thresh_q = -1;
            retract_hys_active = 0;
            on_use_hi_gate_q = -1;
            on_use_hi_gate_t0_ms = 0u;
        }

        if (_motion == filament_motion_enum::filament_motion_pull)
        {
            post_sendout_retract_thresh_q = -1;
            retract_hys_active = 0;
        }

        if (!keep_pwm)
        {
            x_prev[CHx] = 0;
        }
        else
        {
            if (x_prev[CHx] > 600)  x_prev[CHx] = 600;
            if (x_prev[CHx] < -850) x_prev[CHx] = -850;
        }
    }

    filament_motion_enum get_motion() { return motion; }

    __attribute__((noinline, cold)) void stop_output_cold()
    {
        PID_speed.clear();
        PID_pressure.clear();
        pwm_zeroed = 1;
        x_prev[CHx] = 0;
        Motion_control_set_PWM(CHx, 0);
    }

    static inline void hold_load(
        int32_t pct_q,
        int32_t motor_polarity_negate_mask,
        MOTOR_PI<100, 25, 0> &PID_pressure,
        int16_t &post_sendout_retract_thresh_q,
        uint8_t &retract_hys_active,
        int32_t &x,
        bool &on_use_need_move,
        int32_t &on_use_abs_err_q,
        bool &on_use_linear
    )
    {
        constexpr int32_t hold_target_q =
            static_cast<int32_t>(MC_LOAD_S2_HOLD_TARGET_PCT * 100.0f);

        int32_t threshold_q = post_sendout_retract_thresh_q;
        if (threshold_q < hold_target_q) threshold_q = hold_target_q;

        if (pct_q > threshold_q)
        {
            retract_hys_active = hyst_q(
                retract_hys_active, pct_q, threshold_q + 25, threshold_q);

            if (!retract_hys_active)
            {
                x = 0;
                PID_pressure.clear();
                on_use_need_move = false;
                on_use_abs_err_q = 0;
                on_use_linear = false;
            }
            else
            {
                const int32_t err_q = pct_q - threshold_q;
                on_use_need_move = true;
                on_use_abs_err_q = err_q;
                on_use_linear = false;

                const int32_t magnitude = retract_mag_from_err_q(err_q, 850);
                x = apply_integer_sign_mask(magnitude, motor_polarity_negate_mask);
                if (apply_integer_sign_mask(x, motor_polarity_negate_mask) < 0) x = 0;
            }
            return;
        }

        retract_hys_active = 0u;
        constexpr int32_t push_hi_q = hold_target_q -
            static_cast<int32_t>(MC_LOAD_S2_HOLD_BAND_LO_DELTA * 100.0f);
        constexpr int32_t push_start_q =
            static_cast<int32_t>(MC_LOAD_S2_PUSH_START_PCT * 100.0f);
        constexpr int32_t pwm_hi = static_cast<int32_t>(MC_LOAD_S2_PWM_HI);
        constexpr int32_t pwm_lo = static_cast<int32_t>(MC_LOAD_S2_PWM_LO);

        if (pct_q >= push_hi_q)
        {
            x = 0;
            PID_pressure.clear();
            on_use_need_move = false;
            on_use_abs_err_q = 0;
            on_use_linear = false;
            return;
        }

        int32_t pwm = pwm_lo;
        if (pct_q > push_start_q)
        {
            pwm = pwm_hi + ((pwm_lo - pwm_hi) * (push_hi_q - pct_q)) /
                               (push_hi_q - push_start_q);
        }

        x = apply_integer_sign_mask(-pwm, motor_polarity_negate_mask);
        PID_pressure.clear();
        on_use_need_move = true;
        on_use_abs_err_q = hold_target_q - pct_q;
        on_use_linear = true;
    }

    void run(uint32_t dt_us, uint32_t now_ms)
    {
        const uint16_t mode = motion_mask;

        if ((mode & kMotionStop) != 0u &&
            motor_stop_time == 0 &&
            pwm_zeroed)
            return;

        if (__builtin_expect((mode & kMotionStop) == 0u &&
                             motor_stop_time != 0 &&
                             static_cast<int32_t>(now_ms - motor_stop_time) >= 0, 0))
        {
            if ((mode & kMotionPressureOnUse) != 0u)
                g_last_on_use_exit_ms[CHx] = now_ms;

            motion = filament_motion_enum::filament_motion_stop;
            motion_mask = kMotionStop;
            stop_output_cold();
            return;
        }

        if (__builtin_expect(!motor_polarity_valid, 0))
        {
            stop_output_cold();
            return;
        }

        if ((mode & kMotionPressureOnUse) != 0u &&
            __builtin_expect(g_on_use_low_latch[CHx] != 0u, 0))
        {
            g_on_use_hi_pwm_us[CHx] = 0u;
            stop_output_cold();
            return;
        }

        int32_t speed_set_q = 0;
        const int32_t now_speed_q = speed_as5600_q[CHx];
        int32_t x = 0;
#if BMCU_DM_TWO_MICROSWITCH
        bool  dm_autoload_active = false;
        int32_t dm_autoload_x = 0;
#endif

        // info o ostatnim wyjściu z on_use
        const uint32_t t_exit = g_last_on_use_exit_ms[CHx];
        const bool had_on_use  = (t_exit != 0);
        const bool has_exit_ts = (t_exit > 1);
        uint32_t dt_exit = 0u;
        if (has_exit_ts) dt_exit = (now_ms - t_exit);

        // aktywne tylko: idle + brak filamentu + kanał wpięty + kiedykolwiek był w on_use
        const bool post_on_use_active =
            ((mode & kMotionPressureIdle) != 0u) &&
            (MC_ONLINE_key_stu[CHx] == 0) &&
            filament_channel_inserted[CHx] &&
            had_on_use;

        const bool post_on_use_10s =
            post_on_use_active && has_exit_ts && (dt_exit < 10000u);

        const bool on_use_like =
            (mode & kMotionOnUseLike) != 0u ||
            post_on_use_10s ||
            (((mode & kMotionSend) != 0u) && send_stop_latch);

        bool  on_use_need_move = false;
        int32_t on_use_abs_err_q = 0;
        bool  on_use_linear    = false;

        if ((mode & kMotionPressureIdle) != 0u)
        {
        #if BMCU_DM_TWO_MICROSWITCH
                    // --- DM autoload (Stage1 + Stage2) ---
                    if (filament_channel_inserted[CHx] && (dm_loaded[CHx] == 0u))
                    {
                        const uint8_t ks = MC_ONLINE_key_stu[CHx];
                        const int32_t cur_counts = g_filament_position_counts[CHx];

                        if (dm_fail_latch[CHx])
                        {
                            dm_autoload_active = true;
                            dm_autoload_x = 0;
                            MC_STU_RGB_set(CHx, 0xFF, 0x00, 0x00);
                        }
                        else
                        {
                            if (dm_auto_state[CHx] == DM_AUTO_IDLE)
                            {
                                if (ks == 2u)
                                {
                                    if (dm_autoload_gate[CHx] == 0u)
                                    {
                                        dm_autoload_gate[CHx] = 1u;
                                        dm_auto_state[CHx] = DM_AUTO_S1_DEBOUNCE;
                                        dm_auto_t0_ms[CHx] = now_ms;
                                    }
                                }
                                else if (ks == 1u)
                                {
                                    dm_auto_state[CHx]    = DM_AUTO_S2_PUSH;
                                    dm_auto_try[CHx]      = 0u;
                                    dm_auto_remain_counts[CHx] = DM_AUTO_S2_TARGET_COUNTS;
                                    dm_auto_last_counts[CHx]   = cur_counts;
                                }
                            }

                            switch (dm_auto_state[CHx])
                            {
                            case DM_AUTO_S1_DEBOUNCE:
                                dm_autoload_active = true;
                                MC_STU_RGB_set(CHx, 0xFF, 0xFF, 0x00);

                                if (ks != 2u)
                                {
                                    dm_auto_state[CHx] = DM_AUTO_IDLE;
                                    dm_auto_t0_ms[CHx] = 0u;
                                }
                                else if ((now_ms - dm_auto_t0_ms[CHx]) >= DM_AUTO_S1_DEBOUNCE_MS)
                                {
                                    dm_auto_state[CHx] = DM_AUTO_S1_PUSH;
                                    dm_auto_t0_ms[CHx] = now_ms;
                                }
                                break;

                            case DM_AUTO_S1_PUSH:
                                dm_autoload_active = true;
                                MC_STU_RGB_set(CHx, 0xFF, 0xFF, 0x00);

                                if (ks == 0u)
                                {
                                    dm_auto_state[CHx] = DM_AUTO_IDLE;
                                    dm_auto_t0_ms[CHx] = 0u;
                                }
                                else if (ks == 1u)
                                {
                                    dm_auto_state[CHx]    = DM_AUTO_S2_PUSH;
                                    dm_auto_try[CHx]      = 0u;
                                    dm_auto_remain_counts[CHx] = DM_AUTO_S2_TARGET_COUNTS;
                                    dm_auto_last_counts[CHx]   = cur_counts;
                                }
                                else if ((now_ms - dm_auto_t0_ms[CHx]) >= DM_AUTO_S1_TIMEOUT_MS)
                                {
                                    dm_fail_latch[CHx] = 1u;
                                    dm_auto_state[CHx] = DM_AUTO_S1_FAIL_RETRACT;
                                    dm_auto_t0_ms[CHx] = now_ms;
                                }
                                else
                                {
                                    dm_autoload_x = apply_motor_polarity(-DM_AUTO_PWM_PUSH);
                                }
                                break;

                            case DM_AUTO_S1_FAIL_RETRACT:
                                dm_autoload_active = true;
                                MC_STU_RGB_set(CHx, 0xFF, 0x00, 0x00);

                                if (ks == 0u)
                                {
                                    dm_auto_state[CHx] = DM_AUTO_IDLE;
                                    dm_auto_t0_ms[CHx] = 0u;
                                }
                                else if ((now_ms - dm_auto_t0_ms[CHx]) >= DM_AUTO_S1_FAIL_RETRACT_MS)
                                {
                                    dm_auto_state[CHx] = DM_AUTO_IDLE;
                                    dm_auto_t0_ms[CHx] = 0u;
                                }
                                else
                                {
                                    dm_autoload_x = apply_motor_polarity(DM_AUTO_PWM_PULL);
                                }
                                break;

                            case DM_AUTO_S2_PUSH:
                                dm_autoload_active = true;
                                MC_STU_RGB_set(CHx, 0xFF, 0xFF, 0x00);

                                if (ks != 1u)
                                {
                                    if (ks == 2u)
                                    {
                                        dm_auto_state[CHx] = DM_AUTO_S1_DEBOUNCE;
                                        dm_auto_t0_ms[CHx] = now_ms;
                                    }
                                    else
                                    {
                                        dm_auto_state[CHx]    = DM_AUTO_IDLE;
                                        dm_auto_try[CHx]      = 0u;
                                        dm_auto_remain_counts[CHx] = 0;
                                        dm_auto_t0_ms[CHx]    = 0u;
                                    }
                                    break;
                                }

                                {
                                    const uint32_t moved = delta_magnitude_counts(
                                        cur_counts - dm_auto_last_counts[CHx]);
                                    dm_auto_last_counts[CHx] = cur_counts;

                                    int32_t remain = dm_auto_remain_counts[CHx] -
                                        static_cast<int32_t>(moved);
                                    if (remain < 0) remain = 0;
                                    dm_auto_remain_counts[CHx] = remain;
                                }

                                if (MC_PULL_pct_q[CHx] >
                                    static_cast<int32_t>(DM_AUTO_BUF_ABORT_PCT * 100.0f))
                                {
                                    uint8_t t = dm_auto_try[CHx];
                                    if (t < 255u) t++;
                                    dm_auto_try[CHx] = t;

                                    dm_auto_last_counts[CHx] = cur_counts;

                                    if (t >= 3u)
                                    {
                                        dm_fail_latch[CHx] = 1u;
                                        dm_auto_state[CHx] = DM_AUTO_S2_FAIL_RETRACT;
                                    }
                                    else
                                    {
                                        dm_auto_state[CHx] = DM_AUTO_S2_RETRACT;
                                    }

                                    MC_STU_RGB_set(CHx, 0xFF, 0x00, 0x00);
                                }
                                else if (dm_auto_remain_counts[CHx] <= 0)
                                {
                                    dm_loaded[CHx] = 1u;

                                    dm_auto_state[CHx]    = DM_AUTO_IDLE;
                                    dm_auto_try[CHx]      = 0u;
                                    dm_auto_remain_counts[CHx] = 0;
                                    dm_auto_t0_ms[CHx]    = 0u;

                                    MC_STU_RGB_set(CHx, 0x38, 0x35, 0x32);
                                    dm_autoload_x = 0;
                                }
                                else
                                {
                                    dm_autoload_x = apply_motor_polarity(-DM_AUTO_PWM_PUSH);
                                }
                                break;

                            case DM_AUTO_S2_RETRACT:
                                dm_autoload_active = true;

                                if (ks == 0u)
                                {
                                    dm_auto_state[CHx]    = DM_AUTO_IDLE;
                                    dm_auto_try[CHx]      = 0u;
                                    dm_auto_remain_counts[CHx] = 0;
                                    dm_auto_t0_ms[CHx]    = 0u;
                                    break;
                                }

                                {
                                    const uint32_t moved = delta_magnitude_counts(
                                        cur_counts - dm_auto_last_counts[CHx]);
                                    dm_auto_last_counts[CHx] = cur_counts;

                                    int32_t remain = dm_auto_remain_counts[CHx] +
                                        static_cast<int32_t>(moved);
                                    if (remain > DM_AUTO_S2_TARGET_COUNTS)
                                        remain = DM_AUTO_S2_TARGET_COUNTS;
                                    dm_auto_remain_counts[CHx] = remain;
                                }

                                MC_STU_RGB_set(CHx, 0xFF, 0x00, 0x00);

                                if ((MC_PULL_pct_q[CHx] <=
                                     static_cast<int32_t>(DM_AUTO_BUF_RECOVER_PCT * 100.0f)) ||
                                    (ks == 2u))
                                {
                                    dm_auto_last_counts[CHx] = cur_counts;

                                    if (ks == 1u)
                                    {
                                        dm_auto_state[CHx] = DM_AUTO_S2_PUSH;
                                    }
                                    else if (ks == 2u)
                                    {
                                        dm_auto_state[CHx] = DM_AUTO_S1_DEBOUNCE;
                                        dm_auto_t0_ms[CHx] = now_ms;
                                    }
                                    else
                                    {
                                        dm_auto_state[CHx]    = DM_AUTO_IDLE;
                                        dm_auto_try[CHx]      = 0u;
                                        dm_auto_remain_counts[CHx] = 0;
                                        dm_auto_t0_ms[CHx]    = 0u;
                                    }
                                    dm_autoload_x = 0;
                                }
                                else
                                {
                                    dm_autoload_x = apply_motor_polarity(DM_AUTO_PWM_PULL);
                                }
                                break;

                            case DM_AUTO_S2_FAIL_RETRACT:
                                dm_autoload_active = true;
                                MC_STU_RGB_set(CHx, 0xFF, 0x00, 0x00);

                                if (ks == 0u)
                                {
                                    dm_auto_state[CHx]    = DM_AUTO_IDLE;
                                    dm_auto_try[CHx]      = 0u;
                                    dm_auto_remain_counts[CHx] = 0;
                                    dm_auto_t0_ms[CHx]    = 0u;
                                }
                                else if (ks == 2u)
                                {
                                    dm_auto_state[CHx] = DM_AUTO_S2_FAIL_EXTRA;
                                    dm_auto_t0_ms[CHx] = now_ms;
                                }
                                else
                                {
                                    dm_autoload_x = apply_motor_polarity(DM_AUTO_PWM_PULL);
                                }
                                break;

                            case DM_AUTO_S2_FAIL_EXTRA:
                                dm_autoload_active = true;
                                MC_STU_RGB_set(CHx, 0xFF, 0x00, 0x00);

                                if (ks == 0u)
                                {
                                    dm_auto_state[CHx]    = DM_AUTO_IDLE;
                                    dm_auto_try[CHx]      = 0u;
                                    dm_auto_remain_counts[CHx] = 0;
                                    dm_auto_t0_ms[CHx]    = 0u;
                                }
                                else if ((now_ms - dm_auto_t0_ms[CHx]) >= DM_AUTO_FAIL_EXTRA_MS)
                                {
                                    dm_auto_state[CHx]    = DM_AUTO_IDLE;
                                    dm_auto_try[CHx]      = 0u;
                                    dm_auto_remain_counts[CHx] = 0;
                                    dm_auto_t0_ms[CHx]    = 0u;
                                }
                                else
                                {
                                    dm_autoload_x = apply_motor_polarity(DM_AUTO_PWM_PULL);
                                }
                                break;

                            default:
                                dm_auto_state[CHx]    = DM_AUTO_IDLE;
                                dm_auto_try[CHx]      = 0u;
                                dm_auto_remain_counts[CHx] = 0;
                                dm_auto_t0_ms[CHx]    = 0u;
                                break;
                            }
                        }
                    }

                    if (dm_autoload_active)
                    {
                        x = dm_autoload_x;
                        PID_pressure.clear();
                        PID_speed.clear();
                    }
                    else
        #endif

            if (MC_ONLINE_key_stu[CHx] == 0)
            {
                if (!filament_channel_inserted[CHx] || !had_on_use)
                {
                    PID_pressure.clear();
                    pwm_zeroed = 1;
                    x_prev[CHx] = 0;
                    Motion_control_set_PWM(CHx, 0);
                    return;
                }

                if (post_on_use_10s)
                {
                    if ((uint8_t)MC_PULL_pct[CHx] >= 49u)
                    {
                        x = 0;
                        PID_pressure.clear();
                        on_use_need_move = false;
                        on_use_abs_err_q = 0;
                    }
                    else
                    {
                        const int32_t err_q = MC_PULL_pct_q[CHx] - 4900;

                        on_use_need_move = true;
                        on_use_abs_err_q = -err_q;

                        x = apply_motor_polarity(
                            PID_pressure.calculate(MC_PULL_pct_q[CHx] - 4900, dt_us));

                        int32_t lim = 500 + (80 * on_use_abs_err_q) / 100;
                        if (lim > 900) lim = 900;

                        if (x >  lim) x =  lim;
                        if (x < -lim) x = -lim;
                        if (to_logical_pwm(x) > 0)
                        {
                            x = 0;
                            PID_pressure.clear();
                            on_use_need_move = false;
                            on_use_abs_err_q = 0;
                        }
                    }
                }
                else
                {
                    // po 10s: idle jakby filament był -> tylko na krańcach (MC_PULL_stu != 0)
                    if (MC_PULL_stu[CHx] != 0)
                    {
                        x = apply_motor_polarity(
                            PID_pressure.calculate(MC_PULL_pct_q[CHx] - 5000, dt_us));
                    }
                    else
                    {
                        x = 0;
                        PID_pressure.clear();
                    }
                }
            }
            else
            {
                // normalny idle z filamentem
                if (MC_PULL_stu[CHx] != 0)
                {
                    x = apply_motor_polarity(
                        PID_pressure.calculate(MC_PULL_pct_q[CHx] - 5000, dt_us));
                }
                else
                {
                    x = 0;
                    PID_pressure.clear();
                }
            }
        }
        else if ((mode & kMotionRedetect) != 0u) // wyjście do braku filamentu -> ponowne podanie
        {
            x = apply_motor_polarity(-900);
        }
        else if (MC_ONLINE_key_stu[CHx] != 0) // kanał aktywny i jest filament
        {
            if ((mode & kMotionBeforePullBack) != 0u)
            {
                const int32_t pct_q = MC_PULL_pct_q[CHx];
                constexpr int32_t target_q = 5000;
                static uint8_t pb_active[4] = {0,0,0,0};

                pb_active[CHx] = hyst_q(pb_active[CHx], pct_q, 5025, target_q);

                if (!pb_active[CHx])
                {
                    x = 0;
                    on_use_need_move = false;
                    on_use_abs_err_q = 0;
                }
                else
                {
                    const int32_t err_q = pct_q - target_q;
                    on_use_need_move = true;
                    on_use_abs_err_q = err_q;

                    x = apply_motor_polarity(retract_mag_from_err_q(err_q, 850));
                    if (to_logical_pwm(x) < 0) x = 0;
                }
            }
            else if ((mode & kMotionBeforeOnUse) != 0u)
            {
                hold_load(
                    MC_PULL_pct_q[CHx],
                    motor_polarity_negate_mask,
                    PID_pressure,
                    post_sendout_retract_thresh_q,
                    retract_hys_active,
                    x,
                    on_use_need_move,
                    on_use_abs_err_q,
                    on_use_linear
                );
            }
            else if ((mode & kMotionStopOnUse) != 0u)
            {
                PID_pressure.clear();
                pwm_zeroed = 1;
                x_prev[CHx] = 0;
                Motion_control_set_PWM(CHx, 0);
                return;
            }
            else if ((mode & kMotionPressureOnUse) != 0u)
            {
                const int32_t pct_q = MC_PULL_pct_q[CHx];
                constexpr int32_t target_q =
                    static_cast<int32_t>(MC_ON_USE_TARGET_PCT * 100.0f);
                constexpr int32_t band_lo_q = target_q -
                    static_cast<int32_t>(MC_ON_USE_BAND_LO_DELTA * 100.0f);
                constexpr int32_t band_hi_q =
                    static_cast<int32_t>(MC_ON_USE_BAND_HI_PCT * 100.0f);
                int32_t band_hi_eff_q = band_hi_q;

                if (on_use_hi_gate_q >= 0)
                {
                    const bool gate_active =
                        (on_use_hi_gate_t0_ms != 0u) &&
                        ((now_ms - on_use_hi_gate_t0_ms) < 5000u);

                    if (!gate_active)
                    {
                        on_use_hi_gate_q = -1;
                        on_use_hi_gate_t0_ms = 0u;
                    }
                    else
                    {
                        if (pct_q > on_use_hi_gate_q)
                            on_use_hi_gate_q = static_cast<int16_t>(pct_q);

                        if (on_use_hi_gate_q - pct_q > 200)
                        {
                            int32_t new_gate_q = pct_q + 100;
                            if (new_gate_q < band_hi_q) new_gate_q = band_hi_q;
                            if (new_gate_q > 10000) new_gate_q = 10000;
                            on_use_hi_gate_q = static_cast<int16_t>(new_gate_q);
                        }

                        if (on_use_hi_gate_q > band_hi_eff_q)
                            band_hi_eff_q = on_use_hi_gate_q;
                    }
                }

                retract_hys_active = 0u;

                if (in_closed_range_i32(pct_q, band_lo_q, band_hi_eff_q))
                {
                    x = 0;
                    PID_pressure.clear();
                    on_use_need_move = false;
                    on_use_abs_err_q = 0;
                }
                else if (pct_q < band_lo_q)
                {
                    constexpr int32_t pwm_lo = 380;
                    constexpr int32_t pct_fast_q = 5000;
                    constexpr int32_t pwm_fast = 900;
                    int32_t pwm = pwm_fast;
                    if (pct_q >= pct_fast_q)
                    {
                        pwm = pwm_lo + ((pwm_fast - pwm_lo) *
                              (band_lo_q - pct_q)) / (band_lo_q - pct_fast_q);
                    }
                    if (pwm > pwm_fast) pwm = pwm_fast;

                    x = apply_motor_polarity(-pwm);
                    PID_pressure.clear();
                    on_use_need_move = true;
                    on_use_abs_err_q = target_q - pct_q;
                    on_use_linear = true;
                }
                else
                {
                    const int32_t err_q = pct_q - target_q;
                    on_use_need_move = true;
                    on_use_abs_err_q = err_q < 0 ? -err_q : err_q;

                    x = apply_motor_polarity(PID_pressure.calculate(err_q, dt_us));

                    int32_t limit = 500 + (80 * on_use_abs_err_q) / 100;
                    if (limit > 900) limit = 900;
                    if (x > limit) x = limit;
                    if (x < -limit) x = -limit;

                    constexpr int32_t retrigger_q = 5500;
                    if (err_q > 0 && pct_q >= retrigger_q)
                    {
                        int32_t multiplier_q = 100 + (pct_q - retrigger_q) / 2;
                        if (multiplier_q > 300) multiplier_q = 300;
                        int32_t scaled = (static_cast<int32_t>(x) * multiplier_q) / 100;
                        if (scaled > 950) scaled = 950;
                        if (scaled < -950) scaled = -950;
                        x = scaled;
                    }
                }
            }
            else
            {
                if ((mode & kMotionStop) != 0u)
                {
                    PID_speed.clear();
                    pwm_zeroed = 1;
                    x_prev[CHx] = 0;
                    Motion_control_set_PWM(CHx, 0);
                    return;
                }

                bool do_speed_pid = true;

                if ((mode & kMotionSend) != 0u)
                {
                    const int32_t pct_q = MC_PULL_pct_q[CHx];

                    if (!send_len_abort)
                    {
                        constexpr uint32_t SEND_MAX_COUNTS =
                            static_cast<uint32_t>(distance_m_to_counts(10.0f));
                        if (travel_budget_counts >= SEND_MAX_COUNTS)
                            send_len_abort = 1;
                    }

                    if (send_len_abort)
                    {
                        PID_speed.clear();
                        PID_pressure.clear();
                        pwm_zeroed = 1;
                        x_prev[CHx] = 0;
                        Motion_control_set_PWM(CHx, 0);
                        return;
                    }

                    // HARD STOP
                    if (MC_PULL_pct[CHx] >= MC_LOAD_S1_HARD_STOP_PCT)
                    {
                        send_hard = true;
                        PID_speed.clear();
                        PID_pressure.clear();
                        pwm_zeroed = 1;
                        x_prev[CHx] = 0;
                        Motion_control_set_PWM(CHx, 0);
                        return;
                    }

                    if (send_hard)
                    {
                        if (MC_PULL_pct[CHx] >=
                            (MC_LOAD_S1_HARD_STOP_PCT - MC_LOAD_S1_HARD_HYS))
                        {
                            PID_speed.clear();
                            PID_pressure.clear();
                            pwm_zeroed = 1;
                            x_prev[CHx] = 0;
                            Motion_control_set_PWM(CHx, 0);
                            return;
                        }
                        send_hard = false;
                    }

                    if (!send_stop_latch && (MC_PULL_pct[CHx] >= MC_LOAD_S1_FAST_PCT))
                    {
                        send_stop_latch = true;

                        post_sendout_retract_thresh_q =
                            static_cast<int16_t>(pct_q);
                        retract_hys_active = 0;

                        PID_speed.clear();
                        PID_pressure.clear();
                    }

                    if (send_stop_latch)
                    {
                        do_speed_pid = false;

                        hold_load(
                            pct_q,
                            motor_polarity_negate_mask,
                            PID_pressure,
                            post_sendout_retract_thresh_q,
                            retract_hys_active,
                            x,
                            on_use_need_move,
                            on_use_abs_err_q,
                            on_use_linear
                        );
                    }
                    else
                    {
                        constexpr uint32_t SEND_SOFTSTART_MS = 300u;
                        constexpr int32_t V0_Q = 10000;
                        constexpr int32_t V_Q = 60000;

                        const uint32_t dt = (send_start_ms != 0u)
                            ? (now_ms - send_start_ms) : 1000000u;

                        if (dt < SEND_SOFTSTART_MS)
                        {
                            speed_set_q = V0_Q + static_cast<int32_t>(
                                (static_cast<uint32_t>(V_Q - V0_Q) * dt) /
                                SEND_SOFTSTART_MS);
                        }
                        else
                        {
                            speed_set_q = V_Q;
                        }
                    }
                }

                if ((mode & kMotionPull) != 0u) // cofanie
                {
                    speed_set_q = g_pull_speed_set_q[CHx]; // 0.001 mm/s
                }

                if (do_speed_pid)
                    x = apply_motor_polarity(
                        PID_speed.calculate(now_speed_q - speed_set_q, dt_us));
            }
        }
        else
        {
            x = 0;
        }

        // stałe tryby
        const bool pull_mode = ((mode & kMotionPull) != 0u);
        const bool pb_mode = ((mode & kMotionBeforePullBack) != 0u);

        const bool send_stop_hold_mode =
            ((mode & kMotionSend) != 0u) && send_stop_latch;

        const bool hold_mode =
            (mode & kMotionHold) != 0u ||
            post_on_use_active ||
            send_stop_hold_mode;

        const int deadband =
            pb_mode ? 0 :
            (hold_mode ? 1 : (pull_mode ? 2 : 10));

        const int32_t pwm0 =
            pb_mode ? 0 : (hold_mode ? 420 :
            (pull_mode ? g_pull_pwm_floor[CHx] : pwm_zero));

        if (x > deadband)
        {
            if (x < pwm0) x = pwm0;
        }
        else if (x < -deadband)
        {
            if (-x < pwm0) x = -pwm0;
        }
        else
        {
            x = 0;
        }

        // clamp
        if ((mode & kMotionPressureIdle) != 0u)
        {
        #if BMCU_DM_TWO_MICROSWITCH
            const int32_t lim = dm_autoload_active ? DM_AUTO_IDLE_LIM : 800;
            if (x >  lim) x =  lim;
            if (x < -lim) x = -lim;
        #else
            constexpr int32_t PWM_IDLE_LIM = 800;
            if (x >  PWM_IDLE_LIM) x =  PWM_IDLE_LIM;
            if (x < -PWM_IDLE_LIM) x = -PWM_IDLE_LIM;
        #endif
        }
        else
        {
            if (x > PWM_lim) x = PWM_lim;
            if (x < -PWM_lim) x = -PWM_lim;
        }

        // ON_USE: min PWM + anty-stall
        static uint32_t stall_us[4] = {0u,0u,0u,0u};
        static uint32_t block_until_ms[4] = {0u,0u,0u,0u};

        if (on_use_like)
        {
            if (static_cast<int32_t>(block_until_ms[CHx] - now_ms) > 0)
            {
                PID_pressure.clear();
                pwm_zeroed = 1;
                x_prev[CHx] = 0;
                Motion_control_set_PWM(CHx, 0);
                return;
            }

            if (on_use_need_move && x != 0)
            {
                if (!on_use_linear)
                {
                    const int MIN_MOVE_PWM = (on_use_abs_err_q >= 130) ? 500 : 0;
                    if (MIN_MOVE_PWM)
                    {
                        const int xi = x;
                        const int ax = abs_i32(xi);
                        if (ax < MIN_MOVE_PWM)
                            x = (x > 0) ? MIN_MOVE_PWM : -MIN_MOVE_PWM;
                    }
                }
            }

            const bool motor_not_moving =
                (now_speed_q > -1000) && (now_speed_q < 1000);

            if (on_use_need_move && motor_not_moving &&
                (on_use_abs_err_q >= 200) && (abs_i32(x) >= 450))
            {
                uint32_t elapsed = stall_us[CHx] + dt_us;
                if (elapsed < stall_us[CHx] || elapsed > 800001u) elapsed = 800001u;
                stall_us[CHx] = elapsed;

                if (elapsed > 150000u)
                {
                    constexpr int32_t KICK_PWM = 850;
                    x = (x > 0) ? KICK_PWM : -KICK_PWM;
                }

                if (elapsed > 800000u)
                {
                    stall_us[CHx] = 0u;
                    block_until_ms[CHx] = now_ms + 500;
                    PID_pressure.clear();
                    pwm_zeroed = 1;
                    x_prev[CHx] = 0;
                    Motion_control_set_PWM(CHx, 0);
                    return;
                }
            }
            else
            {
                stall_us[CHx] = 0u;
            }
        }
        else
        {
            stall_us[CHx] = 0u;
            block_until_ms[CHx] = 0u;
        }

        if ((mode & kMotionRedetect) != 0u)
        {
            const int pwm_out = (int)x;
            pwm_zeroed = (pwm_out == 0);
            x_prev[CHx] = x;
            Motion_control_set_PWM(CHx, pwm_out);
            return;
        }

        const bool use_ramping =
            (((mode & kMotionSend) != 0u) && !send_stop_latch) ||
            ((mode & kMotionPull) != 0u);

        if (use_ramping)
        {
            const bool pull_soft_start =
                ((mode & kMotionPull) != 0u) &&
                (pull_start_ms != 0) &&
                ((now_ms - pull_start_ms) < 400u);

            uint32_t rate_up = pull_soft_start ? 2500u : 4500u;
            uint32_t rate_down = 6500u;

            if ((mode & kMotionSend) != 0u)
            {
                rate_down = 25000u;
                rate_up   = 18000u;
            }

            const uint32_t dt_ms_ceil = (dt_us + 999u) / 1000u;
            const int32_t max_step_up = static_cast<int32_t>(
                (rate_up * dt_ms_ceil + 999u) / 1000u);
            const int32_t max_step_down = static_cast<int32_t>(
                (rate_down * dt_ms_ceil + 999u) / 1000u);

            const int32_t prev = x_prev[CHx];
            const int32_t lo = prev - max_step_down;
            const int32_t hi = prev + max_step_up;

            if (x < lo) x = lo;
            if (x > hi) x = hi;
        }

        const int pwm_out0 = (int)x;

        if ((mode & kMotionPressureOnUse) != 0u && !g_on_use_low_latch[CHx])
        {
            if (MC_ONLINE_key_stu[CHx] == 0u)
            {
                g_on_use_hi_pwm_us[CHx] = 0u;
            }
            else
            {
                if (MC_PULL_pct_q[CHx] < 4000)
                {
                    g_on_use_low_latch[CHx] = 1u;
                    g_on_use_jam_latch[CHx] = 1u;
                }
                else
                {
                    const int pwm_cmd = pwm_out0;
                    const int ax = (pwm_cmd < 0) ? -pwm_cmd : pwm_cmd;

                    const bool push_hi =
                        motor_polarity_valid &&
                        (to_logical_pwm(pwm_cmd) < 0) &&
                        (ax > 800);

                    if (push_hi)
                    {
                        uint32_t t1 = g_on_use_hi_pwm_us[CHx] + dt_us;
                        if (t1 > 20000000u) t1 = 20000000u;
                        g_on_use_hi_pwm_us[CHx] = t1;

                        if (t1 >= 20000000u)
                        {
                            g_on_use_low_latch[CHx] = 1u;
                            g_on_use_jam_latch[CHx] = 0u;
                        }
                    }
                    else
                    {
                        g_on_use_hi_pwm_us[CHx] = 0u;
                    }
                }

                if (__builtin_expect(g_on_use_low_latch[CHx] != 0u, 0))
                {
                    g_on_use_hi_pwm_us[CHx] = 0u;

                    auto &A = ams[motion_control_ams_num];
                    if (g_on_use_jam_latch[CHx] && A.now_filament_num == (uint8_t)CHx)
                        bmcu_set_pressure(A, 0xF06Fu);

                    MC_STU_RGB_set(CHx, 0xFF, 0x00, 0x00);

                    PID_speed.clear();
                    PID_pressure.clear();
                    pwm_zeroed = 1;
                    x_prev[CHx] = 0;
                    Motion_control_set_PWM(CHx, 0);
                    return;
                }
            }
        }
        else
        {
            g_on_use_hi_pwm_us[CHx] = 0u;
        }

        const int pwm_out = pwm_out0;
        pwm_zeroed = (pwm_out == 0);
        x_prev[CHx] = x;
        Motion_control_set_PWM(CHx, pwm_out);
    }
};

_MOTOR_CONTROL MOTOR_CONTROL[4] = {_MOTOR_CONTROL(0), _MOTOR_CONTROL(1), _MOTOR_CONTROL(2), _MOTOR_CONTROL(3)};
int32_t _MOTOR_CONTROL::x_prev[4] = {0,0,0,0};

void Motion_control_set_PWM(uint8_t CHx, int PWM)
{
    if (CHx >= kChCount) return;
    if (PWM > PWM_lim) PWM = PWM_lim;
    if (PWM < -PWM_lim) PWM = -PWM_lim;
    g_motor_pwm[CHx] = static_cast<int16_t>(PWM);

    uint16_t set1 = 0, set2 = 0;

    if (PWM > 0)       set1 = (uint16_t)PWM;
    else if (PWM < 0)  set2 = (uint16_t)(-PWM);
    else { set1 = 1000; set2 = 1000; }

    switch (CHx)
    {
    case 3:
        TIM_SetCompare1(TIM2, set1);
        TIM_SetCompare2(TIM2, set2);
        break;
    case 2:
        TIM_SetCompare1(TIM3, set1);
        TIM_SetCompare2(TIM3, set2);
        break;
    case 1:
        TIM_SetCompare1(TIM4, set1);
        TIM_SetCompare2(TIM4, set2);
        break;
    case 0:
        TIM_SetCompare3(TIM4, set1);
        TIM_SetCompare4(TIM4, set2);
        break;
    default:
        break;
    }
}

// ===== AS5600 distance/speed =====
int32_t as5600_distance_save[4] = {0,0,0,0};

void AS5600_distance_updata(uint32_t now_ticks)
{
    static uint32_t last_ticks = 0u;
    static uint32_t last_poll_ticks = 0u;
    static uint8_t  have_last_ticks = 0u;
    static uint8_t  was_ok[4] = {0,0,0,0};
    static uint32_t last_stu_ticks = 0u;

    uint32_t tpm = time_hw_tpms;
    if (!tpm) tpm = 1u;

    uint32_t tpus = time_hw_tpus;
    if (!tpus) tpus = 1u;

    uint32_t min_poll_ticks = tpm;
    if ((uint32_t)(now_ticks - last_poll_ticks) < min_poll_ticks)
        return;

    last_poll_ticks = now_ticks;

    if ((uint32_t)(now_ticks - last_stu_ticks) >= (200u * tpm))
    {
        last_stu_ticks = now_ticks;
        MC_AS5600.updata_stu();
    }

    if (!have_last_ticks)
    {
        last_ticks = now_ticks;
        have_last_ticks = 1u;
        return;
    }

    const uint32_t dt_ticks = (uint32_t)(now_ticks - last_ticks);
    if (dt_ticks == 0u) return;
    last_ticks = now_ticks;

    uint32_t dt_us = dt_ticks / tpus;
    if (dt_us == 0u) dt_us = 1u;
    if (dt_us > 200000u) dt_us = 200000u;
    constexpr int32_t kAS5600_UM_PER_CNT_Q8 = -1473;
    constexpr uint32_t kSpeedReciprocalNumeratorQ20 = 4096000000u;
    const uint32_t speed_scale_q20 =
        (kSpeedReciprocalNumeratorQ20 + dt_us / 2u) / dt_us;

    MC_AS5600.updata_angle();

    for (uint8_t i = 0; i < kChCount; i++)
    {
        const bool ok_now = MC_AS5600.online[i] && (MC_AS5600.magnet_stu[i] != AS5600_soft_IIC_many::offline);

        if (ok_now)
        {
            g_as5600_fail[i] = 0;
            if (g_as5600_okstreak[i] < 255u) g_as5600_okstreak[i]++;
            if (g_as5600_okstreak[i] >= kAS5600_OK_RECOVER) g_as5600_good[i] = 1u;
        }
        else
        {
            g_as5600_okstreak[i] = 0u;
            if (g_as5600_fail[i] < 255u) g_as5600_fail[i]++;
            if (g_as5600_fail[i] >= kAS5600_FAIL_TRIP) g_as5600_good[i] = 0u;
        }

        if (!AS5600_is_good(i))
        {
            was_ok[i] = 0u;
            speed_as5600_q[i] = 0;
            g_as5600_last_delta[i] = 0;
            continue;
        }

        if (!was_ok[i])
        {
            as5600_distance_save[i] = MC_AS5600.raw_angle[i];
            speed_as5600_q[i] = 0;
            g_as5600_last_delta[i] = 0;
            was_ok[i] = 1u;
            continue;
        }

        const int32_t last = as5600_distance_save[i];
        const int32_t now  = MC_AS5600.raw_angle[i];

        int32_t diff = now - last;
        if (diff > 2048) diff -= 4096;
        if (diff < -2048) diff += 4096;

        as5600_distance_save[i] = now;
        g_as5600_last_delta[i] = static_cast<int16_t>(diff);
        g_filament_position_counts[i] += diff;

        auto &motor = MOTOR_CONTROL[i];
        motor.directed_progress_counts +=
            (diff ^ motor.progress_negate_mask) - motor.progress_negate_mask;
        motor.travel_budget_counts += delta_magnitude_counts(diff);

        // Future safety hardening: stop after reverse directed displacement
        // persists beyond a validated count/time window. Until then, reverse
        // motion reduces progress and can never satisfy the completion target.


        const int32_t speed_numerator_q8 =
            diff * kAS5600_UM_PER_CNT_Q8;
        const int64_t speed_product_q20 =
            static_cast<int64_t>(speed_numerator_q8) * speed_scale_q20;
        speed_as5600_q[i] = speed_product_q20 >= 0
            ? static_cast<int32_t>((speed_product_q20 + (1ll << 19)) >> 20)
            : -static_cast<int32_t>(((-speed_product_q20) + (1ll << 19)) >> 20);

    }
}

// ===== stany logiki filamentu =====
enum filament_now_position_enum
{
    filament_idle,
    filament_sending_out,
    filament_using,
    filament_before_pull_back,
    filament_pulling_back,
    filament_redetect,
};

static filament_now_position_enum filament_now_position[4];

static int32_t filament_pull_back_target_counts[4] = {
    distance_m_to_counts(motion_control_pull_back_distance),
    distance_m_to_counts(motion_control_pull_back_distance),
    distance_m_to_counts(motion_control_pull_back_distance),
    distance_m_to_counts(motion_control_pull_back_distance)
};

// BEFORE_PULLBACK distance remains in native encoder counts. Direction is learned
// after a small displacement so sensor orientation does not alter semantics.
static int32_t before_pb_last_counts[4]      = {0,0,0,0};
static int32_t before_pb_retracted_counts[4] = {0,0,0,0};
static int8_t  before_pb_sign[4]             = {0,0,0,0};
static constexpr int32_t BEFORE_PB_SIGN_COUNTS = distance_m_to_counts(0.0005f);
static constexpr int32_t BEFORE_PB_MAX_COUNTS  = distance_m_to_counts(2.0f);

// Redetect pushes filament back toward the online key and waits for it to
// register. If the filament is withdrawn past the switches while this runs the
// key never reports, so the state needs a bound of its own -- without one the
// channel drives indefinitely at the fixed output filament_motion_redetect
// commands, keeps reporting online to the printer, and keeps itself out of
// motor_motion_switch's dispatch.
//
// The bound counts time spent driving, not time in the state: it is armed in
// the drive branch and disarmed whenever driving stops. Generous relative to
// the travel involved, which is millimetres at near-full PWM -- this is a
// backstop for filament that is no longer there, not a limit on a redetect
// that is making progress.
static constexpr uint32_t REDETECT_TIMEOUT_MS = 10000u;
static uint32_t redetect_t0_ms[4] = {0u,0u,0u,0u};

static __attribute__((noinline, cold)) void latch_pull_fault(uint8_t channel, uint8_t fault, uint32_t time_now)
{
    auto &A = ams[motion_control_ams_num];
    auto &motor = MOTOR_CONTROL[channel];

    const uint8_t previous_fault = motor.motion_fault;
    motor.motion_fault = fault;
    bmcu_link_motion_fault(channel, previous_fault, fault);
    motor.set_motion(filament_motion_enum::filament_motion_stop, 100, time_now);
    filament_now_position[channel] = filament_idle;
    redetect_t0_ms[channel] = 0u;
    g_pull_remain_counts[channel] = 0;
    g_pull_speed_set_q[channel] = -PULL_V_FAST_Q;
    g_pull_pwm_floor[channel] = PULL_PWM_MIN;

    // Match the successful exits, which clear this alongside the motion. Left
    // set, the reply builder keeps transmitting the pull_back flag while the
    // motion beside it reads idle, so the printer sees the two contradict
    // until its next command overwrites the flag.
    A.filament_use_flag = 0x00;

    if (A.filament[channel].motion != _filament_motion::idle)
    {
        A.filament[channel].motion = _filament_motion::idle;
        bmcu_link_status_changed(BMCU_STATUS_CHANGE_MOTION);
    }
    MC_STU_RGB_set_latch(channel, 0xFFu, 0x00u, 0x00u, time_now, 1u);
}

// Returns the set of channels this sequencer is driving, one bit per channel.
// Those channels own their motor for the cycle and must not also be dispatched
// by motor_motion_switch; the rest still must be. The caller previously
// collapsed this to a single bool and skipped the dispatch entirely, which let
// one channel's pull-back freeze the other three.
static uint8_t motor_motion_filamnet_pull_back_to_online_key(uint32_t time_now)
{
    uint8_t held = 0u;
    auto &A = ams[motion_control_ams_num];

    for (uint8_t i = 0; i < kChCount; i++)
    {
        switch (filament_now_position[i])
        {
        case filament_pulling_back:
        {
            MC_STU_RGB_set_latch(i, 0xFFu, 0x00u, 0xFFu, time_now, 1u);

            auto &motor = MOTOR_CONTROL[i];
            const int32_t target_counts = filament_pull_back_target_counts[i];
            const int32_t progress_counts = motor.directed_progress_counts;
            constexpr int32_t PULL_PROGRESS_QUANTUM_COUNTS = distance_m_to_counts(0.0005f);
            constexpr uint32_t PULL_BUDGET_FLOOR_COUNTS =
                static_cast<uint32_t>(distance_m_to_counts(0.050f));
            constexpr uint32_t PULL_NO_PROGRESS_TIMEOUT_MS = 3000u;

            if (target_counts <= 0 || progress_counts >= target_counts)
            {
                g_pull_remain_counts[i] = 0;
                g_pull_speed_set_q[i] = -PULL_V_FAST_Q;
                g_pull_pwm_floor[i] = PULL_PWM_MIN;
                motor.set_motion(filament_motion_enum::filament_motion_stop, 100, time_now);
                filament_pull_back_target_counts[i] =
                    distance_m_to_counts(motion_control_pull_back_distance);
                filament_now_position[i] = filament_redetect;
            }
            else if (MC_ONLINE_key_stu[i] == 0)
            {
                g_pull_remain_counts[i] = 0;
                g_pull_speed_set_q[i] = -PULL_V_FAST_Q;
                g_pull_pwm_floor[i] = PULL_PWM_MIN;
                motor.set_motion(filament_motion_enum::filament_motion_stop, 100, time_now);
                filament_pull_back_target_counts[i] =
                    distance_m_to_counts(motion_control_pull_back_distance);
                filament_now_position[i] = filament_redetect;
            }
            else
            {
                if (progress_counts - motor.pull_progress_checkpoint_counts >=
                    PULL_PROGRESS_QUANTUM_COUNTS)
                {
                    motor.pull_progress_checkpoint_counts = progress_counts;
                    motor.pull_progress_checkpoint_ms = time_now;
                }

                const uint32_t target_budget = static_cast<uint32_t>(target_counts);
                const uint32_t budget_margin = target_budget > PULL_BUDGET_FLOOR_COUNTS
                    ? target_budget : PULL_BUDGET_FLOOR_COUNTS;
                const uint32_t pull_budget = target_budget + budget_margin;

                if (motor.travel_budget_counts >= pull_budget)
                {
                    latch_pull_fault(i, MOTION_FAULT_PULL_TRAVEL_BUDGET, time_now);
                }
                else if (time_now - motor.pull_progress_checkpoint_ms >=
                         PULL_NO_PROGRESS_TIMEOUT_MS)
                {
                    latch_pull_fault(i, MOTION_FAULT_PULL_NO_PROGRESS, time_now);
                }
                else
                {
                    const int32_t remain_counts = target_counts - progress_counts;
                    constexpr int32_t ramp_counts = distance_m_to_counts(PULL_RAMP_M);
                    int32_t ramped_counts = remain_counts;
                    if (ramped_counts < 0) ramped_counts = 0;
                    if (ramped_counts > ramp_counts) ramped_counts = ramp_counts;
                    g_pull_remain_counts[i] = remain_counts;

                    const int32_t speed_q = PULL_V_END_Q +
                        ((PULL_V_FAST_Q - PULL_V_END_Q) * ramped_counts +
                         ramp_counts / 2) / ramp_counts;
                    g_pull_speed_set_q[i] = -speed_q;

                    const int32_t pwm_span =
                        static_cast<int32_t>(motor.pwm_zero) - PULL_PWM_MIN;
                    g_pull_pwm_floor[i] = static_cast<int16_t>(
                        PULL_PWM_MIN +
                        (pwm_span * ramped_counts + ramp_counts / 2) / ramp_counts);
                    motor.set_motion(filament_motion_enum::filament_motion_pull, 100, time_now);
                }
            }

            held |= static_cast<uint8_t>(1u << i);
            break;
        }

        case filament_redetect:
        {
            MC_STU_RGB_set_latch(i, 0xFFu, 0xFFu, 0x00u, time_now, 0u);

            if (MC_ONLINE_key_stu[i] != 0)
            {
                MOTOR_CONTROL[i].set_motion(filament_motion_enum::filament_motion_stop, 100, time_now);
                filament_now_position[i] = filament_idle;
                redetect_t0_ms[i] = 0u;

                A.filament_use_flag = 0x00;
                if (A.filament[i].motion != _filament_motion::idle)
                {
                    A.filament[i].motion = _filament_motion::idle;
                    bmcu_link_status_changed(BMCU_STATUS_CHANGE_MOTION);
                }
            }
            else if (redetect_t0_ms[i] != 0u &&
                     (time_now - redetect_t0_ms[i]) >= REDETECT_TIMEOUT_MS)
            {
                // The key never reported. Treat it as filament that is gone
                // rather than pushing forever: latch_pull_fault stops the
                // motor, returns the channel to idle and clears the AMS-side
                // motion, which lets online fall away and releases the hold.
                redetect_t0_ms[i] = 0u;
                latch_pull_fault(i, MOTION_FAULT_REDETECT_TIMEOUT, time_now);
                break;
            }
            else
            {
                // Armed here rather than on entry so the bound measures time
                // spent driving. A bus error skips this sequencer entirely and
                // parks every motor, and that dead time must not be charged
                // against a redetect that has not been given a chance to push.
                if (redetect_t0_ms[i] == 0u)
                    redetect_t0_ms[i] = time_now ? time_now : 1u;
                MOTOR_CONTROL[i].set_motion(filament_motion_enum::filament_motion_redetect, 100, time_now);
            }

            held |= static_cast<uint8_t>(1u << i);
            break;
        }

        default:
            break;
        }
    }

    return held;
}

// held_mask names the channels the pull-back sequencer is driving this cycle.
// The loop below is per-channel -- every write it makes is indexed by i, and
// the only cross-channel values, num and motion, are read once before it and
// never written -- so leaving a channel out is well defined: that channel
// simply keeps the motion the sequencer just gave it, and the others are
// dispatched exactly as they would be with nothing held.
//
// A held channel must be left out whether or not it is the selected slot. When
// it is not selected, the i != num branch would set it back to filament_idle
// and stop it, aborting a pull-back the printer has already moved on from --
// which finishing after the selection changes is the normal case. When it is
// selected, the pull_back arm would re-stamp the target counts and the
// before_pb accumulators every cycle, so the sequencer's progress would never
// reach a target that keeps being reset.
static void motor_motion_switch(uint32_t time_now, uint8_t held_mask)
{
    auto &A = ams[motion_control_ams_num];

    const uint8_t num = A.now_filament_num;
    const _filament_motion motion = (num < kChCount) ? A.filament[num].motion : _filament_motion::idle;

    for (uint8_t i = 0; i < kChCount; i++)
    {
        if (held_mask & static_cast<uint8_t>(1u << i)) continue;

        auto &motor = MOTOR_CONTROL[i];
        if (motor.motion_fault != MOTION_FAULT_NONE)
        {
            const bool explicit_pull_retry =
                (i == num) && (motion == _filament_motion::pull_back);
            if (explicit_pull_retry)
            {
                const uint8_t previous_fault = motor.motion_fault;
                motor.motion_fault = MOTION_FAULT_NONE;
                bmcu_link_motion_fault(i, previous_fault, MOTION_FAULT_NONE);
            }
            else
            {
                filament_now_position[i] = filament_idle;
                motor.set_motion(filament_motion_enum::filament_motion_stop, 100, time_now);
                continue;
            }
        }

        if (i != num)
        {
            filament_now_position[i] = filament_idle;

            if (filament_channel_inserted[i] && (MC_ONLINE_key_stu[i] != 0 || g_last_on_use_exit_ms[i] != 0))
                MOTOR_CONTROL[i].set_motion(filament_motion_enum::filament_motion_pressure_ctrl_idle, 1000, time_now);
            else
                MOTOR_CONTROL[i].set_motion(filament_motion_enum::filament_motion_stop, 1000, time_now);

            continue;
        }

        if (num >= kChCount) continue;

        if (MC_ONLINE_key_stu[num] != 0)
        {
            switch (motion)
            {
            case _filament_motion::before_on_use:
            {
                filament_now_position[num] = filament_using;
                MOTOR_CONTROL[num].set_motion(filament_motion_enum::filament_motion_before_on_use, 300, time_now);
                MC_STU_RGB_set_latch(num, 0xFFu, 0xFFu, 0x00u, time_now, 0u);
                break;
            }

            case _filament_motion::stop_on_use:
            {
                filament_now_position[num] = filament_using;
                MOTOR_CONTROL[num].set_motion(filament_motion_enum::filament_motion_stop_on_use, 300, time_now);
                MC_STU_RGB_set_latch(num, 0xFFu, 0x00u, 0x00u, time_now, 0u);
                break;
            }

            case _filament_motion::send_out:
            {
                if (g_on_use_jam_latch[num])
                {
                    if (MC_PULL_pct_q[num] > 8500)
                    {
                        g_on_use_low_latch[num] = 0u;
                        g_on_use_jam_latch[num] = 0u;
                        g_on_use_hi_pwm_us[num] = 0u;
                    }
                    else
                    {
                        MOTOR_CONTROL[num].set_motion(filament_motion_enum::filament_motion_stop, 100, time_now);
                        MC_STU_RGB_set_latch(num, 0x00u, 0xD5u, 0x2Au, time_now, 0u);
                        break;
                    }
                }

                MC_STU_RGB_set_latch(num, 0x00u, 0xD5u, 0x2Au, time_now, 0u);
                filament_now_position[num] = filament_sending_out;
                MOTOR_CONTROL[num].set_motion(filament_motion_enum::filament_motion_send, 100, time_now);
                break;
            }

            case _filament_motion::pull_back:
            {
                MC_STU_RGB_set_latch(num, 0xA0u, 0x2Du, 0xFFu, time_now, 1u);
                filament_now_position[num] = filament_pulling_back;

                constexpr int32_t normal_target_counts =
                    distance_m_to_counts(motion_control_pull_back_distance);
                int32_t target_counts;
                if (g_on_use_jam_latch[num])
                {
                    target_counts = distance_m_to_counts(0.100f);
                }
                else
                {
                    target_counts = normal_target_counts -
                        before_pb_retracted_counts[num];
                    if (target_counts < 0) target_counts = 0;
                    if (target_counts > normal_target_counts)
                        target_counts = normal_target_counts;
                }

                filament_pull_back_target_counts[num] = target_counts;

                g_pull_remain_counts[num] = target_counts;
                g_pull_speed_set_q[num] = -PULL_V_FAST_Q;
                g_pull_pwm_floor[num] = static_cast<int16_t>(MOTOR_CONTROL[num].pwm_zero);

                before_pb_retracted_counts[num] = 0;
                before_pb_sign[num]             = 0;
                before_pb_last_counts[num]      = g_filament_position_counts[num];

                MOTOR_CONTROL[num].set_motion(filament_motion_enum::filament_motion_pull, 100, time_now);
                break;
            }

            case _filament_motion::before_pull_back:
            {
                MC_STU_RGB_set_latch(num, 0xFFu, 0xA0u, 0x00u, time_now, 1u);

                if (filament_now_position[num] != filament_before_pull_back)
                {
                    filament_now_position[num] = filament_before_pull_back;
                    before_pb_last_counts[num]      = g_filament_position_counts[num];
                    before_pb_retracted_counts[num] = 0;
                    before_pb_sign[num]             = 0;
                }

                {
                    const int32_t position_counts = g_filament_position_counts[num];
                    const int32_t delta_counts =
                        position_counts - before_pb_last_counts[num];
                    before_pb_last_counts[num] = position_counts;

                    if (MC_PULL_pct_q[num] > 5025)
                    {
                        if (before_pb_sign[num] == 0 &&
                            static_cast<int32_t>(delta_magnitude_counts(delta_counts)) >
                                BEFORE_PB_SIGN_COUNTS)
                        {
                            before_pb_sign[num] = (delta_counts >= 0) ? 1 : -1;
                        }

                        const int32_t directed_delta =
                            delta_counts * static_cast<int32_t>(before_pb_sign[num]);
                        if (directed_delta > 0)
                        {
                            int32_t total = before_pb_retracted_counts[num] +
                                directed_delta;
                            if (total > BEFORE_PB_MAX_COUNTS)
                                total = BEFORE_PB_MAX_COUNTS;
                            before_pb_retracted_counts[num] = total;
                        }
                    }
                }

                MOTOR_CONTROL[num].set_motion(filament_motion_enum::filament_motion_before_pull_back, 300, time_now);
                break;
            }

            case _filament_motion::on_use:
            {
                filament_now_position[num] = filament_using;
                MOTOR_CONTROL[num].set_motion(filament_motion_enum::filament_motion_pressure_ctrl_on_use, 300, time_now);
                MC_STU_RGB_set_latch(num, 0x00u, 0xB0u, 0xFFu, time_now, 0u);
                break;
            }

            case _filament_motion::idle:
            default:
            {
                filament_now_position[num] = filament_idle;

                if (g_on_use_jam_latch[num])
                {
                    MOTOR_CONTROL[num].set_motion(filament_motion_enum::filament_motion_stop, 100, time_now);
                    MC_STU_RGB_set_latch(num, 0x38u, 0x35u, 0x32u, time_now, 0u);
                    break;
                }

                MOTOR_CONTROL[num].set_motion(filament_motion_enum::filament_motion_pressure_ctrl_idle, 100, time_now);

#if BMCU_DM_TWO_MICROSWITCH
                if (dm_fail_latch[num])      MC_STU_RGB_set_latch(num, 0xFFu, 0x00u, 0x00u, time_now, 0u);
                else if (dm_loaded[num])     MC_STU_RGB_set_latch(num, 0x38u, 0x35u, 0x32u, time_now, 0u);
                else                         MC_STU_RGB_set_latch(num, 0x00u, 0x00u, 0x00u, time_now, 0u);
#else
                MC_STU_RGB_set_latch(num, 0x38u, 0x35u, 0x32u, time_now, 0u);
#endif
                break;
            }
            }
        }
        else
        {
            filament_now_position[num] = filament_idle;
            MOTOR_CONTROL[num].set_motion(filament_motion_enum::filament_motion_pressure_ctrl_idle, 100, time_now);
            MC_STU_RGB_set_latch(num, 0x00u, 0x00u, 0x00u, time_now, 0u);
        }
    }
}

static inline void stu_apply_baseline(int error, uint32_t now_ms)
{
    for (uint8_t i = 0; i < kChCount; i++)
    {
        if (g_on_use_low_latch[i])
        {
            MC_STU_RGB_set(i, 0xFFu, 0x00u, 0x00u);
            continue;
        }

#if BMCU_DM_TWO_MICROSWITCH
        if (dm_fail_latch[i])
        {
            MC_STU_RGB_set(i, 0xFFu, 0x00u, 0x00u);
            continue;
        }

        const bool ins_ok = error ? true : filament_channel_inserted[i];
        const bool show_loaded =
            (dm_loaded[i] != 0u) &&
            (MC_ONLINE_key_stu[i] != 0u) &&
            ins_ok;

        if (show_loaded) MC_STU_RGB_set_latch(i, 0x38u, 0x35u, 0x32u, now_ms, 0u);
        else             MC_STU_RGB_set_latch(i, 0x00u, 0x00u, 0x00u, now_ms, 0u);
#else
        if (error)
        {
            if (MC_ONLINE_key_stu[i] != 0) MC_STU_RGB_set_latch(i, 0x38u, 0x35u, 0x32u, now_ms, 0u);
            else                           MC_STU_RGB_set_latch(i, 0x00u, 0x00u, 0x00u, now_ms, 0u);
        }
        else
        {
            if (MC_ONLINE_key_stu[i] != 0 && filament_channel_inserted[i])
                MC_STU_RGB_set_latch(i, 0x38u, 0x35u, 0x32u, now_ms, 0u);
            else
                MC_STU_RGB_set_latch(i, 0x00u, 0x00u, 0x00u, now_ms, 0u);
        }
#endif
    }
}


static void motor_motion_run(int error, uint32_t time_now, uint32_t now_ticks)
{
#if BMCU_DM_TWO_MICROSWITCH
    for (uint8_t ch = 0; ch < kChCount; ch++)
    {
        if (!filament_channel_inserted[ch])
        {
            dm_loaded[ch]            = 1u;
            dm_fail_latch[ch]        = 0u;
            dm_auto_state[ch]        = DM_AUTO_IDLE;
            dm_auto_try[ch]          = 0u;
            dm_auto_t0_ms[ch]        = 0u;
            dm_auto_remain_counts[ch] = 0;
            dm_auto_last_counts[ch]   = 0;
            dm_loaded_drop_t0_ms[ch] = 0u;
            dm_autoload_gate[ch]     = 0u;
            continue;
        }

        const uint8_t ks = MC_ONLINE_key_stu[ch];

        if (ks == 0u)
        {
            if (filament_now_position[ch] == filament_idle)
                dm_autoload_gate[ch] = 0u;

            dm_loaded[ch]            = 0u;
            dm_fail_latch[ch]        = 0u;
            dm_auto_state[ch]        = DM_AUTO_IDLE;
            dm_auto_try[ch]          = 0u;
            dm_auto_t0_ms[ch]        = 0u;
            dm_auto_remain_counts[ch] = 0;
            dm_auto_last_counts[ch]   = 0;
            dm_loaded_drop_t0_ms[ch] = 0u;
            continue;
        }

        // held_for clears the stamp both on the firing pass and on any pass
        // where the condition is false, which is what the else branch used to
        // do by hand.
        if (signal_hold::held_for(dm_loaded_drop_t0_ms[ch],
                                  dm_loaded[ch] && (ks != 1u),
                                  time_now, DM_LOADED_DROP_MS))
        {
            dm_loaded[ch]            = 0u;

            dm_auto_state[ch]    = DM_AUTO_IDLE;
            dm_auto_try[ch]      = 0u;
            dm_auto_t0_ms[ch]    = 0u;
            dm_auto_remain_counts[ch] = 0;
            dm_auto_last_counts[ch]   = 0;
        }
    }
#endif

    static uint32_t last_ticks = 0u;
    static uint8_t  have_last_ticks = 0u;

    uint32_t dt_ticks = 0u;
    if (!have_last_ticks)
    {
        have_last_ticks = 1u;
    }
    else
    {
        dt_ticks = (uint32_t)(now_ticks - last_ticks);
    }
    last_ticks = now_ticks;

    uint32_t tpm = time_hw_tpms;
    if (!tpm) tpm = 1u;

    const uint32_t max_dt_ticks = 200u * tpm;
    if (dt_ticks > max_dt_ticks) dt_ticks = max_dt_ticks;

    uint32_t tpus = time_hw_tpus;
    if (!tpus) tpus = 1u;

    const bool have_time_step = (dt_ticks != 0u);
    const uint32_t dt_us = have_time_step ? (dt_ticks / tpus) : 0u;

    stu_apply_baseline(error, time_now);

#if BMCU_ONLINE_LED_FILAMENT_RGB
    auto &Acol = ams[motion_control_ams_num];
#endif

    if (!error)
    {
        const uint8_t held = motor_motion_filamnet_pull_back_to_online_key(time_now);
        motor_motion_switch(time_now, held);
    }
    else
    {
        for (uint8_t i = 0; i < kChCount; i++)
        {
            MOTOR_CONTROL[i].set_motion(filament_motion_enum::filament_motion_stop, 100, time_now);
            // The sequencer above is skipped while the bus is in error, so a
            // channel left in redetect stops driving but keeps its position.
            // Disarm the bound; it re-arms when driving resumes.
            redetect_t0_ms[i] = 0u;
        }
    }

    for (uint8_t i = 0; i < kChCount; i++)
    {
        if (!AS5600_is_good(i))
        {
            MOTOR_CONTROL[i].set_motion(filament_motion_enum::filament_motion_stop, 100, time_now);
            Motion_control_set_PWM(i, 0);
            continue;
        }

        if (!filament_channel_inserted[i] ||
            (!auto_unload_active[i] && MOTOR_CONTROL[i].motion != filament_motion_enum::filament_motion_pressure_ctrl_idle))
        {
            auto_unload_arm[i]          = 0u;
            auto_unload_active[i]       = 0u;
            auto_unload_blocked[i]      = 0u;
            auto_unload_arm_t0_ms[i]    = 0u;
            auto_unload_active_t0_ms[i] = 0u;
            auto_unload_empty_t0_ms[i]  = 0u;
        }
        else
        {
            const int16_t pct_q = MC_PULL_pct_q[i];
            const uint8_t ks = MC_ONLINE_key_stu[i];

            if (pct_q >= static_cast<int32_t>(AUTO_UNLOAD_START_PCT * 100.0f))
            {
                auto_unload_blocked[i] = 0u;

                if (!auto_unload_arm[i] && !auto_unload_active[i])
                {
                    auto_unload_arm[i] = 1u;
                    auto_unload_arm_t0_ms[i] = time_now;
                }
            }

            if (auto_unload_arm[i] && !auto_unload_active[i])
            {
                const uint32_t dt = time_now - auto_unload_arm_t0_ms[i];

                constexpr int32_t neutral_lo_q =
                    static_cast<int32_t>(AUTO_UNLOAD_NEUTRAL_LO_PCT * 100.0f) + 1;
                constexpr int32_t neutral_hi_q =
                    static_cast<int32_t>(AUTO_UNLOAD_NEUTRAL_HI_PCT * 100.0f) - 1;
                if (in_closed_range_i32(pct_q, neutral_lo_q, neutral_hi_q))
                {
                    if (!auto_unload_blocked[i] && dt <= AUTO_UNLOAD_ARM_MS)
                    {
                        auto_unload_active[i]       = 1u;
                        auto_unload_active_t0_ms[i] = time_now;
                        auto_unload_empty_t0_ms[i]  = 0u;
                        auto_unload_blocked[i]      = 1u;
                    }

                    auto_unload_arm[i]       = 0u;
                    auto_unload_arm_t0_ms[i] = 0u;
                }
                else if (dt > AUTO_UNLOAD_ARM_MS)
                {
                    auto_unload_arm[i]       = 0u;
                    auto_unload_arm_t0_ms[i] = 0u;
                }
            }

            if (auto_unload_active[i])
            {
                if (pct_q < static_cast<int32_t>(AUTO_UNLOAD_ABORT_PCT * 100.0f))
                {
                    auto_unload_active[i]       = 0u;
                    auto_unload_active_t0_ms[i] = 0u;
                    auto_unload_empty_t0_ms[i]  = 0u;
                    auto_unload_blocked[i]      = 1u;
                }
                else
                {
                    // ks == 1 is the channel still reading filament, which both
                    // resets the empty window and is where the operation's own
                    // AUTO_UNLOAD_MAX_MS timeout is checked. held_for does the
                    // reset, so the branch below only has to keep the split.
                    const bool empty = (ks != 1u);
                    const bool empty_held =
                        signal_hold::held_for(auto_unload_empty_t0_ms[i], empty,
                                              time_now, AUTO_UNLOAD_EMPTY_MS);

                    if (!empty)
                    {
                        if ((time_now - auto_unload_active_t0_ms[i]) >= AUTO_UNLOAD_MAX_MS)
                        {
                            auto_unload_active[i]       = 0u;
                            auto_unload_active_t0_ms[i] = 0u;
                            auto_unload_empty_t0_ms[i]  = 0u;
                            auto_unload_blocked[i]      = 1u;
                        }
                    }
                    else if (empty_held)
                    {
                        auto_unload_active[i]       = 0u;
                        auto_unload_active_t0_ms[i] = 0u;
                        auto_unload_empty_t0_ms[i]  = 0u;
                        auto_unload_blocked[i]      = 1u;
                    }
                }
            }
        }

        const bool manual_empty_pull =
            filament_channel_inserted[i] &&
            (MC_ONLINE_key_stu[i] == 0u) &&
            (MC_PULL_pct_q[i] > 8000) &&
            (auto_unload_active[i] == 0u);

        if (auto_unload_active[i])
        {
            int32_t x = MOTOR_CONTROL[i].motor_polarity_valid
                ? MOTOR_CONTROL[i].apply_motor_polarity(AUTO_UNLOAD_PWM_PULL) : 0;
            if (MOTOR_CONTROL[i].to_logical_pwm(x) < 0) x = 0;

            MOTOR_CONTROL[i].PID_speed.clear();
            MOTOR_CONTROL[i].PID_pressure.clear();
            MOTOR_CONTROL[i].pwm_zeroed = (x == 0) ? 1u : 0u;
            _MOTOR_CONTROL::x_prev[i] = x;

            Motion_control_set_PWM(i, (int)x);
            MC_STU_RGB_set_latch(i, 0xA0u, 0x2Du, 0xFFu, time_now, 1u);
        }
        else if (manual_empty_pull)
        {
            int32_t x = MOTOR_CONTROL[i].motor_polarity_valid
                ? MOTOR_CONTROL[i].apply_motor_polarity(700) : 0;
            if (MOTOR_CONTROL[i].to_logical_pwm(x) < 0) x = 0;

            MOTOR_CONTROL[i].PID_speed.clear();
            MOTOR_CONTROL[i].PID_pressure.clear();
            MOTOR_CONTROL[i].pwm_zeroed = (x == 0) ? 1u : 0u;
            _MOTOR_CONTROL::x_prev[i] = x;

            Motion_control_set_PWM(i, (int)x);
        }
        else if (have_time_step)
        {
            MOTOR_CONTROL[i].run(dt_us, time_now);
        }

        uint8_t r = 0u, g = 0u, b = 0u;
        bool is_filament_rgb = false;

        const uint8_t pct = MC_PULL_pct[i];

        int hi_thr = MC_PULL_DEADBAND_PCT_HIGH;

        const filament_motion_enum m = MOTOR_CONTROL[i].motion;

        bool hi_hold =
            (m == filament_motion_enum::filament_motion_send) ||
            (m == filament_motion_enum::filament_motion_before_on_use) ||
            (m == filament_motion_enum::filament_motion_stop_on_use);

        if (!hi_hold && (m == filament_motion_enum::filament_motion_pressure_ctrl_on_use))
        {
            const uint32_t t0 = MOTOR_CONTROL[i].on_use_hi_gate_t0_ms;
            if (t0 != 0u && (time_now - t0) < 5000u) hi_hold = true;
        }

        if (hi_hold)
        {
            hi_thr = (int)MC_LOAD_S2_HOLD_TARGET_PCT + 3;
            if (hi_thr > 100) hi_thr = 100;
            if (hi_thr < 0) hi_thr = 0;
        }

        if (!(m == filament_motion_enum::filament_motion_before_on_use) && (int)pct >= hi_thr)
        {
            r = 0x10u;
        }
        else if (pct <= 30u)
        {
            b = 0x10u;
        }
        else
        {
            const uint8_t key = MC_ONLINE_key_stu[i];

#if BMCU_ONLINE_LED_FILAMENT_RGB
    #if BMCU_DM_TWO_MICROSWITCH
            const bool show_filament_rgb = (key == 1u) && dm_loaded[i] && !dm_fail_latch[i];
    #else
            const bool show_filament_rgb = (key != 0u);
    #endif
            if (show_filament_rgb)
            {
                r = Acol.filament[i].color_R;
                g = Acol.filament[i].color_G;
                b = Acol.filament[i].color_B;
                is_filament_rgb = true;
            }
            else
#endif
            {
                if (key == 0u)
                {
                    if ((uint8_t)(pct - 49u) <= 2u) { r = 0x10u; g = 0x08u; }
                }
            }
        }

        MC_PULL_ONLINE_RGB_set(i, r, g, b, is_filament_rgb);
    }
}

void Motion_control_run(int error)
{
    const uint32_t now_ticks = time_ticks32();
    const uint32_t now_ms = time_ms_fast_from_ticks32(now_ticks);

    MC_PULL_ONLINE_read(now_ticks);

    // Sustained key-zero on the owning channel demotes LOADED to TAIL. It does
    // not release the merger: see LOADED_LATCH_DROP_MS above for why a release
    // here is the runout defect, and why TAIL has no way back to UNLOADED that
    // is not a printer command.
    //
    // Nothing to time once the state is already TAIL -- the key reading empty
    // is that state's defining condition, not evidence of a change -- so the
    // window is parked by naming no channel, which also discards any window
    // that was in flight.
    const uint8_t owner_ch = ams_state_get_loaded();
    const uint8_t timing_ch =
        ams_state_is_tail() ? ams_loaded_latch::kNoChannel : owner_ch;
    if (ams_loaded_latch::poll(g_loaded_latch_drop, timing_ch,
                               (owner_ch < kChCount) &&
                                   (MC_ONLINE_key_stu[owner_ch] == 0u),
                               now_ms, LOADED_LATCH_DROP_MS))
    {
        ams_state_set_tail();
        // Bit 6 of this channel's flags has just changed. Status delivery is
        // dirty-driven, and the next printer command is not guaranteed to be
        // soon -- on a runout the printer may pause and wait for a human. The
        // whole case for spending a wire bit on TAIL is that the monitor can
        // show it while it is happening.
        bmcu_link_status_changed(BMCU_STATUS_CHANGE_MOTION);
    }

    auto &A = ams[motion_control_ams_num];

    for (uint8_t ch = 0; ch < kChCount; ch++)
    {
        const uint8_t ks = MC_ONLINE_key_stu[ch];
        if (ks == 0u)
        {
            if (!error)
            {
                if (A.now_filament_num == ch)
                {
                    if (A.filament[ch].motion == _filament_motion::send_out)
                        MOTOR_CONTROL[ch].set_motion(filament_motion_enum::filament_motion_stop, 100, now_ms);
                }
            }

            if (g_on_use_jam_latch[ch])
            {
                g_on_use_low_latch[ch] = 0u;
                g_on_use_jam_latch[ch] = 0u;
            }

            g_on_use_hi_pwm_us[ch] = 0u;
        }
    }

    if (!error)
    {
        const uint8_t n = A.now_filament_num;

        if ((n < kChCount) && filament_channel_inserted[n] && g_on_use_jam_latch[n])
        {
            const _filament_motion m = A.filament[n].motion;

            if (m == _filament_motion::on_use || m == _filament_motion::send_out)
                bmcu_set_pressure(A, 0xF06Fu);
        }
    }

    if ((error <= 0) && all_no_filament())
    {
        int pressed = -1;

        for (uint8_t ch = 0; ch < kChCount; ch++)
        {
            if (!filament_channel_inserted[ch]) continue;

            const int   pct = (int)MC_PULL_pct[ch];
            const float v   = MC_PULL_stu_raw[ch];

            const bool hard_blue =
                (pct <= CAL_RESET_PCT_THRESH) ||
                (v <= (1.65f - CAL_RESET_V_DELTA)) ||
                (v <= (MC_PULL_V_MIN[ch] + CAL_RESET_NEAR_MIN));

            if (hard_blue) { pressed = (int)ch; break; }
        }

        uint32_t tpm = time_hw_tpms;
        if (!tpm) tpm = 1u;

        if (pressed >= 0)
        {
            if (g_hold_ch != pressed)
            {
                g_hold_ch = pressed;
                g_hold_t0_ticks = now_ticks;
            }
            else
            {
                if ((uint32_t)(now_ticks - g_hold_t0_ticks) >= (uint32_t)CAL_RESET_HOLD_MS * tpm)
                    calibration_reset_and_reboot();
            }
        }
        else
        {
            g_hold_ch = -1;
            g_hold_t0_ticks = 0u;
        }
    }
    else
    {
        g_hold_ch = -1;
        g_hold_t0_ticks = 0u;
    }

    AS5600_distance_updata(now_ticks);

    uint8_t online_changed = 0u;
    for (uint8_t i = 0; i < kChCount; i++)
    {
        const bool online = (MC_ONLINE_key_stu[i] != 0u) ||
            (filament_now_position[i] == filament_redetect) ||
            (filament_now_position[i] == filament_pulling_back);
        if (A.filament[i].online != online)
        {
            A.filament[i].online = online;
            online_changed = 1u;
        }
    }
    if (online_changed) bmcu_link_status_changed(BMCU_STATUS_CHANGE_ONLINE);

    motor_motion_run(error, now_ms, now_ticks);

    const uint8_t pressure_class = bmcu_pressure_class(A.pressure);
    if (pressure_class != g_bmcu_reported_pressure_class)
    {
        g_bmcu_reported_pressure_class = pressure_class;
        bmcu_link_status_changed(BMCU_STATUS_CHANGE_PRESSURE);
    }

    for (uint8_t i = 0; i < kChCount; i++)
    {
        if ((MC_AS5600.online[i] == false) || (MC_AS5600.magnet_stu[i] == -1))
            MC_STU_RGB_set(i, 0xFF, 0x00, 0x00);
    }
}

float Motion_control_get_filament_meters(uint8_t channel)
{
    if (channel >= kChCount) return 0.0f;
    return 1.0f - static_cast<float>(g_filament_position_counts[channel]) *
                      kAS5600_M_PER_CNT;
}

bool Motion_control_get_channel_telemetry(uint8_t channel, MotionControlChannelTelemetry* output)
{
    if (channel >= kChCount || output == nullptr) return false;
    output->raw_angle = MC_AS5600.raw_angle[channel];
    output->position_delta = g_as5600_last_delta[channel];
    output->motor_pwm = g_motor_pwm[channel];
    output->sensor_online = MC_AS5600.online[channel] ? 1u : 0u;
    output->sensor_good = AS5600_is_good(channel) ? 1u : 0u;
    output->motion_fault = MOTOR_CONTROL[channel].motion_fault;
    output->controller_motion = static_cast<uint8_t>(MOTOR_CONTROL[channel].motion);
    return true;
}

uint8_t Motion_control_get_channel_flags(uint8_t channel)
{
    if (channel >= kChCount) return 0u;

    BmcuChannelFlags flags;
    flags.raw = 0u;
    flags.bits.ks = static_cast<uint8_t>(MC_ONLINE_key_stu[channel] & 0x03u);
    flags.bits.low = g_on_use_low_latch[channel] ? 1u : 0u;
    flags.bits.jam = g_on_use_jam_latch[channel] ? 1u : 0u;
#if BMCU_DM_TWO_MICROSWITCH
    flags.bits.dm_fail = dm_fail_latch[channel] ? 1u : 0u;
#else
    // Single-microswitch builds have no DM autoload stage to fail, and
    // MC_ONLINE_key_stu only ever holds 0 or 1 there.
    flags.bits.dm_fail = 0u;
#endif
    const bool owns = (ams_state_get_loaded() == channel);
    flags.bits.loaded = owns ? 1u : 0u;
    flags.bits.tail = (owns && ams_state_is_tail()) ? 1u : 0u;
    return flags.raw;
}

// Whether every channel is parked with nothing in flight, so a reset cannot
// interrupt an operation. kMotionParked carries the per-state reasoning.
//
// The controller and AMS phases are both tested because neither implies the
// other. A bus that is idle does not mean the controller is parked: redetect
// is entered locally with no bus command at all and drives the motor. A parked
// controller does not mean the bus is idle: when the online key reads empty,
// motor_motion_switch forces pressure_ctrl_idle regardless of what the printer
// asked for, so the bus can still be mid send_out or pull_back.
//
// Note the two enums overlap numerically with different meanings -- 3 is
// filament_motion_stop here and _filament_motion::before_pull_back in ams.h.
//
// No DM autoload check is needed. Motion_control_init derives
// dm_autoload_gate = (ks != 0) and dm_loaded = (ks == 1) at boot, so after the
// reboot this reset performs: ks == 1 closes the entry condition, ks == 2 is
// blocked by the gate, and ks 0 and 3 have no transition out of DM_AUTO_IDLE.
// A reset taken while filament sits on the outer switch alone therefore leaves
// autoload gated until that filament is withdrawn to ks == 0.
bool Motion_control_is_reset_safe(void)
{
    const _ams& state = ams[BAMBU_BUS_AMS_NUM];
    for (uint8_t channel = 0u; channel < kChCount; ++channel)
    {
        if (g_motor_pwm[channel] != 0 ||
            (MOTOR_CONTROL[channel].motion_mask & kMotionParked) == 0u ||
            state.filament[channel].motion != _filament_motion::idle)
            return false;
    }
    return true;
}

// ===== PWM init =====
void MC_PWM_init()
{
    GPIO_InitTypeDef GPIO_InitStructure;

    RCC_APB2PeriphClockCmd(RCC_APB2Periph_GPIOA | RCC_APB2Periph_GPIOB, ENABLE);

    GPIO_InitStructure.GPIO_Pin   = GPIO_Pin_3 | GPIO_Pin_4 | GPIO_Pin_5 |
                                    GPIO_Pin_6 | GPIO_Pin_7 | GPIO_Pin_8 | GPIO_Pin_9;
    GPIO_InitStructure.GPIO_Mode  = GPIO_Mode_AF_PP;
    GPIO_InitStructure.GPIO_Speed = GPIO_Speed_50MHz;
    GPIO_Init(GPIOB, &GPIO_InitStructure);

    GPIO_InitStructure.GPIO_Pin = GPIO_Pin_15;
    GPIO_Init(GPIOA, &GPIO_InitStructure);

    RCC_APB2PeriphClockCmd(RCC_APB2Periph_AFIO, ENABLE);
    RCC_APB1PeriphClockCmd(RCC_APB1Periph_TIM2, ENABLE);
    RCC_APB1PeriphClockCmd(RCC_APB1Periph_TIM3, ENABLE);
    RCC_APB1PeriphClockCmd(RCC_APB1Periph_TIM4, ENABLE);

    TIM_TimeBaseInitTypeDef TIM_TimeBaseStructure;
    TIM_OCInitTypeDef TIM_OCInitStructure;

    TIM_TimeBaseStructure.TIM_Period        = 999;
    TIM_TimeBaseStructure.TIM_Prescaler     = 1;
    TIM_TimeBaseStructure.TIM_ClockDivision = 0;
    TIM_TimeBaseStructure.TIM_CounterMode   = TIM_CounterMode_Up;

    TIM_TimeBaseInit(TIM2, &TIM_TimeBaseStructure);
    TIM_TimeBaseInit(TIM3, &TIM_TimeBaseStructure);
    TIM_TimeBaseInit(TIM4, &TIM_TimeBaseStructure);

    TIM_OCInitStructure.TIM_OCMode      = TIM_OCMode_PWM1;
    TIM_OCInitStructure.TIM_OutputState = TIM_OutputState_Enable;
    TIM_OCInitStructure.TIM_Pulse       = 0;
    TIM_OCInitStructure.TIM_OCPolarity  = TIM_OCPolarity_High;

    TIM_OC1Init(TIM2, &TIM_OCInitStructure);
    TIM_OC2Init(TIM2, &TIM_OCInitStructure);

    TIM_OC1Init(TIM3, &TIM_OCInitStructure);
    TIM_OC2Init(TIM3, &TIM_OCInitStructure);

    TIM_OC1Init(TIM4, &TIM_OCInitStructure);
    TIM_OC2Init(TIM4, &TIM_OCInitStructure);
    TIM_OC3Init(TIM4, &TIM_OCInitStructure);
    TIM_OC4Init(TIM4, &TIM_OCInitStructure);

    TIM_OC1PreloadConfig(TIM2, TIM_OCPreload_Enable);
    TIM_OC2PreloadConfig(TIM2, TIM_OCPreload_Enable);

    TIM_OC1PreloadConfig(TIM3, TIM_OCPreload_Enable);
    TIM_OC2PreloadConfig(TIM3, TIM_OCPreload_Enable);

    TIM_OC1PreloadConfig(TIM4, TIM_OCPreload_Enable);
    TIM_OC2PreloadConfig(TIM4, TIM_OCPreload_Enable);
    TIM_OC3PreloadConfig(TIM4, TIM_OCPreload_Enable);
    TIM_OC4PreloadConfig(TIM4, TIM_OCPreload_Enable);

    // CH1/CH2 still map to PA15/PB3, while CH3/CH4 stay off PB10/PB11.
    // PB10/PB11 are the dedicated H1 USART3 monitor/control link.
    GPIO_PinRemapConfig(GPIO_PartialRemap1_TIM2, ENABLE);
    GPIO_PinRemapConfig(GPIO_PartialRemap_TIM3, ENABLE);
    GPIO_PinRemapConfig(GPIO_Remap_TIM4, DISABLE);

    TIM_CtrlPWMOutputs(TIM2, ENABLE);
    TIM_ARRPreloadConfig(TIM2, ENABLE);
    TIM_Cmd(TIM2, ENABLE);

    TIM_CtrlPWMOutputs(TIM3, ENABLE);
    TIM_ARRPreloadConfig(TIM3, ENABLE);
    TIM_Cmd(TIM3, ENABLE);

    TIM_CtrlPWMOutputs(TIM4, ENABLE);
    TIM_ARRPreloadConfig(TIM4, ENABLE);
    TIM_Cmd(TIM4, ENABLE);
}

// różnica kątów
static inline int M5600_angle_dis(int16_t angle1, int16_t angle2)
{
    int d = (int)angle1 - (int)angle2;
    if (d >  2048) d -= 4096;
    if (d < -2048) d += 4096;
    return d;
}

// test kierunku silników
static void MOTOR_get_dir()
{
    bmcu_link_set_calibration_busy(true);
    int  polarity[4] = {0,0,0,0};
    bool test[4]    = {false,false,false,false};
    bool any_detect = false;
    bool any_change = false;
    bool timed_out  = false;

    const bool have_data = Motion_control_read();
    if (!have_data)
    {
        for (uint8_t i = 0; i < kChCount; i++)
            Motion_control_data_save.motor_polarity[i] = 0;
    }

    MC_AS5600.updata_angle();

    int16_t last_angle[4];
    for (uint8_t i = 0; i < kChCount; i++)
    {
        last_angle[i] = MC_AS5600.raw_angle[i];
        polarity[i] = Motion_control_data_save.motor_polarity[i];
    }

    // Start test tylko tam, gdzie:
    // - AS5600 online
    // - kanał fizycznie wpięty
    // - polarity unknown (0)
    for (uint8_t i = 0; i < kChCount; i++)
    {
        if (AS5600_is_good(i) && filament_channel_inserted[i] && (polarity[i] == 0))
        {
            Motion_control_set_PWM(i, 1000);
            test[i] = true;
        }
    }

    // jeśli nie ma nic do testowania -> nie rób NIC, nie zapisuj, nie psuj
    if (!(test[0] || test[1] || test[2] || test[3]))
    {
        bmcu_link_set_calibration_busy(false);
        return;
    }

    // czekaj max 2s na ruch (200 * 10ms)
    for (int t = 0; t < 200; t++)
    {
        delay(10);
        bmcu_link_service();
        MC_AS5600.updata_angle();

        bool done = true;

        for (uint8_t i = 0; i < kChCount; i++)
        {
            if (!test[i]) continue;

            // jeśli czujnik zniknął po drodze -> abort kanału (nie zapisuj)
            if (!MC_AS5600.online[i])
            {
                Motion_control_set_PWM(i, 0);
                test[i] = false;
                continue;
            }

            const int angle_dis = M5600_angle_dis((int16_t)MC_AS5600.raw_angle[i], last_angle[i]);

            if ((angle_dis > 163) || (angle_dis < -163))
            {
                Motion_control_set_PWM(i, 0);

                // AS5600 odwrotnie względem magnesu
                polarity[i] = (angle_dis > 0) ? 1 : -1;

                test[i] = false;
                any_detect = true;
            }
            else
            {
                done = false;
            }
        }

        if (done) break;
        if (t == 199) timed_out = true;
    }

    // stop dla niedokończonych
    for (uint8_t i = 0; i < kChCount; i++)
        if (test[i]) Motion_control_set_PWM(i, 0);

    // update only channels where calibration changed the polarity
    for (uint8_t i = 0; i < kChCount; i++)
    {
        if (polarity[i] != Motion_control_data_save.motor_polarity[i])
        {
            Motion_control_data_save.motor_polarity[i] = polarity[i];
            any_change = true;
        }
    }

    // save only after real movement established polarity (+1 or -1)
    // Jak brak 24V i nic się nie ruszyło -> any_detect=false -> NIE zapisujemy.
    if (any_detect && any_change)
    {
        Motion_control_save();
    }
    else
    {
        (void)timed_out;
    }
    bmcu_link_set_calibration_busy(false);
}

// init motorów
static void MOTOR_init()
{
    MC_PWM_init();

    RCC_APB2PeriphClockCmd(RCC_APB2Periph_GPIOC | RCC_APB2Periph_GPIOD, ENABLE);

    MOTOR_get_dir();

    for (uint8_t i = 0; i < kChCount; i++)
    {
        Motion_control_set_PWM(i, 0);
        MOTOR_CONTROL[i].set_pwm_zero(500);
        const int polarity = Motion_control_data_save.motor_polarity[i];
        MOTOR_CONTROL[i].motor_polarity_negate_mask =
            motor_polarity_to_negate_mask(polarity);
        MOTOR_CONTROL[i].motor_polarity_valid = polarity != 0 ? 1u : 0u;
    }
}

void Motion_control_init()
{
    auto &A = ams[motion_control_ams_num];
    A.online   = true;
    A.ams_type = 0x03;

    (void)Motion_control_read();

    MC_PULL_ONLINE_init();
    MC_PULL_ONLINE_read(time_ticks32());

    #if BMCU_DM_TWO_MICROSWITCH
        for (uint8_t ch = 0; ch < kChCount; ch++)
        {
            if (!filament_channel_inserted[ch])
            {
                dm_loaded[ch]            = 1u;
                dm_fail_latch[ch]        = 0u;
                dm_auto_state[ch]        = DM_AUTO_IDLE;
                dm_auto_try[ch]          = 0u;
                dm_auto_t0_ms[ch]        = 0u;
                dm_auto_remain_counts[ch] = 0;
                dm_auto_last_counts[ch]   = 0;
                dm_loaded_drop_t0_ms[ch] = 0u;
                dm_autoload_gate[ch]     = 0u;
                continue;
            }

            const uint8_t ks = MC_ONLINE_key_stu[ch];

            dm_autoload_gate[ch] = (ks != 0u) ? 1u : 0u;
            dm_loaded[ch] = (ks == 1u) ? 1u : 0u;

            dm_fail_latch[ch]        = 0u;
            dm_auto_state[ch]        = DM_AUTO_IDLE;
            dm_auto_try[ch]          = 0u;
            dm_auto_t0_ms[ch]        = 0u;
            dm_auto_remain_counts[ch] = 0;
            dm_auto_last_counts[ch]   = 0;
            dm_loaded_drop_t0_ms[ch] = 0u;
        }
    #endif

    MC_AS5600.init(AS5600_SCL_PORT, AS5600_SCL_PIN,
               AS5600_SDA_PORT, AS5600_SDA_PIN,
               4);
    MC_AS5600.updata_angle();
    MC_AS5600.updata_stu();

    for (uint8_t i = 0; i < kChCount; i++)
    {
        const bool ok = MC_AS5600.online[i] && (MC_AS5600.magnet_stu[i] != AS5600_soft_IIC_many::offline);
        g_as5600_good[i]     = ok ? 1u : 0u;
        g_as5600_fail[i]     = ok ? 0u : kAS5600_FAIL_TRIP;
        g_as5600_okstreak[i] = ok ? kAS5600_OK_RECOVER : 0u;
    }

    for (uint8_t i = 0; i < kChCount; i++)
    {
        as5600_distance_save[i] = MC_AS5600.raw_angle[i];
        filament_now_position[i] = filament_idle;
    }

    MOTOR_init();
}
