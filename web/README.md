# BMCU monitor web UI

TypeScript + Preact source for the page the Pico serves at `/`. Vite bundles it
into one self-contained HTML file, which `tools/build_web_ui.py` gzips into
`pico/www/index.html.gz` for `pico/web_ui.py` to stream off littlefs.

## Why it is built rather than written inline

The page used to be a 14 KB `bytes` literal in `pico/web_ui.py`. That cost a
permanent heap allocation on a device whose own UI warns below 20 KB free, made
every UI test a substring match against a blob, and left every wire offset and
TLV tag hand-copied out of `pico/bmcu_binary_constants.py`.

## Working on it

```bash
npm install
npm run dev      # http://localhost:5173, no Pico required
npm run check    # tsc --noEmit
npm test         # decoders vs. the Python-generated fixtures
```

`npm run dev` serves the same endpoints the Pico serves from
`src/mock/fixtures/`, which `tools/generate_web_mock_fixtures.py` encodes with
the real `pico/` codec. Regenerate them after changing an encoder:

```bash
python tools/generate_web_mock_fixtures.py
```

## Wire constants

`src/api/generated.ts` is generated and must not be edited:

```bash
python tools/generate_ts_registry.py          # write
python tools/generate_ts_registry.py --check  # CI freshness gate
```

It covers everything `docs/bmcu_binary_registry.json` and
`docs/bmcu_link_enum_registry.json` describe. Byte offsets are not in either
registry, so they live in `src/api/layout.ts` and are pinned instead by
`test/decode.test.ts`, which decodes the fixtures in
`tests/fixtures/bmcu_binary/` that the Python codec produced. A struct format
change in `pico/bmcu_binary.py` therefore fails a test rather than silently
shifting a field.

## Shipping a change

```bash
python tools/build_web_ui.py    # runs the Vite build, then stages the gzip
```

Commit both the source change and the regenerated `pico/www/index.html.gz`;
`ci/test_web_ui_build.py` and the `web-ui` CI job fail if they disagree. The
artifact is committed because deployment is WebREPL from a developer machine,
not from CI.

Uploading: `pico/deploy.ps1` sends the asset before the modules. Over WebREPL,
the file has to be pushed into `www/` explicitly — see
`docs/PICO_USER_GUIDE.md`.
