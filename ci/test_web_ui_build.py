"""The staged web UI artifact and its generated sources must not drift.

The page is built outside Python and committed, so nothing else would notice a
stale ``generated.ts`` or a ``pico/www/index.html.gz`` that no longer matches
the build. These checks run in the ordinary Python suite so a developer without
Node still finds out.
"""
import gzip
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STAGED = ROOT / "pico" / "www" / "index.html.gz"
GENERATED = ROOT / "web" / "src" / "api" / "generated.ts"

# littlefs on the Pico 2 W also holds the journal (8 x 64 KB) and the modules.
# A page beyond this is a signal that a dependency was pulled in by accident.
MAX_STAGED_BYTES = 64 * 1024


class WebUIBuildTests(unittest.TestCase):
    def test_generated_registry_is_current(self):
        result = subprocess.run(
            [sys.executable, str(ROOT / "tools" / "generate_ts_registry.py"),
             "--check"],
            capture_output=True, text=True)
        self.assertEqual(result.returncode, 0,
                         result.stderr or result.stdout)

    def test_generated_registry_carries_the_tags_the_ui_reads(self):
        source = GENERATED.read_text(encoding="utf-8")
        for expected in ("ExceptionCount: 48", "Uart0Backlog: 64",
                         "Uart1Backlog: 72", "Uart0OverflowCount: 81",
                         "BmcuKind", "Status: 2"):
            self.assertIn(expected, source)

    def test_staged_page_is_present_and_bounded(self):
        self.assertTrue(STAGED.is_file(),
                        "run tools/build_web_ui.py to stage the web UI")
        self.assertLess(STAGED.stat().st_size, MAX_STAGED_BYTES)

    def test_staged_page_decompresses_to_the_single_file_build(self):
        html = gzip.decompress(STAGED.read_bytes()).decode("utf-8")
        self.assertIn('<div id="app">', html)
        self.assertIn("BMCU Loader Monitor", html)
        # viteSingleFile must have inlined everything: the Pico serves exactly
        # one file and cannot answer a request for a sibling asset.
        self.assertNotIn('src="/assets', html)
        self.assertNotIn('href="/assets', html)

    def test_staged_page_gzip_carries_no_timestamp(self):
        # tools/build_web_ui.py pins mtime=0 so the committed artifact only
        # changes when the page changes. Recompressing here to compare bytes
        # would be wrong: deflate output differs between zlib builds, so the
        # header field we actually control is what gets asserted.
        header = STAGED.read_bytes()[:8]
        self.assertEqual(header[:2], b"\x1f\x8b")
        self.assertEqual(header[4:8], b"\0\0\0\0",
                         "gzip mtime must be zeroed; re-run tools/build_web_ui.py")

    def test_web_ui_module_no_longer_embeds_the_page(self):
        source = (ROOT / "pico" / "web_ui.py").read_text(encoding="utf-8")
        self.assertNotIn("PAGE = b", source,
                         "the page belongs on littlefs, not in the module heap")
        self.assertIn("INDEX_PATH", source)

    def test_deploy_script_ships_the_staged_page(self):
        script = (ROOT / "pico" / "deploy.ps1").read_text(encoding="utf-8")
        self.assertIn("www/index.html.gz", script,
                      "deploy.ps1 uploads pico/*.py only; without the asset the "
                      "device serves 503 after an update")


if __name__ == "__main__":
    unittest.main()
