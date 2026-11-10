import { useEffect, useState } from 'react'
import { cls, inr } from '../fmt'
import { useDraft } from '../store'

/** D2/D3/D4: sweep settings on the server; pick on in-sample by the steady score, check out of sample. */
const PRESETS = (spec: any): [string, string, string][] => {
  const out: [string, string, string][] = [['risk.stop_loss.value', 'Stop loss', String(spec?.risk?.stop_loss?.value ?? '')]]
  if (spec?.risk?.trailing) out.push(['risk.trailing.value', 'Trail distance', String(spec.risk.trailing.value)])
  ;(spec?.legs || []).forEach((l: any, i: number) => out.push([`legs.${i}.strike.value`, `Leg ${i + 1} strike (${l.strike.by})`, String(l.strike.value)]))
  Object.entries(spec?.params || {}).forEach(([k, v]) => typeof v === 'number' && out.push([`params.${k}`, k.replace(/_/g, ' '), String(v)]))
  return out
}
export default function Sweep() {
  const [spec] = useDraft()
  const [sel, setSel] = useState<Record<string, string>>({})
  const [start, setStart] = useState('2024-10-15'), [split, setSplit] = useState('2026-01-01'), [end, setEnd] = useState('2026-10-21')
  const [folds, setFolds] = useState(0)
  const [job, setJob] = useState<any>(null), [res, setRes] = useState<any>(null), [err, setErr] = useState('')
  const grid = Object.fromEntries(Object.entries(sel).filter(([, v]) => v.trim()).map(([k, v]) => [k, v.split(',').map((x) => x.trim()).filter(Boolean).map((x) => (isNaN(+x) ? x : +x))]))
  const n = Object.values(grid).reduce((a: number, v: any) => a * v.length, 1)
  const run = () => {
    setErr(''); setRes(null)
    fetch('/api/sweeps', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ spec, grid, start, split, end, folds }) })
      .then(async (r) => { const j = await r.json(); if (!r.ok) throw new Error(j.detail?.errors?.[0]?.message || r.status); setJob({ id: j.id, progress: 0, status: 'queued' }) })
      .catch((e) => setErr(String(e.message || e)))
  }
  useEffect(() => {
    if (!job || job.status === 'done' || job.status === 'error') return
    const t = setTimeout(() => fetch(`/api/backtests/${job.id}`).then((r) => r.json()).then((j) => {
      setJob(j); if (j.status === 'done') fetch(`/api/sweeps/${j.sweep_id}`).then((r) => r.json()).then(setRes)
    }), 3000)
    return () => clearTimeout(t)
  }, [job])
  if (!spec) return <main className="fade"><div className="card small muted">Open a strategy or a result first, then choose Sweep.</div></main>
  return (
    <main className="fade">
      <div className="row"><button className="back" aria-label="Back" onClick={() => history.back()}>‹</button><h1 style={{ flex: 1 }}>Sweep · {spec.name}</h1></div>
      <section className="card">
        <h2>Settings to try</h2>
        <p className="small muted">Comma-separated values; leave empty to keep the strategy's value. At most 60 combinations.</p>
        {PRESETS(spec).map(([path, label, cur]) => (
          <div className="field" key={path}><label htmlFor={path}>{label} <span className="muted">(now {cur})</span></label>
            <input id={path} className="input n" inputMode="decimal" placeholder={`e.g. ${cur}`} value={sel[path] || ''} onChange={(e) => setSel({ ...sel, [path]: e.target.value })} /></div>))}
        <div className="small n">{n} combination{n === 1 ? '' : 's'}</div>
      </section>
      <section className="card">
        <h2>Periods</h2>
        <div className="grid3">
          <div className="field"><label>From</label><input className="input" type="date" value={start} onChange={(e) => setStart(e.target.value)} /></div>
          <div className="field"><label>Pick until</label><input className="input" type="date" value={split} onChange={(e) => setSplit(e.target.value)} /></div>
          <div className="field"><label>Check until</label><input className="input" type="date" value={end} onChange={(e) => setEnd(e.target.value)} /></div>
        </div>
        <div className="chips">{[0, 3, 4, 6].map((f) => <button key={f} className={`chip ${folds === f ? 'on' : ''}`} onClick={() => setFolds(f)}>{f ? `Walk-forward ${f}` : 'No walk-forward'}</button>)}</div>
      </section>
      <button className="btn primary" disabled={!Object.keys(grid).length || (job && !['done', 'error'].includes(job.status))} onClick={run}>Run sweep on the server</button>
      {err && <div className="alert">{err}</div>}
      {job && job.status !== 'done' && <div className="card small n">{job.status === 'error' ? <span className="dn">Stopped: {job.error}</span> : <>Running… {Math.round((job.progress || 0) * 100)}%</>}</div>}
      {res && <>
        {res.luck_warning && <div className="warn small">{res.luck_warning}</div>}
        <section className="card">
          <h2>Ranked (steady score, in-sample)</h2>
          <div className="list">{res.rows.filter((x: any) => !x.in_sample.error).sort((a: any, b: any) => (b.in_sample.score_steady ?? -1e9) - (a.in_sample.score_steady ?? -1e9)).map((x: any, i: number) => (
            <div className="li n" key={i} style={{ flexDirection: 'column', alignItems: 'stretch', gap: 2 }}>
              <span>{Object.entries(x.params).map(([k, v]) => `${k.split('.').pop()} ${v}`).join(' · ')}</span>
              <span className="small muted">in: <span className={cls(x.in_sample.net_pnl)}>{inr(x.in_sample.net_pnl, true)}</span> · DD {inr(-x.in_sample.max_drawdown)} · score {x.in_sample.score_steady ?? '—'}
                {x.out_of_sample && <> · <b>out: <span className={cls(x.out_of_sample.net_pnl)}>{inr(x.out_of_sample.net_pnl, true)}</span></b> DD {inr(-x.out_of_sample.max_drawdown)}</>}</span>
            </div>))}</div>
        </section>
        {res.walk_forward?.length > 0 && <section className="card"><h2>Walk-forward</h2><div className="list">{res.walk_forward.map((w: any, i: number) => (
          <div className="li n" key={i}><span>{w.test[0]} → {w.test[1]}<br /><span className="muted small">{Object.entries(w.params).map(([k, v]) => `${k.split('.').pop()} ${v}`).join(' · ')}</span></span>
            <span className={cls(w.test_result.net_pnl)}>{inr(w.test_result.net_pnl, true)}</span></div>))}</div></section>}
      </>}
    </main>)
}
