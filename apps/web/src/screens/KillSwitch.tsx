import { useState } from 'react'
import { api } from '../api'
import Slide from '../components/Slide'

/** STOP ALL LIVE: a sheet with slide-to-confirm. `pill` renders the small red button for the status strip. */
export default function KillSwitch({ pill = false }: { pill?: boolean }) {
  const [ask, setAsk] = useState(false), [msg, setMsg] = useState('')
  return <>
    {pill ? <button className="stoppill" onClick={() => setAsk(true)} aria-label="Stop all live trading">■ STOP</button>
      : <button className="btn" style={{ borderColor: 'var(--dn)', color: 'var(--dn)', fontWeight: 700 }} onClick={() => setAsk(true)}>■ STOP ALL LIVE</button>}
    {!pill && msg && <div className={msg.startsWith('⚠') ? 'alert' : 'small'} role="status">{msg}</div>}
    {ask && <><div className="scrim" onClick={() => setAsk(false)} />
      <div className="sheet" role="alertdialog" aria-label="Stop all live trading">
        <h2>Stop all live trading?</h2>
        <p className="small">Every live position is exited at the market touch — your SL orders at Kite become the exit orders, shorts are bought back before wings are sold. Unfilled entries are cancelled, all live sessions are disarmed and live is locked until tomorrow. Paper keeps running.</p>
        <Slide label="Slide to stop all" onDone={() => api.kill().then(() => { setMsg('Stop all received by the server. Watch the legs close in Live orders.'); setAsk(false) })
          .catch((e) => { setAsk(false); setMsg('⚠ ' + String(e.message).replace(/^\d+ /, '').replace(/^"|"$/g, '') + ' Sessions are disarmed; open positions need Kite or Zerodha Call & Trade.') })} />
        <button className="btn" onClick={() => setAsk(false)}>Cancel</button>
        {pill && msg && <div className="small">{msg}</div>}
      </div></>}
    {pill && msg && !ask && <span className="sr-only" role="status">{msg}</span>}
  </>
}
