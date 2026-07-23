import importlib.util
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import bmcu_link as host_link


def load_pico_link():
    spec = importlib.util.spec_from_file_location("pico_bmcu_link", ROOT / "pico" / "bmcu_link.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ProtocolVectorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pico_link = load_pico_link()
        cls.corpus = json.loads((ROOT / "ci" / "protocol_alpha3_vectors.json").read_text())

    def test_corpus_covers_every_implemented_kind(self):
        expected = {0x01, 0x02, 0x03, 0x10, 0x11, 0x12, 0x17, 0x72, 0x73, 0x7F}
        actual = {int(vector["kind"], 16) for vector in self.corpus["vectors"]}
        self.assertEqual(actual, expected)

    def test_host_encoder_and_decoder_match_corpus(self):
        for vector in self.corpus["vectors"]:
            with self.subTest(vector=vector["name"]):
                kind = int(vector["kind"], 16)
                payload = bytes.fromhex(vector["payload_hex"])
                wire = bytes.fromhex(vector["wire_hex"])
                self.assertEqual(host_link.encode_frame(kind, vector["sequence"], payload), wire)
                decoded = host_link.decode_frame(wire)
                self.assertEqual((decoded.kind, decoded.sequence, decoded.payload),
                                 (kind, vector["sequence"], payload))

    def test_pico_encoder_and_stream_decoder_match_corpus(self):
        for vector in self.corpus["vectors"]:
            with self.subTest(vector=vector["name"]):
                kind = int(vector["kind"], 16)
                payload = bytes.fromhex(vector["payload_hex"])
                wire = bytes.fromhex(vector["wire_hex"])
                self.assertEqual(self.pico_link.encode_frame(kind, vector["sequence"], payload), wire)
                decoder = self.pico_link.FrameDecoder()
                frames = decoder.feed(wire[:3]) + decoder.feed(wire[3:])
                self.assertEqual(len(frames), 1)
                self.assertEqual((frames[0]["kind"], frames[0]["sequence"], frames[0]["payload"]),
                                 (kind, vector["sequence"], payload))


if __name__ == "__main__":
    unittest.main()
