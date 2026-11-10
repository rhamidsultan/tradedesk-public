import { useState } from 'react'

/** Slide-to-confirm for real-money actions: drag (or use arrow keys) to the end. */
export default function Slide({ label, onDone, color = 'var(--dn)' }: { label: string; onDone: () => void; color?: string }) {
  const [v, setV] = useState(0)
  return (
    <div className="slide" style={{ borderColor: color }}>
      <div className="slidefill" style={{ width: `${v}%`, background: color }} />
      <span className="slidelabel">{v >= 100 ? 'Sending…' : label}</span>
      <input type="range" min={0} max={100} value={v} aria-label={label}
        onChange={(e) => setV(+e.target.value)}
        onPointerUp={() => { if (v >= 95) { setV(100); onDone() } else setV(0) }}
        onKeyUp={(e) => { if (e.key === 'Enter' || v >= 95) { setV(100); onDone() } }} />
    </div>)
}
