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
BMCU_SIM_IMAGE=firmware.bin python -m sim.shard gear_slip   # one CI shard
```

CI runs the tests as parallel shards (`sim/shard.py`, one runner each); the
required `sim` check passes only when every shard does. A test class that is
not named in `SHARDS` runs in the `rest` shard, so a new test is never left
out; give a class its own shard once it takes minutes.

A bare-board 8-second boot takes about 8–15 s of wall time; with feeder
modules and encoders attached the firmware bit-bangs four AS5600 buses
continuously and the simulator runs at roughly 1/6 of real time. Busy-waits on SysTick are
fast-forwarded (never past the next scheduled peripheral event); everything
else executes instruction by instruction at 1 instruction = 1 HCLK cycle.

The trace (JSON Lines) has one record per Pico message (exactly as
`BMCUMonitor` emits it), printer frame sent or received (CRC-checked), flash
erase/program, and RS-485 violation, plus an end-of-run summary listing any
registers the firmware touched that have no model yet.

## Sensor world and scenarios

`sim/world.py` stands in for what is plugged into the board, so a failure seen
on the printer can be replayed:

- **Pull (buffer) sensors** on ADC PA6/4/2/0: voltage from the buffer slider
  position, which feeding into a held strand compresses and the extruder
  pulling stretches.  An unplugged module reads 0 V.
- **Key (microswitch) sensors** on ADC PA7/5/3/1: one of four voltage levels
  for none / outer only / inner only / both, from where the filament front
  and tail are.
- **AS5600 encoders**: an I2C slave per feeder on the bit-banged pins
  (SCL PB15/14/13/12, SDA PD0/PC15/PC14/PC13) answering raw angle, angle,
  status, AGC and magnitude.  The angle follows the drive gear.
- **Motors**: signed duty from the H-bridge PWM (TIM4/TIM3/TIM2), a
  first-order speed response and a dead band; the gear only moves filament
  whose front has reached it.
- **The user's hand**: `insert` puts the front just past the inner switch and
  keeps pushing toward the gear for `user_push_s`.  Whether the firmware spins
  the gear while the hand is still there decides whether it catches.

A scenario (JSON, format in `sim/scenario.py`) schedules what happens and when:
insert, remove, runout, jam, extruder pull, printer frames, a calibration
gesture, and `override` to force any one reading (key or pull voltage, raw
angle, magnet status, an encoder going offline, a stalled motor).

```sh
# first-boot calibration once, keep the flash
python -m sim.run fw.bin --scenario sim/scenarios/calibrate.json --nvm-out cal.bin
# replay from the calibrated state
python -m sim.run fw.bin --scenario sim/scenarios/jam_ch0.json --nvm-in cal.bin --trace jam.jsonl
```

`--nvm-in` takes only the bytes past the image, so the same dump works with a
newer build, the way a reflash keeps NVM on the device.  The trace gains a
`world` record every 100 ms (front/tail position, speed, buffer, angle,
drive, voltages, active overrides).

## Recordings

`python -m sim.bmb1_extract export.db --from ... --to ... --out w.jsonl` cuts a
window out of a Bambuddy BMB1 export (the `bmcu_binary_*` tables) and decodes
every stored frame with the Pico's own decoder: STATUS, EVENT and full-status
snapshot records (CHANNEL: PWM, encoder delta, controller phase; DM_KEY: key
voltages and learned thresholds).

`sim/recordings/2026-11-25_ams_a_short_insert.jsonl` is such a window: on
AMS-A, ch0 went online and the auto-load gave up twice with
`dm_teardown(dm_auto_state=2, cause=3, ks=2, held_ms=5000)`.  The simulator
reproduces that record field for field from "filament pushed past the outer
switch only, then let go", and `test_firmware_sensors` checks it.

Fitted from the 2026-11-24..26 AMS-A export: key voltage with no filament
(8-13 mV) and with both switches pressed (1845-1868 mV).  Autoload PWM 900
in the recording matches what the simulated firmware commands.

**Every other number in `Params` is an assumption** (switch and gear positions,
voltages, motor speed, buffer travel, path length) until fitted to Pico
recordings.  The checks built on them use loose bounds for that reason.

## What the first stage checks

`sim/tests/test_firmware_boot.py`, on a real build:

- the image boots with no CPU fault,
- HELLO (wire version 0x83) and STATUS replies appear on the monitor link with
  zero decoder CRC/frame errors, and every GET_STATUS is answered,
- every printer `0x03` short-frame query gets exactly one reply whose CRC8 and
  CRC16 validate under the public parameters,
- the BMCU transmits only while it drives RS-485 DE, and the printer never has
  to talk over it.

`sim/tests/test_firmware_sensors.py` runs the first-boot calibration with the
gesture, checks that it is written to flash, then boots again from that flash,
inserts filament in channel 0 and checks that only channel 0 is driven, the
filament moves, the channel goes online on the monitor link and every encoder
is polled without a NACK. It also runs a printer unload and reload. The pull
back must stop with the tip still in front of the gear (the outer lever stays
pressed, so the key never reads none; see `src/pull_back_end_policy.h`), and
the following send_out must drive it forward again.

`sim/tests/test_world.py` checks the world models alone: the AS5600 slave
against a datasheet-sequence I2C master, switch voltages along the path, the
gear catching only while the hand pushes, encoder counts per millimetre,
jams, buffer compression, and bulk DMA against per-item DMA.

`sim/tests/test_models.py` checks the models themselves against outside known
answers (Bambu-Bus header CRC, `binascii.crc_hqx`, the STM32 CRC unit value,
XW encodings built from the LLVM layout).

## Known gaps (not modelled yet)

- **Fitted parameters.** The sensor world runs on assumed values (see
  above).  Fitting them to BMB1 recordings is the next step.
- **Flash contents.** Without `--nvm-in` the firmware starts from erased NVM
  and runs its first-boot calibration.  A dump from a real device has not
  been loaded yet.
- **Other inputs.** LEDs, the RS-485 bus's electrical side and the printer's
  own behaviour beyond the frames a scenario sends are not modelled.
- **Interrupt priority.** Preemption follows PFIC priority, but the exact
  QingKe nesting rules (preemption bits per group) are simplified.
- **Privilege.** `mret` is executed by the model and always stays in machine
  mode. Whether the startup code's `mstatus.MPP=0` takes a real core to user
  mode is not modelled.
- **Timing.** One cycle per instruction; flash wait states and DMA bus
  contention are ignored.
- **Recordings.** Comparing against real Pico recordings (BMB1 exports from
  Bambuddy) and checking traces with the Quint invariants are the next steps.
