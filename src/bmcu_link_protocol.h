#ifndef BMCU_LINK_PROTOCOL_H
#define BMCU_LINK_PROTOCOL_H

#include <stdint.h>

// Wire values are ABI: append new values, never renumber existing ones.
namespace bmcu_link_protocol
{
constexpr uint8_t VERSION_PRERELEASE = 0x80u;
constexpr uint8_t VERSION_REVISION = 3u;
constexpr uint8_t VERSION = VERSION_PRERELEASE | VERSION_REVISION;
static_assert(VERSION == 0x83u, "alpha.3 wire version changed");

enum Kind : uint8_t
{
    KIND_HELLO = 0x01u, KIND_STATUS = 0x02u, KIND_EVENT = 0x03u,
    KIND_PRINTER_TRANSACTION = 0x04u, KIND_SENSOR_RECORD = 0x05u,
    KIND_GET_STATUS = 0x10u, KIND_SET_LED_MODE = 0x11u, KIND_PING = 0x12u,
    KIND_GET_FULL_STATUS = 0x17u, KIND_REQUEST_SOFT_RESET = 0x18u,
    KIND_PONG = 0x72u, KIND_FULL_STATUS_RECORD = 0x73u, KIND_ACK = 0x7Fu,
};

enum Capability : uint16_t
{
    CAP_STATUS_EVENTS = 1u << 0, CAP_LED_OVERRIDE = 1u << 1,
    CAP_PING_PONG = 1u << 2, CAP_RAW_HW_TICK = 1u << 3,
    CAP_PRINTER_TRACE = 1u << 4, CAP_SENSOR_RECORD = 1u << 5,
    CAP_FULL_STATUS = 1u << 6, CAP_SOFT_RESET = 1u << 7,
};

// Build identity, carried by KIND_HELLO from alpha.3 onward as payload[9..14].
//
// The firmware is not one artefact -- the matrix builds 780 variants -- and a
// board on the bench could be running any of them, or something built by hand
// from an unknown tree. Before this existed there was no way to ask. A bench
// session was spent reasoning about line numbers that could not be confirmed to
// match what was executing.
//
// VariantFlag packs the compile-time configuration into a u16, which names the
// variant exactly. BUILD_HASH (bmcu_link.cpp) is a hash of the translation
// unit's build timestamp folded with those flags: it does not decode to
// anything, but two boards reporting the same value are running the same build,
// and the matrix manifest maps it back to a commit.
//
// Bits 6..15 hold AMS_RETRACT_LEN in thousandths, which is the axis the matrix
// varies most, so the u16 is fully spent. A new boolean needs a wider field
// rather than a spare bit here.
enum VariantFlag : uint16_t
{
    VARIANT_DM_TWO_MICROSWITCH = 1u << 0,
    VARIANT_ONLINE_LED_FILAMENT_RGB = 1u << 1,
    VARIANT_P1S = 1u << 2,
    VARIANT_SOFT_LOAD = 1u << 3,
    VARIANT_AMS_NUM_SHIFT = 4u,
    VARIANT_AMS_NUM_MASK = 0x3u << 4,
    VARIANT_RETRACT_MILLI_SHIFT = 6u,
    VARIANT_RETRACT_MILLI_MASK = 0x3FFu << 6,
};

enum FullStatusSection : uint8_t
{
    FULL_SECTION_GLOBAL = 1u << 0, FULL_SECTION_CHANNELS = 1u << 1,
    FULL_SECTION_PRINTER_BUS = 1u << 2, FULL_SECTION_COUNTERS = 1u << 3,
    FULL_SECTION_ALL = 0x0Fu,
};

enum FullStatusRecordType : uint8_t
{
    FULL_RECORD_GLOBAL = 1u, FULL_RECORD_CHANNEL = 2u,
    FULL_RECORD_PRINTER_BUS = 3u, FULL_RECORD_COUNTERS = 4u,
    FULL_RECORD_PRINTER_AUTH = 5u, FULL_RECORD_PRINTER_RX_CORE = 6u,
    FULL_RECORD_PRINTER_RX_LOSS = 7u, FULL_RECORD_PRINTER_RX_DMA = 8u,
    FULL_RECORD_PRINTER_TX_CORE = 9u, FULL_RECORD_PRINTER_TX_FAULT = 10u,
    FULL_RECORD_AMS_SERVICE = 11u, FULL_RECORD_AMS_REGISTRATION = 12u,
    FULL_RECORD_PROBE = 13u,
    // Raw DM online-key voltages and the per-channel "none" threshold, in
    // millivolts. Everything downstream of dm_key_to_state sees only the
    // decoded four-value ks, so a channel that reads `inner` when its outer
    // switch is pressed is indistinguishable from one whose switch never
    // moved. The bands are 1.4 V and 1.7 V, hardcoded and shared by every
    // channel, while none_thr alone is per-channel and comes from calibration
    // -- so both "the switch does not reach the band" and "the threshold sits
    // on top of the resting voltage" are live explanations that the wire could
    // not tell apart. This record carries the numbers that separate them.
    FULL_RECORD_DM_KEY = 14u,
};

enum AckResult : uint8_t
{
    ACK_OK = 0u, ACK_BAD_VALUE = 1u, ACK_UNSUPPORTED = 2u, ACK_BUSY = 3u,
    ACK_BAD_STATE = 4u, ACK_DENIED = 5u, ACK_EXPIRED = 6u,
    ACK_DUPLICATE = 7u, ACK_INTERNAL = 8u,
};

enum RecordSeverity : uint8_t
{
    SEVERITY_DEBUG = 0u, SEVERITY_INFO = 1u, SEVERITY_NOTICE = 2u,
    SEVERITY_WARNING = 3u, SEVERITY_ERROR = 4u, SEVERITY_CRITICAL = 5u,
};

enum RecordSource : uint8_t
{
    SOURCE_SYSTEM = 0u, SOURCE_PRINTER_BUS = 1u, SOURCE_MOTION = 2u,
    SOURCE_SENSOR = 3u, SOURCE_MANAGEMENT = 4u, SOURCE_SAFETY = 5u,
};

enum CommandOwner : uint8_t
{
    OWNER_NONE = 0u, OWNER_PRINTER = 1u, OWNER_USER = 2u,
    OWNER_BMCU_LOCAL = 3u, OWNER_SAFETY = 4u, OWNER_SYSTEM = 5u,
};

enum TransactionOutcome : uint8_t
{
    OUTCOME_ACCEPTED = 0u, OUTCOME_APPLIED = 1u, OUTCOME_REPLIED = 2u,
    OUTCOME_IGNORED = 3u, OUTCOME_REJECTED = 4u, OUTCOME_FAILED = 5u,
};

enum DecisionReason : uint8_t
{
    REASON_OK = 0u, REASON_TARGET_MISMATCH = 1u, REASON_SLOT_RANGE = 2u,
    REASON_AMS_OFFLINE = 3u, REASON_INVALID_LENGTH = 4u, REASON_INVALID_CRC = 5u,
    REASON_UNSUPPORTED = 6u, REASON_NO_HANDLER = 7u, REASON_TX_BUSY = 8u,
    REASON_BAD_STATE = 9u, REASON_SAFETY_INHIBIT = 10u, REASON_INTERNAL = 11u,
    REASON_OWNER_CONFLICT = 12u, REASON_LEASE_EXPIRED = 13u,
    REASON_NO_RESPONSE = 14u, REASON_NO_RESPONSE_EXPECTED = 15u,
};

enum RecordType : uint8_t
{
    RECORD_BOOT = 1u, RECORD_PRINTER_LINK = 2u, RECORD_PRINTER_TRANSACTION = 3u,
    RECORD_STATE_CHANGE = 4u, RECORD_SENSOR = 5u, RECORD_COMMAND_RESULT = 6u,
    RECORD_SAFETY_DECISION = 7u, RECORD_DIAGNOSTIC_COUNTER = 8u,
    RECORD_PRINTER_LONG_TRANSACTION = 9u, RECORD_RESET_STATE = 10u,
    RECORD_DM_TEARDOWN = 11u,
};

// Why the DM autoload state machine left the state it was in. The machine is
// torn down from several places on a key reading it did not expect, and until
// this record existed every one of those was silent: the bench could see that
// autoload had not happened and nothing about why.
//
// TEARDOWN_KS_DEVIATED is the one the chatter investigation needs. S1_DEBOUNCE
// resets on any deviation from `outer`, with no tolerance, so a lever that
// floats across a threshold aborts a load the operator is actively attempting.
// The record carries how long the state had been held, which is the excursion
// width the bench measured only as a count.
enum DmTeardownCause : uint8_t
{
    TEARDOWN_KS_DEVIATED = 1u, TEARDOWN_KS_EMPTY = 2u,
    TEARDOWN_TIMEOUT = 3u, TEARDOWN_GLOBAL_CLEAR = 4u,
    TEARDOWN_BUFFER_ABORT = 5u,
};

// Counter ids carried by RECORD_DIAGNOSTIC_COUNTER (LogDiagnosticCounterPayload).
//
// MERGER_TAIL_HELD: a session release was refused because the merger is held in
// TAIL. The value is the running count since boot. TAIL has no timeout, so
// nothing expires a merger that is wedged; this is the signal that replaces the
// fuse a timeout would have been.
//
// MERGER_TAIL_PREEMPTED: a channel claimed the merger while another channel's
// tail was still in it. The value is the claiming channel. This is the witness
// that a strand was still in the shared tube when the next one was pushed in --
// the collision happens regardless, because the printer is master, so the
// record is the only thing the BMCU can contribute.
//
// Keep this block comment-free. tools/generate_bmcu_enum_registry.py splits the
// body on commas and does not strip comments, so a comment between entries
// makes the registry build fail.
enum DiagCounter : uint8_t
{
    DIAG_COUNTER_AMS_SERVICE_GAP_MS = 1u, DIAG_COUNTER_AMS_WOULD_REOFFER = 2u,
    DIAG_COUNTER_MERGER_TAIL_HELD = 3u, DIAG_COUNTER_MERGER_TAIL_PREEMPTED = 4u,
};

enum ResetState : uint8_t
{
    RESET_SCHEDULED = 1u, RESET_CANCELLED = 2u,
};

enum ResetCancelReason : uint8_t
{
    RESET_CANCEL_NONE = 0u, RESET_CANCEL_SAFETY_CHANGED = 1u,
    RESET_CANCEL_EXPIRED = 2u, RESET_CANCEL_LINK_TX_FAULT = 3u,
};

enum SensorValidity : uint8_t
{
    SENSOR_UNKNOWN = 0u, SENSOR_VALID = 1u, SENSOR_STALE = 2u,
    SENSOR_OFFLINE = 3u, SENSOR_FAULT = 4u,
};

enum StateField : uint8_t
{
    STATE_FIELD_SLOT = 1u, STATE_FIELD_INSERTED_MASK = 2u,
    STATE_FIELD_ONLINE_MASK = 3u, STATE_FIELD_MOTION = 4u,
    STATE_FIELD_PRESSURE = 5u, STATE_FIELD_LED_MODE = 6u,
    STATE_FIELD_CONTROL_ERROR = 7u, STATE_FIELD_MOTION_FAULT = 8u,
};

// In-memory binary log record. It is copied to a wire payload without formatting.
// payload_length selects the meaningful prefix of payload for each RecordType.
// Initialize LogRecord with {} so unused union bytes are deterministic on the wire.
struct LogRecordHeader
{
    uint32_t hw_tick32;
    RecordType type;
    RecordSeverity severity;
    RecordSource source;
    uint8_t payload_length;
};

struct LogBootPayload
{
    uint8_t reset_reason;
    uint8_t fw_major;
    uint8_t fw_minor;
    uint8_t reserved;
};

struct LogPrinterLinkPayload
{
    uint8_t online;
    uint8_t previous_online;
    DecisionReason reason;
    uint8_t reserved;
};

struct LogPrinterTransactionPayload
{
    uint8_t command;
    CommandOwner owner;
    TransactionOutcome outcome;
    DecisionReason reason;
    uint8_t request_length;
    uint8_t response_length;
    // The bambubus_package_type the parser resolved. Without it a reader sees
    // that a transaction went unanswered and cannot tell what went unanswered:
    // command is buf[4] or buf[11] off the wire, not the class. Diagnosing an
    // A1 running printer firmware 1.08 stalled on exactly this -- the class was
    // only in the snapshot's one-slot last_rx_class field, and the snapshot
    // does not refresh while the bridge is busy.
    uint8_t rx_class;
};

// Appended record type: preserves the alpha.3 PRINTER_TRANSACTION payload while
// retaining the complete u16 type used by printer long frames.
struct LogPrinterLongTransactionPayload
{
    uint16_t type;
    CommandOwner owner;
    TransactionOutcome outcome;
    DecisionReason reason;
    uint8_t request_length;
    uint8_t response_length;
    uint8_t payload_hash;
};

struct LogResetStatePayload
{
    uint32_t operation_id;
    uint8_t state;
    uint8_t request_reason;
    uint8_t cancel_reason;
    uint8_t reserved;
};

struct LogStateChangePayload
{
    uint8_t field;
    uint8_t slot;
    uint16_t previous_value;
    uint16_t value;
};

struct LogSensorPayload
{
    uint8_t sensor;
    uint8_t slot;
    SensorValidity validity;
    uint8_t value_format;
    int32_t value;
};

struct LogCommandResultPayload
{
    uint8_t command;
    CommandOwner owner;
    AckResult result;
    DecisionReason reason;
    uint16_t sequence;
};

struct LogSafetyDecisionPayload
{
    uint8_t action;
    uint8_t slot;
    DecisionReason reason;
    CommandOwner displaced_owner;
    uint16_t evidence_flags;
};

struct LogDiagnosticCounterPayload
{
    uint8_t counter;
    uint8_t reserved[3];
    uint32_t value;
};

// held_ms is the point of the record. Sizing the hold in items 5-6 of the
// bench notes needs excursion widths, and the session that raised the question
// recorded only how many there were.
struct LogDmTeardownPayload
{
    uint8_t slot;
    uint8_t state;
    uint8_t cause;
    uint8_t ks;
    uint32_t held_ms;
};

union LogRecordPayload
{
    LogBootPayload boot;
    LogPrinterLinkPayload printer_link;
    LogPrinterTransactionPayload printer_transaction;
    LogPrinterLongTransactionPayload printer_long_transaction;
    LogResetStatePayload reset_state;
    LogStateChangePayload state_change;
    LogSensorPayload sensor;
    LogCommandResultPayload command_result;
    LogSafetyDecisionPayload safety_decision;
    LogDiagnosticCounterPayload diagnostic_counter;
    LogDmTeardownPayload dm_teardown;
    uint8_t raw[8];
};

struct LogRecord
{
    LogRecordHeader header;
    LogRecordPayload payload;
};

static_assert(sizeof(LogRecordHeader) == 8u, "LogRecordHeader ABI changed");
static_assert(sizeof(LogRecordPayload) == 8u, "LogRecordPayload ABI changed");
// Seven of the union's eight bytes. The remaining byte is the only room left
// for this record; anything further needs its own record type rather than
// silently growing LogRecord, which is fixed at 16 bytes on the wire.
static_assert(sizeof(LogPrinterTransactionPayload) == 7u,
              "LogPrinterTransactionPayload ABI changed");
static_assert(sizeof(LogPrinterLongTransactionPayload) == 8u,
              "LogPrinterLongTransactionPayload ABI changed");
static_assert(sizeof(LogResetStatePayload) == 8u,
              "LogResetStatePayload ABI changed");
static_assert(sizeof(LogDmTeardownPayload) == 8u,
              "LogDmTeardownPayload ABI changed");
static_assert(sizeof(LogRecord) == 16u, "LogRecord ABI changed");
}

#endif
