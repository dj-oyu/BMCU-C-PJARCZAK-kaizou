#pragma once

#include "bmcu_link_protocol.h"
#include <stdint.h>

namespace bmcu_soft_reset
{
using bmcu_link_protocol::AckResult;
using namespace bmcu_link_protocol;

constexpr uint8_t kReasonManual = 0u;
constexpr uint8_t kReasonRecovery409d = 1u;
constexpr uint8_t kReasonCommissioning = 2u;
constexpr uint16_t kMaxTtlMs = 5000u;

struct Request
{
    uint32_t operation_id;
    uint8_t reason;
    uint8_t flags;
    uint16_t ttl_ms;
};

struct SafetyState
{
    bool reset_pending;
    bool calibration_busy;
    bool snapshot_active;
    bool motion_safe;
    bool printer_quiescent;
};

inline AckResult validate_request(const Request& request)
{
    if (request.operation_id == 0u || request.reason > kReasonCommissioning ||
        request.flags != 0u || request.ttl_ms == 0u || request.ttl_ms > kMaxTtlMs)
        return ACK_BAD_VALUE;
    return ACK_OK;
}

inline AckResult evaluate_safety(const SafetyState& state)
{
    if (state.reset_pending || state.snapshot_active) return ACK_BUSY;
    if (state.calibration_busy || !state.motion_safe || !state.printer_quiescent)
        return ACK_BAD_STATE;
    return ACK_OK;
}

inline AckResult evaluate_request(const Request& request, const SafetyState& state,
                                  bool duplicate_operation)
{
    const AckResult validation = validate_request(request);
    if (validation != ACK_OK) return validation;
    if (duplicate_operation) return ACK_DUPLICATE;
    return evaluate_safety(state);
}
}