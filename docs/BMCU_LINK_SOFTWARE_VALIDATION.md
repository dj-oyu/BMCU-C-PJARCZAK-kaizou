# BMCU Link software validation — 2026-07-19

## Implemented stages

| Stage branch | Commit | Scope |
| --- | --- | --- |
| `codex/phase0-baseline` | `02f1d34` | alpha.3 golden corpus, shared host/Pico tests, artifact baseline capture, report template |
| `codex/phase1-calibration-liveness` | `87e9cb5` | management service during calibration and motor direction detection, snapshot `ACK_BUSY` |
| `codex/phase3-4-pico-links` | `817a786` | bounded decoder, snapshot recovery, sequence/tick handling, dual hardware-UART contexts, scoped HTTP API |

## Automated results

- Host protocol, firmware matrix, shared golden-vector, and Pico monitor tests: 23 passed.
- Python syntax validation: `pico/bmcu_link.py`, `pico/main.py`, and `pico/web_ui.py` passed.
- PlatformIO environment: `fw`.
- Build parameters:
  - `BAMBU_BUS_AMS_NUM=0`
  - `AMS_RETRACT_LEN=0.50f`
  - `BMCU_DM_TWO_MICROSWITCH=1`
  - `BMCU_ONLINE_LED_FILAMENT_RGB=0`
  - `DBMCU_P1S=0`
  - `BMCU_SOFT_LOAD=0`
- RAM: 13,892 / 20,480 bytes (67.8%).
- Flash report: 52,316 / 61,440 bytes (85.1%).
- `firmware.bin`: 52,320 bytes.
- `firmware.bin` SHA256: `5f436052a1de86ded0c622ac2855042c7cc02241993653369cc0060cd641ff22`.

## Gates that remain open

The following implementation-plan gates require hardware, measured distributions,
or an external Bambuddy contract and were not marked complete by software tests:

- calibration PING maximum latency, RX drop comparison, and direction-mask retention;
- send/pull fault thresholds and four-channel 24 V fault-injection validation;
- dual-BMCU isolation with real UART noise, simultaneous motion, reboot, and endurance;
- authenticated Bambuddy WebSocket/NDJSON transport and 30-second ACKed FIFO;
- hot-path timing/assembly comparison against an instrumented Phase 0 hardware baseline;
- 8-hour endurance, full build-matrix regression, ABI freeze, and stable `0x01` promotion.

Wire version remains alpha.3 `0x83`. It must not be changed to stable `0x01` until
all Phase 0–7 promotion gates have evidence.

## Hardware execution record

Use [BMCU_LINK_TEST_REPORT_TEMPLATE.md](BMCU_LINK_TEST_REPORT_TEMPLATE.md) for each
single- and dual-link run. Attach wiring, firmware hash, build parameters, latency,
drop counters, and endurance observations.
