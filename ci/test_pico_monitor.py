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
        self.assertGreater(decoder.frame_errors, 0)

    def test_hello_starts_one_atomic_baseline(self):
        self.hello()
        kinds = [link.FrameDecoder().feed(wire)[0]["kind"] for wire in self.uart.writes]
        self.assertEqual(kinds, [link.GET_STATUS, link.GET_FULL_STATUS])
        self.assertEqual(self.monitor.link_state, "resyncing")
        self.assertEqual(self.messages[0]["link_id"], "bmcu-b")

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
        self.assertEqual(self.monitor.printer_tx["tx_event_suppressed"], 20)

    def test_incomplete_snapshot_times_out_and_retries(self):
        self.hello()
        self.monitor._handle_frame(frame(link.FULL_STATUS_RECORD, 2,
                                         snapshot_payload(9, 0, 2)), 200)
        self.monitor._service_snapshot_timeout(1400)
        before = len(self.uart.writes)
        self.monitor._service_snapshot_timeout(1900)
        self.assertEqual(len(self.uart.writes), before + 1)
        self.assertTrue(any(item.get("reason") == "timeout" for item in self.messages))


if __name__ == "__main__":
    unittest.main()
