"""Dependency-free BMCU Link protocol codec."""
from dataclasses import dataclass
from enum import IntEnum
import struct
PROTOCOL_PRERELEASE=0x80; PROTOCOL_REVISION=3; PROTOCOL_VERSION=PROTOCOL_PRERELEASE|PROTOCOL_REVISION; MAX_DECODED_FRAME=64; SYNC=b'\xA5\x5A'
KIND_HELLO=1; KIND_STATUS=2; KIND_EVENT=3; KIND_GET_STATUS=0x10; KIND_SET_LED_MODE=0x11; KIND_PING=0x12; KIND_GET_FULL_STATUS=0x17; KIND_PONG=0x72; KIND_FULL_STATUS_RECORD=0x73; KIND_ACK=0x7f
CAP_STATUS_EVENTS=1<<0; CAP_LED_OVERRIDE=1<<1; CAP_PING_PONG=1<<2; CAP_RAW_HW_TICK=1<<3; CAP_PRINTER_TRACE=1<<4; CAP_FULL_STATUS=1<<6
FULL_SECTION_GLOBAL=1<<0; FULL_SECTION_CHANNELS=1<<1; FULL_SECTION_PRINTER_BUS=1<<2; FULL_SECTION_COUNTERS=1<<3; FULL_SECTION_ALL=0x0f
FULL_RECORD_GLOBAL=1; FULL_RECORD_CHANNEL=2; FULL_RECORD_PRINTER_BUS=3; FULL_RECORD_COUNTERS=4; FULL_RECORD_PRINTER_AUTH=5
ACK_OK=0; ACK_BAD_VALUE=1; ACK_UNSUPPORTED=2; ACK_BUSY=3; ACK_BAD_STATE=4; ACK_DENIED=5; ACK_EXPIRED=6; ACK_DUPLICATE=7; ACK_INTERNAL=8
class RecordType(IntEnum):
 BOOT=1; PRINTER_LINK=2; PRINTER_TRANSACTION=3; STATE_CHANGE=4; SENSOR=5; COMMAND_RESULT=6; SAFETY_DECISION=7; DIAGNOSTIC_COUNTER=8; PRINTER_LONG_TRANSACTION=9
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
@dataclass(frozen=True)
class FullStatusRecord: snapshot_id:int; index:int; count:int; record_type:int; flags:int; hw_tick32:int; data:bytes
@dataclass(frozen=True)
class EventRecord: hw_tick32:int; record_type:int; severity:int; source:int; payload_length:int; data:bytes
@dataclass(frozen=True)
class PrinterAuthTrace: last_type:int; count_040d:int; count_040e:int; payload_length:int; hw_tick32:int; outcome:int; reason:int; response_length:int; payload_hash:int
@dataclass(frozen=True)
class PrinterLongTransaction: frame_type:int; owner:int; outcome:int; reason:int; request_length:int; response_length:int; payload_hash:int
def crc16_ccitt_false(data):
 c=0xffff
 for v in data:
  c^=v<<8
  for _ in range(8): c=(((c<<1)^0x1021)&0xffff) if c&0x8000 else ((c<<1)&0xffff)
 return c
def encode_frame(kind,sequence,payload=b'',version=PROTOCOL_VERSION):
 if not(0<=kind<256 and 0<=version<256 and 0<=sequence<65536): raise LinkError('header value out of range')
 if len(payload)>MAX_DECODED_FRAME-7: raise LinkError('payload too large')
 body=struct.pack('<BBHB',version,kind,sequence,len(payload))+payload
 return SYNC+body+struct.pack('<H',crc16_ccitt_false(body))
def decode_frame(wire):
 if not wire.startswith(SYNC): raise LinkError('sync header mismatch')
 d=wire[len(SYNC):]
 if not 7<=len(d)<=MAX_DECODED_FRAME: raise LinkError('frame length is invalid')
 v,k,s,n=struct.unpack('<BBHB',d[:5])
 if v!=PROTOCOL_VERSION: raise LinkError('unsupported protocol version')
 if n+7!=len(d): raise LinkError('payload length does not match frame')
 if crc16_ccitt_false(d[:-2])!=struct.unpack('<H',d[-2:])[0]: raise LinkError('CRC mismatch')
 return Frame(v,k,s,d[5:-2])
def decode_full_status_record(payload):
 if len(payload)!=26: raise LinkError('FULL_STATUS_RECORD payload must be 26 bytes')
 snapshot_id,index,count,record_type,flags,hw_tick32=struct.unpack('<HBBBBI',payload[:10])
 if count==0 or index>=count: raise LinkError('invalid full-status record index/count')
 return FullStatusRecord(snapshot_id,index,count,record_type,flags,hw_tick32,payload[10:])
def decode_event_record(payload):
 if len(payload)!=16: raise LinkError('EVENT payload must be 16 bytes')
 hw_tick32,record_type,severity,source,payload_length=struct.unpack('<IBBBB',payload[:8])
 if payload_length>8: raise LinkError('event payload length exceeds union')
 return EventRecord(hw_tick32,record_type,severity,source,payload_length,payload[8:8+payload_length])
def decode_printer_auth_trace(data):
 if len(data)!=16: raise LinkError('PRINTER_AUTH record data must be 16 bytes')
 frame_type,count_040d,count_040e,payload_length,hw_tick32,outcome,reason,response_length,payload_hash=struct.unpack('<HHHHIBBBB',data)
 return PrinterAuthTrace(frame_type,count_040d,count_040e,payload_length,hw_tick32,outcome,reason,response_length,payload_hash)
def decode_printer_long_transaction(event):
 if event.record_type!=RecordType.PRINTER_LONG_TRANSACTION or event.payload_length!=8: raise LinkError('event is not a PRINTER_LONG_TRANSACTION')
 frame_type,owner,outcome,reason,request_length,response_length,payload_hash=struct.unpack('<HBBBBBB',event.data)
 return PrinterLongTransaction(frame_type,owner,outcome,reason,request_length,response_length,payload_hash)
def kind_name(k): return {1:'HELLO',2:'STATUS',3:'EVENT',0x10:'GET_STATUS',0x11:'SET_LED_MODE',0x12:'PING',0x17:'GET_FULL_STATUS',0x72:'PONG',0x73:'FULL_STATUS_RECORD',0x7f:'ACK'}.get(k,f'UNKNOWN_0x{k:02X}')
