"""The OLED ticker renders live BMCU state without stalling the main loop."""
import importlib.util
import pathlib
import re
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "pico"))

import oled_ticker  # noqa: E402
from oled_ticker import (OledTicker, channel_line, event_text, header_line,
                         link_lines)  # noqa: E402

# Loaded under its own name rather than imported: `bmcu_link` is also a host
# module in ci/host, and binding the Pico copy to the plain name breaks every
# later test that wanted the host one.
_SPEC = importlib.util.spec_from_file_location(
    "pico_oled_link", ROOT / "pico" / "bmcu_link.py")
_link = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_link)
decode_channel_flags = _link.decode_channel_flags


def flags(ks=0, low=False, jam=False, dm=False, loaded=False, tail=False):
    """Built through the wire decoder so the panel cannot drift from it."""
    raw = (ks & 0x03) | (0x04 if low else 0) | (0x08 if jam else 0) | \
        (0x10 if dm else 0) | (0x20 if loaded else 0) | (0x40 if tail else 0)
    return decode_channel_flags(raw)


def status(**overrides):
    base = {"current_slot": 0xFF, "inserted_mask": 0, "online_mask": 0,
            "motion": [0, 0, 0, 0], "pull_pct": [0, 0, 0, 0], "pressure": 0,
            "led_mode": 0, "control_error": 0,
            "channel_flags": [flags() for _ in range(4)]}
    base.update(overrides)
    return base


class Decoder:
    crc_errors = 0
    frame_errors = 0


class Monitor:
    def __init__(self, link_id="bmcu-a", link_state="online", status=None,
                stale=False, last_status_ms=float("inf")):
        self.link_id = link_id
        self.link_state = link_state
        self.status = status
        self.decoder = Decoder()
        self.sequence_gap_count = 0
        self.events = []
        # Real BMCUMonitor.is_stale() is keyed off the last valid frame's
        # timestamp, independent of link_state (a snapshot-retry-exhausted
        # link can sit at link_state == "stale" while STATUS keeps arriving,
        # and a link with any other state can still have gone quiet). The
        # stub takes a flat flag so a test can set it without reproducing
        # that timing logic.
        self._stale = stale
        # Defaults to "infinitely fresh" (never triggers
        # CHIP_STATUS_MAX_AGE_MS) so every test that does not care about the
        # age gate can keep constructing a Monitor with a `status` and not
        # separately think about when it "arrived". Tests that exercise the
        # age gate itself set this explicitly (e.g. to None, matching a real
        # BMCUMonitor that has never decoded a STATUS, or to an old tick).
        self.last_status_ms = last_status_ms

    def is_stale(self, now_ms):
        return self._stale


class Frame:
    def __init__(self):
        self.lines = {}

    def fill_rect(self, x, y, width, height, colour):
        for top in range(y, y + height, 8):
            self.lines.pop(top, None)

    def text(self, value, x, y):
        self.lines[y] = (value, x)


class Display:
    def __init__(self, width=128, pages=8, fail=False):
        self.width = width
        self.pages = pages
        self.frame = Frame()
        self.fail = fail
        self.written = []

    def show_page(self, page):
        if self.fail:
            raise OSError(5)
        self.written.append(page)


class ChannelLineTests(unittest.TestCase):
    def test_a_loaded_channel_shows_the_merger_owner_and_its_pull(self):
        line = channel_line(0, status(
            inserted_mask=0b0001, online_mask=0b0001, motion=[2, 0, 0, 0],
            pull_pct=[85, 0, 0, 0],
            channel_flags=[flags(ks=2, loaded=True), flags(), flags(),
                           flags()]))

        self.assertEqual(line, "1*#oK2U 85")

    def test_the_tail_state_outranks_plain_ownership(self):
        line = channel_line(1, status(
            channel_flags=[flags(), flags(ks=0, loaded=True, tail=True),
                           flags(), flags()]))

        self.assertEqual(line[1], "T")

    def test_every_latch_gets_its_own_letter(self):
        line = channel_line(0, status(
            motion_fault=[3, 0, 0, 0],
            channel_flags=[flags(low=True, jam=True, dm=True), flags(),
                           flags(), flags()]))

        self.assertTrue(line.endswith("LJDF"), line)

    def test_firmware_without_the_flags_byte_reports_unknown_not_healthy(self):
        # Absent is not clear: a `K0` here would invent a channel with no
        # switch closed, which is a diagnosis the BMCU never made.
        line = channel_line(0, status(channel_flags=None))

        self.assertIn("K?", line)
        self.assertNotIn("*", line)

    def test_a_link_with_no_status_yet_says_so(self):
        self.assertEqual(channel_line(2, None), "3 no status")

    def test_a_channel_line_never_overflows_the_panel(self):
        line = channel_line(3, status(
            inserted_mask=0xFF, online_mask=0xFF, motion=[2, 2, 2, 2],
            pull_pct=[100, 100, 100, 100],
            channel_flags=[flags(ks=3, low=True, jam=True, dm=True, loaded=True)
                           for _ in range(4)]), columns=16)

        self.assertLessEqual(len(line), 16)


class HeaderTests(unittest.TestCase):
    def test_the_header_carries_both_links_the_rssi_and_the_uplink(self):
        line = header_line(
            [Monitor("bmcu-a"), Monitor("bmcu-b", "stale")],
            wifi_state="online", wifi_rssi=-58, client_state="online")

        self.assertEqual(line, "A+ B? -58 U+")

    def test_an_offline_station_shows_its_state_instead_of_a_stale_rssi(self):
        line = header_line([Monitor("bmcu-a")], wifi_state="connecting",
                           wifi_rssi=-58, client_state="backoff")

        self.assertEqual(line, "A+ conn Ub")


class LinkPageTests(unittest.TestCase):
    def test_the_body_is_four_channels_plus_a_slot_row_and_an_error_row(self):
        monitor = Monitor(status=status(current_slot=2, pressure=140,
                                        control_error=1))
        monitor.decoder.crc_errors = 7
        monitor.sequence_gap_count = 2

        lines = link_lines(monitor)

        self.assertEqual(len(lines), 6)
        self.assertEqual(lines[4], "s3 P140 E1")
        self.assertEqual(lines[5], "crc7 frm0 g2")


class EventTextTests(unittest.TestCase):
    def test_a_dm_teardown_reports_the_excursion_width(self):
        text = event_text("bmcu-a", {
            "event_name": "dm_teardown", "slot": 2, "dm_auto_state": 1,
            "cause": 3, "ks": 0, "held_ms": 1200})

        self.assertEqual(text, "A ch3 DM st1 cause3 ks0 1200ms")

    def test_a_successful_printer_transaction_is_not_news(self):
        self.assertEqual(event_text("bmcu-a", {
            "event_name": "printer_transaction", "command": 3, "outcome": 0,
            "reason": 0}), "")

    def test_a_failed_printer_transaction_is(self):
        self.assertIn("out2", event_text("bmcu-a", {
            "event_name": "printer_transaction", "command": 0x3D,
            "outcome": 2, "reason": 4}))

    def test_a_state_change_names_the_field_it_moved(self):
        self.assertEqual(event_text("bmcu-b", {
            "event_name": "state_change", "field": 4, "slot": 0,
            "previous_value": 0, "value": 2}), "B ch1 mot 0>2")

    def test_an_unnamed_informational_record_stays_off_the_panel(self):
        self.assertEqual(event_text("bmcu-a", {
            "event_name": "record_9", "severity": 0}), "")


class TickerLoopTests(unittest.TestCase):
    def ticker(self, display=None, monitors=None, **kwargs):
        display = display or Display()
        monitors = monitors if monitors is not None else [
            Monitor(status=status())]
        return OledTicker(display, monitors, **kwargs), display, monitors

    def test_one_page_is_written_per_service_call(self):
        ticker, display, _ = self.ticker()

        for step in range(8):
            ticker.service(step)

        # Eight pages, each once: a whole frame in one call would block the
        # UART drain for the length of a 1 KB I2C write.
        self.assertEqual(sorted(display.written), list(range(8)))

    def test_the_frame_stops_being_rewritten_once_it_is_clean(self):
        ticker, display, _ = self.ticker()
        for step in range(8):
            ticker.service(step)
        display.written.clear()

        ticker.service(9)

        self.assertEqual(display.written, [])

    def test_the_marquee_scrolls_without_touching_the_body_pages(self):
        ticker, display, _ = self.ticker()
        for step in range(8):
            ticker.service(step)
        display.written.clear()
        before = ticker._marquee_x

        for step in range(130, 140):
            ticker.service(step)

        self.assertLess(ticker._marquee_x, before)
        self.assertEqual(set(display.written), {7})

    def test_the_page_rotates_through_every_link_and_the_health_page(self):
        ticker, _, _ = self.ticker(monitors=[Monitor("bmcu-a"),
                                             Monitor("bmcu-b")])

        self.assertEqual(ticker.page_count(), 3)
        ticker.service(0)
        ticker.service(5000)

        self.assertEqual(ticker.page_index, 1)

    def test_a_new_event_reaches_the_marquee_once(self):
        monitor = Monitor(status=status())
        ticker, _, _ = self.ticker(monitors=[monitor])
        event = {"event_name": "state_change", "field": 4, "slot": 0,
                 "previous_value": 0, "value": 2}
        monitor.events.append(event)

        ticker.collect_events()
        ticker.collect_events()

        self.assertEqual(ticker._messages, ["A ch1 mot 0>2"])

    def test_the_marquee_keeps_only_the_last_four_messages(self):
        monitor = Monitor(status=status())
        ticker, _, _ = self.ticker(monitors=[monitor])
        for value in range(6):
            monitor.events.append({"event_name": "state_change", "field": 4,
                                   "slot": 0, "previous_value": 0,
                                   "value": value})
            ticker.collect_events()

        self.assertEqual(len(ticker._messages), 4)
        self.assertTrue(ticker._messages[-1].endswith("0>5"))

    def test_an_unchanged_body_is_not_redrawn(self):
        # The redraw is ~2 KB of short-lived strings and MicroPython keeps
        # every one of them until the next collection, which main.py schedules
        # a minute apart. An idle machine must cost nothing.
        monitor = Monitor(status=status())
        ticker, display, _ = self.ticker(monitors=[monitor])
        for step in range(8):
            ticker.service(step)
        display.written.clear()

        for step in range(1000, 1008):
            ticker.service(step)

        # The marquee keeps scrolling -- that is the point of it -- but no body
        # page is touched.
        self.assertEqual(set(display.written) - {7}, set())

    def test_a_moved_channel_redraws_the_body(self):
        monitor = Monitor(status=status())
        ticker, display, _ = self.ticker(monitors=[monitor])
        for step in range(8):
            ticker.service(step)
        display.written.clear()

        monitor.status["pull_pct"] = [42, 0, 0, 0]
        for step in range(1000, 1008):
            ticker.service(step)

        self.assertTrue(set(display.written) >= {0, 1}, display.written)

    def test_a_tripped_latch_alone_redraws_the_body(self):
        # ks and every latch live in one raw byte; the digest must not read
        # past it and miss a fault that changed nothing else.
        monitor = Monitor(status=status())
        ticker, display, _ = self.ticker(monitors=[monitor])
        for step in range(8):
            ticker.service(step)
        display.written.clear()

        monitor.status["channel_flags"][0] = flags(jam=True)
        for step in range(1000, 1008):
            ticker.service(step)

        self.assertNotEqual(display.written, [])

    def test_a_burst_of_events_all_reach_the_marquee(self):
        # A single UART drain decodes several events. Taking only the newest
        # dropped the ones in between, which are exactly the transitions that
        # explain the last one.
        monitor = Monitor(status=status())
        ticker, _, _ = self.ticker(monitors=[monitor])
        for value in (1, 2, 3):
            monitor.events.append({"event_name": "state_change", "field": 4,
                                   "slot": 0, "previous_value": 0,
                                   "value": value})

        ticker.collect_events()

        self.assertEqual([text[-3:] for text in ticker._messages],
                         ["0>1", "0>2", "0>3"])

    def test_uptime_accumulates_from_deltas_and_ignores_a_backward_clock(self):
        # ticks_diff against a start tick saturates at ~6.2 days; this bridge
        # is meant to run for months.
        ticker, _, _ = self.ticker()

        ticker.service(0)
        ticker.service(1000)
        ticker.service(500)

        self.assertEqual(ticker.uptime_s, 1)

    def test_uptime_carries_sub_second_remainders_into_whole_seconds(self):
        # Seconds plus a remainder, not milliseconds: a millisecond counter
        # leaves the 31-bit small int range after 12.4 days and then allocates
        # a big integer on every service call for the rest of the uptime.
        ticker, _, _ = self.ticker()

        for step in (0, 999, 1001, 1500):
            ticker.service(step)

        self.assertEqual(ticker.uptime_s, 1)
        self.assertEqual(ticker._uptime_frac_ms, 500)

    def test_the_digest_stays_inside_the_device_small_int_range(self):
        # The device constraint cannot be reproduced on CPython, so the mask
        # invariant stands in for it: every intermediate `value * 31` has to
        # stay under 2**30 or MicroPython allocates a big integer for it.
        for term in (0, 1, -7, 10 ** 9, hash("bmcu-a"), -(2 ** 62)):
            value = OledTicker._mix(0xFFFFFF, term)

            self.assertTrue(0 <= value <= 0xFFFFFF, (term, value))
            self.assertLess(value * 31 + 0xFFFFFF, 2 ** 30)

    def test_a_link_state_change_alone_redraws_the_body(self):
        monitor = Monitor(status=status())
        ticker, display, _ = self.ticker(monitors=[monitor])
        for step in range(8):
            ticker.service(step)
        display.written.clear()

        monitor.link_state = "stale"
        for step in range(1000, 1008):
            ticker.service(step)

        self.assertNotEqual(set(display.written) - {7}, set())

    def test_gaining_the_flags_byte_changes_the_digest(self):
        # Old firmware reports channel_flags as None. The panel says "K?" then
        # and prints real values after, so the two must never digest alike.
        monitor = Monitor(status=status(channel_flags=None))
        ticker, _, _ = self.ticker(monitors=[monitor])
        before = ticker.content_digest()

        monitor.status["channel_flags"] = [flags() for _ in range(4)]

        self.assertNotEqual(ticker.content_digest(), before)

    def test_a_recovered_panel_redraws_every_page(self):
        display = Display()
        ticker, _, _ = self.ticker(display)
        for step in range(8):
            ticker.service(step)
        display.written.clear()

        display.fail = True
        with self.assertRaises(OSError):
            ticker.service(200)  # the next marquee step, so a flush is due
        display.fail = False
        display.written.clear()
        for step in range(6000, 6008):
            ticker.service(step)

        # The framebuffer survived the outage, so recovery is a re-flush of
        # every page rather than a rebuild.
        self.assertEqual(sorted(display.written), list(range(8)))

    def test_a_dead_panel_backs_off_instead_of_retrying_every_loop(self):
        ticker, display, _ = self.ticker(Display(fail=True))

        with self.assertRaises(OSError):
            ticker.service(0)
        ticker.service(10)  # inside the backoff window: silent
        with self.assertRaises(OSError):
            ticker.service(6000)

        self.assertEqual(ticker.error_count, 2)

    def test_a_short_panel_drops_body_rows_rather_than_overrunning(self):
        # 128x32 is four rows: header, two body rows, marquee.
        ticker, display, _ = self.ticker(Display(pages=4))
        for step in range(4):
            ticker.service(step)

        self.assertEqual(ticker.body_rows, 2)
        self.assertEqual(sorted(display.written), [0, 1, 2, 3])
        # Header, two body rows, marquee -- nothing drawn past the panel.
        self.assertEqual(sorted(display.frame.lines), [0, 8, 16, 24])


class SingleSourceOfTruthTests(unittest.TestCase):
    """The panel abbreviates canonical tables; it must not fork them."""

    def registry(self):
        import json

        with open(ROOT / "docs" / "bmcu_link_enum_registry.json",
                  encoding="utf-8") as handle:
            return json.load(handle)["enums"]

    def test_every_canonical_state_field_has_a_panel_abbreviation(self):
        # The ids are generated from src/bmcu_link_protocol.h. A field added
        # there and not here would scroll past as "f?" with no way to tell
        # which field moved.
        ids = {int(key) for key in self.registry()["state_field"]}

        self.assertEqual(set(oled_ticker.FIELD_NAMES), ids)

    def test_the_panel_symbols_match_the_wire_link_state_codes(self):
        # binary_api owns the name -> code mapping the snapshot endpoint puts
        # on the wire; the panel must cover exactly that set. An unmapped state
        # falls back to "?", which is the symbol for `stale`, so a link that is
        # merely offline would be indistinguishable from a silent one.
        from binary_api import LINK_STATE_CODES

        self.assertEqual(set(oled_ticker.LINK_CHARS), set(LINK_STATE_CODES))
        symbols = list(oled_ticker.LINK_CHARS.values())
        self.assertEqual(len(symbols), len(set(symbols)))

    def test_every_link_state_the_monitor_assigns_has_a_symbol(self):
        source = (ROOT / "pico" / "bmcu_link.py").read_text(encoding="utf-8")
        assigned = set(re.findall(r'link_state\s*=\s*"([a-z_]+)"', source))

        self.assertTrue(assigned)
        self.assertEqual(assigned - set(oled_ticker.LINK_CHARS), set())

    def test_every_uplink_state_the_client_defines_has_a_symbol(self):
        source = (ROOT / "pico" / "bambuddy_binary_tcp.py").read_text(
            encoding="utf-8")
        states = set(re.findall(r'^[A-Z_]+ = "([a-z_]+)"$', source, re.M))

        self.assertTrue(states)
        self.assertEqual(states - set(oled_ticker.UPLINK_CHARS), set())

    def test_the_motion_table_matches_the_firmware_enum(self):
        # _filament_motion is the AMS enum, not part of the link registry, so
        # the header itself is the source. MOTION_CHARS is positional: an
        # enumerator added in the middle would silently relabel every motion
        # after it.
        header = (ROOT / "src" / "ams.h").read_text(encoding="utf-8")
        body = header.split("enum class _filament_motion", 1)[1]
        body = body.split("{", 1)[1].split("}", 1)[0]
        names = re.findall(r"^\s*([a-z_]+)\s*=\s*(\d+)", body, re.M)

        self.assertEqual(len(names), len(oled_ticker.MOTION_CHARS))
        self.assertEqual([int(value) for _, value in names],
                         list(range(len(names))))

    def test_the_latch_table_covers_every_latch_decode_channel_flags_reports(
            self):
        # channel_line indexes LATCH_LABELS from the decoded booleans, so the
        # table has to span every combination of them.
        latch_bits = ("low_latch", "jam_latch", "dm_fail_latch")

        self.assertEqual(len(oled_ticker.LATCH_LABELS), 2 ** len(latch_bits))
        for name in latch_bits:
            self.assertIn(name, decode_channel_flags(0))


class ConfigWiringTests(unittest.TestCase):
    def test_config_keys_are_only_forwarded_when_they_are_set(self):
        # The defaults live in the constructors. build() must not restate them,
        # or the same number exists twice and whichever copy the call reaches
        # wins.
        class Sparse:
            OLED_PAGE_MS = 250

        self.assertEqual(
            oled_ticker.config_options(Sparse, oled_ticker.TICKER_KEYS),
            {"page_ms": 250})

    def test_every_documented_oled_key_is_wired_to_something(self):
        example = (ROOT / "pico" / "config_example.py").read_text(
            encoding="utf-8")
        documented = set(re.findall(r"^(OLED_[A-Z_]+) =", example, re.M))
        wired = {name for keys in (oled_ticker.BUS_KEYS,
                                   oled_ticker.DISPLAY_KEYS,
                                   oled_ticker.TICKER_KEYS)
                 for name, _ in keys} | {"OLED_ENABLED"}

        self.assertEqual(documented - wired, set())


class ChipConditionTests(unittest.TestCase):
    """Raw (undebounced) chip conditions -- what CHIP_HOLD_MS is applied to."""

    def test_latch_lights_on_use_online_below_the_low_latch_threshold(self):
        raw = oled_ticker._status_chip_bits(status(
            motion=[2, 0, 0, 0], pull_pct=[39, 0, 0, 0], online_mask=0b0001))

        self.assertTrue(raw & oled_ticker.CHIP_LATCH)

    def test_latch_stays_dark_at_the_threshold_itself(self):
        # g_on_use_low_latch trips *below* 40%, not at it; 40 is still inside
        # normal on-use operation.
        raw = oled_ticker._status_chip_bits(status(
            motion=[2, 0, 0, 0], pull_pct=[40, 0, 0, 0], online_mask=0b0001))

        self.assertFalse(raw & oled_ticker.CHIP_LATCH)

    def test_latch_stays_dark_off_use_even_at_zero_pull(self):
        # Low pull during, say, a retract is expected -- the chip is about
        # ON_USE specifically, the phase where the extruder depends on the
        # BMCU keeping up.
        raw = oled_ticker._status_chip_bits(status(
            motion=[4, 0, 0, 0], pull_pct=[0, 0, 0, 0], online_mask=0b0001))

        self.assertFalse(raw & oled_ticker.CHIP_LATCH)

    def test_latch_stays_dark_once_the_channel_has_cleared_the_online_key(
            self):
        # A runout also sits in on_use with pull<40 while the tail clears the
        # buffer -- ordinary operation, not the 10.7 stall. online_mask is
        # what tells the two apart: it clears once the filament passes the
        # online key, which a genuine motor latch never does on its own.
        raw = oled_ticker._status_chip_bits(status(
            motion=[2, 0, 0, 0], pull_pct=[10, 0, 0, 0], online_mask=0b0000))

        self.assertFalse(raw & oled_ticker.CHIP_LATCH)

    def test_stuck_lights_when_parked_with_pressure_pegged_and_pull_off_centre(
            self):
        raw = oled_ticker._status_chip_bits(status(
            motion=[0, 0, 0, 0], pull_pct=[71, 0, 0, 0], pressure=0xFFFF))

        self.assertTrue(raw & oled_ticker.CHIP_STUCK)

        raw = oled_ticker._status_chip_bits(status(
            motion=[0, 0, 0, 0], pull_pct=[29, 0, 0, 0], pressure=0xFFFF))

        self.assertTrue(raw & oled_ticker.CHIP_STUCK)

    def test_stuck_stays_dark_inside_the_idle_deadband(self):
        # The idle controller's own deadband is 30-70, and relief stops right
        # at the 70 edge, so 30 and 70 are ordinary parked readings, not a
        # stuck buffer -- the condition is a strict inequality on purpose.
        raw = oled_ticker._status_chip_bits(status(
            motion=[0, 0, 0, 0], pull_pct=[70, 50, 50, 50], pressure=0xFFFF))
        self.assertFalse(raw & oled_ticker.CHIP_STUCK)

        raw = oled_ticker._status_chip_bits(status(
            motion=[0, 0, 0, 0], pull_pct=[30, 50, 50, 50], pressure=0xFFFF))
        self.assertFalse(raw & oled_ticker.CHIP_STUCK)

    def test_stuck_stays_dark_while_anything_is_moving(self):
        raw = oled_ticker._status_chip_bits(status(
            motion=[2, 0, 0, 0], pull_pct=[90, 0, 0, 0], pressure=0xFFFF))

        self.assertFalse(raw & oled_ticker.CHIP_STUCK)

    def test_stuck_stays_dark_when_pressure_is_not_the_idle_sentinel(self):
        # A live pressure reading means the printer is in the middle of a
        # transaction, not sitting parked, so the same pull skew is not the
        # 10.7 park-jam signature yet.
        raw = oled_ticker._status_chip_bits(status(
            motion=[0, 0, 0, 0], pull_pct=[90, 0, 0, 0], pressure=140))

        self.assertFalse(raw & oled_ticker.CHIP_STUCK)

    def test_no_status_yet_lights_nothing(self):
        self.assertEqual(oled_ticker._status_chip_bits(None), 0)


class ChipDebounceTests(unittest.TestCase):
    """OledTicker debounces chip conditions over CHIP_HOLD_MS."""

    def ticker(self, monitor):
        return OledTicker(Display(), [monitor])

    def latch_monitor(self):
        return Monitor(status=status(
            motion=[2, 0, 0, 0], pull_pct=[10, 0, 0, 0], online_mask=0b0001))

    def test_a_momentary_condition_never_lights_the_chip(self):
        monitor = self.latch_monitor()
        ticker = self.ticker(monitor)

        ticker._update_chips(0)
        # Cleared well inside the hold window.
        monitor.status["motion"] = [0, 0, 0, 0]
        ticker._update_chips(500)

        self.assertEqual(ticker._chip_active_mask[0], 0)

    def test_a_condition_held_past_chip_hold_ms_lights_the_chip(self):
        monitor = self.latch_monitor()
        ticker = self.ticker(monitor)

        ticker._update_chips(0)
        ticker._update_chips(oled_ticker.CHIP_HOLD_MS)

        self.assertTrue(ticker._chip_active_mask[0] & oled_ticker.CHIP_LATCH)

    def test_the_chip_stays_dark_one_tick_before_the_hold_elapses(self):
        monitor = self.latch_monitor()
        ticker = self.ticker(monitor)

        ticker._update_chips(0)
        ticker._update_chips(oled_ticker.CHIP_HOLD_MS - 1)

        self.assertEqual(ticker._chip_active_mask[0], 0)

    def test_the_chip_clears_as_soon_as_the_condition_clears(self):
        monitor = self.latch_monitor()
        ticker = self.ticker(monitor)
        ticker._update_chips(0)
        ticker._update_chips(oled_ticker.CHIP_HOLD_MS)
        self.assertNotEqual(ticker._chip_active_mask[0], 0)

        monitor.status["motion"] = [0, 0, 0, 0]
        ticker._update_chips(oled_ticker.CHIP_HOLD_MS + 10)

        self.assertEqual(ticker._chip_active_mask[0], 0)

    def test_a_stale_link_never_lights_a_chip_no_matter_how_long_held(self):
        # A frozen last-known STATUS is not a current condition. Without this
        # gate a board that stopped talking -- measured on the bench at 58
        # minutes since its last frame, with the printer powered down -- would
        # show a permanent, false alert off whatever it last reported.
        monitor = self.latch_monitor()
        monitor._stale = True
        ticker = self.ticker(monitor)

        ticker._update_chips(0)
        ticker._update_chips(oled_ticker.CHIP_HOLD_MS)

        self.assertEqual(ticker._chip_active_mask[0], 0)

    def test_a_chip_that_went_stale_mid_hold_restarts_from_zero_on_recovery(
            self):
        monitor = self.latch_monitor()
        ticker = self.ticker(monitor)
        ticker._update_chips(0)
        ticker._update_chips(1500)  # short of CHIP_HOLD_MS

        monitor._stale = True
        ticker._update_chips(2000)
        monitor._stale = False
        # If the hold had merely paused rather than reset, this would already
        # be lit (2000 + 1500 >= CHIP_HOLD_MS from the original since[]).
        ticker._update_chips(3500)

        self.assertEqual(ticker._chip_active_mask[0], 0)

        ticker._update_chips(3500 + oled_ticker.CHIP_HOLD_MS)
        self.assertTrue(ticker._chip_active_mask[0] & oled_ticker.CHIP_LATCH)


class ChipStatusAgeTests(unittest.TestCase):
    """CHIP_STATUS_MAX_AGE_MS gates on data freshness, separately from
    is_stale()'s link-liveness gate -- a BMCU that keeps answering PING while
    the printer has simply stopped polling it is not stale, but `status` is
    still a frozen snapshot.
    """

    def ticker(self, monitor):
        return OledTicker(Display(), [monitor])

    def latch_monitor(self, last_status_ms):
        return Monitor(status=status(
            motion=[2, 0, 0, 0], pull_pct=[10, 0, 0, 0], online_mask=0b0001),
            last_status_ms=last_status_ms)

    def test_a_chip_does_not_light_once_status_is_older_than_the_max_age(
            self):
        monitor = self.latch_monitor(last_status_ms=0)
        ticker = self.ticker(monitor)

        # The condition has held continuously since t=0, long past
        # CHIP_HOLD_MS, but the data itself is now older than
        # CHIP_STATUS_MAX_AGE_MS -- there has been no new STATUS to confirm
        # it is still true.
        ticker._update_chips(0)
        ticker._update_chips(oled_ticker.CHIP_STATUS_MAX_AGE_MS + 1)

        self.assertEqual(ticker._chip_active_mask[0], 0)

    def test_an_already_lit_chip_goes_dark_once_its_status_ages_out(self):
        monitor = self.latch_monitor(last_status_ms=0)
        ticker = self.ticker(monitor)
        ticker._update_chips(0)
        ticker._update_chips(oled_ticker.CHIP_HOLD_MS)
        self.assertTrue(ticker._chip_active_mask[0] & oled_ticker.CHIP_LATCH)

        # No new STATUS arrives; the same reading just gets old.
        ticker._update_chips(oled_ticker.CHIP_STATUS_MAX_AGE_MS + 1)

        self.assertEqual(ticker._chip_active_mask[0], 0)

    def test_a_fresh_status_relights_the_chip_with_the_hold_restarted(self):
        monitor = self.latch_monitor(last_status_ms=0)
        ticker = self.ticker(monitor)
        ticker._update_chips(0)
        ticker._update_chips(oled_ticker.CHIP_STATUS_MAX_AGE_MS + 1)
        self.assertEqual(ticker._chip_active_mask[0], 0)  # aged out

        # A new STATUS frame arrives: last_status_ms moves forward, same as
        # BMCUMonitor._handle_frame stamping it on every decoded STATUS.
        now = oled_ticker.CHIP_STATUS_MAX_AGE_MS + 2000
        monitor.last_status_ms = now
        ticker._update_chips(now)
        # Not yet held long enough from this fresh arrival.
        self.assertEqual(ticker._chip_active_mask[0], 0)

        ticker._update_chips(now + oled_ticker.CHIP_HOLD_MS)

        self.assertTrue(ticker._chip_active_mask[0] & oled_ticker.CHIP_LATCH)

    def test_the_age_gate_alone_darkens_a_chip_when_the_link_is_not_stale(
            self):
        # The independence this proves: is_stale() can be false (the link is
        # answering PING) while the age gate alone still refuses the chip,
        # because the two measure different things.
        monitor = self.latch_monitor(last_status_ms=0)
        monitor._stale = False
        ticker = self.ticker(monitor)

        ticker._update_chips(0)
        ticker._update_chips(oled_ticker.CHIP_STATUS_MAX_AGE_MS + 1)

        self.assertFalse(monitor.is_stale(oled_ticker.CHIP_STATUS_MAX_AGE_MS + 1))
        self.assertEqual(ticker._chip_active_mask[0], 0)

    def test_a_monitor_that_has_never_decoded_a_status_never_lights(self):
        # last_status_ms is None until BMCUMonitor decodes its first STATUS
        # frame (see bmcu_link.py __init__). Startup, and a link that only
        # ever gets HELLO/EVENT/PONG, must not light off a status that never
        # arrived.
        monitor = self.latch_monitor(last_status_ms=None)
        ticker = self.ticker(monitor)

        ticker._update_chips(0)
        ticker._update_chips(oled_ticker.CHIP_HOLD_MS)

        self.assertEqual(ticker._chip_active_mask[0], 0)


class ChipDisplayTests(unittest.TestCase):
    """The lit chips have to redraw the panel and stay visible on every page."""

    def test_header_carries_the_lit_chip_letter_for_its_own_link(self):
        line = header_line(
            [Monitor("bmcu-a")], wifi_state="online", wifi_rssi=-58,
            client_state="online", chip_masks=[oled_ticker.CHIP_LATCH])

        self.assertEqual(line, "A+L -58 U+")

    def test_both_lit_chips_order_latch_before_stuck(self):
        # LATCH first: it is the actively-harmful one -- the motor is
        # stopped right now and the extruder is dragging filament through
        # the BMCU unassisted for as long as the print continues (10.7).
        # STUCK is a parked skew; nothing is being dragged.
        mask = oled_ticker.CHIP_STUCK | oled_ticker.CHIP_LATCH
        line = header_line([Monitor("bmcu-a")], chip_masks=[mask])

        self.assertIn("A+LS", line)

    def test_a_lit_chip_changes_the_digest_so_the_header_redraws(self):
        # content_digest is what gates _draw_body; a chip that toggled but
        # left the digest unchanged would light up in state only, never on
        # the panel.
        monitor = Monitor(status=status(
            motion=[2, 0, 0, 0], pull_pct=[10, 0, 0, 0], online_mask=0b0001))
        ticker = OledTicker(Display(), [monitor])
        before = ticker.content_digest()

        ticker._update_chips(0)
        ticker._update_chips(oled_ticker.CHIP_HOLD_MS)

        self.assertNotEqual(ticker.content_digest(), before)

    def test_a_lit_chip_actually_redraws_the_body_through_service(self):
        monitor = Monitor(status=status())
        ticker, display, _ = TickerLoopTests().ticker(monitors=[monitor])
        for step in range(8):
            ticker.service(step)
        display.written.clear()

        monitor.status["motion"] = [2, 0, 0, 0]
        monitor.status["pull_pct"] = [10, 0, 0, 0]
        monitor.status["online_mask"] = 0b0001
        for step in range(1000, 1000 + oled_ticker.CHIP_HOLD_MS + 8000, 1000):
            ticker.service(step)

        self.assertTrue(set(display.written) >= {0, 1}, display.written)

    def test_a_stale_link_never_redraws_the_header_off_a_frozen_condition(
            self):
        # Without the staleness gate this status would light LATCH forever;
        # the point of the gate is that a link that has simply gone quiet
        # must not keep claiming a fault is still happening.
        monitor = Monitor(status=status(
            motion=[2, 0, 0, 0], pull_pct=[10, 0, 0, 0], online_mask=0b0001),
            stale=True)
        ticker = OledTicker(Display(), [monitor])
        before = ticker.content_digest()

        ticker._update_chips(0)
        ticker._update_chips(oled_ticker.CHIP_HOLD_MS)

        self.assertEqual(ticker.content_digest(), before)
        self.assertEqual(ticker._chip_active_mask[0], 0)

    def test_the_marquee_shows_a_lit_chip_ahead_of_queued_events(self):
        monitor = Monitor(status=status(
            motion=[2, 0, 0, 0], pull_pct=[10, 0, 0, 0], online_mask=0b0001))
        ticker = OledTicker(Display(), [monitor])
        monitor.events.append({"event_name": "state_change", "field": 4,
                               "slot": 0, "previous_value": 0, "value": 2})
        ticker.collect_events()
        self.assertTrue(ticker._messages)  # a routine event is queued

        ticker._update_chips(0)
        ticker._update_chips(oled_ticker.CHIP_HOLD_MS)

        self.assertIn("LATCH", ticker.marquee_text())
        self.assertNotIn("mot", ticker.marquee_text())

    def test_the_marquee_falls_back_to_events_once_the_chip_clears(self):
        monitor = Monitor(status=status(
            motion=[2, 0, 0, 0], pull_pct=[10, 0, 0, 0], online_mask=0b0001))
        ticker = OledTicker(Display(), [monitor])
        ticker._update_chips(0)
        ticker._update_chips(oled_ticker.CHIP_HOLD_MS)
        self.assertIn("LATCH", ticker.marquee_text())

        monitor.status["motion"] = [0, 0, 0, 0]
        ticker._update_chips(oled_ticker.CHIP_HOLD_MS + 10)

        self.assertNotIn("LATCH", ticker.marquee_text())


class HealthPageTests(unittest.TestCase):
    def test_the_health_page_fits_and_reports_the_uptime(self):
        lines = oled_ticker.health_lines(uptime_s=3725)

        self.assertEqual(lines[0], "up 1h02m")
        for line in lines:
            self.assertLessEqual(len(line), 16)


if __name__ == "__main__":
    unittest.main()
