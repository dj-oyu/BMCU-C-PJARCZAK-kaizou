import importlib.util
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("pico_monitor_link", ROOT / "pico" / "bmcu_link.py")
link = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(link)


class FakeUART:
    def __init__(self):
        self.rx = bytearray()
        self.writes = []

    def any(self):
        return len(self.rx)

    def read(self, count):
        data = bytes(self.rx[:count])
        del self.rx[:count]
        return data

    def write(self, data):
        self.writes.append(bytes(data))


def frame(kind, sequence, payload):
    return {"version": link.VERSION_ALPHA3, "kind": kind,
            "sequence": sequence, "payload": payload}


def snapshot_payload(snapshot_id, index, count, record_type=1, tick=10):
    return (snapshot_id.to_bytes(2, "little") + bytes((index, count, record_type, 0)) +
            tick.to_bytes(4, "little") + bytes(16))


class PicoMonitorTests(unittest.TestCase):
    def setUp(self):
        self.uart = FakeUART()
        self.messages = []
        self.monitor = link.BMCUMonitor(self.uart, self.messages.append, link_id="bmcu-b")

    def hello(self, sequence=10):
        payload = bytes((0x83, 0x7f, 0, 1, 2)) + (144000000).to_bytes(4, "little")
        self.monitor._handle_frame(frame(link.HELLO, sequence, payload), 100)

    def test_hello_carries_build_identity(self):
        # 0x3211: dm_two_microswitch, ams_num 1, retract 0.200 m.
        payload = (bytes((0x83, 0x7f, 0, 1, 2)) +
                   (144000000).to_bytes(4, "little") +
                   (0x3211).to_bytes(2, "little") +
                   (0xDEADBEEF).to_bytes(4, "little"))
        self.monitor._handle_frame(frame(link.HELLO, 10, payload), 100)

        self.assertEqual(self.monitor.variant_flags, 0x3211)
        self.assertEqual(self.monitor.build_hash, 0xDEADBEEF)
        hello = [m for m in self.messages if m.get("type") == "hello"][-1]
        self.assertEqual(hello["variant_flags"], 0x3211)
        self.assertEqual(hello["build_hash"], 0xDEADBEEF)

    def test_a_short_hello_still_brings_the_link_up(self):
        # A BMCU built before build identity existed sends 9 bytes. Rejecting it
        # would drop the one message that drives resync, so the link would never
        # come up at all -- worse than not knowing which build is running.
        self.hello()

        self.assertEqual(self.monitor.link_state, "resyncing")
        self.assertIsNone(self.monitor.variant_flags)
        self.assertIsNone(self.monitor.build_hash)

    def test_decoder_retains_a_fixed_noise_bound(self):
        decoder = link.FrameDecoder()
        decoder.feed(b"x" * 10000)
        self.assertLessEqual(len(decoder._buffer), link.MAX_DECODER_BUFFER)

    def test_a_chunk_larger_than_the_noise_bound_is_fully_parsed(self):
        # The decoder used to keep only the last MAX_DECODER_BUFFER bytes of a
        # larger chunk, discarding intact frames read in the same drain.
        decoder = link.FrameDecoder()
        wire = b"".join(
            link.encode_frame(link.STATUS, index, bytes(range(31)))
            for index in range(1, 21))
        self.assertGreater(len(wire), link.MAX_DECODER_BUFFER * 4)

        frames = decoder.feed(wire)

        self.assertEqual([f["sequence"] for f in frames], list(range(1, 21)))
        self.assertEqual(decoder.crc_errors, 0)
        self.assertEqual(decoder.frame_errors, 0)
        self.assertEqual(decoder.discarded_bytes, 0)

    def test_frames_split_across_chunk_boundaries_still_decode(self):
        decoder = link.FrameDecoder()
        wire = b"".join(
            link.encode_frame(link.STATUS, index, bytes(range(31)))
            for index in range(1, 13))
        frames = []
        for offset in range(0, len(wire), 7):
            frames.extend(decoder.feed(wire[offset:offset + 7]))

        self.assertEqual([f["sequence"] for f in frames], list(range(1, 13)))
        self.assertEqual(decoder.crc_errors, 0)

    def test_pure_noise_is_counted_as_discarded_not_as_frame_errors(self):
        decoder = link.FrameDecoder()
        decoder.feed(b"x" * 10000)
        self.assertEqual(decoder.frame_errors, 0)
        self.assertEqual(decoder.crc_errors, 0)

    def test_frames_buried_in_noise_are_recovered(self):
        decoder = link.FrameDecoder()
        good = link.encode_frame(link.STATUS, 5, bytes(range(31)))
        frames = decoder.feed(b"\x00" * 400 + good + b"\x11" * 400)
        self.assertEqual([f["sequence"] for f in frames], [5])

    def test_crc_rejection_captures_the_whole_candidate_frame(self):
        # Counters alone cannot tell a flipped bit from a truncated frame, so
        # the rejected bytes have to survive for offline analysis.
        capture = link.RejectCapture()
        decoder = link.FrameDecoder(capture)
        decoder.clock_ms = 4242
        wire = bytearray(link.encode_frame(link.STATUS, 7, bytes(range(31))))
        wire[9] ^= 0x01  # one flipped bit inside the payload

        decoder.feed(bytes(wire))

        self.assertEqual(decoder.crc_errors, 1)
        records = list(capture.records())
        self.assertEqual(len(records), 1)
        ordinal, reason, timestamp, data = records[0]
        self.assertEqual((ordinal, reason, timestamp), (1, link.REJECT_CRC, 4242))
        self.assertEqual(bytes(data), bytes(wire))

    def test_capture_keeps_the_most_recent_runs_and_drops_older_ones(self):
        capture = link.RejectCapture(slots=3)
        for index in range(5):
            capture.add(link.REJECT_CRC, bytes((index,)) * 4, index)

        ordinals = [record[0] for record in capture.records()]
        self.assertEqual(capture.captured, 5)
        self.assertEqual(ordinals, [3, 4, 5], "oldest first, newest retained")

    def test_capture_truncates_a_run_longer_than_a_slot(self):
        capture = link.RejectCapture(slots=1, size=8)
        capture.add(link.REJECT_LENGTH, bytes(range(40)), 1)
        _ordinal, _reason, _timestamp, data = next(iter(capture.records()))
        self.assertEqual(bytes(data), bytes(range(8)))

    def test_bad_length_byte_is_captured_with_its_context(self):
        capture = link.RejectCapture()
        decoder = link.FrameDecoder(capture)
        decoder.feed(link.SYNC + b"\x83\x02\x01\x00\xff" + b"junkjunk")

        self.assertEqual(decoder.frame_errors, 1)
        _ordinal, reason, _timestamp, data = next(iter(capture.records()))
        self.assertEqual(reason, link.REJECT_LENGTH)
        self.assertTrue(bytes(data).startswith(link.SYNC))

    def test_a_clean_stream_captures_nothing(self):
        capture = link.RejectCapture()
        decoder = link.FrameDecoder(capture)
        decoder.feed(link.encode_frame(link.STATUS, 1, bytes(range(31))))

        self.assertEqual(capture.captured, 0)
        self.assertEqual(list(capture.records()), [])

    def test_monitor_capture_is_wired_to_its_decoder(self):
        self.assertIsNotNone(self.monitor.capture)
        self.assertIs(self.monitor.decoder.capture, self.monitor.capture)

    def test_hello_starts_one_atomic_baseline(self):
        self.hello()
        kinds = [link.FrameDecoder().feed(wire)[0]["kind"] for wire in self.uart.writes]
        self.assertEqual(kinds, [link.GET_STATUS, link.GET_FULL_STATUS])
        self.assertEqual(self.monitor.link_state, "resyncing")
        self.assertEqual(self.messages[0]["link_id"], "bmcu-b")

    def test_status_without_hello_requests_one_full_baseline(self):
        payload = (25).to_bytes(4, "little") + bytes(link.STATUS_PAYLOAD_SIZE - 4)
        self.monitor._handle_frame(frame(link.STATUS, 15, payload), 200)
        kinds = [link.FrameDecoder().feed(wire)[0]["kind"] for wire in self.uart.writes]
        self.assertEqual(kinds, [link.GET_FULL_STATUS])
        self.monitor._handle_frame(frame(link.STATUS, 16, payload), 210)
        self.assertEqual(len(self.uart.writes), 1)

    def test_channel_flags_separate_the_three_latches_and_the_switch_reading(self):
        # The point of the byte: one red LED used to stand for all three
        # latches, and online collapsed a four-valued switch into one bit.
        self.assertEqual(
            link.decode_channel_flags(0x00),
            {"ks": 0, "low_latch": False, "jam_latch": False,
             "dm_fail_latch": False, "loaded": False, "tail": False,
             "raw": 0x00})
        # loaded and tail are the merger mutex, and tail is the half of it that
        # only shows up on a runout: the filament is past the switch (ks 0) but
        # the channel still owns the tube, so the printer's retract must still
        # be accepted. Reading tail as "not loaded" is the defect it exists to
        # make visible, so the two bits are asserted apart here.
        held = link.decode_channel_flags(0x20)
        self.assertEqual((held["loaded"], held["tail"]), (True, False))
        tail = link.decode_channel_flags(0x60)
        self.assertEqual((tail["ks"], tail["loaded"], tail["tail"]),
                         (0, True, True))
        # ks == 2 is the state online cannot distinguish: resting on the outer
        # switch alone, which autoloads differently from settled on both.
        self.assertEqual(link.decode_channel_flags(0x02)["ks"], 2)
        # low without jam: the stop latch a soft reset clears.
        low = link.decode_channel_flags(0x04)
        self.assertTrue(low["low_latch"])
        self.assertFalse(low["jam_latch"])
        # jam rides with low today, and each bit still reads independently.
        jam = link.decode_channel_flags(0x0d)
        self.assertEqual((jam["ks"], jam["low_latch"], jam["jam_latch"]), (1, True, True))
        # dm_fail: the one that needs the filament withdrawn.
        self.assertTrue(link.decode_channel_flags(0x11)["dm_fail_latch"])
        # Reserved bits are ignored, not folded into a neighbour.
        self.assertEqual(link.decode_channel_flags(0xe0)["raw"], 0xe0)
        self.assertEqual(link.decode_channel_flags(0xe0)["ks"], 0)

    def test_status_carries_one_channel_flags_byte_per_channel(self):
        payload = bytearray(link.STATUS_PAYLOAD_SIZE)
        payload[27:31] = bytes((0x01, 0x06, 0x12, 0x00))
        decoded = link.BMCUMonitor._decode_status(bytes(payload))
        self.assertEqual([entry["ks"] for entry in decoded["channel_flags"]],
                         [1, 2, 2, 0])
        self.assertEqual([entry["low_latch"] for entry in decoded["channel_flags"]],
                         [False, True, False, False])
        self.assertEqual([entry["dm_fail_latch"] for entry in decoded["channel_flags"]],
                         [False, False, True, False])

    def test_a_status_of_the_previous_length_still_decodes(self):
        # The lock-step reading of the transport contract turned out to cost
        # more than it was worth in practice: requiring 31 exactly means two
        # BMCUs and a Pico have to be flashed together, and a BMCU flash can
        # leave a board that looks bricked. So 27 is accepted.
        self.monitor._handle_frame(frame(link.STATUS, 15, bytes(27)), 200)
        self.assertIsNotNone(self.monitor.status)
        self.assertEqual(self.messages[-1]["type"], "status")

    def test_a_status_without_the_flags_byte_reports_absent_not_clear(self):
        # Absent and all-clear are different claims. Reporting every latch as
        # false against an older BMCU would invent a healthy machine, which is
        # the mistake the channel-flags byte exists to prevent.
        decoded = link.BMCUMonitor._decode_status(bytes(27))
        self.assertIsNone(decoded["channel_flags"])
        self.assertEqual(decoded["motion"], [0, 0, 0, 0])

    def test_a_status_of_an_unknown_length_is_still_rejected(self):
        self.monitor._handle_frame(frame(link.STATUS, 16, bytes(29)), 210)
        self.assertEqual(self.messages[-1]["type"], "unknown_or_invalid")

    def test_channel_record_repeats_the_flags_byte_in_its_high_bits(self):
        record_data = bytearray(16)
        record_data[0] = 2
        # Bits 0-4 are the record's own flags; bits 8-12 are the shared byte.
        record_data[6:8] = ((0x0c | (0x16 << 8)).to_bytes(2, "little"))
        payload = ((1).to_bytes(2, "little") + bytes((0, 1, 2, 0)) +
                   (10).to_bytes(4, "little") + bytes(record_data))
        message = {}
        self.monitor._handle_snapshot(payload, message, 100)
        flags = message["channel_data"]["channel_flags"]
        self.assertEqual(flags["ks"], 2)
        self.assertTrue(flags["low_latch"])
        self.assertTrue(flags["dm_fail_latch"])
        self.assertFalse(flags["jam_latch"])
        # The record's own bits are untouched by the fold.
        self.assertTrue(message["channel_data"]["sensor_online"])
        self.assertTrue(message["channel_data"]["sensor_good"])

    def _channel_record(self, own_flags):
        record_data = bytearray(16)
        record_data[0] = 2
        record_data[6:8] = own_flags.to_bytes(2, "little")
        payload = ((1).to_bytes(2, "little") + bytes((0, 1, 2, 0)) +
                   (10).to_bytes(4, "little") + bytes(record_data))
        message = {}
        self.monitor._handle_snapshot(payload, message, 100)
        return message["channel_data"]

    def test_a_channel_with_no_learned_polarity_is_visible(self):
        # The whole point: such a channel is completely dead -- autoload, the
        # idle pull PID, the buffer gesture and manual unload all stop at the
        # same early return -- while reporting nothing else at all. A bench
        # session was spent rediscovering that by hand.
        self.assertFalse(self._channel_record(0x0c)["polarity_valid"])
        self.assertTrue(self._channel_record(0x2c)["polarity_valid"])

    def test_polarity_bit_does_not_disturb_the_folded_flags_byte(self):
        # Bit 5 sits between the record's own bits and the STATUS byte folded
        # in at bit 8; setting it must not bleed into either neighbour.
        channel = self._channel_record(0x2c | (0x16 << 8))
        self.assertTrue(channel["polarity_valid"])
        self.assertTrue(channel["sensor_online"])
        self.assertTrue(channel["sensor_good"])
        self.assertEqual(channel["channel_flags"]["ks"], 2)
        self.assertTrue(channel["channel_flags"]["dm_fail_latch"])

    def test_sequence_gap_discards_old_baseline_and_resyncs(self):
        self.hello()
        self.monitor.status = {"old": True}
        self.monitor.snapshot = [{"old": True}]
        payload = (25).to_bytes(4, "little") + bytes(link.STATUS_PAYLOAD_SIZE - 4)
        before = len(self.uart.writes)
        self.monitor._handle_frame(frame(link.STATUS, 15, payload), 200)
        self.assertEqual(len(self.uart.writes), before + 2)
        self.assertNotIn("old", self.monitor.status)
        self.assertTrue(any(item.get("type") == "resync" for item in self.messages))

    def test_ping_is_suppressed_while_frames_are_active(self):
        self.assertFalse(self.monitor.ping_if_idle(100))
        self.monitor.last_valid_ms = 1900
        self.assertFalse(self.monitor.ping_if_idle(2100))
        self.assertEqual(self.uart.writes, [])

    def test_ping_probes_an_idle_link_at_the_interval(self):
        self.assertFalse(self.monitor.ping_if_idle(100))
        self.monitor.last_valid_ms = 500
        self.assertTrue(self.monitor.ping_if_idle(2500))
        decoded = link.FrameDecoder().feed(self.uart.writes[-1])
        self.assertEqual(decoded[0]["kind"], link.PING)
        self.assertFalse(self.monitor.ping_if_idle(3000))

    def test_snapshot_is_installed_only_after_all_unique_records(self):
        self.hello()
        self.monitor._handle_frame(frame(link.FULL_STATUS_RECORD, 2,
                                         snapshot_payload(7, 1, 2)), 200)
        self.assertIsNone(self.monitor.snapshot)
        self.monitor._handle_frame(frame(link.FULL_STATUS_RECORD, 2,
                                         snapshot_payload(7, 0, 2)), 250)
        self.assertEqual(len(self.monitor.snapshot), 2)
        self.assertEqual(self.monitor.link_state, "online")

    def test_duplicate_snapshot_index_is_rejected(self):
        self.hello()
        payload = snapshot_payload(8, 0, 2)
        self.monitor._handle_frame(frame(link.FULL_STATUS_RECORD, 2, payload), 200)
        self.monitor._handle_frame(frame(link.FULL_STATUS_RECORD, 2, payload), 210)
        self.assertIsNone(self.monitor._snapshot_parts)
        self.assertTrue(any(item.get("snapshot_error") == "duplicate_index"
                            for item in self.messages))

    def test_printer_long_transaction_event_is_decoded(self):
        data = (0x040d).to_bytes(2, "little") + bytes((1, 3, 6, 17, 0, 0x5a))
        payload = (77).to_bytes(4, "little") + bytes((
            link.RECORD_PRINTER_LONG_TRANSACTION, 3, 1, 8)) + data
        event = self.monitor._decode_event(payload)
        self.assertEqual(event["event_name"], "printer_long_transaction")
        self.assertEqual((event["frame_type"], event["request_length"], event["payload_hash"]),
                         (0x040d, 17, 0x5a))

    def test_printer_transaction_event_carries_the_resolved_class(self):
        # command is a raw wire byte and cannot identify the frame. Without
        # rx_class an unanswered transaction says only that something went
        # unanswered, which is where an A1 on printer firmware 1.08 stalled:
        # the class lived solely in the snapshot's one-slot last_rx_class.
        data = bytes((0x04, 1, 5, 14, 13, 0, 2))
        payload = (91).to_bytes(4, "little") + bytes((
            link.RECORD_PRINTER_TRANSACTION, 3, 1, 7)) + data + b"\x00"
        event = self.monitor._decode_event(payload)
        self.assertEqual(event["event_name"], "printer_transaction")
        self.assertEqual(
            (event["command"], event["outcome"], event["reason"],
             event["response_length"], event["rx_class"]),
            (0x04, 5, 14, 0, 2))

    def test_printer_transaction_without_rx_class_is_not_misread(self):
        # A six-byte payload is the pre-rx_class encoding. Decoding it as if it
        # carried a class would attribute a transaction to whatever byte
        # followed, so the decoder must decline instead.
        payload = (91).to_bytes(4, "little") + bytes((
            link.RECORD_PRINTER_TRANSACTION, 3, 1, 6)) + bytes((0x04, 1, 5, 14, 13, 0)) + b"\x00\x00"
        event = self.monitor._decode_event(payload)
        self.assertNotEqual(event["event_name"], "printer_transaction")
        self.assertNotIn("rx_class", event)

    def test_printer_auth_is_installed_with_complete_snapshot(self):
        self.hello()
        trace = ((0x040e).to_bytes(2, "little") + (2).to_bytes(2, "little") +
                 (3).to_bytes(2, "little") + (17).to_bytes(2, "little") +
                 (1234).to_bytes(4, "little") + bytes((3, 6, 0, 0x6b)))
        payload = ((7).to_bytes(2, "little") + bytes((
            0, 1, link.FULL_RECORD_PRINTER_AUTH, 0)) +
            (1300).to_bytes(4, "little") + trace)
        self.monitor._handle_frame(frame(link.FULL_STATUS_RECORD, 2, payload), 250)
        self.assertEqual(self.monitor.printer_auth["last_type"], 0x040e)
        self.assertEqual(self.monitor.printer_auth["count_040e"], 3)
        self.assertEqual(self.monitor.printer_auth["payload_hash"], 0x6b)

    def test_printer_rx_metrics_are_installed_atomically(self):
        self.hello()
        records = (
            (link.FULL_RECORD_PRINTER_RX_CORE, (1, 2, 3, 4)),
            (link.FULL_RECORD_PRINTER_RX_LOSS, (5, 6, 7, 8)),
            (link.FULL_RECORD_PRINTER_RX_DMA, (9, 10, 11, 12)),
            (link.FULL_RECORD_PRINTER_TX_CORE, (13, 14, 15, 16)),
            (link.FULL_RECORD_PRINTER_TX_FAULT, (17, 18, 19, 20)),
        )
        for index, (record_type, values) in enumerate(records):
            data = b"".join(value.to_bytes(4, "little") for value in values)
            payload = ((21).to_bytes(2, "little") + bytes((
                index, len(records), record_type, 0)) +
                (100).to_bytes(4, "little") + data)
            self.monitor._handle_frame(frame(link.FULL_STATUS_RECORD, 2, payload), 250)
        self.assertEqual(self.monitor.printer_rx["rx_bytes"], 1)
        self.assertEqual(self.monitor.printer_rx["rx_publish_drop"], 6)
        self.assertEqual(self.monitor.printer_rx["rx_dma_overrun"], 9)
        self.assertEqual(self.monitor.printer_rx["rx_compat_copy"], 12)
        self.assertEqual(self.monitor.printer_tx["tx_started"], 13)
        self.assertEqual(self.monitor.printer_tx["tx_response_missing"], 16)
        self.assertEqual(self.monitor.printer_tx["tx_dma_error"], 18)
        self.assertEqual(self.monitor.printer_tx["tx_timeout"], 19)
        self.assertEqual(self.monitor.printer_tx["tx_no_response_expected"], 20)

    def feed_ams_snapshot(self, snapshot_id=31, assert_atomic=False):
        service = b"".join(value.to_bytes(4, "little")
                           for value in (1500, 64000, 2000, 9000))
        registration = (b"".join(value.to_bytes(2, "little")
                                 for value in (11, 22, 33, 44, 55, 66, 77)) +
                        bytes((0x1B, 0)))
        records = ((link.FULL_RECORD_AMS_SERVICE, service),
                   (link.FULL_RECORD_AMS_REGISTRATION, registration))
        for index, (record_type, data) in enumerate(records):
            payload = (snapshot_id.to_bytes(2, "little") + bytes((
                index, len(records), record_type, 0)) +
                (400).to_bytes(4, "little") + data)
            self.monitor._handle_frame(frame(link.FULL_STATUS_RECORD, 2, payload), 250)
            if assert_atomic and index == 0:
                self.assertIsNone(self.monitor.ams_service)
                self.assertIsNone(self.monitor.ams_registration)

    def test_ams_service_records_are_installed_with_a_complete_snapshot(self):
        self.hello()
        self.feed_ams_snapshot(assert_atomic=True)
        self.assertEqual(self.monitor.ams_service, {
            "gap_now_ms": 1500, "gap_max_ms": 64000,
            "gap_max_since_confirm_ms": 2000, "ms_since_confirm": 9000,
        })
        installed = self.monitor.ams_registration
        self.assertEqual((installed["count_motion"], installed["count_stu_motion"],
                          installed["count_mc_online"]), (11, 22, 33))
        self.assertEqual((installed["registration_query_count"],
                          installed["would_reoffer_count"]), (44, 55))
        self.assertEqual((installed["confirm_count"], installed["reset_count"],
                          installed["flags"]), (66, 77, 0x1B))
        self.assertTrue(installed["registered"])
        self.assertTrue(installed["confirm_settled"])
        self.assertFalse(installed["service_stale"])
        self.assertTrue(installed["reoffer_armed"])
        self.assertTrue(installed["have_service"])

    def test_ams_families_are_cleared_by_an_ams_less_snapshot(self):
        # A snapshot without the AMS records means the firmware did not report
        # them; serving the previous snapshot's numbers would hand the operator
        # stale values on exactly the lines the 409D runbook reads.
        self.hello()
        self.feed_ams_snapshot()
        self.assertIsNotNone(self.monitor.ams_service)
        self.assertIsNotNone(self.monitor.ams_registration)
        self.monitor._handle_frame(frame(link.FULL_STATUS_RECORD, 2,
                                         snapshot_payload(32, 0, 1)), 260)
        self.assertTrue(self.monitor.snapshot)
        self.assertIsNone(self.monitor.ams_service)
        self.assertIsNone(self.monitor.ams_registration)

    def test_soft_reset_guard_requires_complete_idle_snapshot(self):
        self.assertEqual(self.monitor.soft_reset_guard_error(),
                         "complete fresh BMCU status is required")
        self.monitor.link_state = "online"
        self.monitor.snapshot = [{}]
        self.assertEqual(self.monitor.soft_reset_guard_error(),
                         "complete channel status is required")
        idle = {"motor_pwm": 0, "controller_motion": 3, "ams_motion": 0}
        self.monitor.channels = [dict(idle) for _ in range(4)]
        self.assertIsNone(self.monitor.soft_reset_guard_error())
        self.monitor.channels[2]["motor_pwm"] = 1
        self.assertEqual(self.monitor.soft_reset_guard_error(),
                         "BMCU motion is not idle")

    def test_soft_reset_permits_a_channel_resting_in_pressure_ctrl_idle(self):
        # A channel holding filament rests in pressure_ctrl_idle (7), never in
        # stop (3). Requiring 3 on all four made the accept path unreachable
        # with filament loaded, which is where 0500_409D recovery starts.
        self._idle_snapshot()
        self.monitor.channels[1]["controller_motion"] = 7
        self.assertIsNone(self.monitor.soft_reset_guard_error(1000))

    def test_soft_reset_still_refuses_the_moving_controller_phases(self):
        # Everything outside stop and pressure_ctrl_idle either drives the
        # motor or is a transition into driving it.
        for motion in (0, 1, 2, 4, 5, 6, 8):
            with self.subTest(controller_motion=motion):
                self._idle_snapshot()
                self.monitor.channels[0]["controller_motion"] = motion
                self.assertEqual(self.monitor.soft_reset_guard_error(1000),
                                 "BMCU motion is not idle")

    def _idle_snapshot(self, at_ms=1000):
        self.monitor.link_state = "online"
        self.monitor.snapshot = [{}]
        self.monitor.snapshot_at_ms = at_ms
        idle = {"motor_pwm": 0, "controller_motion": 3, "ams_motion": 0}
        self.monitor.channels = [dict(idle) for _ in range(4)]

    def test_soft_reset_refuses_a_snapshot_older_than_the_limit(self):
        # The gate reads motor state from the snapshot, which is only requested
        # when the baseline is invalidated. Without an age check a snapshot
        # taken while idle keeps permitting a reset after motion has started.
        self._idle_snapshot(at_ms=1000)
        fresh = 1000 + self.monitor.MAX_SNAPSHOT_AGE_MS - 1
        stale = 1000 + self.monitor.MAX_SNAPSHOT_AGE_MS

        self.assertIsNone(self.monitor.soft_reset_guard_error(fresh))
        self.assertEqual(self.monitor.soft_reset_guard_error(stale),
                         "BMCU status is stale; retry once it refreshes")

    def test_soft_reset_refuses_when_the_snapshot_has_no_timestamp(self):
        self._idle_snapshot(at_ms=1000)
        self.monitor.snapshot_at_ms = None
        self.assertEqual(self.monitor.soft_reset_guard_error(5000),
                         "BMCU status is stale; retry once it refreshes")

    def test_omitting_the_clock_keeps_the_previous_contract(self):
        # Callers that cannot supply a clock still get the completeness and
        # idleness checks rather than an exception.
        self._idle_snapshot(at_ms=1000)
        self.assertIsNone(self.monitor.soft_reset_guard_error())

    def test_a_completed_snapshot_records_when_it_arrived(self):
        self.hello()
        self.monitor._handle_frame(
            frame(link.FULL_STATUS_RECORD, 2, snapshot_payload(7, 0, 1)), 4242)
        self.assertEqual(self.monitor.snapshot_at_ms, 4242)

    def test_invalidating_the_baseline_clears_the_snapshot_timestamp(self):
        self._idle_snapshot(at_ms=1000)
        self.monitor._invalidate_baseline(2000, "test")
        self.assertIsNone(self.monitor.snapshot_at_ms)
        self.assertEqual(self.monitor.soft_reset_guard_error(2000),
                         "complete fresh BMCU status is required")

    def test_refresh_is_requested_once_while_one_is_outstanding(self):
        self._idle_snapshot()
        self.assertTrue(self.monitor.refresh_snapshot_if_idle())
        decoded = link.FrameDecoder().feed(self.uart.writes[-1])[0]
        self.assertEqual(decoded["kind"], link.GET_FULL_STATUS)

        before = len(self.uart.writes)
        self.assertFalse(self.monitor.refresh_snapshot_if_idle())
        self.assertEqual(len(self.uart.writes), before)

    def test_the_guard_never_sends_anything(self):
        # Requesting a refresh belongs to the caller; a predicate that probes
        # the link cannot be evaluated safely from a diagnostic view.
        self._idle_snapshot(at_ms=1000)
        before = len(self.uart.writes)
        self.monitor.soft_reset_guard_error(1000 + 10 ** 6)
        self.assertEqual(len(self.uart.writes), before)

    def test_soft_reset_request_and_ack_are_tracked(self):
        sequence = self.monitor.request_soft_reset(0x12345678, reason=2, ttl_ms=4000)
        decoded = link.FrameDecoder().feed(self.uart.writes[-1])[0]
        self.assertEqual(decoded["kind"], link.REQUEST_SOFT_RESET)
        self.assertEqual(decoded["payload"], bytes.fromhex("785634120200a00f"))
        self.monitor._handle_frame(
            frame(link.ACK, sequence, bytes((link.REQUEST_SOFT_RESET, 0))), 200)
        self.assertEqual(self.monitor.soft_reset["state"], "scheduled")
        self.hello(sequence=20)
        self.assertEqual(self.monitor.soft_reset["state"], "rebooted")
        self.monitor._handle_frame(frame(link.FULL_STATUS_RECORD, 2,
                                         snapshot_payload(8, 0, 1)), 250)
        self.assertEqual(self.monitor.soft_reset["state"], "completed")
        self.assertEqual(self.monitor.soft_reset["bmcu_boot_session"], 1)

    def test_reset_cancel_event_is_decoded_and_applied(self):
        self.monitor.request_soft_reset(7)
        data = (7).to_bytes(4, "little") + bytes((2, 0, 1, 0))
        payload = (99).to_bytes(4, "little") + bytes((
            link.RECORD_RESET_STATE, 3, 0, 8)) + data
        self.monitor._handle_frame(frame(link.EVENT, 8, payload), 200)
        self.assertEqual(self.monitor.events[-1]["event_name"], "reset_state")
        self.assertEqual(self.monitor.soft_reset["state"], "cancelled")
        self.assertEqual(self.monitor.soft_reset["cancel_reason"], 1)

    def test_solicited_status_reply_does_not_trigger_resync(self):
        self.hello()
        get_status_wire = next(wire for wire in self.uart.writes
                                if link.FrameDecoder().feed(wire)[0]["kind"] == link.GET_STATUS)
        request_sequence = link.FrameDecoder().feed(get_status_wire)[0]["sequence"]
        before = len(self.uart.writes)
        payload = (25).to_bytes(4, "little") + bytes(link.STATUS_PAYLOAD_SIZE - 4)
        self.monitor._handle_frame(frame(link.STATUS, request_sequence, payload), 200)
        self.assertFalse(any(item.get("type") == "resync" for item in self.messages))
        self.assertIsNotNone(self.monitor.status)
        self.assertEqual(self.monitor._last_unsolicited_sequence, 10)
        self.assertEqual(len(self.uart.writes), before)

    def test_solicited_sequence_is_consumed_after_one_reply(self):
        self.hello()
        get_status_wire = next(wire for wire in self.uart.writes
                                if link.FrameDecoder().feed(wire)[0]["kind"] == link.GET_STATUS)
        request_sequence = link.FrameDecoder().feed(get_status_wire)[0]["sequence"]
        payload = (25).to_bytes(4, "little") + bytes(link.STATUS_PAYLOAD_SIZE - 4)
        self.monitor._handle_frame(frame(link.STATUS, request_sequence, payload), 200)
        self.assertFalse(any(item.get("type") == "resync" for item in self.messages))
        self.monitor._handle_frame(frame(link.STATUS, request_sequence, payload), 210)
        self.assertTrue(any(item.get("type") == "resync" for item in self.messages))

    def test_resync_loop_is_broken_end_to_end(self):
        self.hello()
        get_status_wire = next(wire for wire in self.uart.writes
                                if link.FrameDecoder().feed(wire)[0]["kind"] == link.GET_STATUS)
        request_sequence = link.FrameDecoder().feed(get_status_wire)[0]["sequence"]
        payload = (25).to_bytes(4, "little") + bytes(link.STATUS_PAYLOAD_SIZE - 4)
        self.monitor._handle_frame(frame(link.STATUS, request_sequence, payload), 200)
        self.assertFalse(any(item.get("type") == "resync" for item in self.messages))
        # The solicited reply must not have advanced _last_unsolicited_sequence, so the
        # next unsolicited STATUS/EVENT keeps following the hello sequence (10), not the
        # solicited reply's sequence -- this is what breaks the endless resync loop.
        self.monitor._handle_frame(frame(link.STATUS, 11, payload), 250)
        self.assertFalse(any(item.get("type") == "resync" for item in self.messages))

    def test_incomplete_snapshot_times_out_and_retries(self):
        self.hello()
        self.monitor._handle_frame(frame(link.FULL_STATUS_RECORD, 2,
                                         snapshot_payload(9, 0, 2)), 200)
        self.monitor._service_snapshot_timeout(1400)
        before = len(self.uart.writes)
        self.monitor._service_snapshot_timeout(1900)
        self.assertEqual(len(self.uart.writes), before + 1)
        self.assertTrue(any(item.get("reason") == "timeout" for item in self.messages))

    def _full_status_write_count(self):
        count = 0
        for wire in self.uart.writes:
            if link.FrameDecoder().feed(wire)[0]["kind"] == link.GET_FULL_STATUS:
                count += 1
        return count

    def test_ack_busy_backs_off_and_eventually_exhausts_with_growing_delay(self):
        self.hello()
        self.assertEqual(self._full_status_write_count(), 1)

        # 1st BUSY ack -> retries=1, backoff 250<<1=500ms.
        self.monitor._handle_frame(
            frame(link.ACK, 900, bytes((link.GET_FULL_STATUS, 3))), 150)
        self.assertEqual(self.monitor._snapshot_retries, 1)
        retry_at_1 = self.monitor._snapshot_retry_ms
        self.assertEqual(retry_at_1, 150 + 500)
        self.monitor._service_snapshot_timeout(retry_at_1)
        self.assertEqual(self._full_status_write_count(), 2)

        # 2nd BUSY ack -> retries=2, backoff 250<<2=1000ms (strictly larger than before).
        self.monitor._handle_frame(
            frame(link.ACK, 901, bytes((link.GET_FULL_STATUS, 3))), 750)
        self.assertEqual(self.monitor._snapshot_retries, 2)
        retry_at_2 = self.monitor._snapshot_retry_ms
        self.assertEqual(retry_at_2, 750 + 1000)
        self.monitor._service_snapshot_timeout(retry_at_2)
        self.assertEqual(self._full_status_write_count(), 3)

        # 3rd BUSY ack -> retries=3, still within SNAPSHOT_MAX_RETRIES, one more request.
        self.monitor._handle_frame(
            frame(link.ACK, 902, bytes((link.GET_FULL_STATUS, 3))), 1850)
        self.assertEqual(self.monitor._snapshot_retries, 3)
        retry_at_3 = self.monitor._snapshot_retry_ms
        self.assertEqual(retry_at_3, 1850 + 2000)
        self.monitor._service_snapshot_timeout(retry_at_3)
        self.assertEqual(self._full_status_write_count(), 4)

        # 4th BUSY ack -> retries=4, exceeds SNAPSHOT_MAX_RETRIES once its retry fires.
        self.monitor._handle_frame(
            frame(link.ACK, 903, bytes((link.GET_FULL_STATUS, 3))), 3950)
        self.assertEqual(self.monitor._snapshot_retries, 4)
        retry_at_4 = self.monitor._snapshot_retry_ms
        self.assertEqual(retry_at_4, 3950 + 4000)
        writes_before_exhaustion = self._full_status_write_count()
        self.monitor._service_snapshot_timeout(retry_at_4)
        self.assertEqual(self._full_status_write_count(), writes_before_exhaustion)
        self.assertEqual(self.monitor.link_state, "stale")
        self.assertTrue(any(item.get("reason") == "retry_exhausted" for item in self.messages))
        # The hot loop is broken: retries are reset so a later baseline request can
        # start a fresh, slow-paced round instead of being permanently exhausted.
        self.assertEqual(self.monitor._snapshot_retries, 0)

        # Recovery: once the BMCU stops being busy, the next baseline-triggering
        # frame (an unsolicited STATUS advancing the sequence) requests a fresh
        # snapshot instead of staying stuck forever.
        writes_before_recovery = self._full_status_write_count()
        payload = (25).to_bytes(4, "little") + bytes(link.STATUS_PAYLOAD_SIZE - 4)
        self.monitor._handle_frame(frame(link.STATUS, 11, payload), retry_at_4 + 50)
        self.assertEqual(self._full_status_write_count(), writes_before_recovery + 1)

    def test_stale_outstanding_get_status_expires_and_is_treated_as_unsolicited(self):
        self.hello()
        self.assertEqual(len(self.monitor._outstanding_get_status), 1)
        stale_sequence, sent_at = self.monitor._outstanding_get_status[0]

        past_expiry = sent_at + link.BMCUMonitor.OUTSTANDING_GET_STATUS_TTL_MS + 1
        payload = (25).to_bytes(4, "little") + bytes(link.STATUS_PAYLOAD_SIZE - 4)
        self.monitor._handle_frame(frame(link.STATUS, stale_sequence, payload), past_expiry)

        # Expired: the stale solicited sequence must not swallow this frame -- it is
        # classified as unsolicited (and, since it doesn't follow the hello sequence,
        # triggers the gap/resync path) and updates _last_unsolicited_sequence.
        self.assertTrue(any(item.get("type") == "resync" for item in self.messages))
        self.assertEqual(self.monitor._last_unsolicited_sequence, stale_sequence)


if __name__ == "__main__":
    unittest.main()
