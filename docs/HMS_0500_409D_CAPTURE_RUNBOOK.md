# HMS 0500_409D capture runbook (issue #3, phase 1)

Phase-1 instrumentation is **observe-only**. It changes no byte the BMCU puts on the printer bus and no
registration decision. Its job is to let one capture decide between two competing explanations for the
recurring HMS 0500_409D at print start.

- **H1 — BMCU-side session state.** `src/bambu_bus_ams.cpp` keeps a RAM latch `have_registered`. When the
  printer re-queries registration (`0x05` online_detect, subtype `0x00`) and the latch is already set, the
  firmware returns silently. The latch is cleared only at boot, when the AMS goes offline, and when the
  heartbeat lapses. If the printer keeps the heartbeat alive between prints, the print-start re-query is
  answered with silence.
- **H2 — printer-side authorization lock.** `bmcu-vs-firmware-locks.md` (repository root) reports that Bambu Lab firmware
  added BUS certification frame `0x040D` and authorization frame `0x040E`, that BMCU cannot answer them, and
  that the printer consequently permits the first print after a **printer** power-on and blocks later ones;
  recovery requires resetting the **printer**.

Both predict the same visible symptom. The instrumentation separates them by measuring whether the printer
is still addressing this AMS, whether it is still asking for registration, and whether authorization frames
are flowing — all in one snapshot.

## 0. Fixed parameters of every dataset

**Read state over HTTP, not WebREPL.** `GET /api/snapshot.bin` returns the same per-channel and per-link
data without interrupting the bridge. Reaching the same numbers through WebREPL costs a Ctrl-C and a soft
reset of the Pico, after which both links sit in `resyncing` for roughly 20 seconds and every reading is
meaningless until they recover — long enough to lose the window this runbook is trying to capture.

Record these before any measurement. A dataset without them is not comparable.

| Parameter | Why it matters |
| --- | --- |
| Printer model | A1 mini here; other models gate differently. |
| **Printer firmware version** | A1 behaviour changed at `01.05.00.00` and again at `01.08.00.00`. Always record it. |
| BMCU firmware commit | Instrumentation semantics are pinned to the build. |
| `BAMBU_BUS_AMS_NUM` / build env (`ams_a_010`, …) | Each build measures only its own AMS number. |
| Which BMCU in the chain | In a multi-BMCU setup, a dataset belongs to one device. |

## 1. What to read

One snapshot of the PRINTER_BUS section carries everything needed:

```
python tools/bmcu_debug.py full-status --port <PORT> --section-mask 0x04
```

Three lines matter:

- `AMS_SERVICE gap_now_ms=… gap_max_ms=… gap_max_since_confirm_ms=… ms_since_confirm=…`
- `AMS_REG motion=… stu=… mc_online=… queries=… would_reoffer=… confirms=… resets=… flags=0x… registered=… confirm_settled=… stale=… armed=… have_service=…`
- `PRINTER_AUTH type=0x…. 040D=… 040E=… payload_len=… tick=… outcome=… reason=… response_len=… hash=0x…`

`gap_max_ms` and `gap_max_since_confirm_ms` are monotonic max registers. A lossy readout link — the printer-site
UART is known to be noisy — can delay a read but cannot corrupt them. If a snapshot comes back incomplete,
simply retry; the numbers wait.

## 2. Procedure A — idle between prints (the headline number)

1. Note the fixed parameters from section 0.
2. After each completed print, take one snapshot and log the whole AMS_SERVICE and AMS_REGISTRATION lines
   plus PRINTER_AUTH.
3. Between prints, take a snapshot every few minutes while the machine sits idle.

The headline number is the **idle-between-prints `gap_max_ms`**: the longest stretch during which the
printer sent this AMS none of `0x03`, `0x04`, or `0x21A`. Existing telemetry cannot express this — the
PRINTER_BUS record counts every valid frame including broadcasts and heartbeats, so silence towards *us* is
masked.

Log alongside it:

- `registration_query_count` — is the printer still asking us to register while idle?
- `confirm_count` / `reset_count` — did the session restart on its own (a lapsed heartbeat would show as a
  reset edge followed by a confirm)?
- `would_reoffer_count` — how often a re-offer would have fired under the candidate predicate
  (`registered AND ms_since_confirm >= 3000 ms AND gap_now_ms >= 1500 ms`).

Note that `gap_max_since_confirm_ms` resets at each confirm. If `confirm_count` advanced between two reads,
a between-confirms maximum may have been lost; `gap_max_ms` remains the loss-proof since-boot number.

## 3. Procedure B — the discriminating experiment at a 409D incident

**Do this before touching the printer.** Power-cycling or resetting the printer first destroys the evidence.

1. **Snapshot immediately**, at the moment the print refuses to start.

   *Baseline for the delta checks.* Steps 2 and 3 ask what "advanced since the previous snapshot". The
   baseline is the **most recent Procedure A idle snapshot** for this same BMCU, printer firmware, and boot
   session (`bmcu_boot_session` must match, or the counters restarted and no delta is defined). If no
   Procedure A log exists — a 409D can be the first incident anyone captured — then **the delta checks are
   simply unavailable; do not guess a baseline.** Record that fact in the dataset and fall back to the
   since-boot monotonic values, which need no baseline and are readable from the single snapshot alone:
   `gap_now_ms`, `gap_max_ms`, `confirm_count`, `reset_count`, and the absolute
   `registration_query_count` / `would_reoffer_count` totals. A single snapshot still settles a lot: e.g.
   `registration_query_count == 0` since boot refutes H1 for this session outright, and a large `gap_now_ms`
   with `registered` latched is already the H1 signature. Then take a second snapshot a few minutes later,
   before step 5, to manufacture the missing baseline for the remaining delta questions.
2. From AMS_REGISTRATION, record:
   - did `registration_query_count` advance since the previous snapshot?
   - is `registered` still latched (bit0)?
   - did `would_reoffer_count` advance, and is `reoffer_armed` set right now?
   - did `reset_count` / `confirm_count` move?
3. From FULL_RECORD_PRINTER_AUTH, record: did `040D` and/or `040E` advance around the incident, and what are
   `last_type` and `last_tick`?
4. From AMS_SERVICE, record `gap_now_ms` and `gap_max_ms`.
5. **Now reset ONLY the BMCU, using the soft-reset control.** Leave the printer running and untouched.

   **As of 2026-08-03 there is no way to issue the soft reset, so use the power cycle below.** The command
   itself is implemented at both ends — the BMCU accepts `KIND_REQUEST_SOFT_RESET` and the Pico accepts
   `CONTROL_SOFT_RESET` over BMB1 (`pico/main.py`, `binary_control`) — but nothing calls it. The local HTTP
   route this step used to name, `POST /api/devices/<link-id>/soft-reset`, existed at `aeaf263` and was
   removed by the binary transport migration at `9e1f235`; no `/api/devices` route survives anywhere in
   `pico/`. Bambuddy has CONTROL framing but defines no soft-reset command and offers no control for it. Until
   an invoker is built, the two paths this step used to offer are both dead.

   **Use a genuine BMCU power cycle**: remove BMCU power at the supply, wait for the LEDs to go out, and
   reapply. Leave the printer running and untouched throughout.

   **Do not simply unplug or reseat the RS-485 cable.** Interrupting only the data connection can leave the
   BMCU powered and its RAM state — including `have_registered` — intact, so a "no recovery" result would
   look like H2 when nothing was actually cleared. This is the single discriminating step of the runbook; the
   mechanism has to be one that provably clears BMCU RAM.

   A power cycle clears strictly more than the soft reset would, so it does not weaken the experiment — it
   only means the result cannot distinguish "BMCU RAM cleared" from "BMCU power cycled". If the outcome table
   ends up depending on that distinction, the run has to be repeated once an invoker exists. When it does, the
   soft reset issues `NVIC_SystemReset()`, reinitialising all firmware RAM and clearing the `have_registered`
   latch while the printer never loses power or bus continuity.

   The guard accepts a channel resting in `pressure_ctrl_idle`, so filament being loaded is not by itself a
   refusal — which matters here, because 0500_409D is reached with filament loaded. What it still refuses is
   any channel with motor PWM applied or a controller phase that drives, so a machine that has genuinely
   stopped moving will pass.

   On `BMCU_DM_TWO_MICROSWITCH` builds the reboot does not restart autoload: `Motion_control_init` derives the
   autoload gate and `dm_loaded` from the switch reading at boot, which closes the gate on every channel that
   has filament at a switch. Filament may be left loaded across the reset.

6. **Verify the reset actually happened** before retrying the print. Take a snapshot and confirm **both**:
   - `hw_tick32` is small — seconds since boot, not the large value from step 1 (printed by
     `tools/bmcu_debug.py full-status`), and
   - `bmcu_boot_session` has advanced past the value in the step-1 snapshot (the Pico bridge counts BMCU
     boots per link; it is carried in the synthetic link record, type 240, of `GET /api/snapshot.bin` —
     see `link_record` in `docs/bmcu_wire_layout.json`).

   Also expect `gap_max_ms`, `confirm_count`, and `reset_count` to have restarted from zero. If the boot
   session is unchanged, the BMCU did not reboot — the experiment is void, and its result must not be entered
   in the outcome table.
7. Retry the print. Record whether it starts.

### Four-way outcome table

| Auth frames (`040D`/`040E`) arrived around the incident | Verified BMCU-only reset (steps 5-6) recovers the print | Reading |
| --- | --- | --- |
| Yes | Yes | **Leans H1.** The printer ran its authorization exchange and still accepted a fresh BMCU session without a printer reset, so what blocked the print was BMCU-side session state, not a printer-side lock. |
| Yes | No | **H2.** Printer-side authorization lock, matching `bmcu-vs-firmware-locks.md`: recovery requires resetting the printer, and clearing BMCU state changes nothing. |
| No | Yes | **H1.** No authorization traffic at all; rebooting the BMCU — which clears `have_registered` — fixed it. |
| Either | Reset not verified (step 6 failed) | **Void.** The BMCU did not actually reboot; repeat the experiment at the next incident. |
| No | No | **Neither hypothesis as stated.** Inspect AMS_SERVICE gaps and the PRINTER_BUS / PRINTER_RX records for total bus silence or a physical-layer fault before theorising further. |

### Independent registration discriminator

The registration counters give a verdict on H1 without the reset experiment:

- `registration_query_count` advancing **while** `registered` is latched, together with `would_reoffer_count`
  advancing, means the printer is asking and the BMCU is answering with silence. That is direct evidence for
  H1 — it is precisely the early return taken when the latch is already set.
- `registration_query_count` **not** advancing at print start means the printer never asked. H1 is refuted
  for that incident regardless of what the reset experiment shows, because there was no query to swallow.

Beware the asymmetry: if H2 holds and the printer simply stops emitting `0x05`/`0x00` at print start,
`would_reoffer_count` stays flat forever. Read the **query count delta**, not just the would-reoffer count —
the absence of queries is itself a signal, not a null result.

## 4. Events

Two bounded EVENTs are timestamped into the telemetry stream so an incident is located in time rather than
inferred from counters alone (`RECORD_DIAGNOSTIC_COUNTER`, severity NOTICE, source PRINTER_BUS):

- `counter=1` (`AMS_SERVICE_GAP_MS`) — first crossing of 5000 ms of AMS service silence in one episode.
- `counter=2` (`AMS_WOULD_REOFFER`) — the edge where the candidate re-offer predicate became armed.

Both carry as `value` the `gap_now_ms` sampled at the instant the edge fired, and are edge-latched per
episode: re-arming requires an intervening service frame (id 1) or registration confirm (id 2). That latch is
the only bound on the emission rate — there is no additional time-based suppression, so an event that a
counter delta says should exist is genuinely missing rather than throttled away.

## 5. What phase 1 deliberately does not do

Nothing here re-offers registration, resets the latch, or alters any emitted byte. `would_reoffer_count` and
`reoffer_armed` are computed and reported only. Acting on the predicate is phase 2, and should not be
attempted until a dataset from this runbook says which hypothesis it would be treating.
