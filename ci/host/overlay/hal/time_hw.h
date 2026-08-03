#pragma once
#include <stdint.h>

// Host stand-in for src/hal/time_hw.h.
//
// The real header is the dangerous one of the three blockers, because it does
// not announce itself: every declaration compiles cleanly on a host, and
// `time_ticks32()` is an inline dereference of STK_CNTL at the absolute
// address 0xE000F008. A host binary built against it links, runs, and
// segfaults on the first call -- which in bambubus_run() is the first
// statement. A grep for register names finds nothing in bambu_bus_ams.cpp
// itself; the register is one include away.
//
// Everything below is the real header's arithmetic verbatim. Only the two
// sites that read hardware are replaced: time_ticks32() returns a settable
// counter, and delayTicks32() advances it instead of spinning on the SysTick.
// The unit stays what the firmware uses -- time_hw_tpus / time_hw_tpms carry
// the same initialisers as src/hal/time_hw.c (1 and 1000), so one tick is one
// microsecond until a test says otherwise.
//
// The counter is deliberately a plain uint32_t with the same width as the
// hardware register: the wrap behaviour that time_diff32 exists to survive is
// reproducible here, and a test can seek the clock to just below 2^32 to
// exercise it.

#ifdef __cplusplus
extern "C" {
#endif

void     time_hw_init(void);
uint32_t time_hw_ticks_per_us(void);
uint32_t time_hw_ticks_per_ms(void);

extern uint32_t time_hw_tpus;
extern uint32_t time_hw_tpms;

// The fake clock. Defined in ci/host/bambu_bus_host.cpp; drive it with the
// host_clock_* helpers below rather than assigning to it directly, so a test
// reads as "advance 20 ms" and not as tick arithmetic.
extern uint32_t host_clock_ticks;

static inline uint32_t time_diff_u32(uint32_t a, uint32_t b)
{
    return (uint32_t)(a - b);
}

static inline int time_reached32(uint32_t now, uint32_t deadline)
{
    return ((time_diff_u32(now, deadline) >> 31) == 0u);
}

static inline int32_t time_diff32(uint32_t a, uint32_t b)
{
    return (int32_t)(a - b);
}

static inline uint32_t ms_to_ticks32(uint32_t ms)
{
    const uint32_t tpm = time_hw_tpms;
    if (!ms || !tpm) return 0u;

    const uint32_t max_ms = 0xFFFFFFFFu / tpm;
    if (ms > max_ms) return 0xFFFFFFFFu;

    return ms * tpm;
}

static inline uint32_t us_to_ticks32(uint32_t us)
{
    const uint32_t tpu = time_hw_tpus;
    if (!us || !tpu) return 0u;

    const uint32_t max_us = 0xFFFFFFFFu / tpu;
    if (us > max_us) return 0xFFFFFFFFu;

    return us * tpu;
}

static inline uint32_t time_ticks32(void)
{
    return host_clock_ticks;
}

uint64_t time_ticks64(void);
uint64_t time_us64(void);
uint64_t time_ms64(void);

static inline void delayTicks32(uint32_t ticks)
{
    host_clock_ticks += ticks;
}

void delay_us(uint32_t us);
void delay(uint32_t ms);

// Test-side controls. Not part of the firmware API.
static inline void host_clock_set(uint32_t ticks)
{
    host_clock_ticks = ticks;
}

static inline void host_clock_advance_us(uint32_t us)
{
    host_clock_ticks += us * time_hw_tpus;
}

static inline void host_clock_advance_ms(uint32_t ms)
{
    host_clock_ticks += ms * time_hw_tpms;
}

#ifdef __cplusplus
}
#endif
