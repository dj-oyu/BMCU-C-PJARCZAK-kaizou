"""Rolling OLED status panel for the Pico bridge.

The panel shows what only matters while it is happening: which channel owns the
merger, what each motor is doing, which latch just tripped. Counters that are
worth reading after the fact stay in the web UI and the journal -- this is the
sign above the machine, not a second copy of the diagnostics.

Every render helper is a pure function over the monitor's live STATUS dict, so
the layout is testable on CPython; only OledTicker touches I2C.

Layout on a 128x64 panel (16 columns, 8 rows of the built-in 8x8 font):

    row 0      header: `A:ok B:off srv:ok` -- each link, the bambuddy server
    rows 1..6  the rotating page
    row 7      the marquee: alerts, then the latest events, else the IP

Anything a passer-by reads is a word, not a code. Codes stay on the detail
page, which is for debugging.

The pages rotate overview -> flow -> one detail page per link that has
reported a STATUS -> network/health, and back. A link that never answered
gets no detail page of its own (the overview already says it is down), so an
unplugged second BMCU costs no dead air.

Overview: every link side by side, one column each (64 px for two links, the
whole width for one). A column reads

    A:ok           link and its state
    1 READY #      per channel: slot, `>` on the channel holding the merger
    2>FEED  #      (the one wired to the printer), one state word, and the
    3 EMPTY #      buffer pull as a small level meter (a bar with the number
    4 JAM   #      when one link has the whole width)
    ERR 3          only when there is something to say

The word is the most urgent of: JAM, LOW (low-pull latch), LOAD! (autoload
failed), FAULT; then the motion: LOAD, FEED (printing), UNLD, STOP; then what
sits idle in the channel: TAIL, SET (owns the merger), READY, EMPTY. A latched
channel's text is drawn inverted.

Flow: a moving picture -- spools per link (spoke turning on the active one,
a latched one blinking), the active filament path marching toward the
printer or back, the buffer as a spring following its pull, and the toolhead
sweeping while the channel feeds. See the flow section below.

Detail (one per link) is the dense text view; a channel row reads `1*#oK2U 85LJ`:

    1   slot number
    *   owns the shared PTFE merger (`T` once the tail passed the online key)
    #   filament inserted at the entry switch (`.` when not)
    o   filament seen at the online key
    K2  the decoded ks: 0 none, 1 both, 2 external only, 3 internal only
    U   motion, indexed into MOTION_CHARS below
    85  pull percentage
    LJ  latched faults: L low pull, J jam, D DM autoload, F motion fault

Network/health puts the IP address on a line of its own: a dotted quad is up to
15 characters, and sharing its line with anything cut it to `192.168`.
"""

try:
    import utime as time
except ImportError:
    import time
import gc

import bmcu_stream_udp as stream

# _filament_motion in src/ams.h: idle, send_out, on_use, before_pull_back,
# pull_back, before_on_use, stop_on_use.
MOTION_CHARS = ".SU<Pb="
# Every state BMCUMonitor can assign, and every uplink state the client class
# defines, as the word the header and overview print. Anything a passer-by
# reads is a word: a row of one-letter codes only means something with the
# table in hand. A state missing here would fall through to "?", so
# ci/test_pico_oled_ticker.py asserts both tables stay complete against their
# sources rather than trusting this list to be maintained.
LINK_WORDS = {"online": "ok", "resyncing": "sync", "stale": "old",
              "incompatible": "bad", "offline": "off", "unknown": "--"}
UPLINK_WORDS = {stream.ONLINE: "ok", stream.DISABLED: "off",
                stream.WIFI_WAIT: "wifi", stream.RESOLVING: "dns",
                stream.SEND_ERROR: "err"}
# One word per channel, padded to five so rows line up without a per-row
# ljust(). Indexed by motion (_filament_motion in src/ams.h): idle, send_out,
# on_use, before_pull_back, pull_back, before_on_use, stop_on_use. Idle is
# None: what an idle channel is depends on what is sitting in it.
MOTION_WORDS = (None, "LOAD ", "FEED ", "UNLD ", "UNLD ", "LOAD ", "STOP ")
WORD_EMPTY = "EMPTY"
WORD_READY = "READY"
WORD_SET = "SET  "
WORD_TAIL = "TAIL "
WORD_UNKNOWN = "?    "
# Latches outrank motion. Indexed by low | jam << 1 | dm << 2, most urgent
# first: a jam is a motor pushing on a blockage now; the low latch has stopped
# the motor; a DM autoload failure is a load that gave up.
LATCH_WORDS = ("", "LOW  ", "JAM  ", "JAM  ", "LOAD!", "LOW  ", "JAM  ",
               "JAM  ")
WORD_FAULT = "FAULT"

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
# Pseudo page ids; a non-negative page is an index into the monitors.
PAGE_OVERVIEW = -1
PAGE_HEALTH = -2
PAGE_FLOW = -3
# Pixel geometry of one overview channel row: four characters of text, then the
# pull bar up to just short of the column's right edge (the last pixel column
# of the left half is the divider between links).
OVERVIEW_TEXT_PX = 56
OVERVIEW_DIGITS_MIN_WIDTH = 96

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


def link_word(monitor):
    """`A:ok`, `B:off` -- the link and its state, readable without a key."""
    return link_letter(getattr(monitor, "link_id", "")) + ":" + \
        LINK_WORDS.get(getattr(monitor, "link_state", None), "?")


def header_line(monitors, wifi_state=None, wifi_rssi=None, client_state=None,
                chip_masks=None, columns=DEFAULT_COLUMNS):
    """`A:ok B:off S:ok`: each link, then the server (bambuddy) uplink.

    chip_masks is a per-monitor list of 0..3 (see CHIP_LATCH/CHIP_STUCK). A
    lit chip adds `!` after its link: the header is the one line drawn on
    every page, so the flag has to live here, while the marquee spells out
    which chip and which channel. The RSSI moved to the network page; it is
    accepted here only so existing callers keep working.
    """
    parts = []
    for index in range(len(monitors)):
        mask = chip_masks[index] if chip_masks and index < len(chip_masks) \
            else 0
        parts.append(link_word(monitors[index]) + ("!" if mask else ""))
    if wifi_state is not None and wifi_state != "online":
        uplink = "wifi"
    else:
        uplink = UPLINK_WORDS.get(client_state, "?")
    line = " ".join(parts)
    # `srv` (the bambuddy server) when it fits, `S` when chips crowd it out:
    # the state word matters more than the label.
    if len(line) + 5 + len(uplink) <= columns:
        return line + " srv:" + uplink
    return _fit(line + " S:" + uplink, columns)


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
        # The link letter leads: the header lists every link, so without it
        # nothing on a detail page says which BMCU it is.
        lines.append(_fit("%s s%s P%d E%d" % (
            link_letter(getattr(monitor, "link_id", "")),
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


def wire_error_total(monitor):
    """crc + framing errors + sequence gaps: one number for "is the wire ok"."""
    decoder = getattr(monitor, "decoder", None)
    total = getattr(monitor, "sequence_gap_count", 0)
    if decoder:
        total += getattr(decoder, "crc_errors", 0) + \
            getattr(decoder, "frame_errors", 0)
    return total


def overview_title(monitor, columns):
    status = getattr(monitor, "status", None)
    badge = link_word(monitor)
    if not status:
        # The state word goes on the row below (overview_idle), where it has
        # the whole column instead of what is left after the badge.
        return badge
    # The channel feeding the printer is marked on its own row (`>`), so the
    # title is just the link and its state.
    return _fit(badge, columns)


def channel_word(index, status, flags):
    """The one word that matters most for a channel right now.

    Latch, then motion, then what is sitting in the channel. Always five
    characters (the tables are pre-padded), so callers never pad.
    """
    if flags:
        latch = LATCH_WORDS[(1 if flags["low_latch"] else 0) |
                            (2 if flags["jam_latch"] else 0) |
                            (4 if flags["dm_fail_latch"] else 0)]
        if latch:
            return latch, True
    faults = status.get("motion_fault") or ()
    if index < len(faults) and faults[index]:
        return WORD_FAULT, True
    motion_list = status.get("motion") or ()
    motion = motion_list[index] if index < len(motion_list) else 0
    if 0 < motion < len(MOTION_WORDS):
        return MOTION_WORDS[motion], False
    if not flags:
        return WORD_UNKNOWN, False
    if flags["tail"]:
        return WORD_TAIL, False
    if flags["loaded"]:
        return WORD_SET, False
    if (status.get("inserted_mask", 0) >> index) & 1:
        return WORD_READY, False
    return WORD_EMPTY, False


def overview_channel(index, status):
    """(text, latched, pull) for one overview channel row, e.g. `1>FEED `.

    `>` marks the channel that owns the shared merger, i.e. the one wired
    through to the printer. Returns None when there is no STATUS to draw
    from: the column then shows the link state instead of rows that would
    read as "empty and healthy".
    """
    if not status:
        return None
    flags_list = status.get("channel_flags") or ()
    flags = flags_list[index] if index < len(flags_list) else None
    word, latched = channel_word(index, status, flags)
    owner = ">" if flags and (flags["loaded"] or flags["tail"]) else " "
    pull_list = status.get("pull_pct") or ()
    pull = pull_list[index] if index < len(pull_list) else 0
    pull = 0 if pull < 0 else (100 if pull > 100 else pull)
    return SLOT_LABELS[index] + owner + word, latched, pull


def overview_idle(monitor, columns):
    """What a column says in place of its channels while it has no STATUS."""
    # The title already carries the state word (`B:off`).
    return _fit("no data", columns)


def overview_footer(monitor, columns):
    status = getattr(monitor, "status", None)
    if not status:
        return ""
    # Blank while healthy: a row of zeros is one more thing to decode.
    control = status.get("control_error", 0)
    if control:
        return _fit("ERR %d" % control, columns)
    wire = wire_error_total(monitor)
    if wire:
        return _fit("wire%d" % wire, columns)
    return ""


def _uptime_text(seconds):
    if seconds < 3600:
        return "%dm%02ds" % (seconds // 60, seconds % 60)
    if seconds < 86400:
        return "%dh%02dm" % (seconds // 3600, (seconds // 60) % 60)
    return "%dd%02dh" % (seconds // 86400, (seconds // 3600) % 24)


def health_lines(uptime_s=0, metrics=None, outbox=None, client=None,
                 journal=None, exception_count=0, wifi=None,
                 temperature_milli_c=None, wifi_rssi=None,
                 columns=DEFAULT_COLUMNS):
    """Network first, then the bridge's own health.

    The IP is alone on the first row: a dotted quad needs up to 15 of the 16
    columns, and every earlier layout that shared its row cut it short.
    """
    heap_free = gc.mem_free() if hasattr(gc, "mem_free") else 0
    loop = getattr(metrics, "loop_gap", None)
    wifi_state = getattr(wifi, "state", None)
    ip = getattr(wifi, "ip", None)
    if ip:
        address = ip
    else:
        address = "ip - " + (wifi_state or "no wifi")
    if wifi_state == "online" and wifi_rssi is not None:
        radio = "rssi%d" % wifi_rssi
    else:
        radio = "wifi " + (wifi_state or "-")
    lines = [
        address,
        radio,
        "udp %s rc%d" % (UPLINK_WORDS.get(getattr(client, "state", None),
                                          "-"),
                          getattr(client, "reconnect_count", 0)),
        "up %s%s" % (_uptime_text(uptime_s),
                     (" %dC" % (temperature_milli_c // 1000))
                     if temperature_milli_c is not None else ""),
        "heap%dk exc%d" % (heap_free // 1024, exception_count),
    ]
    drops = getattr(outbox, "forced_drop_count", 0)
    failures = getattr(journal, "failure_count", 0)
    if drops or failures:
        # Lost records outrank the loop timing once any have been lost; the
        # web UI still has both.
        lines.append("q%d drop%d jf%d" % (getattr(outbox, "queue_depth", 0),
                                          drops, failures))
    else:
        lines.append("loop%d/%dms q%d" % (
            (loop.percentile(99) // 1000) if loop else 0,
            (loop.maximum // 1000) if loop else 0,
            getattr(outbox, "queue_depth", 0)))
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


# -- flow page -----------------------------------------------------------
#
# A moving picture of the machine: both BMCUs' spools on the left, the path
# of whichever channel is active into the shared merger, the buffer as a
# spring whose length follows that channel's pull, and the printer's
# toolhead on the right. Everything is drawn from STATUS alone, so the
# printer is only as animated as the BMCU knows: the head sweeps while the
# active channel is feeding (on_use), and parks otherwise.
#
# Geometry for the body area of a 128x64 panel (y 8..55). Integer tables
# only, so a frame allocates nothing.
FLOW_SPOOL_X = (15, 29, 43, 57)
FLOW_SPOOL_Y = (18, 42)
FLOW_SPOOL_R = 5
FLOW_BUS_Y = (27, 33)
FLOW_MERGE_X = 66
FLOW_TUBE_Y = 30
FLOW_SPRING_X = 70
FLOW_RAIL_Y = 10
FLOW_RAIL_X = 96
FLOW_HEAD_TRAVEL = 22
FLOW_BED_Y = 50
# Spoke end points for eight rotation steps, radius 4.
FLOW_SPOKE = ((4, 0), (3, 3), (0, 4), (-3, 3), (-4, 0), (-3, -3), (0, -4),
              (3, -3))
# +1 toward the printer, -1 back toward the spool, 0 holding still.
FLOW_DIRECTION = (0, 1, 1, -1, -1, 1, 0)


def flow_active(monitors):
    """(link, channel, motion) of the channel to animate, or None.

    A moving channel wins; failing that, the one holding the merger. Only
    the first two links are drawn, matching the overview.
    """
    shown = len(monitors) if len(monitors) < 2 else 2
    held = None
    for link in range(shown):
        status = getattr(monitors[link], "status", None)
        if not status:
            continue
        motion = status.get("motion") or ()
        flags = status.get("channel_flags") or ()
        for channel in range(4):
            m = motion[channel] if channel < len(motion) else 0
            if 0 < m < len(FLOW_DIRECTION):
                return link, channel, m
            f = flags[channel] if channel < len(flags) else None
            if held is None and f and (f["loaded"] or f["tail"]):
                held = (link, channel, 0)
    return held


def _flow_segment(frame, x0, y0, x1, y1, start, phase, moving):
    """A straight, axis-aligned or 45-degree run; dashed and marching when
    `moving`. Returns the path position after it, so dashes stay continuous
    around corners."""
    dx = (x1 > x0) - (x1 < x0)
    dy = (y1 > y0) - (y1 < y0)
    steps = abs(x1 - x0) if abs(x1 - x0) > abs(y1 - y0) else abs(y1 - y0)
    x, y = x0, y0
    for step in range(steps + 1):
        if not moving or ((start + step - phase) & 3) < 2:
            frame.pixel(x, y, 1)
        x += dx
        y += dy
    return start + steps


def _flow_spring(frame, x0, length, y):
    """Zigzag from x0 over `length` pixels: the buffer, longer when slack."""
    x = x0
    up = True
    while x + 2 <= x0 + length:
        frame.line(x, y, x + 2, y - 2 if up else y + 2, 1)
        frame.line(x + 2, y - 2 if up else y + 2, x + 4, y, 1)
        x += 4
        up = not up
    return x


def draw_flow(frame, monitors, tick):
    """Draw the flow page for animation step `tick` into `frame`."""
    active = flow_active(monitors)
    shown = len(monitors) if len(monitors) < 2 else 2
    phase = 0
    direction = 0
    if active:
        direction = FLOW_DIRECTION[active[2]]
        phase = tick * direction
    ellipse = getattr(frame, "ellipse", None)
    for link in range(shown):
        monitor = monitors[link]
        sy = FLOW_SPOOL_Y[link]
        frame.text(LINK_LETTERS[link] if link < len(LINK_LETTERS) else "?",
                   0, sy - 4, 1)
        status = getattr(monitor, "status", None)
        if not status:
            frame.text(LINK_WORDS.get(getattr(monitor, "link_state", None),
                                      "?"), 16, sy - 4, 1)
            continue
        inserted = status.get("inserted_mask", 0)
        flags = status.get("channel_flags") or ()
        for channel in range(4):
            sx = FLOW_SPOOL_X[channel]
            f = flags[channel] if channel < len(flags) else None
            latched = f and (f["low_latch"] or f["jam_latch"] or
                             f["dm_fail_latch"])
            if ellipse is None:
                frame.rect(sx - FLOW_SPOOL_R, sy - FLOW_SPOOL_R,
                           2 * FLOW_SPOOL_R + 1, 2 * FLOW_SPOOL_R + 1, 1)
            elif latched and tick & 1:
                # A latched spool blinks solid.
                ellipse(sx, sy, FLOW_SPOOL_R, FLOW_SPOOL_R, 1, True)
                continue
            else:
                ellipse(sx, sy, FLOW_SPOOL_R, FLOW_SPOOL_R, 1)
            if (inserted >> channel) & 1:
                if ellipse is not None:
                    ellipse(sx, sy, 3, 3, 1)
                turning = active and active[0] == link and \
                    active[1] == channel and direction
                spoke = FLOW_SPOKE[(tick * direction) & 7] if turning \
                    else FLOW_SPOKE[0]
                frame.line(sx, sy, sx + spoke[0], sy + spoke[1], 1)
    # Path of the active channel: spool -> bus -> merger -> spring -> head.
    head_x = FLOW_RAIL_X
    feeding = active is not None and active[2] == MOTION_ON_USE
    if feeding:
        sweep = tick % (2 * FLOW_HEAD_TRAVEL)
        head_x += sweep if sweep <= FLOW_HEAD_TRAVEL else \
            2 * FLOW_HEAD_TRAVEL - sweep
    if active:
        link, channel, motion = active
        moving = direction != 0
        sx = FLOW_SPOOL_X[channel]
        bus = FLOW_BUS_Y[link]
        edge = FLOW_SPOOL_Y[link] + (FLOW_SPOOL_R + 1 if link == 0 else
                                     -FLOW_SPOOL_R - 1)
        pos = _flow_segment(frame, sx, edge, sx, bus, 0, phase, moving)
        corner = FLOW_MERGE_X - abs(FLOW_TUBE_Y - bus)
        pos = _flow_segment(frame, sx, bus, corner, bus, pos, phase, moving)
        pos = _flow_segment(frame, corner, bus, FLOW_MERGE_X, FLOW_TUBE_Y,
                            pos, phase, moving)
        status = getattr(monitors[link], "status", None) or {}
        pull_list = status.get("pull_pct") or ()
        pull = pull_list[channel] if channel < len(pull_list) else 50
        pull = 0 if pull < 0 else (100 if pull > 100 else pull)
        _flow_segment(frame, FLOW_MERGE_X, FLOW_TUBE_Y, FLOW_SPRING_X,
                      FLOW_TUBE_Y, pos, phase, moving)
        end = _flow_spring(frame, FLOW_SPRING_X, 8 + pull * 16 // 100,
                           FLOW_TUBE_Y)
        # The bowden tube follows the head.
        frame.line(end, FLOW_TUBE_Y, head_x + 4, FLOW_RAIL_Y + 8, 1)
    # The printer: rail, head with nozzle, bed.
    frame.hline(FLOW_RAIL_X, FLOW_RAIL_Y, 128 - FLOW_RAIL_X, 1)
    frame.rect(head_x, FLOW_RAIL_Y + 1, 9, 7, 1)
    frame.hline(head_x + 3, FLOW_RAIL_Y + 8, 3, 1)
    frame.pixel(head_x + 4, FLOW_RAIL_Y + 9, 1)
    if feeding:
        # Plastic leaving the nozzle on alternate frames.
        frame.vline(head_x + 4, FLOW_RAIL_Y + 11, 1 + (tick & 1), 1)
    frame.hline(FLOW_RAIL_X, FLOW_BED_Y, 128 - FLOW_RAIL_X, 1)


LINK_LETTERS = ("A", "B")
OVERLAY_CHECK_MS = 200


class OledTicker:
    """Drives the panel from the main loop without ever blocking on a frame."""

    def __init__(self, display, monitors, wifi=None, client=None, metrics=None,
                 outbox=None, journal=None, platform_stats=None,
                 page_ms=5000, overview_ms=10000, flow_ms=10000,
                 flow_frame_ms=200, refresh_ms=1000, marquee_ms=120,
                 marquee_step=4, error_backoff_ms=5000,
                 exception_count=None, overlay_provider=None):
        self.display = display
        # (now_ms) -> tuple of rows or None. While it returns rows the panel
        # shows them and nothing else: local_reset.py puts its confirmation
        # code here, and the code must not scroll away mid-read.
        self.overlay_provider = overlay_provider
        self._overlay = None
        self._overlay_check_ms = None
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
        # The overview is the page the panel exists for; the others are
        # drill-downs, so it is held longer.
        self.overview_ms = overview_ms
        # 0 drops the flow page from the rotation.
        self.flow_ms = flow_ms
        # One animation step per flow_frame_ms; each step redraws the body,
        # so this is also what the page costs in I2C (six 128-byte pages).
        self.flow_frame_ms = flow_frame_ms
        self._flow_tick = 0
        self._flow_ms_mark = None
        self.refresh_ms = refresh_ms
        self.marquee_ms = marquee_ms
        self.marquee_step = marquee_step
        self.error_backoff_ms = error_backoff_ms
        self.columns = display.width // 8
        self.rows = display.pages
        self.body_rows = max(1, self.rows - 2)
        # page_index counts rotations (tests and logs read it); page is what
        # is on screen: PAGE_OVERVIEW, PAGE_HEALTH, or a monitor index.
        self.page_index = 0
        self.page = PAGE_OVERVIEW
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
        """Every page the rotation can reach: overview, links, health."""
        return len(self.monitors) + 2

    def next_page(self, page):
        """Overview, then each link that has a STATUS, then health, then back.

        A link with no STATUS is skipped rather than shown as six rows of
        "no status": the overview column already says the link is down.
        """
        if page == PAGE_HEALTH:
            return PAGE_OVERVIEW
        if page == PAGE_OVERVIEW and self.flow_ms and self.body_rows >= 6:
            # The picture needs the whole body; a short panel skips it.
            return PAGE_FLOW
        candidate = 0 if page in (PAGE_OVERVIEW, PAGE_FLOW) else page + 1
        while candidate < len(self.monitors):
            if getattr(self.monitors[candidate], "status", None):
                return candidate
            candidate += 1
        return PAGE_HEALTH

    def page_dwell_ms(self):
        if self.page == PAGE_OVERVIEW:
            return self.overview_ms
        if self.page == PAGE_FLOW:
            return self.flow_ms
        return self.page_ms

    def body_lines(self):
        """The text rows of the current page; the overview is drawn, not
        written, and has none."""
        page = self.page
        if page in (PAGE_OVERVIEW, PAGE_FLOW):
            return []
        if page == PAGE_HEALTH or page >= len(self.monitors):
            rssi, temperature = self._platform()
            return health_lines(
                self.uptime_s, self.metrics, self.outbox, self.client,
                self.journal, self.exception_count(), self.wifi, temperature,
                rssi, self.columns)
        return link_lines(self.monitors[page], self.columns)

    def content_digest(self):
        """An allocation-free fingerprint of everything the body draws.

        Rebuilding the body costs some sixty short-lived strings. MicroPython
        has no reference counting, so all of them sit in the heap until the
        next collection, which main.py schedules a minute apart at best. Most
        seconds nothing on the panel actually moved, and this is what keeps an
        idle machine from spending ~2 KB/s to redraw the same six rows.
        """
        page = self.page
        value = self._mix(page + 3, page + 3)
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
        if page == PAGE_HEALTH or page >= len(self.monitors):
            # The health page carries the uptime, so it redraws on its own
            # schedule -- one second is the resolution it prints.
            value = self._mix(value, hash(getattr(self.wifi, "ip", None)
                                          or ""))
            return self._mix(value, self.uptime_s)
        if page in (PAGE_OVERVIEW, PAGE_FLOW):
            for index in range(len(self.monitors)):
                value = self._mix_link(value, self.monitors[index])
            return value
        return self._mix_link(value, self.monitors[page])

    def _mix_link(self, value, monitor):
        status = getattr(monitor, "status", None)
        decoder = getattr(monitor, "decoder", None)
        value = self._mix(value, getattr(decoder, "crc_errors", 0) if decoder
                          else 0)
        value = self._mix(value, getattr(decoder, "frame_errors", 0) if decoder
                          else 0)
        value = self._mix(value, getattr(monitor, "sequence_gap_count", 0))
        if not status:
            return self._mix(value, 0x5A5A)
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
            # The header already carries the link badges; on its own the IP
            # fits the row and is drawn still (see _step_marquee).
            return getattr(self.wifi, "ip", None) or "no events yet"
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
        if self.page == PAGE_OVERVIEW:
            self._draw_overview(frame)
        elif self.page == PAGE_FLOW:
            draw_flow(frame, self.monitors, self._flow_tick)
        else:
            lines = self.body_lines()
            for row in range(min(self.body_rows, len(lines))):
                frame.text(lines[row], 0, (row + 1) * 8)
        self._dirty |= (1 << (self.rows - 1)) - 1

    def _draw_overview(self, frame):
        """Every link in its own column; see the module docstring."""
        shown = len(self.monitors) if len(self.monitors) < 2 else 2
        if not shown:
            return
        width = self.display.width // shown
        columns = width // 8
        body = self.body_rows
        for column in range(shown):
            monitor = self.monitors[column]
            left = column * width
            frame.text(overview_title(monitor, columns), left, 8)
            status = getattr(monitor, "status", None)
            # Rows left for channels once the title is placed; the footer
            # only gets drawn on a panel tall enough for all four channels.
            channel_rows = body - 1 if body - 1 < 4 else 4
            if not status:
                if channel_rows:
                    frame.text(overview_idle(monitor, columns), left, 16)
                channel_rows = 0
            for index in range(channel_rows):
                row = overview_channel(index, status)
                self._draw_channel(frame, left, (index + 2) * 8, width, row)
            if body >= 6:
                footer = overview_footer(monitor, columns)
                if footer:
                    frame.text(footer, left, 6 * 8)
            if column:
                # The divider sits on the last pixel column of the left half,
                # which the bars leave free.
                frame.vline(left - 1, 8, body * 8, 1)

    @staticmethod
    def _draw_channel(frame, left, top, width, row):
        text, latched, pull = row
        if latched:
            # Only the text cell is inverted: an inverted bar reads backwards,
            # its empty part looking like the full one.
            frame.fill_rect(left, top, OVERVIEW_TEXT_PX - 1, 8, 1)
            frame.text(text, left, top, 0)
        else:
            frame.text(text, left, top, 1)
        bar_left = left + OVERVIEW_TEXT_PX
        bar_right = left + width - 3
        if bar_right - bar_left < 16:
            # Two-column layout: no room across, so the buffer is a small
            # level meter filling upwards. 7 px tall, so a 1 px gap keeps
            # stacked rows from merging into one column.
            frame.rect(bar_left + 1, top, 5, 7, 1)
            fill = 5 * pull // 100
            if fill:
                frame.fill_rect(bar_left + 2, top + 6 - fill, 3, fill, 1)
            return
        if width >= OVERVIEW_DIGITS_MIN_WIDTH:
            # A full-width column has room to print the number too.
            bar_right -= 24
            frame.text("%3d" % pull, bar_right + 2, top, 1)
        bar_width = bar_right - bar_left + 1
        frame.rect(bar_left, top + 1, bar_width, 6, 1)
        fill = (bar_width - 2) * pull // 100
        if fill:
            frame.fill_rect(bar_left + 1, top + 2, fill, 4, 1)
        # Centre tick above and below the bar. Stacked rows line their ticks
        # up into a dotted 50 % rule down the column.
        centre = bar_left + bar_width // 2
        frame.pixel(centre, top, 1)
        frame.pixel(centre, top + 7, 1)

    def _draw_marquee(self):
        frame = self.display.frame
        top = (self.rows - 1) * 8
        frame.fill_rect(0, top, self.display.width, 8, 0)
        frame.text(self._marquee,
                   0 if self._marquee_fits() else self._marquee_x, top)
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

    def panel_ready(self):
        """True while the panel answers: no write has failed since the last
        backoff window. local_reset.py refuses to create a code otherwise."""
        return self._retry_ms is None

    def _draw_overlay(self, lines):
        frame = self.display.frame
        frame.fill_rect(0, 0, self.display.width, self.rows * 8, 0)
        for row in range(min(self.rows, len(lines))):
            frame.text(lines[row][:self.columns], 0, row * 8)
        self._dirty = (1 << self.rows) - 1

    def _service_overlay(self, now_ms):
        """True while an overlay owns the panel."""
        if self.overlay_provider is None:
            return False
        # Asked five times a second, not once per loop: while a code is up the
        # provider builds its rows as fresh strings, and at loop rate that
        # would be thousands of short-lived objects a second.
        if self._overlay_check_ms is not None and _ticks_diff(
                now_ms, self._overlay_check_ms) < OVERLAY_CHECK_MS:
            if self._overlay is not None:
                self._flush_one_page()
            return self._overlay is not None
        self._overlay_check_ms = now_ms
        lines = self.overlay_provider(now_ms)
        if lines is None:
            if self._overlay is not None:
                # Back to the rotation: force a full redraw of the page and
                # the marquee row the overlay painted over.
                self._overlay = None
                self._digest = None
                self._content_ms = None
                self._draw_marquee()
            return False
        if lines != self._overlay:
            self._overlay = lines
            self._draw_overlay(lines)
        self._flush_one_page()
        return True

    def _service(self, now_ms):
        if self._service_overlay(now_ms):
            return
        self._update_chips(now_ms)
        if self._page_ms_mark is None:
            self._page_ms_mark = now_ms
        elif _ticks_diff(now_ms, self._page_ms_mark) >= self.page_dwell_ms():
            self.page_index += 1
            self.page = self.next_page(self.page)
            self._page_ms_mark = now_ms
            self._content_ms = None
        if self.page == PAGE_FLOW:
            # Animated: redrawn on its own clock, not when the digest moves.
            if self._flow_ms_mark is None or _ticks_diff(
                    now_ms, self._flow_ms_mark) >= self.flow_frame_ms:
                self._flow_ms_mark = now_ms
                self._flow_tick = (self._flow_tick + 1) & 0xFFFF
                self._draw_body()
                self._content_ms = now_ms
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
        """Scroll copy wider than the panel; hold copy that fits still.

        Text that fits is drawn at x=0 and left alone: scrolling an IP
        address means any one glance (or photo) catches it cut in half.
        _marquee_x keeps ticking as the clock that decides when to re-read
        the copy, so a new event still replaces it on the same schedule.
        """
        if self._marquee_x <= -(len(self._marquee) * 8):
            # Re-read the copy only at the wrap: swapping it mid-scroll would
            # make the line jump while somebody is still reading it.
            previous = self._marquee
            self._marquee = self.marquee_text()
            self._marquee_x = self.display.width
            if self._marquee_fits() and self._marquee == previous:
                return  # still the same still line; nothing to redraw
        else:
            if not self._marquee:
                self._marquee = self.marquee_text()
                self._marquee_x -= self.marquee_step
            else:
                self._marquee_x -= self.marquee_step
                if self._marquee_fits():
                    return
        self._draw_marquee()

    def _marquee_fits(self):
        return len(self._marquee) * 8 <= self.display.width

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
TICKER_KEYS = (("OLED_PAGE_MS", "page_ms"),
               ("OLED_OVERVIEW_MS", "overview_ms"),
               ("OLED_FLOW_MS", "flow_ms"),
               ("OLED_FLOW_FRAME_MS", "flow_frame_ms"),
               ("OLED_REFRESH_MS", "refresh_ms"),
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
