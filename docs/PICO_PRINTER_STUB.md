# Raspberry Pi Pico 2 W printer stub for BMCU

Status: design specification

Related documents:

- [BMCU UART physical interface](BMCU_UART_PHYSICAL_SPEC.md)
- [Pico command surface and physical paths](PICO_COMMAND_SURFACE.md)
- [BMCU Link Protocol v2](BMCU_LINK_PROTOCOL_V2.md)
- [BMCU command ownership](BMCU_COMMAND_OWNERSHIP.md)

## 1. Purpose

The printer stub makes a BMCU believe it is connected to a real A1 mini. Pico sends the existing printer wire
protocol through the BMCU printer-bus path, receives the normal BMCU replies, and optionally correlates them
with management telemetry.

The stub does **not** use special BMCU commands, a stub-only parser, or a USER motor-control API.

```text
Pico scenario engine
    -> authentic printer packet/address/CRC/timing
    -> BMCU USART1 printer parser
    -> normal BMCU decision and motor state machine
    -> authentic BMCU printer reply
```

From BMCU firmware's point of view, stub traffic is ordinary printer traffic. Its owner is derived from the
USART1 printer ingress and is therefore `PRINTER`.

Primary uses:

- reproduce normal load, unload, feed, retract, and slot-selection sequences;
- reproduce filament runout and spool-swap failures;
- distinguish printer-parser faults from BMCU motor/sensor faults;
- replay captured A1 mini transactions deterministically;
- test CRC, target, duplicate, timeout, and heartbeat behavior;
- observe the BMCU reply and physical loader outcome without running a print job.

## 2. Operating modes

The human operator selects the physical mode before power-on. Runtime hot switching and automatic pin
detection are outside the supported model.

| Mode | Printer path | H1 management path | 24 V source |
| --- | --- | --- | --- |
| Production monitor | real A1 mini | Pico optional-device monitor | printer |
| Bench stub | Pico RS-485 stub | optional second Pico UART for telemetry | bench supply |

Rules:

- Never connect the real printer and stub to the printer bus simultaneously.
- Never connect the real printer and bench supply as simultaneous 24 V sources.
- Power off before changing the printer/stub harness.
- Stub mode is explicit in Pico/Bambuddy UI and session logs.
- Human physical exclusion is the primary mode interlock.
- BMCU-local motor timeout remains the final protection against Pico/Wi-Fi loss.

## 3. Recommended physical construction

### 3.1 CN1 combined power and printer bus

The V1.1/V0.4 manufacturing net data identifies the logical CN1 pinout:

| CN1 logical pin | Net | Bench connection |
| ---: | --- | --- |
| 1 | `24V` | bench supply positive through fuse/stop switch |
| 2 | `RS485_A` | Pico-side RS-485 transceiver A |
| 3 | `GND` | bench supply negative and transceiver signal ground |
| 4 | `RS485_B` | Pico-side RS-485 transceiver B |

Pin numbers are logical manufacturing numbers. Do not infer physical left/right while viewing the rear of the
board. Use a keyed CN1-compatible 2x2, 3.0 mm-pitch harness.

Recommended bench harness:

```text
Bench PSU +24 V
    -> inline fuse
    -> normally-open emergency/power stop
    -> CN1-1 24V

Bench PSU negative
    -> CN1-3 GND
    -> Pico RS-485 transceiver GND

Pico UART TX/RX + direction GPIO
    -> 3.3 V-compatible RS-485 transceiver, >= 1.25 Mbps
    -> CN1-2 A / CN1-4 B
```

The BMCU board converts CN1 24 V to its 3.3 V logic rail and uses 24 V in the four motor-driver stages. Loader
assemblies remain connected to CN2 through CN5 with their normal harnesses. Do not apply raw 24 V directly to
loader motor pins; the BMCU H-bridge outputs drive them.

### 3.2 Pico power and ground

Pico remains USB powered.

```text
Pico USB supply -> Pico
Pico/transceiver GND -- CN1-3/BMCU GND
Pico 3.3 V -- not connected to BMCU H1-3
```

If H1 management telemetry is also connected, its ground is the same BMCU ground. Arrange the harness as a
single common reference and do not join the positive rails.

### 3.3 RS-485 details

- BMCU runtime printer UART: 1,250,000 baud, 8E1.
- Pico transceiver must accept 3.3 V logic and support more than 1.25 Mbps.
- Pico controls transceiver DE and `/RE` for half-duplex turnaround.
- Assert transmit direction before the first start bit.
- Return to receive only after the UART shift register reports transmission complete.
- BMCU contains a 120-ohm termination across its RS-485 pair.
- A point-to-point cable may use termination at the Pico end as well; do not enable multiple unnecessary
  termination resistors from breakout modules.
- Vendor A/B naming is not fully consistent. If a correctly encoded stub receives nothing, power off before
  testing swapped A/B polarity.

### 3.4 H1 monitoring during a stub session

The most useful bench arrangement uses both Pico hardware UARTs:

```text
Pico UART A <-> RS-485 transceiver <-> CN1 A/B <-> BMCU USART1
Pico UART B <-> H1 EXIT_RX/EXIT_TX       <-> BMCU USART3
```

UART A acts as the printer. UART B receives BMCU Link STATUS, EVENT, printer transaction, decision, and sensor
records. The protocols remain independent and are joined only by timestamps and scenario/transaction IDs in
Pico/Bambuddy.

If the selected Pico pinout or harness permits only one UART connection, stub operation can still use the
printer path alone, but detailed H1 decision telemetry will not be available during that run.

### 3.5 H2 TTL path

H2 exposes MCU-side USART1 nets:

```text
H2-3 MCU_TX = PA9 / USART1_TX and U7 DI
H2-4 MCU_RX = PA10 / USART1_RX and U7 RO
```

This is useful for ROM UART flashing, but it is not electrically identical to the printer connector. During
normal firmware operation the onboard U7 receiver can drive `MCU_RX`; an external Pico TX on H2-4 can contend
with it even when the real printer is absent. Therefore the canonical stub harness uses CN1 A/B. H2 direct TTL
stub injection remains an unsupported experiment unless U7 is electrically isolated or disabled.

## 4. Power arrangement

### 4.1 Supply requirements

- Regulated 24 V DC bench supply.
- Current limiting enabled before connection.
- Short, adequately rated power wires and a keyed connector.
- Inline fuse sized from the confirmed motor/harness requirement, not from the bench supply maximum.
- A physical 24 V stop switch within reach.

Do not assign a final current limit until the loader motor and harness ratings or known-good operating current
are established. Commission in stages:

1. Start with all loader assemblies disconnected and verify BMCU logic boot on the supply display.
2. Power off and attach one loader channel.
3. Reapply power with current limit enabled.
4. Send only heartbeat and read-only printer queries first.
5. Run a short, bounded, low-demand motion sequence.
6. Add channels one at a time after their direction, sensors, and stop behavior are confirmed.

If the supply enters constant-current mode unexpectedly, stop the scenario and remove power; do not compensate
by immediately raising the limit.

### 4.2 Power-up sequence

```text
1. Bench supply OFF; Pico stub output disabled.
2. Confirm real printer cable is physically absent.
3. Connect CN1 power, ground, and A/B harness.
4. Connect desired loader channels.
5. Set 24 V and a conservative current limit.
6. Power BMCU from the bench supply.
7. Wait for BMCU boot and Pico receive mode.
8. Establish printer heartbeat/discovery.
9. Request status and verify the expected idle state.
10. Arm and run the selected scenario.
```

### 4.3 Shutdown sequence

```text
1. Stop issuing new printer motion requests.
2. Complete or explicitly abort the active scenario.
3. Confirm BMCU motor state is stopped where telemetry is available.
4. Disable Pico RS-485 transmit.
5. Switch off 24 V.
6. Wait for the board to power down before changing the harness.
```

The physical stop may remove 24 V immediately if motion is unsafe; graceful shutdown is only for normal test
completion.

## 5. Pico software architecture

```text
Scenario controller
    |
    +-- Printer protocol state machine
    |      +-- packet builder
    |      +-- address/CRC encoder
    |      +-- heartbeat scheduler
    |      +-- request/reply matcher
    |      +-- retry/timeout policy
    |
    +-- RS-485 UART driver
    |      +-- RX ring
    |      +-- TX queue
    |      +-- DE/RE turnaround
    |
    +-- Optional H1 BMCU Link decoder
    |
    +-- Trace recorder and Bambuddy transport
```

The stub must be stateful. Sending isolated captured packets without the expected heartbeat, target address,
preceding state, reply handling, and timing may exercise a different BMCU path from the real printer.

Recommended fixed resources:

- no allocation in UART IRQ handlers;
- fixed RX rings for printer bus and H1;
- bounded TX queues;
- fixed packet and decoded-frame buffers;
- monotonic Pico timestamps on every RX/TX edge;
- explicit maximum scenario duration and maximum retry count.

## 6. Printer-wire behavior

### 6.1 Authenticity requirements

To remain indistinguishable from a printer at the BMCU parser, Pico reproduces:

- 1,250,000 baud and even parity;
- packet framing and exact length;
- target/source addressing;
- CRC algorithm and byte order;
- heartbeat/discovery cadence;
- request-to-request spacing;
- reply turnaround and timeout expectations;
- transaction order and required preceding state;
- duplicate/retry behavior where the real printer uses it.

No owner field is sent to BMCU as an authority claim. The USART1 ingress itself establishes `PRINTER` owner.

### 6.2 Request/reply transaction

```text
Pico: build printer packet
Pico: RS-485 TX direction -> send -> wait TX complete -> RX direction
BMCU: parse through normal USART1 path
BMCU: apply normal validation/decision/state transition
BMCU: return normal printer response
Pico: correlate response by expected type/address/transaction context
Pico: record outcome and any H1 telemetry
```

An unexpected or missing response is retained as an outcome; the scenario engine must not hide it by retrying
forever.

## 7. Scenario catalog

### 7.1 Non-mechanical commissioning

1. Receive idle bus data without transmitting.
2. Send heartbeat/discovery only.
3. Query device/status/capability data.
4. Validate BMCU reply CRC, length, and timing.
5. Stop heartbeat and verify printer-offline transition.

### 7.2 Normal mechanical flows

- select each slot without unintended movement;
- short feed and retract;
- normal load sequence;
- normal unload sequence;
- slot-to-slot transition;
- stop/idle transition after completion.

Start with one physically connected loader and no filament resistance. Add filament and additional channels only
after the bounded stop behavior is confirmed.

### 7.3 Runout and spool swap

Record one scenario timeline containing:

```text
printer request
-> BMCU parser classification
-> BMCU decision/reason
-> selected slot and motion state
-> inserted/online/pressure/position observations
-> BMCU printer reply
-> next printer request
-> final slot and filament-path state
```

Vary one condition at a time: runout sensor transition, online key, selected slot, replacement-spool presence,
heartbeat timing, and duplicate request. This makes it possible to locate whether automatic swap diverges at
printer control, BMCU sensing, decision logic, or physical motion.

### 7.4 Negative tests

- invalid CRC;
- invalid length;
- wrong target AMS;
- out-of-range slot;
- duplicate request;
- request while another operation is active;
- heartbeat loss before, during, and after motion;
- delayed or missing expected reply;
- sensor offline/fault where it can be simulated without unsafe wiring.

Negative tests must have explicit expected behavior and must begin with non-mechanical packets.

## 8. Trace and replay format

Store printer traffic as binary records rather than formatted strings:

```text
timestamp_us:u64
direction:u8       // stub->BMCU or BMCU->stub
record_flags:u8
wire_length:u16
wire_bytes[bounded]
```

Keep H1 management records in a parallel stream with Pico receive timestamps. Bambuddy may render names and
JSON later. A replay file also includes:

```text
format_version
source_printer/firmware metadata
baud/parity
scenario name
initial assumptions
timing mode: exact / scaled / step
expected replies and final state
```

Replay modes:

- `exact`: preserve captured inter-packet timing;
- `scaled`: multiply delays while preserving order;
- `step`: operator advances one transaction at a time;
- `semantic`: rebuild known packet fields and CRC from decoded records.

Raw replay is useful for reproduction; semantic replay is required when addresses, counters, or checksums depend
on the current session.

## 9. Bambuddy representation

Bambuddy treats the Pico as one optional device with a test-session facet. Suggested state:

```text
mode: production_monitor | bench_stub
armed: bool
scenario_id
scenario_phase
printer_link_state
last_request/response
expected_vs_actual
BMCU full status baseline
active operation/owner
sensor and motion timeline
power-stop reminder/status where manually supplied
```

Arming is a Pico/Bambuddy operator workflow, not a different BMCU protocol. BMCU continues to see ordinary
printer packets.

## 10. Acceptance criteria

- A valid stub heartbeat produces the same BMCU online state as the real printer.
- Known status queries receive byte-compatible reply classes and valid CRC.
- BMCU classifies all stub traffic as printer ingress/owner.
- No stub-specific command is required in BMCU firmware.
- Real printer and bench supply/stub cannot be present under the documented operating procedure.
- Loss of Pico traffic cannot leave a motor running without a BMCU-local timeout.
- A full runout/spool-swap scenario can be correlated with H1 decision and sensor records.
- Trace replay has bounded duration/retries and preserves unexpected responses.
- Power removal and reconnect do not require hot-swapping signal pins.
