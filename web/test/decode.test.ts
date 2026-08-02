/**
 * Pins the TypeScript decoders against fixtures the Python codec produced.
 *
 * ``layout.ts`` cannot be generated from any registry, so this is what keeps it
 * honest: if a struct format string in ``pico/bmcu_binary.py`` changes, the
 * regenerated fixtures stop matching these expectations.
 */
import { readFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'

import {
  activeLinks,
  counter,
  linkCounters,
  parseMessages,
  parseStatus,
  parseTlvs,
  readLogRecords,
  readTelemetry,
} from '../src/api/decode'
import { Diag, LogSeverity, MessageType } from '../src/api/generated'
import { SLOT_COUNT } from '../src/api/layout'

const here = dirname(fileURLToPath(import.meta.url))
const fixtures = resolve(here, '../../tests/fixtures/bmcu_binary')
const mocks = resolve(here, '../src/mock/fixtures')

function load(directory: string, name: string): ArrayBuffer {
  const file = readFileSync(resolve(directory, name))
  return file.buffer.slice(file.byteOffset, file.byteOffset + file.byteLength) as ArrayBuffer
}

describe('parseMessages', () => {
  it('reads the BMB1 envelope of a single fixture message', () => {
    const messages = parseMessages(load(fixtures, 'bmcu_status.bin'))
    expect(messages).toHaveLength(1)
    expect(messages[0]?.messageType).toBe(MessageType.BmcuFrame)
    expect(messages[0]?.linkIndex).toBe(0)
  })

  it('splits a concatenated response into both messages', () => {
    const messages = parseMessages(load(fixtures, 'concatenated.bin'))
    expect(messages.map((message) => message.messageType)).toEqual([
      MessageType.LinkState,
      MessageType.Ack,
    ])
  })

  it('stops at a truncated header instead of throwing', () => {
    expect(parseMessages(load(fixtures, 'truncated_header.bin'))).toEqual([])
  })
})

describe('parseStatus', () => {
  it('decodes the fixture STATUS payload of bytes 0..26', () => {
    // ci/generate_bmcu_binary_fixtures.py fills the STATUS payload with
    // range(27), so every field decodes to its own offset. That makes each
    // assertion below a direct check of the offset in layout.ts.
    const [message] = parseMessages(load(fixtures, 'bmcu_status.bin'))
    const status = parseStatus(message!.payload)
    expect(status).not.toBeNull()
    expect(status!.selectedSlot).toBeNull() // payload[12] === 12, past slot 3
    expect(status!.channelPresent).toEqual([true, false, true, true]) // 13 = 0b1101
    expect(status!.filamentLoaded).toEqual([false, true, true, true]) // 14 = 0b1110
    expect(status!.motion).toEqual([15, 16, 17, 18])
    expect(status!.pullPercent).toEqual([19, 20, 21, 22])
  })

  it('rejects a BMCU frame that is not a STATUS kind', () => {
    const [message] = parseMessages(load(fixtures, 'bmcu_event.bin'))
    expect(parseStatus(message!.payload)).toBeNull()
  })

  it('rejects a frame too short to hold a STATUS payload', () => {
    const [message] = parseMessages(load(fixtures, 'bmcu_unknown.bin'))
    expect(parseStatus(message!.payload)).toBeNull()
  })
})

describe('parseTlvs', () => {
  it('keeps unknown tags as raw bytes rather than dropping them', () => {
    const [message] = parseMessages(load(fixtures, 'diagnostic_unknown_tags.bin'))
    const values = parseTlvs(message!.payload)
    expect(values.get(240)).toBe(1)
    expect(values.has(241)).toBe(true)
  })
})

describe('log records', () => {
  it('decodes component and UTF-8 message text', () => {
    const records = readLogRecords(parseMessages(load(fixtures, 'pico_log_utf8.bin')))
    expect(records).toHaveLength(1)
    expect(records[0]?.component).toBe('uart')
    expect(records[0]?.message).toBe('通信警告')
    expect(records[0]?.severity).toBe(LogSeverity.Warning)
  })

  it('decodes a maximum-length record without truncating', () => {
    const records = readLogRecords(parseMessages(load(fixtures, 'pico_log_max.bin')))
    expect(records[0]?.component).toHaveLength(40)
    expect(records[0]?.message).toHaveLength(320)
  })
})

describe('mock telemetry', () => {
  const telemetry = readTelemetry(
    parseMessages(load(mocks, 'current.bin')),
    parseMessages(load(mocks, 'diagnostics.bin')),
  )

  it('reads the diagnostic counters the bridge cards render', () => {
    expect(counter(telemetry.diagnostics, Diag.ExceptionCount)).toBe(1483)
    expect(counter(telemetry.diagnostics, Diag.UptimeMs)).toBe(86_400_000)
    expect(counter(telemetry.diagnostics, Diag.WifiRssiDbm)).toBe(-57)
  })

  it('addresses per-link UART counters by stride, not a second tag list', () => {
    expect(linkCounters(0, telemetry.diagnostics).crcErrors).toBe(0)
    const second = linkCounters(1, telemetry.diagnostics)
    expect(second.crcErrors).toBe(314)
    expect(second.frameErrors).toBe(271)
    expect(second.sequenceGaps).toBe(12)
    expect(second.overflows).toBe(3)
  })

  it('separates channel presence from filament detection', () => {
    // The mock's second loader has all four channels plugged in and no
    // filament, which the old UI rendered as "Filament: Present".
    const empty = telemetry.statuses.get(1)
    expect(empty?.channelPresent).toEqual(Array(SLOT_COUNT).fill(true))
    expect(empty?.filamentLoaded).toEqual(Array(SLOT_COUNT).fill(false))

    const loaded = telemetry.statuses.get(0)
    expect(loaded?.filamentLoaded).toEqual([true, false, true, false])
    expect(loaded?.selectedSlot).toBe(0)
  })

  it('lists both links when both report telemetry', () => {
    expect(activeLinks(telemetry)).toEqual([0, 1])
  })

  it('falls back to every configured link when nothing has reported', () => {
    expect(activeLinks({ diagnostics: new Map(), statuses: new Map() })).toEqual([0, 1])
  })
})
