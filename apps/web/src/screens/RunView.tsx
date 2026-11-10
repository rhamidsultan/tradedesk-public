import { useEffect, useState } from 'react'
import { api, type RunResult } from '../api'
import Result from '../components/Result'
import { IBack } from '../icons'
import { go, setDraft } from '../store'

export default function RunView({ id }: { id: string }) {
  const [r, setR] = useState<RunResult | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [del, setDel] = useState(false)
  useEffect(() => { api.run(id).then(setR).catch((e) => setErr(String(e))) }, [id])
  return (<>
    <header style={{ padding: '6px 8px 10px', borderBottom: '1px solid var(--bd)' }}>
      <div className="row" style={{ justifyContent: 'flex-start', gap: 4 }}>
        <button className="back" aria-label="Back" onClick={() => history.back()}><IBack /></button>
        <div><b>{r?.strategy?.name ?? 'Result'}</b><div className="small muted" style={{ overflowWrap: 'anywhere' }}>{id}</div></div>
      </div>
    </header>
    <main>
      {err && <div className="alert">Could not load: {err}</div>}
      {!r && !err && <div className="card small muted">Loading…</div>}
      {r && <Result r={r} />}
    </main>
    {r && <footer className="footer">
      <button className="btn primary" onClick={() => { setDraft(r.strategy); go('/builder') }}>Open these settings in the builder</button>
      <div className="row" style={{ gap: 8 }}>
        <button className="btn" style={{ flex: 1 }} onClick={() => { setDraft(r.strategy); go('/sweep') }}>Sweep</button>
        <button className="btn" style={{ flex: 1 }} onClick={() => { setDraft(r.strategy); go('/replay') }}>Replay a day</button>
        {del ? <button className="btn" style={{ flex: 1, color: 'var(--dn)', borderColor: 'var(--dn)' }} onClick={() => fetch(`/api/runs/${id}`, { method: 'DELETE' }).then(() => go('/strategies?tab=results'))}>Delete</button>
          : <button className="btn" style={{ flex: 1 }} onClick={() => setDel(true)}>Delete…</button>}
      </div></footer>}
  </>)
}
