import { useEffect, useState } from 'react'

const u2a = (s: string) => { const p = s.replace(/-/g, '+').replace(/_/g, '/'); const r = atob(p + '==='.slice((p.length + 3) % 4)); return Uint8Array.from(r, (c) => c.charCodeAt(0)) }

export default function AlertsCard() {
  const [a, setA] = useState<any>(null)
  const [msg, setMsg] = useState('')
  const load = () => fetch('/api/alerts').then((r) => r.json()).then(setA).catch(() => setA(null))
  useEffect(() => { load() }, [])
  const enable = async () => {
    setMsg('')
    try {
      if (!('serviceWorker' in navigator) || !('PushManager' in window)) throw new Error('On iPhone, open TradeDesk from the Home Screen icon (not Safari) to allow notifications.')
      const perm = await Notification.requestPermission()
      if (perm !== 'granted') throw new Error('Notifications were not allowed. Settings → TradeDesk → Notifications.')
      const reg = await navigator.serviceWorker.ready
      const sub = (await reg.pushManager.getSubscription()) || await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: u2a(a.public_key) })
      const name = /iPhone/.test(navigator.userAgent) ? 'iPhone' : /Android/.test(navigator.userAgent) ? 'Android' : 'Mac'
      await fetch('/api/alerts/subscribe', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ subscription: sub.toJSON(), name }) })
      setMsg('Notifications on for this device.'); load()
    } catch (e: any) { setMsg(String(e.message || e)) }
  }
  const test = () => fetch('/api/alerts/test', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ level: 'critical' }) })
    .then((r) => r.json()).then((r) => { setMsg('Sent: ' + Object.entries(r.result).map(([k, v]) => `${k} ${v}`).join(' · ')); load() })
  const ch = a?.channels || {}
  const S = (ok: any, label: string, how: string) => <div className="li"><span>{label}</span><span className={ok ? 'up' : 'muted'}>{ok ? (typeof ok === 'number' ? `${ok} device${ok > 1 ? 's' : ''}` : 'on') : how}</span></div>
  return (
    <section className="card">
      <h2>Alerts</h2>
      <div className="list">
        {S(ch.push, 'Push', 'not on this device yet')}
        {S(ch.whatsapp, 'WhatsApp (critical only)', 'set up on the server')}
        {S(ch.email, 'Email', 'set up on the server')}
      </div>
      <div className="chips"><button className="chip" onClick={enable} disabled={!a}>Turn on notifications here</button><button className="chip" onClick={test}>Send test alert</button></div>
      {msg && <p className="small">{msg}</p>}
      <div className="small muted">Entries: push · stops and day close: push + email · daily loss limit, kill rule, feed down, Kite login missing: push + WhatsApp + email.</div>
      {a?.recent?.length > 0 && <div className="list">{a.recent.slice(0, 6).map((r: any, i: number) => (
        <div className="li small" key={i}><span>{r.at.slice(5, 16).replace('T', ' ')} {r.title}<br /><span className="muted">{r.body}</span></span></div>))}</div>}
    </section>)
}
