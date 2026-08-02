import type { LogRecord } from '../api/decode'
import { LogSeverity } from '../api/generated'

const SEVERITY_NAMES = new Map<number, string>(
  Object.entries(LogSeverity).map(([name, value]) => [value, name.toUpperCase()]),
)

/**
 * The component name is the fastest way to tell which subsystem is raising --
 * ``journal.flush`` and ``bambuddy.binary`` mean very different things -- so it
 * is rendered as its own column rather than folded into the message.
 */
export function LogView({ records }: { records: readonly LogRecord[] }) {
  if (!records.length) return <pre>No runtime log</pre>
  return (
    <pre>
      {records
        .map((record) => {
          const severity = SEVERITY_NAMES.get(record.severity) ?? String(record.severity)
          return `[${record.uptimeMs} ms] ${severity} ${record.component}: ${record.message}`
        })
        .join('\n')}
    </pre>
  )
}
