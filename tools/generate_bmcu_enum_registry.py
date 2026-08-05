"""Generate the Pico/Bambuddy enum registry from the BMCU wire ABI header."""
import argparse
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HEADER = ROOT / "src" / "bmcu_link_protocol.h"
OUTPUT = ROOT / "docs" / "bmcu_link_enum_registry.json"
PREFIXES = {
    "Kind": "KIND_", "Capability": "CAP_", "FullStatusSection": "FULL_SECTION_",
    "FullStatusRecordType": "FULL_RECORD_", "AckResult": "ACK_",
    "RecordSeverity": "SEVERITY_", "RecordSource": "SOURCE_",
    "CommandOwner": "OWNER_", "TransactionOutcome": "OUTCOME_",
    "DecisionReason": "REASON_", "RecordType": "RECORD_",
    "DiagCounter": "DIAG_COUNTER_", "DmTeardownCause": "TEARDOWN_",
    "ResetState": "RESET_", "ResetCancelReason": "RESET_CANCEL_",
    "SensorValidity": "SENSOR_", "StateField": "STATE_FIELD_",
}
NAMES = {
    "Kind": "kind", "Capability": "capability", "FullStatusSection": "full_status_section",
    "FullStatusRecordType": "full_status_record_type", "AckResult": "ack_result",
    "RecordSeverity": "severity", "RecordSource": "source", "CommandOwner": "command_owner",
    "TransactionOutcome": "outcome", "DecisionReason": "reason", "RecordType": "record_type",
    "DiagCounter": "diag_counter", "DmTeardownCause": "dm_teardown_cause",
    "ResetState": "reset_state", "ResetCancelReason": "reset_cancel_reason",
    "SensorValidity": "sensor_validity", "StateField": "state_field",
}

def value(expression, symbols):
    expression = re.sub(r"(?<![A-Za-z_])((?:0x[0-9A-Fa-f]+)|(?:\d+))[uUlL]+\b", r"\1", expression.strip())
    if not re.fullmatch(r"[A-Za-z0-9_ ()<>&|+~-]+", expression):
        raise ValueError("unsupported ABI expression: " + expression)
    return eval(expression, {"__builtins__": {}}, symbols)

def registry():
    text = HEADER.read_text(encoding="utf-8")
    protocol = value(re.search(r"constexpr uint8_t VERSION = (.*?);", text).group(1), {
        "VERSION_PRERELEASE": value(re.search(r"VERSION_PRERELEASE = (.*?);", text).group(1), {}),
        "VERSION_REVISION": value(re.search(r"VERSION_REVISION = (.*?);", text).group(1), {}),
    })
    result = {}
    for name, body in re.findall(r"enum (\w+) : uint\d+_t\s*\{(.*?)\};", text, re.S):
        if name not in NAMES:
            continue
        symbols, entries = {}, {}
        for item in body.split(","):
            item = item.strip()
            if not item:
                continue
            raw_name, expression = map(str.strip, item.split("=", 1))
            numeric = value(expression, symbols)
            symbols[raw_name] = numeric
            entries[str(numeric)] = raw_name.removeprefix(PREFIXES[name]).lower()
        result[NAMES[name]] = entries
    return {"schema": "bmcu.link.enum-registry.v1", "registry_version": "alpha.3",
            "wire_protocol": protocol, "source": "src/bmcu_link_protocol.h", "enums": result}

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    rendered = json.dumps(registry(), indent=2, ensure_ascii=False) + "\n"
    if args.check:
        if not OUTPUT.exists() or OUTPUT.read_text(encoding="utf-8") != rendered:
            raise SystemExit("enum registry is stale; run tools/generate_bmcu_enum_registry.py")
        return
    OUTPUT.write_text(rendered, encoding="utf-8")

if __name__ == "__main__":
    main()