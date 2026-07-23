#pragma once

#include "bmcu_link_protocol.h"

#include <stdint.h>

namespace printer_bus_result
{
enum class HandlerDisposition : uint8_t
{
    no_handler,
    unsupported,
    no_response_expected,
    response_expected,
};

struct TransactionResult
{
    bmcu_link_protocol::TransactionOutcome outcome;
    bmcu_link_protocol::DecisionReason reason;
};

inline TransactionResult classify_transaction(HandlerDisposition disposition,
                                               bool response_pending_before,
                                               uint16_t response_length)
{
    using namespace bmcu_link_protocol;
    if (disposition == HandlerDisposition::no_handler)
        return {OUTCOME_REJECTED, REASON_NO_HANDLER};
    if (disposition == HandlerDisposition::unsupported)
        return {OUTCOME_IGNORED, REASON_UNSUPPORTED};
    if (response_pending_before)
        return {OUTCOME_REJECTED, REASON_TX_BUSY};
    if (disposition == HandlerDisposition::no_response_expected)
        return {OUTCOME_IGNORED, REASON_NO_RESPONSE_EXPECTED};
    if (response_length != 0u)
        return {OUTCOME_REPLIED, REASON_OK};
    return {OUTCOME_FAILED, REASON_NO_RESPONSE};
}
}