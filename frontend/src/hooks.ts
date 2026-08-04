import { DependencyList, useEffect, useRef } from 'react'

export function usePolling(
  callback: () => void | Promise<void>,
  intervalMs: number,
  deps: DependencyList = [],
  enabled: boolean = true,
) {
  const savedCallback = useRef(callback)
  savedCallback.current = callback
  useEffect(() => {
    if (!enabled) return
    const tick = () => {
      void savedCallback.current()
    }
    tick()
    const timer = window.setInterval(tick, intervalMs)
    return () => window.clearInterval(timer)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [intervalMs, enabled, ...deps])
}
