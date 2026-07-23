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
- Printer RX mode (`RXNE` / `DMA`):
- Printer response latency p50 / p95 / maximum:
- RX bytes / valid frames:
- RX bad length / header CRC / body CRC:
- RX partial timeout / resync bytes:
- RX DMA error / USART overrun / DMA overrun:
- RX wrap frames / compatibility copies:
- RX dispatch-budget hits:
- Portable parser line / branch coverage:
- RXNE-versus-DMA differential corpus result:
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
| RXNE fallback golden corpus | | | |
| DMA corpus equivalence | | | |
| DMA ring wrap / exact boundary | | | |
| Minimum-gap burst / overrun | | | |
| Partial frame timeout / resync | | | |
| Printer-bus quiescent fault injection | | | |
| Idle endurance | | | |

## Deviations and open defects

- None recorded.
