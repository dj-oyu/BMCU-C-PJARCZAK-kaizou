/**
 * Decoders for /api/snapshot.bin.
 *
 * The device hands back the FULL_STATUS payloads exactly as the BMCU sent them,
 * so the layout knowledge lives here rather than being duplicated on the Pico.
 * Everything inside a record body is little-endian, like the rest of the BMCU
 * link protocol; the record header around it is big-endian, like the transport.
 */
import { BmcuSeverity, BmcuSource, LinkState } from './generated'
import {
  ChannelRecord,
  EventPayload,
  LinkRecord,
  SLOT_COUNT,
  SnapshotKind,
  SnapshotRecord,
} from './layout'

export interface RawSnapshotRecord {
  readonly linkIndex: number
  readonly recordType: number
  readonly recordIndex: number
  readonly hwTick32: number
  readonly body: DataView
}

/** Splits the response, stopping at the first record that is not ours. */
export function parseSnapshotRecords(buffer: ArrayBuffer): RawSnapshotRecord[] {
  const view = new DataView(buffer)
  const records: RawSnapshotRecord[] = []
  let offset = 0
  while (offset + SnapshotRecord.HeaderSize <= buffer.byteLength) {
    if (view.getUint32(offset) !== SnapshotRecord.Magic) break
    if (view.getUint8(offset + SnapshotRecord.VersionOffset) !== SnapshotRecord.Version) {
      break
    }
    const length = view.getUint16(offset + SnapshotRecord.PayloadLengthOffset)
    const start = offset + SnapshotRecord.HeaderSize
    if (start + length > buffer.byteLength) break
    records.push({
      linkIndex: view.getUint8(offset + SnapshotRecord.LinkIndexOffset),
      recordType: view.getUint8(offset + SnapshotRecord.RecordTypeOffset),
      recordIndex: view.getUint8(offset + SnapshotRecord.RecordIndexOffset),
      hwTick32: view.getUint32(offset + SnapshotRecord.HwTickOffset),
      body: new DataView(buffer, start, length),
    })
    offset = start + length
  }
  return records
}

export type LinkStateName =
  | 'unknown'
  | 'resyncing'
  | 'online'
  | 'stale'
  | 'offline'
  | 'incompatible'

const STATE_NAMES = new Map<number, LinkStateName>(
  Object.entries(LinkState).map(([name, value]) => [
    value,
    name.toLowerCase() as LinkStateName,
  ]),
)

export interface LinkSummary {
  readonly state: LinkStateName
  readonly channelsPresent: number
  readonly bootSession: number
  /** Null until the BMCU has sent HELLO; the device reports 0 for that. */
  readonly tickHz: number | null
  readonly sequenceGaps: number
}

function readLink(body: DataView): LinkSummary {
  const tickHz = body.getUint32(LinkRecord.TickHzOffset)
  return {
    state: STATE_NAMES.get(body.getUint8(LinkRecord.StateOffset)) ?? 'unknown',
    channelsPresent: body.getUint8(LinkRecord.ChannelsPresentOffset),
    bootSession: body.getUint32(LinkRecord.BootSessionOffset),
    tickHz: tickHz || null,
    sequenceGaps: body.getUint32(LinkRecord.SequenceGapOffset),
  }
}

export interface ChannelDetail {
  readonly channel: number
  readonly amsMotion: number
  readonly present: boolean
  readonly filament: boolean
  readonly pullPercent: number
  readonly sensorValidity: number
  readonly sensorOnline: boolean
  readonly sensorGood: boolean
  readonly rawAngle: number
  readonly positionDelta: number
  readonly motorPwm: number
  readonly motionFault: number
  /** Null when the BMCU did not report a controller phase for this channel. */
  readonly controllerMotion: number | null
}

function readChannel(body: DataView): ChannelDetail | null {
  const channel = body.getUint8(ChannelRecord.ChannelOffset)
  if (channel >= SLOT_COUNT) return null
  const flags = body.getUint16(ChannelRecord.FlagsOffset, true)
  const controller = body.getUint8(ChannelRecord.ControllerMotionOffset)
  return {
    channel,
    amsMotion: body.getUint8(ChannelRecord.AmsMotionOffset),
    present: body.getUint8(ChannelRecord.InsertedOffset) !== 0,
    filament: body.getUint8(ChannelRecord.OnlineOffset) !== 0,
    pullPercent: body.getUint8(ChannelRecord.PullPercentOffset),
    sensorValidity: body.getUint8(ChannelRecord.SensorValidityOffset),
    sensorOnline: (flags & ChannelRecord.SensorOnlineBit) !== 0,
    sensorGood: (flags & ChannelRecord.SensorGoodBit) !== 0,
    rawAngle: body.getUint16(ChannelRecord.RawAngleOffset, true),
    positionDelta: body.getInt16(ChannelRecord.PositionDeltaOffset, true),
    motorPwm: body.getInt16(ChannelRecord.MotorPwmOffset, true),
    motionFault: body.getUint8(ChannelRecord.MotionFaultOffset),
    controllerMotion: controller & 0x80 ? controller & 0x7f : null,
  }
}

/** Four little-endian u32 counters, the shape every printer/AMS record uses. */
function readCounters(body: DataView): [number, number, number, number] {
  return [0, 1, 2, 3].map((index) => body.getUint32(index * 4, true)) as [
    number,
    number,
    number,
    number,
  ]
}

export interface SnapshotEvent {
  readonly hwTick32: number
  readonly recordType: number
  readonly severity: number
  readonly severityName: string
  readonly source: number
  readonly sourceName: string
  readonly detail: Uint8Array
}

const SEVERITY_NAMES = new Map<number, string>(
  Object.entries(BmcuSeverity).map(([name, value]) => [value, name.toLowerCase()]),
)
const SOURCE_NAMES = new Map<number, string>(
  Object.entries(BmcuSource).map(([name, value]) => [value, name.toLowerCase()]),
)

function readEvent(body: DataView): SnapshotEvent {
  const length = body.getUint8(EventPayload.PayloadLengthOffset)
  const severity = body.getUint8(EventPayload.SeverityOffset)
  const source = body.getUint8(EventPayload.SourceOffset)
  return {
    hwTick32: body.getUint32(EventPayload.HwTickOffset, true),
    recordType: body.getUint8(EventPayload.RecordTypeOffset),
    severity,
    severityName: SEVERITY_NAMES.get(severity) ?? String(severity),
    source,
    sourceName: SOURCE_NAMES.get(source) ?? String(source),
    detail: new Uint8Array(
      body.buffer,
      body.byteOffset + EventPayload.DetailOffset,
      Math.min(length, EventPayload.DetailSize),
    ),
  }
}

export interface PrinterCounters {
  readonly rxBytes: number
  readonly rxFramesValid: number
  readonly rxBadLength: number
  readonly rxHeaderCrcError: number
  readonly rxResyncBytes: number
  readonly rxPublishDrop: number
  readonly rxDmaError: number
  readonly rxUsartOverrun: number
  readonly rxDmaOverrun: number
  readonly rxDmaWrap: number
  readonly rxDmaMaxPending: number
  readonly rxCompatCopy: number
  readonly txStarted: number
  readonly txCompleted: number
  readonly txResponseBusy: number
  readonly txResponseMissing: number
  readonly txInvalidLength: number
  readonly txDmaError: number
  readonly txTimeout: number
  readonly txNoResponseExpected: number
}

export interface AmsCounters {
  readonly gapNowMs: number
  readonly gapMaxMs: number
  readonly gapMaxSinceConfirmMs: number
  readonly msSinceConfirm: number
  readonly countMotion: number
  readonly countStuMotion: number
  readonly countMcOnline: number
  readonly registrationQueries: number
  readonly wouldReoffer: number
  readonly confirms: number
  readonly resets: number
}

export interface LinkSnapshot {
  readonly linkIndex: number
  readonly summary: LinkSummary | null
  readonly channels: readonly (ChannelDetail | null)[]
  readonly printer: Partial<PrinterCounters>
  readonly ams: Partial<AmsCounters>
  readonly events: readonly SnapshotEvent[]
  /** False when the BMCU has not delivered a snapshot for this link. */
  readonly complete: boolean
}

function emptyLink(linkIndex: number): LinkSnapshot {
  return {
    linkIndex,
    summary: null,
    channels: Array(SLOT_COUNT).fill(null),
    printer: {},
    ams: {},
    events: [],
    complete: false,
  }
}

/**
 * Folds the record stream into one entry per link.
 *
 * A link with no channel records is reported as incomplete rather than as a set
 * of zeroes, because "the BMCU has not sent a snapshot" and "every counter is
 * zero" are different situations and the pages render them differently.
 */
export function readSnapshot(buffer: ArrayBuffer): Map<number, LinkSnapshot> {
  const links = new Map<number, LinkSnapshot>()
  for (const record of parseSnapshotRecords(buffer)) {
    const current = links.get(record.linkIndex) ?? emptyLink(record.linkIndex)
    const channels = current.channels.slice()
    const printer: Partial<PrinterCounters> = { ...current.printer }
    const ams: Partial<AmsCounters> = { ...current.ams }
    const events = current.events.slice()
    let summary = current.summary

    switch (record.recordType) {
      case SnapshotKind.Link:
        summary = readLink(record.body)
        break
      case SnapshotKind.Channel: {
        const channel = readChannel(record.body)
        if (channel) channels[channel.channel] = channel
        break
      }
      case SnapshotKind.PrinterRxCore: {
        const [a, b, c, d] = readCounters(record.body)
        Object.assign(printer, {
          rxBytes: a, rxFramesValid: b, rxBadLength: c, rxHeaderCrcError: d,
        })
        break
      }
      case SnapshotKind.PrinterRxLoss: {
        const [a, b, c, d] = readCounters(record.body)
        Object.assign(printer, {
          rxResyncBytes: a, rxPublishDrop: b, rxDmaError: c, rxUsartOverrun: d,
        })
        break
      }
      case SnapshotKind.PrinterRxDma: {
        const [a, b, c, d] = readCounters(record.body)
        Object.assign(printer, {
          rxDmaOverrun: a, rxDmaWrap: b, rxDmaMaxPending: c, rxCompatCopy: d,
        })
        break
      }
      case SnapshotKind.PrinterTxCore: {
        const [a, b, c, d] = readCounters(record.body)
        Object.assign(printer, {
          txStarted: a, txCompleted: b, txResponseBusy: c, txResponseMissing: d,
        })
        break
      }
      case SnapshotKind.PrinterTxFault: {
        const [a, b, c, d] = readCounters(record.body)
        Object.assign(printer, {
          txInvalidLength: a, txDmaError: b, txTimeout: c,
          txNoResponseExpected: d,
        })
        break
      }
      case SnapshotKind.AmsService: {
        const [a, b, c, d] = readCounters(record.body)
        Object.assign(ams, {
          gapNowMs: a, gapMaxMs: b, gapMaxSinceConfirmMs: c, msSinceConfirm: d,
        })
        break
      }
      case SnapshotKind.AmsRegistration: {
        const body = record.body
        Object.assign(ams, {
          countMotion: body.getUint16(0, true),
          countStuMotion: body.getUint16(2, true),
          countMcOnline: body.getUint16(4, true),
          registrationQueries: body.getUint16(6, true),
          wouldReoffer: body.getUint16(8, true),
          confirms: body.getUint16(10, true),
          resets: body.getUint16(12, true),
        })
        break
      }
      case SnapshotKind.Event:
        events.push(readEvent(record.body))
        break
      default:
        break
    }

    links.set(record.linkIndex, {
      linkIndex: record.linkIndex,
      summary,
      channels,
      printer,
      ams,
      events,
      complete: channels.some(Boolean),
    })
  }
  return links
}
