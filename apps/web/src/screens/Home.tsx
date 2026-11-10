import { useEffect, useState } from 'react'
import { api, type RunRow } from '../api'
import { cls, fmtDate, inr } from '../fmt'
import { go, useDraft } from '../store'
import IndexStrip from '../components/IndexStrip'

export default function Home({ status }: { status: any }) {
  const [runs, setRuns] = useState<RunRow[] | null>(null)
  const [draft] = useDraft()
  useEffect(() => { api.runs().then(setRuns).catch(() => setRuns([])) }, [])
  return (
    <main className="fade">
      <h1 style={{ fontSize: 24 }}>TradeDesk</h1>
      <IndexStrip />
      <a className="card" href="#/trade" style={{ textDecoration: 'none', color: 'var(--tx)' }}><div className="row"><h2>Paper and live</h2><span className="muted">›</span></div>
        <div className="small muted">Sessions, positions and today's P&L</div></a>
      <section className="card">
        <div className="row"><h2>Data</h2><a href="#/more" className="small" style={{ minHeight: 44, display: 'flex', alignItems: 'center' }}>Details</a></div>
        <div className="small muted n">{status?.store_ok ? <>1-minute option and index data {status.first_data ? fmtDate(status.first_data) : ''} → {fmtDate(status.latest_data)}</> : 'Store not found on the server'}</div>
      </section>
      {draft && <section className="card">
        <div className="row"><h2>Continue editing</h2><span className="tag">draft</span></div>
        <div className="small">{draft.name}</div>
        <div className="row" style={{ gap: 8 }}>
          <a className="btn" style={{ flex: 1 }} href="#/builder">Edit</a>
          <a className="btn primary" style={{ flex: 1 }} href="#/backtest">Backtest</a>
        </div>
      </section>}
      <section style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
        <div className="row"><h2>Latest results</h2><a href="#/strategies?tab=results" className="small" style={{ minHeight: 44, display: 'flex', alignItems: 'center' }}>All</a></div>
        {runs === null && <div className="card small muted">Loading…</div>}
        {runs?.length === 0 && <div className="card small muted">No backtests yet. Start from a template.</div>}
        {runs?.slice(0, 4).map((r) => (
          <a key={r.run_id} href={`#/run/${encodeURIComponent(r.run_id)}`} className="card" style={{ textDecoration: 'none', color: 'var(--tx)' }}>
            <div className="row"><b>{r.name}</b><b className={`n ${cls(r.net_pnl)}`} style={{ whiteSpace: 'nowrap' }}>{inr(r.net_pnl, true)}</b></div>
            <div className="small muted n">{fmtDate(r.start)} → {fmtDate(r.end)} · {r.trades} closed trades · worst day {inr(r.worst_day)}</div>
          </a>))}
      </section>
      <button className="btn primary" onClick={() => go('/strategies')}>New strategy from a template</button>
    </main>)
}
