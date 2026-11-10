import { useEffect, useState } from 'react'
import { api } from './api'
import { fmtDate } from './fmt'
import { useRoute } from './store'
import { IHome, IMore, IOrders, IStrat, ITrade } from './icons'
import Home from './screens/Home'
import Strategies from './screens/Strategies'
import Builder from './screens/Builder'
import Backtest from './screens/Backtest'
import RunView from './screens/RunView'
import Later from './screens/Later'
import More from './screens/More'
import Login from './screens/Login'
import Paper from './screens/Paper'
import PaperView from './screens/PaperView'
import Cycle from './screens/Cycle'
import Chart from './screens/Chart'
import Sweep from './screens/Sweep'
import Replay from './screens/Replay'
import Compare from './screens/Compare'
import Users from './screens/Users'
import { amountsHidden, setAmountsHidden } from './fmt'
import KillSwitch from './screens/KillSwitch'

export default function App() {
  const [path] = useRoute()
  const [status, setStatus] = useState<any>(null)
  const [online, setOnline] = useState(navigator.onLine)
  const [me, setMe] = useState<any>(null)
  const [broker, setBroker] = useState<any>(null)
  const loadMe = () => fetch('/api/auth/me').then((r) => r.json()).then(setMe).catch(() => setMe({ logged_in: true }))
  useEffect(() => { loadMe() }, [])
  const [, bump] = useState(0)
  const [attn, setAttn] = useState(false)
  const [liveOn, setLiveOn] = useState(false)
  useEffect(() => { const f = () => fetch('/api/live').then((r) => r.ok ? r.json() : null).then((l) => setLiveOn(!!l && (l.oms?.legs || []).some((x: any) => ['entering', 'open', 'exiting'].includes(x.status)) || false)).catch(() => {}); f(); const t = setInterval(f, 10000); return () => clearInterval(t) }, [])
  useEffect(() => { const f = () => fetch('/api/health/details').then((r) => r.json()).then((h) => setAttn(!!h.attention)).catch(() => {}); f(); const t = setInterval(f, 60000); return () => clearInterval(t) }, [])
  useEffect(() => { const f = () => bump((x) => x + 1); addEventListener('td-tz', f); return () => removeEventListener('td-tz', f) }, [])
  useEffect(() => { if (me?.logged_in) { api.status().then(setStatus).catch(() => setStatus({ error: true })); api.broker().then(setBroker).catch(() => setBroker(null)) } }, [me?.logged_in])
  useEffect(() => { const on = () => setOnline(true), off = () => setOnline(false); addEventListener('online', on); addEventListener('offline', off); return () => { removeEventListener('online', on); removeEventListener('offline', off) } }, [])
  if (me && !me.logged_in) return <div className="app"><Login hasDevices={me.has_devices} onDone={loadMe} /></div>
  const tab = path.startsWith('/strateg') || path.startsWith('/sweep') || path.startsWith('/replay') || path.startsWith('/compare') || path.startsWith('/builder') || path.startsWith('/backtest') || path.startsWith('/run') ? 'strategies'
    : path.startsWith('/trade') || path.startsWith('/paper') || path.startsWith('/cycle') || path.startsWith('/chart') ? 'trade' : path.startsWith('/orders') ? 'orders' : path.startsWith('/more') ? 'more' : 'home'
  let screen
  if (path.startsWith('/strategies')) screen = <Strategies />
  else if (path.startsWith('/builder')) screen = <Builder />
  else if (path.startsWith('/backtest')) screen = <Backtest />
  else if (path.startsWith('/run/')) screen = <RunView id={decodeURIComponent(path.slice(5))} />
  else if (path.startsWith('/trade')) screen = <Paper />
  else if (path.startsWith('/users')) screen = <Users />
  else if (path.startsWith('/sweep')) screen = <Sweep />
  else if (path.startsWith('/replay')) screen = <Replay />
  else if (path.startsWith('/compare/')) screen = <Compare ids={path.slice(9).split(',').map(decodeURIComponent)} />
  else if (path.startsWith('/chart/')) screen = <Chart sym={decodeURIComponent(path.slice(7))} />
  else if (path.startsWith('/cycle/')) { const [, , a, b] = path.split('/'); screen = <Cycle sid={decodeURIComponent(a)} tid={Number(b)} /> }
  else if (path.startsWith('/paper/')) screen = <PaperView id={decodeURIComponent(path.slice(7))} />
  else if (path.startsWith('/orders')) screen = <Later title="Orders" what="Positions, orders and trades (paper first)" />
  else if (path.startsWith('/more')) screen = <More status={status} broker={broker} me={me} />
  else screen = <Home status={status} />
  const T = (id: string, href: string, label: string, Icon: () => JSX.Element) => (
    <a href={'#' + href} className={tab === id ? 'on' : ''} aria-current={tab === id ? 'page' : undefined}><Icon />{label}</a>)
  return (
    <div className="app">
      <header className="strip" role="status">
        <span>
          {!online ? <>⚠ Offline</> : status?.error ? <>⚠ Server not reachable</> :
            <><span className="dot" style={{ background: status?.store_ok ? 'var(--up)' : 'var(--mu)' }} />Data to {status?.latest_data ? fmtDate(status.latest_data) : '…'}
              {broker?.configured && <> · Kite {broker.logged_in_today ? '✓' : '✗ log in'}</>}</>}
        </span>
        {liveOn && <KillSwitch pill />}
        <button className="mode" style={{ cursor: 'pointer' }} aria-label={amountsHidden() ? 'Show amounts' : 'Hide amounts'}
          onClick={() => setAmountsHidden(!amountsHidden())}>{amountsHidden() ? '₹ hidden' : '₹ shown'}</button>
        <span className="mode" title="Live orders only for armed live sessions">PAPER + LIVE</span>
      </header>
      {screen}
      <nav className="tabs" aria-label="Main">
        {T('home', '/', 'Home', IHome)}{T('strategies', '/strategies', 'Strategies', IStrat)}{T('trade', '/trade', 'Trade', ITrade)}
        {T('orders', '/orders', 'Orders', IOrders)}{T('more', '/more', attn ? 'More •' : 'More', IMore)}
      </nav>
    </div>
  )
}
