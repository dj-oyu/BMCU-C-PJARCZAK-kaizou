"""Bounded non-blocking HTTP server for static UI and BMB1 binary APIs."""

import socket
import time
try:
    import errno
except ImportError:
    import uerrno as errno

MAX_REQUEST_BYTES = 2048
MAX_BODY_BYTES = 256
MAX_RECV_BYTES = 256
MAX_SEND_BYTES = 256
HTTP_LISTEN_BACKLOG = 4
BINARY_TYPE = "application/vnd.bmcu-monitor.v1"


def _would_block(error):
    code = error.args[0] if error.args else None
    return code in (
        getattr(errno, "EAGAIN", -1),
        getattr(errno, "EWOULDBLOCK", -1),
    )


PAGE = b"""<!doctype html>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>Dual BMCU Loader Monitor</title>
<style>
:root{color-scheme:dark;--bg:#081019;--panel:#111d2b;--panel2:#17263a;--line:#29415c;--text:#edf6ff;--muted:#91a6bb;--a:#54c8ff;--b:#b88cff;--ok:#47d7a0;--warn:#ffc75a;--bad:#ff6f7d}
*{box-sizing:border-box}body{max-width:1180px;margin:auto;padding:20px;font:14px/1.45 system-ui;background:radial-gradient(circle at top,#10233a 0,var(--bg) 42%);color:var(--text)}header{display:flex;justify-content:space-between;align-items:flex-start;gap:16px;margin-bottom:24px}.kicker{margin:0;color:var(--a);font-size:.72rem;font-weight:700;letter-spacing:.16em;text-transform:uppercase}h1{margin:.15rem 0;font-size:clamp(1.6rem,4vw,2.5rem)}h2{margin:24px 0 10px;font-size:1rem;color:#c9d9e8;text-transform:uppercase;letter-spacing:.08em}h3{margin:0;font-size:1.15rem}.muted{color:var(--muted)}.badge,.pill{display:inline-flex;align-items:center;border:1px solid var(--line);border-radius:999px;padding:.35rem .65rem;background:#0c1724;font-size:.76rem;font-weight:700}.ok{color:var(--ok);border-color:#26785d}.warn{color:var(--warn);border-color:#816624}.bad{color:var(--bad);border-color:#813543}.loaders{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px}.loader{position:relative;overflow:hidden;border:1px solid var(--line);border-radius:16px;background:linear-gradient(145deg,var(--panel2),var(--panel));padding:16px;box-shadow:0 18px 48px #0005}.loader:before{content:'';position:absolute;inset:0 auto 0 0;width:4px;background:var(--accent)}.loader-head{display:flex;justify-content:space-between;gap:12px;align-items:flex-start;margin-bottom:13px}.loader-name{display:flex;align-items:center;gap:8px}.dot{width:9px;height:9px;border-radius:50%;background:var(--accent);box-shadow:0 0 14px var(--accent)}.summary-grid,.comm-grid,.bridge-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:8px}.summary-grid{margin-bottom:12px}.metric{min-width:0;border:1px solid #233a53;border-radius:10px;background:#0d1825;padding:9px}.metric span{display:block;color:var(--muted);font-size:.68rem;text-transform:uppercase;letter-spacing:.04em}.metric strong{display:block;overflow:hidden;text-overflow:ellipsis;font-size:1rem;margin-top:2px}.slots{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:8px}.slot{border:1px solid #29415c;border-radius:12px;background:#0d1825;padding:10px;min-height:118px}.slot.selected{border-color:var(--accent);box-shadow:inset 0 0 0 1px var(--accent)}.slot.offline{opacity:.58}.slot-title{display:flex;justify-content:space-between;font-weight:750;margin-bottom:7px}.slot dl{display:grid;grid-template-columns:auto 1fr;gap:3px 7px;margin:0}.slot dt{color:var(--muted)}.slot dd{margin:0;text-align:right}.comm-grid{margin-top:12px}.bridge-grid{grid-template-columns:repeat(4,minmax(130px,1fr))}pre{max-height:230px;overflow:auto;white-space:pre-wrap;border:1px solid var(--line);border-radius:12px;background:#07101a;padding:12px;color:#b9cbe0;font-size:.75rem}.empty{border:1px dashed var(--line);border-radius:12px;padding:24px;text-align:center;color:var(--muted)}.settings-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px}.key-card{border:1px solid var(--line);border-radius:16px;background:linear-gradient(145deg,var(--panel2),var(--panel));padding:16px}.key-top{display:flex;justify-content:space-between;gap:12px;align-items:flex-start}.key-card label{display:block;margin:14px 0 6px;color:var(--muted);font-size:.78rem}.key-row{display:flex;gap:8px}.endpoint-row{display:grid;grid-template-columns:minmax(0,1fr) 120px;gap:8px}.key-row input,.endpoint-row input{min-width:0;flex:1;border:1px solid #36516f;border-radius:10px;background:#07101a;color:var(--text);padding:11px 12px;font:13px ui-monospace,monospace;letter-spacing:.04em}.key-row input:focus,.endpoint-row input:focus{outline:2px solid var(--a);outline-offset:1px}.actions{display:flex;flex-wrap:wrap;gap:8px;margin-top:10px}button{border:1px solid #36516f;border-radius:9px;background:#172a40;color:var(--text);padding:9px 12px;font-weight:700;cursor:pointer}button.primary{background:#14759a;border-color:var(--a)}button:disabled{opacity:.55;cursor:wait}.key-note{margin:8px 0 0;font-size:.78rem}
@media(max-width:850px){.loaders,.settings-grid{grid-template-columns:1fr}.summary-grid,.comm-grid{grid-template-columns:repeat(2,1fr)}}@media(max-width:520px){body{padding:14px}header{display:block}header>.badge{margin-top:10px}.slots{grid-template-columns:repeat(2,1fr)}.bridge-grid{grid-template-columns:repeat(2,1fr)}.endpoint-row{grid-template-columns:1fr}}
</style>
<header><div><p class=kicker>Pico 2 W / BMB1 live diagnostics</p><h1>Dual BMCU Loader Monitor</h1><p id=summary class=muted>Connecting to the Pico...</p></div><span id=health class=badge>Connecting</span></header>
<main><section><h2>Loaders</h2><div id=loaders class=loaders><div class=empty>Waiting for BMCU STATUS...</div></div></section><section><h2>Bridge health</h2><div id=bridge class=bridge-grid></div></section><section><h2>Connection settings</h2><div class=settings-grid><article class=key-card><div class=key-top><div><h3>Bambuddy endpoint</h3><div class=muted>Configure the BMB1 TCP destination used by this Pico.</div></div><span id=endpoint-state class=badge>Checking</span></div><div class=endpoint-row><div><label for=bambuddy-host>Host or IPv4 address</label><input id=bambuddy-host maxlength=240 autocomplete=off spellcheck=false placeholder="bambuddy.local"></div><div><label for=bambuddy-port>TCP port</label><input id=bambuddy-port type=number min=1 max=65535 inputmode=numeric placeholder=8766></div></div><div class=actions><button id=save-endpoint class=primary type=button>Save endpoint &amp; reconnect</button></div><p id=endpoint-message class="muted key-note">Saved locally; UI values override config.py.</p></article><article class=key-card><div class=key-top><div><h3>BMB1 device key</h3><div class=muted>Generate or paste a 256-bit key, copy it to Bambuddy, then save it on this Pico.</div></div><span id=key-state class=badge>Checking</span></div><label for=device-key>64 hexadecimal characters</label><div class=key-row><input id=device-key type=password maxlength=64 inputmode=text autocomplete=new-password spellcheck=false placeholder="Generate a new key or paste an existing key"><button id=show-key type=button>Show</button></div><div class=actions><button id=generate-key type=button>Generate new</button><button id=copy-key type=button>Copy</button><button id=save-key class=primary type=button>Save key &amp; reconnect</button></div><p id=key-fingerprint class="muted key-note">Fingerprint: --</p><p id=key-message class="muted key-note">Stored keys are write-only and cannot be read back.</p></article></div></section><section><h2>Recent device log</h2><pre id=logs>No runtime log</pre></section></main>
<script>
const $=id=>document.getElementById(id),td=new TextDecoder(),u64=(v,o)=>v.getBigUint64(o).toString(),n=x=>Number(x===undefined?0:x),fmt=x=>n(x).toLocaleString(),bit=(m,i)=>((m>>i)&1)!==0;
function frames(b){let v=new DataView(b),a=[],o=0;while(o+32<=b.byteLength){if(v.getUint32(o)!==0x424d4231||v.getUint8(o+4)!==1)break;let z=v.getUint32(o+8);if(z>4096||o+32+z>b.byteLength)break;a.push({t:v.getUint8(o+5),l:v.getUint8(o+28),v:new DataView(b,o+32,z)});o+=32+z}return a}
function tlvs(v){let m={},o=0;while(o+4<=v.byteLength){let t=v.getUint8(o),k=v.getUint8(o+1),z=v.getUint16(o+2);o+=4;if(o+z>v.byteLength)break;m[t]=k===4&&z===8?u64(v,o):(k===7&&z===4?v.getInt32(o):td.decode(new Uint8Array(v.buffer,v.byteOffset+o,z)));o+=z}return m}
function status(v){if(v.byteLength<44||v.getUint8(13)!==2)return null;let motion=[],pull=[];for(let i=0;i<4;i++){motion.push(v.getUint8(32+i));pull.push(v.getUint8(36+i))}return{selected:v.getUint8(29),inserted:v.getUint8(30),online:v.getUint8(31),motion,pull}}
function comm(link,m){let b=64+link*8;return{backlog:n(m[b]),peak:n(m[b+1]),rx:n(m[b+2]),crc:n(m[b+3]),frame:n(m[b+4]),gaps:n(m[b+5]),delay:n(m[b+7]),overflow:n(m[81+link*2])}}
function metric(label,value,cls=''){return'<div class="metric '+cls+'"><span>'+label+'</span><strong>'+value+'</strong></div>'}
function slotCard(s,i){let selected=s.selected===i,online=bit(s.online,i),inserted=bit(s.inserted,i);return'<article class="slot '+(selected?'selected ':'')+(online?'':'offline')+'"><div class=slot-title><span>Slot '+(i+1)+'</span>'+(selected?'<span class="pill ok">Selected</span>':'')+'</div><dl><dt>Filament</dt><dd>'+(inserted?'Present':'Empty')+'</dd><dt>Online</dt><dd class='+(online?'ok':'bad')+'>'+(online?'Yes':'No')+'</dd><dt>Motion</dt><dd>'+s.motion[i]+'</dd><dt>Pull</dt><dd>'+s.pull[i]+'%</dd></dl></article>'}
function loaderCard(link,s,m){let c=comm(link,m),name='bmcu-'+String.fromCharCode(97+link),pins=link===0?'GP0 TX / GP1 RX':'GP4 TX / GP5 RX',errors=c.crc+c.frame+c.overflow,accent=link===0?'var(--a)':'var(--b)',online=s!==null,selected=online&&s.selected<4?'Slot '+(s.selected+1):'None',onlineSlots=online?[0,1,2,3].filter(i=>bit(s.online,i)).length:0;return'<article class=loader style="--accent:'+accent+'"><div class=loader-head><div><div class=loader-name><span class=dot></span><h3>'+name+'</h3></div><div class=muted>UART'+link+' / '+pins+'</div></div><span class="badge '+(online?'ok':'bad')+'">'+(online?'Receiving':'Waiting')+'</span></div><div class=summary-grid>'+metric('Selected',selected)+metric('Online slots',onlineSlots+' / 4')+metric('RX bytes',fmt(c.rx))+metric('Errors',fmt(errors),errors?'warn':'ok')+'</div>'+(online?'<div class=slots>'+[0,1,2,3].map(i=>slotCard(s,i)).join('')+'</div>':'<div class=empty>No STATUS received on UART'+link+'</div>')+'<div class=comm-grid>'+metric('Backlog / peak',fmt(c.backlog)+' / '+fmt(c.peak),c.peak>=4096?'warn':'')+metric('CRC / frame',fmt(c.crc)+' / '+fmt(c.frame),c.crc+c.frame?'warn':'ok')+metric('Sequence gaps',fmt(c.gaps),c.gaps?'warn':'ok')+metric('Overflows',fmt(c.overflow),c.overflow?'warn':'ok')+'</div></article>'}
function bridgeCards(m){let cards=[['Uptime',Math.floor(n(m[1])/1000)+' s',''],['Heap free',fmt(m[4])+' B',n(m[4])<20000?'warn':''],['Wi-Fi RSSI',m[17]===undefined?'--':m[17]+' dBm',''],['Loop p99',fmt(m[14])+' us',n(m[14])>100000?'warn':''],['Exceptions',fmt(m[48]),n(m[48])?'bad':'ok'],['BMB1 queue',fmt(m[32]),n(m[32])>128?'warn':''],['Transport drops',fmt(m[33]),n(m[33])?'warn':'ok'],['TCP reconnects',fmt(m[27]),'']];return cards.map(x=>metric(x[0],x[1],x[2])).join('')}
function logLine(v){if(v.byteLength<22)return null;let q=BigInt(u64(v,0)),up=u64(v,8),sev=v.getUint8(16),cn=v.getUint8(17),mn=v.getUint16(18),o=22,component=td.decode(new Uint8Array(v.buffer,v.byteOffset+o,cn));o+=cn;let message=td.decode(new Uint8Array(v.buffer,v.byteOffset+o,mn));return{q,text:'['+up+' ms] '+sev+' '+component+': '+message}}
async function get(path){let r=await fetch(path,{cache:'no-store'});if(!r.ok)throw Error('HTTP '+r.status);return frames(await r.arrayBuffer())}
function keyMessage(text,cls='muted'){let e=$('key-message');e.textContent=text;e.className=cls+' key-note'}function endpointMessage(text,cls='muted'){let e=$('endpoint-message');e.textContent=text;e.className=cls+' key-note'}
async function loadEndpoint(){try{let r=await fetch('/api/transport/status',{cache:'no-store'});if(!r.ok)throw Error('HTTP '+r.status);let values={};for(let line of (await r.text()).trim().split('\\n')){let i=line.indexOf('=');if(i>0)values[line.slice(0,i)]=line.slice(i+1)}let configured=values.configured==='1';$('endpoint-state').textContent=configured?'Configured':'Not configured';$('endpoint-state').className='badge '+(configured?'ok':'bad');$('bambuddy-host').value=values.host||'';$('bambuddy-port').value=values.port||''}catch(e){$('endpoint-state').textContent='Unavailable';$('endpoint-state').className='badge bad';endpointMessage('Cannot read endpoint: '+e,'bad');setTimeout(loadEndpoint,1000)}}
async function saveEndpoint(){let host=$('bambuddy-host').value.trim(),port=Number($('bambuddy-port').value),button=$('save-endpoint');if(!/^[A-Za-z0-9._-]{1,240}$/.test(host)||host.includes('..')){endpointMessage('Enter an IPv4 address or DNS name without a URL scheme.','bad');return}if(!Number.isInteger(port)||port<1||port>65535){endpointMessage('Port must be between 1 and 65535.','bad');return}button.disabled=true;try{let r=await fetch('/api/transport',{method:'POST',headers:{'Content-Type':'application/octet-stream','X-BMCU-Settings-Action':'update'},body:host+'\\n'+port});if(!r.ok)throw Error((await r.text()).trim()||'HTTP '+r.status);endpointMessage('Saved. BMB1 is reconnecting to '+host+':'+port+'.','ok');await loadEndpoint()}catch(e){endpointMessage('Save failed: '+e,'bad')}finally{button.disabled=false}}
async function loadKeyStatus(){try{let r=await fetch('/api/device-key/status',{cache:'no-store'});if(!r.ok)throw Error('HTTP '+r.status);let values={};for(let line of (await r.text()).trim().split('\\n')){let i=line.indexOf('=');if(i>0)values[line.slice(0,i)]=line.slice(i+1)}let configured=values.configured==='1';$('key-state').textContent=configured?'Configured':'Not configured';$('key-state').className='badge '+(configured?'ok':'bad');$('key-fingerprint').textContent='Fingerprint: '+(values.fingerprint||'--')}catch(e){$('key-state').textContent='Unavailable';$('key-state').className='badge bad';keyMessage('Cannot read key status: '+e,'bad');setTimeout(loadKeyStatus,1000)}}
function generateKey(){let bytes=new Uint8Array(32);crypto.getRandomValues(bytes);$('device-key').value=Array.from(bytes,x=>x.toString(16).padStart(2,'0')).join('');$('device-key').type='text';$('show-key').textContent='Hide';keyMessage('New key generated. Copy it before saving; it cannot be retrieved later.','warn')}
async function copyKey(){let field=$('device-key'),value=field.value.trim();if(!/^[0-9a-fA-F]{64}$/.test(value)){keyMessage('Enter or generate exactly 64 hexadecimal characters.','bad');return}try{await navigator.clipboard.writeText(value)}catch(e){field.type='text';field.select();document.execCommand('copy')}keyMessage('Key copied. Store the same value in Bambuddy before or after saving.','ok')}
async function saveKey(){let field=$('device-key'),value=field.value.trim().toLowerCase(),button=$('save-key');if(!/^[0-9a-f]{64}$/.test(value)){keyMessage('Enter or generate exactly 64 hexadecimal characters.','bad');return}button.disabled=true;try{let r=await fetch('/api/device-key',{method:'POST',headers:{'Content-Type':'application/octet-stream','X-BMCU-Key-Action':'update'},body:value});if(!r.ok)throw Error((await r.text()).trim()||'HTTP '+r.status);field.value='';field.type='password';$('show-key').textContent='Show';keyMessage('Saved. BMB1 is reconnecting with the new key.','ok');await loadKeyStatus()}catch(e){keyMessage('Save failed: '+e,'bad')}finally{button.disabled=false}}
$('save-endpoint').onclick=saveEndpoint;$('generate-key').onclick=generateKey;$('copy-key').onclick=copyKey;$('save-key').onclick=saveKey;$('show-key').onclick=()=>{let field=$('device-key'),show=field.type==='password';field.type=show?'text':'password';$('show-key').textContent=show?'Hide':'Show'};
let last=0n,logText='';async function refresh(){try{let cur=await get('/api/current.bin'),diag=await get('/api/diagnostics.bin'),m={};for(let f of diag)if(f.t===19)Object.assign(m,tlvs(f.v));let states={};for(let f of cur)if(f.t===16)states[f.l]=status(f.v);let links=[0,1].filter(i=>states[i]||m[64+i*8]!==undefined);if(!links.length)links=[0,1];$('loaders').innerHTML=links.map(i=>loaderCard(i,states[i]||null,m)).join('');$('bridge').innerHTML=bridgeCards(m);let receiving=links.filter(i=>states[i]).length,all=receiving===links.length;$('health').textContent=receiving+' / '+links.length+' receiving';$('health').className='badge '+(all?'ok':receiving?'warn':'bad');$('summary').textContent='Live binary STATUS and per-UART health / refresh 3 s';let logs=await get('/api/logs.bin?after='+last.toString()+'&limit=24'),lines=[];for(let f of logs)if(f.t===20){let x=logLine(f.v);if(x){if(x.q>last)last=x.q;lines.push(x.text)}}if(lines.length){logText=(lines.join('\\n')+'\\n'+logText).slice(0,12000);$('logs').textContent=logText}}catch(e){$('health').textContent='Refresh failed';$('health').className='badge bad';$('summary').textContent='Web UI error: '+e}}async function start(){await loadEndpoint();await loadKeyStatus();await refresh();setInterval(refresh,3000)}start()
</script>"""


class _Response:
    def __init__(self, header, body):
        self.parts = [header]
        if isinstance(body, (list, tuple)):
            self.parts.extend(body)
        else:
            self.parts.append(body)
        self.offset = 0

    def current(self, maximum=None):
        while self.parts and self.offset >= len(self.parts[0]):
            self.parts.pop(0)
            self.offset = 0
        if not self.parts:
            return b""
        value = memoryview(self.parts[0])[self.offset:]
        return value[:maximum] if maximum and len(value) > maximum else value

    def consume(self, count):
        while count and self.parts:
            remaining = len(self.parts[0]) - self.offset
            if count < remaining:
                self.offset += count
                return
            count -= remaining
            self.parts.pop(0)
            self.offset = 0

    def done(self):
        return not self.parts

    def __contains__(self, value):
        return any(value in part for part in self.parts)


class WebUI:
    def __init__(self, binary_provider, port=80, error_handler=None,
                 settings_provider=None):
        self.binary_provider = binary_provider
        self.error_handler = error_handler
        self.settings_provider = settings_provider
        self.port = port
        self.server = None
        self.servers = []
        self.client = None
        self.request = bytearray(MAX_REQUEST_BYTES)
        self.request_view = memoryview(self.request)
        self.request_length = 0
        self.request_meta = None
        self.response = None
        self.close_at_ms = None
        self.client_deadline_ms = None

    @staticmethod
    def _listener(family, address, port, ipv6_only=False):
        server = socket.socket(family, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if ipv6_only:
            server.setsockopt(41, 27, 1)
        server.bind((address, port))
        server.listen(HTTP_LISTEN_BACKLOG)
        server.setblocking(False)
        return server

    def start(self):
        try:
            self.servers.append(self._listener(
                socket.AF_INET6, "::", self.port, True))
        except (AttributeError, OSError):
            pass
        self.server = self._listener(socket.AF_INET, "0.0.0.0", self.port)
        self.servers.append(self.server)

    @staticmethod
    def _http_response(status, content_type, body):
        size = sum(len(item) for item in body) if isinstance(
            body, (list, tuple)) else len(body)
        header = ("HTTP/1.1 %s\r\nContent-Type: %s\r\nContent-Length: %d\r\n"
                  "Cache-Control: no-store\r\nX-Content-Type-Options: nosniff\r\n"
                  "Referrer-Policy: no-referrer\r\nConnection: close\r\n\r\n" %
                  (status, content_type, size)).encode()
        return _Response(header, body)

    def _request_metadata(self):
        if self.request_meta is not None:
            return self.request_meta
        marker = self.request.find(b"\r\n\r\n", 0, self.request_length)
        if marker < 0:
            return None
        lines = bytes(self.request_view[:marker]).split(b"\r\n")
        request_line = lines[0].split() if lines else ()
        method = request_line[0] if request_line else b""
        path = request_line[1] if len(request_line) > 1 else b""
        headers = {}
        for line in lines[1:]:
            if b":" not in line:
                continue
            name, value = line.split(b":", 1)
            headers[name.strip().lower().decode()] = value.strip().decode()
        try:
            length = int(headers.get("content-length", "0"))
        except ValueError:
            length = -1
        self.request_meta = method, path, headers, length, marker + 4
        return self.request_meta

    def _request_complete(self):
        metadata = self._request_metadata()
        if metadata is None:
            return False
        length, body_start = metadata[3], metadata[4]
        return length < 0 or length > MAX_BODY_BYTES or \
            self.request_length - body_start >= length

    def _finish_request(self):
        method, path, headers, length, body_start = \
            self._request_metadata()
        if length < 0:
            self.response = self._http_response(
                "400 Bad Request", "text/plain", b"invalid content length\n")
            return
        if length > MAX_BODY_BYTES:
            self.response = self._http_response(
                "413 Payload Too Large", "text/plain", b"body too large\n")
            return
        body = bytes(self.request_view[body_start:body_start + length]) \
            if length else b""
        if (path.startswith(b"/api/device-key") or
                path.startswith(b"/api/transport")) and self.settings_provider:
            result = self.settings_provider(
                method.decode(), path.decode(), headers, body)
            self.response = self._http_response(*result) if result else \
                self._http_response("404 Not Found", "text/plain",
                                    b"Not found\n")
        elif method != b"GET":
            self.response = self._http_response(
                "405 Method Not Allowed", "text/plain", b"GET only\n")
        elif path == b"/":
            self.response = self._http_response(
                "200 OK", "text/html; charset=utf-8", PAGE)
        elif path.startswith(b"/api/") and b".bin" in path:
            value = self.binary_provider(path.decode())
            self.response = self._http_response(
                "200 OK", BINARY_TYPE, value) if value is not None else \
                self._http_response("404 Not Found", "text/plain",
                                    b"Not found\n")
        else:
            self.response = self._http_response(
                "404 Not Found", "text/plain", b"Not found\n")

    def _close_client(self):
        if self.client:
            try:
                self.client.close()
            except OSError:
                pass
        self.client = None
        self.request_length = 0
        self.request_meta = None
        self.response = None
        self.close_at_ms = None
        self.client_deadline_ms = None

    @staticmethod
    def _now_ms():
        return time.ticks_ms() if hasattr(time, "ticks_ms") else int(
            time.monotonic() * 1000)

    def _touch(self):
        self.client_deadline_ms = self._now_ms() + 3000

    def poll(self):
        now = self._now_ms()
        if self.client and self.client_deadline_ms is not None and \
                now >= self.client_deadline_ms:
            self._close_client()
            return
        if self.client and self.response is not None:
            try:
                sent = self.client.send(self.response.current(MAX_SEND_BYTES))
            except OSError as error:
                if _would_block(error):
                    return
                self._close_client()
                return
            if not sent:
                self._close_client()
                return
            self.response.consume(sent)
            if self.response.done():
                self._close_client()
            else:
                self._touch()
            return
        if self.client:
            if self.request_length >= MAX_REQUEST_BYTES:
                self.response = self._http_response(
                    "413 Payload Too Large", "text/plain", b"Too large\n")
                return
            maximum = min(
                MAX_RECV_BYTES, MAX_REQUEST_BYTES - self.request_length)
            target = self.request_view[
                self.request_length:self.request_length + maximum]
            try:
                try:
                    count = self.client.readinto(target)
                except AttributeError:
                    data = self.client.recv(maximum)
                    count = len(data)
                    target[:count] = data
            except OSError as error:
                if _would_block(error):
                    return
                self._close_client()
                return
            if count is None:
                return
            if count == 0:
                self._close_client()
                return
            self.request_length += count
            self._touch()
            if self._request_complete():
                try:
                    self._finish_request()
                except Exception as error:
                    if self.error_handler:
                        self.error_handler("request", error)
                    self.response = self._http_response(
                        "500 Internal Server Error", "text/plain",
                        b"internal Pico error\n")
            return
        for server in self.servers or (self.server,):
            if server is None:
                continue
            try:
                self.client, _ = server.accept()
                self.client.setblocking(False)
                self._touch()
                return
            except OSError:
                pass
