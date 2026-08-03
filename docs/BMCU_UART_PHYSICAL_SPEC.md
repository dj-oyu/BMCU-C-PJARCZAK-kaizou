# BMCU UART physical interface specification

Status: verified on hardware on 2026-07-14
Scope: BMCU V1.1/V0.4 manufacturing data and the tested black BMCU board

## 1. Purpose

This document fixes the physical and electrical assumptions used by the BMCU
firmware, Raspberry Pi Pico 2 W bridge, and the Bambuddy optional-device
integration. It distinguishes the independent application UART from the
printer/programming UART. They are physically adjacent, but they are not
interchangeable.

The stable runtime architecture is:

```text
A1 mini <-- RS-485 / USART1 --> BMCU <-- H1 / USART3 --> Pico 2 W <-- Wi-Fi --> Bambuddy
```

## 2. Two-row header layout

The manufacturing data names the two parallel four-pin rows H1 and H2. Pin
numbers below are logical manufacturing pin numbers; do not infer left/right
from this diagram when viewing the board from its reverse side.

```text
H2: [1 SWIO]    [2 SWCLK]    [3 MCU_TX]  [4 MCU_RX]
H1: [1 EXIT_TX] [2 EXIT_RX]  [3 3.3V]    [4 GND]
```

| Header pin | Net | MCU/peripheral | Direction at BMCU | Intended use |
| --- | --- | --- | --- | --- |
| H1-1 | `EXIT_TX` | PB10 / USART3_TX | output | Runtime telemetry to Pico |
| H1-2 | `EXIT_RX` | PB11 / USART3_RX | input | Runtime commands from Pico |
| H1-3 | `3.3V` | 3.3 V rail | power | Optional target power/reference |
| H1-4 | `GND` | ground | power | Signal ground |
| H2-1 | `SWIO` | WCH debug interface | bidirectional | Programming/debug |
| H2-2 | `SWCLK` | WCH debug interface | input | Programming/debug |
| H2-3 | `MCU_TX` | PA9 / USART1_TX | output | Printer bus and UART ROM programming |
| H2-4 | `MCU_RX` | PA10 / USART1_RX | input | Printer bus and UART ROM programming |

Manufacturing-net evidence:

- H1-1 `EXIT_TX` connects to U2 pin 21.
- H1-2 `EXIT_RX` connects to U2 pin 22.
- H2-3 `MCU_TX` connects to U2 pin 30 and U7 pin 4 (RS-485 DI).
- H2-4 `MCU_RX` connects to U2 pin 31 and U7 pin 1 (RS-485 RO).
- U7 pins 2 and 3 share `MCU_RTS`; firmware drives this from U2 pin 33 to
  switch the RS-485 transceiver between receive and transmit.

Source: `FlyingProbeTesting.json` in the locally inspected V1.1 manufacturing
package under `tmp/BMCU-hardware-V1.1`. The package uses Chinese directory
names; the authoritative identifiers are the H1/H2/U2/U7 designators and net
names listed above.

## 3. UART settings

| Interface | MCU pins | Settings | Notes |
| --- | --- | --- | --- |
| H1 application link | PB10/PB11 | 115200 baud, 8E1 | Independent BMCU Link |
| Printer bus | PA9/PA10 | 1,250,000 baud, 8E1 | USART1 through U7 RS-485 transceiver |
| WCH ROM UART entry | PA9/PA10 | Starts at 115200 baud, 8N1 | Flasher may negotiate a faster baud |

In WCH terminology, `USART_WordLength_9b` plus even parity carries eight data
bits and one parity bit, so the host setting is 8E1.

## 4. Wiring

### 4.1 UART firmware programming

The `BMCU Burning Tutorial(UART)` wiring deliberately mixes the H1 power pins
with the H2 UART pins:

```text
USB-UART 3.3V -> H1-3 3.3V
USB-UART GND  -> H1-4 GND
USB-UART RX   <- H2-3 MCU_TX / PA9
USB-UART TX   -> H2-4 MCU_RX / PA10
```

This is a programming connection, not the Pico runtime connection. Successful
flashing proves the PA9/PA10 path; it does not test PB10/PB11.

### 4.2 Pico 2 W runtime connection

For a Pico powered from its own USB connector:

```text
BMCU H1-1 EXIT_TX -> Pico UART RX
BMCU H1-2 EXIT_RX <- Pico UART TX
BMCU H1-4 GND     -- Pico GND
BMCU H1-3 3.3V    -- not connected
```

The Pico UART GPIO numbers are a board-software choice. Firmware and the
Bambuddy adapter must describe them as configuration, not as part of the BMCU
connector specification.

Do not join the Pico USB-derived supply to H1-3. Only the grounds and signal
lines are required. A permanent connection is preferred over hot-swapping.

## 5. Electrical and safety constraints

- H1 is a 3.3 V TTL interface. Do not apply 5 V to EXIT_TX, EXIT_RX, or 3.3V.
- Connect ground before TX/RX when commissioning a separately powered device.
- Do not connect two actively driven TX outputs together.
- Do not use H2 MCU_RX for Pico commands while the printer is connected. U7 RO
  also drives that net, so direct Pico drive can cause electrical contention.
- Do not power the BMCU simultaneously from the printer and a USB-UART 3.3 V
  output unless the power design explicitly provides isolation.
- Hot-swapping is not part of the supported operating model. Keep the Pico on
  H1 and keep H2 reserved for programming/debug.
- Optional series resistors (approximately 1 kohm) in Pico TX/RX provide fault
  current limiting during development, but do not make incorrect wiring valid.

## 6. Peripheral ownership and firmware constraints

### USART3

- USART3 owns PB10/PB11 for the H1 link.
- TX uses DMA1 Channel 2 at low priority.
- RX IRQ only enqueues bytes; sync-header parsing, CRC validation, and command
  execution occur in the main loop.
- The interrupt function must have C linkage. With LTO enabled, omitting
  `extern "C"` leaves the weak startup handler installed and the first RX byte
  traps the firmware in the default handler.

### TIM2 remap

TIM2 Full Remap assigns unused TIM2 CH3/CH4 functions to PB10/PB11 and conflicts
with H1. The BMCU motor implementation only uses TIM2 CH1/CH2 on PA15/PB3.
Firmware must therefore use `GPIO_PartialRemap1_TIM2`, which preserves CH1/CH2
on PA15/PB3 while keeping PB10/PB11 available to USART3.

### USART1 and the printer

PA9/PA10 remain owned by the printer protocol during normal operation. H2 is
not a fallback command channel. Firmware must not automatically switch H2 into
a BMCU Link mode based on a probe or timeout.

USART1 RX uses DMA1 Channel 5 in circular mode when `BMCU_PRINTER_RX_DMA=1`;
TX uses DMA1 Channel 4 in normal mode in both RX builds. The main loop polls the
TX transfer-error flag and a 25 ms completion deadline. Either fault disables
the USART DMA request and channel, clears DMA/USART completion state, drives
PA12/RS-485 DE back to receive, and increments the corresponding snapshot
counter. USART TC is the only successful TX completion signal.

## 7. Connection detection

Detect the peer by protocol handshake, not by scanning or dynamically
reassigning pins:

1. BMCU initializes H1 USART3 and emits protocol alpha.3 `HELLO` once at boot.
2. Pico validates `A5 5A` framing and CRC, then sends `GET_STATUS` on H1.
3. BMCU marks the H1 peer present only after a complete, version-compatible,
   CRC-valid command.
4. Pico/Bambuddy marks the BMCU link offline after a configurable STATUS
   timeout; it does not fall back to H2.

Recommended Bambuddy device-online condition:

```text
Pico network session is online
AND last valid BMCU HELLO/STATUS age <= 3 seconds
```

Pin-mode hot switching is unnecessary and prohibited for the production
integration.

## 8. Hardware validation record

The following observations were made with a CH340 adapter on COM6:

1. With the PDF programming wiring, a one-shot PA9 software-UART message
   (`UUUU PA9`) was received. The equivalent PB10 message was not received.
   This proved that the programming data wires were on H2, not H1.
2. After moving the adapter data wires to soldered EXIT_TX/EXIT_RX, H1 produced
   continuous valid BMCU Link frames at 115200 8E1 (566 bytes in 15 seconds;
   later 180 bytes in 5 seconds).
3. `GET_STATUS` sent through EXIT_RX returned a CRC-valid STATUS response with
   the matching sequence number.
4. `SET_LED_MODE` sequence 2 returned ACK payload `11 00`, and the board LED
   changed color.
5. A missing C linkage declaration on `USART3_IRQHandler` was reproduced as a
   stop on the first received byte, then fixed and verified in the ELF symbol
   table as a strong `T USART3_IRQHandler` symbol.

These tests establish both directions of H1 independently from the H2
programming/printer path.

## 9. Bambuddy fork requirements

The Bambuddy fork should treat the Pico as an optional bridge device and H1 as
the only BMCU control link.

- Device type: `bmcu-monitor` or another stable project identifier.
- Report separately: Pico network state, BMCU UART link state, BMCU protocol
  version, firmware version, and capabilities.
- Preserve numeric BMCU event/status fields; convert them to UI labels on the
  Bambuddy side.
- Never advertise BMCU online solely because the Pico is reachable.
- On UART loss, mark BMCU telemetry stale without affecting printer control.
- Initially expose only read-only status and the non-motion LED command.
- Printer pause/stop policy belongs to Bambuddy and must require explicit
  safety rules; loss of Pico/Bambuddy must never block the BMCU main loop.

## 10. Related implementation files

- `src/bmcu_link.cpp`: H1 framing, USART3/DMA, status, and commands.
- `src/Debug_log.cpp`: USART3 RX interrupt binding.
- `src/Motion_control.cpp`: TIM2 Partial Remap required for PB10/PB11.
- `src/_bus_hardware.cpp`: printer USART1 and RS-485 direction control.
- `tools/bmcu_debug.py`: host monitor, active probe, and LED command client.
- `BMCU_LINK_PROTOCOL_ALPHA3.md`: the link frame this UART carries — header, kinds, payloads.
- `BMCU_BINARY_TRANSPORT_V1.md`: the Pico↔Bambuddy transport the bridge forwards to.
- `archive/BMCU_PICO_BAMBUDDY_DESIGN.md`: the original integration design. Archived — its pin
  table predates multi-link and its message list predates three shipped kinds. History only.