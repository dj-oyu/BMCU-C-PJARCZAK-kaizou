#ifndef BMCU_LINK_PROTOCOL_H
#define BMCU_LINK_PROTOCOL_H

#include <stdint.h>

// Wire values are ABI: append new values, never renumber existing ones.
namespace bmcu_link_protocol
{
constexpr uint8_t VERSION = 2u;

enum Kind : uint8_t
{
    KIND_HELLO = 0x01u, KIND_STATUS = 0x02u, KIND_EVENT = 0x03u,
    KIND_PRINTER_TRANSACTION = 0x04u, KIND_SENSOR_RECORD = 0x05u,
    KIND_GET_STATUS = 0x10u, KIND_SET_LED_MODE = 0x11u, KIND_PING = 0x12u,
    KIND_PONG = 0x72u, KIND_ACK = 0x7Fu,
};

enum Capability : uint16_t
{
    CAP_STATUS_EVENTS = 1u << 0, CAP_LED_OVERRIDE = 1u << 1,
    CAP_PING_PONG = 1u << 2, CAP_RAW_HW_TICK = 1u << 3,
    CAP_PRINTER_TRACE = 1u << 4, CAP_SENSOR_RECORD = 1u << 5,
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
};

enum RecordType : uint8_t
{
    RECORD_BOOT = 1u, RECORD_PRINTER_LINK = 2u, RECORD_PRINTER_TRANSACTION = 3u,
    RECORD_STATE_CHANGE = 4u, RECORD_SENSOR = 5u, RECORD_COMMAND_RESULT = 6u,
    RECORD_SAFETY_DECISION = 7u, RECORD_DIAGNOSTIC_COUNTER = 8u,
};

enum SensorValidity : uint8_t
{
    SENSOR_UNKNOWN = 0u, SENSOR_VALID = 1u, SENSOR_STALE = 2u,
    SENSOR_OFFLINE = 3u, SENSOR_FAULT = 4u,
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

union LogRecordPayload
{
    LogBootPayload boot;
    LogPrinterLinkPayload printer_link;
    LogPrinterTransactionPayload printer_transaction;
    LogStateChangePayload state_change;
    LogSensorPayload sensor;
    LogCommandResultPayload command_result;
    LogSafetyDecisionPayload safety_decision;
    LogDiagnosticCounterPayload diagnostic_counter;
    uint8_t raw[8];
};

struct LogRecord
{
    LogRecordHeader header;
    LogRecordPayload payload;
};

static_assert(sizeof(LogRecordHeader) == 8u, "LogRecordHeader ABI changed");
static_assert(sizeof(LogRecordPayload) == 8u, "LogRecordPayload ABI changed");
static_assert(sizeof(LogRecord) == 16u, "LogRecord ABI changed");
}

#endif
