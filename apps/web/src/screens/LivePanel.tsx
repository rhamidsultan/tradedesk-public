import { useEffect, useState } from 'react'
import { api } from '../api'
import { cls, inr } from '../fmt'
import Slide from '../components/Slide'

const side = (s: string) => (s === 'SELL' ? 'Sell' : 'Buy')

/** Live orders for one session: Kite MTM, daily-loss meter, legs with exit and stop changes, arming sheet. */
export default function LivePanel({ s, reload }: { s: any; reload: () => void }) {
  const [live, setLive] = useState<any>(null)
  const [arm, setArm] = useState(false), [typed, setTyped] = useState(''), [ovr, setOvr] = useState(false), [why, setWhy] = useState(''), [err, setErr] = useState('')
  const [exitLeg, setExitLeg] = useState<any>(null), [stopLeg, setStopLeg] = useState<any>(null), [newStop, setNewStop] = useState(''), [msg, setMsg] = useState('')
  const load = () => api.live().then(setLive).catch(() => setLive(null))
  useEffect(() => { load(); const t = setInterval(load, 2000); return () => clearInterval(t) }, [])
  const legs = (live?.oms?.legs || []).filter((l: any) => l.session === s.id)
  const open = legs.filter((l: any) => ['open', 'entering', 'exiting'].includes(l.status))
  const pnl = (l: any) => (l.ltp && l.entry_avg ? (l.side === 'BUY' ? 1 : -1) * (l.ltp - l.entry_avg) * (l.open_qty || l.filled) : null)
  const mtm = live?.oms?.mtm_today, cap = live?.oms?.limits?.daily_loss || s.spec?.risk?.max_loss_per_day
  const used = mtm != null && cap ? Math.min(100, Math.max(0, (-mtm / cap) * 100)) : 0
  const r = s.spec?.risk || {}
  const doArm = () => { setErr(''); api.arm(s.id, typed, ovr, why).then(() => { setArm(false); setTyped(''); reload() }).catch((e) => setErr(String(e.message).replace(/^\d+ /, ''))) }
  return (
    <section className="card liverow">
      <div className="row"><h2>Live · Zerodha</h2><span className={`tag livetag ${s.armed ? 'armed' : ''}`}>{s.armed ? 'ARMED' : 'not armed'}</span></div>
      {live && !live.runner_ok && open.length > 0 && <div className="alert">Order runner not responding{live.runner_age_s != null ? ` (last seen ${Math.round(live.runner_age_s / 60)} min ago)` : ''}. Your SL orders at Kite still protect open legs; to close now use Kite or call Zerodha Call & Trade.</div>}
      {live?.pending_commands?.length > 0 && <div className="small" style={{ color: 'var(--live)' }}>Waiting for the runner: {live.pending_commands.join(', ')}</div>}
      <div className="k small muted">Live MTM today (Kite prices, after charges)</div>
      <div className={`big n ${cls(mtm)}`}>{mtm != null ? inr(mtm, true) : '—'}</div>
      {cap ? <><div className="meter" aria-label={`Daily loss used ${Math.round(used)}%`}><i style={{ width: `${used}%`, background: used > 75 ? 'var(--dn)' : 'var(--live)' }} /></div>
        <div className="small muted n">Daily loss used {Math.round(used)}% of {inr(cap)} · locked: {live?.oms?.locked || 'no'}</div></> : null}
      {s.gate_override && <div className="small" style={{ color: 'var(--live)' }}>Armed with paper-gate override: {s.gate_override.replace('owner override: ', '')}</div>}
      <div className="list">{legs.map((l: any) => (
        <div className="li n" key={l.id} style={{ flexDirection: 'column', alignItems: 'stretch', gap: 4 }}>
          <div className="row"><span><b className={l.side === 'SELL' ? 'dn' : 'up'}>{side(l.side)}</b> {l.symbol} × {l.filled || l.qty}</span><b className={cls(pnl(l))}>{pnl(l) != null ? inr(pnl(l), true) : ''}</b></div>
          <span className="small muted">{l.status}{l.entry_avg ? ` · in ₹${l.entry_avg}` : ''}{l.ltp ? ` · now ₹${l.ltp}` : ''}{l.exit_avg ? ` · out ₹${l.exit_avg}` : ''}
            {' · '}{l.sl_trigger != null ? `stop ₹${l.sl_trigger} (option price, at Kite)` : 'hedge (no stop)'}{l.reason ? ` · ${l.reason.replace(/_/g, ' ')}` : ''}</span>
          {['open', 'entering'].includes(l.status) && <div className="row" style={{ gap: 8 }}>
            {l.sl_trigger != null && l.status === 'open' && <button className="btn" style={{ flex: 1 }} onClick={() => { setStopLeg(l); setNewStop(String(l.sl_trigger)) }}>Change stop</button>}
            <button className="btn" style={{ flex: 1, color: 'var(--dn)', borderColor: 'var(--dn)' }} onClick={() => setExitLeg(l)}>Exit…</button></div>}
        </div>))}</div>
      {!legs.length && <div className="small muted">No live orders yet.</div>}
      {msg && <div className="small" role="status">{msg}</div>}
      {s.armed ? <button className="btn" onClick={() => api.disarm(s.id).then(reload)}>Disarm (no new entries; open legs stay protected)</button>
        : live?.enabled ? <button className="btn" style={{ borderColor: 'var(--live)', color: 'var(--live)', fontWeight: 700 }} onClick={() => setArm(true)}>Review and arm live…</button>
        : <div className="small muted">Live is not enabled on the server yet. It is switched on by the 1-lot order test.</div>}

      {arm && <><div className="scrim" onClick={() => setArm(false)} /><div className="sheet" role="dialog" aria-label="Arm live">
        <h2>Arm live: {s.name}</h2>
        <div className="list small n">
          <div className="li"><span>Account</span><span>Zerodha (portfolio app's login)</span></div>
          <div className="li"><span>Lots per leg</span><span>{(s.spec?.legs || []).map((l: any) => l.lots).join(' / ') || `max ${r.max_lots}`} · account max {live?.oms?.limits?.max_lots}</span></div>
          <div className="li"><span>Stop loss</span><span>{r.stop_loss?.value}{r.stop_loss?.type === 'multiple' ? '× premium' : r.stop_loss?.type === 'percent' ? '% of premium' : ' points'} at Kite{r.trailing ? `, trailing ${r.trailing.value}` : ''}</span></div>
          <div className="li"><span>Max loss per day</span><span>{inr(r.max_loss_per_day)} (account {inr(live?.oms?.limits?.daily_loss)})</span></div>
          <div className="li"><span>Margin now</span><span>{s.margin_now ? inr(s.margin_now.final) : 'shown after the first entry'}</span></div>
          <div className="li"><span>Paper gate</span><span>{s.gate?.cycles ?? 0}/20 cycles · {s.gate?.weeks ?? 0}/8 weeks</span></div>
        </div>
        <label className="toggle"><span className="small">The paper gate is not passed — I override it</span><input type="checkbox" checked={ovr} onChange={(e) => setOvr(e.target.checked)} /></label>
        {ovr && <input className="input" placeholder="Why (recorded with the session)" value={why} onChange={(e) => setWhy(e.target.value)} />}
        <div className="field"><label htmlFor="typed">Type LIVE</label><input id="typed" className="input" value={typed} onChange={(e) => setTyped(e.target.value)} autoCapitalize="characters" /></div>
        {typed.trim().toUpperCase() === 'LIVE' && <Slide label="Slide to arm live" color="var(--live)" onDone={doArm} />}
        {err && <div className="small dn" role="alert">{err}</div>}
        <button className="btn" onClick={() => setArm(false)}>Cancel</button>
      </div></>}

      {exitLeg && <><div className="scrim" onClick={() => setExitLeg(null)} /><div className="sheet" role="dialog" aria-label="Exit leg">
        <h2>Exit {exitLeg.symbol}?</h2>
        <div className="small n">{side(exitLeg.side === 'SELL' ? 'BUY' : 'SELL')} {exitLeg.open_qty || exitLeg.filled} at the touch (limit, chased). Estimated P&L on this leg: <b className={cls(pnl(exitLeg))}>{pnl(exitLeg) != null ? inr(pnl(exitLeg), true) : '—'}</b>.
          {exitLeg.sl_trigger == null ? ' This is a hedge: its shorts are bought back first.' : ' Its SL order at Kite becomes the exit order.'}</div>
        <Slide label="Slide to exit" onDone={() => api.exitLeg(exitLeg.id).then(() => { setMsg('Exit sent.'); setExitLeg(null) }).catch((e) => setMsg('Not sent: ' + e.message))} />
        <button className="btn" onClick={() => setExitLeg(null)}>Cancel</button>
      </div></>}

      {stopLeg && <><div className="scrim" onClick={() => setStopLeg(null)} /><div className="sheet" role="dialog" aria-label="Change stop">
        <h2>Change stop · {stopLeg.symbol}</h2>
        <div className="small muted">Now ₹{stopLeg.sl_trigger}{stopLeg.ltp ? ` · option at ₹${stopLeg.ltp}` : ''}. Stops can only be tightened here ({stopLeg.side === 'SELL' ? 'lower' : 'higher'}), never widened.</div>
        <input className="input n" inputMode="decimal" value={newStop} onChange={(e) => setNewStop(e.target.value)} aria-label="New stop trigger" />
        <button className="btn primary" disabled={!(+newStop > 0) || (stopLeg.side === 'SELL' ? +newStop >= stopLeg.sl_trigger : +newStop <= stopLeg.sl_trigger)}
          onClick={() => fetch(`/api/live/stop/${stopLeg.id}`, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ trigger: +newStop, command_id: `stop${stopLeg.id}-${Date.now()}` }) })
            .then((x) => { if (!x.ok) throw new Error(String(x.status)); setMsg(`Stop change to ₹${newStop} sent.`); setStopLeg(null) }).catch((e) => setMsg('Not sent: ' + e.message))}>Move stop to ₹{newStop}</button>
        <button className="btn" onClick={() => setStopLeg(null)}>Cancel</button>
      </div></>}
    </section>)
}
