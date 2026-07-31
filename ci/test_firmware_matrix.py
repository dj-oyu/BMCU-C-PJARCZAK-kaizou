import json
from pathlib import Path
import tempfile
import unittest
import zipfile

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


    def test_package_release_writes_profile_zips_and_checksums(self):
        with tempfile.TemporaryDirectory() as temporary:
            temporary_root = Path(temporary)
            root = temporary_root / "dist"
            entries = []
            manifests = root / "manifests"
            manifests.mkdir(parents=True)
            for profile in ("standard", "high_force", "soft_load"):
                relative = Path(
                    f"firmwares/{profile}/autoload_off/filament_rgb_off/solo/solo_0.095f.bin"
                )
                binary = root / relative
                binary.parent.mkdir(parents=True)
                binary.write_bytes(f"firmware-{profile}".encode())
                sha256, crc32, size = fm.file_metadata(binary)
                entries.append({
                    "profile": profile, "profile_label": profile,
                    "bmcu_p1s": int(profile == "high_force"),
                    "bmcu_soft_load": int(profile == "soft_load"), "autoload": 0,
                    "filament_rgb": 0, "target": "solo", "target_id": "solo",
                    "ams_num": 0, "retract_m": "0.095", "path": relative.as_posix(),
                    "size": size, "sha256": sha256, "crc32": crc32, "git_sha": "test",
                })
            (manifests / "manifest-test.json").write_text(json.dumps(entries), encoding="utf-8")
            (root / "guides").mkdir()
            (root / "guides" / "which_to_choose_test.txt").write_text("guide", encoding="utf-8")
            (root / "FIRMWARE_BUILD_MATRIX.md").write_text("matrix", encoding="utf-8")

            output = temporary_root / "release"
            assets = fm.package_release(root, output, "test label", expected_count=3)
            self.assertEqual(len(assets), 9)
            self.assertTrue(all(path.name.startswith("BMCU-test-label-") for path in assets))

            all_zip = output / "BMCU-test-label-all.zip"
            with zipfile.ZipFile(all_zip) as archive:
                binaries = [name for name in archive.namelist() if name.endswith(".bin")]
                self.assertEqual(len(binaries), 3)
                self.assertIn("manifest.json", archive.namelist())
            for slug in fm.RELEASE_PROFILE_SLUGS.values():
                with zipfile.ZipFile(output / f"BMCU-test-label-{slug}.zip") as archive:
                    self.assertEqual(
                        len([name for name in archive.namelist() if name.endswith(".bin")]), 1
                    )
            checksum_lines = (
                output / "BMCU-test-label-SHA256SUMS.txt"
            ).read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(checksum_lines), 8)


if __name__ == "__main__":
    unittest.main()
