import type { LinkSnapshot } from '../api/snapshot'
import { Metric, count } from '../components/Metric'
import { linkName } from '../components/LoaderCard'

/**
 * Whether the printer still believes this is an AMS.
 *
 * That is the whole point of the device, and none of it reached any screen
 * before: the BMCU decodes the printer bus, counts its own registration
 * handshakes, and the numbers went straight into the bin.
 */
const AMS_GAP_WARN_MS = 1000
const CONFIRM_STALE_MS = 60_000

function Counter({
  label,
  value,
  tone,
}: {
  label: string
  value: number | undefined
  tone?: 'ok' | 'warn' | 'bad'
}) {
  // An absent counter is not a zero: the snapshot may simply not have arrived.
  if (value === undefined) return <Metric label={label} value="--" />
  return <Metric label={label} value={count(value)} tone={tone ?? ''} />
}

export function PrinterPage({ links }: { links: Map<number, LinkSnapshot> }) {
  const entries = [...links.values()].sort((a, b) => a.linkIndex - b.linkIndex)
  if (!entries.length) return <div class="empty">Waiting for the first snapshot...</div>

  return (
    <>
      {entries.map((link) => {
        const { ams, printer } = link
        const gapNow = ams.gapNowMs
        const sinceConfirm = ams.msSinceConfirm
        return (
          <section key={link.linkIndex}>
            <h2>{linkName(link.linkIndex)}</h2>

            {!link.complete ? (
              <div class="empty">No snapshot from this link yet</div>
            ) : (
              <>
                <h3>Is the printer still talking?</h3>
                <div class="bridge-grid">
                  <Counter
                    label="Service gap now"
                    value={gapNow}
                    tone={
                      gapNow !== undefined && gapNow > AMS_GAP_WARN_MS ? 'bad' : 'ok'
                    }
                  />
                  <Counter label="Worst gap" value={ams.gapMaxMs} />
                  <Counter label="Worst since confirm" value={ams.gapMaxSinceConfirmMs} />
                  <Counter
                    label="Since last confirm"
                    value={sinceConfirm}
                    tone={
                      sinceConfirm !== undefined && sinceConfirm > CONFIRM_STALE_MS
                        ? 'warn'
                        : 'ok'
                    }
                  />
                </div>

                <h3>AMS registration</h3>
                <div class="bridge-grid">
                  <Counter
                    label="Confirms"
                    value={ams.confirms}
                    tone={ams.confirms ? 'ok' : 'warn'}
                  />
                  <Counter label="Registration queries" value={ams.registrationQueries} />
                  <Counter
                    label="Would re-offer"
                    value={ams.wouldReoffer}
                    tone={ams.wouldReoffer ? 'warn' : 'ok'}
                  />
                  <Counter
                    label="Resets"
                    value={ams.resets}
                    tone={ams.resets ? 'warn' : 'ok'}
                  />
                </div>

                <h3>Printer bus receive</h3>
                <div class="bridge-grid">
                  <Counter label="Valid frames" value={printer.rxFramesValid} />
                  <Counter
                    label="Bad length"
                    value={printer.rxBadLength}
                    tone={printer.rxBadLength ? 'warn' : 'ok'}
                  />
                  <Counter
                    label="Header CRC"
                    value={printer.rxHeaderCrcError}
                    tone={printer.rxHeaderCrcError ? 'warn' : 'ok'}
                  />
                  <Counter
                    label="USART overrun"
                    value={printer.rxUsartOverrun}
                    tone={printer.rxUsartOverrun ? 'bad' : 'ok'}
                  />
                </div>

                <h3>Printer bus transmit</h3>
                <div class="bridge-grid">
                  <Counter label="Completed" value={printer.txCompleted} />
                  <Counter
                    label="Response missing"
                    value={printer.txResponseMissing}
                    tone={printer.txResponseMissing ? 'warn' : 'ok'}
                  />
                  <Counter
                    label="Timeout"
                    value={printer.txTimeout}
                    tone={printer.txTimeout ? 'warn' : 'ok'}
                  />
                  <Counter
                    label="No response expected"
                    value={printer.txNoResponseExpected}
                  />
                </div>

                {link.events.length > 0 && (
                  <>
                    <h3>Recent BMCU events</h3>
                    <pre>
                      {link.events
                        .map(
                          (event) =>
                            `[${event.hwTick32}] ${event.severityName} ` +
                            `${event.sourceName} record=${event.recordType} ` +
                            Array.from(event.detail)
                              .map((byte) => byte.toString(16).padStart(2, '0'))
                              .join(' '),
                        )
                        .join('\n')}
                    </pre>
                  </>
                )}
              </>
            )}
          </section>
        )
      })}
      <p class="muted key-note">
        A growing "since last confirm" with a healthy service gap means the
        printer is still polling but has stopped accepting the registration.
      </p>
    </>
  )
}
