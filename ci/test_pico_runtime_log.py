import importlib.util
import pathlib
import sys
import tempfile
import unittest

PICO = pathlib.Path(__file__).parents[1] / "pico"
sys.path.insert(0, str(PICO))
spec = importlib.util.spec_from_file_location(
    "runtime_log_binary", PICO / "runtime_log.py")
runtime_log = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runtime_log)
sys.path.remove(str(PICO))

import bmcu_binary as binary
import bmcu_binary_constants as C


class RuntimeLogTests(unittest.TestCase):
    def setUp(self):
        self.now = 100
        self.log = runtime_log.PicoRuntimeLog(
            lambda: self.now, limit=8, crash_path=None, pico_boot_id=7)

    def messages(self):
        return list(self.log.iter_messages())

    def parse(self, message):
        parser = binary.StreamParser(bytearray(C.MAX_MESSAGE_SIZE))
        parser.feed(message)
        return binary.parse_log(parser.next_message())

    def test_ring_is_fixed_and_binary(self):
        for index in range(12):
            self.log.info("test", "message-%d" % index)
        self.assertEqual(len(self.messages()), 8)
        parsed = self.parse(self.messages()[-1])
        self.assertEqual(parsed[0], 8)
        self.assertEqual(bytes(parsed[3]), b"test")
        self.assertEqual(bytes(parsed[4]), b"message-7")

    def test_guarded_call_records_binary_error_and_recovers(self):
        recovered = []
        self.assertFalse(runtime_log.guarded_call(
            self.log, "web",
            lambda: (_ for _ in ()).throw(RuntimeError("socket exploded")),
            lambda: recovered.append(True)))
        self.assertEqual(recovered, [True])
        parsed = self.parse(self.messages()[-1])
        self.assertEqual(parsed[2], C.LOG_ERROR)
        self.assertEqual(bytes(parsed[3]), b"web")
        self.assertIn(b"socket exploded", bytes(parsed[4]))

    def test_duplicate_exception_is_suppressed_without_new_tree(self):
        self.log.exception("web", RuntimeError("same"))
        self.now = 200
        self.log.exception("web", RuntimeError("same"))
        self.assertEqual(len(self.messages()), 1)
        self.assertEqual(self.log.suppressed_count, 1)
        self.assertEqual(self.log.exception_count, 2)

    def test_bmcr1_roundtrip_and_torn_record(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(pathlib.Path(directory) / "crash.bmcr")
            first = runtime_log.PicoRuntimeLog(
                lambda: 77, crash_path=path, pico_boot_id=8)
            first.exception("wifi", OSError("radio failure"))
            raw = pathlib.Path(path).read_bytes()
            self.assertEqual(raw[:5], b"BMCR1")
            second = runtime_log.PicoRuntimeLog(
                lambda: 3, crash_path=path, pico_boot_id=9)
            self.assertIsNotNone(second.previous_crash)
            pathlib.Path(path).write_bytes(raw[:-1])
            third = runtime_log.PicoRuntimeLog(
                lambda: 3, crash_path=path, pico_boot_id=10)
            self.assertIsNone(third.previous_crash)

    def test_sink_receives_payload_without_dictionary(self):
        received = []
        log = runtime_log.PicoRuntimeLog(
            lambda: 5, crash_path=None, sink=lambda *args: received.append(args))
        log.warning("uart", "gap")
        self.assertEqual(received[0][0], C.PICO_LOG)
        self.assertIsInstance(received[0][4], memoryview)

    def test_full_protected_ring_and_broken_sink_never_raise(self):
        log = runtime_log.PicoRuntimeLog(
            lambda: 5, limit=8, crash_path=None,
            sink=lambda *_: (_ for _ in ()).throw(RuntimeError("sink")))
        for index in range(20):
            log.exception("main", RuntimeError("failure-%d" % index))
        self.assertEqual(len(list(log.iter_messages())), 8)
        log.info("main", "low priority dropped safely")
        self.assertEqual(len(list(log.iter_messages())), 8)


if __name__ == "__main__":
    unittest.main()
