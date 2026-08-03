# BMCU target hardware: CH32V203C8T6

Status: derived from the checked-in build configuration and the locally
installed PlatformIO platform/toolchain on 2026-08-04
Scope: what the firmware's target chip permits, costs, or forbids, for anyone
about to make a decision that depends on it

## 1. Purpose

This is not a datasheet. It answers the question a change to this firmware
keeps needing answered: given this specific chip, this specific toolchain, and
this specific build configuration, what does an instruction cost, what does a
flash write cost, and what is silently absent that a design must not assume.
Every number below is derived from a file in this tree, the installed
PlatformIO platform package, or the installed toolchain binary — the sources
are cited inline. A claim with no source attached is flagged as such rather
than stated as fact.

## 2. Identity

`platformio.ini` builds `[env:base]` (and everything that extends it) with
`board = genericCH32V203C8T6` (`platformio.ini:5`). The board definition
resolves that to a WCH CH32V203C8T6: RISC-V core, `march = rv32imacxw`,
`mabi = ilp32`, `f_cpu = 144000000L`, and the extra defines
`-DCH32V203C8 -DCH32V20X -DCH32V20x -DCH32V203 -DCH32V20x_D6`
(`boards/genericCH32V203C8T6.json` in the installed `ch32v` platform package).
`CH32V20x_D6` selects the "D6" memory-density group of the CH32V20x family in
the vendor SDK headers and linker script — the group that covers the
F6/F8/G6/G8/K6/K8/C6/C8 parts, as opposed to `CH32V20x_D8` (bigger RB parts)
or `CH32V20x_D8W` (the CH32V208 Wi-Fi/BLE variant). It is not itself a size
selector; the size comes from which `MEMORY` block in the linker script is
left uncommented for this chip (see below).

The board JSON's own `upload` block states `maximum_size: 65536` and
`maximum_ram_size: 20480` — 64 KiB flash, 20 KiB RAM (same file). The vendor
linker script for this family, `Link_CH32V20x.ld`
(`framework-wch-noneos-sdk/platformio/ldscripts/Link_CH32V20x.ld`), contains
several commented-out `MEMORY` blocks for sibling parts and exactly one live
block:

```
FLASH (rx) : ORIGIN = 0x00000000, LENGTH = 64K
RAM (xrw)  : ORIGIN = 0x20000000, LENGTH = 20K
```

commented as the range for `CH32V203K8-CH32V203C8-CH32V203G8-CH32V203F8`. That
block is what the build actually enforces, because it is the only
uncommented one in the file — the linker will place code and data anywhere
inside those 64 KiB/20 KiB, full stop, regardless of what any board JSON or
`platformio.ini` setting claims.

`platformio.ini` itself sets `board_upload.maximum_size = 61440`
(`platformio.ini:8`) — 60 KiB, 4 KiB **less** than the linker's 64 KiB ceiling.
This is not a second, disagreeing source of truth about the chip; it is a
narrower postbuild size check layered on top of the same chip. The 4 KiB it
subtracts is exactly the flash region `src/Flash_saves.h` reserves for
persisted state:

```
FLASH_NVM_BASE_ADDR = 0x0800F000   // last 4 KiB of a 64 KiB part
```

(`src/Flash_saves.h:9`). `0x0800F000` is offset `0xF000` (61440 decimal) into
the flash, i.e. exactly the `maximum_size` cutoff. The linker script does not
know this sector is reserved — it would happily place `.text` into it if the
image grew that large — so `board_upload.maximum_size` is the only thing
standing between an oversized build and code that overlaps and eventually
corrupts the persisted filament/calibration/loaded-latch records. **The
build-enforced ceiling for code and constant data is 60 KiB (61440 bytes),
not the chip's physical 64 KiB**, and the last 4 KiB is off-limits by
convention, not by hardware protection. The worst configuration measured at the time of writing (DM two-microswitch,
filament RGB, soft load) reads `text+data = 57420` against
`board_upload.maximum_size = 61440` (`.pio/build/fw/firmware.elf`,
`riscv-wch-elf-size`), leaving 4020 bytes of actual headroom before the postbuild check fails, and 8116 bytes before it would
start colliding with the NVM sector if the check were ever loosened.

RAM: 20480 bytes, agreed by the board JSON and the linker script; nothing in
this codebase narrows that the way flash is narrowed.

## 3. Instruction set: `-march=rv32imacxw`

The march string reaching the compiler is exactly `rv32imacxw`, confirmed two
ways: it is the literal value of `build.march` in
`boards/genericCH32V203C8T6.json`, and the platform's build script
(`builder/frameworks/_bare.py:12,42,78,89`) passes `board.get("build.march")`
straight through to `-march=` on `ASFLAGS`, `CCFLAGS`, and `LINKFLAGS` without
substitution for GCC ≥ 12 (the remap table at `_bare.py:16-22` only rewrites
`rv32imacxw`-shaped strings for older, GCC-8-only toolchains; the installed
toolchain is `riscv-wch-elf-gcc-12.2.0`, confirmed by
`riscv-wch-elf-gcc.exe -v`, so no remap fires). The actual firmware ELF
records the fully expanded ISA string in its RISC-V build attributes
(`riscv-wch-elf-readelf -A .pio/build/fw/firmware.elf`):

```
Tag_RISCV_arch: "rv32i2p0_m2p0_a2p0_c2p0_zmmul1p0_xw2p2"
```

- **`i`** — base 32-bit integer instructions.
- **`m`** — integer multiply/divide. GCC's canonical expansion also lists the
  implied `zmmul` (multiply-only) subset; this is normal decomposition, not a
  separate feature the build asked for.
- **`a`** — atomic memory operations (`lr.w`/`sc.w`/AMOs). This exists in
  hardware and the compiler will use it, but see §7 for why this codebase
  doesn't need it.
- **`c`** — compressed (16-bit) instruction encodings, for code density.
- **`xw`, version 2.2** — a WCH vendor-custom extension, present in the ISA
  string. Its practical effect was checked directly rather than assumed:
  compiling identical C (including a function with the
  `__attribute__((interrupt))` attribute that this codebase's ISRs also use)
  with and without `xw` in `-march` produces **byte-identical instruction
  sequences** from this GCC 12.2.0; the only diff is the `.attribute arch`
  string embedded in the object file. In other words, on this specific
  toolchain, `xw` is an ELF/tooling identification tag (consumed by things
  like the WCH-Link debug probe or OpenOCD to confirm chip identity), **not**
  a source of extra instructions the compiler will emit on its own. Nothing
  in this codebase should be written expecting `xw` to change codegen; if a
  future WCH-supplied toolchain does hook real custom opcodes to it, that
  would be a toolchain upgrade to verify separately, not something to assume
  from the flag alone.

**Absent, and verified absent:** the `B` (bit-manipulation) extension and its
`Zb*` sub-extensions are not part of this march string, and this is not just
an omission — the assembler in this exact toolchain refuses it outright:

```
$ riscv-wch-elf-gcc -march=rv32imacb -mabi=ilp32 -c -x c -o t.o /dev/null
Error: cannot find default versions of the ISA extension `b'
```

Consequently `__builtin_ctz`/`__builtin_popcount` do not lower to a hardware
instruction; they compile to calls into the software libgcc routines
`__ctzsi2`/`__popcountsi2`, confirmed by compiling both intrinsics at `-O2`
with this exact `-march` and reading the emitted assembly. This project's
existing assumption — no hardware `ctz`/`popcount`, no bit-manipulation
extension — is correct.

`mabi = ilp32` (`boards/genericCH32V203C8T6.json`): 32-bit `int`/`long`/
pointer, soft integer ABI (no hardware float in the ABI; there is no `F`/`D`
in the march string either, so all `float` math in `ADC_DMA.cpp` and
elsewhere is software-emulated).

## 4. Clocks and time

`platformio.ini` defines `-DSYSCLK_FREQ_144MHz_HSI=144000000`
(`platformio.ini:16`). Tracing that through
`System/ch32v20x/system_ch32v20x.c`:

- `SystemInit` calls `SetSysClockTo144_HSI()` (selected by the
  `SYSCLK_FREQ_144MHz_HSI` branch of the `#elif` ladder at
  `system_ch32v20x.c:233-234`).
- That function sets `EXTEN->EXTEN_CTR |= EXTEN_PLL_HSI_PRE`
  (`system_ch32v20x.c:957`) before configuring the PLL. This bit matters:
  `SystemCoreClockUpdate()` (`system_ch32v20x.c:151-159`) shows that with
  `EXTEN_PLL_HSI_PRE` set, the PLL multiplier is applied to the **raw** HSI
  (`SystemCoreClock = HSI_VALUE * pllmull`), and only when that bit is clear
  does the hardware instead divide HSI by 2 first. `HSI_VALUE` for this chip
  family is 8 MHz (`Peripheral/ch32v20x/inc/ch32v20x.h:40`). The function then
  requests `RCC_PLLSRC_HSI_Div2 | RCC_PLLMULL18` (`system_ch32v20x.c:969`) —
  the register field is still named "Div2", but `EXTEN_PLL_HSI_PRE` overrides
  that division at the PLL input stage. Net: PLL input = 8 MHz, multiplier =
  18, **SystemCoreClock = 144 MHz**. This matches the `-D` define and is not
  a case where the two disagree.
- Bus dividers, same function: `RCC_HPRE_DIV1` (HCLK = SYSCLK = **144 MHz**),
  `RCC_PPRE2_DIV1` (**PCLK2 = 144 MHz**), `RCC_PPRE1_DIV2` (**PCLK1 = 72
  MHz**) (`system_ch32v20x.c:960-964`).

SysTick: `src/hal/time_hw.c` programs `STK_CTLR = (1u << 3) | (1u << 0)` —
bit 3 is `STRE` (auto-reload/counter enable per the SDK's own register
layout) and bit 0 is `STE` (counter enable), and critically **not** bit 2,
which would select `STCLK = HCLK` — leaving the divider at its default,
`STCLK = HCLK/8` (`time_hw.c:28`, comment confirms `STCLK=HCLK/8`). At
HCLK = 144 MHz that is a **SysTick tick rate of 18 MHz**, and
`time_hw_init()` computes `time_hw_tpus = (SystemCoreClock/8)/1_000_000 = 18`
ticks per microsecond at runtime rather than hardcoding it — so this holds
for any `SystemCoreClock`, not just 144 MHz.

`time_ticks32()` (`hal/time_hw.h:61-64`) returns the raw 32-bit `STK_CNTL`
register directly — a genuine 32-bit hardware counter, not a truncation of
something wider. At 18 MHz that register wraps every
2^32 / 18,000,000 ≈ **238.6 seconds**. This project's own derivation
(recorded in `Motion_control.cpp:320-329`, "so the smallest transient... is
already several milliseconds wide") and this document's independent
re-derivation agree: **the 18 MHz / ~238 s figure is correct.** Note that a
separate 64-bit path exists — `time_ticks64()` (`time_hw.c:42-45`) stitches
`STK_CNTH:STK_CNTL` into a 64-bit value with the standard double-read-of-high
guard — so code that needs an interval longer than 238 s should call that API
instead of differencing two `time_ticks32()` samples across a suspected
wrap; only 32-bit call sites are exposed to the wraparound, and
`time_diff32`/`time_reached32` in the same header are written to tolerate
exactly one wrap (i.e. deadlines up to ~119 s), not more.

## 5. ADC

`src/ADC_DMA.cpp` runs ADC1 and ADC2 in `ADC_Mode_RegSimult` (dual
simultaneous regular mode, `ADC_DMA.cpp:280`), each scanning 8 channels
(`ADC_NbrOfChannel = 8`, `ADC_DMA.cpp:285,296`) continuously, with
`ADC_SampleTime_71Cycles5` on every channel (`ADC_DMA.cpp:303-304`) and
`RCC_ADCCLKConfig(RCC_PCLK2_Div8)` (`ADC_DMA.cpp:245`). With PCLK2 = 144 MHz
(§4), **ADCCLK = 144 MHz / 8 = 18 MHz**.

Per-conversion time for this SAR ADC family is sample time plus a fixed
successive-approximation overhead; the vendor SDK source in this tree states
the sample-time constant (`ADC_SampleTime_71Cycles5 = 71.5 cycles`,
`ch32v20x_adc.h:129`) but does not itself document the fixed overhead in a
comment. **Unverified against a local source, taken from the well-known
STM32F1-peripheral-compatible constant this SDK is modeled on:** total
conversion ≈ sample-time + 12.5 cycles = 84 cycles. At 18 MHz that is
84 / 18,000,000 ≈ **4.67 µs per channel**. An 8-channel scan is then
≈ 37.3 µs — and because RegSimult drives ADC1 and ADC2 in lockstep, this is
the time for one *paired* 8-channel scan, not double it.

The DMA buffer (`ADC_DMA.cpp:8-12`) is `kCh(8) * kBlock(32) * 2 = 512` words,
split into two 256-word halves via `DMA1_FLAG_HT1`/`DMA1_FLAG_TC1`. Each
32-bit DMA word holds one channel's ADC1 result in the low half and ADC2's in
the high half (unpacked by `adc_pair_sum`, `ADC_DMA.cpp:77-82`), so one
256-word half is 32 full 8-channel scans:
32 × 37.3 µs ≈ **1.19 ms per half-buffer**, i.e. one `HT`/`TC` DMA interrupt
flag roughly every ~1.2 ms. This reproduces the project's own figure
(`Motion_control.cpp:322-326`, "the DMA half-buffer holds 32 scans and
therefore completes every ~1.2 ms") from the ADC/DMA configuration
independently — **confirmed**, modulo the one unverified constant (the 12.5-
cycle SAR overhead) noted above.

`process_half_update_filter` (`ADC_DMA.cpp:84-130`) maintains a 4-slot ring
(`kNBlocks = 4`) of per-channel block sums and a running total
(`g_acc_sum`), i.e. a boxcar filter over the last 4 half-buffers ≈ **4.8 ms**
of accumulated samples. `ADC_DMA_get_value()` (`ADC_DMA.cpp:168-193`) does
not itself block or trigger a conversion; it calls `ADC_DMA_poll()` to drain
any pending DMA flags into the ring and hands back the most recently
finalized value, so **the value a caller reads is never fresher than the
last completed half-buffer, and represents an average with an effective age
of roughly one boxcar window (~4.8 ms)** — not an instantaneous sample. Any
control loop reading these channels (e.g. `MC_PULL_ONLINE_read`, referenced
in `Motion_control.cpp`) is therefore bounded below by this latency;
treating a single read as an instantaneous key/position sample is wrong by
construction.

## 6. Flash

Page/erase granularity used by this firmware is **256 bytes**
(`FLASH_NVM256_PAGE_SIZE = 256`, `Flash_saves.h:15`), via the SDK's *Fast*
program/erase entry points (`FLASH_ErasePage_Fast`, `FLASH_ProgramPage_Fast`,
`ch32v20x_flash.c:838,878`) rather than the standard (non-Fast) 4 KiB-page
API. `Flash_saves.cpp` reserves the whole last 4 KiB sector
(`FLASH_NVM_BASE_ADDR = 0x0800F000`, §2) and subdivides it into 256-byte
pages for: calibration (1 page), motion state (1 page), 4 filament-info
journals (1 page each, wear-leveled by scanning for the first blank slot,
`fil_scan_page`/`Flash_AMS_filament_write`, `Flash_saves.cpp:182-343`), and a
10-page, append-only ring of 8-byte "loaded channel" records (`STA_PAGE_FIRST
= 6`, `STA_PAGE_COUNT = 10`, `Flash_saves.cpp:250-254`) that backs the
merger's loaded latch — written on every channel-ownership change per the
project's loaded-latch design (`docs/BMCU_LOADED_LATCH_DESIGN.md`).

Every write and erase path in `Flash_saves.cpp`
(`flash256_prog`, `flash256_erase`, `flash_word_prog_std`,
`Flash_saves.cpp:44-93`) wraps the operation in
`irq_save_wch()` / `irq_restore_wch()` (`hal/irq_wch.h`), which clears and
later restores CSR `0x800` bits `0x88` — the same CSR and mask the vendor
SDK's own `__enable_irq`/`__disable_irq` use
(`Core/ch32v20x/core_riscv.h:130-145`). **This is a global interrupt
disable for the full duration of the flash operation**, not a
peripheral-scoped lock. The `Fast` erase/program routines themselves
(`ch32v20x_flash.c:838-899`) are blocking spin loops on `FLASH->STATR &
SR_BSY` / `SR_WR_BSY` with no software timeout (unlike the non-Fast
`FLASH_ErasePage`, which uses the `EraseTimeout` constant at
`ch32v20x_flash.c:59`) — so the CPU is inside the interrupt-disabled section
for however long the hardware actually takes to erase or program.

**Unverified from any local source:** the actual wall-clock duration of a
256-byte fast erase or fast program cycle. Neither the SDK source in this
tree nor this repository states it in cycles or microseconds; it is a
datasheet figure this document does not have access to. Do not carry
forward any prior "a flash write costs about N ms" number unless it is
re-derived from the WCH datasheet or measured on hardware — treat it as
unknown, not as "small."

What is verified is the *shape* of the cost, and it is the one the soft-reset
gate and the printer bus both have to respect:

- While a flash write or erase is in progress, **all** interrupts are
  masked, including the USART1/DMA1_Channel5 receive path for the printer
  bus (`_bus_hardware.cpp:474-475,522-523`, both declared
  `__attribute__((interrupt("WCH-Interrupt-fast")))`). The printer bus runs
  at 1.25 Mbps (`docs/BMCU_UART_PHYSICAL_SPEC.md`, §3) — roughly 6.4 µs per
  byte. DMA continues moving already-arrived bytes into the receive buffer
  without CPU involvement, so a masked IRQ does not by itself drop bytes
  that DMA can still land; but frame completion, CRC validation, and the
  main-loop hand-off all stall until interrupts are re-enabled, so a flash
  write extends end-to-end printer-bus latency by its own duration at
  minimum, and risks overrun if the DMA receive buffer fills before the
  stall ends. This is the same failure family recorded in this project's own
  history (Pico UART byte loss: "CRC errors were flash commits blocking
  IRQs, not wiring") for a different processor on the same theme — a flash
  commit that blocks interrupts for too long looks exactly like wire noise
  from the consuming end.
- Every ownership change of the merger latch costs one 8-byte flash program
  (`Flash_AMS_state_write`, `Flash_saves.cpp:439-469`) inside this same
  interrupt-disabled window — not a 256-byte erase on every write, because
  the ring only erases a page (`flash256_erase`) when its 32 slots
  (`STA_SLOTS_PER_PAGE = 256/8 = 32`) are exhausted; most writes are a plain
  8-byte program into the next free slot. Endurance-wise, this means the
  10-page ring absorbs 320 latch changes before it must wrap and erase,
  spreading wear across the whole reserved sector rather than rewriting one
  page per change.

## 7. Execution model

`framework = noneos-sdk` (`platformio.ini:5`) — there is no RTOS. `main.cpp`
runs a single `while (1)` super-loop (`main.cpp:351` onward) that polls the
printer-bus TX/RX pumps, runs the bambu-bus and ahub-bus state machines, and
sends any built response, once per pass. **`bambubus_run()` is called
directly from this loop** (`main.cpp:356`), not from an interrupt context —
confirmed by reading the call site; it is a plain function call inside the
`while(1)` body, with no ISR anywhere in this tree calling into
`bambu_bus_ams.cpp`. This is why the loaded-latch state that
`bambubus_run()` and friends mutate does not need an atomic
compare-and-swap or a lock against itself: the only other writer of that
state is also the main loop, and the two calls cannot interleave. The `A`
(atomics) extension in the ISA (§3) exists in hardware and the toolchain
will use it if asked, but nothing in this codebase's core control flow needs
it for that reason — the real concurrency boundary in this firmware is
between the main loop and its `WCH-Interrupt-fast` ISRs (USART1, DMA1
channels), and that boundary is closed with `irq_save_wch`/`irq_restore_wch`
(a global interrupt mask), not with atomic read-modify-write.

`-msave-restore` (`builder/frameworks/_bare.py:29,68`, applied because this
MCU is neither a `ch5x` nor `ch32h41` part) lets GCC emit calls to compact
`__riscv_save_$N`/`__riscv_restore_$N` helper routines for callee-saved
register spill/reload instead of inlining `sw`/`lw` sequences at every
prologue/epilogue — a code-size trade (smaller `.text`, one extra `call`/
`tail` per non-leaf function) that matters directly here because §2 shows
the real ceiling is 60 KiB, not 64 KiB.

`-msmall-data-limit=8` (`_bare.py:10,67`) tells the compiler that any global
or static object of 8 bytes or less may be placed in `.sdata`/`.sbss` and
addressed `gp`-relative (a single instruction) instead of via a full
`lui`/`addi` address computation. The linker script provides the anchor for
this: `PROVIDE(__global_pointer$ = . + 0x800)` inside the `.data` section
(`Link_CH32V20x.ld`), and the startup code loads `gp` from that exact symbol
before anything else runs (`la gp, __global_pointer$`,
`startup_ch32v20x_D6.S:209`, guarded by `.option norelax` so the assembler
does not try to relax that load before `gp` itself is valid). Small,
frequently-touched globals (the boxcar ring indices in `ADC_DMA.cpp`, the
loaded-latch cache in `Flash_saves.cpp`, etc.) benefit from this
automatically; nothing in application code needs to request it per-symbol.

## 8. Consequences for this codebase

- **Self-modifying code is not available.** Flash writes go through the
  blocking, interrupt-disabling `Fast` program/erase path described in §6;
  there is no separate code-flash-write-while-executing mode documented or
  used anywhere in this tree, and the vendor SDK's own timeout constants
  (`EraseTimeout`, `ProgramTimeout`, `ch32v20x_flash.c:59-60`) exist
  precisely because a flash operation is not something code can run through
  or around. Any design that wants to patch its own `.text` at runtime would
  need to reason about exactly the same interrupt-disabled stall this
  document flags for the loaded-latch writes, at a program-flash-page
  granularity larger than the 256-byte NVM pages — not something this
  firmware does today.
- **No atomic compare-and-swap is needed for the loaded-latch/merger state**,
  because `bambubus_run()` — the only writer — runs in the main loop, not an
  interrupt handler (§7, verified at the call site). The concurrency
  boundary that actually exists in this firmware is main-loop-vs-ISR, closed
  with a global interrupt mask (`irq_save_wch`), not core-vs-core or
  ISR-vs-ISR.
- **A flash write is a stall the printer bus (1.25 Mbps, `USART1`/
  `DMA1_Channel4`/`DMA1_Channel5`, §6) has to survive**, because both its RX
  completion interrupt and its TX-DMA-driven send path are masked for the
  operation's duration. DMA hardware keeps accepting bytes during the mask,
  which is why moving the printer RX path to DMA fixed the historical
  byte-loss symptom this project traced to "flash commits blocking IRQs" —
  but DMA only buys buffer depth, not immunity: a flash operation long
  enough to fill the DMA receive buffer, or one that lands while the main
  loop needed to react to a time-sensitive printer command, still costs
  real protocol latency. This document does not have a verified duration
  for a single 256-byte fast erase/program (§6) to size that risk
  precisely; obtaining one (datasheet or scope measurement) is the natural
  next step before relying on "it's fast enough" for anything on this bus.
- **The soft-reset safety gate's refusal to act during flash activity is a
  proxy, not a direct flash-busy flag.** `bmcu_soft_reset_policy.h` refuses
  a reset when `calibration_busy` is set (`evaluate_safety`,
  `bmcu_soft_reset_policy.h:41-46`), and `MC_PULL_calibration.cpp`'s
  `CalibrationBusyGuard` (`MC_PULL_calibration.cpp:17-18`) holds that flag
  for the calibration routine's *entire* run, which ends in a
  `Flash_MC_PULL_cal_write_all` call (`Motion_control.cpp:3440-3550`) — so
  the gate is closed well before and after the actual flash write, not just
  during it. There is no separate "a flash write is in progress right now"
  flag guarding the merger-latch writes in `bambu_bus_ams.cpp`; those happen
  inline in the main loop and are covered only by the interrupt mask in §6,
  not by the soft-reset gate.
