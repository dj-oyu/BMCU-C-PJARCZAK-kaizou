import json
import pathlib
import sys
import tempfile
import unittest


PICO_DIR = pathlib.Path(__file__).parents[1] / "pico"
sys.path.insert(0, str(PICO_DIR))

from runtime_log import PicoRuntimeLog, guarded_call


class RuntimeLogTests(unittest.TestCase):
    def setUp(self):
        self.now = 100
        self.log = PicoRuntimeLog(lambda: self.now, limit=8, crash_path=None)

    def test_ring_buffer_is_bounded(self):
        for index in range(12):
            self.log.info("test", "message-%d" % index)
        self.assertEqual(len(self.log.entries), 8)
        self.assertEqual(self.log.entries[0]["message"], "message-4")
        self.assertEqual(self.log.entries[-1]["sequence"], 12)

    def test_ring_buffer_retains_error_entries(self):
        self.log.exception("web", RuntimeError("important"))
        for index in range(12):
            self.log.info("state", "transition-%d" % index)
        errors = [entry for entry in self.log.entries
                  if entry["level"] == "error"]
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["component"], "web")

    def test_guarded_call_records_error_and_runs_recovery(self):
        recovered = []

        def fail():
            raise RuntimeError("socket exploded")

        self.assertFalse(guarded_call(
            self.log, "web", fail, lambda: recovered.append(True)))
        self.assertEqual(recovered, [True])
        self.assertEqual(self.log.exception_count, 1)
        entry = self.log.entries[-1]
        self.assertEqual(entry["component"], "web")
        self.assertEqual(entry["details"]["exception"], "RuntimeError")
        self.assertIn("socket exploded", entry["details"]["traceback"])

    def test_duplicate_exception_is_suppressed(self):
        self.log.exception("web", RuntimeError("same"))
        self.now = 200
        self.log.exception("web", RuntimeError("same"))
        self.assertEqual(len(self.log.entries), 1)
        self.assertEqual(self.log.entries[0]["details"]["suppressed"], 1)
        self.assertEqual(self.log.exception_count, 2)

    def test_last_crash_is_persisted_and_loaded_on_next_boot(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(pathlib.Path(directory) / "crash.json")
            first = PicoRuntimeLog(lambda: 77, crash_path=path)
            first.exception("wifi", OSError("radio failure"))
            with open(path, encoding="utf-8") as source:
                persisted = json.load(source)
            self.assertEqual(persisted["component"], "wifi")

            second = PicoRuntimeLog(lambda: 3, crash_path=path)
            self.assertEqual(second.previous_crash["component"], "wifi")
            self.assertEqual(second.entries[0]["message"],
                             "previous crash recovered")


if __name__ == "__main__":
    unittest.main()
