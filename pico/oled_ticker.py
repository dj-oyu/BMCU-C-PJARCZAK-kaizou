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

# -- alert chips ---------------------------------------------------------
#
# Failure modes that are visible on the wire today but were invisible on the
# panel when they actually happened (handover.md 10.5, 10.7): a printer power
# cycle erased the BMCU's memory of who owns the merger, and a marginal
# on-use snag tripped the *silent* variant of the low-pull latch, which
# leaves no bit set anywhere except pull_pct itself. Both are catchable from
# fields STATUS already carries; nobody was watching for the combination.
#
# There is no DESYNC chip here, on purpose -- an earlier version of this file
# had one ("no channel loaded, but some channel's motion implies one is
# owned"), and it was wrong in both directions, confirmed against
# src/bambu_bus_ams.cpp rather than guessed:
#
#   - false negative: a real desync (loaded == 0xFF) hits the `!allow_stop`
#     guard at bambu_bus_ams.cpp:384, which returns *before* motion is ever
#     written. The frame that would have proven the desync is exactly the one
#     that never touches the field this chip read. handover.md 10.5's own
#     timeline shows motion sitting at [0,0,0,0] for the entire incident.
#   - false positive: accepting an ordinary retract calls
#     ams_state_set_unloaded(ch) as part of the *same* transaction that moves
#     the channel through before_pull_back/pull_back -- ams_merger_policy.h
#     says outright that the merger is "occupied-but-unowned" on every
#     retract, runout or not. So the motion side of the old condition is true
#     on every routine unload, not just a desync.
#
# In short: the desync is a fact about what a frame was *not allowed to do*,
# not a fact any single STATUS snapshot encodes -- release happens at request
# acceptance, not at completion, and a genuine desync's reject returns before
# motion is written at all. A single-frame predicate over STATUS cannot see
# it. Catching it for real needs either a cross-frame view (e.g. "printer
# commanded before_pull_back/stop_on_use, channel had no loaded flag, request
# was accepted anyway" traced across the printer_transaction event and the
# STATUS that follows it) or a firmware-side signal -- an event emitted at
# the reject itself, or at ams_state_set_unloaded when loaded was already
# 0xFF -- which does not exist on the wire today. Neither belongs in this
# Pico-only branch. Today's incident is still covered without it: 10.5's
# buffer reached 100% twice, which STUCK below catches on its own.

MOTION_ON_USE = 2

# g_on_use_low_latch's actual threshold (Motion_control.cpp). This chip does
# not read the low_latch wire bit, on purpose: 10.7 found a *second*, silent
# route into the same stall (the 20s push_hi latch) that never sets it, and
# leaves pull_pct as its only trace. Recomputing the threshold here catches
# both routes instead of trusting a bit that is known to miss one of them.
LOW_LATCH_PULL_PCT = 40

# The idle control deadband is 30-70 and relief stops at the 70 edge
# (Motion_control.cpp), so a park within that band is ordinary settling, not
# a stuck buffer. The condition is deliberately ">70 or <30", not "more than
# 15pp from the 50 centre": 15pp reaches 35..65 for the low side but 65 for
# the high side is still inside normal relief travel, so a symmetric margin
# around the centre misfires against the (asymmetric-in-practice) relief
# behaviour at the top of the band.
STUCK_HIGH_PULL_PCT = 70
STUCK_LOW_PULL_PCT = 30
STUCK_PRESSURE = 0xFFFF

CHIP_LATCH = 1
CHIP_STUCK = 2
CHIP_COUNT = 2
# Priority when both chips are lit at once (they are not mutually exclusive
# -- different channels on the same link can trip each one in the same
# STATUS frame). LATCH first: the motor is stopped *right now* and the
# extruder is dragging filament through the BMCU unassisted for as long as
# the print continues (10.7). STUCK second: a parked buffer skew is wrong
# but nothing is actively being dragged.
CHIP_NAMES = ("LATCH", "STUCK")
# Indexed by a 2-bit mask (bit0 LATCH, bit1 STUCK). Built once per distinct
# combination like LATCH_LABELS above, rather than concatenated per bit on
# every header/marquee rebuild.
CHIP_SUFFIXES = ("", "L", "S", "LS")
# How long a chip condition has to hold, continuously, before it lights.
# On a link the printer is actively polling, STATUS arrives at roughly
# 12.5 Hz (the interval in src/pressure_event_policy.h), so CHIP_HOLD_MS
# covers ~37 frames there and mainly exists to reject one noisy or
# borderline decode. A printer that is powered polls even while idle -- the
# idle heartbeat goes through set_motion like everything else, and main.cpp
# says as much: "Idle frames arrive continuously while a printer is paused"
# -- so that rate is the ordinary case for both chips, not just for LATCH.
# The case that is genuinely quiet is a BMCU with no polling printer at all:
# a bench rig, or a broken bus line. There the hold cannot confirm across
# frames, because pull_pct is not a dirty source (STATUS is emitted on
# set_motion, motion transitions, the masks, pressure *class* changes, LED
# and errors -- never on pull alone), so the buffer can move without
# producing a frame. A frame-count debounce would then mean STUCK could
# never light at all, which is why the hold is wall-clock; the age gate
# below is what keeps that from becoming a lie.
CHIP_HOLD_MS = 3000

# How old the *data*, not the link, is allowed to be before a chip is
# refused. is_stale() (below) answers "is this link alive at all" -- HELLO,
# EVENT and PONG all count -- but a BMCU that keeps answering PING while the
# printer has simply stopped polling it is not stale by that measure, and
# `status` is still a frozen snapshot. pull_pct is not itself a dirty source
# on the wire: the firmware only re-sends STATUS on set_motion, a motion
# transition, INSERTED/ONLINE, a pressure sentinel-class change, LED or
# ERROR, so a buffer that keeps moving physically produces no new frame if
# none of those fire while the printer is not asking. Design choice: this
# gate makes the chips fail dark, not fail loud or fail frozen. A quiet bus
# means no badge, ever, even if the last real reading would still qualify --
# never a stale reading kept alive, and never a guess in either direction.
# On a bus the printer is actively polling this changes nothing (STATUS
# arrives every ~80ms per CHIP_HOLD_MS's comment above, so 10s of silence
# never happens); it only matters on a bench BMCU with the printer powered
# down, where the chips now go dark instead of latching on the last thing
# they saw -- a known limitation, accepted on purpose over the alternative
# of an alert that can no longer be trusted to mean "still true". A periodic
# get_status() poll from the Pico side (not part of this change) would keep
# a live link's status age under ~2s unconditionally and retire this
# limitation; last_status_ms is also the plumbing that change would need.
CHIP_STATUS_MAX_AGE_MS = 10000


def _status_chip_bits(status):
    """Instantaneous (undebounced) chip conditions for one STATUS dict.

    Pure integer arithmetic over an existing dict/lists -- no allocation --
    so this is safe to call every service() tick. OledTicker is the one that
    turns a momentary bit into a lit chip.
    """
    if not status:
        return 0
    motion = status.get("motion") or ()
    pull = status.get("pull_pct") or ()
    online_mask = status.get("online_mask", 0)
    any_on_use_low = False
    all_idle = True
    any_far_from_centre = False
    for index in range(4):
        m = motion[index] if index < len(motion) else 0
        p = pull[index] if index < len(pull) else 0
        if m != 0:
            all_idle = False
        # A runout also sits in on_use with pull<40 while the tail clears the
        # buffer -- that is expected, not a stall, and the online key is what
        # tells the two apart: online_mask clears when the filament passes it
        # (decode_channel_flags' `tail` bit is the same signal, folded into
        # the flags byte instead of the mask). Gating on the mask keeps this
        # chip specific to 10.7 -- a motor latched off with filament still
        # present -- instead of firing on every ordinary runout.
        if m == MOTION_ON_USE and p < LOW_LATCH_PULL_PCT and \
                (online_mask >> index) & 1:
            any_on_use_low = True
        if p > STUCK_HIGH_PULL_PCT or p < STUCK_LOW_PULL_PCT:
            any_far_from_centre = True
    bits = 0
    if any_on_use_low:
        bits |= CHIP_LATCH
    if all_idle and status.get("pressure") == STUCK_PRESSURE and \
            any_far_from_centre:
        bits |= CHIP_STUCK
    return bits


def _chip_channel(status, bit):
    """First channel index that explains a chip bit already known to be set.

    Only called while building marquee/alert text, which is already
    throttled to the scroll-wrap schedule, so recomputing here rather than
    caching a channel index alongside the bit is fine.
    """
    motion = status.get("motion") or ()
    pull = status.get("pull_pct") or ()
    online_mask = status.get("online_mask", 0)
    for index in range(4):
        m = motion[index] if index < len(motion) else 0
        p = pull[index] if index < len(pull) else 0
        if bit == CHIP_LATCH and m == MOTION_ON_USE and \
                p < LOW_LATCH_PULL_PCT and (online_mask >> index) & 1:
            return index
        if bit == CHIP_STUCK and (p > STUCK_HIGH_PULL_PCT or
                                  p < STUCK_LOW_PULL_PCT):
            return index
    return 0

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
                chip_masks=None, columns=DEFAULT_COLUMNS):
    """The header, with each link's badge followed by its lit chip letters.

    chip_masks is a per-monitor list of 0..3 (see CHIP_LATCH/CHIP_STUCK).
    The header is the one line drawn on every page, so this is where the
    chips have to land to stay visible regardless of which page is up --
    same as the link and uplink state that already live here.
    """
    parts = []
    for index in range(len(monitors)):
        mask = chip_masks[index] if chip_masks and index < len(chip_masks) \
            else 0
        parts.append(link_badge(monitors[index]) + CHIP_SUFFIXES[mask])
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
        # One [since_latch, since_stuck] tick per monitor,
        # `None` when that bit is not currently true. Preallocated here, not
        # grown per call: _update_chips only ever mutates existing slots.
        self._chip_since_ms = [[None] * CHIP_COUNT for _ in monitors]
        self._chip_active_mask = [0] * len(monitors)

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
            # Every monitor's chips, not just the one on the current page:
            # the header shows all of them on every page, so a chip toggling
            # on a link that is not currently displayed still has to redraw.
            value = self._mix(value, self._chip_active_mask[index])
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

    def _update_chips(self, now_ms):
        """Debounce raw chip conditions against CHIP_HOLD_MS.

        Runs unconditionally every service() call, not gated behind the
        refresh/page schedule, because the hold time has to be measured in
        real time regardless of how often the body actually redraws. Mutates
        the preallocated per-monitor lists in place -- no new list, dict, or
        tuple is created on this path.
        """
        for index in range(len(self.monitors)):
            monitor = self.monitors[index]
            since = self._chip_since_ms[index]
            # Two independent gates, both required. is_stale() answers "is
            # this link alive at all" (any valid frame -- HELLO, EVENT,
            # PONG -- counts). link_state cannot substitute for it: a
            # snapshot-retry exhaustion path leaves link_state stuck at
            # "stale" while STATUS keeps arriving, and a real board has been
            # seen in exactly that state. But is_stale() alone is not enough
            # either: a BMCU that keeps answering PING while the printer has
            # simply stopped polling it is not stale by that measure, and
            # `status` is still a frozen snapshot -- that needs the
            # CHIP_STATUS_MAX_AGE_MS check on last_status_ms below. Called
            # directly, not through getattr(): the indirection would
            # materialise a fresh bound-method object on every call on this
            # port.
            gated = monitor.is_stale(now_ms)
            if not gated:
                last_status_ms = getattr(monitor, "last_status_ms", None)
                gated = last_status_ms is None or _ticks_diff(
                    now_ms, last_status_ms) > CHIP_STATUS_MAX_AGE_MS
            if gated:
                # A frozen snapshot is not a current condition. Reset so a
                # chip cannot stay latched on forever off a link that has
                # simply gone quiet, and so the hold restarts from zero
                # rather than partway through once fresh data arrives again.
                for bit_index in range(CHIP_COUNT):
                    since[bit_index] = None
                self._chip_active_mask[index] = 0
                continue
            status = getattr(monitor, "status", None)
            raw = _status_chip_bits(status)
            active = 0
            for bit_index in range(CHIP_COUNT):
                bit = 1 << bit_index
                if raw & bit:
                    if since[bit_index] is None:
                        since[bit_index] = now_ms
                    elif _ticks_diff(now_ms, since[bit_index]) >= CHIP_HOLD_MS:
                        # Clamp instead of leaving the original timestamp:
                        # ticks_diff wraps after ~6.2 days of continuous
                        # truth, which a genuinely abandoned stuck machine
                        # can reach, and an unclamped since[] would flicker
                        # the chip off as the wrap crosses zero.
                        since[bit_index] = _ticks_add(now_ms, -CHIP_HOLD_MS)
                        active |= bit
                else:
                    since[bit_index] = None
            self._chip_active_mask[index] = active

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

    def _chip_alerts(self):
        """Alert copy for every lit chip, LATCH first then STUCK.

        Only called from marquee_text(), which only runs at the scroll-wrap
        boundary, so building strings here does not touch the loop budget.
        """
        alerts = []
        for bit_index in range(CHIP_COUNT):
            bit = 1 << bit_index
            name = CHIP_NAMES[bit_index]
            for index in range(len(self.monitors)):
                if not self._chip_active_mask[index] & bit:
                    continue
                monitor = self.monitors[index]
                status = getattr(monitor, "status", None)
                channel = _chip_channel(status, bit) if status else 0
                alerts.append("%s ch%d %s" % (
                    link_letter(getattr(monitor, "link_id", "")),
                    channel + 1, name))
        return alerts

    def marquee_text(self):
        # A lit chip outranks the event queue: 10.5 and 10.7 both happened
        # with nobody watching the panel, and a routine state_change message
        # scrolling past would bury the one line that mattered.
        alerts = self._chip_alerts()
        if alerts:
            return "   ".join(alerts)
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
            getattr(self.client, "state", None),
            chip_masks=self._chip_active_mask, columns=self.columns), 0, 0)
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
        self._update_chips(now_ms)
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
