import { useMemo, useState } from 'react'
import { inr } from '../fmt'

/** B6 / D1: closed-cycle P&L by the day each cycle closed. days = {"2026-10-23": {net, cycles}} */
export default function Calendar({ days, onDay }: { days: Record<string, { net: number; cycles: number }>; onDay?: (d: string) => void }) {
  const keys = Object.keys(days).sort()
  const months = useMemo(() => Array.from(new Set(keys.map((k) => k.slice(0, 7)))), [keys.join()])
  const [mi, setMi] = useState(months.length - 1)
  if (!months.length) return <div className="small muted">No closed cycles yet.</div>
  const ym = months[Math.max(0, Math.min(mi, months.length - 1))]
  const [y, m] = ym.split('-').map(Number)
  const first = new Date(y, m - 1, 1).getDay() // 0 Sun
  const n = new Date(y, m, 0).getDate()
  const max = Math.max(1, ...keys.filter((k) => k.startsWith(ym)).map((k) => Math.abs(days[k].net)))
  const tot = keys.filter((k) => k.startsWith(ym)).reduce((a, k) => a + days[k].net, 0)
  const cells = [...Array((first + 6) % 7).fill(null), ...Array.from({ length: n }, (_, i) => i + 1)]
  return (
    <div className="cal">
      <div className="row">
        <button className="back" aria-label="Previous month" disabled={mi <= 0} onClick={() => setMi(mi - 1)}>‹</button>
        <b>{new Date(y, m - 1, 1).toLocaleDateString('en-IN', { month: 'short', year: 'numeric' })} <span className={`n ${tot > 0 ? 'up' : tot < 0 ? 'dn' : 'muted'}`}>{inr(tot, true)}</span></b>
        <button className="back" aria-label="Next month" disabled={mi >= months.length - 1} onClick={() => setMi(mi + 1)}>›</button>
      </div>
      <div className="calgrid">
        {['M', 'T', 'W', 'T', 'F', 'S', 'S'].map((d, i) => <span key={'h' + i} className="small muted calh">{d}</span>)}
        {cells.map((d, i) => {
          if (d === null) return <span key={i} />
          const k = `${ym}-${String(d).padStart(2, '0')}`
          const v = days[k]
          const a = v ? 0.18 + 0.62 * Math.min(1, Math.abs(v.net) / max) : 0
          return <button key={i} className="calday n" disabled={!v} onClick={() => v && onDay?.(k)}
            title={v ? `${k}: ${inr(v.net, true)} · ${v.cycles} cycle${v.cycles > 1 ? 's' : ''}` : k}
            style={v ? { background: `color-mix(in srgb, ${v.net >= 0 ? 'var(--up)' : 'var(--dn)'} ${Math.round(a * 100)}%, transparent)` } : undefined}>{d}</button>
        })}
      </div>
    </div>)
}
