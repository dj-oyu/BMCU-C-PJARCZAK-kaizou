import unittest
from bmcu_link import *
class T(unittest.TestCase):
 def test_protocol_version(self): self.assertEqual(PROTOCOL_VERSION,0x83)
 def test_sync_header(self): self.assertTrue(encode_frame(KIND_GET_STATUS,1).startswith(SYNC))
 def test_frame(self):
  payload=b'\x00\xa5\x5a\x00'
  f=decode_frame(encode_frame(KIND_GET_STATUS,0x1234,payload)); self.assertEqual((f.kind,f.sequence,f.payload),(KIND_GET_STATUS,0x1234,payload))
 def test_wire_size_is_payload_plus_nine(self):
  self.assertEqual(len(encode_frame(KIND_PING,1,b'abcd')),13)
 def test_rejects_old_version(self):
  with self.assertRaises(LinkError): decode_frame(encode_frame(KIND_GET_STATUS,1,version=0x82))
 def test_rejects_bad_sync(self):
  wire=bytearray(encode_frame(KIND_GET_STATUS,1)); wire[0]=0
  with self.assertRaises(LinkError): decode_frame(bytes(wire))
 def test_crc(self):
  x=bytearray(encode_frame(KIND_GET_STATUS,1)); x[2]^=1
  with self.assertRaises(LinkError): decode_frame(bytes(x))
 def test_ping_token(self):
  token=(0x12345678).to_bytes(4,'little')
  f=decode_frame(encode_frame(KIND_PING,7,token))
  self.assertEqual((f.kind,f.sequence,f.payload),(KIND_PING,7,token))
 def test_full_status_record(self):
  payload=(7).to_bytes(2,'little')+bytes([2,4,FULL_RECORD_CHANNEL,0])+(123).to_bytes(4,'little')+bytes(range(16))
  r=decode_full_status_record(payload)
  self.assertEqual((r.snapshot_id,r.index,r.count,r.record_type,r.hw_tick32),(7,2,4,FULL_RECORD_CHANNEL,123))
  self.assertEqual(r.data,bytes(range(16)))
 def test_event_record(self):
  payload=(99).to_bytes(4,'little')+bytes([4,2,3,6])+b'abcdef'+b'\x00\x00'
  r=decode_event_record(payload)
  self.assertEqual((r.hw_tick32,r.record_type,r.severity,r.source),(99,4,2,3))
  self.assertEqual(r.data,b'abcdef')
 def test_rejects_bad_record_bounds(self):
  with self.assertRaises(LinkError): decode_full_status_record(b'\x00'*26)
if __name__=='__main__': unittest.main()
