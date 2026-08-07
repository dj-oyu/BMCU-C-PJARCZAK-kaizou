"""Rolling OLED status ticker for the Pico bridge.

The panel shows what only matters while it is happening: which channel owns the
merger, what each motor is doing, which latch just tripped. Counters that are
worth reading after the fact stay in the web UI and the journal -- this is the
scrolling sign above the machine, not a second copy of the diagnostics.

Every render helper is a pure function over the monitor's live STATUS dict, so
the layout is testable on CPython; only OledTicker touches I2C.

Layout on a 128x64 panel (16 columns, 8 rows of the built-in 8x8 font):

    row 0      header: link states, Wi-Fi, uplink        (always visible)
    rows 1..6  the rotating page                         (a link, or health)
    row 7      the marquee: the most recent BMCU events  (always scrolling)

A channel row reads `1*#oK2U 85LJ`:

    1   slot number
    *   owns the shared PTFE merger (`T` once the tail passed the online key)
    #   filament inserted at the entry switch (`.` when not)
    o   filament seen at the online key
    K2  the decoded ks: 0 none, 1 both, 2 external only, 3 internal only
    U   motion, indexed into MOTION_CHARS below
    85  pull percentage
    LJ  latched faults: L low pull, J jam, D DM autoload, F motion fault
"""

try:
    import utime as time
except ImportError:
    import time
import gc

import bambuddy_binary_tcp as tcp

# _filament_motion in src/ams.h: idle, send_out, on_use, before_pull_back,
# pull_back, before_on_use, stop_on_use.
MOTION_CHARS = ".SU<Pb="
# Every state BMCUMonitor can assign, and every uplink state the client class
# defines. A state with no symbol here would fall through to "?" and read as
# `stale`, so ci/test_pico_oled_ticker.py asserts both tables stay complete
# against their sources rather than trusting this list to be maintained.
LINK_CHARS = {"online": "+", "resyncing": "~", "stale": "?",
              "incompatible": "!", "offline": "x", "unknown": "-"}
UPLINK_CHARS = {tcp.ONLINE: "+", tcp.DISABLED: "-", tcp.WIFI_WAIT: "w",
                tcp.BACKOFF: "b", tcp.CONNECTING: "c",
                tcp.CHALLENGE_WAIT: "h", tcp.HELLO_SEND: "h",
                tcp.ACCEPT_WAIT: "a"}
# Abbreviations of the canonical state_field ids. The ids themselves are
# generated into docs/bmcu_link_enum_registry.json from
# src/bmcu_link_protocol.h; only the short spellings are local to the panel.
FIELD_NAMES = {1: "slot", 2: "ins", 3: "onl", 4: "mot", 5: "prs", 6: "led",
               7: "cerr", 8: "mflt"}
DEFAULT_COLUMNS = 16
# Interned once. `"%d" % slot` and `"K%d" % ks` would be two fresh strings per
# channel row, and MicroPython has no reference counting: every one of them
# survives until the next collection.
SLOT_LABELS = ("1", "2", "3", "4")
KS_LABELS = ("K0", "K1", "K2", "K3")
LATCH_LABELS = ("", "L", "J", "LJ", "D", "LD", "JD", "LJD")

# Resolved at import rather than per call: _service consults the clock several
# times per loop iteration and the port never changes underneath us.
if hasattr(time, "ticks_diff"):
    _ticks_diff = time.ticks_diff
    _ticks_add = time.ticks_add
else:
    def _ticks_diff(now, then):
        return now - then

    def _ticks_add(base, delta):
        return base + delta


def _fit(text, columns):
    """Truncate only when it is actually needed; a slice is an allocation."""
    return text if len(text) <= columns else text[:columns]


def link_letter(link_id):
    """`bmcu-a` -> `A`; the panel has no room for the whole id."""
    return link_id[-1].upper() if link_id else "?"


def link_badge(monitor):
    return link_letter(getattr(monitor, "link_id", "")) + \
        LINK_CHARS.get(getattr(monitor, "link_state", None), "?")


def header_line(monitors, wifi_state=None, wifi_rssi=None, client_state=None,
                columns=DEFAULT_COLUMNS):
    parts = [link_badge(monitor) for monitor in monitors]
    if wifi_state == "online":
        # The RSSI is the number that moves; the word "online" is not.
        parts.append("%d" % wifi_rssi if wifi_rssi is not None else "wifi")
    else:
        parts.append((wifi_state or "no wifi")[:4])
    parts.append("U" + UPLINK_CHARS.get(client_state, "~"))
    return _fit(" ".join(parts), columns)


def channel_line(index, status, columns=DEFAULT_COLUMNS):
    number = SLOT_LABELS[index]
    if not status:
        return _fit(number + " no status", columns)
    flags_list = status.get("channel_flags") or []
    flags = flags_list[index] if index < len(flags_list) else None
    if flags:
        # Tail implies loaded, and is the more specific of the two: the
        # filament has left the switch but the merger is still held.
        owner = "T" if flags["tail"] else ("*" if flags["loaded"] else " ")
        key = KS_LABELS[flags["ks"] & 0x03]
        # Indexed from the decoded booleans, not from the raw byte: reading the
        # latch bits out of `raw` here would put a second copy of the wire
        # layout next to decode_channel_flags, which owns it. The table lookup
        # is only there to avoid concatenating up to three one-character
        # strings on a path that runs for every channel row.
        latches = LATCH_LABELS[(1 if flags["low_latch"] else 0) |
                               (2 if flags["jam_latch"] else 0) |
                               (4 if flags["dm_fail_latch"] else 0)]
    else:
        # Firmware too old to send the flags byte. A blank reads as "not
        # reported", which is the truth; zeros would read as "healthy".
        owner, key, latches = " ", "K?", ""
    inserted = "#" if (status.get("inserted_mask", 0) >> index) & 1 else "."
    online = "o" if (status.get("online_mask", 0) >> index) & 1 else " "
    motion = (status.get("motion") or [0, 0, 0, 0])[index]
    motion = MOTION_CHARS[motion] if motion < len(MOTION_CHARS) else "?"
    pull = (status.get("pull_pct") or [0, 0, 0, 0])[index]
    faults = status.get("motion_fault") or [0, 0, 0, 0]
    if faults[index]:
        latches += "F"
    # One format call rather than a seven-step concatenation, which would leave
    # six dead intermediates behind for every row.
    return _fit("%s%s%s%s%s%s%3d%s" % (number, owner, inserted, online, key,
                                       motion, pull, latches), columns)


def link_lines(monitor, columns=DEFAULT_COLUMNS):
    """The six body rows of a per-link page."""
    status = getattr(monitor, "status", None)
    lines = [channel_line(index, status, columns) for index in range(4)]
    if not status:
        lines.append(_fit("%s %s" % (getattr(monitor, "link_id", "?"),
                                     getattr(monitor, "link_state", "?")),
                          columns))
    else:
        slot = status.get("current_slot", 0xFF)
        lines.append(_fit("s%s P%d E%d" % (
            "-" if slot > 3 else SLOT_LABELS[slot],
            status.get("pressure", 0),
            status.get("control_error", 0)), columns))
    decoder = getattr(monitor, "decoder", None)
    lines.append(_fit("crc%d frm%d g%d" % (
        getattr(decoder, "crc_errors", 0) if decoder else 0,
        getattr(decoder, "frame_errors", 0) if decoder else 0,
        getattr(monitor, "sequence_gap_count", 0)), columns))
    return lines


def _uptime_text(seconds):
    if seconds < 3600:
        return "%dm%02ds" % (seconds // 60, seconds % 60)
    return "%dh%02dm" % (seconds // 3600, (seconds // 60) % 60)


def health_lines(uptime_s=0, metrics=None, outbox=None, client=None,
                 journal=None, exception_count=0, wifi=None,
                 temperature_milli_c=None, columns=DEFAULT_COLUMNS):
    heap_free = gc.mem_free() if hasattr(gc, "mem_free") else 0
    loop = getattr(metrics, "loop_gap", None)
    lines = [
        "up " + _uptime_text(uptime_s),
        "heap %dk" % (heap_free // 1024),
        "loop %dms max%d" % (
            (loop.percentile(99) // 1000) if loop else 0,
            (loop.maximum // 1000) if loop else 0),
        "q%d drop%d" % (getattr(outbox, "queue_depth", 0),
                        getattr(outbox, "forced_drop_count", 0)),
        "tcp rc%d jf%d" % (getattr(client, "reconnect_count", 0),
                           getattr(journal, "failure_count", 0)),
        "exc%d %s%s" % (
            exception_count,
            ("%dC " % (temperature_milli_c // 1000))
            if temperature_milli_c is not None else "",
            getattr(wifi, "ip", None) or ""),
    ]
    return [_fit(line, columns) for line in lines]


def event_text(link_id, event):
    """One line of marquee copy for a decoded BMCU event."""
    prefix = link_letter(link_id) + " "
    name = event.get("event_name", "?")
    if name == "state_change":
        return prefix + "ch%d %s %d>%d" % (
            event.get("slot", 0) + 1, FIELD_NAMES.get(event.get("field"), "f?"),
            event.get("previous_value", 0), event.get("value", 0))
    if name == "dm_teardown":
        # held_ms is the width of the lever excursion that aborted the load,
        # the one number a bench watcher cannot recover afterwards.
        return prefix + "ch%d DM st%d cause%d ks%d %dms" % (
            event.get("slot", 0) + 1, event.get("dm_auto_state", 0),
            event.get("cause", 0), event.get("ks", 0),
            event.get("held_ms", 0))
    if name == "sensor":
        return prefix + "ch%d sens%d v%d=%d" % (
            event.get("slot", 0) + 1, event.get("sensor", 0),
            event.get("validity", 0), event.get("value", 0))
    if name == "reset_state":
        return prefix + "reset st%d cancel%d" % (
            event.get("reset_state", 0), event.get("cancel_reason", 0))
    if name == "printer_transaction":
        if not event.get("outcome"):
            return ""  # a transaction that worked is not news
        return prefix + "prn t%02x out%d r%d" % (
            event.get("command", 0) & 0xFF,
            event.get("outcome", 0), event.get("reason", 0))
    if name == "printer_long_transaction":
        if not event.get("outcome"):
            return ""
        # frame_type is 16 bits and the high byte is what distinguishes the
        # long frames; masking it to a byte would name the wrong frame.
        return prefix + "prn L%04x out%d r%d" % (
            event.get("frame_type", 0) & 0xFFFF,
            event.get("outcome", 0), event.get("reason", 0))
    severity = event.get("severity", 0)
    if severity < 2:
        # Below NOTICE, and with no decoder that named it: nothing a 16-column
        # panel can say about it is worth the space.
        return ""
    return prefix + "%s sev%d" % (name, severity)


class OledTicker:
    """Drives the panel from the main loop without ever blocking on a frame."""

    def __init__(self, display, monitors, wifi=None, client=None, metrics=None,
                 outbox=None, journal=None, platform_stats=None,
                 page_ms=5000, refresh_ms=1000, marquee_ms=120,
                 marquee_step=4, error_backoff_ms=5000,
                 exception_count=None):
        self.display = display
        self.monitors = monitors
        self.wifi = wifi
        self.client = client
        self.metrics = metrics
        self.outbox = outbox
        self.journal = journal
        # () -> (wifi_rssi, temperature_milli_c). The main loop already samples
        # both on its own schedule; re-reading the ADC here would double it.
        self.platform_stats = platform_stats
        self.exception_count = exception_count or (lambda: 0)
        self.page_ms = page_ms
        self.refresh_ms = refresh_ms
        self.marquee_ms = marquee_ms
        self.marquee_step = marquee_step
        self.error_backoff_ms = error_backoff_ms
        self.columns = display.width // 8
        self.rows = display.pages
        self.body_rows = max(1, self.rows - 2)
        self.page_index = 0
        self.error_count = 0
        # Accumulated from per-call deltas rather than measured against a start
        # tick: ticks_diff saturates at about 6.2 days, and this bridge is meant
        # to run for months. Held as seconds plus a sub-second remainder rather
        # than milliseconds, because a millisecond counter leaves the 31-bit
        # small int range after 12.4 days and then allocates a big integer on
        # every one of the ~1000 service calls a second.
        self.uptime_s = 0
        self._uptime_frac_ms = 0
        self._service_ms = None
        self._retry_ms = None
        self._content_ms = None
        self._digest = None
        self._page_ms_mark = None
        self._marquee_ms_mark = None
        self._marquee = ""
        self._marquee_x = display.width
        self._messages = []
        self._last_event = [None] * len(monitors)
        self._dirty = (1 << self.rows) - 1
        self._next_flush = 0

    # -- content -----------------------------------------------------------

    def page_count(self):
        return len(self.monitors) + 1

    def body_lines(self):
        index = self.page_index % self.page_count()
        if index < len(self.monitors):
            return link_lines(self.monitors[index], self.columns)
        rssi, temperature = self._platform()
        return health_lines(
            self.uptime_s, self.metrics, self.outbox, self.client,
            self.journal, self.exception_count(), self.wifi, temperature,
            self.columns)

    def content_digest(self):
        """An allocation-free fingerprint of everything the body draws.

        Rebuilding the body costs some sixty short-lived strings. MicroPython
        has no reference counting, so all of them sit in the heap until the
        next collection, which main.py schedules a minute apart at best. Most
        seconds nothing on the panel actually moved, and this is what keeps an
        idle machine from spending ~2 KB/s to redraw the same six rows.
        """
        page = self.page_index % self.page_count()
        value = self._mix(page, page)
        for index in range(len(self.monitors)):
            monitor = self.monitors[index]
            value = self._mix(value, hash(
                getattr(monitor, "link_state", None) or ""))
        wifi_state = getattr(self.wifi, "state", None) or ""
        value = self._mix(value, hash(wifi_state))
        value = self._mix(value, hash(
            getattr(self.client, "state", None) or ""))
        if wifi_state == "online":
            # Only then does the header print it. Mixing the RSSI while it is
            # offscreen would redraw the panel for a number nobody can read.
            rssi, _ = self._platform()
            value = self._mix(value, rssi if rssi is not None else 0)
        if page >= len(self.monitors):
            # The health page carries the uptime, so it redraws on its own
            # schedule -- one second is the resolution it prints.
            return self._mix(value, self.uptime_s)
        monitor = self.monitors[page]
        status = getattr(monitor, "status", None)
        decoder = getattr(monitor, "decoder", None)
        value = self._mix(value, getattr(decoder, "crc_errors", 0) if decoder
                          else 0)
        value = self._mix(value, getattr(decoder, "frame_errors", 0) if decoder
                          else 0)
        value = self._mix(value, getattr(monitor, "sequence_gap_count", 0))
        if not status:
            return value
        for key in ("current_slot", "inserted_mask", "online_mask", "pressure",
                    "control_error"):
            value = self._mix(value, status.get(key, 0))
        motion = status.get("motion") or ()
        pull = status.get("pull_pct") or ()
        faults = status.get("motion_fault") or ()
        flags = status.get("channel_flags") or ()
        for index in range(4):
            if index < len(motion):
                value = self._mix(value, motion[index])
            if index < len(pull):
                value = self._mix(value, pull[index])
            if index < len(faults):
                value = self._mix(value, faults[index])
            if index < len(flags):
                # The raw byte covers ks and every latch bit in one integer.
                value = self._mix(value, flags[index]["raw"])
        return value

    @staticmethod
    def _mix(value, term):
        # 24 bits, not 30. A 32-bit MicroPython port holds small integers in 31
        # signed bits, so an accumulator masked to 0x3FFFFFFF still overflows
        # inside `value * 31` and allocates a big integer for the intermediate
        # -- every step, which is precisely what this function exists to avoid.
        # 0xFFFFFF * 31 + 0xFFFFFF stays under 2**30.
        # The term is masked too: a counter can outgrow the range over a long
        # uptime, and CPython's str hash is 64-bit and often negative.
        return ((value * 31) + (term & 0xFFFFFF)) & 0xFFFFFF

    def _platform(self):
        if self.platform_stats is None:
            return None, None
        try:
            return self.platform_stats()
        except Exception:  # noqa: BLE001 - a stat must not blank the panel
            return None, None

    def collect_events(self):
        """Fold newly decoded events into the marquee copy.

        Indexed with range rather than enumerate: iterating a list is free on
        MicroPython, but enumerate builds an object per call, and this used to
        run at loop rate.
        """
        for index in range(len(self.monitors)):
            monitor = self.monitors[index]
            events = getattr(monitor, "events", None)
            if not events:
                continue
            seen = self._last_event[index]
            # Compared by identity: the window drops from the front, so no
            # index into it stays valid across calls.
            if events[-1] is seen:
                continue
            # A single drain can decode several events, so walk back to the
            # last one shown rather than taking only the newest. Not finding it
            # means the window rolled over and everything in it is unseen.
            start = 0
            for position in range(len(events) - 1, -1, -1):
                if events[position] is seen:
                    start = position + 1
                    break
            self._last_event[index] = events[-1]
            link_id = getattr(monitor, "link_id", "")
            for position in range(start, len(events)):
                text = event_text(link_id, events[position])
                if text:
                    self._messages.append(text)
        if len(self._messages) > 4:
            del self._messages[:len(self._messages) - 4]

    def marquee_text(self):
        if not self._messages:
            return "%s  %s" % (
                " ".join(link_badge(m) for m in self.monitors),
                getattr(self.wifi, "ip", None) or "no events yet")
        return "   ".join(self._messages)

    # -- drawing -----------------------------------------------------------

    def _draw_body(self):
        frame = self.display.frame
        frame.fill_rect(0, 0, self.display.width, (self.rows - 1) * 8, 0)
        rssi, _ = self._platform()
        frame.text(header_line(
            self.monitors, getattr(self.wifi, "state", None), rssi,
            getattr(self.client, "state", None), self.columns), 0, 0)
        lines = self.body_lines()
        for row in range(min(self.body_rows, len(lines))):
            frame.text(lines[row], 0, (row + 1) * 8)
        self._dirty |= (1 << (self.rows - 1)) - 1

    def _draw_marquee(self):
        frame = self.display.frame
        top = (self.rows - 1) * 8
        frame.fill_rect(0, top, self.display.width, 8, 0)
        frame.text(self._marquee, self._marquee_x, top)
        self._dirty |= 1 << (self.rows - 1)

    # -- loop --------------------------------------------------------------

    def service(self, now_ms):
        if self._service_ms is not None:
            elapsed = _ticks_diff(now_ms, self._service_ms)
            if elapsed > 0:
                remainder = self._uptime_frac_ms + elapsed
                if remainder >= 1000:
                    self.uptime_s += remainder // 1000
                    remainder %= 1000
                self._uptime_frac_ms = remainder
        self._service_ms = now_ms
        if self._retry_ms is not None:
            if _ticks_diff(now_ms, self._retry_ms) < 0:
                return
            self._retry_ms = None
        try:
            self._service(now_ms)
        except OSError:
            # A panel that stopped answering must not take the bridge with it,
            # and must not be retried every millisecond either. One attempt per
            # backoff window keeps the log readable and the loop fast.
            self.error_count += 1
            self._retry_ms = _ticks_add(now_ms, self.error_backoff_ms)
            self._dirty = (1 << self.rows) - 1
            raise

    def _service(self, now_ms):
        if self._page_ms_mark is None:
            self._page_ms_mark = now_ms
        elif _ticks_diff(now_ms, self._page_ms_mark) >= self.page_ms:
            self.page_index += 1
            self._page_ms_mark = now_ms
            self._content_ms = None
        if self._content_ms is None or \
                _ticks_diff(now_ms, self._content_ms) >= self.refresh_ms:
            # Redraw only when something the body shows actually moved. The
            # digest costs integer arithmetic; the redraw costs ~2 KB of
            # strings that live until the next collection.
            digest = self.content_digest()
            if digest != self._digest:
                self._digest = digest
                self._draw_body()
            self._content_ms = now_ms
        if self._marquee_ms_mark is None or \
                _ticks_diff(now_ms, self._marquee_ms_mark) >= self.marquee_ms:
            # Event collection rides the marquee's schedule rather than the
            # loop's: at loop rate it was the single largest allocator in the
            # bridge, and the marquee cannot show a message any sooner anyway.
            self.collect_events()
            self._step_marquee()
            self._marquee_ms_mark = now_ms
        self._flush_one_page()

    def _step_marquee(self):
        if self._marquee_x <= -(len(self._marquee) * 8):
            # Re-read the copy only at the wrap: swapping it mid-scroll would
            # make the line jump while somebody is still reading it.
            self._marquee = self.marquee_text()
            self._marquee_x = self.display.width
        else:
            if not self._marquee:
                self._marquee = self.marquee_text()
            self._marquee_x -= self.marquee_step
        self._draw_marquee()

    def _flush_one_page(self):
        """At most one 128-byte I2C write per loop iteration."""
        if not self._dirty:
            return
        for _ in range(self.rows):
            page = self._next_flush
            self._next_flush = (page + 1) % self.rows
            if self._dirty & (1 << page):
                self._dirty &= ~(1 << page)
                self.display.show_page(page)
                return


# config key -> keyword argument, for each constructor `build` feeds. The
# defaults live in the constructors alone: repeating them here as getattr
# fallbacks meant the same number was written twice, and whichever copy the
# call happened to reach won.
BUS_KEYS = (("OLED_I2C_ID", "id"), ("OLED_SDA_PIN", "sda"),
            ("OLED_SCL_PIN", "scl"), ("OLED_I2C_FREQ", "freq"))
DISPLAY_KEYS = (("OLED_WIDTH", "width"), ("OLED_HEIGHT", "height"),
                ("OLED_ADDRESS", "address"), ("OLED_CONTRAST", "contrast"))
TICKER_KEYS = (("OLED_PAGE_MS", "page_ms"), ("OLED_REFRESH_MS", "refresh_ms"),
               ("OLED_MARQUEE_MS", "marquee_ms"),
               ("OLED_MARQUEE_STEP_PX", "marquee_step"))
# Only the pins are defaulted here, because there is nowhere else to put them:
# GPIO6/7 are I2C1 and are clear of the UARTs in the default link map.
BUS_FALLBACKS = {"id": 1, "sda": 6, "scl": 7}


def config_options(config, keys):
    options = {}
    for name, keyword in keys:
        value = getattr(config, name, None)
        if value is not None:
            options[keyword] = value
    return options


def build(config, monitors, **kwargs):
    """Construct the ticker from config, or return None when it is disabled."""
    if not getattr(config, "OLED_ENABLED", True):
        return None
    from machine import I2C, Pin

    from oled_ssd1306 import SSD1306_I2C

    bus = BUS_FALLBACKS.copy()
    bus.update(config_options(config, BUS_KEYS))
    i2c_options = {"sda": Pin(bus["sda"]), "scl": Pin(bus["scl"])}
    if "freq" in bus:
        i2c_options["freq"] = bus["freq"]
    i2c = I2C(bus["id"], **i2c_options)
    display = SSD1306_I2C(i2c, **config_options(config, DISPLAY_KEYS))
    options = config_options(config, TICKER_KEYS)
    options.update(kwargs)
    return OledTicker(display, monitors, **options)
