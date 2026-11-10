import { useState } from 'react'
import { registerPasskey, signIn } from '../passkey'

export default function Login({ hasDevices, onDone }: { hasDevices: boolean; onDone: () => void }) {
  const [code, setCode] = useState(''), [name, setName] = useState('iPhone')
  const [err, setErr] = useState(''), [busy, setBusy] = useState(false)
  const [inv, setInv] = useState(false)
  const go = async (f: () => Promise<any>) => { setBusy(true); setErr(''); try { await f(); onDone() } catch (e: any) { setErr(e?.name === 'NotAllowedError' ? 'Cancelled.' : String(e?.message || e)) } finally { setBusy(false) } }
  return (
    <main className="fade" style={{ paddingTop: 48 }}>
      <h1>TradeDesk</h1>
      {hasDevices && !inv ? (
        <section className="card">
          <p>Sign in with Face ID / Touch ID on this device.</p>
          <button className="btn primary" disabled={busy} onClick={() => go(signIn)}>Sign in with passkey</button>
          <button className="btn" onClick={() => setInv(true)}>I have an invite code</button>
        </section>
      ) : (
        <section className="card">
          <h2>Set up this device</h2>
          <p className="small muted">Enter your invite code (from the owner) or, for the owner's first device, the setup code the installer printed. It works once.</p>
          <label>Setup or invite code<input className="input" value={code} onChange={(e) => setCode(e.target.value)} autoCapitalize="none" autoComplete="one-time-code" /></label>
          <label>Device name<input className="input" value={name} onChange={(e) => setName(e.target.value)} /></label>
          <button className="btn primary" disabled={busy || !code} onClick={() => go(() => registerPasskey(code, name))}>Create passkey</button>
        </section>
      )}
      {err && <p className="dn" role="alert">{err}</p>}
    </main>)
}
