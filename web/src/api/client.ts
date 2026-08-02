/** Fetch wrappers for the Pico's HTTP surface. */
import { type BmbMessage, parseMessages } from './decode'

/**
 * The Pico serves one HTTP client at a time: web_ui.py holds a single
 * `self.client` and only accepts the next connection after the current
 * response is written, with a 3 s deadline on the one in progress. Requests
 * issued in parallel therefore strand each other, so every call goes through
 * one chain. This is not an optimisation choice — it is the server's contract.
 */
let pending: Promise<unknown> = Promise.resolve()

function serialize<T>(work: () => Promise<T>): Promise<T> {
  const result = pending.then(work, work)
  // Keep the chain alive after a rejection, or one failed poll would wedge
  // every later request.
  pending = result.catch(() => undefined)
  return result
}

async function fetchBinary(path: string): Promise<BmbMessage[]> {
  return serialize(async () => {
    const response = await fetch(path, { cache: 'no-store' })
    if (!response.ok) throw new Error(`HTTP ${response.status}`)
    return parseMessages(await response.arrayBuffer())
  })
}

/** The status and settings endpoints answer with ``key=value`` lines. */
async function fetchFields(path: string): Promise<Map<string, string>> {
  return serialize(async () => parseFields(await requestText(path)))
}

async function requestText(path: string): Promise<string> {
  const response = await fetch(path, { cache: 'no-store' })
  if (!response.ok) throw new Error(`HTTP ${response.status}`)
  return response.text()
}

function parseFields(body: string): Map<string, string> {
  const fields = new Map<string, string>()
  for (const raw of body.trim().split('\n')) {
    // A trailing CR is legal in an HTTP text body; without trimming it, a
    // "configured=1\r" reads as unconfigured.
    const line = raw.trim()
    const separator = line.indexOf('=')
    if (separator > 0) {
      fields.set(line.slice(0, separator), line.slice(separator + 1))
    }
  }
  return fields
}

async function post(
  path: string,
  header: [string, string],
  body: string,
): Promise<void> {
  return serialize(async () => {
    const response = await fetch(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/octet-stream', [header[0]]: header[1] },
      body,
    })
    if (!response.ok) {
      throw new Error((await response.text()).trim() || `HTTP ${response.status}`)
    }
  })
}

export const api = {
  current: () => fetchBinary('/api/current.bin'),
  diagnostics: () => fetchBinary('/api/diagnostics.bin'),
  logs: (after: bigint, limit: number) =>
    fetchBinary(`/api/logs.bin?after=${after.toString()}&limit=${limit}`),
  transportStatus: () => fetchFields('/api/transport/status'),
  deviceKeyStatus: () => fetchFields('/api/device-key/status'),
  saveTransport: (host: string, port: number) =>
    post('/api/transport', ['X-BMCU-Settings-Action', 'update'], `${host}\n${port}`),
  saveDeviceKey: (key: string) =>
    post('/api/device-key', ['X-BMCU-Key-Action', 'update'], key),
}
