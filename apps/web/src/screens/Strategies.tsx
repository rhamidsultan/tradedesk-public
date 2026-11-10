import { useEffect, useState } from 'react'
import { api, type RunRow, type Template } from '../api'
import { cls, fmtDate, inr } from '../fmt'
import { go, setDraft, useRoute } from '../store'

export default function Strategies() {
  const [, q] = useRoute()
  const [tab, setTab] = useState(q.get('tab') || 'templates')
  const [tpls, setTpls] = useState<Template[] | null>(null)
  const [mine, setMine] = useState<any[] | null>(null)
  const [runs, setRuns] = useState<RunRow[] | null>(null)
  const [pick, setPick] = useState<string[]>([])
  const [msg, setMsg] = useState('')
  useEffect(() => {
    api.templates().then(setTpls).catch(() => setTpls([]))
    api.strategies().then(setMine).catch(() => setMine([]))
    api.runs().then(setRuns).catch(() => setRuns([]))
  }, [])
  const open = (spec: any, to: string) => { setDraft(spec); go(to) }
  return (
    <main className="fade">
      <div className="row"><h1>Strategies</h1></div>
      <div className="chips" role="tablist">
        {[['templates', 'Templates'], ['mine', 'My strategies'], ['results', 'Results']].map(([id, l]) =>
          <button key={id} role="tab" aria-selected={tab === id} className={`chip ${tab === id ? 'on' : ''}`} onClick={() => setTab(id)}>{l}</button>)}
      </div>
      {tab === 'templates' && (tpls ?? []).map((t) => (
        <div className="card" key={t.id}>
          <div className="row"><b style={{ fontSize: 16 }}>{t.name}</b><span className="tag">{t.kind}</span></div>
          <div className="small muted">{t.notes}</div>
          <div className="small muted n">{t.underlying} · stop {t.spec.risk.stop_loss.value}{t.spec.risk.stop_loss.type === 'multiple' ? '× premium' : ' pts on spot'} · max loss/day {inr(t.spec.risk.max_loss_per_day)}</div>
          <div className="row" style={{ gap: 8 }}>
            <button className="btn" style={{ flex: 1 }} onClick={() => open(t.spec, '/builder')}>Customise</button>
            <button className="btn primary" style={{ flex: 1.3 }} onClick={() => open(t.spec, '/backtest')}>Backtest it</button>
          </div>
        </div>))}
      {tab === 'mine' && <label className="btn" style={{ cursor: 'pointer' }}>Import a strategy file (.json)
        <input type="file" accept="application/json,.json" hidden onChange={(e) => {
          const f = e.target.files?.[0]; if (!f) return
          f.text().then((t) => api.save(JSON.parse(t))).then(() => { setMsg('Imported.'); api.strategies().then(setMine) })
            .catch((er) => setMsg('Not imported: ' + String(er.message || er).replace(/^\d+ /, '')))
        }} /></label>}
      {tab === 'mine' && msg && <div className="small" role="status">{msg}</div>}
      {tab === 'mine' && (mine?.length ? mine.map((s) => (
        <div className="card" key={s.id}>
          <div className="row"><b>{s.name}</b><span className="tag">v{s.version}</span></div>
          <div className="row" style={{ gap: 8 }}>
            <button className="btn" style={{ flex: 1 }} onClick={() => open(s, '/builder')}>Edit</button>
            <button className="btn primary" style={{ flex: 1 }} onClick={() => open(s, '/backtest')}>Backtest</button>
            <a className="btn" style={{ flex: 1 }} download={`${s.id}.json`} href={URL.createObjectURL(new Blob([JSON.stringify(s, null, 1)], { type: 'application/json' }))}>Export</a>
            <button className="btn paper" style={{ flex: 1 }} onClick={() => api.paperStart(s.id).then((r) => go('/paper/' + encodeURIComponent(r.id))).catch((e) => alert(String(e.message || e).replace(/^\d+ /, '')))}>Paper</button>
          </div>
        </div>)) : <div className="card small muted">Nothing saved yet. Customise a template and save it from the Review step.</div>)}
      {tab === 'results' && pick.length > 0 && <button className="btn primary" disabled={pick.length < 2} onClick={() => go('/compare/' + pick.map(encodeURIComponent).join(','))}>Compare {pick.length} (pick 2–3)</button>}
      {tab === 'results' && (runs?.length ? runs.map((r) => (
        <div key={r.run_id} className="row" style={{ alignItems: 'stretch', gap: 6 }}>
        <label className="chip" style={{ display: 'flex', alignItems: 'center', minWidth: 44, justifyContent: 'center' }} aria-label="Select to compare">
          <input type="checkbox" checked={pick.includes(r.run_id)} onChange={(e) => setPick(e.target.checked ? [...pick, r.run_id].slice(-3) : pick.filter((x) => x !== r.run_id))} /></label>
        <a href={`#/run/${encodeURIComponent(r.run_id)}`} className="card" style={{ textDecoration: 'none', color: 'var(--tx)', flex: 1, minWidth: 0 }}>
          <div className="row"><b>{r.name}</b><b className={`n ${cls(r.net_pnl)}`} style={{ whiteSpace: 'nowrap' }}>{inr(r.net_pnl, true)}</b></div>
          <div className="small muted n">{fmtDate(r.start)} → {fmtDate(r.end)} · {r.trades} closed trades · win {r.win_rate_pct ?? '—'}% · max DD {inr(-(r.max_drawdown || 0))}</div>
          <div className="small muted" style={{ overflowWrap: 'anywhere' }}>{r.run_id}</div>
        </a></div>)) : <div className="card small muted">No results yet.</div>)}
    </main>)
}
