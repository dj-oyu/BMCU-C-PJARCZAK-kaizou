import { useEffect, useRef, useState } from 'preact/hooks'
import { api } from './api/client'
import {
  activeLinks,
  readLogRecords,
  readTelemetry,
  type LogRecord,
  type Telemetry,
} from './api/decode'
import { readSnapshot, type LinkSnapshot } from './api/snapshot'
import { DeviceKeyCard } from './components/DeviceKeyCard'
import { EndpointCard } from './components/EndpointCard'
import { DiagnosticsPage } from './pages/DiagnosticsPage'
import { OperatePage } from './pages/OperatePage'
import { PrinterPage } from './pages/PrinterPage'
import { SetupPage } from './pages/SetupPage'
import { ROUTES, href, useRoute } from './routes'

const LIVE_REFRESH_MS = 3000
const LOG_LIMIT = 24
const LOG_HISTORY = 200

const EMPTY: Telemetry = {
  diagnostics: new Map(),
  statuses: new Map(),
  payloads: new Map(),
}

/**
 * Polling follows how often the data changes, not which page is open, because
 * the Pico serves one HTTP client at a time. The operate page therefore costs
 * one request per cycle; the snapshot, which the BMCU sends once per link
 * session, is fetched when its page is opened and not on a timer.
 */
export function App() {
  const route = useRoute()
  const [telemetry, setTelemetry] = useState<Telemetry>(EMPTY)
  const [snapshot, setSnapshot] = useState<Map<number, LinkSnapshot>>(new Map())
  const [logs, setLogs] = useState<LogRecord[]>([])
  const [error, setError] = useState<string | null>(null)
  const [loaded, setLoaded] = useState(false)
  const watermark = useRef(0n)

  const wantsLive = route === 'operate' || route === 'diagnostics'
  const wantsSnapshot = route === 'setup' || route === 'printer'
  const wantsLogs = route === 'diagnostics'

  useEffect(() => {
    if (!wantsLive) return
    let stopped = false

    const refresh = async () => {
      try {
        // Sequential on purpose; see the serialisation note in api/client.ts.
        const current = await api.current()
        const diagnostics = await api.diagnostics()
        if (stopped) return
        setTelemetry(readTelemetry(current, diagnostics))

        if (wantsLogs) {
          const records = readLogRecords(await api.logs(watermark.current, LOG_LIMIT))
          if (stopped) return
          if (records.length) {
            for (const record of records) {
              if (record.sequence > watermark.current) watermark.current = record.sequence
            }
            setLogs((previous) =>
              [...records].reverse().concat(previous).slice(0, LOG_HISTORY),
            )
          }
        }
        setError(null)
        setLoaded(true)
      } catch (cause) {
        if (!stopped) setError(String(cause))
      }
    }

    void refresh()
    const timer = setInterval(refresh, LIVE_REFRESH_MS)
    return () => {
      stopped = true
      clearInterval(timer)
    }
  }, [wantsLive, wantsLogs])

  useEffect(() => {
    if (!wantsSnapshot) return
    let stopped = false
    void (async () => {
      try {
        const buffer = await api.snapshot()
        if (!stopped) {
          setSnapshot(readSnapshot(buffer))
          setError(null)
        }
      } catch (cause) {
        if (!stopped) setError(String(cause))
      }
    })()
    return () => {
      stopped = true
    }
  }, [wantsSnapshot, route])

  // The diagnostics page shows link state, which only the snapshot carries.
  useEffect(() => {
    if (route !== 'diagnostics') return
    void api
      .snapshot()
      .then((buffer) => setSnapshot(readSnapshot(buffer)))
      .catch(() => undefined)
  }, [route])

  const links = activeLinks(telemetry)
  const receiving = links.filter((link) => telemetry.statuses.has(link)).length
  const active = ROUTES.find((entry) => entry.path === route) ?? ROUTES[0]
  const health = error
    ? { text: 'Refresh failed', tone: 'bad' }
    : !wantsLive
      ? { text: 'On demand', tone: '' }
      : !loaded
        ? { text: 'Connecting', tone: '' }
        : {
            text: `${receiving} / ${links.length} receiving`,
            tone: receiving === links.length ? 'ok' : receiving ? 'warn' : 'bad',
          }

  return (
    <>
      <header>
        <div>
          <p class="kicker">Pico 2 W / BMB1 live diagnostics</p>
          <h1>BMCU Loader Monitor</h1>
          <p class="muted">{error ? `Web UI error: ${error}` : active.question}</p>
        </div>
        <span class={`badge ${health.tone}`}>{health.text}</span>
      </header>

      <nav class="tabs">
        {ROUTES.map((entry) => (
          <a
            key={entry.path}
            class={`tab ${entry.path === route ? 'active' : ''}`.trim()}
            href={href(entry.path)}
          >
            {entry.label}
          </a>
        ))}
      </nav>

      <main>
        {route === 'operate' && <OperatePage telemetry={telemetry} links={links} />}
        {route === 'setup' && <SetupPage links={snapshot} />}
        {route === 'printer' && <PrinterPage links={snapshot} />}
        {route === 'diagnostics' && (
          <DiagnosticsPage
            telemetry={telemetry}
            links={links}
            snapshot={snapshot}
            logs={logs}
            statusPayloads={telemetry.payloads}
          />
        )}

        {route === 'operate' && (
          <section>
            <h2>Connection settings</h2>
            <div class="settings-grid">
              <EndpointCard />
              <DeviceKeyCard />
            </div>
          </section>
        )}
      </main>
    </>
  )
}
