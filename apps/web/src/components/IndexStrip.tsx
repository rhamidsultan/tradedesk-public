import { useState } from 'react'
import { api } from '../api'
import { usePoll } from '../hooks/usePoll'

const NAMES: [string, string][] = [['NIFTY', 'NIFTY'], ['BANKNIFTY', 'BANK NIFTY'], ['SENSEX', 'SENSEX'], ['INDIAVIX', 'VIX']]

/** C2: live index strip; tap opens the chart. */
export default function IndexStrip() {
  const [m, setM] = useState<any>(null)
  usePoll(() => api.market().then(setM).catch(() => { /* keep last */ }), true)
  if (!m?.ok) return null
  return (
    <div className="istrip" role="list" aria-label="Indices">
      {NAMES.map(([k, label]) => {
        const q = m.quotes?.[k]
        if (!q) return null
        const ch = q.prev_close ? q.ltp - q.prev_close : 0
        const pct = q.prev_close ? (ch / q.prev_close) * 100 : 0
        return (
          <a role="listitem" key={k} href={`#/chart/${k}`} className="iq">
            <span className="small muted">{label}</span>
            <b className="n">{q.ltp.toLocaleString('en-IN', { maximumFractionDigits: 2 })}</b>
            <span className={`small n ${ch > 0 ? 'up' : ch < 0 ? 'dn' : 'muted'}`}>{ch > 0 ? '▲' : ch < 0 ? '▼' : ''}{Math.abs(pct).toFixed(2)}%</span>
          </a>)
      })}
    </div>)
}
