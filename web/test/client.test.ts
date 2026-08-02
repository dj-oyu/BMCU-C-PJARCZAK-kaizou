import { afterEach, describe, expect, it, vi } from 'vitest'
import { api } from '../src/api/client'

function respondWith(body: string, ok = true) {
  vi.stubGlobal(
    'fetch',
    vi.fn(async () => ({
      ok,
      status: ok ? 200 : 500,
      text: async () => body,
    })),
  )
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('key=value status endpoints', () => {
  it('parses the line format the device writes', async () => {
    respondWith('configured=1\nhost=bambuddy.local\nport=8766\n')
    const fields = await api.transportStatus()
    expect(fields.get('configured')).toBe('1')
    expect(fields.get('host')).toBe('bambuddy.local')
    expect(fields.get('port')).toBe('8766')
  })

  it('tolerates CRLF, which otherwise reads as unconfigured', async () => {
    respondWith('configured=1\r\nhost=bambuddy.local\r\nport=8766\r\n')
    const fields = await api.transportStatus()
    expect(fields.get('configured')).toBe('1')
    expect(fields.get('host')).toBe('bambuddy.local')
  })

  it('keeps values containing an equals sign intact', async () => {
    respondWith('fingerprint=3f9a=c1d0\n')
    expect((await api.deviceKeyStatus()).get('fingerprint')).toBe('3f9a=c1d0')
  })

  it('raises on a non-OK response instead of reporting empty fields', async () => {
    respondWith('', false)
    await expect(api.transportStatus()).rejects.toThrow('HTTP 500')
  })
})
