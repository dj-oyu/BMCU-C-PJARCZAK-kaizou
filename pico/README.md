# Pico BMCU Monitor

The Pico firmware bridges up to two BMCU UART links to Bambuddy using the BMB1
persistent binary TCP protocol. Production telemetry, ACK, CONTROL,
diagnostics, runtime logs, crash recovery, and the local diagnostic UI are
binary. There is no JSON WebSocket or HTTPS NDJSON compatibility mode.

## Configuration

Copy `config_example.py` to `config.py` and configure:

- `BMCU_LINKS` for UART IDs and pins (the default maps `bmcu-a` to GP0/GP1
  and `bmcu-b` to GP4/GP5);
- `UART_RXBUF` for per-link receive headroom (4096 bytes by default);
- `BMCU_BINARY_HOST` and `BMCU_BINARY_PORT`;
- `BMCU_BINARY_DEVICE_ID`;
- `BMCU_BINARY_DEVICE_KEY`, an optional bootstrap 256-bit key encoded as 64 hex
  characters (the local UI can provision or replace it);
- journal and queue sizes when the defaults are unsuitable.

Wi-Fi credentials remain in separately provisioned `secrets.py`.

## Runtime design

Validated UART frames enter fixed byte storage before semantic display
decoding. STATUS is coalesced until it receives a global transport sequence;
EVENT, fault, link, loss, CONTROL result, warning log, and crash records are
protected. BMJ1 segments retain important records across restart.

The cooperative order is UART drain, local state, TCP receive/send, bounded
journal flush, bounded local HTTP, then safe-point maintenance. Garbage
collection is scheduled and measured; it is never invoked by an HTTP request.

## Local UI

The root page is static and fetches binary snapshots/deltas:

- `/api/current.bin`
- `/api/events.bin`
- `/api/history/status.bin`
- `/api/diagnostics.bin`
- `/api/logs.bin`

JavaScript performs BMB1 and TLV decoding with `DataView`.

The **Connection settings** section manages the Bambuddy BMB1 host and TCP port
alongside device authentication. Host and port changes are persisted and cause
an immediate reconnect without rebooting. The device-key card can generate a
cryptographically random 256-bit key, copy it once for Bambuddy, accept an
existing 64-hex-character key, and persist it without rebooting. The saved key
is write-only: the read API exposes only configured state and a 48-bit SHA-256
fingerprint. Mutations require a non-simple content type and action header so a
cross-origin form cannot silently replace settings. The UI is intended only
for a trusted LAN.

## OLED status ticker

An optional SSD1306 panel on I2C1 (`GPIO6` SDA, `GPIO7` SCL, 3V3 from physical
pin 36) shows link state as it happens. It is enabled by default and disables
itself with one `oled` warning when no panel answers; set `OLED_ENABLED = False`
to keep it quiet. Panel geometry, address, page dwell, and marquee speed are the
`OLED_*` keys in `config_example.py`.

On a 128x64 panel the top row is always the header (`A+ B? -58 U+`: per-link
state, RSSI, uplink), the bottom row is always a marquee of the most recent
decoded events, and the six rows between rotate through one page per link plus a
Pico health page. A channel row reads `1*#oK2U 85LJ`:

| field | meaning |
| --- | --- |
| `1` | slot number |
| `*` | owns the shared PTFE merger; `T` once the tail passed the online key |
| `#` | filament inserted at the entry switch (`.` when not) |
| `o` | filament seen at the online key |
| `K2` | decoded ks: 0 none, 1 both, 2 external only, 3 internal only, `K?` not reported |
| `U` | motion: `.` idle, `S` send out, `U` on use, `<` before pull back, `P` pull back, `b` before on use, `=` stop on use |
| `85` | pull percentage |
| `LJ` | latches: `L` low pull, `J` jam, `D` DM autoload, `F` motion fault |

The header can also carry an alert badge right after a link's own state, e.g.
`A+L`. Two conditions are computed purely from fields STATUS already carries
-- nobody was watching for them the day one happened for real (see
`handover.md` 10.7):

| badge | condition | what it means |
| --- | --- | --- |
| `L` LATCH | a channel is `on use`, online, and its pull is below 40% | the motor has latched off permanently; the extruder is dragging filament unassisted. The online-key check excludes an ordinary runout, which also parks in on_use/low-pull while the tail clears the buffer |
| `S` STUCK | every channel is idle, pressure reads the idle sentinel (0xFFFF), and some channel's pull is outside 30-70% | parked with a buffer skew, e.g. an unload that never finished |

A badge only lights after its condition holds continuously for `CHIP_HOLD_MS`
(3 s), and only while the data behind it is both a live link and fresh --
two separate gates, both required:

- `BMCUMonitor.is_stale()`, not `link_state`, gates on the link being alive at
  all (any valid frame -- HELLO, EVENT, PONG -- counts). A link can sit in
  `link_state == "stale"` for reasons other than a quiet STATUS feed (a
  snapshot-retry exhaustion path leaves it there while STATUS keeps
  arriving), so `link_state` cannot substitute for it.
- `last_status_ms` (stamped in `bmcu_link.py` on every decoded STATUS) gates
  on the *data* being fresh, via `CHIP_STATUS_MAX_AGE_MS` (10 s). A BMCU can
  keep answering PING -- not stale by the measure above -- while the printer
  has simply stopped polling it for STATUS, in which case `status` is a
  frozen snapshot all the same.

Both gates make the chips **fail dark**: a quiet bus means no badge, ever,
even if the last real reading would still qualify -- never a stale reading
kept alive, and never a guess in either direction. On a bus the printer is
actively polling this changes nothing (STATUS arrives roughly every 80ms
while printing); it only matters on a bench BMCU with the printer powered
down, where the badges now go dark instead of latching on the last thing
they saw. That is a known, accepted limitation -- correct behaviour would
need the Pico to poll `get_status()` on its own schedule, which is a
separate change; `last_status_ms` is also the plumbing that change would
need. `pull_pct` is not itself a dirty source -- the firmware re-sends STATUS
on `set_motion`, motion transitions, the masks, a pressure sentinel-class
change, LED and errors, never on pull alone -- so without either gate a chip
could stay lit off a frozen last-known reading long after it stopped being
true.

Both badges can be lit at once, shown `L` before `S` (the more actively
harmful condition first, see `oled_ticker.py`). While any badge is lit the
marquee shows the alert (`A ch2 LATCH`) instead of the usual event scroll,
since the whole point is that these are the lines nobody was reading.

There is deliberately no DESYNC badge for the 10.5 merger-ownership loss:
`oled_ticker.py`'s alert-chips comment explains why no single STATUS frame
can prove it (the reject that defines a real desync returns before motion is
written; an ordinary retract makes the merger occupied-but-unowned on
purpose). 10.5's own buffer-at-100% is still caught by STUCK.

The panel is driven one 128-byte page per main-loop iteration, roughly 3 ms of
I2C, because pushing a whole 1 KB frame in one pass would stall the UART drain
for about 23 ms. The marquee occupies only the last page and steps every 120 ms;
the body is considered for redraw once a second.

The heap is the binding constraint, not the bus. MicroPython has no reference
counting, so every temporary survives until the next collection, and main.py
schedules that a minute apart at best -- anything allocated per loop iteration
would force implicit collections at arbitrary points in the UART drain and
defeat that policy. So the steady state allocates nothing:

- the loop path (`service` -> `_service` -> `_flush_one_page`) is integer work
  over preallocated buffers; the flush uses per-page memoryviews built once in
  `SSD1306_I2C.__init__` and `i2c.writevto`, so no slice or staging copy is made;
- event collection runs on the marquee's 120 ms schedule, not the loop's, and
  indexes with `range` because `enumerate` allocates an object per call;
- the body is rebuilt only when `OledTicker.content_digest()` -- an
  allocation-free integer fingerprint of every field on the page -- changes.
  A rebuild costs roughly 40 short-lived strings, so on an idle machine it is
  the difference between about 2 KB/s and nothing.

Both the digest accumulator and the uptime counter are kept deliberately small.
A 32-bit port holds small integers in 31 signed bits, so a digest masked to 30
bits still overflows inside `value * 31`, and a millisecond uptime counter
leaves the range after 12.4 days; either one then allocates a big integer per
step, which is the cost these mechanisms exist to remove. Hence the 24-bit mask
in `_mix` and the seconds-plus-remainder uptime. `ci/test_pico_oled_ticker.py`
asserts those invariants, since CPython cannot reproduce the constraint itself.

What the digest saves is idle time, not print time: while a load is running,
`pull_pct` and `motion` move every second, so the body redraws at the full
`OLED_REFRESH_MS` rate and costs its ~2 KB each time -- during exactly the
window when a busy UART can defer collection for up to 300 s. Raising
`OLED_REFRESH_MS` is the only knob for that; the cost is inherent to rendering
through `framebuf.text`, which takes strings.

Retained footprint is about 1.6 KB: the 1 KB framebuffer, the page vectors, at
most four marquee messages, and one event reference per link.

## Deployment and tests

`deploy.ps1` uploads the staged web UI, then every Python module, and places
`main.py` last. Host tests live under `ci/`; canonical cross-repository binary
fixtures live under `tests/fixtures/bmcu_binary/`.

The page itself is not a module. `web_ui.py` streams `www/index.html.gz` off
littlefs with `Content-Encoding: gzip`, so only one 512-byte chunk is resident
per request instead of the whole page sitting in the heap for the entire
uptime. Sources and the build live in `web/`; `tools/build_web_ui.py` stages the
artifact.
