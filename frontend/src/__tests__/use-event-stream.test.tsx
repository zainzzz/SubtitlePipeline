import { act, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { useEventStream, UseEventStreamOptions } from '../hooks'

class MockEventSource {
  url: string
  onopen: ((ev: Event) => void) | null = null
  onmessage: ((ev: MessageEvent) => void) | null = null
  onerror: ((ev: Event) => void) | null = null
  readyState = 0

  constructor(url: string) {
    MockEventSource.instances.push(this)
    this.url = url
  }

  close = vi.fn(() => {
    this.readyState = 2
    MockEventSource.closed.push(this)
  })

  triggerOpen() {
    this.onopen?.(new Event('open'))
  }
  triggerMessage(type: string, data: unknown) {
    this.onmessage?.({ data: JSON.stringify(data), type } as MessageEvent)
  }
  triggerMessageRaw(raw: string) {
    this.onmessage?.({ data: raw, type: 'message' } as MessageEvent)
  }
  triggerError() {
    this.onerror?.(new Event('error'))
  }

  static instances: MockEventSource[] = []
  static closed: MockEventSource[] = []
  static reset() {
    MockEventSource.instances = []
    MockEventSource.closed = []
  }
}

function renderStreamHook(
  handler: (type: string, data: any) => void,
  options?: UseEventStreamOptions,
) {
  return renderHook(({ h, o }) => useEventStream(h, o), {
    initialProps: { h: handler, o: options },
  })
}

function setHidden(hidden: boolean) {
  Object.defineProperty(document, 'visibilityState', {
    value: hidden ? 'hidden' : 'visible',
    writable: true,
    configurable: true,
  })
  document.dispatchEvent(new Event('visibilitychange'))
}

describe('useEventStream', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    MockEventSource.reset()
    Object.defineProperty(document, 'visibilityState', {
      value: 'visible',
      writable: true,
      configurable: true,
    })
  })
  afterEach(() => {
    vi.useRealTimers()
    vi.restoreAllMocks()
  })

  it('constructs EventSource with the default endpoint on mount', () => {
    const handler = vi.fn()
    renderStreamHook(handler, { EventSourceImpl: MockEventSource as unknown as typeof EventSource })
    expect(MockEventSource.instances).toHaveLength(1)
    expect(MockEventSource.instances[0].url).toBe('/api/events/stream')
  })

  it('reports status "open" after onopen fires', () => {
    const handler = vi.fn()
    const { result } = renderStreamHook(handler, {
      EventSourceImpl: MockEventSource as unknown as typeof EventSource,
    })
    expect(result.current.status).toBe('connecting')
    act(() => {
      MockEventSource.instances[0].triggerOpen()
    })
    expect(result.current.status).toBe('open')
  })

  it('calls handler with parsed JSON on default message event', () => {
    const handler = vi.fn()
    renderStreamHook(handler, { EventSourceImpl: MockEventSource as unknown as typeof EventSource })
    act(() => {
      MockEventSource.instances[0].triggerMessage('message', { foo: 'bar' })
    })
    expect(handler).toHaveBeenCalledWith('message', { foo: 'bar' })
  })

  it('forwards custom event type to handler', () => {
    const handler = vi.fn()
    renderStreamHook(handler, { EventSourceImpl: MockEventSource as unknown as typeof EventSource })
    act(() => {
      MockEventSource.instances[0].triggerMessage('task.updated', { id: 42 })
    })
    expect(handler).toHaveBeenCalledWith('task.updated', { id: 42 })
  })

  it('swallows invalid JSON without calling handler and logs error', () => {
    const handler = vi.fn()
    const errSpy = vi.spyOn(console, 'error').mockImplementation(() => {})
    renderStreamHook(handler, { EventSourceImpl: MockEventSource as unknown as typeof EventSource })
    act(() => {
      MockEventSource.instances[0].triggerMessageRaw('not-json')
    })
    expect(handler).not.toHaveBeenCalled()
    expect(errSpy).toHaveBeenCalled()
  })

  it('reconnects with exponential backoff after an error', () => {
    const handler = vi.fn()
    const { result } = renderStreamHook(handler, {
      EventSourceImpl: MockEventSource as unknown as typeof EventSource,
      initialReconnectDelay: 1000,
      maxReconnectDelay: 30000,
    })
    expect(MockEventSource.instances).toHaveLength(1)

    act(() => {
      MockEventSource.instances[0].triggerError()
    })
    expect(result.current.status).toBe('closed')
    expect(MockEventSource.instances).toHaveLength(1)

    act(() => {
      vi.advanceTimersByTime(1000)
    })
    expect(MockEventSource.instances).toHaveLength(2)
  })

  it('doubles reconnect delay on each failure and caps at maxReconnectDelay', () => {
    const handler = vi.fn()
    renderStreamHook(handler, {
      EventSourceImpl: MockEventSource as unknown as typeof EventSource,
      initialReconnectDelay: 1000,
      maxReconnectDelay: 4000,
    })

    const setTimeoutSpy = vi.spyOn(globalThis, 'setTimeout')

    act(() => {
      MockEventSource.instances[0].triggerError()
    })
    expect(setTimeoutSpy).toHaveBeenLastCalledWith(expect.any(Function), 1000)

    act(() => {
      vi.advanceTimersByTime(1000)
    })
    act(() => {
      MockEventSource.instances[1].triggerError()
    })
    expect(setTimeoutSpy).toHaveBeenLastCalledWith(expect.any(Function), 2000)

    act(() => {
      vi.advanceTimersByTime(2000)
    })
    act(() => {
      MockEventSource.instances[2].triggerError()
    })
    expect(setTimeoutSpy).toHaveBeenLastCalledWith(expect.any(Function), 4000)

    act(() => {
      vi.advanceTimersByTime(4000)
    })
    act(() => {
      MockEventSource.instances[3].triggerError()
    })
    expect(setTimeoutSpy).toHaveBeenLastCalledWith(expect.any(Function), 4000)
  })

  it('closes the connection when the tab becomes hidden', () => {
    const handler = vi.fn()
    renderStreamHook(handler, { EventSourceImpl: MockEventSource as unknown as typeof EventSource })
    const first = MockEventSource.instances[0]

    act(() => {
      setHidden(true)
    })
    expect(first.close).toHaveBeenCalled()
  })

  it('reconnects when the tab becomes visible again', () => {
    const handler = vi.fn()
    renderStreamHook(handler, { EventSourceImpl: MockEventSource as unknown as typeof EventSource })
    expect(MockEventSource.instances).toHaveLength(1)

    act(() => {
      setHidden(true)
    })
    expect(MockEventSource.instances).toHaveLength(1)

    act(() => {
      setHidden(false)
    })
    expect(MockEventSource.instances).toHaveLength(2)
  })

  it('cleans up EventSource and timers on unmount', () => {
    const handler = vi.fn()
    const { unmount } = renderStreamHook(handler, {
      EventSourceImpl: MockEventSource as unknown as typeof EventSource,
    })
    const es = MockEventSource.instances[0]

    unmount()
    expect(es.close).toHaveBeenCalled()
  })

  it('does not construct EventSource when enabled is false', () => {
    const handler = vi.fn()
    renderStreamHook(handler, {
      EventSourceImpl: MockEventSource as unknown as typeof EventSource,
      enabled: false,
    })
    expect(MockEventSource.instances).toHaveLength(0)
  })

  it('uses a custom endpoint path when provided', () => {
    const handler = vi.fn()
    renderStreamHook(handler, {
      EventSourceImpl: MockEventSource as unknown as typeof EventSource,
      endpoint: '/api/custom',
    })
    expect(MockEventSource.instances[0].url).toBe('/api/custom')
  })
})
