# Pico binary output

The local diagnostic API returns concatenated BMB1 messages with content type
`application/vnd.bmcu-monitor.v1`, except for `/api/schema.json` itself, which is JSON.

The authoritative endpoint list — path, cadence, body shape, and which structures decode
it — is the `endpoints` array in `docs/bmcu_wire_layout.json`, the same file the served
`/api/schema.json` is generated from. Fetch `/api/schema.json` first; do not hand-copy the
endpoint list here.

`/api/history/status.bin` is not a distinct endpoint: `pico/binary_api.py` dispatches it
through the same branch as `/api/current.bin` to the same retained STATUS data. It exists
for existing callers; prefer `/api/current.bin` in new code.

The static browser UI decodes these messages with `ArrayBuffer` and
`DataView`. The Pico does not construct a live JSON device tree, device log,
or full-state response.
