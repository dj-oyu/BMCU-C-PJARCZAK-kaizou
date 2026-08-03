#pragma once
#include <stdint.h>

// Host stand-in for src/hal/irq_wch.h.
//
// The real header is inline RISC-V assembly against CSR 0x800 (`csrr`/`csrs`/
// `csrc`), which does not assemble for a host target at all -- this is a
// compile failure rather than a runtime one, so it is the blocker a reader
// meets first.
//
// The critical sections it guards in bambu_bus_ams.cpp exist to make the RX
// publish handshake atomic against the USART interrupt. The host harness is
// single-threaded and publishes frames by calling _bus_port_deal::irq directly
// from the test body, so there is no concurrent writer and nothing to mask.
// Returning a constant keeps the save/restore pairing observable -- an
// unbalanced restore would still be a type error -- without pretending to
// implement interrupt state that does not exist here.

static inline uint32_t irq_save_wch(void)
{
    return 0u;
}

static inline void irq_restore_wch(uint32_t s88)
{
    (void)s88;
}
