// Host test for src/sensor_fixed.h. The reference functions below are the
// float arithmetic the firmware used before it dropped soft-float, copied
// verbatim from Motion_control.cpp / MC_PULL_calibration.cpp / ADC_DMA.cpp.
// Each fixed-point replacement must agree with them to within the stated
// tolerance over the whole input range.

#include "sensor_fixed.h"

#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

using namespace sensor_fixed;

#define CHECK(cond, code) do { if (!(cond)) { printf("fail %d at %s:%d\n", (code), __FILE__, __LINE__); return (code); } } while (0)

static float clampf(float x, float a, float b) { return x < a ? a : (x > b ? b : x); }

static float ref_pull_v_to_percent_f(float v, float vmin, float vmax)
{
    const float c = 1.65f;
    if (vmin > 1.60f) vmin = 1.60f;
    if (vmax < 1.70f) vmax = 1.70f;
    if (vmax <= (vmin + 0.10f)) { vmin = 1.55f; vmax = 1.75f; }
    float low_den = c - vmin;
    float high_den = vmax - c;
    if (low_den < 0.05f) low_den = 0.05f;
    if (high_den < 0.05f) high_den = 0.05f;
    const float low_scale = 0.5f / low_den;
    const float high_scale = 0.5f / high_den;
    if (v <= c) return clampf((v - vmin) * low_scale, 0.0f, 1.0f) * 100.0f;
    return clampf(0.5f + (v - c) * high_scale, 0.0f, 1.0f) * 100.0f;
}

static int32_t ref_pct_q(float v, float vmin, float vmax)
{
    int32_t q = static_cast<int32_t>(ref_pull_v_to_percent_f(v, vmin, vmax) * 100.0f + 0.5f);
    if (q < 0) q = 0;
    if (q > 10000) q = 10000;
    return q;
}

static uint8_t ref_centi_ceil(float v)
{
    if (v <= 0.0f) return 0u;
    float x = v * 100.0f - 0.0001f;
    int iv = (int)x;
    if ((float)iv < x) iv++;
    if (iv < 0) iv = 0;
    if (iv > 255) iv = 255;
    return (uint8_t)iv;
}

static uint32_t lcg = 12345u;
static uint32_t rnd() { lcg = lcg * 1664525u + 1013904223u; return lcg >> 8; }

static int test_adc_scale()
{
    for (uint32_t blocks = 1; blocks <= 4; ++blocks)
    {
        const float scale = 3.3f / (8190.0f * 32.0f * (float)blocks);
        const uint32_t full = 8190u * 32u * blocks;
        CHECK(adc_sum_to_dmv(full, kAdcMul[blocks]) == 33000, 10 + (int)blocks);
        CHECK(adc_sum_to_dmv(0u, kAdcMul[blocks]) == 0, 20 + (int)blocks);
        for (int i = 0; i < 20000; ++i)
        {
            const uint32_t sum = rnd() % (full + 1u);
            const float ref = (float)sum * scale * 10000.0f;
            const int32_t got = adc_sum_to_dmv(sum, kAdcMul[blocks]);
            CHECK(fabsf((float)got - ref) <= 0.51f, 30 + (int)blocks);
        }
    }
    return 0;
}

static int test_pull_pct()
{
    // Calibrations seen in the field and the degenerate ones the clamps exist for.
    const float cal[][2] = {
        {1.00f, 2.00f}, {0.85f, 2.40f}, {1.20f, 2.05f}, {1.62f, 1.68f},
        {1.55f, 1.75f}, {1.64f, 2.90f}, {0.40f, 1.66f}, {1.60f, 1.70f},
    };
    for (unsigned k = 0; k < sizeof(cal) / sizeof(cal[0]); ++k)
    {
        const int32_t vmin = dmv(cal[k][0]);
        const int32_t vmax = dmv(cal[k][1]);
        for (int32_t v = 0; v <= 33000; ++v)
        {
            const int32_t ref = ref_pct_q((float)v / 10000.0f, (float)vmin / 10000.0f, (float)vmax / 10000.0f);
            const int32_t got = pull_dmv_to_pct_q(v, vmin, vmax);
            // One pct_q is 0.01 %; the float path rounded through two
            // single-precision products, so allow one step either way.
            if (got - ref > 1 || ref - got > 1)
            {
                printf("pct v=%d vmin=%d vmax=%d ref=%d got=%d\n", v, vmin, vmax, ref, got);
                return 40 + (int)k;
            }
        }
    }
    // The decisions downstream are made on whole percent and on the
    // 1000/1500/4500/5500 gesture bands; make sure the endpoints are exact.
    CHECK(pull_dmv_to_pct_q(kCenter, dmv(1.0), dmv(2.0)) == 5000, 50);
    CHECK(pull_dmv_to_pct_q(dmv(1.0), dmv(1.0), dmv(2.0)) == 0, 51);
    CHECK(pull_dmv_to_pct_q(dmv(2.0), dmv(1.0), dmv(2.0)) == 10000, 52);
    CHECK(pull_dmv_to_pct_q(-5000, dmv(1.0), dmv(2.0)) == 0, 53);
    CHECK(pull_dmv_to_pct_q(40000, dmv(1.0), dmv(2.0)) == 10000, 54);
    return 0;
}

static int test_centi()
{
    for (int32_t v = -100; v <= 30000; ++v)
    {
        const uint8_t ref = ref_centi_ceil((float)v / 10000.0f);
        const uint8_t got = dmv_to_centi_ceil(v);
        // The float reference subtracts 1e-4 cV before ceiling, which only
        // matters when v is an exact multiple of 1 cV -- where both agree.
        if (ref != got) { printf("centi v=%d ref=%u got=%u\n", v, ref, got); return 60; }
    }
    CHECK(dmv_to_centi_ceil(6000) == 60u, 61);
    CHECK(dmv_to_centi_ceil(6001) == 61u, 62);
    CHECK(none_threshold_from_idle(0) == 6000, 63);
    CHECK(none_threshold_from_idle(dmv(0.55)) == 6500, 64);
    CHECK(none_threshold_from_idle(dmv(2.0)) == 13900, 65);
    CHECK(centi_to_dmv(60u) == 6000, 66);
    return 0;
}

static int test_mv()
{
    CHECK(dmv_to_mv_saturating(-1) == 0u, 70);
    CHECK(dmv_to_mv_saturating(0) == 0u, 71);
    CHECK(dmv_to_mv_saturating(16504) == 1650u, 72);
    CHECK(dmv_to_mv_saturating(16505) == 1651u, 73);
    CHECK(dmv_to_mv_saturating(33000) == 3300u, 74);
    return 0;
}

static uint32_t bits_of(float f) { uint32_t b; memcpy(&b, &f, 4); return b; }
static float float_of(uint32_t b) { float f; memcpy(&f, &b, 4); return f; }

static int test_float_decode()
{
    const float samples[] = {0.0f, -0.0f, 1.65f, 1.00f, 2.00f, 0.60f, 1.39f,
                             -0.123f, 0.0001f, 0.00004f, 3.3f, 1.55f, 1.75f, -1.65f};
    for (unsigned i = 0; i < sizeof(samples) / sizeof(samples[0]); ++i)
    {
        int32_t got = 99999;
        CHECK(float_bits_to_dmv(bits_of(samples[i]), &got), 80);
        const long ref = lroundf(samples[i] * 10000.0f);
        if (labs((long)got - ref) > 1) { printf("decode %g ref=%ld got=%d\n", samples[i], ref, got); return 81; }
    }
    for (int i = 0; i < 200000; ++i)
    {
        const float f = ((float)(rnd() % 130001u) - 65000.0f) / 10000.0f;
        int32_t got = 0;
        CHECK(float_bits_to_dmv(bits_of(f), &got), 82);
        if (labs((long)got - lroundf(f * 10000.0f)) > 1) { printf("decode %g got=%d\n", f, got); return 83; }
    }
    int32_t sink;
    CHECK(!float_bits_to_dmv(0x7FC00000u, &sink), 84);   // NaN
    CHECK(!float_bits_to_dmv(0x7F800000u, &sink), 85);   // inf
    CHECK(!float_bits_to_dmv(bits_of(9.0f), &sink), 86);
    CHECK(!float_bits_to_dmv(bits_of(-7.0f), &sink), 87);
    CHECK(!float_bits_to_dmv(0xFFFFFFFFu, &sink), 88);   // erased flash
    return 0;
}

static int test_meters()
{
    // The reference is the old expression, 1 - counts * (pi*7.5/4096 mm).
    const float m_per_cnt = (3.14159265358979323846f * 7.5f) / 4096.0f * 0.001f;
    const int32_t probes[] = {0, 1, -1, 7, 100, -100, 5000, -5000, 173913, -173913,
                              174000, 1000000, -1000000, 50000000, -50000000};
    for (unsigned i = 0; i < sizeof(probes) / sizeof(probes[0]); ++i)
    {
        const float ref = 1.0f - (float)probes[i] * m_per_cnt;
        const float got = float_of(filament_meters_bits(probes[i]));
        const float tol = 2e-6f + fabsf(ref) * 2e-6f;
        if (fabsf(got - ref) > tol) { printf("meters %d ref=%.9g got=%.9g\n", probes[i], ref, got); return 90; }
    }
    for (int i = 0; i < 100000; ++i)
    {
        const int32_t c = (int32_t)(rnd() % 20000001u) - 10000000;
        const float ref = 1.0f - (float)c * m_per_cnt;
        const float got = float_of(filament_meters_bits(c));
        if (fabsf(got - ref) > 2e-6f + fabsf(ref) * 2e-6f) { printf("meters %d ref=%.9g got=%.9g\n", c, ref, got); return 91; }
    }
    // Zero travel is exactly 1.0 m, the value the printer sees at idle.
    CHECK(filament_meters_bits(0) == bits_of(1.0f), 92);
    // Exactly 1 m consumed is exactly zero.
    CHECK(fixed_to_float_bits(0, 20u) == 0u, 93);
    CHECK(fixed_to_float_bits(-(1 << 20), 20u) == bits_of(-1.0f), 94);
    CHECK(fixed_to_float_bits(3, 1u) == bits_of(1.5f), 95);
    return 0;
}

int main()
{
    int rc;
    if ((rc = test_adc_scale())) return rc;
    if ((rc = test_pull_pct())) return rc;
    if ((rc = test_centi())) return rc;
    if ((rc = test_mv())) return rc;
    if ((rc = test_float_decode())) return rc;
    if ((rc = test_meters())) return rc;
    return 0;
}
