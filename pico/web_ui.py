"""Small non-blocking read-only HTTP UI for the Pico BMCU monitor."""

try:
    import ujson as json
except ImportError:
    import json
import socket
import gc
import time
try:
    import errno
except ImportError:
    import uerrno as errno


def _would_block(error):
    code = error.args[0] if error.args else None
    return code in (
        getattr(errno, "EAGAIN", -1),
        getattr(errno, "EWOULDBLOCK", -1),
    )


PAGE = """<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"><title>BMCU Monitor</title><style>:root{color-scheme:dark;--bg:#0b1018;--card:#151d29;--line:#2a3b52;--muted:#9eafc5;--ok:#44d19a;--bad:#ff7180}*{box-sizing:border-box}body{max-width:920px;margin:auto;padding:20px;font:15px system-ui,sans-serif;background:var(--bg);color:#edf4ff}h1{margin:0;font-size:1.5rem}.sub{color:var(--muted);margin:.35rem 0 1rem}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px}.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px}.label{font-size:.78rem;color:var(--muted);text-transform:uppercase}.value{font-size:1.35rem;font-weight:650;margin-top:4px}.ok{color:var(--ok)}.bad{color:var(--bad)}h2{font-size:1.1rem;margin:24px 0 10px}h3{font-size:.95rem;margin:18px 0 8px;color:var(--muted)}.device{margin-top:26px;border-top:1px solid var(--line);padding-top:6px}.slots{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-top:10px}.slot{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px}.slot.active{border-color:var(--ok);box-shadow:0 0 0 1px var(--ok)}.slot b{font-size:1.05rem}dl{margin:9px 0 0}dt{color:var(--muted);font-size:.75rem}dd{margin:1px 0 8px}details{margin-top:20px;background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px}pre{overflow:auto;white-space:pre-wrap;font-size:.75rem;color:var(--muted)}button{padding:.5rem .8rem}@media(max-width:500px){.slots{grid-template-columns:repeat(2,1fr)}}</style></head><body><h1>BMCU Monitor</h1><p><a href='/settings'>Bambuddy settings</a> / <a href='/api/pico/logs'>Pico logs JSON</a></p><p class="sub" id="summary">Connecting...</p><section class="grid"><article class="card"><div class="label">Wi-Fi</div><div class="value" id="wifi">--</div></article><article class="card"><div class="label">Bambuddy</div><div class="value" id="bambuddy">--</div></article><article class="card"><div class="label">Uptime</div><div class="value" id="picoUptime">--</div></article><article class="card"><div class="label">Free heap</div><div class="value" id="picoHeap">--</div></article><article class="card"><div class="label">Exceptions</div><div class="value" id="picoExceptions">--</div></article></section><div id="devices"></div><details open><summary>Pico internal log (BMCU frames excluded)</summary><pre id="picoLog">No runtime log</pre></details><details><summary>Show diagnostic JSON</summary><pre id="raw"></pre></details><script>const $=id=>document.getElementById(id),bit=(m,n)=>((m||0)&(1<<n))?'Present':'None';function val(x,d='--'){return x===undefined||x===null?d:x}const amsName=n=>['idle','send-out','on-use','before-pull-back','pull-back','before-on-use','stop-on-use'][n]||'#'+val(n),ctrlName=n=>n===undefined||n===null?'--':(['send','redetect','pull','stop','before-on-use','stop-on-use','pressure-on-use','pressure-idle','before-pull-back'][n]||'#'+n);function set(id,x,good){let e=$(id);e.textContent=x;e.className='value '+(good===true?'ok':good===false?'bad':'')}const resetUi={};function slotHtml(s,c,i){let active=s.current_slot===i,t=c[i]||{};return '<article class="slot '+(active?'active':'')+'"><b>Slot '+(i+1)+'</b><dl><dt>Filament</dt><dd>'+bit(s.inserted_mask,i)+'</dd><dt>Online</dt><dd>'+bit(s.online_mask,i)+'</dd><dt>Pull</dt><dd>'+val(s.pull_pct&&s.pull_pct[i])+' %</dd><dt>AMS state</dt><dd>'+amsName(s.motion&&s.motion[i])+'</dd><dt>Controller</dt><dd>'+ctrlName(t.controller_motion)+'</dd><dt>Motor PWM</dt><dd>'+val(t.motor_pwm)+'</dd><dt>Encoder delta</dt><dd>'+val(t.position_delta)+'</dd><dt>Sensor</dt><dd>'+(t.sensor_good?'Good':(t.sensor_online?'Fault':'Offline'))+'</dd><dt>Motion fault</dt><dd>#'+val(t.motion_fault)+'</dd></dl></article>'}function eventHtml(e){return '<article class="card"><div class="label">'+e.event_name+' / severity '+e.severity+'</div><div>'+((e.event_name==='state_change')?'Field '+e.field+', slot '+e.slot+': '+e.previous_value+' -> '+e.value:((e.event_name==='sensor')?'Sensor '+e.sensor+', slot '+e.slot+': '+e.value:'Record '+e.record_type))+'</div></article>'}function devHtml(d){let s=d.status||{},c=d.channels||[],on=d.link==='online',sel=s.current_slot,act=(sel!==undefined&&sel!==255&&((s.inserted_mask||0)&(1<<sel))&&s.pull_pct)?s.pull_pct[sel]:null,r=resetUi[d.link_id]||{};return '<section class="device"><h2>'+d.link_id+' <span class="'+(on?'ok':'bad')+'">'+val(d.link).toUpperCase()+'</span></h2><section class="grid"><article class="card"><div class="label">Selected Slot</div><div class="value">'+(sel===255||sel===undefined?'None':'#'+(sel+1))+'</div></article><article class="card"><div class="label">Selected Pull</div><div class="value '+(act!==null?'ok':'')+'">'+(act===null?'N/A':act+' %')+'</div></article><article class="card"><div class="label">Pull Delta</div><div class="value">'+(act===null?'N/A':(act>=50?'+':'')+(act-50)+' pts')+'</div></article><article class="card"><div class="label">BMCU TX drop</div><div class="value">'+val(s.tx_drop)+'</div></article><article class="card"><div class="label">BMCU RX / CRC / frame</div><div class="value">'+val(s.rx_drop)+' / '+val(s.crc_error)+' / '+val(s.frame_error)+'</div></article><article class="card"><div class="label">Pico decoder CRC / frame</div><div class="value">'+val(d.decoder_crc_errors)+' / '+val(d.decoder_frame_errors)+'</div></article></section><section class="slots">'+[0,1,2,3].map(i=>slotHtml(s,c,i)).join('')+'</section><h3>Recent events</h3><section class="grid">'+((d.events||[]).slice().reverse().map(eventHtml).join('')||'<article class="card">No push events received yet.</article>')+'</section><p><span class="label">BMCU recovery (idle-only; active motion is rejected)</span><br><button data-link="'+d.link_id+'"'+(r.busy?' disabled':'')+'>Request '+d.link_id+' soft reset</button> <span>'+(r.msg||'')+'</span></p></section>'}function draw(x){let w=x.wifi||{},t=x.bambuddy||{},p=x.pico||{},logs=p.entries||[];set('wifi',val(w.state).toUpperCase(),w.state==='online');set('bambuddy',val(t.state,'disabled').toUpperCase(),t.state==='connected'?true:(t.state==='disabled'?undefined:false));$('picoUptime').textContent=Math.floor((p.uptime_ms||0)/1000)+' s';$('picoHeap').textContent=p.heap_free===undefined?'--':Math.floor(p.heap_free/1024)+' KiB';$('picoExceptions').textContent=val(p.exception_count,0);$('picoLog').textContent=logs.slice().reverse().map(e=>'['+e.uptime_ms+' ms] '+e.level.toUpperCase()+' '+e.component+': '+e.message).join('\\n')||'No runtime log';$('summary').textContent='Bridge '+val(x.bridge_id)+(w.hostname?' / http://'+w.hostname+'.local/':'')+' ('+val(w.ip,'no IP')+') / refreshes every second';$('devices').innerHTML=(x.devices||[]).map(devHtml).join('')||'<p class="sub">No BMCU links configured.</p>';$('raw').textContent=JSON.stringify(x,null,2)}$('devices').onclick=async ev=>{let b=ev.target.closest('button[data-link]');if(!b)return;let link=b.dataset.link;if(prompt('Type RESET BMCU to confirm the reset of '+link)!=='RESET BMCU')return;resetUi[link]={busy:1,msg:'Requesting...'};b.disabled=true;try{let c=await(await fetch('/api/bambuddy/config',{cache:'no-store'})).json(),r=await fetch('/api/devices/'+link+'/soft-reset',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({csrf:c.csrf,confirm:'RESET BMCU',reason:0,ttl_ms:5000})}),x=await r.json();resetUi[link]={busy:1,msg:r.ok?'Requested; waiting for BMCU reboot':(x.error||'Rejected')}}catch(e){resetUi[link]={busy:1,msg:'Request failed: '+e}}setTimeout(()=>{resetUi[link].busy=0},6000)};async function refresh(){try{let r=await fetch('/api/devices',{cache:'no-store'});if(!r.ok)throw Error(r.status);draw(await r.json())}catch(e){$('summary').textContent='Refresh error: '+e}}refresh();setInterval(refresh,1000)</script></body></html>""".encode()
SETTINGS_PAGE = """<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"><title>Bambuddy settings</title><style>body{max-width:640px;margin:2rem auto;padding:0 1rem;font:16px system-ui;background:#0b1018;color:#edf4ff}label{display:block;margin:1rem 0}input[type=url],input[type=password]{width:100%;padding:.7rem;background:#151d29;color:inherit;border:1px solid #456;border-radius:6px}button{padding:.7rem 1rem}#status{margin-left:1rem}</style></head><body><p><a href="/">Monitor</a></p><h1>Bambuddy transport</h1><p>Trusted-LAN ws:// push transport. Optional credential scope: bmcu_link:telemetry.</p><form id="form"><label><input id="enabled" type="checkbox"> Enable transport</label><label>WebSocket URL<input id="url" type="url" placeholder="ws://bambuddy.local:8000/api/v1/bmcu-link/ws"></label><label>Token (leave blank to keep current)<input id="token" type="password" autocomplete="new-password"></label><button>Save</button><span id="status"></span></form><script>let csrf;async function load(){let r=await fetch('/api/bambuddy/config',{cache:'no-store'}),x=await r.json();csrf=x.csrf;enabled.checked=x.enabled;url.value=x.url;status.textContent=x.token_set?'Token is set':'No token (server auth must be disabled)'}form.onsubmit=async e=>{e.preventDefault();let body={csrf,enabled:enabled.checked,url:url.value,token:token.value},r=await fetch('/api/bambuddy/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}),x=await r.json();if(!r.ok){status.textContent=x.error||'Save failed';return}csrf=x.csrf;token.value='';status.textContent='Saved'};load()</script></body></html>""".encode()
SETTINGS_PAGE = SETTINGS_PAGE.replace(
    b"</form>", b"<p id='transport'>Connection: --</p></form>", 1)
SETTINGS_PAGE = SETTINGS_PAGE.replace(
    b"status.textContent=x.token_set?'Token is set':'No token (server auth must be disabled)'",
    b"status.textContent=x.token_set?'Token is set':'No token (server auth must be disabled)';"
    b"let t=x.transport||{};transport.textContent='Connection: '+"
    b"(t.state||'disabled')+(t.last_error?' / '+t.last_error:'')", 1)
SETTINGS_PAGE = SETTINGS_PAGE.replace(
    b"status.textContent='Saved'};load()",
    b"status.textContent='Saved';setTimeout(load,300)};load()", 1)

class _JsonChunks:
    """Incremental JSON encoder with bounded contiguous allocations."""

    def __init__(self, chunk_size=512):
        self.chunk_size = chunk_size
        self.buffer = bytearray()
        self.parts = []
        self.length = 0

    def write(self, data):
        if isinstance(data, str):
            data = data.encode()
        offset = 0
        while offset < len(data):
            available = self.chunk_size - len(self.buffer)
            count = min(available, len(data) - offset)
            self.buffer.extend(memoryview(data)[offset:offset + count])
            self.length += count
            offset += count
            if len(self.buffer) == self.chunk_size:
                self.parts.append(bytes(self.buffer))
                self.buffer = bytearray()

    def finish(self):
        if self.buffer:
            self.parts.append(bytes(self.buffer))
            self.buffer = bytearray()
        return self.parts, self.length


def _write_json(writer, value):
    if value is None:
        writer.write(b"null")
    elif value is True:
        writer.write(b"true")
    elif value is False:
        writer.write(b"false")
    elif isinstance(value, bytes):
        writer.write(json.dumps(value.hex()))
    elif isinstance(value, str):
        writer.write(json.dumps(value))
    elif isinstance(value, dict):
        writer.write(b"{")
        first = True
        for key, item in value.items():
            if not first:
                writer.write(b",")
            first = False
            writer.write(json.dumps(str(key)))
            writer.write(b":")
            _write_json(writer, item)
        writer.write(b"}")
    elif isinstance(value, (list, tuple)):
        writer.write(b"[")
        for index, item in enumerate(value):
            if index:
                writer.write(b",")
            _write_json(writer, item)
        writer.write(b"]")
    else:
        writer.write(json.dumps(value))

class _Response:
    """Two-part HTTP response that avoids copying large bodies."""

    def __init__(self, header, body):
        if isinstance(body, list):
            body.insert(0, header)
            self.parts = body
        else:
            self.parts = [header, body]
        self.offset = 0

    def current(self):
        while self.parts and self.offset >= len(self.parts[0]):
            self.parts.pop(0)
            self.offset = 0
        if not self.parts:
            return b""
        if self.offset == 0:
            return self.parts[0]
        return self.parts[0][self.offset:]

    def consume(self, count):
        self.offset += count
        self.current()

    def done(self):
        return not self.parts

    def __contains__(self, value):
        return any(value in part for part in self.parts)


class WebUI:
    """Services at most one non-blocking HTTP connection at a time."""

    def __init__(self, state_provider, port=80, config_provider=None,
                 config_updater=None, command_updater=None, error_handler=None):
        self.state_provider = state_provider
        self.config_provider = config_provider
        self.config_updater = config_updater
        self.command_updater = command_updater
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
        try:
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if ipv6_only:
                # lwIP values; MicroPython does not export these constants.
                server.setsockopt(41, 27, 1)  # IPPROTO_IPV6, IPV6_V6ONLY
            server.bind((address, port))
            server.listen(1)
            server.setblocking(False)
            return server
        except Exception:
            try:
                server.close()
            except Exception:
                pass
            raise

    def start(self):
        self.servers = []
        try:
            self.servers.append(self._listener(
                socket.AF_INET6, '::', self.port, ipv6_only=True))
        except (AttributeError, OSError):
            pass
        self.server = self._listener(
            socket.AF_INET, '0.0.0.0', self.port)
        self.servers.append(self.server)

    @staticmethod
    def _http_response(status, content_type, body, body_length=None):
        if body_length is None:
            body_length = (sum(len(part) for part in body)
                           if isinstance(body, list) else len(body))
        if isinstance(body, bytes) and len(body) > 512:
            body = [body[offset:offset + 512]
                    for offset in range(0, len(body), 512)]
        header = ('HTTP/1.1 %s\r\nContent-Type: %s\r\nContent-Length: %d\r\n'
                  'Cache-Control: no-store\r\nConnection: close\r\n\r\n' %
                  (status, content_type, body_length))
        return _Response(header.encode(), body)

    def _request_ready(self):
        marker = self.request.find(b'\r\n\r\n')
        if marker < 0:
            return False
        header = bytes(self.request[:marker]).split(b'\r\n')
        content_length = 0
        for line in header[1:]:
            name, separator, value = line.partition(b':')
            if separator and name.strip().lower() == b'content-length':
                try:
                    content_length = int(value.strip())
                except ValueError:
                    content_length = -1
        if content_length < 0 or content_length > 2048:
            self.response = self._http_response(
                '413 Payload Too Large', 'text/plain', b'Request too large\n')
            return False
        return len(self.request) >= marker + 4 + content_length

    def _json_response(self, status, value):
        gc.collect()
        writer = _JsonChunks()
        _write_json(writer, value)
        parts, length = writer.finish()
        return self._http_response(
            status, 'application/json', parts, body_length=length)

    def _finish_request(self):
        marker = self.request.find(b'\r\n\r\n')
        header = bytes(self.request[:marker])
        body = bytes(self.request[marker + 4:])
        first_line = header.split(b'\r\n', 1)[0].split()
        method = first_line[0] if len(first_line) >= 1 else b''
        path = first_line[1] if len(first_line) >= 2 else b''
        if method == b'GET' and path == b'/':
            self.response = self._http_response(
                '200 OK', 'text/html; charset=utf-8', PAGE)
        elif method == b'GET' and path == b'/settings':
            self.response = self._http_response(
                '200 OK', 'text/html; charset=utf-8', SETTINGS_PAGE)
        elif path == b'/api/bambuddy/config' and method == b'GET':
            if self.config_provider is None:
                self.response = self._json_response('404 Not Found',
                                                    {"error": "Not found"})
            else:
                self.response = self._json_response('200 OK',
                                                    self.config_provider())
        elif method == b'POST' and path.startswith(b'/api/devices/') and path.endswith(b'/soft-reset'):
            if self.command_updater is None:
                self.response = self._json_response('404 Not Found',
                                                    {"error": "Not found"})
            else:
                try:
                    request = json.loads(body.decode())
                    result = self.command_updater(path.decode(), request)
                    self.response = self._json_response('202 Accepted', result)
                except (ValueError, TypeError) as exc:
                    self.response = self._json_response(
                        '400 Bad Request', {"error": str(exc)})
        elif path == b'/api/bambuddy/config' and method == b'POST':
            if self.config_updater is None:
                self.response = self._json_response('404 Not Found',
                                                    {"error": "Not found"})
            else:
                try:
                    request = json.loads(body.decode())
                    result = self.config_updater(request)
                    self.response = self._json_response('200 OK', result)
                except (ValueError, TypeError) as exc:
                    self.response = self._json_response(
                        '400 Bad Request', {"error": str(exc)})
        elif method == b'GET' and path.startswith(b'/api/'):
            state = self.state_provider(path.decode())
            if state is None:
                self.response = self._json_response('404 Not Found',
                                                    {"error": "Not found"})
            else:
                self.response = self._json_response('200 OK', state)
        elif method not in (b'GET', b'POST'):
            self.response = self._http_response(
                '405 Method Not Allowed', 'text/plain', b'Method not allowed\n')
        else:
            self.response = self._http_response(
                '404 Not Found', 'text/plain', b'Not found\n')

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
        if hasattr(time, "ticks_ms"):
            return time.ticks_ms()
        return int(time.monotonic() * 1000)

    @staticmethod
    def _ticks_diff(left, right):
        if hasattr(time, "ticks_diff"):
            return time.ticks_diff(left, right)
        return left - right

    @staticmethod
    def _ticks_add(value, delta):
        if hasattr(time, "ticks_add"):
            return time.ticks_add(value, delta)
        return value + delta

    def _touch_client(self):
        self.client_deadline_ms = self._ticks_add(self._now_ms(), 5000)

    def _finish_response(self):
        try:
            self.client.shutdown(getattr(socket, "SHUT_WR", 1))
        except (AttributeError, OSError):
            pass
        self.response = None
        self.close_at_ms = self._ticks_add(self._now_ms(), 100)

    def poll(self):
        if (self.client and self.client_deadline_ms is not None and
                self._ticks_diff(self._now_ms(),
                                 self.client_deadline_ms) >= 0):
            self._close_client()
            return
        if self.client and self.close_at_ms is not None:
            if self._ticks_diff(self._now_ms(), self.close_at_ms) >= 0:
                self._close_client()
            return
        if self.client and self.response is not None:
            chunked = isinstance(self.response, _Response)
            sendall = getattr(self.client, "sendall", None)
            if chunked and sendall is not None:
                try:
                    self.client.settimeout(1)
                    while not self.response.done():
                        payload = self.response.current()
                        sendall(payload)
                        self.response.consume(len(payload))
                    self._finish_response()
                except OSError:
                    self._close_client()
                return
            payload = self.response.current() if chunked else self.response
            try:
                sent = self.client.send(payload)
            except OSError as error:
                if _would_block(error):
                    return
                self._close_client()
                return
            if sent == 0:
                self._close_client()
            elif sent is None:
                return
            elif sent > 0 and chunked:
                self._touch_client()
                self.response.consume(sent)
                if self.response.done():
                    self._finish_response()
            elif sent >= len(payload):
                self._finish_response()
            elif sent > 0:
                self._touch_client()
                self.response = self.response[sent:]
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
            self._touch_client()
            self.request.extend(data)
            if len(self.request) > 3072:
                self.response = self._http_response('413 Payload Too Large', 'text/plain', b'Request too large\n')
            elif self._request_ready():
                try:
                    self._finish_request()
                except Exception as error:
                    if self.error_handler is not None:
                        self.error_handler("request", error)
                    self.response = self._http_response(
                        '500 Internal Server Error', 'text/plain',
                        b'internal Pico error\n')
            return
        for server in self.servers or (self.server,):
            if server is None:
                continue
            try:
                self.client, _ = server.accept()
                self.client.setblocking(False)
                self._touch_client()
                return
            except OSError:
                pass
