import { useEffect, useState } from 'react'
import { api } from '../api'
import { cls, fmtDate, inr, tm } from '../fmt'
import { usePoll } from '../hooks/usePoll'
import Fold from '../components/Fold'
import Calendar from '../components/Calendar'
import IndexStrip from '../components/IndexStrip'
import KillSwitch from './KillSwitch'

const todayIst = () => new Date(Date.now() + 5.5 * 3600e3).toISOString().slice(0, 10)

export default function Paper() {
  const [d, setD] = useState<any>(null)
  const [live, setLive] = useState<any>(null)
  const [cal, setCal] = useState<any>({})
  const [mine, setMine] = useState<any[]>([])
  const [pick, setPick] = useState('')
  const [err, setErr] = useState('')
  const anyOpen = (d?.sessions || []).some((s: any) => s.open > 0)
  usePoll(() => {
    api.paper().then(setD).catch(() => setD((x: any) => x || { sessions: [], recorder: { message: 'Server not reachable' } }))
    api.live().then(setLive).catch(() => { /* keep */ })
  }, anyOpen)
  useEffect(() => { api.strategies().then(setMine).catch(() => setMine([])); api.paperCalendar().then(setCal).catch(() => setCal({})) }, [])
  const start = (mode = 'paper') => { setErr(''); api.paperStart(pick, mode).then((r) => { location.hash = '#/paper/' + encodeURIComponent(r.id) }).catch((e) => setErr(String(e.message || e))) }
  const rec = d?.recorder || {}
  const all = d?.sessions || []
  const running = all.filter((s: any) => s.status !== 'stopped')
  const stopped = all.filter((s: any) => s.status === 'stopped')
  const paper = running.filter((s: any) => s.mode !== 'live')
  const sum = (xs: any[], k: string) => xs.reduce((a, s) => a + (s[k] || 0), 0)
  const dayDate = running.map((s: any) => s.day_date).filter(Boolean).sort().pop()
  const Row = (s: any) => {
    const isLive = s.mode === 'live'
    const starts = s.first_day && s.first_day > todayIst()
    return (
      <a key={s.id} href={`#/paper/${encodeURIComponent(s.id)}`} className={`sess ${isLive ? 'liverow' : ''}`}>
        <span className="nm"><span className="dot8" style={{ background: s.error ? 'var(--dn)' : s.status === 'stopped' ? 'var(--mu)' : 'var(--up)' }} />{s.name}</span>
        <b className={`n ${cls(s.day_pnl)}`}>{s.error ? '!' : inr(s.day_pnl ?? 0, true)}</b>
        <span className="small muted n">
          {isLive ? <span className={`chip-s livetag ${s.armed ? 'armed' : ''}`}>{s.armed ? 'LIVE · ARMED' : 'LIVE'}</span> : <span className="chip-s" style={{ color: 'var(--paper)' }}>PAPER</span>}
          {' '}{starts ? `starts ${fmtDate(s.first_day)}` : `${s.open} open · ${s.gate?.cycles ?? 0}/20`}
        </span>
        <span className={`small n ${cls(s.cum_net)}`}>{inr(s.cum_net ?? 0, true)}</span>
      </a>)
  }
  return (
    <main className="fade">
      <div className="row"><h1>Trade</h1><span className="small muted">{rec.connected ? `● live ${tm(rec.last_tick?.replace('T', ' '))}` : '○ ' + (rec.message || '…')}</span></div>
      <IndexStrip />
      {running.some((s: any) => s.mode === 'live') && <KillSwitch />}
      {live?.oms?.limits && live?.oms?.mtm_today != null && <div className="small n">Live daily loss used {Math.round(Math.min(100, Math.max(0, -live.oms.mtm_today / live.oms.limits.daily_loss * 100)))}% of {inr(live.oms.limits.daily_loss)}</div>}
      <section className="tot">
        <div className="stat"><div className="k">Paper {dayDate && dayDate !== todayIst() ? `· ${fmtDate(dayDate)}` : 'today'}</div>
          <div className={`v n ${cls(sum(paper, 'day_pnl'))}`}>{inr(sum(paper, 'day_pnl'), true)}</div>
          <div className="small muted n">total {inr(sum(paper, 'cum_net'), true)}</div></div>
        <div className="stat"><div className="k">Live today</div>
          <div className={`v n ${cls(live?.oms?.mtm_today)}`}>{live?.oms?.mtm_today != null ? inr(live.oms.mtm_today, true) : '—'}</div>
          <div className="small muted n">{live?.enabled ? `realised ${inr(live?.oms?.realised_today ?? 0, true)}` : 'not enabled'}</div></div>
      </section>
      <Fold id="trade-sessions" title="Sessions" right={<span className="muted">{running.length}</span>}>
        {running.length ? <div>{running.map(Row)}</div> : <div className="small muted">No running sessions.</div>}
      </Fold>
      <Fold id="trade-cal" title="Paper P&L by close day" open={false}>
        <Calendar days={cal} />
      </Fold>
      <Fold id="trade-start" title="Start a session" open={!running.length}>
        <p className="small muted">Paper runs the saved strategy on live prices with the backtest engine. A live session places real orders only after you arm it. Entries only follow signals after the start.</p>
        <select className="input" value={pick} onChange={(e) => setPick(e.target.value)} aria-label="Strategy">
          <option value="">Choose a saved strategy…</option>
          {mine.map((s) => <option key={s.id} value={s.id}>{s.name} · stop {s.risk.stop_loss.value}{s.risk.stop_loss.type === 'multiple' ? '×' : ''} · max loss/day {inr(s.risk.max_loss_per_day)}</option>)}
        </select>
        {!mine.length && <div className="small muted">Save a strategy first (Strategies → Customise → Review → Save).</div>}
        <button className="btn paper" disabled={!pick} onClick={() => start('paper')}>Start paper session</button>
        <button className="btn" style={{ borderColor: 'var(--dn)', color: 'var(--dn)' }} disabled={!pick} onClick={() => start('live')}>Create live session (unarmed)</button>
        {err && <div className="small dn" role="alert">{err.replace(/^\d+ /, '')}</div>}
      </Fold>
      {stopped.length > 0 && <Fold id="trade-stopped" title="Stopped" open={false} right={<span className="muted">{stopped.length}</span>}><div>{stopped.map(Row)}</div></Fold>}
    </main>)
}
