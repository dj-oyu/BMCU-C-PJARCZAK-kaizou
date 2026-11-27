#pragma once
// Fixed-point arithmetic for the sensor path. The firmware links no soft-float
// routines: on this part they cost ~4.7 KB of a 60 KB budget. Every voltage is
// an integer in 0.1 mV ("dmV", 3.3 V = 33000), every buffer position is in
// 0.01 % ("pct_q", 0..10000). Header-only so ci/sensor_fixed_test.cpp checks it
// against the float arithmetic it replaced.
//
// Floating-point literals are fine in this file and everywhere else as long as
// they fold at compile time (constexpr). tools/no_softfloat.py fails the build
// if one does not.

#include <stdint.h>

namespace sensor_fixed
{

// Volts to dmV, for constants only.
constexpr int32_t dmv(double volts)
{
    return static_cast<int32_t>(volts * 10000.0 + (volts < 0.0 ? -0.5 : 0.5));
}

constexpr int32_t kFullScale = dmv(3.30);
constexpr int32_t kCenter    = dmv(1.65);

// --- ADC boxcar -> dmV --------------------------------------------------------
// One block is 32 scans; each scan stores ADC1+ADC2 as one 13-bit pair sum, so
// a full-scale block sums to 8190 * 32. `blocks` is how many of the four ring
// slots hold data. kMul[b] = 2^32 * 33000 / (8190 * 32 * b), rounded.
constexpr uint32_t kAdcMul[5] = {
    0u,
    static_cast<uint32_t>(4294967296.0 * 33000.0 / (8190.0 * 32.0 * 1.0) + 0.5),
    static_cast<uint32_t>(4294967296.0 * 33000.0 / (8190.0 * 32.0 * 2.0) + 0.5),
    static_cast<uint32_t>(4294967296.0 * 33000.0 / (8190.0 * 32.0 * 3.0) + 0.5),
    static_cast<uint32_t>(4294967296.0 * 33000.0 / (8190.0 * 32.0 * 4.0) + 0.5),
};

inline int32_t adc_sum_to_dmv(uint32_t sum, uint32_t mul)
{
    return static_cast<int32_t>((static_cast<uint64_t>(sum) * mul + 0x80000000u) >> 32);
}

// --- Buffer lever voltage -> position ----------------------------------------
// Piecewise linear around the 1.65 V rest point: vmin..1.65 V maps to 0..50 %,
// 1.65 V..vmax to 50..100 %, each half with a floor of 50 mV on its span.
inline int16_t pull_dmv_to_pct_q(int32_t v, int32_t vmin, int32_t vmax)
{
    if (vmin > dmv(1.60)) vmin = dmv(1.60);
    if (vmax < dmv(1.70)) vmax = dmv(1.70);
    if (vmax <= vmin + dmv(0.10)) { vmin = dmv(1.55); vmax = dmv(1.75); }

    int32_t q;
    if (v <= kCenter)
    {
        int32_t den = kCenter - vmin;
        if (den < dmv(0.05)) den = dmv(0.05);
        const int32_t num = v - vmin;
        if (num <= 0) return 0;
        q = (num * 5000 + den / 2) / den;
        if (q > 5000) q = 5000;
    }
    else
    {
        int32_t den = vmax - kCenter;
        if (den < dmv(0.05)) den = dmv(0.05);
        q = 5000 + ((v - kCenter) * 5000 + den / 2) / den;
        if (q > 10000) q = 10000;
    }
    return static_cast<int16_t>(q);
}

// --- DM key thresholds, persisted in centivolts -------------------------------
inline uint8_t dmv_to_centi_ceil(int32_t v)
{
    if (v <= 0) return 0u;
    const int32_t cv = (v + 99) / 100;
    return cv > 255 ? 255u : static_cast<uint8_t>(cv);
}

constexpr int32_t centi_to_dmv(uint8_t cv) { return static_cast<int32_t>(cv) * 100; }

// Threshold between "no strand" and "inner switch": 0.10 V above the idle key
// reading, kept within 0.60..1.39 V.
inline int32_t none_threshold_from_idle(int32_t idle)
{
    int32_t cv = static_cast<int32_t>(dmv_to_centi_ceil(idle)) + 10;
    if (cv < 60) cv = 60;
    if (cv > 139) cv = 139;
    return cv * 100;
}

inline uint16_t dmv_to_mv_saturating(int32_t v)
{
    if (v <= 0) return 0u;
    const int32_t mv = (v + 5) / 10;
    return mv >= 65535 ? 65535u : static_cast<uint16_t>(mv);
}

// --- IEEE-754 single without soft-float --------------------------------------
// Decode a stored float (calibration records written before the dmV format)
// into dmV. Rejects NaN, infinity and anything outside +-6.5 V.
inline bool float_bits_to_dmv(uint32_t bits, int32_t* out)
{
    const uint32_t e = (bits >> 23) & 0xFFu;
    if (e == 0u) { *out = 0; return true; }        // zero and denormals
    if (e > 129u) return false;                    // |v| >= 8 V, NaN, inf
    const uint32_t mant = (bits & 0x7FFFFFu) | 0x800000u;
    const uint32_t t = static_cast<uint32_t>((static_cast<uint64_t>(mant) * 10000u) >> 8);
    const uint32_t sh = 142u - e;                  // >= 13 for e <= 129
    const int32_t mag = (sh >= 32u) ? 0 : static_cast<int32_t>((t + (1u << (sh - 1u))) >> sh);
    if (mag > dmv(6.5)) return false;
    *out = (bits & 0x80000000u) ? -mag : mag;
    return true;
}

// Encode value / 2^frac_bits as an IEEE-754 single, truncating the mantissa.
inline uint32_t fixed_to_float_bits(int32_t value, uint32_t frac_bits)
{
    if (value == 0) return 0u;
    uint32_t sign = 0u;
    uint32_t mag = static_cast<uint32_t>(value);
    if (value < 0) { sign = 0x80000000u; mag = 0u - mag; }
    uint32_t msb = 31u;
    while (!(mag & (1u << msb))) --msb;
    const uint32_t mant = (msb >= 23u) ? (mag >> (msb - 23u)) : (mag << (23u - msb));
    const uint32_t exp = 127u + msb - frac_bits;
    return sign | (exp << 23) | (mant & 0x7FFFFFu);
}

// Filament meters reported to the printer: 1 m minus the travel measured by
// the AS5600, which counts (pi * 7.5 mm) / 4096 per step. The travel is taken
// in Q40 meters (the per-count constant is exact to 1e-13 m there) and the
// result encoded from Q20, i.e. to 1 um absolute; counts within +-3.5e8 keep it
// inside int32 (~2 km of travel).
constexpr double kMetersPerCount = 3.14159265358979323846 * 7.5 / 4096.0 / 1000.0;
constexpr int64_t kMetersPerCountQ40 = static_cast<int64_t>(kMetersPerCount * 1099511627776.0 + 0.5);

inline uint32_t filament_meters_bits(int32_t counts)
{
    const int64_t travel_q40 = static_cast<int64_t>(counts) * kMetersPerCountQ40;
    const int32_t travel_q20 = static_cast<int32_t>((travel_q40 + (1 << 19)) >> 20);
    return fixed_to_float_bits((1 << 20) - travel_q20, 20u);
}

} // namespace sensor_fixed
