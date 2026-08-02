import { linkCounters, type Telemetry } from '../api/decode'
import { LoaderCard } from '../components/LoaderCard'

/**
 * The page you glance at while a print runs.
 *
 * Deliberately carries no diagnostic counters. Backlog, CRC errors, sequence
 * gaps and heap were all here before, given the same visual weight as which
 * filament is loaded; they answer a different question, for a different moment,
 * and they now live on the diagnostics page.
 */
export function OperatePage({
  telemetry,
  links,
}: {
  telemetry: Telemetry
  links: number[]
}) {
  return (
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
  )
}
