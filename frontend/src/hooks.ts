import { DependencyList, useEffect, useRef, useState } from 'react'

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

export type SSEEvent = {
  type: string
  data: Record<string, unknown>
  timestamp: string
}

export function useEventStream(
  url: string,
  onEvent: (event: SSEEvent) => void,
  deps: DependencyList = [],
) {
  const savedCallback = useRef(onEvent)
  savedCallback.current = onEvent
  const [connected, setConnected] = useState(false)

  useEffect(() => {
    const es = new EventSource(url)

    es.onopen = () => setConnected(true)
    es.onerror = () => setConnected(false)

    es.onmessage = (e) => {
      try {
        const parsed = JSON.parse(e.data)
        if (parsed.type === 'connected') return
        savedCallback.current(parsed)
      } catch {
        return
      }
    }

    return () => {
      es.close()
      setConnected(false)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [url, ...deps])

  return { connected }
}
