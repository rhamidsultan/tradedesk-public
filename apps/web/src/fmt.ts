/** H5: hide amounts (per device) — money shows as ₹•••• but direction arrows stay. */
export function amountsHidden(): boolean { try { return localStorage.getItem('td-hide') === '1' } catch { return false } }
export function setAmountsHidden(v: boolean) { try { localStorage.setItem('td-hide', v ? '1' : '0') } catch { /* ignore */ } dispatchEvent(new Event('td-tz')) }
/** Indian-format money and signed P&L, as in the prototypes. */
export function inr(x: number | null | undefined, signed = false): string {
  if (x === null || x === undefined || Number.isNaN(x)) return '—'
  const v = Math.round(Math.abs(x))
  const s = String(v)
  let out = s
  if (s.length > 3) {
    let head = s.slice(0, -3)
    const tail = s.slice(-3)
    const parts: string[] = []
    while (head.length > 2) { parts.unshift(head.slice(-2)); head = head.slice(0, -2) }
    if (head) parts.unshift(head)
    out = parts.join(',') + ',' + tail
  }
  if (amountsHidden()) out = '••••'
  if (signed) return (x < 0 ? '▼ −₹' : x > 0 ? '▲ +₹' : '₹') + out
  return (x < 0 ? '−₹' : '₹') + out
}
export const lakh = (x: number) => (Math.abs(x) >= 1e5 && !amountsHidden() ? `${x < 0 ? '−' : ''}₹${(Math.abs(x) / 1e5).toFixed(2)} L` : inr(x))
export const cls = (x: number | null | undefined) => (x === null || x === undefined ? '' : x > 0 ? 'up' : x < 0 ? 'dn' : 'muted')
export function fmtDate(d: string): string {
  const dt = new Date(d.slice(0, 10) + 'T00:00:00')
  return dt.toLocaleDateString('en-IN', { day: 'numeric', month: 'short', year: 'numeric' })
}

/** A1: times are stored in IST; show them in IST (default) or in this device's time zone. */
export type Tz = 'ist' | 'device'
export function getTz(): Tz { try { return (localStorage.getItem('td-tz') as Tz) || 'ist' } catch { return 'ist' } }
export function setTz(t: Tz) { try { localStorage.setItem('td-tz', t) } catch { /* ignore */ } dispatchEvent(new Event('td-tz')) }
const asDate = (ist: string) => new Date(ist.replace(' ', 'T').slice(0, 19) + '+05:30')
/** "2026-10-23 10:55:00" (IST) → "10:55" in the chosen zone. */
export function tm(ist?: string | null): string {
  if (!ist) return '—'
  if (getTz() === 'ist' || ist.length < 16) return ist.slice(11, 16) || ist
  return asDate(ist).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', hour12: false })
}
/** "23 Oct 10:55" in the chosen zone. */
export function dtm(ist?: string | null): string {
  if (!ist) return '—'
  if (getTz() === 'ist') return `${fmtDate(ist).replace(/ \d{4}$/, '')} ${ist.slice(11, 16)}`
  const d = asDate(ist)
  return d.toLocaleDateString('en-IN', { day: 'numeric', month: 'short' }) + ' ' + d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', hour12: false })
}
export const tzLabel = () => (getTz() === 'ist' ? 'IST' : Intl.DateTimeFormat().resolvedOptions().timeZone.split('/').pop()?.replace('_', ' ') || 'local')
