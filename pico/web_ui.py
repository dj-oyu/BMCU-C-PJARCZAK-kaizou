"""Small non-blocking read-only HTTP UI for the Pico BMCU monitor."""

try:
    import ujson as json
except ImportError:
    import json
import socket


PAGE = """<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"><title>BMCU Monitor</title><style>:root{color-scheme:dark;--bg:#0b1018;--card:#151d29;--line:#2a3b52;--muted:#9eafc5;--ok:#44d19a;--bad:#ff7180}*{box-sizing:border-box}body{max-width:920px;margin:auto;padding:20px;font:15px system-ui,sans-serif;background:var(--bg);color:#edf4ff}h1{margin:0;font-size:1.5rem}.sub{color:var(--muted);margin:.35rem 0 1rem}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px}.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px}.label{font-size:.78rem;color:var(--muted);text-transform:uppercase}.value{font-size:1.35rem;font-weight:650;margin-top:4px}.ok{color:var(--ok)}.bad{color:var(--bad)}h2{font-size:1rem;margin:24px 0 10px}.slots{display:grid;grid-template-columns:repeat(4,1fr);gap:10px}.slot{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px}.slot.active{border-color:var(--ok);box-shadow:0 0 0 1px var(--ok)}.slot b{font-size:1.05rem}dl{margin:9px 0 0}dt{color:var(--muted);font-size:.75rem}dd{margin:1px 0 8px}details{margin-top:20px;background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px}pre{overflow:auto;white-space:pre-wrap;font-size:.75rem;color:var(--muted)}@media(max-width:500px){.slots{grid-template-columns:repeat(2,1fr)}}</style></head><body><h1>BMCU Monitor</h1><p class="sub" id="summary">Connecting...</p><section class="grid"><article class="card"><div class="label">BMCU Link</div><div class="value" id="link">--</div></article><article class="card"><div class="label">Wi-Fi</div><div class="value" id="wifi">--</div></article><article class="card"><div class="label">Selected Slot</div><div class="value" id="selected">--</div></article><article class="card"><div class="label">Selected Pull</div><div class="value" id="pull">--</div></article><article class="card"><div class="label">Pull Delta</div><div class="value" id="delta">--</div></article></section><h2>Filament Channels</h2><section class="slots" id="slots"></section><h2>Link Diagnostics</h2><section class="grid"><article class="card"><div class="label">BMCU TX drop</div><div class="value" id="txdrop">--</div></article><article class="card"><div class="label">BMCU RX / CRC / frame</div><div class="value" id="errors">--</div></article></section><h2>Recent Events</h2><section class="grid" id="events"></section><details><summary>Show diagnostic JSON</summary><pre id="raw"></pre></details><script>const $=id=>document.getElementById(id),bit=(m,n)=>((m||0)&(1<<n))?'Present':'None';function val(x,d='--'){return x===undefined||x===null?d:x}const amsName=n=>['idle','send-out','on-use','before-pull-back','pull-back','before-on-use','stop-on-use'][n]||'#'+val(n),ctrlName=n=>n===undefined||n===null?'--':(['send','redetect','pull','stop','before-on-use','stop-on-use','pressure-on-use','pressure-idle','before-pull-back'][n]||'#'+n);function set(id,x,good){let e=$(id);e.textContent=x;e.className='value '+(good===true?'ok':good===false?'bad':'')}function draw(x){let b=x.bmcu||{},s=b.status||{},c=b.channels||[],on=b.link==='online',w=x.wifi||{};set('link',on?'ONLINE':'STALE',on);set('wifi',val(w.state).toUpperCase(),w.state==='online');let selected=s.current_slot,active=(selected!==undefined&&selected!==255&&((s.inserted_mask||0)&(1<<selected))&&s.pull_pct)?s.pull_pct[selected]:null;set('selected',selected===255||selected===undefined?'None':'#'+(selected+1));set('pull',active===null?'N/A':active+' %',active!==null);set('delta',active===null?'N/A':(active>=50?'+':'')+(active-50)+' pts',active!==null);$('summary').textContent=w.hostname?'Pico http://'+w.hostname+'.local/ ('+val(w.ip,'no IP')+') / refreshes every second':'Pico '+val(w.ip,'no IP')+' / refreshes every second';$('slots').innerHTML=[0,1,2,3].map(i=>{let active=s.current_slot===i,t=c[i]||{};return '<article class="slot '+(active?'active':'')+'"><b>Slot '+(i+1)+'</b><dl><dt>Filament</dt><dd>'+bit(s.inserted_mask,i)+'</dd><dt>Online</dt><dd>'+bit(s.online_mask,i)+'</dd><dt>Pull</dt><dd>'+val(s.pull_pct&&s.pull_pct[i])+' %</dd><dt>AMS state</dt><dd>'+amsName(s.motion&&s.motion[i])+'</dd><dt>Controller</dt><dd>'+ctrlName(t.controller_motion)+'</dd><dt>Motor PWM</dt><dd>'+val(t.motor_pwm)+'</dd><dt>Encoder delta</dt><dd>'+val(t.position_delta)+'</dd><dt>Sensor</dt><dd>'+(t.sensor_good?'Good':(t.sensor_online?'Fault':'Offline'))+'</dd><dt>Motion fault</dt><dd>#'+val(t.motion_fault)+'</dd></dl></article>'}).join('');set('txdrop',val(s.tx_drop));set('errors',val(s.rx_drop)+' / '+val(s.crc_error)+' / '+val(s.frame_error));$('events').innerHTML=(b.events||[]).slice().reverse().map(e=>'<article class="card"><div class="label">'+e.event_name+' / severity '+e.severity+'</div><div>'+((e.event_name==='state_change')?'Field '+e.field+', slot '+e.slot+': '+e.previous_value+' -> '+e.value:((e.event_name==='sensor')?'Sensor '+e.sensor+', slot '+e.slot+': '+e.value:'Record '+e.record_type))+'</div></article>').join('')||'<article class="card">No push events received yet.</article>';$('raw').textContent=JSON.stringify(x,null,2)}async function refresh(){try{let r=await fetch('/api/status',{cache:'no-store'});if(!r.ok)throw Error(r.status);draw(await r.json())}catch(e){$('summary').textContent='Refresh error: '+e}}refresh();setInterval(refresh,1000)</script></body></html>""".encode()


def _json_safe(value):
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    return value


class WebUI:
    """Services at most one non-blocking HTTP connection at a time."""

    def __init__(self, state_provider, port=80):
        self.state_provider = state_provider
        self.port = port
        self.server = None
        self.client = None
        self.request = bytearray()
        self.response = None

    def start(self):
        self.server = socket.socket()
        try:
            self.server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        except (AttributeError, OSError):
            pass
        self.server.bind(('0.0.0.0', self.port))
        self.server.listen(1)
        self.server.setblocking(False)

    @staticmethod
    def _http_response(status, content_type, body):
        header = ('HTTP/1.1 %s\r\nContent-Type: %s\r\nContent-Length: %d\r\n'
                  'Cache-Control: no-store\r\nConnection: close\r\n\r\n' %
                  (status, content_type, len(body)))
        return header.encode() + body

    def _finish_request(self):
        first_line = bytes(self.request).split(b'\r\n', 1)[0].split()
        path = first_line[1] if len(first_line) >= 2 else b''
        if path == b'/':
            self.response = self._http_response('200 OK', 'text/html; charset=utf-8', PAGE)
        elif path.startswith(b'/api/'):
            state = self.state_provider(path.decode())
            if state is None:
                self.response = self._http_response('404 Not Found', 'text/plain', b'Not found\n')
            else:
                body = json.dumps(_json_safe(state)).encode()
                self.response = self._http_response('200 OK', 'application/json', body)
        else:
            self.response = self._http_response('404 Not Found', 'text/plain', b'Not found\n')

    def _close_client(self):
        if self.client:
            try:
                self.client.close()
            except OSError:
                pass
        self.client = None
        self.request = bytearray()
        self.response = None

    def poll(self):
        if self.client and self.response is not None:
            try:
                sent = self.client.send(self.response)
            except OSError:
                self._close_client()
                return
            if sent >= len(self.response):
                self._close_client()
            elif sent > 0:
                self.response = self.response[sent:]
            return
        if self.client:
            try:
                data = self.client.recv(256)
            except OSError:
                self._close_client()
                return
            if not data:
                self._close_client()
                return
            self.request.extend(data)
            if len(self.request) > 1024:
                self.response = self._http_response('413 Payload Too Large', 'text/plain', b'Request too large\n')
            elif b'\r\n\r\n' in self.request:
                self._finish_request()
            return
        try:
            self.client, _ = self.server.accept()
            self.client.setblocking(False)
        except OSError:
            pass
