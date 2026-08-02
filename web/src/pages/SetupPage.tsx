import type { LinkSnapshot } from '../api/snapshot'
import { SLOT_COUNT } from '../api/layout'
import { linkName } from '../components/LoaderCard'

/**
 * The page you open once, after wiring a loader: is every channel physically
 * connected, is its magnetic encoder answering, and does the motor move?
 *
 * None of this was visible before. The values come from the FULL_STATUS
 * snapshot, which arrives once per link session, so the page reads on demand
 * rather than on a timer.
 */
export function SetupPage({ links }: { links: Map<number, LinkSnapshot> }) {
  const entries = [...links.values()].sort((a, b) => a.linkIndex - b.linkIndex)
  if (!entries.length) return <div class="empty">Waiting for the first snapshot...</div>

  return (
    <>
      {entries.map((link) => (
        <section key={link.linkIndex}>
          <h2>
            {linkName(link.linkIndex)}
            {link.summary && <span class="muted"> — {link.summary.state}</span>}
          </h2>

          {!link.complete ? (
            // Deliberately not a grid of zeroes: nothing has been reported.
            <div class="empty">
              No snapshot from this link yet
              {link.summary?.state === 'resyncing' && ' — it is resyncing now'}
            </div>
          ) : (
            <div class="slots setup-slots">
              {Array.from({ length: SLOT_COUNT }, (_, index) => {
                const channel = link.channels[index]
                if (!channel) {
                  return (
                    <article class="slot absent" key={index}>
                      <div class="slot-title">
                        <span>Channel {index + 1}</span>
                      </div>
                      <div class="muted">not reported</div>
                    </article>
                  )
                }
                const sensorTone = channel.sensorGood
                  ? 'ok'
                  : channel.sensorOnline
                    ? 'warn'
                    : 'bad'
                return (
                  <article
                    class={`slot ${channel.present ? '' : 'absent'}`.trim()}
                    key={index}
                  >
                    <div class="slot-title">
                      <span>Channel {index + 1}</span>
                      {channel.motionFault !== 0 && (
                        <span class="pill bad">fault {channel.motionFault}</span>
                      )}
                    </div>
                    <dl>
                      <dt>Wired</dt>
                      <dd class={channel.present ? 'ok' : 'bad'}>
                        {channel.present ? 'Yes' : 'No'}
                      </dd>
                      <dt>Sensor</dt>
                      <dd class={sensorTone}>
                        {channel.sensorGood
                          ? 'Good'
                          : channel.sensorOnline
                            ? 'Noisy'
                            : 'Absent'}
                      </dd>
                      <dt>Angle</dt>
                      <dd>{channel.rawAngle}</dd>
                      <dt>Delta</dt>
                      <dd class={channel.positionDelta ? 'ok' : 'muted'}>
                        {channel.positionDelta > 0 ? '+' : ''}
                        {channel.positionDelta}
                      </dd>
                      <dt>Motor</dt>
                      <dd>{channel.motorPwm}</dd>
                      <dt>Pull</dt>
                      <dd>{channel.pullPercent}%</dd>
                      <dt>Phase</dt>
                      <dd class={channel.controllerMotion === null ? 'muted' : ''}>
                        {channel.controllerMotion ?? '--'}
                      </dd>
                    </dl>
                  </article>
                )
              })}
            </div>
          )}
        </section>
      ))}
      <p class="muted key-note">
        Angle is the raw AS5600 reading and Delta its change since the last
        report: a channel whose motor is driving but whose angle never moves has
        a sensor or a coupling problem, not a filament one.
      </p>
    </>
  )
}
