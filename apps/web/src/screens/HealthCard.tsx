import { useState } from 'react'
import { usePoll } from '../hooks/usePoll'
import Fold from '../components/Fold'

const gb = (b?: number) => (b ? `${(b / 2 ** 30).toFixed(1)} GB` : '—')
const LIGHT: Record<string, string> = { green: 'var(--up)', amber: 'var(--live)', red: 'var(--dn)' }

/** H2 / G2 / H1: server health, recording light, version and Update now. */
export default function HealthCard() {
  const [h, setH] = useState<any>(null)
  const [ask, setAsk] = useState(false), [msg, setMsg] = useState('')
  usePoll(() => fetch('/api/health/details').then((r) => r.json()).then(setH).catch(() => setH((x: any) => x || { error: true })), false)
  if (!h) return null
  if (h.error) return <section className="card"><h2>Server health</h2><div className="dn small">Server not reachable.</div></section>
  const v = h.version || {}
  const rec = h.recording || {}
  return (<>
    <Fold id="more-health" title={<>Server health {h.attention && <span className="dot8" style={{ background: 'var(--live)', marginLeft: 6 }} />}</>}>
      {h.notes?.length > 0 && <div className="warn small">{h.notes.join(' · ')}</div>}
      <div className="list">
        <div className="li"><span>Recording <span className="muted small">{rec.now}</span></span><span><span className="dot8" style={{ background: LIGHT[rec.light] }} />{rec.light}</span></div>
        {Object.entries(rec.days || {}).slice(-5).reverse().map(([d, x]: [string, any]) => <div className="li small" key={d}><span>{d}</span><span className={x.ok ? 'muted' : 'dn'}>{x.why}</span></div>)}
        <div className="li"><span>Kite login</span><span className={h.kite?.logged_in_today ? 'up' : 'dn'}>{h.kite?.logged_in_today ? 'valid today' : 'needed'}</span></div>
        {Object.entries(h.services || {}).map(([k, s]: [string, any]) => <div className="li small" key={k}><span>{k}</span><span className={s === 'active' ? 'up' : s === 'unknown' ? 'muted' : 'dn'}>{s}</span></div>)}
        <div className="li small"><span>Memory free</span><span className="n">{gb(h.memory?.available)} of {gb(h.memory?.total)}</span></div>
        <div className="li small"><span>Disk free</span><span className="n">{gb(h.disk?.free)} of {gb(h.disk?.total)}</span></div>
      </div>
    </Fold>
    <Fold id="more-update" title={<>Server version {v.latest && v.latest !== v.running && <span className="dot8" style={{ background: 'var(--ac)', marginLeft: 6 }} />}</>} open={false}>
      <div className="small n">Running <b>{v.running}</b>{v.latest ? <> · on GitHub <b>{v.latest}</b> <span className="muted">{v.latest_title}</span></> : ''}</div>
      {v.state && <div className={`small ${v.state === 'error' ? 'dn' : 'muted'}`}>{v.message} {v.checked ? `(${v.checked})` : ''}</div>}
      {v.latest && v.latest !== v.running && (ask
        ? <div className="warn small">Update now? The app and paper engine restart for about a minute (paper sessions resume; live stops at Kite stay in place). It rolls back by itself if the new version does not start.
            <div className="row" style={{ gap: 8, marginTop: 8 }}><button className="btn" style={{ flex: 1 }} onClick={() => setAsk(false)}>Not now</button>
              <button className="btn primary" style={{ flex: 1 }} onClick={() => fetch('/api/update', { method: 'POST' }).then((r) => r.json()).then((j) => { setMsg(j.message); setAsk(false) })}>Update now</button></div></div>
        : <button className="btn primary" onClick={() => setAsk(true)}>Update to {v.latest}</button>)}
      {msg && <div className="small" role="status">{msg}</div>}
    </Fold>
  </>)
}
