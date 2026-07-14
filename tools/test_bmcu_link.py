import unittest
from bmcu_link import *
class T(unittest.TestCase):
 def test_protocol_version(self): self.assertEqual(PROTOCOL_VERSION,2)
 def test_cobs(self): self.assertEqual(cobs_decode(cobs_encode(b'\x00a\x00b')),b'\x00a\x00b')
 def test_frame(self):
  f=decode_frame(encode_frame(KIND_GET_STATUS,0x1234)); self.assertEqual((f.kind,f.sequence,f.payload),(KIND_GET_STATUS,0x1234,b''))
 def test_crc(self):
  x=bytearray(encode_frame(KIND_GET_STATUS,1)); x[2]^=1
  with self.assertRaises(LinkError): decode_frame(bytes(x))
 def test_ping_token(self):
  token=(0x12345678).to_bytes(4,'little')
  f=decode_frame(encode_frame(KIND_PING,7,token))
  self.assertEqual((f.kind,f.sequence,f.payload),(KIND_PING,7,token))
if __name__=='__main__': unittest.main()
