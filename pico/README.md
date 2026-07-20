# Pico W / Pico 2 W BMCU monitor

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

The default `config_example.py` enables two independent hardware links:

- `bmcu-a`: UART0, GP0 TX / GP1 RX
- `bmcu-b`: UART1, GP4 TX / GP5 RX

Each link owns its decoder, sequence tracking, atomic snapshot assembly, event
ring, retry state, liveness, and error counters.

## Install

1. Install a current MicroPython build for Pico W/Pico 2 W.
2. Run `deploy.ps1` to copy the monitor, transport, configuration, and UI
   modules to the Pico filesystem.
3. Optionally copy `config_example.py` to `config.py` and adjust the UART pin
   assignment.
4. Reset the Pico. With `DEBUG_USB=True`, it prints decoded BMCU frames as
   one-line JSON to USB serial for commissioning.

The implementation includes the UART monitor, local HTTP status and
commissioning pages, and the Pico-to-Bambuddy push transport. Its contract is
[`docs/PICO_BAMBUDDY_ENVELOPE.md`](../docs/PICO_BAMBUDDY_ENVELOPE.md).
Authentication credentials and the deployment-specific Bambuddy ingest URL
remain configuration, not repository defaults.

The production route is Pico-initiated push, never Bambuddy polling:

```text
BMCU UART -> Pico envelope queue -> outbound authenticated WebSocket -> Bambuddy
```

See [`docs/PICO_BAMBUDDY_TRANSPORT.md`](../docs/PICO_BAMBUDDY_TRANSPORT.md) for
the connection state, replay, ACK, backpressure, and diagnostic-HTTP boundary.

The monitor requests `GET_FULL_STATUS(0x0f, 0x0f)` after a valid `HELLO`, or
after the first `STATUS` when Pico restarted after BMCU and therefore missed the
one-shot `HELLO`. It installs the returned records only when the full record set
is complete. The monitor requests another snapshot only after reconnect,
sequence gap, incomplete snapshot, or an explicit diagnostic request. Steady state uses incremental
`STATUS`/`EVENT` updates and PING/PONG.
PING is idle-aware: it is sent only after a link has produced no valid frame for
two seconds. Active `STATUS`/`EVENT` traffic therefore suppresses probe frames.


Only `set_led_mode()` is exposed as a write command.  It does not provide any
printer, motor, slot, or filament control API.

## Wi-Fi configuration

Copy `secrets_example.py` to `secrets.py` **on the Pico filesystem** and set
`WIFI_SSID` and `WIFI_PASSWORD`. `pico/secrets.py` is ignored by Git and must
never be committed. Wi-Fi connection and retry are non-blocking; the UART
reader is run before every network-state service pass. Open
`http://<MDNS_HOSTNAME>.local/settings` to provision the trusted-LAN
`ws://` URL and the `telemetry:write` credential. The token is stored on the
Pico but is never returned by the settings API.

Set `MDNS_HOSTNAME` there to a unique, lowercase LAN name such as
`bmcu-monitor-a`. It is applied before Wi-Fi station mode is enabled, so the
Pico is reachable as `http://bmcu-monitor-a.local/` as well as by its DHCP IP.
Each Pico must use a different hostname. This `.local` address is for
commissioning and browser diagnostics only. It is not a production Bambuddy
ingestion endpoint and Bambuddy must not poll its HTTP API.

`DEBUG_USB` is `False` by default. Set it to `True` only while commissioning,
because USB JSON printing is intentionally excluded from the UART receive
path. `publish()` only enqueues; socket work runs in a later cooperative poll.

## Deploy from this workstation

With the Pico held in BOOTSEL mode, run the matching command from the repository
root. The script checks the BOOTSEL volume label before copying, so an RP2040
image cannot accidentally be written to a Pico 2 W (or vice versa).

For the first-generation Raspberry Pi Pico W (RP2040, BOOTSEL label
`RPI-RP2`), use the **Pico W** MicroPython image. Do not use the non-W Pico
image because it does not include Wi-Fi support:

```powershell
.\pico\flash_micropython.ps1 -Board PicoW -BootDrive D `
    -Uf2Path C:\tmp\RPI_PICO_W-20260406-v1.28.0.uf2
```

For Pico 2 W (RP2350, BOOTSEL label `RP2350`):

```powershell
.\pico\flash_micropython.ps1 -Board Pico2W -BootDrive D
```

`Pico2W` remains the default for compatibility with the previous command. The
default UF2 paths are only workstation conveniences; pass `-Uf2Path` when your
download has a different version or filename.

After it restarts, identify the new MicroPython COM port.  Create a local
`pico\secrets.py` from `secrets_example.py`, then upload the application:

```powershell
.\pico\deploy.ps1 -Port COMx -SecretsPath .\pico\secrets.py
```

`deploy.ps1` uploads the Pico monitor, transport, configuration, Wi-Fi, UI,
and main modules plus the explicitly supplied `secrets.py`; it never touches
BMCU firmware sources. The same application files and default GP0/GP1 plus
GP4/GP5 UART mapping work on both Pico W generations.

## Browser status page

Once Wi-Fi is connected, open `http://<MDNS_HOSTNAME>.local/` (or `http://<Pico-IP>/`) from the same LAN. The page
refreshes once per second and exposes the typed BMCU status, completed full
status snapshots, decoded channel telemetry, printer RX/TX counters (including DMA error and timeout), decoder error counts, Wi-Fi state, and BMCU-link state.
The channel view keeps printer-facing AMS motion separate from the BMCU controller phase and also shows
cached motor PWM, encoder delta, sensor validity, and motion faults.

Telemetry HTTP endpoints are deliberately read-only. The only write endpoint is
the CSRF-protected Bambuddy commissioning configuration at
`POST /api/bambuddy/config`. Link-scoped telemetry endpoints are:

- `GET /api/devices`
- `GET /api/devices/<link-id>/status`
- `GET /api/devices/<link-id>/events`

`GET /api/status` remains a local-page compatibility aggregate. The API exposes no
motor, slot, or filament operations, and no LED control endpoint until an
authenticated Bambuddy/UI contract is defined.

The page's one-second refresh is browser-local polling only. It provides no
delivery ACK, replay, or transient-event completeness and must not be used by a
Bambuddy adapter.

The complete Bambuddy-facing field inventory, enum registry, and current
integration limitations are documented in
[`docs/PICO_BAMBUDDY_OUTPUT.md`](../docs/PICO_BAMBUDDY_OUTPUT.md).
