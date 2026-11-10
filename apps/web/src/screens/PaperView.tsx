import { useState } from 'react'
import { api } from '../api'
import { cls, dtm, fmtDate, inr, tm } from '../fmt'
import { usePoll } from '../hooks/usePoll'
import Fold from '../components/Fold'
import Calendar from '../components/Calendar'
import Payoff from '../components/Payoff'
import LivePanel from './LivePanel'

const G = ({ k, v, of }: { k: string; v: number; of: number }) => (
  <div className="stat"><div className="k">{k}</div><div className="v n">{v}<span className="muted small"> / {of}</span></div>
    <div className="bar" style={{ marginTop: 6 }}><i style={{ width: `${Math.min(100, (v / of) * 100)}%`, background: 'var(--paper)' }} /></div></div>)
const todayIst = () => new Date(Date.now() + 5.5 * 3600e3).toISOString().slice(0, 10)

export default function PaperView({ id }: { id: string }) {
  const [s, setS] = useState<any>(null)
  const [confirm, setConfirm] = useState(false)
  const load = () => api.paperGet(id).then(setS).catch(() => setS((x: any) => x || { error: 'Not found' }))
  usePoll(load, !!s?.positions?.length, [id])
  if (!s) return <main className="fade"><div className="card small muted">Loading…</div></main>
  const g = s.gate || {}
  const stopped = s.session_status === 'stopped'
  const starts = s.first_day && s.first_day > todayIst()
  const closed = (s.trades || []).filter((t: any) => t.exit && !t.open)
  const days: Record<string, { net: number; cycles: number }> = {}
  closed.forEach((t: any) => { const d = t.exit.slice(0, 10); days[d] = days[d] || { net: 0, cycles: 0 }; days[d].net += t.net; days[d].cycles++ })
  const cyc = (t: any) => `#/cycle/${encodeURIComponent(id)}/${t.id}`
  return (
    <main className="fade">
      <div className="row"><a className="back" href="#/trade" aria-label="Back">‹</a><h1 style={{ flex: 1 }}>{s.name}</h1>
        {s.mode === 'live' ? <span className={`tag livetag ${s.armed ? 'armed' : ''}`}>LIVE</span>
          : <span className="tag" style={{ color: 'var(--paper)', borderColor: 'var(--paper)' }}>PAPER</span>}</div>
      {s.mode === 'live' && <LivePanel s={s} reload={load} />}
      {s.error && <div className="alert">{s.error}</div>}
      {g.kill?.length > 0 && <div className="alert"><b>Kill rule hit:</b> {g.kill.join('; ')}. Stop and return to research (paper-trading gate).</div>}
      <section className="card" style={s.mode === 'live' ? { opacity: .85 } : undefined}>
        {s.mode === 'live' && <div className="small muted">Engine's simulated view (paper fills) — real orders are in Live above</div>}
        <div className="row"><span className="small muted">{stopped ? 'Stopped' : starts ? `Starts ${fmtDate(s.first_day)} 09:15`
          : s.day_date && s.day_date !== todayIst() ? `Last day · ${fmtDate(s.day_date)}` : `Today${s.as_of && s.as_of !== 'close' ? ' · ' + s.as_of : ''}`}</span>
          <span className="small muted">since {s.created ? fmtDate(s.created) : '—'}</span></div>
        <div className={`${s.mode === 'live' ? 'v' : 'big'} n ${cls(s.day_pnl)}`} style={s.mode === 'live' ? { fontSize: 20, fontWeight: 700 } : undefined}>{inr(s.day_pnl ?? 0, true)}</div>
        {s.mode !== 'live' && s.spec?.risk?.max_loss_per_day && <><div className="meter"><i style={{ width: `${Math.min(100, Math.max(0, -(s.day_pnl || 0) / s.spec.risk.max_loss_per_day * 100))}%`, background: 'var(--live)' }} /></div>
          <div className="small muted n">Daily loss used {Math.round(Math.min(100, Math.max(0, -(s.day_pnl || 0) / s.spec.risk.max_loss_per_day * 100)))}% of {inr(s.spec.risk.max_loss_per_day)}</div></>}
        <div className="small muted n">Total net after charges <span className={cls(s.cum_net)}>{inr(s.cum_net ?? 0, true)}</span> · {s.daily?.length || 0} days
          {s.margin_now ? <> · margin now {inr(s.margin_now.final)} <span className="muted">(Zerodha, {s.margin_now.at})</span></> : null}</div>
      </section>
      <Fold id={s.mode === 'live' ? 'pv-pos-live' : 'pv-pos'} open={s.mode !== 'live'} title={s.mode === 'live' ? 'Engine positions (simulated)' : 'Open positions'} right={<span className="muted">{s.positions?.length || 0}</span>}>
        {s.positions?.length ? <div className="list">{s.positions.map((p: any) => (
          <a className="li n" key={p.symbol} href={`#/chart/${encodeURIComponent(p.symbol)}`} style={{ color: 'var(--tx)', textDecoration: 'none' }}>
            <span><b className={p.side === 'SELL' ? 'dn' : 'up'}>{p.side === 'SELL' ? 'Sell' : 'Buy'}</b> {p.symbol} × {p.qty} ({p.lots} lot{p.lots > 1 ? 's' : ''})<br />
              <span className="muted">in ₹{p.entry} · now ₹{p.ltp} · {p.sl != null ? `stop ₹${p.sl} (option)` : p.spot_sl != null ? `stop NIFTY ${Math.round(p.spot_sl).toLocaleString('en-IN')}` : 'hedge, no stop'}</span></span><b className={cls(p.pnl)}>{inr(p.pnl, true)}</b></a>))}</div>
          : <div className="small muted">None</div>}
      </Fold>
      {s.positions?.length > 0 && <Fold id="pv-payoff" title="Payoff"><Payoff positions={s.positions} spot={s.spot} /></Fold>}
      <Fold id="pv-fills" title={s.day_date && s.day_date !== todayIst() ? `Fills · ${fmtDate(s.day_date)}` : "Today's fills"} right={<span className="muted">{s.fills_today?.length || 0}</span>}>
        {s.fills_today?.length ? <div className="list">{s.fills_today.map((f: any, i: number) => (
          <div className="li n" key={i}><span>{tm(f.ts)} {f.side} {f.symbol} × {f.qty}<br /><span className="muted">{f.reason.replace(/_/g, ' ')}{f.stale ? ' · stale price' : ''}</span></span><span>₹{f.price}</span></div>))}</div>
          : <div className="small muted">None</div>}
      </Fold>
      <Fold id="pv-cycles" title="Closed cycles" right={<span className="muted">{closed.length}</span>}>
        <Calendar days={days} />
        {closed.length ? <div className="list">{closed.slice().reverse().slice(0, 30).map((t: any) => (
          <a className="li n" key={t.id} href={cyc(t)} style={{ color: 'var(--tx)', textDecoration: 'none' }}>
            <span>{dtm(t.exit)}<br /><span className="muted">{t.legs.length} leg{t.legs.length > 1 ? 's' : ''} · {t.reason.replace(/_/g, ' ')}</span></span>
            <b className={cls(t.net)}>{inr(t.net, true)} ›</b></a>))}</div> : null}
      </Fold>
      <Fold id="pv-gate" title="Paper-trading gate" open={false}>
        <div className="grid3"><G k="Cycles" v={g.cycles || 0} of={20} /><G k="Weeks" v={g.weeks || 0} of={8} /><G k="Stop events" v={g.stop_events || 0} of={15} /></div>
        <div className="small muted n">Stop slippage per unit: median {g.median_slip ?? '—'} (pass ≤ ₹2) · 90th pct {g.p90_slip ?? '—'} (pass ≤ ₹6)</div>
        {s.stop_events?.length > 0 && <div className="list">{s.stop_events.slice().reverse().map((e: any, i: number) => (
          <div className="li n" key={i}><span>{dtm(e.ts)} {e.symbol}<br /><span className="muted">trigger {e.trigger} · sim {e.sim_fill} · real best {e.real_best ?? '—'}</span></span><span>{e.slip_per_unit ?? '—'}</span></div>))}</div>}
      </Fold>
      <Fold id="pv-days" title="Days" open={false}>
        {s.daily?.length ? <div className="list">{s.daily.slice().reverse().slice(0, 30).map(([d, v]: [string, number]) => (
          <div className="li n" key={d}><span>{fmtDate(d)}</span><span className={cls(v)}>{inr(v, true)}</span></div>))}</div> : <div className="small muted">No completed days yet.</div>}
      </Fold>
      {!stopped && (confirm
        ? <div className="warn"><p>Stop this session? {s.mode === 'live' ? 'Its live positions are exited at the touch.' : 'Open paper positions are left as they are.'} It cannot be resumed (start a new one).</p>
            <div className="row" style={{ gap: 8 }}><button className="btn" style={{ flex: 1 }} onClick={() => setConfirm(false)}>Keep running</button>
              <button className="btn primary" style={{ flex: 1 }} onClick={() => api.paperStop(id).then(load)}>Stop session</button></div></div>
        : <button className="btn" style={s.mode === 'live' ? { color: 'var(--dn)', borderColor: 'var(--dn)' } : undefined} onClick={() => setConfirm(true)}>{s.mode === 'live' ? 'Stop live session and exit its positions…' : 'Stop session…'}</button>)}
      <div className="small muted">Spec {s.spec_hash} · rules frozen for this session</div>
    </main>)
}
