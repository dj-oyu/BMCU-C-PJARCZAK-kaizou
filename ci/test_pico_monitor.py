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

    def test_decoder_retains_a_fixed_noise_bound(self):
        decoder = link.FrameDecoder()
        decoder.feed(b"x" * 10000)
        self.assertLessEqual(len(decoder._buffer), link.MAX_DECODER_BUFFER)

    def test_a_chunk_larger_than_the_noise_bound_is_fully_parsed(self):
        # The decoder used to keep only the last MAX_DECODER_BUFFER bytes of a
        # larger chunk, discarding intact frames read in the same drain.
        decoder = link.FrameDecoder()
        wire = b"".join(
            link.encode_frame(link.STATUS, index, bytes(range(27)))
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
            link.encode_frame(link.STATUS, index, bytes(range(27)))
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
        good = link.encode_frame(link.STATUS, 5, bytes(range(27)))
        frames = decoder.feed(b"\x00" * 400 + good + b"\x11" * 400)
        self.assertEqual([f["sequence"] for f in frames], [5])

    def test_crc_rejection_captures_the_whole_candidate_frame(self):
        # Counters alone cannot tell a flipped bit from a truncated frame, so
        # the rejected bytes have to survive for offline analysis.
        capture = link.RejectCapture()
        decoder = link.FrameDecoder(capture)
        decoder.clock_ms = 4242
        wire = bytearray(link.encode_frame(link.STATUS, 7, bytes(range(27))))
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
        decoder.feed(link.encode_frame(link.STATUS, 1, bytes(range(27))))

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
        payload = (25).to_bytes(4, "little") + bytes(23)
        self.monitor._handle_frame(frame(link.STATUS, 15, payload), 200)
        kinds = [link.FrameDecoder().feed(wire)[0]["kind"] for wire in self.uart.writes]
        self.assertEqual(kinds, [link.GET_FULL_STATUS])
        self.monitor._handle_frame(frame(link.STATUS, 16, payload), 210)
        self.assertEqual(len(self.uart.writes), 1)

    def test_sequence_gap_discards_old_baseline_and_resyncs(self):
        self.hello()
        self.monitor.status = {"old": True}
        self.monitor.snapshot = [{"old": True}]
        payload = (25).to_bytes(4, "little") + bytes(23)
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
        payload = (25).to_bytes(4, "little") + bytes(23)
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
        payload = (25).to_bytes(4, "little") + bytes(23)
        self.monitor._handle_frame(frame(link.STATUS, request_sequence, payload), 200)
        self.assertFalse(any(item.get("type") == "resync" for item in self.messages))
        self.monitor._handle_frame(frame(link.STATUS, request_sequence, payload), 210)
        self.assertTrue(any(item.get("type") == "resync" for item in self.messages))

    def test_resync_loop_is_broken_end_to_end(self):
        self.hello()
        get_status_wire = next(wire for wire in self.uart.writes
                                if link.FrameDecoder().feed(wire)[0]["kind"] == link.GET_STATUS)
        request_sequence = link.FrameDecoder().feed(get_status_wire)[0]["sequence"]
        payload = (25).to_bytes(4, "little") + bytes(23)
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
        payload = (25).to_bytes(4, "little") + bytes(23)
        self.monitor._handle_frame(frame(link.STATUS, 11, payload), retry_at_4 + 50)
        self.assertEqual(self._full_status_write_count(), writes_before_recovery + 1)

    def test_stale_outstanding_get_status_expires_and_is_treated_as_unsolicited(self):
        self.hello()
        self.assertEqual(len(self.monitor._outstanding_get_status), 1)
        stale_sequence, sent_at = self.monitor._outstanding_get_status[0]

        past_expiry = sent_at + link.BMCUMonitor.OUTSTANDING_GET_STATUS_TTL_MS + 1
        payload = (25).to_bytes(4, "little") + bytes(23)
        self.monitor._handle_frame(frame(link.STATUS, stale_sequence, payload), past_expiry)

        # Expired: the stale solicited sequence must not swallow this frame -- it is
        # classified as unsolicited (and, since it doesn't follow the hello sequence,
        # triggers the gap/resync path) and updates _last_unsolicited_sequence.
        self.assertTrue(any(item.get("type") == "resync" for item in self.messages))
        self.assertEqual(self.monitor._last_unsolicited_sequence, stale_sequence)


if __name__ == "__main__":
    unittest.main()
