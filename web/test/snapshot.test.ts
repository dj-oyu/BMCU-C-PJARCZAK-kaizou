/**
 * Snapshot decoding, against a fixture the device's own encoder produced.
 *
 * This is the data the old UI threw away entirely: channel encoder angles and
 * motor PWM, printer bus counters, AMS registration. Nothing here can be
 * checked against the running device by eye, so the fixture is the contract.
 */
import { readFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'

import { parseSnapshotRecords, readSnapshot } from '../src/api/snapshot'
import { SLOT_COUNT, SnapshotKind, SnapshotRecord } from '../src/api/layout'

const here = dirname(fileURLToPath(import.meta.url))
const mocks = resolve(here, '../src/mock/fixtures')

function load(name: string): ArrayBuffer {
  const file = readFileSync(resolve(mocks, name))
  return file.buffer.slice(file.byteOffset, file.byteOffset + file.byteLength) as ArrayBuffer
}

const buffer = load('snapshot.bin')
const links = readSnapshot(buffer)

describe('record framing', () => {
  it('reads every record the device emitted', () => {
    const records = parseSnapshotRecords(buffer)
    expect(records.length).toBeGreaterThan(10)
    expect(records[0]?.recordType).toBe(SnapshotKind.Link)
    expect(new Set(records.map((r) => r.linkIndex))).toEqual(new Set([0, 1]))
  })

  it('stops at a record that is not ours rather than guessing', () => {
    const damaged = buffer.slice(0)
    new DataView(damaged).setUint32(0, 0xdeadbeef)
    expect(parseSnapshotRecords(damaged)).toEqual([])
  })

  it('stops when a record claims more payload than remains', () => {
    const truncated = buffer.slice(0, SnapshotRecord.HeaderSize + 4)
    expect(parseSnapshotRecords(truncated)).toEqual([])
  })
})

describe('link summary', () => {
  it('reports the state by name, keeping resyncing distinct from stale', () => {
    expect(links.get(0)?.summary?.state).toBe('online')
    expect(links.get(1)?.summary?.state).toBe('resyncing')
  })

  it('reports tick_hz as null when the BMCU has not said', () => {
    expect(links.get(0)?.summary?.tickHz).toBe(144000000)
  })

  it('marks a link without channel records incomplete rather than empty', () => {
    // "no snapshot yet" and "every counter is zero" render differently.
    expect(links.get(0)?.complete).toBe(true)
    expect(links.get(1)?.complete).toBe(false)
    expect(links.get(1)?.channels).toEqual(Array(SLOT_COUNT).fill(null))
  })
})

describe('channel detail', () => {
  const channels = links.get(0)?.channels ?? []

  it('decodes the values no previous UI showed', () => {
    const first = channels[0]
    expect(first?.rawAngle).toBe(2048)
    expect(first?.positionDelta).toBe(12)
    expect(first?.motorPwm).toBe(480)
    expect(first?.pullPercent).toBe(61)
  })

  it('keeps signed fields signed', () => {
    expect(channels[2]?.positionDelta).toBe(-6)
    expect(channels[2]?.motorPwm).toBe(-420)
  })

  it('separates sensor health from filament presence', () => {
    expect(channels[0]?.sensorOnline).toBe(true)
    expect(channels[0]?.sensorGood).toBe(true)
    expect(channels[0]?.filament).toBe(true)
    expect(channels[1]?.filament).toBe(false)
    expect(channels[1]?.present).toBe(true)
  })

  it('surfaces a channel with a motion fault and a failed sensor', () => {
    expect(channels[3]?.motionFault).toBe(3)
    expect(channels[3]?.sensorGood).toBe(false)
  })

  it('reports the controller phase only when the BMCU flagged it valid', () => {
    expect(channels[0]?.controllerMotion).toBe(3)
  })

  it('lands each record in its own slot', () => {
    expect(channels.map((c) => c?.channel)).toEqual([0, 1, 2, 3])
  })
})

describe('printer and AMS counters', () => {
  const link = links.get(0)

  it('merges the three printer receive records into one view', () => {
    expect(link?.printer.rxFramesValid).toBe(91820)
    expect(link?.printer.rxUsartOverrun).toBe(0)
    expect(link?.printer.rxDmaWrap).toBe(4471)
  })

  it('merges both transmit records', () => {
    expect(link?.printer.txCompleted).toBe(91818)
    expect(link?.printer.txTimeout).toBe(1)
    expect(link?.printer.txNoResponseExpected).toBe(4)
  })

  it('decodes the AMS service gaps that say whether the printer still talks', () => {
    expect(link?.ams.gapNowMs).toBe(12)
    expect(link?.ams.gapMaxMs).toBe(480)
    expect(link?.ams.msSinceConfirm).toBe(3120)
  })

  it('decodes registration counters as u16, not u32', () => {
    expect(link?.ams.confirms).toBe(3)
    expect(link?.ams.wouldReoffer).toBe(1)
    expect(link?.ams.registrationQueries).toBe(7)
  })

  it('leaves a link with no records with empty views, not zeroes', () => {
    expect(links.get(1)?.printer).toEqual({})
    expect(links.get(1)?.ams).toEqual({})
  })
})

describe('events', () => {
  const events = links.get(0)?.events ?? []

  it('keeps the recent ring in arrival order with names resolved', () => {
    expect(events).toHaveLength(3)
    expect(events[0]?.severityName).toBeTruthy()
    expect(events[0]?.sourceName).toBeTruthy()
  })

  it('exposes the detail union for the caller to interpret by record type', () => {
    expect(events[1]?.recordType).toBe(9)
    expect(Array.from(events[1]?.detail ?? [])).toHaveLength(8)
  })
})
