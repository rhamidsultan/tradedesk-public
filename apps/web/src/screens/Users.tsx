import { useEffect, useState } from 'react'

const AREAS: [string, string][] = [['research', 'Strategies & backtests'], ['trade', 'Paper trading'], ['live', 'Live trading'], ['admin', 'Server & updates']]
const LEVELS = ['none', 'view', 'use']

/** H4: family logins — owner invites, sets access per area, removes; activity log. */
export default function Users() {
  const [us, setUs] = useState<any[] | null>(null), [act, setAct] = useState<any[]>([])
  const [name, setName] = useState(''), [acc, setAcc] = useState<Record<string, string>>({ research: 'use', trade: 'view', live: 'none', admin: 'none' })
  const [code, setCode] = useState<any>(null), [err, setErr] = useState('')
  const load = () => { fetch('/api/auth/users').then((r) => (r.ok ? r.json() : Promise.reject(r.status))).then(setUs).catch(() => setUs([])); fetch('/api/activity').then((r) => r.json()).then(setAct).catch(() => setAct([])) }
  useEffect(load, [])
  const post = (url: string, body: any, method = 'POST') => fetch(url, { method, headers: { 'content-type': 'application/json' }, body: JSON.stringify(body) }).then(async (r) => { const j = await r.json(); if (!r.ok) throw new Error(j.detail || r.status); return j })
  const Sel = ({ v, on }: { v: string; on: (x: string) => void }) => <div className="chips">{LEVELS.map((l) => <button key={l} className={`chip ${v === l ? 'on' : ''}`} onClick={() => on(l)}>{l}</button>)}</div>
  return (
    <main className="fade">
      <div className="row"><a className="back" href="#/more" aria-label="Back">‹</a><h1 style={{ flex: 1 }}>Family logins</h1></div>
      <p className="small muted">Each person signs in with their own passkey. Live trading uses the owner's Zerodha account until each person has their own Kite Connect app.</p>
      {(us || []).map((u) => (
        <section className="card" key={u.id}>
          <div className="row"><b>{u.name}</b><span className="tag">{u.role} · {u.devices} device{u.devices === 1 ? '' : 's'}</span></div>
          {u.role !== 'owner' && <>
            {AREAS.map(([a, label]) => <div className="field" key={a}><label>{label}</label><Sel v={u.access[a] || 'none'} on={(l) => post(`/api/auth/users/${u.id}`, { access: { ...u.access, [a]: l } }).then(load)} /></div>)}
            <button className="btn" style={{ color: 'var(--dn)', borderColor: 'var(--dn)' }} onClick={() => fetch(`/api/auth/users/${u.id}`, { method: 'DELETE' }).then(load)}>Remove {u.name} (signs out at once)</button>
          </>}
        </section>))}
      <section className="card">
        <h2>Invite someone</h2>
        <input className="input" placeholder="Name" value={name} onChange={(e) => setName(e.target.value)} />
        {AREAS.map(([a, label]) => <div className="field" key={a}><label>{label}</label><Sel v={acc[a]} on={(l) => setAcc({ ...acc, [a]: l })} /></div>)}
        <button className="btn primary" disabled={!name.trim()} onClick={() => { setErr(''); post('/api/auth/invite', { name, access: acc }).then((j) => { setCode(j); setName(''); load() }).catch((e) => setErr(String(e.message))) }}>Create invite code</button>
        {code && <div className="warn"><b>Invite code: <span className="n" style={{ userSelect: 'all' }}>{code.code}</span></b><div className="small">Valid 24 hours, once. They open this app's address on their phone, enter the code and create their passkey.</div></div>}
        {err && <div className="small dn">{err}</div>}
      </section>
      <section className="card"><h2>Activity</h2>
        <div className="list">{act.slice(0, 50).map((a, i) => <div className="li small" key={i}><span>{a.at.slice(5, 16).replace('T', ' ')} · {a.name || a.user}</span><span className="muted" style={{ overflowWrap: 'anywhere', textAlign: 'right' }}>{a.action}</span></div>)}</div>
        {!act.length && <div className="small muted">Nothing yet.</div>}
      </section>
    </main>)
}
