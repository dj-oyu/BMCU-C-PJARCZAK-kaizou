"""The staged web UI artifact and its generated sources must not drift.

The page is built outside Python and committed, so nothing else would notice a
stale ``generated.ts`` or a ``pico/www/index.html.gz`` that no longer matches
the build. These checks run in the ordinary Python suite so a developer without
Node still finds out.
"""
import gzip
import hashlib
import json
import re
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STAGED = ROOT / "pico" / "www" / "index.html.gz"
SCHEMA = ROOT / "pico" / "www" / "schema.json"
LAYOUT = ROOT / "docs" / "bmcu_wire_layout.json"
GENERATED = ROOT / "web" / "src" / "api" / "generated.ts"

# A consumer fetches /api/schema.json once and caches it by revision, so the
# revision is the only thing telling it to refetch. Nothing else enforces the
# bump, and an unenforced version field rots. The pair below fingerprints the
# parts of the layout the policy covers -- structures and local_enums, not
# prose or the endpoint list. Changing either makes this test fail, and the
# only correct fix is to bump revision in docs/bmcu_wire_layout.json and
# record the new revision and digest here together.
LAYOUT_REVISION = 5
LAYOUT_DIGEST = \
    "48d364384197c03f239fba87de3a01e11f1374a44177f744eaca933bc2d1ca71"

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

    def test_api_schema_is_current(self):
        result = subprocess.run(
            [sys.executable, str(ROOT / "tools" / "generate_api_schema.py"),
             "--check"],
            capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

    def test_api_schema_describes_every_served_endpoint(self):
        # A reader that fetches the schema must not then meet an endpoint the
        # schema never mentioned.
        schema = json.loads(SCHEMA.read_bytes())
        described = {entry["path"] for entry in schema["endpoints"]}
        source = (ROOT / "pico" / "binary_api.py").read_text(encoding="utf-8")
        served = set(re.findall(r'"(/api/[a-z/]+\.bin)"', source))
        self.assertTrue(served, "no endpoints found in binary_api.py")
        self.assertEqual(served - described, set())

    def test_layout_revision_moves_when_the_layout_does(self):
        layout = json.loads(LAYOUT.read_bytes())
        covered = json.dumps(
            {"structures": layout["structures"],
             "local_enums": layout.get("local_enums", {})},
            sort_keys=True, ensure_ascii=False).encode("utf-8")
        digest = hashlib.sha256(covered).hexdigest()
        self.assertEqual(
            (layout["revision"], digest), (LAYOUT_REVISION, LAYOUT_DIGEST),
            "docs/bmcu_wire_layout.json structures or enums changed: bump its "
            "revision and update LAYOUT_REVISION and LAYOUT_DIGEST above")
        self.assertEqual(json.loads(SCHEMA.read_bytes())["revision"],
                         layout["revision"])

    def test_api_schema_resolves_the_offsets_a_reader_needs(self):
        schema = json.loads(SCHEMA.read_bytes())
        structures = schema["structures"]
        prefix = structures["bmcu_frame_prefix"]["size"]
        header = structures["link_wire"]["header_size"]
        fields = {f["name"]: f for f in structures["status_payload"]["fields"]}
        # 10 + 7 + 13 = 30, the absolute offset of the mask inside a BMB1
        # payload. Getting this sum wrong is the mistake the schema exists for.
        self.assertEqual(prefix + header + fields["inserted_mask"]["offset"], 30)
        self.assertIn("not filament", fields["inserted_mask"]["note"].lower())
        self.assertIn("filament", fields["online_mask"]["note"].lower())

    def test_api_schema_resolves_enum_names_for_every_referenced_group(self):
        schema = json.loads(SCHEMA.read_bytes())
        for structure in schema["structures"].values():
            for field in structure["fields"]:
                group = field.get("enum")
                if group:
                    self.assertIn(group, schema["enums"], field["name"])
        self.assertEqual(schema["enums"]["link_states"]["1"], "resyncing")
        self.assertEqual(schema["enums"]["kind"]["2"], "status")

    def test_layout_source_and_the_typescript_copy_agree(self):
        # layout.ts is hand-written and fixture-pinned; the JSON is the source
        # the device schema is built from. They must not drift apart.
        layout = json.loads(
            (ROOT / "docs" / "bmcu_wire_layout.json").read_text(encoding="utf-8"))
        source = (ROOT / "web" / "src" / "api" / "layout.ts").read_text(
            encoding="utf-8")
        status = {f["name"]: f["offset"]
                  for f in layout["structures"]["status_payload"]["fields"]}
        for name, offset in (
            ("CurrentSlotOffset", status["current_slot"]),
            ("InsertedMaskOffset", status["inserted_mask"]),
            ("OnlineMaskOffset", status["online_mask"]),
            ("MotionOffset", status["motion"]),
            ("PullPercentOffset", status["pull_pct"]),
        ):
            self.assertIn("%s: %d," % (name, offset), source)

    def test_deploy_script_ships_the_staged_page(self):
        script = (ROOT / "pico" / "deploy.ps1").read_text(encoding="utf-8")
        self.assertIn("www/index.html.gz", script,
                      "deploy.ps1 uploads pico/*.py only; without the asset the "
                      "device serves 503 after an update")


if __name__ == "__main__":
    unittest.main()
