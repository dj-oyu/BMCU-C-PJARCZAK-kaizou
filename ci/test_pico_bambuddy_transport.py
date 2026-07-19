import importlib.util
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "pico_bambuddy_transport", ROOT / "pico" / "bambuddy_transport.py")
transport = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(transport)


class BambuddyTransportTests(unittest.TestCase):
    def builder(self):
        return transport.EnvelopeBuilder("bridge-a", boot_session="pico-session")

    def test_transport_sequence_is_unique_when_bmcu_sequence_is_shared(self):
        builder = self.builder()
        one = builder.build({"type": "full_status_record", "link_id": "bmcu-a",
                             "kind": 0x73, "sequence": 50, "record": 0}, 100)
        two = builder.build({"type": "full_status_record", "link_id": "bmcu-a",
                             "kind": 0x73, "sequence": 50, "record": 1}, 110)
        self.assertEqual(one["link"]["bmcu_sequence"], 50)
        self.assertEqual(two["link"]["bmcu_sequence"], 50)
        self.assertEqual(one["link"]["transport_sequence"], 0)
        self.assertEqual(two["link"]["transport_sequence"], 1)

    def test_sequences_are_scoped_per_link(self):
        builder = self.builder()
        a = builder.build({"type": "status", "link_id": "a", "sequence": 1}, 1)
        b = builder.build({"type": "status", "link_id": "b", "sequence": 1}, 2)
        self.assertEqual(a["link"]["transport_sequence"], 0)
        self.assertEqual(b["link"]["transport_sequence"], 0)

    def test_persisted_watermark_removes_only_matching_link_session(self):
        outbox = transport.BambuddyOutbox("bridge", boot_session="boot")
        outbox.publish({"type": "status", "link_id": "a", "sequence": 1}, 1, 1000)
        outbox.publish({"type": "status", "link_id": "b", "sequence": 1}, 2, 2000)
        outbox.publish({"type": "event", "link_id": "a", "sequence": 2}, 3, 3000)
        result = outbox.apply_ack({"persisted": [
            {"link_id": "a", "pico_boot_session": "boot", "transport_sequence": 0}
        ]})
        self.assertEqual(result["persisted"], 1)
        self.assertEqual(len(outbox.queue), 2)

    def test_nonretryable_rejection_is_dropped_but_retryable_is_retained(self):
        outbox = transport.BambuddyOutbox("bridge", boot_session="boot")
        outbox.publish({"type": "status", "link_id": "a", "sequence": 1}, 1, 1000)
        outbox.publish({"type": "event", "link_id": "a", "sequence": 2}, 2, 2000)
        outbox.apply_ack({"rejected": [
            {"link_id": "a", "pico_boot_session": "boot",
             "transport_sequence": 0, "retryable": False},
            {"link_id": "a", "pico_boot_session": "boot",
             "transport_sequence": 1, "retryable": True},
        ]})
        self.assertEqual(len(outbox.queue), 1)
        self.assertEqual(outbox.queue.batch()[0]["link"]["transport_sequence"], 1)
        self.assertEqual(len(outbox.queue.quarantined), 1)

    def test_ambiguous_rejection_is_retained_until_adapter_enriches_it(self):
        outbox = transport.BambuddyOutbox("bridge", boot_session="boot")
        outbox.publish({"type": "status", "link_id": "a"}, 1, 1000)
        outbox.publish({"type": "status", "link_id": "b"}, 2, 2000)
        result = outbox.apply_ack({"rejected": [
            {"transport_sequence": 0, "retryable": False}
        ]})
        self.assertEqual(result["rejected"], 0)
        self.assertEqual(len(outbox.queue), 2)


    def test_queue_prefers_dropping_old_status(self):
        queue = transport.TelemetryQueue(limit=3, max_age_ms=30000)
        builder = self.builder()
        event = builder.build({"type": "event", "link_id": "a"}, 1)
        old_status = builder.build({"type": "status", "link_id": "a"}, 2)
        hello = builder.build({"type": "hello", "link_id": "a"}, 3)
        new_status = builder.build({"type": "status", "link_id": "a"}, 4)
        queue.enqueue(event, 1)
        queue.enqueue(old_status, 2)
        queue.enqueue(hello, 3)
        queue.enqueue(new_status, 4)
        sequences = [item["link"]["transport_sequence"] for item in queue.batch()]
        self.assertNotIn(old_status["link"]["transport_sequence"], sequences)
        self.assertIn(event["link"]["transport_sequence"], sequences)

    def test_age_bound_emits_transport_drop_notice(self):
        outbox = transport.BambuddyOutbox(
            "bridge", boot_session="boot", queue_age_ms=10, queue_limit=4)
        outbox.publish({"type": "status", "link_id": "a"}, 0, 0)
        outbox.publish({"type": "status", "link_id": "a"}, 20, 20000)
        kinds = [item["frame"]["kind"] for item in outbox.queue.batch()]
        self.assertIn("transport_drop", kinds)
        self.assertEqual(outbox.queue.dropped_count, 1)


if __name__ == "__main__":
    unittest.main()
