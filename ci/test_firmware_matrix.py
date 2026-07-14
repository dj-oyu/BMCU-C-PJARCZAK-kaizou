import json
from pathlib import Path
import tempfile
import unittest

import firmware_matrix as fm


class FirmwareMatrixTests(unittest.TestCase):
    def setUp(self):
        self.config = fm.load_config()

    def test_supported_matrix_has_780_unique_variants(self):
        shards = fm.matrix_shards(self.config)
        targets = fm.targets(self.config)
        self.assertEqual(len(shards), 12)
        self.assertEqual(len(targets), 65)
        paths = {
            fm.artifact_relative_path(shard["profile"], shard["autoload"], shard["rgb"], target).as_posix()
            for shard in shards
            for target in targets
        }
        self.assertEqual(len(paths), 780)

    def test_profiles_do_not_mix_high_force_and_soft_load(self):
        for profile in self.config["profiles"]:
            self.assertFalse(profile["bmcu_p1s"] and profile["bmcu_soft_load"])

    def test_merge_verifies_binary_and_writes_all_formats(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            relative = Path("firmwares/standard/autoload_off/filament_rgb_off/solo/solo_0.095f.bin")
            binary = root / relative
            binary.parent.mkdir(parents=True)
            binary.write_bytes(b"firmware-test")
            sha256, crc32, size = fm.file_metadata(binary)
            entry = {
                "profile": "standard", "profile_label": "standard(A1)",
                "bmcu_p1s": 0, "bmcu_soft_load": 0, "autoload": 0,
                "filament_rgb": 0, "target": "solo", "target_id": "solo",
                "ams_num": 0, "retract_m": "0.095", "path": relative.as_posix(),
                "size": size, "sha256": sha256, "crc32": crc32, "git_sha": "test",
            }
            manifests = root / "manifests"
            manifests.mkdir()
            (manifests / "manifest-standard-autoload0-rgb0.json").write_text(
                json.dumps([entry]), encoding="utf-8"
            )
            merged = fm.merge_manifests(root)
            self.assertEqual(merged, [entry])
            self.assertTrue((root / "manifest.json").is_file())
            self.assertTrue((root / "manifest.csv").is_file())
            self.assertTrue((root / "manifest.txt").is_file())


if __name__ == "__main__":
    unittest.main()
