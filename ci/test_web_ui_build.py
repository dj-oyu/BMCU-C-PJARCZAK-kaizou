"""The staged web UI artifact and its generated sources must not drift.

The page is built outside Python and committed, so nothing else would notice a
stale ``generated.ts`` or a ``pico/www/index.html.gz`` that no longer matches
the build. These checks run in the ordinary Python suite so a developer without
Node still finds out.
"""
import gzip
import hashlib
import importlib.util
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STAGED = ROOT / "pico" / "www" / "index.html.gz"
SCHEMA = ROOT / "pico" / "www" / "schema.json"
LAYOUT = ROOT / "docs" / "bmcu_wire_layout.json"
GENERATED = ROOT / "web" / "src" / "api" / "generated.ts"
LINK_REGISTRY = ROOT / "docs" / "bmcu_link_enum_registry.json"


def _load_enum_registry_generator():
    """A fresh module object per call: tests mutate its HEADER constant to
    point at a synthetic file, and a shared module instance would leak that
    across tests.
    """
    spec = importlib.util.spec_from_file_location(
        "generate_bmcu_enum_registry",
        ROOT / "tools" / "generate_bmcu_enum_registry.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

# A consumer fetches /api/schema.json once and caches it by revision, so the
# revision is the only thing telling it to refetch. Nothing else enforces the
# bump, and an unenforced version field rots. The pair below fingerprints the
# parts of the layout the policy covers -- structures and local_enums, not
# prose or the endpoint list. Changing either makes this test fail, and the
# only correct fix is to bump revision in docs/bmcu_wire_layout.json and
# record the new revision and digest here together.
LAYOUT_REVISION = 11
LAYOUT_DIGEST = \
    "ecdc280abdda71e638f84d6ced658da41bec4fb90affea11c042a6b6cbca507d"

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

    def test_bmcu_link_enum_registry_is_current(self):
        # docs/bmcu_link_enum_registry.json had no freshness check at all
        # until this test: generate_ts_registry.py and generate_api_schema.py
        # both trust it as a source, but nothing verified it still matched
        # src/bmcu_link_protocol.h. It had drifted -- FULL_RECORD_DM_KEY (14)
        # was missing -- and the drift was silent because generated.ts and
        # this registry were stale *together*, so test_generated_registry_is_
        # current above stayed green throughout. A generated artifact is only
        # as trustworthy as its freshness is enforced; this is that
        # enforcement for the registry itself, not just its downstream
        # consumers.
        #
        # The drift also cost real coverage once: ci/test_pico_monitor.py's
        # regression test for the DM_KEY snapshot-assembly bug (996eda5)
        # could not use this registry to enumerate FullStatusRecordType --
        # sourcing from it would have reproduced the exact blind spot that
        # let the bug through -- and read src/bmcu_link_protocol.h directly
        # instead.
        result = subprocess.run(
            [sys.executable,
             str(ROOT / "tools" / "generate_bmcu_enum_registry.py"),
             "--check"],
            capture_output=True, text=True)
        self.assertEqual(result.returncode, 0,
                         result.stderr or result.stdout)

    def test_staged_page_matches_the_web_sources(self):
        # tools/build_web_ui.py has had --check since it was written; nothing
        # called it. So a change under web/src/ could land with the committed
        # gz still holding the previous page, and every other check here would
        # pass: they assert the artifact is present, bounded and timestamp-free,
        # never that it is the current one. That happened -- 47725a4 changed the
        # STATUS decoder for the 27/31 rollout and left the staged page behind,
        # so the deployed bridge and the repository disagreed.
        #
        # This rebuilds through npm, so it skips where the toolchain is absent
        # rather than failing. The build script compares decompressed bytes,
        # not gzip bytes, because deflate output differs between zlib builds.
        #
        # The presence of `npm` is not the precondition -- every GitHub runner
        # ships one. `npm run build` needs the *installed* dependencies, and
        # without them vite is simply missing and the build exits 127, which
        # this test reported as a drifted artifact. That was the whole of the
        # host-tests CI failure from 2026-08-03 to 2026-08-08.
        if shutil.which("npm") is None and shutil.which("npm.cmd") is None:
            self.skipTest("npm is not installed")
        if not (ROOT / "web" / "node_modules").is_dir():
            self.skipTest("web dependencies are not installed; run npm ci")
        result = subprocess.run(
            [sys.executable, str(ROOT / "tools" / "build_web_ui.py"), "--check"],
            capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

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

        # link_record went unpinned until it grew build identity, and layout.ts
        # kept its old offsets without anything failing.
        link = {f["name"]: f["offset"]
                for f in layout["structures"]["link_record"]["fields"]}
        for name, offset in (
            ("StateOffset", link["link_state"]),
            ("ChannelsPresentOffset", link["channels_present"]),
            ("BootSessionOffset", link["bmcu_boot_session"]),
            ("TickHzOffset", link["tick_hz"]),
            ("SequenceGapOffset", link["sequence_gap_count"]),
            ("VariantFlagsOffset", link["variant_flags"]),
            ("BuildHashOffset", link["build_hash"]),
        ):
            self.assertIn("%s: %d," % (name, offset), source)

    def test_deploy_script_ships_the_staged_page(self):
        script = (ROOT / "pico" / "deploy.ps1").read_text(encoding="utf-8")
        self.assertIn("www/index.html.gz", script,
                      "deploy.ps1 uploads pico/*.py only; without the asset the "
                      "device serves 503 after an update")


class BmcuEnumRegistryGeneratorTests(unittest.TestCase):
    """Exercises the generator itself, not just its committed output.

    test_bmcu_link_enum_registry_is_current above only proves the *checked
    in* file matches what the generator produces from today's header; it
    says nothing about whether the generator can survive tomorrow's header.
    """

    def registry_from(self, header_text):
        generator = _load_enum_registry_generator()
        with tempfile.TemporaryDirectory() as directory:
            header_path = Path(directory) / "bmcu_link_protocol.h"
            header_path.write_text(header_text, encoding="utf-8")
            generator.HEADER = header_path
            return generator.registry()

    HEADER_PREAMBLE = (
        "constexpr uint8_t VERSION_PRERELEASE = 0;\n"
        "constexpr uint8_t VERSION_REVISION = 3;\n"
        "constexpr uint8_t VERSION = "
        "(VERSION_PRERELEASE << 4) | VERSION_REVISION;\n")

    def test_strip_line_comments_removes_only_the_comment_text(self):
        stripped = _load_enum_registry_generator().strip_line_comments(
            "FULL_RECORD_GLOBAL = 1u, // note, with a comma in it\n"
            "FULL_RECORD_CHANNEL = 2u,")
        self.assertEqual(
            stripped,
            "FULL_RECORD_GLOBAL = 1u, \nFULL_RECORD_CHANNEL = 2u,")

    def test_an_explanatory_comment_full_of_commas_does_not_break_parsing(
            self):
        # Regression for the bug that shipped alongside FULL_RECORD_DM_KEY
        # (df4727a/88b94c2): a multi-line prose comment ahead of a real enum
        # member, itself containing several commas, used to make the
        # generator's naive `body.split(",")` hand a comment fragment to the
        # `name = expression` parser and raise ValueError -- not "produce a
        # stale registry", but "cannot produce a registry at all". This is
        # the shape that broke it, reproduced deliberately: a comment with
        # commas, spanning several lines, sitting directly before the member
        # it explains.
        header = self.HEADER_PREAMBLE + (
            "enum FullStatusRecordType : uint8_t\n"
            "{\n"
            "    FULL_RECORD_GLOBAL = 1u, FULL_RECORD_CHANNEL = 2u,\n"
            "    // TEMPORARY, with a comma, and another one here too,\n"
            "    // spanning several lines, each carrying commas, of its\n"
            "    // own, describing the member that follows.\n"
            "    FULL_RECORD_DM_KEY = 14u,\n"
            "};\n")

        result = self.registry_from(header)

        self.assertEqual(
            result["enums"]["full_status_record_type"],
            {"1": "global", "2": "channel", "14": "dm_key"})

    def test_a_single_line_trailing_comment_is_also_stripped(self):
        header = self.HEADER_PREAMBLE + (
            "enum FullStatusRecordType : uint8_t\n"
            "{\n"
            "    FULL_RECORD_GLOBAL = 1u, // ordinary trailing note\n"
            "    FULL_RECORD_CHANNEL = 2u,\n"
            "};\n")

        result = self.registry_from(header)

        self.assertEqual(
            result["enums"]["full_status_record_type"],
            {"1": "global", "2": "channel"})


if __name__ == "__main__":
    unittest.main()
