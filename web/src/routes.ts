/**
 * Hash routing, hand-rolled.
 *
 * A router library would be most of the page's transfer budget again, and the
 * device streams this file off littlefs. Hash routes also survive being served
 * from a single static file with no server-side rewriting.
 */
import { useEffect, useState } from 'preact/hooks'

export const ROUTES = [
  {
    path: 'operate',
    label: 'Operate',
    question: 'What is each loader doing right now?',
  },
  {
    path: 'setup',
    label: 'Setup',
    question: 'Is every channel wired, sensed and moving?',
  },
  {
    path: 'printer',
    label: 'Printer',
    question: 'Is the printer still treating this as an AMS?',
  },
  {
    path: 'diagnostics',
    label: 'Diagnostics',
    question: 'What is wrong, and on which side of the link?',
  },
] as const

export type RoutePath = (typeof ROUTES)[number]['path']

const DEFAULT: RoutePath = 'operate'

function currentRoute(): RoutePath {
  const hash = window.location.hash.replace(/^#\/?/, '')
  return ROUTES.some((route) => route.path === hash) ? (hash as RoutePath) : DEFAULT
}

export function useRoute(): RoutePath {
  const [route, setRoute] = useState<RoutePath>(currentRoute)
  useEffect(() => {
    const update = () => setRoute(currentRoute())
    window.addEventListener('hashchange', update)
    return () => window.removeEventListener('hashchange', update)
  }, [])
  return route
}

export function href(path: RoutePath): string {
  return `#/${path}`
}
