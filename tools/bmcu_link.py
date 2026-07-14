"""Dependency-free BMCU Link protocol codec."""
from dataclasses import dataclass
from enum import IntEnum
import struct
PROTOCOL_VERSION=2; MAX_DECODED_FRAME=64
KIND_HELLO=1; KIND_STATUS=2; KIND_EVENT=3; KIND_GET_STATUS=0x10; KIND_SET_LED_MODE=0x11; KIND_PING=0x12; KIND_PONG=0x72; KIND_ACK=0x7f
CAP_STATUS_EVENTS=1<<0; CAP_LED_OVERRIDE=1<<1; CAP_PING_PONG=1<<2; CAP_RAW_HW_TICK=1<<3
ACK_OK=0; ACK_BAD_VALUE=1; ACK_UNSUPPORTED=2; ACK_BUSY=3; ACK_BAD_STATE=4; ACK_DENIED=5; ACK_EXPIRED=6; ACK_DUPLICATE=7; ACK_INTERNAL=8
class RecordType(IntEnum):
 BOOT=1; PRINTER_LINK=2; PRINTER_TRANSACTION=3; STATE_CHANGE=4; SENSOR=5; COMMAND_RESULT=6; SAFETY_DECISION=7; DIAGNOSTIC_COUNTER=8
class RecordSeverity(IntEnum): DEBUG=0; INFO=1; NOTICE=2; WARNING=3; ERROR=4; CRITICAL=5
class RecordSource(IntEnum): SYSTEM=0; PRINTER_BUS=1; MOTION=2; SENSOR=3; MANAGEMENT=4; SAFETY=5
class CommandOwner(IntEnum): NONE=0; PRINTER=1; USER=2; BMCU_LOCAL=3; SAFETY=4; SYSTEM=5
class TransactionOutcome(IntEnum): ACCEPTED=0; APPLIED=1; REPLIED=2; IGNORED=3; REJECTED=4; FAILED=5
class DecisionReason(IntEnum):
 OK=0; TARGET_MISMATCH=1; SLOT_RANGE=2; AMS_OFFLINE=3; INVALID_LENGTH=4; INVALID_CRC=5; UNSUPPORTED=6; NO_HANDLER=7; TX_BUSY=8; BAD_STATE=9; SAFETY_INHIBIT=10; INTERNAL=11; OWNER_CONFLICT=12; LEASE_EXPIRED=13
class SensorValidity(IntEnum): UNKNOWN=0; VALID=1; STALE=2; OFFLINE=3; FAULT=4
class LinkError(ValueError): pass
@dataclass(frozen=True)
class Frame: version:int; kind:int; sequence:int; payload:bytes
def crc16_ccitt_false(data):
 c=0xffff
 for v in data:
  c^=v<<8
  for _ in range(8): c=(((c<<1)^0x1021)&0xffff) if c&0x8000 else ((c<<1)&0xffff)
 return c
def cobs_encode(data):
 o=bytearray(b'\x00'); p=0; c=1
 for v in data:
  if v==0: o[p]=c; p=len(o); o.append(0); c=1
  else:
   o.append(v); c+=1
   if c==0xff: o[p]=c; p=len(o); o.append(0); c=1
 o[p]=c; return bytes(o)
def cobs_decode(data):
 if not data or 0 in data: raise LinkError('invalid COBS input')
 o=bytearray(); i=0
 while i<len(data):
  c=data[i]; i+=1; e=i+c-1
  if c==0 or e>len(data): raise LinkError('truncated COBS frame')
  o.extend(data[i:e]); i=e
  if c!=0xff and i<len(data): o.append(0)
 return bytes(o)
def encode_frame(kind,sequence,payload=b'',version=PROTOCOL_VERSION):
 if not(0<=kind<256 and 0<=version<256 and 0<=sequence<65536): raise LinkError('header value out of range')
 if len(payload)>MAX_DECODED_FRAME-7: raise LinkError('payload too large')
 b=struct.pack('<BBHB',version,kind,sequence,len(payload))+payload
 return cobs_encode(b+struct.pack('<H',crc16_ccitt_false(b)))+b'\x00'
def decode_frame(wire):
 if wire.endswith(b'\x00'): wire=wire[:-1]
 d=cobs_decode(wire)
 if not 7<=len(d)<=MAX_DECODED_FRAME: raise LinkError('decoded frame length is invalid')
 v,k,s,n=struct.unpack('<BBHB',d[:5])
 if n+7!=len(d): raise LinkError('payload length does not match frame')
 if crc16_ccitt_false(d[:-2])!=struct.unpack('<H',d[-2:])[0]: raise LinkError('CRC mismatch')
 return Frame(v,k,s,d[5:-2])
def kind_name(k): return {1:'HELLO',2:'STATUS',3:'EVENT',0x10:'GET_STATUS',0x11:'SET_LED_MODE',0x12:'PING',0x72:'PONG',0x7f:'ACK'}.get(k,f'UNKNOWN_0x{k:02X}')
