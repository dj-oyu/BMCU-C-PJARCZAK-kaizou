import base64
import hashlib
import hmac as hmac_lib
import importlib.util
import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


import sys

control = load("bmcu_control", ROOT / "pico" / "bmcu_control.py")
sys.modules.setdefault("bambuddy_ws",
                       load("bambuddy_ws", ROOT / "pico" / "bambuddy_ws.py"))
config_module = load("bambuddy_config", ROOT / "pico" / "bambuddy_config.py")


KEY = bytes(range(32))
KEY_HEX = KEY.hex()
DEVICE = "bmcu-monitor-a"
LINK = "bmcu-a"
BOOT = "a" * 32
NONCE = "b" * 32
PAYLOAD_B64 = base64.b64encode(
    json.dumps({"reason": 0, "ttl_ms": 5000}).encode()).decode()

# Known-answer vectors: pin MAC input byte contract v1 (issue #2). If the
# byte layout changes for any reason these constants must fail.
KAT_MAC_INPUT = (
    "424d43552d4354524c2d763101000e626d63752d6d6f6e69746f722d610006626d63"
    "752d6100206161616161616161616161616161616161616161616161616161616161"
    "6161610020626262626262626262626262626262626262626262626262626262626262"
    "6262000000000000000700076f702d30303031000a736f66745f726573657400001388"
    "002865794a795a57467a623234694f6941774c434169644852735832317a496a6f674e"
    "5441774d48303d")
KAT_MAC = "671bd8d8ff5405f3601ff93b437ad16ed816a4bf590a5b00e8546dad9d41539f"
KAT_RESULT_MAC = (
    "314529fd5f351d1d04795855f38d2b9ec919ced5c0660578a3685c6473afac5c")


def signed_message(sequence=7, operation_id="op-0001", command="soft_reset",
                   ttl_ms=5000, payload_b64=PAYLOAD_B64, key=KEY, **overrides):
    message = {
        "type": "control", "version": 1, "device_id": DEVICE, "link_id": LINK,
        "pico_boot_session": BOOT, "session_nonce": NONCE,
        "control_sequence": sequence, "operation_id": operation_id,
        "command": command, "ttl_ms": ttl_ms, "payload_b64": payload_b64,
    }
    message.update(overrides)
    mac_input = control.mac_input(
        message["version"] if isinstance(message["version"], int) else 1,
        message["device_id"], message["link_id"],
        message["pico_boot_session"], message["session_nonce"],
        message["control_sequence"], message["operation_id"],
        message["command"], message["ttl_ms"], message["payload_b64"])
    message["mac"] = hmac_lib.new(key, mac_input, hashlib.sha256).hexdigest()
    return message


def make_gateway(executor=None, enabled=True, key_hex=KEY_HEX):
    calls = []

    def default_executor(link_id, command, payload):
        calls.append((link_id, command, payload))
        return {"bmcu_operation_id": 1, "sequence": 42}

    gateway = control.ControlGateway(
        DEVICE, BOOT, key_hex, enabled, [LINK, "bmcu-b"],
        executor or default_executor)
    gateway.session_nonce = NONCE
    gateway.last_sequence = -1
    return gateway, calls


class MacContractTests(unittest.TestCase):
    def test_known_answer_mac_input_bytes(self):
        built = control.mac_input(1, DEVICE, LINK, BOOT, NONCE, 7, "op-0001",
                                  "soft_reset", 5000, PAYLOAD_B64)
        self.assertEqual(built.hex(), KAT_MAC_INPUT)

    def test_known_answer_hmac(self):
        built = control.mac_input(1, DEVICE, LINK, BOOT, NONCE, 7, "op-0001",
                                  "soft_reset", 5000, PAYLOAD_B64)
        self.assertEqual(control.hmac_sha256(KEY, built).hex(), KAT_MAC)

    def test_known_answer_result_mac(self):
        built = control.result_input(1, DEVICE, LINK, BOOT, NONCE, 7,
                                     "op-0001", "scheduled", "")
        self.assertEqual(control.hmac_sha256(KEY, built).hex(),
                         KAT_RESULT_MAC)

    def test_hmac_matches_reference_for_long_keys(self):
        for key in (b"k", KEY, bytes(range(96))):
            for message in (b"", b"x" * 200):
                self.assertEqual(
                    control.hmac_sha256(key, message),
                    hmac_lib.new(key, message, hashlib.sha256).digest())

    def test_context_separates_message_and_result_macs(self):
        message = control.mac_input(1, DEVICE, LINK, BOOT, NONCE, 7, "op",
                                    "soft_reset", 5000, "")
        result = control.result_input(1, DEVICE, LINK, BOOT, NONCE, 7, "op",
                                      "soft_reset", "")
        self.assertNotEqual(control.hmac_sha256(KEY, message),
                            control.hmac_sha256(KEY, result))

    def test_length_prefix_rejects_oversized_fields(self):
        with self.assertRaises(ValueError):
            control.mac_input(1, "x" * 1025, LINK, BOOT, NONCE, 0, "op",
                              "soft_reset", 5000, "")


class GatewayAcceptTests(unittest.TestCase):
    def test_valid_command_executes_and_signs_scheduled_result(self):
        gateway, calls = make_gateway()
        gateway.handle(signed_message())
        self.assertEqual(calls, [(LINK, "soft_reset",
                                  {"reason": 0, "ttl_ms": 5000})])
        (result,) = gateway.take_results()
        self.assertEqual(result["status"], "scheduled")
        self.assertEqual(result["operation_id"], "op-0001")
        payload_b64 = result["result_payload_b64"]
        expected = hmac_lib.new(KEY, control.result_input(
            1, DEVICE, LINK, BOOT, NONCE, 7, "op-0001", "scheduled",
            payload_b64), hashlib.sha256).hexdigest()
        self.assertEqual(result["mac"], expected)
        self.assertEqual(gateway.pending_links(), [("op-0001", LINK)])

    def test_uppercase_mac_is_accepted(self):
        gateway, calls = make_gateway()
        message = signed_message()
        message["mac"] = message["mac"].upper()
        gateway.handle(message)
        self.assertEqual(len(calls), 1)

    def test_executor_refusal_becomes_signed_rejection(self):
        def refusing(_link, _command, _payload):
            raise ValueError("precondition_failed")

        gateway, _ = make_gateway(executor=refusing)
        gateway.handle(signed_message())
        (result,) = gateway.take_results()
        self.assertEqual(result["status"], "rejected")
        detail = json.loads(base64.b64decode(result["result_payload_b64"]))
        self.assertEqual(detail["reason"], "precondition_failed")


class GatewayFailClosedTests(unittest.TestCase):
    def assert_unauthenticated(self, gateway, message):
        before = gateway.rejected_unauthenticated
        gateway.handle(message)
        self.assertEqual(gateway.take_results(), [])
        self.assertEqual(gateway.rejected_unauthenticated, before + 1)

    def test_disabled_gateway_answers_nothing(self):
        gateway, calls = make_gateway(enabled=False)
        self.assert_unauthenticated(gateway, signed_message())
        self.assertEqual(calls, [])

    def test_missing_key_answers_nothing(self):
        gateway, calls = make_gateway(key_hex="")
        gateway.handle(signed_message())
        self.assertEqual(gateway.take_results(), [])
        self.assertEqual(calls, [])

    def test_tampered_fields_produce_no_signed_response(self):
        for field, value in (
                ("command", "motor_on"), ("ttl_ms", 4999),
                ("operation_id", "op-0002"), ("control_sequence", 8),
                ("payload_b64", ""), ("link_id", "bmcu-b"),
                ("device_id", "other"), ("session_nonce", "c" * 32),
                ("pico_boot_session", "d" * 32)):
            gateway, calls = make_gateway()
            message = signed_message()
            message[field] = value
            self.assert_unauthenticated(gateway, message)
            self.assertEqual(calls, [])

    def test_wrong_mac_produces_no_signed_response(self):
        gateway, _ = make_gateway()
        message = signed_message()
        message["mac"] = "0" * 64
        self.assert_unauthenticated(gateway, message)

    def test_structural_ttl_bounds(self):
        gateway, _ = make_gateway()
        for ttl in (0, 60001):
            self.assert_unauthenticated(gateway, signed_message(ttl_ms=ttl))
        message = signed_message()
        message["ttl_ms"] = "5000"
        self.assert_unauthenticated(gateway, message)

    def test_sequence_replay_is_rejected_signed(self):
        gateway, calls = make_gateway()
        gateway.handle(signed_message())
        gateway.take_results()
        gateway.handle(signed_message())
        (result,) = gateway.take_results()
        self.assertEqual(result["status"], "rejected")
        detail = json.loads(base64.b64decode(result["result_payload_b64"]))
        self.assertEqual(detail["reason"], "sequence_replay")
        self.assertEqual(len(calls), 1)

    def test_operation_replay_with_new_sequence_is_rejected(self):
        gateway, calls = make_gateway()
        gateway.handle(signed_message())
        gateway.take_results()
        gateway.handle(signed_message(sequence=8))
        (result,) = gateway.take_results()
        detail = json.loads(base64.b64decode(result["result_payload_b64"]))
        self.assertEqual(detail["reason"], "operation_replay")
        self.assertEqual(len(calls), 1)

    def test_wrong_session_is_rejected_signed(self):
        gateway, calls = make_gateway()
        message = signed_message(session_nonce="c" * 32)
        gateway.handle(message)
        (result,) = gateway.take_results()
        detail = json.loads(base64.b64decode(result["result_payload_b64"]))
        self.assertEqual(detail["reason"], "session_mismatch")
        self.assertEqual(calls, [])

    def test_soft_reset_payload_ttl_limit(self):
        gateway, calls = make_gateway()
        payload = base64.b64encode(
            json.dumps({"reason": 0, "ttl_ms": 6000}).encode()).decode()
        gateway.handle(signed_message(payload_b64=payload))
        (result,) = gateway.take_results()
        detail = json.loads(base64.b64decode(result["result_payload_b64"]))
        self.assertEqual(detail["reason"], "ttl_invalid")
        self.assertEqual(calls, [])

    def test_unknown_command_is_rejected(self):
        gateway, calls = make_gateway()
        gateway.handle(signed_message(command="motor_on"))
        (result,) = gateway.take_results()
        detail = json.loads(base64.b64decode(result["result_payload_b64"]))
        self.assertEqual(detail["reason"], "command_disabled")
        self.assertEqual(calls, [])

    def test_new_session_resets_replay_window(self):
        gateway, calls = make_gateway()
        gateway.handle(signed_message())
        gateway.take_results()
        nonce = gateway.new_session()
        self.assertNotEqual(nonce, NONCE)
        self.assertEqual(gateway.last_sequence, -1)
        self.assertEqual(gateway.pending_links(), [])


class GatewayTransitionTests(unittest.TestCase):
    def test_transitions_emit_signed_results_once(self):
        gateway, _ = make_gateway()
        gateway.handle(signed_message())
        gateway.take_results()
        gateway.report_transition("op-0001", "rebooted", {"boot_session": 2})
        gateway.report_transition("op-0001", "rebooted", {"boot_session": 2})
        gateway.report_transition("op-0001", "completed")
        results = gateway.take_results()
        self.assertEqual([item["status"] for item in results],
                         ["rebooted", "completed"])
        for item in results:
            expected = hmac_lib.new(KEY, control.result_input(
                1, DEVICE, LINK, BOOT, NONCE, 7, "op-0001", item["status"],
                item["result_payload_b64"]), hashlib.sha256).hexdigest()
            self.assertEqual(item["mac"], expected)
        self.assertEqual(gateway.pending_links(), [])

    def test_cancelled_removes_pending(self):
        gateway, _ = make_gateway()
        gateway.handle(signed_message())
        gateway.take_results()
        gateway.report_transition("op-0001", "cancelled", {"cancel_reason": 2})
        self.assertEqual(gateway.pending_links(), [])


class ControlConfigTests(unittest.TestCase):
    def make_config(self, tmp="control_test_config.json"):
        import os
        path = Path(tmp)
        if path.exists():
            os.remove(path)
        self.addCleanup(lambda: path.exists() and path.unlink())
        return config_module.BambuddyConfig(None, path=str(path))

    def test_control_disabled_by_default_and_key_never_echoed(self):
        config = self.make_config()
        public = config.public()
        self.assertFalse(public["control_enabled"])
        self.assertFalse(public["control_key_set"])
        self.assertNotIn("control_key", public)

    def test_enable_requires_key_and_key_is_validated(self):
        config = self.make_config()
        csrf = config.public()["csrf"]
        with self.assertRaises(ValueError):
            config.update({"csrf": csrf, "control_enabled": True})
        csrf = config.public()["csrf"]
        with self.assertRaises(ValueError):
            config.update({"csrf": csrf, "control_enabled": True,
                           "control_key": "zz" * 32})
        csrf = config.public()["csrf"]
        public = config.update({"csrf": csrf, "control_enabled": True,
                                "control_key": KEY_HEX.upper()})
        self.assertTrue(public["control_enabled"])
        self.assertTrue(public["control_key_set"])
        self.assertEqual(config.control_key, KEY_HEX)

    def test_clear_control_key_also_disables(self):
        config = self.make_config()
        csrf = config.public()["csrf"]
        config.update({"csrf": csrf, "control_enabled": True,
                       "control_key": KEY_HEX})
        csrf = config.public()["csrf"]
        public = config.update({"csrf": csrf, "clear_control_key": True})
        self.assertFalse(public["control_enabled"])
        self.assertFalse(public["control_key_set"])


if __name__ == "__main__":
    unittest.main()
