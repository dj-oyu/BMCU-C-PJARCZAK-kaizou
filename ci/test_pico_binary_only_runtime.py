import importlib.util
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).parents[1]
PICO = ROOT / "pico"
sys.path.insert(0, str(PICO))
spec = importlib.util.spec_from_file_location(
    "device_metrics_test", PICO / "device_metrics.py")
device_metrics = importlib.util.module_from_spec(spec)
spec.loader.exec_module(device_metrics)

import bmcu_binary as binary
import bmcu_binary_constants as C
from bmcu_binary_outbox import BMB1Outbox


class Monitor:
    uart_backlog = 3
    uart_max_backlog = 12
    uart_drain_bytes = 900
    uart_overflow_count = 2
    uart_max_service_gap_us = 400
    sequence_gap_count = 1

    class decoder:
        crc_errors = 4
        frame_errors = 5


class BinaryOnlyRuntimeTests(unittest.TestCase):
    def test_metric_window_is_fixed_and_reports_tail_quantiles(self):
        window = device_metrics.MetricWindow()
        for value in (1, 2, 3, 4, 1000):
            window.add(value)
        self.assertEqual(window.count, 5)
        self.assertEqual(window.average(), 202)
        self.assertGreaterEqual(window.percentile(95), 1000)

    def test_metric_window_average_remains_sensible_after_decay(self):
        window = device_metrics.MetricWindow()
        for _ in range(2048):
            window.add(100)
        self.assertGreater(window.count, 0)
        self.assertGreaterEqual(window.average(), 64)
        self.assertLessEqual(window.average(), 128)

    def test_diagnostic_is_bounded_typed_tlv(self):
        metrics = device_metrics.DeviceMetrics()
        for value in range(1, 50):
            metrics.observe_loop_gap(value)
        payload = metrics.snapshot(123, [Monitor()])
        items = list(binary.parse_tlvs(payload))
        tags = {item[0] for item in items}
        self.assertIn(C.DIAG_LOOP_GAP_P95_US, tags)
        self.assertIn(C.DIAG_UART0_DRAIN_BYTES, tags)
        self.assertIn(C.DIAG_UART0_OVERFLOW_COUNT, tags)
        self.assertLessEqual(len(payload), C.MAX_PAYLOAD_SIZE)
        outbox = BMB1Outbox(9, durable_slots=2, large_slots=2)
        sequence = outbox.enqueue_payload(
            C.PICO_DIAGNOSTIC, 0, C.GLOBAL_SCOPE, 1, payload)
        self.assertEqual(sequence, 1)
        self.assertEqual(len(outbox.large), 1)

    def test_large_log_and_oversized_payload_have_bounded_outcomes(self):
        outbox = BMB1Outbox(9, durable_slots=2, large_slots=2)
        sequence = outbox.enqueue_payload(
            C.PICO_LOG, 0, C.GLOBAL_SCOPE, 1,
            b"x" * (1024 - C.HEADER_SIZE))
        self.assertEqual(sequence, 1)
        self.assertEqual(len(outbox.large), 1)
        self.assertEqual(outbox.enqueue_payload(
            C.PICO_LOG, 0, C.GLOBAL_SCOPE, 2,
            b"x" * (C.MAX_PAYLOAD_SIZE + 1)), 0)
        self.assertEqual(outbox.forced_drop_count, 1)

    def test_legacy_json_transports_and_endpoints_are_absent(self):
        for name in (
            "bambuddy_config.py", "bambuddy_https.py",
            "bambuddy_session.py", "bambuddy_tls.py",
            "bambuddy_transport.py", "bambuddy_ws.py", "bmcu_control.py",
        ):
            self.assertFalse((PICO / name).exists(), name)
        source = (PICO / "main.py").read_text(encoding="utf-8")
        web = (PICO / "web_ui.py").read_text(encoding="utf-8")
        runtime = (PICO / "runtime_log.py").read_text(encoding="utf-8")
        for forbidden in (
            "BambuddyWebSocketClient", "BambuddyNdjsonClient",
            "/api/devices", "/api/pico/logs", "json.dumps", "json.loads",
        ):
            self.assertNotIn(forbidden, source + web + runtime)

    def test_control_surface_remains_soft_reset_only(self):
        registry = (ROOT / "docs" / "bmcu_binary_registry.json").read_text(
            encoding="utf-8")
        self.assertIn('"SOFT_RESET": 1', registry)
        self.assertNotIn("SET_LED_MODE", registry)


if __name__ == "__main__":
    unittest.main()
