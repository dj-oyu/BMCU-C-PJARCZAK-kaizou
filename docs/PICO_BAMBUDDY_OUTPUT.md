# Pico binary output

The local diagnostic API returns concatenated BMB1 messages with content type
`application/vnd.bmcu-monitor.v1`.

- `GET /api/current.bin`
- `GET /api/events.bin?after=<sequence>&limit=<n>`
- `GET /api/history/status.bin`
- `GET /api/diagnostics.bin`
- `GET /api/logs.bin?after=<sequence>&limit=<n>`

The static browser UI decodes these messages with `ArrayBuffer` and
`DataView`. The Pico does not construct a live JSON device tree, device log,
or full-state response.
