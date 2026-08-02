import { type LinkCounters, type LoaderStatus } from '../api/decode'
import { SLOT_COUNT } from '../api/layout'
import { Metric, count } from './Metric'
import { SlotCard } from './SlotCard'

/** Pin labels match the BMCU_LINKS defaults in pico/config_example.py. */
const LINK_PINS = ['GP0 TX / GP1 RX', 'GP4 TX / GP5 RX'] as const
const LINK_ACCENTS = ['var(--a)', 'var(--b)'] as const

export function linkName(link: number): string {
  return `bmcu-${String.fromCharCode(97 + link)}`
}

export function LoaderCard({
  link,
  status,
  counters,
}: {
  link: number
  status: LoaderStatus | null
  counters: LinkCounters
}) {
  const errors = counters.crcErrors + counters.frameErrors + counters.overflows
  const selected =
    status && status.selectedSlot !== null ? `Slot ${status.selectedSlot + 1}` : 'None'
  const loadedSlots = status
    ? status.filamentLoaded.filter(Boolean).length
    : 0

  return (
    <article class="loader" style={{ '--accent': LINK_ACCENTS[link] ?? 'var(--a)' }}>
      <div class="loader-head">
        <div>
          <div class="loader-name">
            <span class="dot" />
            <h3>{linkName(link)}</h3>
          </div>
          <div class="muted">
            UART{link} / {LINK_PINS[link] ?? 'unmapped pins'}
          </div>
        </div>
        <span class={`badge ${status ? 'ok' : 'bad'}`}>
          {status ? 'Receiving' : 'Waiting'}
        </span>
      </div>

      <div class="summary-grid">
        <Metric label="Selected" value={selected} />
        <Metric label="Filament loaded" value={`${loadedSlots} / ${SLOT_COUNT}`} />
        <Metric label="RX bytes" value={count(counters.rxBytes)} />
        <Metric label="Errors" value={count(errors)} tone={errors ? 'warn' : 'ok'} />
      </div>

      {status ? (
        <div class="slots">
          {Array.from({ length: SLOT_COUNT }, (_, index) => (
            <SlotCard key={index} status={status} index={index} />
          ))}
        </div>
      ) : (
        <div class="empty">No STATUS received on UART{link}</div>
      )}

      <div class="comm-grid">
        <Metric
          label="Backlog / peak"
          value={`${count(counters.backlog)} / ${count(counters.peakBacklog)}`}
          tone={counters.peakBacklog >= 4096 ? 'warn' : ''}
        />
        <Metric
          label="CRC / frame"
          value={`${count(counters.crcErrors)} / ${count(counters.frameErrors)}`}
          tone={counters.crcErrors + counters.frameErrors ? 'warn' : 'ok'}
        />
        <Metric
          label="Sequence gaps"
          value={count(counters.sequenceGaps)}
          tone={counters.sequenceGaps ? 'warn' : 'ok'}
        />
        <Metric
          label="Overflows"
          value={count(counters.overflows)}
          tone={counters.overflows ? 'warn' : 'ok'}
        />
      </div>
    </article>
  )
}
