from datetime import date
import json
from pathlib import Path
import tempfile
import unittest

import firmware_matrix as fm
import release_metadata as rm


class ReleaseMetadataTests(unittest.TestCase):
    def test_default_metadata_matches_established_release_format(self):
        metadata = rm.release_metadata(release_day=date(2026, 7, 30))
        self.assertEqual(metadata["release_label"], "V10.5-kaizou-20260730")
        self.assertEqual(
            metadata["release_name"],
            "BMCU V10.5 kaizou firmware matrix (2026-07-30)",
        )

    def test_manual_formatted_label_controls_title_date(self):
        metadata = rm.release_metadata(
            "V10.6-kaizou-20260801",
            release_day=date(2026, 7, 30),
        )
        self.assertEqual(metadata["release_label"], "V10.6-kaizou-20260801")
        self.assertEqual(
            metadata["release_name"],
            "BMCU V10.6 kaizou firmware matrix (2026-08-01)",
        )

    def test_notes_follow_previous_release_sections_and_refresh_checksums(self):
        with tempfile.TemporaryDirectory() as temporary:
            assets = Path(temporary)
            label = "V10.5-kaizou-20260730"
            prefix = f"BMCU-{label}"
            for suffix in (
                "all.zip",
                "standard-A1.zip",
                "high-force-P1S.zip",
                "soft-load-A1.zip",
            ):
                (assets / f"{prefix}-{suffix}").write_bytes(suffix.encode())
            (assets / f"{prefix}-manifest.json").write_text(
                json.dumps([{"path": "one.bin"}, {"path": "two.bin"}]),
                encoding="utf-8",
            )
            (assets / f"{prefix}-manifest.csv").write_text("path\n", encoding="utf-8")
            (assets / f"{prefix}-manifest.txt").write_text("path\n", encoding="utf-8")
            notes = assets / f"{prefix}-release-notes.md"
            notes.write_text("old notes", encoding="utf-8")
            checksums = assets / f"{prefix}-SHA256SUMS.txt"
            checksums.write_text("old checksums", encoding="utf-8")

            rm.write_release_notes(assets, label)

            body = notes.read_text(encoding="utf-8")
            self.assertIn("## Downloads", body)
            self.assertIn("## Selecting a binary", body)
            self.assertIn("## Verification", body)
            self.assertIn("## Package SHA-256", body)
            self.assertIn("Host tests: passed", body)
            checksum_lines = checksums.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(checksum_lines), 8)
            notes_sha = fm.file_metadata(notes)[0]
            self.assertIn(f"{notes_sha}  {notes.name}", checksum_lines)


if __name__ == "__main__":
    unittest.main()
