"""Build the browser UI and stage it as the gzip the Pico streams off littlefs.

The Pico serves ``pico/www/index.html.gz`` verbatim with
``Content-Encoding: gzip``; it has neither the RAM nor the cycles to compress at
request time. The artifact is committed because deployment is WebREPL from a
developer machine, so the gzip is written deterministically (fixed mtime, no
embedded filename) and identical inputs produce byte-identical output.
"""
import argparse
import gzip
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"
BUILT = WEB / "dist" / "index.html"
STAGED = ROOT / "pico" / "www" / "index.html.gz"


def compress(source_bytes):
    # mtime=0 keeps the artifact reproducible: without it every build produces a
    # different byte stream and the committed file churns on every commit.
    return gzip.compress(source_bytes, compresslevel=9, mtime=0)


def npm_build():
    npm = shutil.which("npm") or shutil.which("npm.cmd")
    if npm is None:
        raise SystemExit("npm not found; run 'npm run build' in web/ first")
    subprocess.run([npm, "run", "build"], cwd=WEB, check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-npm", action="store_true",
                        help="reuse web/dist/index.html instead of rebuilding")
    parser.add_argument("--check", action="store_true",
                        help="fail instead of writing when the artifact drifts")
    arguments = parser.parse_args()

    if not arguments.skip_npm:
        npm_build()
    if not BUILT.exists():
        raise SystemExit("%s is missing; run the web build first"
                         % BUILT.relative_to(ROOT))

    source_bytes = BUILT.read_bytes()
    if arguments.check:
        # Compare the decompressed page, not the gzip bytes: deflate output
        # differs between zlib builds (several distros ship zlib-ng), so a byte
        # comparison would fail on CI for a page that is actually identical.
        try:
            staged_bytes = gzip.decompress(STAGED.read_bytes())
        except (OSError, gzip.BadGzipFile):
            staged_bytes = b""
        if staged_bytes != source_bytes:
            raise SystemExit(
                "%s is stale; run tools/build_web_ui.py"
                % STAGED.relative_to(ROOT))
        return
    compressed = compress(source_bytes)

    STAGED.parent.mkdir(parents=True, exist_ok=True)
    STAGED.write_bytes(compressed)
    print("%s: %d bytes gzip (from %d bytes html)"
          % (STAGED.relative_to(ROOT), len(compressed),
             BUILT.stat().st_size), file=sys.stderr)


if __name__ == "__main__":
    main()
