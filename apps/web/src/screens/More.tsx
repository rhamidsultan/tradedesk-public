import { useEffect, useState } from 'react'
import { api } from '../api'
import { registerPasskey } from '../passkey'
import AlertsCard from './AlertsCard'
import HealthCard from './HealthCard'
import DataCard from './DataCard'
import { getTz, setTz, type Tz } from '../fmt'

export default function More({ status, broker, me }: { status: any; broker: any; me: any }) {
  const [msg, setMsg] = useState('')
  const [tz, setTzS] = useState<Tz>(getTz())
  const [data, setData] = useState<any>(null)
  const [theme, setTheme] = useState(() => { try { return localStorage.getItem('td-theme') || 'auto' } catch { return 'auto' } })
  useEffect(() => { api.data().then(setData).catch(() => setData({ coverage: [], dq: [] })) }, [])
  const pick = (t: string) => {
    setTheme(t)
    try { if (t === 'auto') { localStorage.removeItem('td-theme'); delete document.documentElement.dataset.theme } else { localStorage.setItem('td-theme', t); document.documentElement.dataset.theme = t } } catch { /* ignore */ }
  }
  return (
    <main className="fade">
      <h1>More</h1>
      <section className="card">
        <h2>Theme</h2>
        <div className="chips">{['auto', 'dark', 'light'].map((t) => <button key={t} className={`chip ${theme === t ? 'on' : ''}`} onClick={() => pick(t)}>{t[0].toUpperCase() + t.slice(1)}</button>)}</div>
      </section>
      <section className="card">
        <h2>Show times in</h2>
        <div className="chips">{([['ist', 'India time (IST)'], ['device', `This device (${Intl.DateTimeFormat().resolvedOptions().timeZone})`]] as [Tz, string][]).map(([k, l]) =>
          <button key={k} className={`chip ${tz === k ? 'on' : ''}`} onClick={() => { setTz(k); setTzS(k) }}>{l}</button>)}</div>
        <div className="small muted">Market rules (09:15, 15:30) always stay in IST.</div>
      </section>
      <section className="card">
        <h2>Data</h2>
        <div className="small muted">{status?.store}</div>
        <div className="list">{(data?.coverage || []).map((c: any) => (
          <div className="li n" key={c.segment + c.underlying + c.source}><span>{c.underlying} {c.segment === 'opt' ? 'options' : c.segment === 'idx' ? 'index' : c.segment} <span className="muted">({c.source})</span></span>
            <span className="muted">{c.first} → {c.last} · {c.days.toLocaleString('en-IN')} days</span></div>))}</div>
      </section>
      <section className="card">
        <h2>Data quality</h2>
        <div className="list">{(data?.dq || []).map((d: any) => (
          <div className="li" key={d.period}><span>{d.period}</span><span className={d.verdict.startsWith('FAIL') ? 'dn' : d.verdict === 'PASS' ? 'up' : 'muted'}>{d.verdict}</span></div>))}</div>
      </section>
      <section className="card small muted">
        <h2 style={{ color: 'var(--tx)' }}>Broker</h2>
        {broker ? <>{broker.message}{broker.user && <> · {broker.user}</>} <span className="muted">(checked {broker.checked})</span></> : status?.kite}
      </section>
      {me?.role === 'owner' && me?.auth_required && <a className="card" href="#/users" style={{ textDecoration: 'none', color: 'var(--tx)' }}><div className="row"><h2>Family logins</h2><span className="muted">›</span></div><div className="small muted">Invite, set access, activity</div></a>}
      <HealthCard />
      <DataCard />
      <Reports />
      <AlertsCard />
      {me?.auth_required && <section className="card">
        <h2>Devices</h2>
        <div className="list">{(me.devices || []).map((d: any) => <div className="li" key={d.name + d.added}><span>{d.name}</span><span className="muted">{d.added}</span></div>)}</div>
        <div className="chips">
          <button className="chip" onClick={() => registerPasskey(null, prompt('Device name') || 'Device').then(() => setMsg('Device added.')).catch((e) => setMsg(String(e.message || e)))}>Add this device</button>
          <button className="chip" onClick={() => fetch('/api/auth/logout', { method: 'POST' }).then(() => location.reload())}>Sign out</button>
        </div>
        {msg && <p className="small">{msg}</p>}
      </section>}
    </main>)
}

function Reports() {
  const [r, setR] = useState<any[]>([])
  const [open, setOpen] = useState<string | null>(null)
  useEffect(() => { fetch('/api/reports').then((x) => x.json()).then(setR).catch(() => setR([])) }, [])
  if (!r.length) return null
  return (
    <section className="card"><h2>Weekly paper reviews</h2>
      <div className="list">{r.map((x) => <div key={x.name}><button className="li" style={{ width: '100%', background: 'none', border: 0, cursor: 'pointer' }} onClick={() => setOpen(open === x.name ? null : x.name)}><span>{x.name}</span><span className="muted">{open === x.name ? '▾' : '▸'}</span></button>
        {open === x.name && <div className="small" style={{ whiteSpace: 'pre-wrap' }}>{x.text}</div>}</div>)}</div>
    </section>)
}
