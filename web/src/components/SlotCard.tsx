import type { LoaderStatus } from '../api/decode'

/**
 * The two masks are easy to mix up, and the previous UI did: it labelled the
 * inserted mask "Filament". The inserted mask is per-channel hardware presence
 * latched at boot from the PULL potentiometer voltage; the online mask is the
 * microswitch, which is the one that actually tracks filament.
 */
export function SlotCard({ status, index }: { status: LoaderStatus; index: number }) {
  const selected = status.selectedSlot === index
  const present = status.channelPresent[index] ?? false
  const loaded = status.filamentLoaded[index] ?? false
  return (
    <article class={`slot ${selected ? 'selected ' : ''}${present ? '' : 'absent'}`.trim()}>
      <div class="slot-title">
        <span>Slot {index + 1}</span>
        {selected && <span class="pill ok">Selected</span>}
      </div>
      <dl>
        <dt>Channel</dt>
        <dd class={present ? '' : 'muted'}>{present ? 'Present' : 'Absent'}</dd>
        <dt>Filament</dt>
        <dd class={loaded ? 'ok' : 'muted'}>{loaded ? 'Loaded' : 'Empty'}</dd>
        <dt>Motion</dt>
        <dd>{status.motion[index] ?? 0}</dd>
        <dt>Pull</dt>
        <dd>{status.pullPercent[index] ?? 0}%</dd>
      </dl>
    </article>
  )
}
