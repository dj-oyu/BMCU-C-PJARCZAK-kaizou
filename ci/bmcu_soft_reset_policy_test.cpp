#include "bmcu_soft_reset_policy.h"

using namespace bmcu_link_protocol;
using namespace bmcu_soft_reset;

int main()
{
    Request request = {1u, kReasonManual, 0u, 5000u};
    if (validate_request(request) != ACK_OK) return 1;
    request.operation_id = 0u;
    if (validate_request(request) != ACK_BAD_VALUE) return 2;
    request = {2u, 3u, 0u, 100u};
    if (validate_request(request) != ACK_BAD_VALUE) return 3;
    request = {3u, kReasonCommissioning, 1u, 100u};
    if (validate_request(request) != ACK_BAD_VALUE) return 4;
    request = {4u, kReasonManual, 0u, 0u};
    if (validate_request(request) != ACK_BAD_VALUE) return 5;
    request.ttl_ms = kMaxTtlMs + 1u;
    if (validate_request(request) != ACK_BAD_VALUE) return 6;

    request = {5u, kReasonManual, 0u, 5000u};
    SafetyState safety = {false, false, false, true, true};
    if (evaluate_request(request, safety, false) != ACK_OK) return 9;
    if (evaluate_request(request, safety, true) != ACK_DUPLICATE) return 8;
    if (evaluate_safety(safety) != ACK_OK) return 9;
    safety.reset_pending = true;
    if (evaluate_safety(safety) != ACK_BUSY) return 8;
    safety = {false, false, true, true, true};
    if (evaluate_safety(safety) != ACK_BUSY) return 9;
    safety = {false, true, false, true, true};
    if (evaluate_safety(safety) != ACK_BAD_STATE) return 10;
    safety = {false, false, false, false, true};
    if (evaluate_safety(safety) != ACK_BAD_STATE) return 11;
    safety = {false, false, false, true, false};
    if (evaluate_safety(safety) != ACK_BAD_STATE) return 12;
    return 0;
}