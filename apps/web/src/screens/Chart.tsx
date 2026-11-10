import { useEffect, useRef, useState } from 'react'
import { createChart, ColorType, CrosshairMode, type IChartApi, type UTCTimestamp } from 'lightweight-charts'
import { getTz } from '../fmt'
import { usePoll } from '../hooks/usePoll'

const css = (v: string) => getComputedStyle(document.documentElement).getPropertyValue(v).trim()
/** IST wall time → chart epoch: shown as IST (default) or shifted to the device zone. */
function epoch(ist: string): UTCTimestamp {
  const asUtc = Date.parse(ist.replace(' ', 'T') + ':00Z') / 1000
  if (getTz() === 'ist') return asUtc as UTCTimestamp
  const real = asUtc - 5.5 * 3600
  return (real - new Date(real * 1000).getTimezoneOffset() * 60) as UTCTimestamp
}

/** C1: 1-minute chart of any index, option or stock, with this app's entries, stops and fills marked. */
export default function Chart({ sym }: { sym: string }) {
  const box = useRef<HTMLDivElement>(null)
  const chart = useRef<IChartApi | null>(null)
  const series = useRef<any>(null)
  const [days, setDays] = useState(1)
  const [d, setD] = useState<any>(null)
  const load = () => fetch(`/api/chart/${encodeURIComponent(sym)}?days=${days}`).then((r) => r.json()).then(setD).catch(() => { /* keep */ })
  useEffect(() => { setD(null); load() }, [sym, days])
  usePoll(load, false, [sym, days])
  useEffect(() => {
    if (!box.current) return
    const c = createChart(box.current, {
      width: box.current.clientWidth, height: 340, layout: { background: { type: ColorType.Solid, color: css('--sf') }, textColor: css('--mu') },
      grid: { vertLines: { color: css('--bd') }, horzLines: { color: css('--bd') } }, crosshair: { mode: CrosshairMode.Normal },
      timeScale: { timeVisible: true, secondsVisible: false, borderColor: css('--bd') }, rightPriceScale: { borderColor: css('--bd') },
    })
    series.current = c.addCandlestickSeries({ upColor: css('--up'), downColor: css('--dn'), borderVisible: false, wickUpColor: css('--up'), wickDownColor: css('--dn') })
    chart.current = c
    const ro = new ResizeObserver(() => c.applyOptions({ width: box.current!.clientWidth }))
    ro.observe(box.current)
    return () => { ro.disconnect(); c.remove() }
  }, [])
  useEffect(() => {
    const s = series.current
    if (!s || !d) return
    s.setData(d.candles.map((k: any) => ({ time: epoch(k.t), open: k.o, high: k.h, low: k.l, close: k.c })))
    chart.current?.timeScale().fitContent()
    const first = d.candles[0]?.t || ''
    s.setMarkers(d.marks.filter((m: any) => m.t >= first).sort((a: any, b: any) => a.t.localeCompare(b.t)).map((m: any) => ({
      time: epoch(m.t), position: m.side === 'BUY' ? 'belowBar' : 'aboveBar', shape: m.side === 'BUY' ? 'arrowUp' : 'arrowDown',
      color: m.reason === 'stop_loss' ? css('--dn') : css('--ac'), text: `${m.side === 'BUY' ? 'B' : 'S'} ${m.price}`,
    })))
    ;(s._lines || []).forEach((l: any) => s.removePriceLine(l))
    s._lines = d.lines.map((l: any) => s.createPriceLine({ price: l.price, color: l.kind === 'sl' ? css('--dn') : css('--ac'), lineWidth: 1, lineStyle: 2, title: l.label }))
  }, [d])
  const last = d?.candles?.[d.candles.length - 1]
  return (
    <main className="fade">
      <div className="row"><button className="back" aria-label="Back" onClick={() => history.back()}>‹</button><h1 style={{ flex: 1, overflowWrap: 'anywhere' }}>{sym}</h1>
        {last && <b className="n">{last.c}</b>}</div>
      <div className="chips">{[1, 2, 5].map((n) => <button key={n} className={`chip ${days === n ? 'on' : ''}`} onClick={() => setDays(n)}>{n === 1 ? 'Last day' : `${n} days`}</button>)}</div>
      <div ref={box} className="card" style={{ padding: 0, overflow: 'hidden' }} aria-label={`1-minute chart of ${sym}`} />
      {d && !d.candles?.length && <div className="card small muted">No stored 1-minute data for {sym}. Options appear once the live recorder has seen them, or from the imported history.</div>}
      {(d?.lines?.length || 0) > 0 && <div className="small muted">Dashed lines: your open entries and stops. Arrows: your fills (red = stop loss).</div>}
      <div className="small muted">Times in {getTz() === 'ist' ? 'IST' : 'this device’s time zone'} · 1-minute candles · updates every 5 s</div>
    </main>)
}
