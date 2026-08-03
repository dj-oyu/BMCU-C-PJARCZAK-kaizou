/**
 * The Pico's HTTP server holds one client at a time (pico/web_ui.py), so the
 * UI must never have two requests outstanding. This is the regression that
 * showed up only against real hardware: on the mock dev server, parallel
 * requests worked fine and every other test passed.
 */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { api } from '../src/api/client'

afterEach(() => {
  vi.unstubAllGlobals()
})

function trackingFetch() {
  let inFlight = 0
  let maxInFlight = 0
  const fetchMock = vi.fn(async () => {
    inFlight += 1
    maxInFlight = Math.max(maxInFlight, inFlight)
    await new Promise((resolve) => setTimeout(resolve, 5))
    inFlight -= 1
    return {
      ok: true,
      status: 200,
      text: async () => 'configured=1\n',
      arrayBuffer: async () => new ArrayBuffer(0),
    }
  })
  vi.stubGlobal('fetch', fetchMock)
  return { peak: () => maxInFlight, calls: () => fetchMock.mock.calls.length }
}

describe('request serialisation', () => {
  it('never has two requests outstanding', async () => {
    const tracker = trackingFetch()
    await Promise.all([
      api.current(),
      api.diagnostics(),
      api.transportStatus(),
      api.deviceKeyStatus(),
      api.logs(0n, 24),
    ])
    expect(tracker.calls()).toBe(5)
    expect(tracker.peak()).toBe(1)
  })

  it('keeps serving requests after one fails', async () => {
    let attempt = 0
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => {
        attempt += 1
        if (attempt === 1) throw new Error('connection refused')
        return { ok: true, status: 200, text: async () => 'configured=1\n' }
      }),
    )
    await expect(api.transportStatus()).rejects.toThrow('connection refused')
    await expect(api.deviceKeyStatus()).resolves.toBeInstanceOf(Map)
  })
})
