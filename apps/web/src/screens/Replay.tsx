import { useEffect, useRef, useState } from 'react'
import { createChart, ColorType, type UTCTimestamp } from 'lightweight-charts'
import { cls, inr } from '../fmt'
import { useDraft } from '../store'

const css = (v: string) => getComputedStyle(document.documentElement).getPropertyValue(v).trim()
const ep = (t: string) => (Date.parse(t.replace(' ', 'T') + ':00Z') / 1000) as UTCTimestamp

/** D11: replay one stored day minute by minute — chart, positions and P&L as the strategy saw them. */
export default function Replay() {
  const [spec] = useDraft()
  const [day, setDay] = useState('2026-10-20')
  const [data, setData] = useState<any>(null), [i, setI] = useState(0), [play, setPlay] = useState(false), [speed, setSpeed] = useState(4)
  const [busy, setBusy] = useState(false), [err, setErr] = useState('')
  const box = useRef<HTMLDivElement>(null), series = useRef<any>(null), chart = useRef<any>(null)
  const load = () => {
    setBusy(true); setErr(''); setPlay(false)
    fetch('/api/replay', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ spec, day }) })
      .then(async (r) => { const j = await r.json(); if (!r.ok) throw new Error(j.detail || r.status); setData(j); setI(0) })
      .catch((e) => setErr(String(e.message || e))).finally(() => setBusy(false))
  }
  useEffect(() => {
    if (!box.current || !data) return
    const c = createChart(box.current, { width: box.current.clientWidth, height: 260, layout: { background: { type: ColorType.Solid, color: css('--sf') }, textColor: css('--mu') },
      grid: { vertLines: { color: css('--bd') }, horzLines: { color: css('--bd') } }, timeScale: { timeVisible: true } })
    series.current = c.addCandlestickSeries({ upColor: css('--up'), downColor: css('--dn'), borderVisible: false, wickUpColor: css('--up'), wickDownColor: css('--dn') })
    chart.current = c
    return () => c.remove()
  }, [data])
  useEffect(() => {
    if (!series.current || !data) return
    const f = data.frames[i]
    const upto = data.candles.filter((k: any) => +k.t.slice(11, 13) * 60 + +k.t.slice(14, 16) - 555 <= f.m)
    series.current.setData(upto.map((k: any) => ({ time: ep(k.t), open: k.o, high: k.h, low: k.l, close: k.c })))
    const cut = upto.length ? upto[upto.length - 1].t : ''
    series.current.setMarkers(data.fills.filter((x: any) => x.ts.slice(0, 16) <= cut).map((x: any) => ({ time: ep(x.ts.slice(0, 16)), position: x.side === 'BUY' ? 'belowBar' : 'aboveBar',
      shape: x.side === 'BUY' ? 'arrowUp' : 'arrowDown', color: x.reason === 'stop_loss' ? css('--dn') : css('--ac'), text: `${x.side[0]} ${x.symbol.slice(-7)}` })))
    chart.current?.timeScale().fitContent()
  }, [i, data])
  useEffect(() => {
    if (!play || !data) return
    const t = setInterval(() => setI((x) => { if (x >= data.frames.length - 1) { setPlay(false); return x } return x + 1 }), 1000 / speed)
    return () => clearInterval(t)
  }, [play, speed, data])
  if (!spec) return <main className="fade"><div className="card small muted">Open a strategy or a result first, then choose Replay.</div></main>
  const f = data?.frames[i]
  const hm = (m: number) => `${String(9 + Math.floor((15 + m) / 60)).padStart(2, '0')}:${String((15 + m) % 60).padStart(2, '0')}`
  return (
    <main className="fade">
      <div className="row"><button className="back" aria-label="Back" onClick={() => history.back()}>‹</button><h1 style={{ flex: 1 }}>Replay · {spec.name}</h1></div>
      <div className="row" style={{ gap: 8 }}><input className="input" type="date" value={day} onChange={(e) => setDay(e.target.value)} style={{ flex: 1 }} />
        <button className="btn primary" disabled={busy} onClick={load}>{busy ? 'Loading…' : 'Load day'}</button></div>
      {err && <div className="alert">{err}</div>}
      {data && <>
        <div ref={box} className="card" style={{ padding: 0, overflow: 'hidden' }} />
        <div className="row n"><b>{hm(f.m)} IST</b><span>NIFTY {f.spot?.toFixed(1)}</span><b className={cls(f.pnl)}>{inr(f.pnl, true)}</b></div>
        <input type="range" min={0} max={data.frames.length - 1} value={i} onChange={(e) => setI(+e.target.value)} aria-label="Minute" style={{ width: '100%' }} />
        <div className="chips">
          <button className="chip" onClick={() => setI(Math.max(0, i - 1))} aria-label="Back one minute">◀ 1</button>
          <button className="chip on" onClick={() => setPlay(!play)}>{play ? 'Pause' : 'Play'}</button>
          <button className="chip" onClick={() => setI(Math.min(data.frames.length - 1, i + 1))} aria-label="Forward one minute">1 ▶</button>
          {[1, 4, 16].map((s) => <button key={s} className={`chip ${speed === s ? 'on' : ''}`} onClick={() => setSpeed(s)}>{s}×</button>)}
        </div>
        <section className="card"><h2>Positions at {hm(f.m)}</h2>
          {f.positions.length ? <div className="list">{f.positions.map((p: any) => (
            <div className="li n" key={p.symbol}><span><b className={p.side === 'SELL' ? 'dn' : 'up'}>{p.side[0]}</b> {p.symbol} × {p.qty}<br /><span className="muted">in {p.entry} · SL {p.sl ?? '—'} · now {p.last}</span></span><b className={cls(p.pnl)}>{inr(p.pnl, true)}</b></div>))}</div>
            : <div className="small muted">None</div>}</section>
        {data.logs?.length > 0 && <section className="card"><h2>Strategy log</h2><div className="small n" style={{ whiteSpace: 'pre-wrap' }}>{data.logs.slice(-15).join('\n')}</div></section>}
      </>}
    </main>)
}
