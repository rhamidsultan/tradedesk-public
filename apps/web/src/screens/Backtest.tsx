import { useEffect, useRef, useState } from 'react'
import { api, type Job } from '../api'
import Result from '../components/Result'
import { IBack } from '../icons'
import { go, useDraft } from '../store'

export default function Backtest() {
  const [spec] = useDraft()
  const [start, setStart] = useState('2024-10-15')
  const [end, setEnd] = useState('')
  const [job, setJob] = useState<Job | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [frac, setFrac] = useState(0.5)
  const [slLimit, setSlLimit] = useState(false)
  const [where, setWhere] = useState(() => { try { return localStorage.getItem('td-where') || 'server' } catch { return 'server' } })
  const poll = useRef<number>()
  useEffect(() => { api.status().then((s) => setEnd(s.latest_data || '2026-10-21')).catch(() => setEnd('2026-10-21')) }, [])
  useEffect(() => () => window.clearTimeout(poll.current), [])
  if (!spec) return <main><div className="card">No strategy open. <a href="#/strategies">Pick a template</a>.</div></main>
  const run = async () => {
    setErr(null); setJob(null)
    try {
      const s = structuredClone(spec); s.costs = { ...(s.costs || {}), stop_fill_frac: frac, sl_order: slLimit ? 'limit' : 'assume_fill' }
      const { id } = await api.backtest(s, start, end, where)
      const tick = async () => {
        const j = await api.job(id); setJob(j)
        if (j.status === 'queued' || j.status === 'running') poll.current = window.setTimeout(tick, 1000)
      }
      tick()
    } catch (e: any) { setErr(String(e.message || e)) }
  }
  const running = job && (job.status === 'queued' || job.status === 'running')
  return (<>
    <header style={{ padding: '6px 8px 10px', borderBottom: '1px solid var(--bd)' }}>
      <div className="row" style={{ justifyContent: 'flex-start', gap: 4 }}>
        <button className="back" aria-label="Back to builder" onClick={() => go('/builder')}><IBack /></button>
        <div><b>{spec.name}</b><div className="small muted">Backtest · {spec.instrument.underlying} · net of all charges</div></div>
      </div>
    </header>
    <main className="fade">
      {job?.status !== 'done' && <>
        <div className="grid3" style={{ gridTemplateColumns: '1fr 1fr' }}>
          <div className="field"><label htmlFor="f">From</label><input id="f" className="input n" type="date" min="2024-10-15" value={start} onChange={(e) => setStart(e.target.value)} /></div>
          <div className="field"><label htmlFor="t">To</label><input id="t" className="input n" type="date" value={end} onChange={(e) => setEnd(e.target.value)} /></div>
        </div>
        <div className="field"><label>How stops fill inside a fast bar</label>
          <div className="chips">{[[0.25, 'Kind'], [0.5, 'Normal'], [1, 'Harsh']].map(([v, l]) =>
            <button key={l} className={`chip ${frac === v ? 'on' : ''}`} onClick={() => setFrac(v as number)}>{l as string}</button>)}</div>
          <div className="small muted">Kind = 25%, Normal = 50%, Harsh = the bar's worst price.</div></div>
        <div className="field"><label>Run on</label>
          <div className="chips">{[['server', 'Server'], ['mac', 'Mac (helper)']].map(([v, l]) =>
            <button key={v} className={`chip ${where === v ? 'on' : ''}`} onClick={() => { setWhere(v); try { localStorage.setItem('td-where', v) } catch { /* */ } }}>{l}</button>)}</div>
          {where === 'mac' && <div className="small muted">Needs the Mac helper running (scripts/mac-helper.sh). The job waits until the Mac picks it up.</div>}</div>
        <label className="toggle card" style={{ flexDirection: 'row' }}><span>Model Zerodha SL-limit orders<div className="small muted">A stop can stay unfilled in a spike, then chases</div></span>
          <input type="checkbox" checked={slLimit} onChange={(e) => setSlLimit(e.target.checked)} /></label>
        {running && <div className="card" aria-live="polite">
          <div className="row small"><span>{job!.status === 'queued' ? (where === 'mac' ? 'Waiting for the Mac helper…' : 'Waiting for the previous backtest…') : `Running · ${job!.at ?? ''}`}</span><span className="n">{Math.round((job!.progress || 0) * 100)}%</span></div>
          <div className="bar"><i style={{ width: `${(job!.progress || 0) * 100}%`, background: 'var(--ac)' }} /></div>
          <div className="small muted">A full 2-year run takes about 2–3 minutes.</div>
        </div>}
        {job?.status === 'error' && <div className="alert" role="alert">Backtest failed: {job.error}</div>}
        {err && <div className="alert" role="alert">{err}</div>}
      </>}
      {job?.status === 'done' && job.result && <Result r={job.result} />}
    </main>
    <footer className="footer">
      {job?.status === 'done' && <div className="small muted">Paper trading this strategy arrives in milestone 5.</div>}
      {job?.status === 'done'
        ? <div className="row" style={{ gap: 10 }}>
            <button className="btn" style={{ flex: 1 }} onClick={() => go('/builder')}>Adjust</button>
            <button className="btn primary" style={{ flex: 1.4 }} onClick={() => setJob(null)}>Run again</button>
          </div>
        : <button className="btn primary" disabled={!!running || !end} onClick={run}>{running ? 'Running…' : 'Run backtest'}</button>}
    </footer>
  </>)
}
