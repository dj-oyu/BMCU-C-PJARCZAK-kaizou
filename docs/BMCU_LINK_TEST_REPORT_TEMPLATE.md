# BMCU Link validation report

## Build identity

- Git commit:
- Firmware SHA256:
- PlatformIO environment:
- Build parameters (`BAMBU_BUS_AMS_NUM`, `AMS_RETRACT_LEN`, board flags):
- Pico firmware / MicroPython version:
- Pico application commit:

## Physical setup

- BMCU hardware revision:
- Pico hardware revision:
- Management UART pins and wiring:
- Printer / 24 V supply:
- USB supply:
- Instrumentation and probe points:

## Baseline

- Flash bytes / capacity:
- RAM bytes / capacity:
- Printer RX ISR maximum:
- Parser/handler maximum:
- Motion update maximum:
- Management service maximum:
- TX queue high-water / drops by reason:
- RX queue high-water / drops by reason:
- Snapshot retries:

Attach the JSON emitted by `ci/capture_firmware_baseline.py` and the compiler map file.

## Test result

| Case | Channel/link | Result | Measurements / evidence |
| --- | --- | --- | --- |
| Clean boot and HELLO | | | |
| Full snapshot | | | |
| Calibration liveness | | | |
| Load / on-use / unload | | | |
| UART corruption / reconnect | | | |
| Idle endurance | | | |

## Deviations and open defects

- None recorded.
