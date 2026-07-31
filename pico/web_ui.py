"""Bounded non-blocking HTTP server for static UI and BMB1 binary APIs."""

import socket
import time
try:
    import errno
except ImportError:
    import uerrno as errno

MAX_REQUEST_BYTES = 2048
BINARY_TYPE = "application/vnd.bmcu-monitor.v1"


def _would_block(error):
    code = error.args[0] if error.args else None
    return code in (
        getattr(errno, "EAGAIN", -1),
        getattr(errno, "EWOULDBLOCK", -1),
    )


PAGE = b"""<!doctype html><meta name=viewport content="width=device-width"><title>BMCU Monitor</title><style>body{max-width:900px;margin:auto;padding:20px;font:15px system-ui;background:#0b1018;color:#edf4ff}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:10px}.card{padding:12px;background:#151d29;border:1px solid #2a3b52;border-radius:10px}.muted{color:#9eafc5}pre{white-space:pre-wrap}</style><h1>BMCU Monitor</h1><p id=s class=muted>Connecting...</p><h2>Loader state</h2><div id=d class=grid></div><h2>Hardware and communications</h2><div id=m class=grid></div><h2>Device log</h2><pre id=l></pre><script>
const td=new TextDecoder(),u64=(v,o)=>Number(v.getBigUint64(o)),names={1:'uptime ms',4:'heap free',6:'heap min',12:'loop avg us',13:'loop p95 us',14:'loop p99 us',11:'loop max us',32:'queue',33:'drops',48:'exceptions',49:'GC us',50:'GC max us',64:'UART0 backlog',65:'UART0 max backlog',66:'UART0 bytes',67:'UART0 CRC',68:'UART0 frame',69:'UART0 gaps',72:'UART1 backlog',73:'UART1 max backlog',74:'UART1 bytes',75:'UART1 CRC',76:'UART1 frame',77:'UART1 gaps'};
function frames(b){let v=new DataView(b),a=[],o=0;while(o+32<=b.byteLength){if(v.getUint32(o)!==0x424d4231||v.getUint8(o+4)!==1)break;let n=v.getUint32(o+8);if(n>4096||o+32+n>b.byteLength)break;a.push([v.getUint8(o+5),new DataView(b,o+32,n)]);o+=32+n}return a}
function tlvs(v){let a=[],o=0;while(o+4<=v.byteLength){let t=v.getUint8(o),k=v.getUint8(o+1),n=v.getUint16(o+2);o+=4;if(o+n>v.byteLength)break;let x=k===4&&n===8?u64(v,o):(k===7&&n===4?v.getInt32(o):td.decode(new Uint8Array(v.buffer,v.byteOffset+o,n)));a.push([t,x]);o+=n}return a}
async function get(p){let r=await fetch(p,{cache:'no-store'});if(!r.ok)throw Error(r.status);return frames(await r.arrayBuffer())}
function status(v){if(v.byteLength<44||v.getUint8(13)!==2)return'';let slot=v.getUint8(29),ins=v.getUint8(30),on=v.getUint8(31),h='';for(let i=0;i<4;i++)h+='<div class=card><b>Slot '+(i+1)+(slot===i?' / selected':'')+'</b><br>Filament '+((ins>>i)&1)+' / Online '+((on>>i)&1)+'<br>Motion '+v.getUint8(32+i)+' / Pull '+v.getUint8(36+i)+'%</div>';return h}
let last=0;async function refresh(){try{let cur=await get('/api/current.bin'),slots='';for(let [t,v] of cur)if(t===16)slots+=status(v);d.innerHTML=slots||'<span class=muted>No STATUS received</span>';let diag=await get('/api/diagnostics.bin'),h='';for(let [t,v] of diag)if(t===19)for(let [k,x] of tlvs(v))h+='<div class=card><span class=muted>'+(names[k]||'metric '+k)+'</span><br>'+x+'</div>';m.innerHTML=h;s.textContent='Binary live snapshot / refresh 3 s';let logs=await get('/api/logs.bin?after='+last+'&limit=32),lines=[];for(let [t,v] of logs)if(t===20){let q=u64(v,0),up=u64(v,8),sev=v.getUint8(16),cn=v.getUint8(17),mn=v.getUint16(18),o=22,c=td.decode(new Uint8Array(v.buffer,v.byteOffset+o,cn));o+=cn;let msg=td.decode(new Uint8Array(v.buffer,v.byteOffset+o,mn));last=Math.max(last,q);lines.push('['+up+' ms] '+sev+' '+c+': '+msg)}if(lines.length)l.textContent=(lines.join('\\n')+'\\n'+l.textContent).slice(0,12000)}catch(e){s.textContent='Refresh error: '+e}}refresh();setInterval(refresh,3000)</script>"""


class _Response:
    def __init__(self, header, body):
        self.parts = [header]
        if isinstance(body, (list, tuple)):
            self.parts.extend(body)
        else:
            self.parts.append(body)
        self.offset = 0

    def current(self):
        while self.parts and self.offset >= len(self.parts[0]):
            self.parts.pop(0)
            self.offset = 0
        if not self.parts:
            return b""
        return memoryview(self.parts[0])[self.offset:]

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
    def __init__(self, binary_provider, port=80, error_handler=None):
        self.binary_provider = binary_provider
        self.error_handler = error_handler
        self.port = port
        self.server = None
        self.servers = []
        self.client = None
        self.request = bytearray()
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
        server.listen(1)
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
                  "Cache-Control: no-store\r\nConnection: close\r\n\r\n" %
                  (status, content_type, size)).encode()
        return _Response(header, body)

    def _finish_request(self):
        line = bytes(self.request).split(b"\r\n", 1)[0].split()
        method = line[0] if line else b""
        path = line[1] if len(line) > 1 else b""
        if method != b"GET":
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
        self.request = bytearray()
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
                sent = self.client.send(self.response.current())
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
            try:
                data = self.client.recv(256)
            except OSError as error:
                if _would_block(error):
                    return
                self._close_client()
                return
            if not data:
                self._close_client()
                return
            self.request.extend(data)
            self._touch()
            if len(self.request) > MAX_REQUEST_BYTES:
                self.response = self._http_response(
                    "413 Payload Too Large", "text/plain", b"Too large\n")
            elif b"\r\n\r\n" in self.request:
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
