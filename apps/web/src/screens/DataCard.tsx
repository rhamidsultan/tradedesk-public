import { useEffect, useState } from 'react'
import Fold from '../components/Fold'

/** G1: stored days per index as a calendar, expiries folded, disk use. */
export default function DataCard() {
  const [u, setU] = useState('NIFTY')
  const [d, setD] = useState<any>(null)
  const [mi, setMi] = useState(0)
  useEffect(() => { setD(null); fetch(`/api/data/days?u=${u}`).then((r) => r.json()).then((x) => { setD(x); setMi(0) }).catch(() => setD({ days: {} })) }, [u])
  const days = d?.days || {}
  const months = [...new Set(Object.keys(days).map((k) => k.slice(0, 7)))].sort().reverse()
  const ym = months[mi]
  const gb = (b: number) => `${(b / 2 ** 30).toFixed(2)} GB`
  return (
    <Fold id="more-data" title="Stored data">
      <div className="chips">{['NIFTY', 'BANKNIFTY', 'SENSEX'].map((x) => <button key={x} className={`chip ${u === x ? 'on' : ''}`} onClick={() => setU(x)}>{x}</button>)}</div>
      {!d ? <div className="small muted">Loading…</div> : !months.length ? <div className="small muted">No stored days for {u}.</div> : <>
        <div className="row"><button className="back" disabled={mi >= months.length - 1} onClick={() => setMi(mi + 1)} aria-label="Older month">‹</button>
          <b>{ym}</b><button className="back" disabled={mi <= 0} onClick={() => setMi(mi - 1)} aria-label="Newer month">›</button></div>
        <div className="calgrid">
          {(() => { const [y, m] = ym.split('-').map(Number); const first = (new Date(y, m - 1, 1).getDay() + 6) % 7; const n = new Date(y, m, 0).getDate()
            return [...Array(first).fill(null), ...Array.from({ length: n }, (_, i) => i + 1)].map((dd, i) => {
              if (dd === null) return <span key={i} />
              const k = `${ym}-${String(dd).padStart(2, '0')}`, src = days[k]
              return <span key={i} className="calday n" title={src ? src.join(', ') : 'no data'}
                style={{ display: 'grid', placeItems: 'center', background: src ? `color-mix(in srgb, ${src.includes('kite_live') ? 'var(--ac)' : 'var(--up)'} 45%, transparent)` : undefined }}>{dd}</span> }) })()}
        </div>
        <div className="small muted">Green: imported history · blue: recorded live by the server · empty: holiday or missing</div>
        {d.expiries && <Fold id={`more-exp-${u}`} title="Expiries" open={false}><div className="list">{Object.entries(d.expiries).map(([e, n]: [string, any]) => <div className="li small n" key={e}><span>{e}</span><span className="muted">{n} days of option data</span></div>)}</div></Fold>}
        {d.sizes && <div className="small muted n">Disk: {Object.entries(d.sizes).filter(([, b]: any) => b > 0).map(([k, b]: any) => `${k} ${gb(b)}`).join(' · ')}</div>}
      </>}
    </Fold>)
}
