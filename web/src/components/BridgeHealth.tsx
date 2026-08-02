import { Diag } from '../api/generated'
import { counter, optionalCounter, type TlvValue } from '../api/decode'
import { Metric, count, type Tone } from './Metric'

const HEAP_WARN_BYTES = 20_000
const LOOP_P99_WARN_US = 100_000
const QUEUE_WARN_DEPTH = 128

export function BridgeHealth({ diagnostics }: { diagnostics: Map<number, TlvValue> }) {
  const at = (tag: number) => counter(diagnostics, tag)
  const heapFree = at(Diag.HeapFree)
  const loopP99 = at(Diag.LoopGapP99Us)
  const exceptions = at(Diag.ExceptionCount)
  const queueDepth = at(Diag.QueueDepth)
  const drops = at(Diag.QueueDropCount)
  const rssi = optionalCounter(diagnostics, Diag.WifiRssiDbm)

  const tone = (bad: boolean, good: Tone = ''): Tone => (bad ? 'warn' : good)

  return (
    <>
      <Metric label="Uptime" value={`${Math.floor(at(Diag.UptimeMs) / 1000)} s`} />
      <Metric
        label="Heap free"
        value={`${count(heapFree)} B`}
        tone={tone(heapFree < HEAP_WARN_BYTES)}
      />
      <Metric label="Wi-Fi RSSI" value={rssi === null ? '--' : `${rssi} dBm`} />
      <Metric
        label="Loop p99"
        value={`${count(loopP99)} us`}
        tone={tone(loopP99 > LOOP_P99_WARN_US)}
      />
      <Metric
        label="Exceptions"
        value={count(exceptions)}
        tone={exceptions ? 'bad' : 'ok'}
      />
      <Metric
        label="BMB1 queue"
        value={count(queueDepth)}
        tone={tone(queueDepth > QUEUE_WARN_DEPTH)}
      />
      <Metric label="Transport drops" value={count(drops)} tone={drops ? 'warn' : 'ok'} />
      <Metric label="TCP reconnects" value={count(at(Diag.TcpReconnectCount))} />
    </>
  )
}
