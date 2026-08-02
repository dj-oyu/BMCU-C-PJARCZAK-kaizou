import { counter, linkCounters, type LogRecord, type Telemetry } from '../api/decode'
import type { LinkSnapshot } from '../api/snapshot'
import { Diag } from '../api/generated'
import { StatusPayload, statusFieldOffset } from '../api/layout'
import { BridgeHealth } from '../components/BridgeHealth'
import { LogView } from '../components/LogView'
import { Metric, count } from '../components/Metric'
import { linkName } from '../components/LoaderCard'

/**
 * The failure page: what is wrong, and on which side of the link.
 *
 * The Pico's decoder counters and the BMCU's own self-reported counters are
 * placed side by side on purpose. Matching rates mean a shared cause; a
 * one-sided count means the loss is in that direction only. Working that out
 * from the previous UI required reading the raw frames by hand.
 */
function bmcuSelfReported(payload: DataView | undefined, offset: number) {
  if (!payload) return undefined
  const absolute = statusFieldOffset(offset)
  if (absolute + 2 > payload.byteLength) return undefined
  return payload.getUint16(absolute, true)
}

export function DiagnosticsPage({
  telemetry,
  links,
  snapshot,
  logs,
  statusPayloads,
}: {
  telemetry: Telemetry
  links: number[]
  snapshot: Map<number, LinkSnapshot>
  logs: readonly LogRecord[]
  statusPayloads: Map<number, DataView>
}) {
  return (
    <>
      <section>
        <h2>Links</h2>
        {links.map((link) => {
          const pico = linkCounters(link, telemetry.diagnostics)
          const payload = statusPayloads.get(link)
          const state = snapshot.get(link)?.summary?.state
          const bmcuCrc = bmcuSelfReported(payload, StatusPayload.CrcErrorOffset)
          const bmcuFrame = bmcuSelfReported(payload, StatusPayload.FrameErrorOffset)
          const bmcuTxDrop = bmcuSelfReported(payload, StatusPayload.TxDropOffset)
          const bmcuRxDrop = bmcuSelfReported(payload, StatusPayload.RxDropOffset)
          return (
            <div key={link}>
              <h3>
                {linkName(link)}{' '}
                <span class={`badge ${state === 'online' ? 'ok' : 'warn'}`}>
                  {/* resyncing and stale are different problems */}
                  {state ?? 'unknown'}
                </span>
              </h3>
              <div class="bridge-grid">
                <Metric
                  label="Pico CRC / frame"
                  value={`${count(pico.crcErrors)} / ${count(pico.frameErrors)}`}
                  tone={pico.crcErrors + pico.frameErrors ? 'bad' : 'ok'}
                />
                <Metric
                  label="BMCU CRC / frame"
                  value={
                    bmcuCrc === undefined
                      ? '--'
                      : `${count(bmcuCrc)} / ${count(bmcuFrame ?? 0)}`
                  }
                  tone={bmcuCrc ? 'bad' : 'ok'}
                />
                <Metric
                  label="BMCU tx / rx drop"
                  value={
                    bmcuTxDrop === undefined
                      ? '--'
                      : `${count(bmcuTxDrop)} / ${count(bmcuRxDrop ?? 0)}`
                  }
                  tone={bmcuTxDrop ? 'warn' : 'ok'}
                />
                <Metric
                  label="Sequence gaps"
                  value={count(pico.sequenceGaps)}
                  tone={pico.sequenceGaps ? 'warn' : 'ok'}
                />
                <Metric
                  label="Backlog / peak"
                  value={`${count(pico.backlog)} / ${count(pico.peakBacklog)}`}
                />
                <Metric
                  label="Ring overflows"
                  value={count(pico.overflows)}
                  tone={pico.overflows ? 'bad' : 'ok'}
                />
                <Metric label="RX bytes" value={count(pico.rxBytes)} />
                <Metric
                  label="Service delay"
                  value={`${count(pico.maxServiceDelayUs)} us`}
                />
              </div>
            </div>
          )
        })}
        <p class="muted key-note">
          Both sides counting errors points at the wire. Only the Pico counting
          them points at this end: interrupt latency, or bytes discarded before
          they were parsed.
        </p>
      </section>

      <section>
        <h2>Bridge</h2>
        <div class="bridge-grid">
          <BridgeHealth diagnostics={telemetry.diagnostics} />
          <Metric
            label="Heap min ever"
            value={`${count(counter(telemetry.diagnostics, Diag.HeapMinFree))} B`}
            tone={
              counter(telemetry.diagnostics, Diag.HeapMinFree) < 20000 ? 'warn' : 'ok'
            }
          />
          <Metric
            label="Journal failures"
            value={count(counter(telemetry.diagnostics, Diag.JournalFailureCount))}
            tone={counter(telemetry.diagnostics, Diag.JournalFailureCount) ? 'bad' : 'ok'}
          />
          <Metric
            label="Ack watermark"
            value={count(counter(telemetry.diagnostics, Diag.LastAckWatermark))}
          />
          <Metric
            label="Replays"
            value={count(counter(telemetry.diagnostics, Diag.ReplayCount))}
          />
        </div>
        <p class="muted key-note">
          A queue depth pinned at its ring size is steady state, not a stall, as
          long as the ack watermark is advancing.
        </p>
      </section>

      <section>
        <h2>Recent device log</h2>
        <LogView records={logs} />
      </section>
    </>
  )
}
