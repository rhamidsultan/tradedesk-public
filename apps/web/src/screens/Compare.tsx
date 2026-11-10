import { useEffect, useState } from 'react'
import { api } from '../api'
import { cls, fmtDate, inr } from '../fmt'

/** D9: two or three runs side by side, with their equity curves on one chart. */
const COLORS = ['var(--ac)', 'var(--live)', 'var(--paper)']
export default function Compare({ ids }: { ids: string[] }) {
  const [rs, setRs] = useState<any[]>([])
  useEffect(() => { Promise.all(ids.map((id) => api.run(id).catch(() => null))).then(setRs) }, [ids.join()])
  const ok = rs.filter(Boolean)
  const curves = ok.map((r) => { let e = 0; return r.daily.map(([d, p]: [string, number]) => [d, (e += p)]) })
  const all = curves.flat().map((x: any) => x[1]); const mx = Math.max(0, ...all), mn = Math.min(0, ...all)
  const dates = [...new Set(curves.flat().map((x: any) => x[0]))].sort()
  const X = (d: string) => (dates.indexOf(d) / Math.max(1, dates.length - 1)) * 340, Y = (v: number) => 8 + (mx - v) / Math.max(1, mx - mn) * 140
  const rows: [string, (m: any) => any][] = [['Net P&L', (m) => inr(m.net_pnl, true)], ['Max drawdown', (m) => inr(-m.max_drawdown)], ['Win rate', (m) => `${m.win_rate_pct ?? '—'}%`],
    ['Trades', (m) => m.trades], ['Expectancy', (m) => inr(m.expectancy, true)], ['Months profitable', (m) => `${m.months_profitable_pct ?? '—'}%`], ['Steady score', (m) => m.score_steady ?? '—'],
    ['Sortino', (m) => m.sortino ?? '—'], ['Charges', (m) => inr(m.charges)], ['Charges ×1.5', (m) => inr(m.net_with_charges_x1_5, true)]]
  return (
    <main className="fade">
      <div className="row"><button className="back" aria-label="Back" onClick={() => history.back()}>‹</button><h1 style={{ flex: 1 }}>Compare</h1></div>
      {ok.length > 0 && <section className="card">
        <svg viewBox="0 0 340 160" width="100%" role="img" aria-label="Equity curves">
          <line x1="0" x2="340" y1={Y(0)} y2={Y(0)} stroke="var(--bd)" />
          {curves.map((c, k) => <polyline key={k} fill="none" stroke={COLORS[k]} strokeWidth="2" points={c.map(([d, v]: any) => `${X(d).toFixed(1)},${Y(v).toFixed(1)}`).join(' ')} />)}
        </svg>
        {ok.map((r, k) => <div className="small" key={k}><span style={{ color: COLORS[k] }}>■</span> {r.strategy.name} · {fmtDate(r.start)} → {fmtDate(r.end)}</div>)}
      </section>}
      <section className="card" style={{ overflowX: 'auto' }}>
        <table className="small n" style={{ width: '100%', borderCollapse: 'collapse' }}>
          <thead><tr><th style={{ textAlign: 'left' }}></th>{ok.map((_r, k) => <th key={k} style={{ textAlign: 'right', color: COLORS[k] }}>{k + 1}</th>)}</tr></thead>
          <tbody>{rows.map(([label, f]) => <tr key={label}><td className="muted" style={{ padding: '6px 0' }}>{label}</td>{ok.map((r, k) => <td key={k} style={{ textAlign: 'right' }} className={label.startsWith('Net') ? cls(r.metrics.net_pnl) : ''}>{f(r.metrics)}</td>)}</tr>)}</tbody>
        </table>
      </section>
    </main>)
}
