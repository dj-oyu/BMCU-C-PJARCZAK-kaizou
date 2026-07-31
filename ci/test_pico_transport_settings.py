import importlib.util
import pathlib
import tempfile
import unittest


PICO = pathlib.Path(__file__).parents[1] / "pico"
spec = importlib.util.spec_from_file_location(
    "transport_settings_runtime", PICO / "transport_settings.py")
transport = importlib.util.module_from_spec(spec)
spec.loader.exec_module(transport)


class TransportSettingsTests(unittest.TestCase):
    def test_config_values_are_available_without_writing(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "transport.cfg"
            store = transport.TransportSettingsStore(
                "bambuddy.local", 8766, str(path))

            self.assertTrue(store.configured)
            self.assertEqual(store.host, "bambuddy.local")
            self.assertEqual(store.port, 8766)
            self.assertFalse(path.exists())

    def test_update_is_persisted_and_overrides_config(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "transport.cfg"
            store = transport.TransportSettingsStore(
                "old.local", 1000, str(path))

            updated = store.update("192.168.1.33", 8766)
            reloaded = transport.TransportSettingsStore(
                "old.local", 1000, str(path))

            self.assertEqual(updated, ("192.168.1.33", 8766))
            self.assertEqual(reloaded.host, "192.168.1.33")
            self.assertEqual(reloaded.port, 8766)
            self.assertFalse(pathlib.Path(str(path) + ".bak").exists())

    def test_backup_is_used_after_interrupted_replacement(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "transport.cfg"
            value = transport.TransportSettingsStore._encode(
                "backup.local", 4321)
            pathlib.Path(str(path) + ".bak").write_bytes(value)

            store = transport.TransportSettingsStore("", 0, str(path))

            self.assertEqual((store.host, store.port),
                             ("backup.local", 4321))

    def test_invalid_values_are_rejected(self):
        invalid_hosts = ("", "http://server", "bad host", "a..b", "日本語")
        for value in invalid_hosts:
            with self.subTest(host=value), self.assertRaises(ValueError):
                transport.validate_host(value)
        for value in (0, 65536, "nope"):
            with self.subTest(port=value), self.assertRaises(ValueError):
                transport.validate_port(value)

    def test_api_updates_endpoint_and_rejects_cross_origin_form_shape(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "transport.cfg"
            store = transport.TransportSettingsStore(
                "old.local", 1000, str(path))
            applied = []
            updates = []
            api = transport.TransportSettingsAPI(
                store, lambda host, port: applied.append((host, port)),
                lambda: updates.append(True))

            status = api.handle(
                "GET", "/api/transport/status", {}, b"")
            rejected = api.handle(
                "POST", "/api/transport", {},
                b"bambuddy.local\n8766")
            saved = api.handle(
                "POST", "/api/transport",
                {"content-type": "application/octet-stream",
                 "x-bmcu-settings-action": "update"},
                b"bambuddy.local\n8766")

            self.assertEqual(status[0], "200 OK")
            self.assertIn(b"host=old.local", status[2])
            self.assertEqual(rejected[0], "403 Forbidden")
            self.assertEqual(saved[0], "204 No Content")
            self.assertEqual(applied, [("bambuddy.local", 8766)])
            self.assertEqual(updates, [True])


if __name__ == "__main__":
    unittest.main()
