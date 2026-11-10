/** B7: payoff of a session's open option legs — at expiry and today (Black-Scholes with each leg's implied vol). */
const PAT = /^([A-Z]+?)(\d{2})(\d{2})(\d{2})(\d+)(CE|PE)$/
const N = (x: number) => { const t = 1 / (1 + 0.2316419 * Math.abs(x)); const d = 0.3989423 * Math.exp(-x * x / 2); const p = d * t * (0.3193815 + t * (-0.3565638 + t * (1.781478 + t * (-1.821256 + t * 1.330274)))); return x > 0 ? 1 - p : p }
function bs(S: number, K: number, T: number, v: number, call: boolean) {
  if (T <= 0 || v <= 0) return Math.max(0, call ? S - K : K - S)
  const d1 = (Math.log(S / K) + 0.5 * v * v * T) / (v * Math.sqrt(T)), d2 = d1 - v * Math.sqrt(T)
  return call ? S * N(d1) - K * N(d2) : K * N(-d2) - S * N(-d1)
}
function iv(px: number, S: number, K: number, T: number, call: boolean) {
  let lo = 0.01, hi = 3
  for (let i = 0; i < 60; i++) { const m = (lo + hi) / 2; if (bs(S, K, T, m, call) > px) hi = m; else lo = m }
  return (lo + hi) / 2
}

export default function Payoff({ positions, spot }: { positions: any[]; spot: number | null }) {
  const legs = positions.map((p) => { const m = PAT.exec(p.symbol); if (!m) return null
    const exp = new Date(`20${m[2]}-${m[3]}-${m[4]}T15:30:00+05:30`)
    return { K: Number(m[5]), call: m[6] === 'CE', qty: p.qty * (p.side === 'BUY' ? 1 : -1), entry: p.entry, ltp: p.ltp, T: Math.max(0, (exp.getTime() - Date.now()) / (365 * 864e5)) } }).filter(Boolean) as any[]
  if (!legs.length || !spot) return null
  const lo = spot * 0.94, hi = spot * 1.06, n = 120
  const xs = Array.from({ length: n + 1 }, (_, i) => lo + (hi - lo) * i / n)
  const vols = legs.map((l) => iv(l.ltp, spot, l.K, l.T, l.call))
  const atExp = xs.map((S) => legs.reduce((a, l) => a + l.qty * (Math.max(0, l.call ? S - l.K : l.K - S) - l.entry), 0))
  const today = xs.map((S) => legs.reduce((a, l, i) => a + l.qty * (bs(S, l.K, l.T, vols[i], l.call) - l.entry), 0))
  const all = [...atExp, ...today]
  const ymin = Math.min(0, ...all), ymax = Math.max(0, ...all)
  const W = 340, H = 180, pl = 4, pb = 18
  const X = (S: number) => pl + (S - lo) / (hi - lo) * (W - pl * 2)
  const Y = (v: number) => 6 + (ymax - v) / ((ymax - ymin) || 1) * (H - pb - 6)
  const path = (ys: number[]) => ys.map((y, i) => `${i ? 'L' : 'M'}${X(xs[i]).toFixed(1)},${Y(y).toFixed(1)}`).join('')
  const be: number[] = []
  atExp.forEach((y, i) => { if (i && (y >= 0) !== (atExp[i - 1] >= 0)) be.push(xs[i - 1] + (xs[i] - xs[i - 1]) * (0 - atExp[i - 1]) / (y - atExp[i - 1])) })
  const fmt = (v: number) => (Math.abs(v) >= 1e5 ? `${(v / 1e5).toFixed(1)} L` : Math.round(v).toLocaleString('en-IN'))
  return (
    <figure style={{ margin: 0 }}>
      <svg viewBox={`0 0 ${W} ${H}`} width="100%" role="img" aria-label={`Payoff: at expiry ranges ₹${fmt(Math.min(...atExp))} to ₹${fmt(Math.max(...atExp))}; breakevens ${be.map((b) => Math.round(b)).join(', ') || 'none'}`}>
        <line x1={pl} x2={W - pl} y1={Y(0)} y2={Y(0)} stroke="var(--bd)" />
        <line x1={X(spot)} x2={X(spot)} y1={4} y2={H - pb} stroke="var(--mu)" strokeDasharray="3 3" />
        <path d={path(today)} fill="none" stroke="var(--ac)" strokeWidth={1.5} strokeDasharray="5 3" />
        <path d={path(atExp)} fill="none" stroke="var(--tx)" strokeWidth={2} />
        {be.map((b, i) => <g key={i}><circle cx={X(b)} cy={Y(0)} r={3} fill="var(--live)" /><text x={X(b)} y={H - 4} fontSize={10} textAnchor="middle" fill="var(--mu)">{Math.round(b)}</text></g>)}
        <text x={X(spot) + 3} y={12} fontSize={10} fill="var(--mu)">spot {Math.round(spot)}</text>
        <text x={W - pl} y={Y(ymax) + 10} fontSize={10} textAnchor="end" fill="var(--mu)">₹{fmt(ymax)}</text>
        <text x={W - pl} y={Y(ymin) - 3} fontSize={10} textAnchor="end" fill="var(--mu)">₹{fmt(ymin)}</text>
      </svg>
      <figcaption className="small muted">Solid: at expiry · dashed: today (implied vol from each leg's price) · dots: breakevens. Stops are on premium: {positions.map((p) => `${p.symbol.slice(-8)} SL ${p.sl ?? '—'}`).join(' · ')}</figcaption>
    </figure>)
}
