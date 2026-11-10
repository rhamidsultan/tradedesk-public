import { useEffect, useState } from 'react'
import { api } from '../api'
import { cls, dtm, inr, tm } from '../fmt'

/** B5: one closed (or open) cycle — legs, every fill, reason, charges. */
export default function Cycle({ sid, tid }: { sid: string; tid: number }) {
  const [s, setS] = useState<any>(null)
  const [why, setWhy] = useState(''), [msg, setMsg] = useState('')
  useEffect(() => { api.paperGet(sid).then(setS).catch(() => setS({ error: 'Not found' })) }, [sid])
  const t = s?.trades?.find((x: any) => x.id === tid)
  return (
    <main className="fade">
      <div className="row"><a className="back" href={`#/paper/${encodeURIComponent(sid)}`} aria-label="Back">‹</a><h1 style={{ flex: 1 }}>Cycle {tid}</h1></div>
      {!s ? <div className="card small muted">Loading…</div> : !t ? <div className="card small muted">Cycle not found (only the last 200 are kept on the phone).</div> : <>
        <section className="card">
          <div className="small muted">{s.name}</div>
          <div className={`big n ${cls(t.net)}`}>{inr(t.net, true)}</div>
          <div className="small muted n">{dtm(t.entry)} → {t.open ? 'open' : dtm(t.exit)} · {t.reason ? t.reason.replace(/_/g, ' ') : '—'}</div>
          <div className="grid3">
            <div className="stat"><div className="k">Before charges</div><div className={`v n ${cls(t.gross)}`}>{inr(t.gross, true)}</div></div>
            <div className="stat"><div className="k">Charges</div><div className="v n">{inr(t.charges)}</div></div>
            <div className="stat"><div className="k">Legs</div><div className="v n">{t.legs.length}</div></div>
          </div>
        </section>
        <section className="card"><h2>Legs</h2><div className="list">{t.legs.map((l: any, i: number) => {
          const out = (t.fills || []).filter((f: any) => f.symbol === l.symbol && f.reason !== 'entry')
          return (
            <a className="li n" key={i} href={`#/chart/${encodeURIComponent(l.symbol)}`} style={{ color: 'var(--tx)', textDecoration: 'none' }}>
              <span><b className={l.side === 'SELL' ? 'dn' : 'up'}>{l.side === 'SELL' ? 'S' : 'B'}</b> {l.symbol} × {l.qty}<br />
                <span className="muted">in {l.entry}{out.length ? ` · out ${out.map((f: any) => f.price).join(', ')}` : ''} · SL {l.sl ?? l.spot_sl ?? '—'}{l.tgt ? ` · target ${l.tgt}` : ''}</span></span>
              <span className="muted">›</span></a>)
        })}</div></section>
        {s.mode !== 'live' && !t.open && <section className="card">
          <h2>{t.excluded ? 'Excluded from the results' : 'Exclude this cycle'}</h2>
          {t.excluded ? <><div className="small">Reason: {t.excluded.reason} ({t.excluded.at})</div>
            <button className="btn" onClick={() => fetch(`/api/paper/${encodeURIComponent(sid)}/include`, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ trade_ids: [tid], reason: '-' }) }).then(() => api.paperGet(sid).then(setS))}>Include it again</button></>
            : <><p className="small muted">For cycles made on bad prices (e.g. a holiday or a feed fault). The cycle is kept and marked; totals and the paper gate leave it out. Paper only.</p>
              <input className="input" placeholder="Reason (required)" value={why} onChange={(e) => setWhy(e.target.value)} />
              <button className="btn" disabled={!why.trim()} onClick={() => fetch(`/api/paper/${encodeURIComponent(sid)}/exclude`, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ trade_ids: [tid], reason: why }) })
                .then((r) => { if (!r.ok) throw new Error(String(r.status)); setMsg('Excluded.'); return api.paperGet(sid).then(setS) }).catch((e) => setMsg('Not excluded: ' + e.message))}>Exclude from results</button></>}
          {msg && <div className="small" role="status">{msg}</div>}
        </section>}
        <section className="card"><h2>Fills</h2><div className="list">{(t.fills || []).map((f: any, i: number) => (
          <div className="li n" key={i}><span>{tm(f.ts)} {f.side} {f.symbol} × {f.qty}<br /><span className="muted">{f.reason.replace(/_/g, ' ')} · charges ₹{f.charges.toFixed(2)}{f.stale ? ' · stale price' : ''}</span></span><span>₹{f.price}</span></div>))}</div></section>
      </>}
    </main>)
}
