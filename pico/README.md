# Pico 2 W BMCU monitor

This directory is independent of the BMCU firmware.  It contains a
MicroPython implementation of the Pico side of BMCU Link protocol alpha.3.

It uses the production management connection only:

```text
BMCU H1-1 EXIT_TX -> Pico RX (GP1 by default)
BMCU H1-2 EXIT_RX <- Pico TX (GP0 by default)
BMCU H1-4 GND     -- Pico GND
```

Keep the Pico USB powered and do **not** connect BMCU H1-3 to it.  The link is
115200 baud, 8E1, 3.3 V TTL.  H2 is a programming/printer interface and must
not be used by this program.

## Install

1. Install a current MicroPython build for Pico W/Pico 2 W.
2. Copy `bmcu_link.py` and `main.py` to the Pico filesystem.
3. Optionally copy `config_example.py` to `config.py` and adjust the UART pin
   assignment.
4. Reset the Pico.  It prints decoded BMCU frames as one-line JSON to USB
   serial.

`main.py` is deliberately only a UART gateway.  Give `BMCUMonitor` an
`on_message` callback to publish its typed dictionaries to the actual
Bambuddy optional-device API.  That API is not specified in this repository,
so no guessed HTTP endpoint or authentication scheme is embedded here.

The monitor performs `GET_FULL_STATUS(0x0f, 0x0f)` after a valid `HELLO` and
installs the returned records only when the full record set is complete.
Thereafter it applies `STATUS` and `EVENT` frames incrementally.  It sends a
`PING` every two seconds and treats six seconds without a valid frame as stale.

Only `set_led_mode()` is exposed as a write command.  It does not provide any
printer, motor, slot, or filament control API.

## Wi-Fi configuration

Copy `secrets_example.py` to `secrets.py` **on the Pico filesystem** and set
`WIFI_SSID` and `WIFI_PASSWORD`. `pico/secrets.py` is ignored by Git and must
never be committed. Wi-Fi connection and retry are non-blocking; the UART
reader is run before every network-state service pass.

Set `MDNS_HOSTNAME` there to a unique, lowercase LAN name such as
`bmcu-monitor-a`. It is applied before Wi-Fi station mode is enabled, so the
Pico is reachable as `http://bmcu-monitor-a.local/` as well as by its DHCP IP.
Each Pico must use a different hostname. A Bambuddy systemd service can use
this `.local` address through the host resolver; verify it with
`resolvectl query -p mdns bmcu-monitor-a.local` on the Bambuddy host.

`DEBUG_USB` is `False` by default. Set it to `True` in `config.py` only while
commissioning, because USB JSON printing is intentionally excluded from the
UART receive path. `publish()` is the one integration boundary to replace
with a bounded Bambuddy WebSocket/HTTP transport after its authenticated API
is available.
## Deploy from this workstation

With the Pico held in BOOTSEL mode, run this from the repository root to copy
the verified Pico 2 W MicroPython UF2 to its `RP2350` USB drive:

```powershell
.\pico\flash_micropython.ps1 -BootDrive D
```

After it restarts, identify the new MicroPython COM port.  Create a local
`pico\secrets.py` from `secrets_example.py`, then upload the application:

```powershell
.\pico\deploy.ps1 -Port COMx -SecretsPath .\pico\secrets.py
```

`deploy.ps1` uploads only `bmcu_link.py`, `wifi.py`, `main.py`, and the
explicitly supplied `secrets.py`; it never touches BMCU firmware sources.

## Browser status page

Once Wi-Fi is connected, open `http://<MDNS_HOSTNAME>.local/` (or `http://<Pico-IP>/`) from the same LAN. The page
refreshes once per second and exposes the typed BMCU status, completed full
status snapshots, decoded channel telemetry, decoder error counts, Wi-Fi state, and BMCU-link state.
The channel view keeps printer-facing AMS motion separate from the BMCU controller phase and also shows
cached motor PWM, encoder delta, sensor validity, and motion faults.

The HTTP API is deliberately read-only: `GET /api/status`.  It exposes no
motor, slot, or filament operations, and no LED control endpoint until an
authenticated Bambuddy/UI contract is defined.

The complete Bambuddy-facing field inventory, enum registry, and current
integration limitations are documented in
[`docs/PICO_BAMBUDDY_OUTPUT.md`](../docs/PICO_BAMBUDDY_OUTPUT.md).
