import { useEffect, useRef, useState } from 'react'
import { api, type Validation } from '../api'
import { inr } from '../fmt'
import { IBack } from '../icons'
import { go, useDraft } from '../store'

const STEPS = ['Instrument', 'Entry', 'Legs', 'Risk', 'Schedule', 'Review']
const OPS: Record<string, string> = { crosses_above: 'crosses above', crosses_below: 'crosses below', '>': 'is above', '<': 'is below', '>=': 'is at or above', '<=': 'is at or below', '==': 'equals' }
const nice = (x: any) => typeof x === 'number' ? String(x) : String(x).replace(/opening_range_(high|low)\((\d+)\)/, (_, hl, m) => `${m}-min opening range ${hl}`)
  .replace(/^close$/, 'close').replace(/_/g, ' ')

function Stepper({ label, hint, value, step, min, max, unit, onChange }: { label: string; hint?: string; value: number; step: number; min: number; max?: number; unit?: string; onChange: (v: number) => void }) {
  const clamp = (v: number) => Math.max(min, max !== undefined ? Math.min(max, v) : v)
  return (
    <div className="field"><label>{label}</label>
      <div className="stepper">
        <button aria-label={`Decrease ${label}`} onClick={() => onChange(clamp(+(value - step).toFixed(2)))}>−</button>
        <div className="val n"><b>{unit === '₹' ? inr(value) : value}</b>{unit !== '₹' && <> <span className="muted">{unit}</span></>}{hint && <div className="small muted">{hint}</div>}</div>
        <button aria-label={`Increase ${label}`} onClick={() => onChange(clamp(+(value + step).toFixed(2)))}>+</button>
      </div></div>)
}
function Chips<T extends string>({ label, value, options, onChange }: { label: string; value: T; options: [T, string][]; onChange: (v: T) => void }) {
  return (<div className="field"><label>{label}</label>
    <div className="chips" role="radiogroup" aria-label={label}>{options.map(([v, l]) =>
      <button key={v} role="radio" aria-checked={value === v} className={`chip ${value === v ? 'on' : ''}`} onClick={() => onChange(v)}>{l}</button>)}</div></div>)
}
function Toggle({ label, hint, checked, onChange }: { label: string; hint?: string; checked: boolean; onChange: (v: boolean) => void }) {
  return (<label className="toggle card" style={{ flexDirection: 'row' }}><span><span>{label}</span>{hint && <div className="small muted">{hint}</div>}</span>
    <input type="checkbox" checked={checked} onChange={(e) => onChange(e.target.checked)} /></label>)
}
function Time({ label, value, onChange }: { label: string; value: string; onChange: (v: string) => void }) {
  return (<div className="field"><label>{label}</label><input className="input n" type="time" min="09:15" max="15:29" value={value} onChange={(e) => onChange(e.target.value)} /></div>)
}

export default function Builder() {
  const [spec, setSpec] = useDraft()
  const [step, setStep] = useState(0)
  const [v, setV] = useState<Validation | null>(null)
  const [saved, setSaved] = useState<string | null>(null)
  const timer = useRef<number>()
  useEffect(() => {
    if (!spec) return
    window.clearTimeout(timer.current)
    timer.current = window.setTimeout(() => api.validate(spec).then(setV).catch(() => setV(null)), 250)
  }, [spec])
  if (!spec) return <main><div className="card">No strategy open. <a href="#/strategies">Pick a template</a>.</div></main>
  const upd = (f: (s: any) => void) => { const s = structuredClone(spec); f(s); setSaved(null); setSpec(s) }
  const r = spec.risk, sch = spec.schedule
  const sell = spec.legs.some((l: any) => l.side === 'SELL')
  const risk = v?.risk
  const blocked = !v?.ok

  return (<>
    <header style={{ padding: '6px 8px 10px', borderBottom: '1px solid var(--bd)' }}>
      <div className="row" style={{ justifyContent: 'flex-start', gap: 4 }}>
        <button className="back" aria-label="Back to strategies" onClick={() => go('/strategies')}><IBack /></button>
        <div><b>{spec.name}</b><div className="small muted">{spec.instrument.underlying} · options · {spec.instrument.timeframe} · draft</div></div>
      </div>
      <ol className="steps" style={{ listStyle: 'none', margin: 0, padding: '0 8px' }} aria-label="Builder steps">
        {STEPS.map((s, i) => <li key={s}><button className={i < step ? 'done' : i === step ? 'cur' : ''} aria-current={i === step ? 'step' : undefined} onClick={() => setStep(i)}><i />{s}</button></li>)}
      </ol>
    </header>
    <main className="fade" key={step}>
      {step === 0 && <>
        <Chips label="Underlying" value={spec.instrument.underlying} options={[['NIFTY', 'NIFTY'], ['BANKNIFTY', 'BANKNIFTY'], ['SENSEX', 'SENSEX']]} onChange={(u) => upd((s) => { s.instrument.underlying = u })} />
        {spec.instrument.underlying !== 'NIFTY' && <div className="warn">Lot sizes for {spec.instrument.underlying} before Jan 2026 are unverified assumptions; treat older results with care.</div>}
        <Chips label="Signal timeframe" value={spec.instrument.timeframe} options={[['1m', '1 min'], ['3m', '3 min'], ['5m', '5 min'], ['15m', '15 min']]} onChange={(t) => upd((s) => { s.instrument.timeframe = t })} />
        <div className="field"><label htmlFor="nm">Name</label><input id="nm" className="input" value={spec.name} onChange={(e) => upd((s) => { s.name = e.target.value })} /></div>
      </>}
      {step === 1 && spec.entry.any_of.map((rule: any, ri: number) => (
        <div className="card" key={ri}>
          <div className="row"><b>When</b><span className="tag">opens: {rule.action.replace(/_/g, ' ')}</span></div>
          {rule.at_time && <Time label="At time (when flat)" value={rule.at_time} onChange={(t) => upd((s) => { s.entry.any_of[ri].at_time = t })} />}
          {rule.all_of.map((c: any, ci: number) => {
            const m = typeof c.right === 'string' && c.right.match(/^opening_range_(high|low)\((\d+)\)$/)
            return (<div key={ci} className="field">
              <div className="small">{spec.instrument.underlying} {spec.instrument.timeframe} <b>{nice(c.left)}</b> {OPS[c.op]} <b>{nice(c.right)}</b></div>
              {m && <Chips label="Opening range" value={m[2]} options={[['15', '15 min'], ['30', '30 min'], ['45', '45 min']]}
                onChange={(mm) => upd((s) => { s.entry.any_of.forEach((rr: any) => rr.all_of.forEach((cc: any) => { if (typeof cc.right === 'string') cc.right = cc.right.replace(/\(\d+\)/, `(${mm})`) })) })} />}
            </div>)
          })}
        </div>))}
      {step === 2 && spec.legs.map((leg: any, li: number) => (
        <div className="card" key={li}>
          <div className="row"><span><b className={leg.side === 'SELL' ? 'dn' : 'up'}>{leg.side}</b> <b>{leg.type}</b> <span className="muted small">for “{leg.when.replace(/_/g, ' ')}”</span></span></div>
          <Chips label="Expiry" value={leg.expiry} options={[['nearest_weekly', 'This week'], ['next_weekly', 'Next week'], ['nearest_monthly', 'Monthly']]} onChange={(x) => upd((s) => { s.legs[li].expiry = x })} />
          {leg.strike.by === 'premium'
            ? <Stepper label="Strike: premium closest to" value={leg.strike.value} step={5} min={5} unit="₹" hint="out of the money only" onChange={(x) => upd((s) => { s.legs[li].strike.value = x })} />
            : <Stepper label="Strike: steps from ATM" value={leg.strike.value} step={1} min={-10} max={10} hint="0 = at the money; + higher strike" onChange={(x) => upd((s) => { s.legs[li].strike.value = x })} />}
          {leg.lots === 'from_risk'
            ? <div className="small muted">Lots: from risk per trade (set in Risk)</div>
            : <Stepper label="Lots" value={leg.lots} step={1} min={1} max={10} hint={risk?.lot_size ? `qty ${leg.lots * risk.lot_size} (lot ${risk.lot_size})` : ''} onChange={(x) => upd((s) => { s.legs[li].lots = x })} />}
        </div>))}
      {step === 3 && <>
        <div className="row"><h2>Stop loss</h2><span className="small dn" style={{ fontWeight: 700 }}>REQUIRED</span></div>
        {r.stop_loss.on === 'premium'
          ? <Stepper label="Stop: option price × entry" value={r.stop_loss.value} step={0.25} min={1.25} max={5} unit="×" hint="SL-buy at Kite; limit 10% above" onChange={(x) => upd((s) => { s.risk.stop_loss.value = x })} />
          : <Stepper label="Stop: points on spot from entry" value={r.stop_loss.value} step={5} min={5} max={300} unit="pts" onChange={(x) => upd((s) => { s.risk.stop_loss.value = x })} />}
        {!sell && <Chips label="Target" value={r.target ? String(r.target.value) : 'none'} options={[['1.5', '1:1.5'], ['2', '1:2'], ['3', '1:3'], ['none', 'None']]}
          onChange={(x) => upd((s) => { s.risk.target = x === 'none' ? null : { on: s.risk.stop_loss.on, type: 'rr', value: +x } })} />}
        {r.risk_per_trade != null && <Stepper label="Risk per trade" value={r.risk_per_trade} step={500} min={500} unit="₹" hint={`${(r.risk_per_trade / (spec.costs?.capital || 200000) * 100).toFixed(1)}% of capital`} onChange={(x) => upd((s) => { s.risk.risk_per_trade = x })} />}
        <Stepper label="Max loss per day (stops for the day)" value={r.max_loss_per_day} step={500} min={500} unit="₹" hint={`${(r.max_loss_per_day / (spec.costs?.capital || 200000) * 100).toFixed(1)}% of capital`} onChange={(x) => upd((s) => { s.risk.max_loss_per_day = x })} />
        <Stepper label="Trades per day" value={r.max_trades_per_day} step={1} min={1} max={10} onChange={(x) => upd((s) => { s.risk.max_trades_per_day = x })} />
        {r.stop_loss.on === 'premium' && <Toggle label="Trailing stop" hint="The stop follows the best option price; the SL order at Kite moves with it" checked={!!r.trailing}
          onChange={(x) => upd((s) => { s.risk.trailing = x ? { type: 'points', value: 10, activate_after: 5, step: 1 } : null })} />}
        {r.trailing && <>
          <Stepper label="Trail distance" value={r.trailing.value} step={1} min={1} max={100} unit="pts" hint="behind the best premium" onChange={(x) => upd((s) => { s.risk.trailing.value = x })} />
          <Stepper label="Start trailing after" value={r.trailing.activate_after} step={1} min={0} max={100} unit="pts" hint="profit on the option" onChange={(x) => upd((s) => { s.risk.trailing.activate_after = x })} />
        </>}
        <Toggle label="Whole-position stop" hint="Close every leg of the trade at this net loss" checked={r.position_stop != null}
          onChange={(x) => upd((s) => { s.risk.position_stop = x ? Math.round((s.risk.max_loss_per_day || 4000) / 2) : null })} />
        {r.position_stop != null && <Stepper label="Position stop" value={r.position_stop} step={250} min={250} unit="₹" onChange={(x) => upd((s) => { s.risk.position_stop = x })} />}
        <Stepper label="Re-entries after a stop" value={r.reentry_after_sl} step={1} min={0} max={5} onChange={(x) => upd((s) => { s.risk.reentry_after_sl = x })} />
        {r.reentry_after_sl > 0 && <Stepper label="Re-enter the same option only beyond its stop-out price by" value={r.reentry_gap || 0} step={1} min={0} max={50} unit="pts" onChange={(x) => upd((s) => { s.risk.reentry_gap = x })} />}
        {sell && sch.hold_overnight && <Toggle label="Close a lone leg at 15:25" hint="After one leg stops, don't carry the other overnight" checked={!!r.close_lone_leg_eod} onChange={(x) => upd((s) => { s.risk.close_lone_leg_eod = x })} />}
      </>}
      {step === 4 && <>
        <div className="grid3"><Time label="Start" value={sch.start} onChange={(t) => upd((s) => { s.schedule.start = t })} />
          <Time label="No new after" value={sch.no_new_after} onChange={(t) => upd((s) => { s.schedule.no_new_after = t })} />
          <Time label="Exit by" value={sch.exit_by} onChange={(t) => upd((s) => { s.schedule.exit_by = t })} /></div>
        <Toggle label="Hold overnight (NRML)" hint="Positions carried to later days, closed on expiry day" checked={sch.hold_overnight} onChange={(x) => upd((s) => { s.schedule.hold_overnight = x })} />
        {sch.hold_overnight && <Time label="Expiry-day exit" value={sch.expiry_exit_time} onChange={(t) => upd((s) => { s.schedule.expiry_exit_time = t })} />}
        <Toggle label="Skip expiry days" checked={sch.skip_expiry_day} onChange={(x) => upd((s) => { s.schedule.skip_expiry_day = x })} />
      </>}
      {step === 5 && <>
        {v && !v.ok && <div className="alert" role="alert"><b className="dn">Fix before saving or backtesting</b>
          <ul style={{ margin: '6px 0 0', paddingLeft: 18 }}>{v.errors.map((e, i) => <li key={i}>{e.field}: {e.message}</li>)}</ul></div>}
        {risk && <div className="card small n">
          <h2>Risk summary</h2>
          {(risk.legs || []).map((l: any, i: number) => <div className="row" key={i}><span>{l.leg}</span><span className="muted">{l.lots ?? 'from risk'} lot · qty {l.qty ?? '—'} · at stop {l.est_loss_at_stop != null ? inr(-l.est_loss_at_stop) : '—'}</span></div>)}
          <div className="row"><span>Max loss per day</span><b className="dn">{inr(-r.max_loss_per_day)}</b></div>
          {(risk.notes || []).map((n: string, i: number) => <div key={i} className="warn">⚠ {n}</div>)}
        </div>}
        {saved && <div className="card small up">Saved as “{saved}”.</div>}
      </>}
    </main>
    <footer className="footer">
      <div className="grid3 small muted n">
        <div>{sell && (risk?.legs?.length ?? 0) > 1 ? 'One leg stops' : 'Loss at stop'}<div className="dn" style={{ fontSize: 15, fontWeight: 700 }}>{risk?.est_loss_per_trade != null ? `≈${inr(-risk.est_loss_per_trade)}` : '—'}</div>
          {sell && risk?.worst_case_all_stops != null && (risk?.legs?.length ?? 0) > 1 && <div>both: <span className="dn">{inr(-risk.worst_case_all_stops)}</span></div>}</div>
        <div>Max loss / day<div className="dn" style={{ fontSize: 15, fontWeight: 700 }}>{inr(-r.max_loss_per_day)}</div></div>
        <div>Lot size<div style={{ fontSize: 15, fontWeight: 700, color: 'var(--tx)' }}>{risk?.lot_size ?? '—'}</div></div>
      </div>
      {step < 5
        ? <button className="btn primary" onClick={() => setStep(step + 1)}>Next: {STEPS[step + 1]}</button>
        : <div className="row" style={{ gap: 10 }}>
            <button className="btn" style={{ flex: 1 }} disabled={blocked} onClick={() => api.save({ ...spec, id: spec.name.toLowerCase().replace(/[^a-z0-9]+/g, '_').replace(/^_|_$/g, '') || spec.id }).then((x) => setSaved(x.id)).catch(() => setSaved(null))}>Save</button>
            <button className="btn primary" style={{ flex: 2 }} disabled={blocked} onClick={() => go('/backtest')}>Backtest</button>
          </div>}
    </footer>
  </>)
}
