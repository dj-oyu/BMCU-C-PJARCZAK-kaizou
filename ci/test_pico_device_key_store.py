import importlib.util
import pathlib
import tempfile
import unittest


PICO = pathlib.Path(__file__).parents[1] / "pico"
spec = importlib.util.spec_from_file_location(
    "device_key_store_runtime", PICO / "device_key_store.py")
device_keys = importlib.util.module_from_spec(spec)
spec.loader.exec_module(device_keys)


class DeviceKeyStoreTests(unittest.TestCase):
    KEY_A = "00" * 32
    KEY_B = "a5" * 32

    def test_config_key_is_available_without_writing_it(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "device.key"
            store = device_keys.DeviceKeyStore(self.KEY_A, str(path))

            self.assertTrue(store.configured)
            self.assertEqual(store.key, bytes(32))
            self.assertFalse(path.exists())
            self.assertNotIn(self.KEY_A.encode(), store.status_body())
            self.assertRegex(store.fingerprint(), r"^[0-9a-f]{12}$")

    def test_update_is_persisted_and_overrides_config(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "device.key"
            store = device_keys.DeviceKeyStore(self.KEY_A, str(path))

            updated = store.update_hex(self.KEY_B.upper())
            reloaded = device_keys.DeviceKeyStore(self.KEY_A, str(path))

            self.assertEqual(updated, bytes([0xA5]) * 32)
            self.assertEqual(path.read_bytes(), updated)
            self.assertEqual(reloaded.key, updated)
            self.assertFalse(path.with_suffix(".key.bak").exists())

    def test_backup_is_used_after_interrupted_replacement(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "device.key"
            pathlib.Path(str(path) + ".bak").write_bytes(bytes([0x5A]) * 32)

            store = device_keys.DeviceKeyStore("", str(path))

            self.assertTrue(store.configured)
            self.assertEqual(store.key, bytes([0x5A]) * 32)

    def test_invalid_key_does_not_replace_current_key(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "device.key"
            store = device_keys.DeviceKeyStore(self.KEY_A, str(path))

            with self.assertRaises(ValueError):
                store.update_hex("not-a-key")

            self.assertEqual(store.key, bytes(32))
            self.assertFalse(path.exists())

    def test_api_is_write_only_and_applies_valid_key(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "device.key"
            store = device_keys.DeviceKeyStore("", str(path))
            applied = []
            updates = []
            api = device_keys.DeviceKeyAPI(
                store, applied.append, lambda: updates.append(True))

            status = api.handle(
                "GET", "/api/device-key/status", {}, b"")
            raw_read = api.handle("GET", "/api/device-key", {}, b"")
            rejected = api.handle(
                "POST", "/api/device-key", {}, self.KEY_B.encode())
            saved = api.handle(
                "POST", "/api/device-key",
                {"content-type": "application/octet-stream",
                 "x-bmcu-key-action": "update"},
                self.KEY_B.encode())

            self.assertEqual(status[0], "200 OK")
            self.assertIn(b"configured=0", status[2])
            self.assertIsNone(raw_read)
            self.assertEqual(rejected[0], "403 Forbidden")
            self.assertEqual(saved[0], "204 No Content")
            self.assertEqual(applied, [bytes([0xA5]) * 32])
            self.assertEqual(updates, [True])
            self.assertNotIn(self.KEY_B.encode(), store.status_body())


if __name__ == "__main__":
    unittest.main()
