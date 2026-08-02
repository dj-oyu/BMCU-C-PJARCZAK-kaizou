import { useEffect, useRef, useState } from 'preact/hooks'
import { api } from './api/client'
import {
  activeLinks,
  linkCounters,
  readLogRecords,
  readTelemetry,
  type LogRecord,
  type Telemetry,
} from './api/decode'
import { BridgeHealth } from './components/BridgeHealth'
import { DeviceKeyCard } from './components/DeviceKeyCard'
import { EndpointCard } from './components/EndpointCard'
import { LoaderCard } from './components/LoaderCard'
import { LogView } from './components/LogView'

const REFRESH_MS = 3000
const LOG_LIMIT = 24
const LOG_HISTORY = 200

const EMPTY: Telemetry = { diagnostics: new Map(), statuses: new Map() }

export function App() {
  const [telemetry, setTelemetry] = useState<Telemetry>(EMPTY)
  const [logs, setLogs] = useState<LogRecord[]>([])
  const [error, setError] = useState<string | null>(null)
  const [loaded, setLoaded] = useState(false)
  const watermark = useRef(0n)

  useEffect(() => {
    let stopped = false

    const refresh = async () => {
      try {
        // Sequential on purpose; see the serialisation note in api/client.ts.
        const current = await api.current()
        const diagnostics = await api.diagnostics()
        if (stopped) return
        setTelemetry(readTelemetry(current, diagnostics))

        const records = readLogRecords(await api.logs(watermark.current, LOG_LIMIT))
        if (stopped) return
        if (records.length) {
          for (const record of records) {
            if (record.sequence > watermark.current) watermark.current = record.sequence
          }
          setLogs((previous) => [...records].reverse().concat(previous).slice(0, LOG_HISTORY))
        }
        setError(null)
        setLoaded(true)
      } catch (cause) {
        if (!stopped) setError(String(cause))
      }
    }

    void refresh()
    const timer = setInterval(refresh, REFRESH_MS)
    return () => {
      stopped = true
      clearInterval(timer)
    }
  }, [])

  const links = activeLinks(telemetry)
  const receiving = links.filter((link) => telemetry.statuses.has(link)).length
  const health = error
    ? { text: 'Refresh failed', tone: 'bad' }
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
          <p class="muted">
            {error
              ? `Web UI error: ${error}`
              : loaded
                ? `Live binary STATUS and per-UART health / refresh ${REFRESH_MS / 1000} s`
                : 'Connecting to the Pico...'}
          </p>
        </div>
        <span class={`badge ${health.tone}`}>{health.text}</span>
      </header>

      <main>
        <section>
          <h2>Loaders</h2>
          <div class="loaders">
            {links.map((link) => (
              <LoaderCard
                key={link}
                link={link}
                status={telemetry.statuses.get(link) ?? null}
                counters={linkCounters(link, telemetry.diagnostics)}
              />
            ))}
          </div>
        </section>

        <section>
          <h2>Bridge health</h2>
          <div class="bridge-grid">
            <BridgeHealth diagnostics={telemetry.diagnostics} />
          </div>
        </section>

        <section>
          <h2>Connection settings</h2>
          <div class="settings-grid">
            <EndpointCard />
            <DeviceKeyCard />
          </div>
        </section>

        <section>
          <h2>Recent device log</h2>
          <LogView records={logs} />
        </section>
      </main>
    </>
  )
}
