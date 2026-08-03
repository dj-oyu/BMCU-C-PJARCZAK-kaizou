import type { ComponentChildren } from 'preact'

export type Tone = '' | 'ok' | 'warn' | 'bad'

export function Metric({
  label,
  value,
  tone = '',
}: {
  label: string
  value: ComponentChildren
  tone?: Tone
}) {
  return (
    <div class={`metric ${tone}`.trim()}>
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  )
}

export function count(value: number): string {
  return value.toLocaleString()
}
