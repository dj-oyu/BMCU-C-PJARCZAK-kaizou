import type { LoaderStatus } from '../api/decode'

/**
 * The switch reading behind the online bit. "Loaded" only means ks is nonzero,
 * which covers three physically different positions with different autoload
 * behaviour, so the position is shown rather than just the bit.
 */
const SWITCH_STATE = ['None', 'Both', 'Outer only', 'Inner only'] as const

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
  const flags = status.channelFlags[index]
  // Three latches with three different recoveries, previously indistinguishable
  // because all three showed as one red LED. A soft reset clears the first two;
  // an autoload failure needs the filament withdrawn all the way.
  const latches = flags
    ? ([
        flags.jamLatch && 'Jam',
        flags.lowLatch && !flags.jamLatch && 'Pull low',
        flags.dmFailLatch && 'Autoload failed',
      ].filter(Boolean) as string[])
    : []
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
        {flags && (
          <>
            <dt>Switch</dt>
            <dd class={flags.ks === 0 ? 'muted' : ''}>{SWITCH_STATE[flags.ks]}</dd>
            <dt>Latches</dt>
            <dd class={latches.length === 0 ? 'muted' : 'bad'}>
              {latches.length === 0 ? 'Clear' : latches.join(', ')}
            </dd>
          </>
        )}
        <dt>Motion</dt>
        <dd>{status.motion[index] ?? 0}</dd>
        <dt>Pull</dt>
        <dd>{status.pullPercent[index] ?? 0}%</dd>
      </dl>
    </article>
  )
}
