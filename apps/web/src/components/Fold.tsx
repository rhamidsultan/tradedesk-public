import { useState, type ReactNode } from 'react'

/** B2: collapsible section whose open/closed state is remembered on this device. */
export default function Fold({ id, title, right, children, open: dflt = true }: { id: string; title: ReactNode; right?: ReactNode; children: ReactNode; open?: boolean }) {
  const key = 'td-fold-' + id
  const [open, setOpen] = useState(() => { try { const v = localStorage.getItem(key); return v === null ? dflt : v === '1' } catch { return dflt } })
  const flip = () => { const n = !open; setOpen(n); try { localStorage.setItem(key, n ? '1' : '0') } catch { /* ignore */ } }
  return (
    <section className="card fold">
      <button className="foldhead" aria-expanded={open} onClick={flip}>
        <span className="chev" aria-hidden>{open ? '▾' : '▸'}</span><h2>{title}</h2><span className="foldright">{right}</span>
      </button>
      {open && children}
    </section>)
}
