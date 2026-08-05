/**
 * Byte offsets that no JSON registry describes.
 *
 * These mirror the ``struct`` format strings in ``pico/bmcu_binary.py`` and the
 * decoders in ``pico/bmcu_link.py``. Codegen cannot reach them, so they are
 * named here and pinned by ``test/decode.test.ts``, which decodes the same
 * fixtures the Python codec produced in ``tests/fixtures/bmcu_binary/``.
 *
 * Never inline these numbers at a call site: the value the UI reads for a slot
 * is the sum of three independent layouts, and that arithmetic is exactly what
 * used to be copied by hand.
 */
import { Limit } from './generated'

/** BMB1 envelope header, ``write_header`` format ``>4sBBHIQQB3s``. */
export const Envelope = {
  Magic: 0x424d4231, // "BMB1"
  Version: 1,
  Size: Limit.HeaderSize,
  MagicOffset: 0,
  VersionOffset: 4,
  MessageTypeOffset: 5,
  FlagsOffset: 6,
  PayloadLengthOffset: 8,
  TransportSequenceOffset: 12,
  PicoBootIdOffset: 20,
  LinkIndexOffset: 28,
} as const

/** BMCU_FRAME payload prefix, ``write_bmcu_frame``: ``>QH`` then the wire frame. */
export const BmcuFramePrefix = {
  ReceivedAtUsOffset: 0,
  WireLengthOffset: 8,
  Size: 10,
} as const

/** BMCU Link wire frame, ``encode_frame`` in ``pico/bmcu_link.py``. */
export const LinkWire = {
  SyncSize: 2,
  VersionOffset: 2,
  KindOffset: 3,
  SequenceOffset: 4,
  PayloadLengthOffset: 6,
  HeaderSize: 7,
  CrcSize: 2,
} as const

/** STATUS payload fields, ``BMCUMonitor._decode_status``. */
export const StatusPayload = {
  HwTick32Offset: 0,
  TxDropOffset: 4,
  RxDropOffset: 6,
  CrcErrorOffset: 8,
  FrameErrorOffset: 10,
  CurrentSlotOffset: 12,
  InsertedMaskOffset: 13,
  OnlineMaskOffset: 14,
  MotionOffset: 15,
  PullPercentOffset: 19,
  PressureOffset: 23,
  LedModeOffset: 25,
  ControlErrorOffset: 26,
  ChannelFlagsOffset: 27,
  Size: 31,
  /**
   * The encoding before the channel-flags byte. Accepted so the bridge and the
   * BMCU can be updated independently -- requiring 31 exactly means flashing
   * two BMCUs and a Pico in lockstep, and a BMCU flash can leave a board that
   * looks bricked.
   */
  LegacySize: 27,
} as const

/**
 * Per-channel fault-latch and switch byte, at ``StatusPayload.ChannelFlagsOffset``
 * and repeated in the channel record's flag bits 8..12.
 *
 * The firmware assembles this through a union in ``src/bmcu_link.h``, but that
 * union is MCU-internal and its bitfield order belongs to the compiler. Only
 * the raw byte is contract, so these are explicit shifts against
 * ``docs/bmcu_wire_layout.json``.
 */
export const ChannelFlags = {
  /** 0 none, 1 both switches, 2 external only, 3 internal only. */
  KsShift: 0,
  KsMask: 0b11,
  /** Pull fell below 40% during pressure control on use; motor latched off. */
  LowLatchBit: 1 << 2,
  /** The jam variant of the low latch, which also raises HMS 0xF06F. */
  JamLatchBit: 1 << 3,
  /** DM autoload failed; clears only on a full withdrawal (ks === 0). */
  DmFailLatchBit: 1 << 4,
} as const

/** PICO_LOG payload, ``write_log`` format ``>QQBBHH`` then three byte runs. */
export const LogPayload = {
  LogSequenceOffset: 0,
  UptimeMsOffset: 8,
  SeverityOffset: 16,
  ComponentLengthOffset: 17,
  MessageLengthOffset: 18,
  DetailLengthOffset: 20,
  HeaderSize: 22,
} as const

/** TLV triplet, ``write_tlv``: tag, value type, big-endian u16 length. */
export const Tlv = {
  TagOffset: 0,
  ValueTypeOffset: 1,
  LengthOffset: 2,
  HeaderSize: 4,
} as const

/** EVENT payload, BMCUMonitor._decode_event. Little-endian. */
export const EventPayload = {
  HwTickOffset: 0,
  RecordTypeOffset: 4,
  SeverityOffset: 5,
  SourceOffset: 6,
  PayloadLengthOffset: 7,
  DetailOffset: 8,
  DetailSize: 8,
  Size: 16,
} as const

/** /api/snapshot.bin record header, pico/binary_api.py _snapshot_record. */
export const SnapshotRecord = {
  Magic: 0x42534e50, // "BSNP"
  Version: 1,
  HeaderSize: 16,
  VersionOffset: 4,
  LinkIndexOffset: 5,
  RecordTypeOffset: 6,
  RecordIndexOffset: 7,
  HwTickOffset: 8,
  PayloadLengthOffset: 14,
} as const

/**
 * FULL_STATUS record types, from the constants in pico/bmcu_link.py. 240 and
 * 241 are synthetic and added by the device's snapshot endpoint.
 */
export const SnapshotKind = {
  Channel: 2,
  PrinterAuth: 5,
  PrinterRxCore: 6,
  PrinterRxLoss: 7,
  PrinterRxDma: 8,
  PrinterTxCore: 9,
  PrinterTxFault: 10,
  AmsService: 11,
  AmsRegistration: 12,
  Link: 0xf0,
  Event: 0xf1,
} as const

/** Synthetic link record, pico/binary_api.py _link_record. Big-endian. */
export const LinkRecord = {
  StateOffset: 0,
  ChannelsPresentOffset: 1,
  BootSessionOffset: 4,
  TickHzOffset: 8,
  SequenceGapOffset: 12,
  /**
   * Which of the 780 firmware builds is on the board. 0xFFFF means the BMCU
   * did not report it -- 0 is a legitimate variant (every option off) and so
   * cannot double as unknown.
   */
  VariantFlagsOffset: 16,
  /**
   * Build fingerprint. Does not decode to anything; equal values mean equal
   * firmware, and the matrix manifest maps it back to a commit. 0 if unreported.
   */
  BuildHashOffset: 20,
} as const

/** Channel record body, BMCUMonitor._handle_snapshot record_type 2. */
export const ChannelRecord = {
  ChannelOffset: 0,
  AmsMotionOffset: 1,
  InsertedOffset: 2,
  OnlineOffset: 3,
  PullPercentOffset: 4,
  SensorValidityOffset: 5,
  FlagsOffset: 6,
  RawAngleOffset: 8,
  PositionDeltaOffset: 10,
  MotorPwmOffset: 12,
  MotionFaultOffset: 14,
  ControllerMotionOffset: 15,
  SensorOnlineBit: 1 << 2,
  SensorGoodBit: 1 << 3,
  /**
   * The high byte of the u16 at ``FlagsOffset`` is the STATUS channel-flags
   * byte shifted up whole, in the same bit order. It is the only free space a
   * channel record has left, and carrying it unrepacked means one decoder
   * serves both carriers.
   */
  ChannelFlagsShift: 8,
} as const

/** Channels per BMCU loader; the STATUS masks and arrays are all this wide. */
export const SLOT_COUNT = 4

/**
 * Offset of a STATUS field inside the BMCU_FRAME payload a BMB1 message
 * carries. Three layouts stack here, which is why this is a function rather
 * than a constant per field.
 */
export function statusFieldOffset(fieldOffset: number): number {
  return BmcuFramePrefix.Size + LinkWire.HeaderSize + fieldOffset
}
