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
    def __init__(self, link_id="bmcu-a", link_state="online", status=None):
        self.link_id = link_id
        self.link_state = link_state
        self.status = status
        self.decoder = Decoder()
        self.sequence_gap_count = 0
        self.events = []


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


class HealthPageTests(unittest.TestCase):
    def test_the_health_page_fits_and_reports_the_uptime(self):
        lines = oled_ticker.health_lines(uptime_s=3725)

        self.assertEqual(lines[0], "up 1h02m")
        for line in lines:
            self.assertLessEqual(len(line), 16)


if __name__ == "__main__":
    unittest.main()
