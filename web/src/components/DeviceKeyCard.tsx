import { useCallback, useEffect, useRef, useState } from 'preact/hooks'
import { api } from '../api/client'

const KEY_PATTERN = /^[0-9a-fA-F]{64}$/
const KEY_BYTES = 32

type Note = { text: string; tone: 'muted' | 'ok' | 'warn' | 'bad' }

export function DeviceKeyCard() {
  const [configured, setConfigured] = useState<boolean | null>(null)
  const [fingerprint, setFingerprint] = useState('--')
  const [key, setKey] = useState('')
  const [revealed, setRevealed] = useState(false)
  const [busy, setBusy] = useState(false)
  const [note, setNote] = useState<Note>({
    text: 'Stored keys are write-only and cannot be read back.',
    tone: 'muted',
  })
  const field = useRef<HTMLInputElement>(null)

  const load = useCallback(async () => {
    try {
      const fields = await api.deviceKeyStatus()
      setConfigured(fields.get('configured') === '1')
      setFingerprint(fields.get('fingerprint') ?? '--')
    } catch (error) {
      setConfigured(null)
      setNote({ text: `Cannot read key status: ${error}`, tone: 'bad' })
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  const generate = () => {
    const bytes = new Uint8Array(KEY_BYTES)
    crypto.getRandomValues(bytes)
    setKey(Array.from(bytes, (x) => x.toString(16).padStart(2, '0')).join(''))
    setRevealed(true)
    setNote({
      text: 'New key generated. Copy it before saving; it cannot be retrieved later.',
      tone: 'warn',
    })
  }

  const copy = async () => {
    if (!KEY_PATTERN.test(key.trim())) {
      setNote({
        text: 'Enter or generate exactly 64 hexadecimal characters.',
        tone: 'bad',
      })
      return
    }
    try {
      await navigator.clipboard.writeText(key.trim())
    } catch {
      // Clipboard API needs a secure context; the Pico is served over plain
      // HTTP on the LAN, so fall back to selecting the field for a manual copy.
      setRevealed(true)
      field.current?.select()
    }
    setNote({
      text: 'Key copied. Store the same value in Bambuddy before or after saving.',
      tone: 'ok',
    })
  }

  const save = async () => {
    const value = key.trim().toLowerCase()
    if (!KEY_PATTERN.test(value)) {
      setNote({
        text: 'Enter or generate exactly 64 hexadecimal characters.',
        tone: 'bad',
      })
      return
    }
    setBusy(true)
    try {
      await api.saveDeviceKey(value)
      setKey('')
      setRevealed(false)
      setNote({ text: 'Saved. BMB1 is reconnecting with the new key.', tone: 'ok' })
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
          <h3>BMB1 device key</h3>
          <div class="muted">
            Generate or paste a 256-bit key, copy it to Bambuddy, then save it on this
            Pico.
          </div>
        </div>
        <span class={`badge ${configured ? 'ok' : 'bad'}`}>{state}</span>
      </div>
      <label for="device-key">64 hexadecimal characters</label>
      <div class="key-row">
        <input
          id="device-key"
          ref={field}
          type={revealed ? 'text' : 'password'}
          maxLength={64}
          inputMode="text"
          autocomplete="new-password"
          spellcheck={false}
          placeholder="Generate a new key or paste an existing key"
          value={key}
          onInput={(event) => setKey(event.currentTarget.value)}
        />
        <button type="button" onClick={() => setRevealed(!revealed)}>
          {revealed ? 'Hide' : 'Show'}
        </button>
      </div>
      <div class="actions">
        <button type="button" onClick={generate}>
          Generate new
        </button>
        <button type="button" onClick={copy}>
          Copy
        </button>
        <button class="primary" type="button" disabled={busy} onClick={save}>
          Save key &amp; reconnect
        </button>
      </div>
      <p class="muted key-note">Fingerprint: {fingerprint}</p>
      <p class={`${note.tone} key-note`}>{note.text}</p>
    </article>
  )
}
