import { useEffect, useRef } from 'react'

/** B4: poll fast (1 s) while the page is visible and `fast` is true, every 5 s otherwise, paused when hidden.
 *  B3: callers update state in place, so the page keeps its scroll and open sections. */
export function usePoll(fn: () => void, fast: boolean, deps: any[] = []) {
  const ref = useRef(fn)
  ref.current = fn
  useEffect(() => {
    let t: number | undefined
    const tick = () => {
      if (document.visibilityState === 'visible') ref.current()
      t = window.setTimeout(tick, fast ? 1000 : 5000)
    }
    tick()
    const vis = () => { if (document.visibilityState === 'visible') ref.current() }
    document.addEventListener('visibilitychange', vis)
    return () => { clearTimeout(t); document.removeEventListener('visibilitychange', vis) }
  }, [fast, ...deps])
}
