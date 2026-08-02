/**
 * Renders the presentational components against the mock telemetry.
 *
 * The old UI could only be tested by grepping a string literal for markup, so
 * a wrong label was indistinguishable from a right one. These assertions are
 * about meaning: which mask drives which row.
 */
import { readFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import renderToString from 'preact-render-to-string'
import { describe, expect, it } from 'vitest'

import { linkCounters, parseMessages, readTelemetry } from '../src/api/decode'
import { BridgeHealth } from '../src/components/BridgeHealth'
import { LoaderCard } from '../src/components/LoaderCard'

const here = dirname(fileURLToPath(import.meta.url))
const mocks = resolve(here, '../src/mock/fixtures')

function load(name: string): ArrayBuffer {
  const file = readFileSync(resolve(mocks, name))
  return file.buffer.slice(file.byteOffset, file.byteOffset + file.byteLength) as ArrayBuffer
}

const telemetry = readTelemetry(
  parseMessages(load('current.bin')),
  parseMessages(load('diagnostics.bin')),
)

function loader(link: number) {
  return renderToString(
    <LoaderCard
      link={link}
      status={telemetry.statuses.get(link) ?? null}
      counters={linkCounters(link, telemetry.diagnostics)}
    />,
  )
}

describe('LoaderCard', () => {
  it('labels the two masks distinctly', () => {
    const html = loader(0)
    expect(html).toContain('<dt>Channel</dt>')
    expect(html).toContain('<dt>Filament</dt>')
  })

  it('reports an empty but plugged-in channel row correctly', () => {
    // The regression that started this: every channel present, no filament.
    // The old UI rendered "Filament: Present" for exactly this state.
    const html = loader(1)
    expect(html).toContain('Present')
    expect(html).toContain('Empty')
    expect(html).not.toContain('Loaded')
  })

  it('shows filament only where the microswitch reports it', () => {
    const html = loader(0)
    expect((html.match(/Loaded/g) ?? []).length).toBe(2)
    expect((html.match(/Empty/g) ?? []).length).toBe(2)
  })

  it('marks the selected slot and names the UART pins', () => {
    const html = loader(0)
    expect(html).toContain('Selected')
    expect(html).toContain('GP0 TX / GP1 RX')
    expect(html).toContain('bmcu-a')
  })

  it('flags a link with UART errors and leaves a clean link unflagged', () => {
    expect(loader(1)).toContain('metric warn')
    expect(loader(0)).toContain('metric ok')
  })

  it('falls back to a placeholder when no STATUS has arrived', () => {
    const html = renderToString(
      <LoaderCard link={0} status={null} counters={linkCounters(0, new Map())} />,
    )
    expect(html).toContain('No STATUS received on UART0')
    expect(html).toContain('Waiting')
  })
})

describe('BridgeHealth', () => {
  it('renders exception count as bad when the Pico is raising', () => {
    const html = renderToString(<BridgeHealth diagnostics={telemetry.diagnostics} />)
    expect(html).toContain('1,483')
    expect(html).toContain('metric bad')
    expect(html).toContain('-57 dBm')
  })

  it('shows a dash rather than a bogus RSSI when Wi-Fi is not reporting', () => {
    const html = renderToString(<BridgeHealth diagnostics={new Map()} />)
    expect(html).toContain('--')
    expect(html).toContain('metric ok')
  })
})
