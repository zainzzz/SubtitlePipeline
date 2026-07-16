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
    let timer = window.setInterval(tick, intervalMs)

    const handleVisibilityChange = () => {
      window.clearInterval(timer)
      if (document.visibilityState === 'visible') {
        tick()
        timer = window.setInterval(tick, intervalMs)
      }
    }
    document.addEventListener('visibilitychange', handleVisibilityChange)

    return () => {
      window.clearInterval(timer)
      document.removeEventListener('visibilitychange', handleVisibilityChange)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [intervalMs, enabled, ...deps])
}
