import { useState } from 'preact/hooks'
import { api, type ControlReply } from '../api/client'
import { linkName } from './LoaderCard'

type Note = { text: string; tone: 'muted' | 'ok' | 'warn' | 'bad' }

const CODE_PATTERN = /^[0-9]{4}$/

/**
 * Soft reset of one BMCU, confirmed with the 4-digit code the Pico shows on
 * its OLED panel (docs/BMCU_MONITOR_STREAM_V2.md section 7).
 *
 * The code is never sent to this page: reading it off the panel is the proof
 * of being at the machine. A reset clears the BMCU's merger ownership, so it
 * is not something to do mid-print without looking at the loaders.
 */
export function ResetCard({ links }: { links: number[] }) {
  const [link, setLink] = useState(links[0] ?? 0)
  const [code, setCode] = useState('')
  const [waiting, setWaiting] = useState(false)
  const [busy, setBusy] = useState(false)
  const [note, setNote] = useState<Note>({
    text: 'Request a code, then type the 4 digits shown on the Pico display.',
    tone: 'muted',
  })

  const explain = (reply: ControlReply): string =>
    reply.body.error ?? `HTTP ${reply.status}`

  const request = async () => {
    setBusy(true)
    try {
      const reply = await api.requestReset(link)
      if (reply.status === 202) {
        setWaiting(true)
        setCode('')
        setNote({
          text: `Code shown on the display for ${linkName(link)}. It expires in ${reply.body.expires_in_s ?? 60} s.`,
          tone: 'warn',
        })
      } else {
        setNote({ text: `Cannot request a code: ${explain(reply)}`, tone: 'bad' })
      }
    } catch (error) {
      setNote({ text: `Request failed: ${error}`, tone: 'bad' })
    } finally {
      setBusy(false)
    }
  }

  const submit = async () => {
    if (!CODE_PATTERN.test(code)) {
      setNote({ text: 'The code is exactly 4 digits.', tone: 'bad' })
      return
    }
    setBusy(true)
    try {
      const reply = await api.confirmReset(link, code)
      if (reply.status === 200) {
        setWaiting(false)
        setNote({ text: `Reset sent to ${linkName(link)}: ${reply.body.result}.`, tone: 'ok' })
      } else if (reply.status === 403) {
        setNote({
          text: `Wrong code. ${reply.body.attempts_left} attempt(s) left.`,
          tone: 'bad',
        })
      } else {
        // 409 refused by the soft-reset guard or no code; 410 expired or
        // invalidated. Either way a fresh code is needed.
        setWaiting(false)
        setNote({ text: `Not reset: ${explain(reply)}`, tone: 'bad' })
      }
    } catch (error) {
      setNote({ text: `Reset failed: ${error}`, tone: 'bad' })
    } finally {
      setBusy(false)
      setCode('')
    }
  }

  return (
    <article class="key-card">
      <div class="key-top">
        <div>
          <h3>Soft reset</h3>
          <div class="muted">Local only. Needs the code on the Pico display.</div>
        </div>
      </div>
      <div class="endpoint-row">
        <div>
          <label for="reset-link">BMCU</label>
          <select
            id="reset-link"
            value={String(link)}
            disabled={waiting}
            onChange={(event) => setLink(Number(event.currentTarget.value))}
          >
            {links.map((value) => (
              <option key={value} value={String(value)}>
                {linkName(value)}
              </option>
            ))}
          </select>
        </div>
        <div>
          <label for="reset-code">Code from display</label>
          <input
            id="reset-code"
            inputMode="numeric"
            autocomplete="off"
            maxLength={4}
            placeholder="0000"
            disabled={!waiting}
            value={code}
            onInput={(event) =>
              setCode(event.currentTarget.value.replace(/[^0-9]/g, '').slice(0, 4))
            }
          />
        </div>
      </div>
      <div class="actions">
        <button type="button" disabled={busy} onClick={request}>
          {waiting ? 'New code' : 'Show code on display'}
        </button>
        <button class="primary" type="button" disabled={busy || !waiting} onClick={submit}>
          Reset
        </button>
      </div>
      <p class={`${note.tone} key-note`}>{note.text}</p>
    </article>
  )
}
