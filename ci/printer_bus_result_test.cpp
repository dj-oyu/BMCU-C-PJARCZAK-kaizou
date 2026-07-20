#include "printer_bus_result.h"

using namespace bmcu_link_protocol;
using namespace printer_bus_result;

int main()
{
    TransactionResult result = classify_transaction(
        HandlerDisposition::no_handler, false, 0u);
    if (result.outcome != OUTCOME_REJECTED || result.reason != REASON_NO_HANDLER) return 1;

    result = classify_transaction(HandlerDisposition::unsupported, false, 0u);
    if (result.outcome != OUTCOME_IGNORED || result.reason != REASON_UNSUPPORTED) return 2;

    result = classify_transaction(HandlerDisposition::response_expected, true, 31u);
    if (result.outcome != OUTCOME_REJECTED || result.reason != REASON_TX_BUSY) return 3;

    result = classify_transaction(HandlerDisposition::response_expected, false, 31u);
    if (result.outcome != OUTCOME_REPLIED || result.reason != REASON_OK) return 4;

    result = classify_transaction(HandlerDisposition::response_expected, false, 0u);
    if (result.outcome != OUTCOME_FAILED || result.reason != REASON_NO_RESPONSE) return 5;

    return 0;
}