# Firmware-independent BMCU simulator

`sim/` runs a **built firmware image, unmodified**, on a model of the
CH32V203C8 and of the world around it, and records what the outside world
observes. The goal is a test bench that cannot be "wrong in the same way" as
the firmware: when the firmware breaks, the simulator does not break with it.

```text
 printer stand-in ──USART1/RS-485──┐                ┌──USART3── Pico stand-in
 (public Bambu-Bus spec)           │  firmware .bin │           (pico/bmcu_link.py,
                                   └── on Unicorn ──┘            the real monitor code)
                                    + PFIC/HPE, SysTick, DMA,
                                      ADC, GPIO, TIM, FLASH, CRC
```

## Independence rules

Nothing in `sim/` is derived from `src/`. Each part has an outside source:

| Part | Source |
| --- | --- |
| CPU core (RV32IMAC) | Unicorn 2.1 (QEMU's RISC-V) |
| QingKe XW compressed loads/stores | LLVM `RISCVInstrInfoXwch.td` encodings |
| Vendor CSRs, HPE, `mtvec` mode 3, PFIC, SysTick | QingKe V4 processor manual / CH32V20x RM |
| USART, DMA, ADC, GPIO, TIM, FLASH, CRC registers | CH32V20x reference manual (STM32F1-compatible layout) |
| Printer framing and CRCs | [Bambu-Research-Group/Bambu-Bus](https://github.com/Bambu-Research-Group/Bambu-Bus) README |
| Monitor link decoding | `pico/bmcu_link.py` — the instrument whose recordings are ground truth |
| RS-485 DE pin (PA12), UART pin roles | board manufacturing data, `docs/BMCU_UART_PHYSICAL_SPEC.md` |

Reading `src/` is allowed only to learn *which* world inputs the firmware
consumes (for example "it waits for the ADC DMA ring to fill"), never to copy
how a model should behave.

Scope follows what the Pico monitor can record: printer transactions,
pressure / pull / insertion signals, and the firmware's reported state. Finer
physics (tube lengths, merger geometry) is added only where needed to explain
those recorded signals.

## Running

```sh
pip install "unicorn==2.1.1"
python -m sim.run firmware.bin --seconds 8 --trace trace.jsonl   # --ams 1 for AMS-B builds
BMCU_SIM_IMAGE=firmware.bin python -m unittest discover -s sim/tests -t . -v
```

An 8-second boot takes about 8–15 s of wall time. Busy-waits on SysTick are
fast-forwarded (never past the next scheduled peripheral event); everything
else executes instruction by instruction at 1 instruction = 1 HCLK cycle.

The trace (JSON Lines) has one record per Pico message (exactly as
`BMCUMonitor` emits it), printer frame sent or received (CRC-checked), flash
erase/program, and RS-485 violation, plus an end-of-run summary listing any
registers the firmware touched that have no model yet.

## What the first stage checks

`sim/tests/test_firmware_boot.py`, on a real build:

- the image boots with no CPU fault,
- HELLO (wire version 0x83) and STATUS replies appear on the monitor link with
  zero decoder CRC/frame errors, and every GET_STATUS is answered,
- every printer `0x03` short-frame query gets exactly one reply whose CRC8 and
  CRC16 validate under the public parameters,
- the BMCU transmits only while it drives RS-485 DE, and the printer never has
  to talk over it.

`sim/tests/test_models.py` checks the models themselves against outside known
answers (Bambu-Bus header CRC, `binascii.crc_hqx`, the STM32 CRC unit value,
XW encodings built from the LLVM layout).

## Known gaps (not modelled yet)

- **Analogue world.** Every ADC input reads 0 V, so no channel is inserted and
  pressure reads 0xFFFF. The next stage drives the pull and key sensors from a
  model fitted to Pico recordings.
- **AS5600 encoders.** The soft-I2C bus has no device on it (all NACK).
- **Motors.** PWM duty is readable from the TIM models but drives nothing.
- **Flash contents.** The simulated flash starts erased past the image, so the
  firmware runs its first-boot calibration (about 5 s) every time. A real
  device's NVM page can be loaded instead once one is dumped.
- **Interrupt priority.** Preemption follows PFIC priority, but the exact
  QingKe nesting rules (preemption bits per group) are simplified.
- **Privilege.** `mret` is executed by the model and always stays in machine
  mode. Whether the startup code's `mstatus.MPP=0` takes a real core to user
  mode is not modelled.
- **Timing.** One cycle per instruction; flash wait states and DMA bus
  contention are ignored.
- **Recordings.** Comparing against real Pico recordings (BMB1 exports from
  Bambuddy) and checking traces with the Quint invariants are the next steps.
