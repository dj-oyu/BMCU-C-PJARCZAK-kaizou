import { useCallback, useEffect, useState } from 'preact/hooks'
import { api } from '../api/client'

/** Host without a URL scheme, matching what transport_settings.py accepts. */
const HOST_PATTERN = /^[A-Za-z0-9._-]{1,240}$/

type Note = { text: string; tone: 'muted' | 'ok' | 'bad' }

export function EndpointCard() {
  const [configured, setConfigured] = useState<boolean | null>(null)
  const [host, setHost] = useState('')
  const [port, setPort] = useState('')
  const [busy, setBusy] = useState(false)
  const [note, setNote] = useState<Note>({
    text: 'Saved locally; UI values override config.py.',
    tone: 'muted',
  })

  const load = useCallback(async () => {
    try {
      const fields = await api.transportStatus()
      setConfigured(fields.get('configured') === '1')
      setHost(fields.get('host') ?? '')
      setPort(fields.get('port') ?? '')
    } catch (error) {
      setConfigured(null)
      setNote({ text: `Cannot read endpoint: ${error}`, tone: 'bad' })
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  const save = async () => {
    const trimmed = host.trim()
    const parsed = Number(port)
    if (!HOST_PATTERN.test(trimmed) || trimmed.includes('..')) {
      setNote({
        text: 'Enter an IPv4 address or DNS name without a URL scheme.',
        tone: 'bad',
      })
      return
    }
    if (!Number.isInteger(parsed) || parsed < 1 || parsed > 65535) {
      setNote({ text: 'Port must be between 1 and 65535.', tone: 'bad' })
      return
    }
    setBusy(true)
    try {
      await api.saveTransport(trimmed, parsed)
      setNote({
        text: `Saved. BMB1 is reconnecting to ${trimmed}:${parsed}.`,
        tone: 'ok',
      })
      await load()
    } catch (error) {
      setNote({ text: `Save failed: ${error}`, tone: 'bad' })
    } finally {
      setBusy(false)
    }
  }

  const state =
    configured === null ? 'Unavailable' : configured ? 'Configured' : 'Not configured'

  return (
    <article class="key-card">
      <div class="key-top">
        <div>
          <h3>Bambuddy endpoint</h3>
          <div class="muted">Configure the BMB1 TCP destination used by this Pico.</div>
        </div>
        <span class={`badge ${configured ? 'ok' : 'bad'}`}>{state}</span>
      </div>
      <div class="endpoint-row">
        <div>
          <label for="bambuddy-host">Host or IPv4 address</label>
          <input
            id="bambuddy-host"
            maxLength={240}
            autocomplete="off"
            spellcheck={false}
            placeholder="bambuddy.local"
            value={host}
            onInput={(event) => setHost(event.currentTarget.value)}
          />
        </div>
        <div>
          <label for="bambuddy-port">TCP port</label>
          <input
            id="bambuddy-port"
            type="number"
            min={1}
            max={65535}
            inputMode="numeric"
            placeholder="8766"
            value={port}
            onInput={(event) => setPort(event.currentTarget.value)}
          />
        </div>
      </div>
      <div class="actions">
        <button class="primary" type="button" disabled={busy} onClick={save}>
          Save endpoint &amp; reconnect
        </button>
      </div>
      <p class={`${note.tone} key-note`}>{note.text}</p>
    </article>
  )
}
