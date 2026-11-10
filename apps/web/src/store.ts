import { useEffect, useState } from 'react'
import type { Spec } from './api'

/** The strategy being edited, shared by Builder and Backtest; kept in localStorage as a per-device convenience. */
let draft: Spec | null = null
const subs = new Set<(s: Spec | null) => void>()
try { const s = localStorage.getItem('td-draft'); if (s) draft = JSON.parse(s) } catch { /* ignore */ }
export function setDraft(s: Spec | null) {
  draft = s ? structuredClone(s) : null
  try { if (draft) localStorage.setItem('td-draft', JSON.stringify(draft)); else localStorage.removeItem('td-draft') } catch { /* ignore */ }
  subs.forEach((f) => f(draft))
}
export function useDraft(): [Spec | null, (s: Spec | null) => void] {
  const [s, set] = useState<Spec | null>(draft)
  useEffect(() => { subs.add(set); return () => { subs.delete(set) } }, [])
  return [s, setDraft]
}
export function useRoute(): [string, URLSearchParams] {
  const read = () => { const h = location.hash.slice(1) || '/'; const [p, q] = h.split('?'); return [p, new URLSearchParams(q || '')] as [string, URLSearchParams] }
  const [r, setR] = useState(read)
  useEffect(() => { const f = () => { setR(read()); document.querySelector('main')?.scrollTo(0, 0) }; addEventListener('hashchange', f); return () => removeEventListener('hashchange', f) }, [])
  return r
}
export const go = (path: string) => { location.hash = path }
