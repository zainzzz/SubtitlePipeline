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

// useEventStream — Server-Sent Events (SSE) real-time updates

export type SseHandler = (eventType: string, data: any) => void

export type EventStreamStatus = 'connecting' | 'open' | 'closed'

export interface UseEventStreamOptions {
  /** Path to the SSE endpoint. Default: '/api/events/stream' */
  endpoint?: string
  /** Enable/disable the stream. Default: true */
  enabled?: boolean
  /** Initial reconnect delay in ms. Default: 1000 */
  initialReconnectDelay?: number
  /** Maximum reconnect delay in ms. Default: 30000 */
  maxReconnectDelay?: number
  /** Custom EventSource constructor for testing. */
  EventSourceImpl?: typeof EventSource
}

export interface UseEventStreamResult {
  status: EventStreamStatus
}

/**
 * Subscribes to a Server-Sent Events endpoint and invokes `handler` for every
 * parsed event. Handles auto-reconnect with exponential backoff (capped at
 * `maxReconnectDelay`), pauses when the tab is hidden, and resumes on focus.
 *
 * Falls back gracefully: if `EventSource` is unavailable (e.g. test env without
 * a polyfill), the hook is a no-op and reports `status: 'closed'`.
 */
export function useEventStream(
  handler: SseHandler,
  options?: UseEventStreamOptions,
): UseEventStreamResult {
  const handlerRef = useRef(handler)
  handlerRef.current = handler

  // Resolve config once; options are captured in the effect deps below.
  const endpoint = options?.endpoint ?? '/api/events/stream'
  const enabled = options?.enabled ?? true
  const initialReconnectDelay = options?.initialReconnectDelay ?? 1000
  const maxReconnectDelay = options?.maxReconnectDelay ?? 30000
  const EventSourceImpl =
    options?.EventSourceImpl ?? (typeof EventSource !== 'undefined' ? EventSource : undefined)

  const [status, setStatus] = useState<EventStreamStatus>('closed')
  const reconnectDelayRef = useRef(initialReconnectDelay)
  const reconnectTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const eventSourceRef = useRef<EventSource | null>(null)

  useEffect(() => {
    if (!enabled || !EventSourceImpl) return

    let cancelled = false

    const clearReconnectTimer = () => {
      if (reconnectTimeoutRef.current !== null) {
        clearTimeout(reconnectTimeoutRef.current)
        reconnectTimeoutRef.current = null
      }
    }

    const teardownConnection = () => {
      if (eventSourceRef.current) {
        eventSourceRef.current.close()
        eventSourceRef.current = null
      }
      clearReconnectTimer()
    }

    const connect = () => {
      if (cancelled) return
      teardownConnection()
      setStatus('connecting')

      const es = new EventSourceImpl(endpoint)
      eventSourceRef.current = es

      es.onopen = () => {
        if (cancelled) return
        setStatus('open')
        // Reset backoff on successful connection.
        reconnectDelayRef.current = initialReconnectDelay
      }

      es.onmessage = (event: MessageEvent) => {
        if (cancelled) return
        try {
          const parsed = JSON.parse(event.data)
          handlerRef.current(event.type || 'message', parsed)
        } catch (err) {
          console.error('Failed to parse SSE event:', err)
        }
      }

      es.onerror = () => {
        if (cancelled) return
        if (eventSourceRef.current) {
          eventSourceRef.current.close()
          eventSourceRef.current = null
        }
        setStatus('closed')
        // Exponential backoff, capped.
        const delay = reconnectDelayRef.current
        reconnectDelayRef.current = Math.min(reconnectDelayRef.current * 2, maxReconnectDelay)
        clearReconnectTimer()
        reconnectTimeoutRef.current = setTimeout(connect, delay)
      }
    }

    const handleVisibility = () => {
      if (document.visibilityState === 'hidden') {
        teardownConnection()
        if (!cancelled) setStatus('closed')
      } else if (!cancelled) {
        // Resume — reset backoff so we reconnect promptly on refocus.
        reconnectDelayRef.current = initialReconnectDelay
        connect()
      }
    }

    document.addEventListener('visibilitychange', handleVisibility)
    connect()

    return () => {
      cancelled = true
      document.removeEventListener('visibilitychange', handleVisibility)
      teardownConnection()
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [endpoint, enabled, EventSourceImpl, initialReconnectDelay, maxReconnectDelay])

  return { status }
}
