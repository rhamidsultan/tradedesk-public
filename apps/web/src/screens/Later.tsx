export default function Later({ title, what }: { title: string; what: string }) {
  return (
    <main className="fade">
      <h1>{title}</h1>
      <div className="card">
        <b>Coming with paper trading (milestone 5)</b>
        <div className="small muted">{what}. These screens are designed and approved; they need the server's daily Kite login and live
          prices first. Until then, use Strategies to build and backtest.</div>
        <a className="btn" href="#/strategies">Go to Strategies</a>
      </div>
    </main>)
}
