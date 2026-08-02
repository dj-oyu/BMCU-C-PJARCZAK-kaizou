/**
 * Typed decoders for the BMB1 byte streams the Pico serves.
 *
 * Every offset comes from ``layout.ts`` and every tag from the generated
 * registry, so renumbering a tag in ``docs/bmcu_binary_registry.json`` breaks
 * the build here instead of silently mislabelling a card in the browser.
 */
import { BmcuKind, Diag, Limit, MessageType, ValueType } from './generated'
import {
  BmcuFramePrefix,
  Envelope,
  LinkWire,
  LogPayload,
  SLOT_COUNT,
  StatusPayload,
  Tlv,
  statusFieldOffset,
} from './layout'

const utf8 = new TextDecoder()

export interface BmbMessage {
  readonly messageType: number
  readonly flags: number
  readonly linkIndex: number
  readonly payload: DataView
}

/** Splits a concatenated BMB1 response, stopping at the first invalid header. */
export function parseMessages(buffer: ArrayBuffer): BmbMessage[] {
  const view = new DataView(buffer)
  const messages: BmbMessage[] = []
  let offset = 0
  while (offset + Envelope.Size <= buffer.byteLength) {
    if (view.getUint32(offset + Envelope.MagicOffset) !== Envelope.Magic) break
    if (view.getUint8(offset + Envelope.VersionOffset) !== Envelope.Version) break
    const length = view.getUint32(offset + Envelope.PayloadLengthOffset)
    if (offset + Envelope.Size + length > buffer.byteLength) break
    messages.push({
      messageType: view.getUint8(offset + Envelope.MessageTypeOffset),
      flags: view.getUint16(offset + Envelope.FlagsOffset),
      linkIndex: view.getUint8(offset + Envelope.LinkIndexOffset),
      payload: new DataView(buffer, offset + Envelope.Size, length),
    })
    offset += Envelope.Size + length
  }
  return messages
}

export type TlvValue = number | bigint | string | Uint8Array

/** Decodes one TLV run. Unknown value types are kept as raw bytes. */
export function parseTlvs(view: DataView): Map<number, TlvValue> {
  const values = new Map<number, TlvValue>()
  let offset = 0
  while (offset + Tlv.HeaderSize <= view.byteLength) {
    const tag = view.getUint8(offset + Tlv.TagOffset)
    const valueType = view.getUint8(offset + Tlv.ValueTypeOffset)
    const length = view.getUint16(offset + Tlv.LengthOffset)
    offset += Tlv.HeaderSize
    if (offset + length > view.byteLength) break
    values.set(tag, decodeValue(view, offset, length, valueType))
    offset += length
  }
  return values
}

function decodeValue(
  view: DataView,
  offset: number,
  length: number,
  valueType: number,
): TlvValue {
  const bytes = () => new Uint8Array(view.buffer, view.byteOffset + offset, length)
  switch (valueType) {
    case ValueType.Uint8:
      return length === 1 ? view.getUint8(offset) : bytes()
    case ValueType.Uint16:
      return length === 2 ? view.getUint16(offset) : bytes()
    case ValueType.Uint32:
      return length === 4 ? view.getUint32(offset) : bytes()
    case ValueType.Uint64:
      return length === 8 ? view.getBigUint64(offset) : bytes()
    case ValueType.Int8:
      return length === 1 ? view.getInt8(offset) : bytes()
    case ValueType.Int16:
      return length === 2 ? view.getInt16(offset) : bytes()
    case ValueType.Int32:
      return length === 4 ? view.getInt32(offset) : bytes()
    case ValueType.Int64:
      return length === 8 ? view.getBigInt64(offset) : bytes()
    case ValueType.Bool:
      return length === 1 ? view.getUint8(offset) : bytes()
    case ValueType.Utf8:
      return utf8.decode(bytes())
    default:
      return bytes()
  }
}

/**
 * Diagnostic counters are u64 but always small enough for a double; the UI
 * formats and compares them as numbers.
 */
export function counter(values: Map<number, TlvValue>, tag: number): number {
  const value = values.get(tag)
  if (typeof value === 'bigint') return Number(value)
  if (typeof value === 'number') return value
  return 0
}

export function optionalCounter(
  values: Map<number, TlvValue>,
  tag: number,
): number | null {
  const value = values.get(tag)
  if (typeof value === 'bigint') return Number(value)
  if (typeof value === 'number') return value
  return null
}

export interface LoaderStatus {
  /** Channel index the BMCU currently has selected, or null when idle. */
  readonly selectedSlot: number | null
  /**
   * Per-channel hardware presence, not filament. The firmware sets this from
   * the PULL potentiometer voltage at boot (``MC_PULL_detect_channels_inserted``
   * in ``src/Motion_control.cpp``), so it reports whether the channel unit is
   * plugged in and never changes when filament is inserted or removed.
   */
  readonly channelPresent: readonly boolean[]
  /** Per-channel filament detection: the microswitch, gated by presence. */
  readonly filamentLoaded: readonly boolean[]
  readonly motion: readonly number[]
  readonly pullPercent: readonly number[]
}

const STATUS_MIN_BYTES =
  BmcuFramePrefix.Size + LinkWire.HeaderSize + StatusPayload.Size

/** Returns null when the BMCU_FRAME does not carry a full STATUS frame. */
export function parseStatus(payload: DataView): LoaderStatus | null {
  if (payload.byteLength < STATUS_MIN_BYTES) return null
  const kindOffset = BmcuFramePrefix.Size + LinkWire.KindOffset
  if (payload.getUint8(kindOffset) !== BmcuKind.Status) return null

  const byte = (fieldOffset: number) =>
    payload.getUint8(statusFieldOffset(fieldOffset))
  const mask = byte(StatusPayload.InsertedMaskOffset)
  const online = byte(StatusPayload.OnlineMaskOffset)
  const selected = byte(StatusPayload.CurrentSlotOffset)
  const slots = range(SLOT_COUNT)
  return {
    selectedSlot: selected < SLOT_COUNT ? selected : null,
    channelPresent: slots.map((index) => ((mask >> index) & 1) !== 0),
    filamentLoaded: slots.map((index) => ((online >> index) & 1) !== 0),
    motion: slots.map((index) => byte(StatusPayload.MotionOffset + index)),
    pullPercent: slots.map((index) => byte(StatusPayload.PullPercentOffset + index)),
  }
}

export interface LogRecord {
  readonly sequence: bigint
  readonly uptimeMs: bigint
  readonly severity: number
  readonly component: string
  readonly message: string
}

export function parseLogRecord(payload: DataView): LogRecord | null {
  if (payload.byteLength < LogPayload.HeaderSize) return null
  const componentLength = payload.getUint8(LogPayload.ComponentLengthOffset)
  const messageLength = payload.getUint16(LogPayload.MessageLengthOffset)
  const end = LogPayload.HeaderSize + componentLength + messageLength
  if (end > payload.byteLength) return null
  const slice = (start: number, length: number) =>
    utf8.decode(new Uint8Array(payload.buffer, payload.byteOffset + start, length))
  return {
    sequence: payload.getBigUint64(LogPayload.LogSequenceOffset),
    uptimeMs: payload.getBigUint64(LogPayload.UptimeMsOffset),
    severity: payload.getUint8(LogPayload.SeverityOffset),
    component: slice(LogPayload.HeaderSize, componentLength),
    message: slice(LogPayload.HeaderSize + componentLength, messageLength),
  }
}

export interface LinkCounters {
  readonly backlog: number
  readonly peakBacklog: number
  readonly rxBytes: number
  readonly crcErrors: number
  readonly frameErrors: number
  readonly sequenceGaps: number
  readonly maxServiceDelayUs: number
  readonly overflows: number
}

/**
 * UART counters are one tag block per link. The blocks are contiguous and
 * identically ordered, so link 1 is link 0's block plus a fixed stride rather
 * than a second hand-written tag list.
 */
const UART_TAG_STRIDE = Diag.Uart1Backlog - Diag.Uart0Backlog
const OVERFLOW_TAG_STRIDE = Diag.Uart1OverflowCount - Diag.Uart0OverflowCount

export function linkCounters(
  link: number,
  values: Map<number, TlvValue>,
): LinkCounters {
  const base = Diag.Uart0Backlog + link * UART_TAG_STRIDE
  const at = (tag: number) => counter(values, tag)
  return {
    backlog: at(base),
    peakBacklog: at(base + (Diag.Uart0MaxBacklog - Diag.Uart0Backlog)),
    rxBytes: at(base + (Diag.Uart0RxBytes - Diag.Uart0Backlog)),
    crcErrors: at(base + (Diag.Uart0CrcErrors - Diag.Uart0Backlog)),
    frameErrors: at(base + (Diag.Uart0FrameErrors - Diag.Uart0Backlog)),
    sequenceGaps: at(base + (Diag.Uart0SequenceGaps - Diag.Uart0Backlog)),
    maxServiceDelayUs: at(
      base + (Diag.Uart0MaxServiceDelayUs - Diag.Uart0Backlog),
    ),
    overflows: at(Diag.Uart0OverflowCount + link * OVERFLOW_TAG_STRIDE),
  }
}

export interface Telemetry {
  readonly diagnostics: Map<number, TlvValue>
  readonly statuses: Map<number, LoaderStatus>
  /**
   * The undecoded BMCU_FRAME payloads, kept so the diagnostics page can read
   * the BMCU's own error counters and place them beside the Pico's. Those
   * fields are not part of LoaderStatus because no operational view wants them.
   */
  readonly payloads: Map<number, DataView>
}

/** Folds a /api/current.bin and /api/diagnostics.bin pair into UI state. */
export function readTelemetry(
  current: BmbMessage[],
  diagnostics: BmbMessage[],
): Telemetry {
  const merged = new Map<number, TlvValue>()
  for (const message of diagnostics) {
    if (message.messageType !== MessageType.PicoDiagnostic) continue
    for (const [tag, value] of parseTlvs(message.payload)) merged.set(tag, value)
  }
  const statuses = new Map<number, LoaderStatus>()
  const payloads = new Map<number, DataView>()
  for (const message of current) {
    if (message.messageType !== MessageType.BmcuFrame) continue
    const status = parseStatus(message.payload)
    if (status) {
      statuses.set(message.linkIndex, status)
      payloads.set(message.linkIndex, message.payload)
    }
  }
  return { diagnostics: merged, statuses, payloads }
}

/**
 * Links worth drawing: one that has reported STATUS, or that the Pico is
 * publishing UART counters for. Falls back to every configured link so a bridge
 * with nothing attached still shows why it is empty.
 */
export function activeLinks(telemetry: Telemetry): number[] {
  const all = range(Limit.MaxLinkCount)
  const seen = all.filter(
    (link) =>
      telemetry.statuses.has(link) ||
      telemetry.diagnostics.has(Diag.Uart0Backlog + link * UART_TAG_STRIDE),
  )
  return seen.length ? seen : all
}

export function readLogRecords(messages: BmbMessage[]): LogRecord[] {
  const records: LogRecord[] = []
  for (const message of messages) {
    if (message.messageType !== MessageType.PicoLog) continue
    const record = parseLogRecord(message.payload)
    if (record) records.push(record)
  }
  return records
}

function range(count: number): number[] {
  return Array.from({ length: count }, (_, index) => index)
}
