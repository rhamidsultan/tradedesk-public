import { useState } from 'react'
import type { RunResult } from '../api'
import { cls, fmtDate, inr, lakh } from '../fmt'
import Calendar from './Calendar'
import Fold from './Fold'

function Equity({ daily, capital }: { daily: [string, number][]; capital: number }) {
  if (!daily.length) return null
  let eq = 0, peak = 0
  const pts = daily.map(([d, p]) => { eq += p; peak = Math.max(peak, eq); return { d, eq, dd: eq - peak } })
  const W = 340, H = 150, top = 8, mid = 100
  const maxE = Math.max(...pts.map((p) => p.eq), 0), minE = Math.min(...pts.map((p) => p.eq), 0)
  const minD = Math.min(...pts.map((p) => p.dd), -1)
  const x = (i: number) => (i / Math.max(1, pts.length - 1)) * W
  const y = (v: number) => top + (maxE - v) / Math.max(1, maxE - minE) * (mid - top)
  const yd = (v: number) => mid + 6 + (v / minD) * (H - mid - 20)
  const line = pts.map((p, i) => `${x(i).toFixed(1)},${y(p.eq).toFixed(1)}`).join(' ')
  const dd = `M0,${mid + 6} ` + pts.map((p, i) => `L${x(i).toFixed(1)},${yd(p.dd).toFixed(1)}`).join(' ') + ` L${W},${mid + 6} Z`
  const years = [...new Set(pts.map((p) => p.d.slice(0, 4)))]
  const final = pts[pts.length - 1].eq, worstDD = Math.min(...pts.map((p) => p.dd))
  return (
    <div className="card">
      <div className="row small muted n"><span>High {inr(maxE)}</span><span>Low {inr(minE)}</span><span className="dn">Worst DD {inr(worstDD)}</span></div>
      <svg viewBox={`0 0 ${W} ${H}`} width="100%" role="img"
        aria-label={`Cumulative net P&L ending at ${inr(final)}; worst drawdown ${inr(worstDD)} on ${inr(capital)} capital`}>
        <line x1="0" x2={W} y1={y(0)} y2={y(0)} stroke="var(--bd)" />
        <polyline points={line} fill="none" stroke="var(--ac)" strokeWidth="2" />
        <path d={dd} fill="var(--dn)" opacity="0.35" />
        {years.map((yr) => { const i = pts.findIndex((p) => p.d.startsWith(yr)); return <text key={yr} x={Math.min(x(i), W - 26)} y={H - 2} fontSize="10" fill="var(--mu)">{yr}</text> })}
      </svg>
      <div className="row small muted"><span>— Cumulative net P&L</span><span style={{ color: 'var(--dn)' }}>■ Drawdown</span></div>
    </div>)
}

export default function Result({ r }: { r: RunResult }) {
  const m = r.metrics, cap = r.strategy?.costs?.capital ?? 200000
  const [showAll, setShowAll] = useState(false)
  const years = Object.entries(m.by_year || {}) as [string, number][]
  const maxY = Math.max(1, ...years.map(([, v]) => Math.abs(v)))
  const trades = showAll ? r.trades : r.trades.slice(0, 20)
  const closedNet = r.trades.filter((t) => t.exit_ts).reduce((a, t) => a + t.net, 0)
  const days = r.daily.length, short = days < 120
  return (
    <div className="fade" style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
      <section>
        <div className="small muted">Net P&L after all charges · {fmtDate(r.start)} → {fmtDate(r.end)} · capital {lakh(cap)}</div>
        <div className={`big n ${cls(m.net_pnl)}`} style={{ whiteSpace: 'nowrap' }}>{inr(m.net_pnl, true)}</div>
        {m.open_at_end > 0 && <div className="warn small n" style={{ marginTop: 8 }}>⚠ Includes {m.open_at_end} open trade valued at its last price.
          Closed trades only: <b className={cls(closedNet)}>{inr(closedNet, true)}</b></div>}
        {short && <div className="warn small" style={{ marginTop: 8 }}>⚠ Short sample ({days} trading days) — not evidence of an edge.</div>}
        <div className="small muted n">Gross {inr(m.gross_pnl)} · charges {inr(m.charges)}{m.charges_pct_of_gross ? ` (${m.charges_pct_of_gross}% of |gross|)` : ''}
          {m.open_at_end ? ` · ${m.open_at_end} open trade marked at last price` : ''}</div>
      </section>
      <Equity daily={r.daily} capital={cap} />
      <section className="grid3 n">
        {[['Win rate', m.win_rate_pct != null ? `${m.win_rate_pct}%` : 'no closed trades', ''], ['Avg win / loss', `${inr(m.avg_win)} / ${inr(m.avg_loss)}`, ''],
          ['Expectancy', inr(m.expectancy, true), cls(m.expectancy)], ['Max drawdown', inr(-m.max_drawdown), 'dn'], ['Trades', m.open_at_end ? `${m.trades} + ${m.open_at_end} open` : String(m.trades), ''],
          ['Worst day', inr(m.worst_day?.pnl), 'dn'], ['Peak margin (est.)', m.peak_margin ? `${inr(m.peak_margin)} · ${Math.round(m.peak_margin / cap * 100)}%` : '—', m.peak_margin > 0.8 * cap ? 'dn' : ''], ['Return on margin', m.return_on_peak_margin_pct != null ? `${m.return_on_peak_margin_pct}%` : '—', cls(m.return_on_peak_margin_pct)], ['Charges', inr(m.charges), '']].map(([k, v, c]) => (
          <div className="stat" key={k}><div className="k">{k}</div><div className={`v ${c}`} style={{ fontSize: k === 'Avg win / loss' ? 13 : 16 }}>{v}</div></div>))}
      </section>
      <Fold id="res-cal" title="P&L by close day">
        <Calendar days={Object.fromEntries(Object.entries(r.trades.filter((t: any) => t.exit_ts).reduce((a: any, t: any) => {
          const d = t.exit_ts.slice(0, 10); a[d] = a[d] || { net: 0, cycles: 0 }; a[d].net += t.net; a[d].cycles++; return a }, {})))} />
      </Fold>
      <Fold id="res-more" title="Ratios and robustness">
        <div className="grid3 n">
          {[['Sortino', m.sortino ?? '—'], ['Calmar', m.calmar ?? '—'], ['Months profitable', m.months_profitable_pct != null ? `${m.months_profitable_pct}%` : '—'],
            ['Steady score', m.score_steady ?? '—'], ['Charges ×1.5', inr(m.net_with_charges_x1_5, true)], ['Index move', m.index_move_pct != null ? `${m.index_move_pct}%` : '—'],
            ['Best day', m.best_day ? inr(m.best_day[1], true) : '—'], ['Big orders', `${m.fills_over_10pct_volume ?? 0} fills`], ['Margin > capital', `${m.days_over_capital_margin ?? 0} days`]].map(([k, v]) => (
            <div className="stat" key={k as string}><div className="k">{k}</div><div className="v">{v as any}</div></div>))}
        </div>
        {m.monte_carlo && <div className="small n">Monte Carlo (1000 reshuffles of the trades): typical drawdown {inr(m.monte_carlo.dd_p50)}, 1 in 20 worse than <b className="dn">{inr(m.monte_carlo.dd_p95)}</b>; chance the first {m.monte_carlo.block_trades} trades lose: {m.monte_carlo.p_losing_block}%.</div>}
        <div className="small muted">Steady score = net ÷ max drawdown × share of months profitable. "Big orders" = fills larger than 10% of that option's last 5 minutes' volume.</div>
      </Fold>
      <Fold id="res-break" title="Breakdowns" open={false}>
        <h2 className="small muted">By exit reason</h2>
        <div className="list">{Object.entries(m.by_exit_reason || {}).map(([k, v]: [string, any]) => <div className="li n" key={k}><span>{k.replace(/_/g, ' ')} <span className="muted">· {v.trades}</span></span><span className={cls(v.net)}>{inr(v.net, true)}</span></div>)}</div>
        <h2 className="small muted">By weekday</h2>
        <div className="list">{Object.entries(m.by_weekday || {}).map(([k, v]: [string, any]) => <div className="li n" key={k}><span>{k}</span><span className={cls(v)}>{inr(v, true)}</span></div>)}</div>
        <h2 className="small muted">By month</h2>
        <div className="list">{Object.entries(m.by_month || {}).slice(-24).map(([k, v]: [string, any]) => <div className="li n" key={k}><span>{k}</span><span className={cls(v)}>{inr(v, true)}</span></div>)}</div>
      </Fold>
      <section className="card">
        <h2>By year</h2>
        {years.map(([yr, v]) => (
          <div className="row small n" key={yr}><span style={{ width: 40 }} className="muted">{yr}</span>
            <div className="bar"><i style={{ width: `${Math.abs(v) / maxY * 100}%`, background: v >= 0 ? 'var(--up)' : 'var(--dn)' }} /></div>
            <span style={{ width: 96, textAlign: 'right' }} className={cls(v)}>{inr(v, true)}</span></div>))}
      </section>
      <section className="card">
        <h2>Event and gap days</h2>
        <div className="row small n"><span className="muted">Net on {m.event_days_count ?? 0} event / big-gap days</span><span className={cls(m.net_on_event_days)}>{inr(m.net_on_event_days, true)}</span></div>
        <div className="row small n"><span className="muted">Net without them</span><span className={cls(m.net_excluding_event_days)}>{inr(m.net_excluding_event_days, true)}</span></div>
        <div className="list">{(m.event_days || []).slice(0, 5).map(([d, p, l]: [string, number, string]) => (
          <div className="li n" key={d}><span>{fmtDate(d)} · <span className="muted">{l}</span></span><span className={cls(p)}>{inr(p, true)}</span></div>))}</div>
      </section>
      <section className="card small">
        <h2>Data and assumptions</h2>
        <div className="muted">Real traded option prices: {m.real_price_fills_pct}% of fills · slippage {r.strategy?.costs?.slippage_pct}% · stops fill {Math.round((r.strategy?.costs?.stop_fill_frac ?? 0.5) * 100)}% into the bar
          {r.strategy?.costs?.sl_order === 'limit' ? ' · SL-limit orders (can stay unfilled)' : ''} · lot size by expiry · charges by trade date</div>
        {m.peak_margin ? <div className="muted n">Peak margin (estimate) {inr(m.peak_margin)} · return on it {m.return_on_peak_margin_pct}%</div> : null}
      </section>
      <section style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
        <h2>Trades <span className="muted small">({r.trade_count})</span></h2>
        {trades.map((t) => (
          <div className="card small n" key={t.trade_id}>
            <div className="row"><b>{fmtDate(t.entry_ts)} {t.entry_ts.slice(11, 16)}</b><b className={cls(t.net)}>{inr(t.net, true)}</b></div>
            {t.legs.map((l: any, i: number) => (
              <div className="row" key={i}><span><span className={l.side === 'SELL' ? 'dn' : 'up'} style={{ fontWeight: 700 }}>{l.side}</span> {l.symbol}</span>
                <span className="muted">{l.lots} lot{l.lots > 1 ? 's' : ''} · qty {l.qty} @ {l.entry}</span></div>))}
            <div className="muted">Exit {t.exit_ts ? `${fmtDate(t.exit_ts)} ${t.exit_ts.slice(11, 16)}` : 'still open'} · {t.exit_reason.split('+').map((x: string) => x === 'open_marked' ? 'open leg marked at last price' : x.replace(/_/g, ' ')).join(', then ')} · charges {inr(t.charges)}</div>
          </div>))}
        {!showAll && r.trades.length > 20 && <button className="btn" onClick={() => setShowAll(true)}>Show {r.trades.length - 20} more</button>}
      </section>
    </div>)
}
