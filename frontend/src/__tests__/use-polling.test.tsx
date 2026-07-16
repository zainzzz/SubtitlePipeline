import { act, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { usePolling } from '../hooks'

function renderPollingHook(
  callback: () => void,
  intervalMs: number = 1000,
  enabled: boolean = true,
) {
  return renderHook(
    ({ cb, ms, en }) => usePolling(cb, ms, [], en),
    {
      initialProps: { cb: callback, ms: intervalMs, en: enabled },
    },
  )
}

describe('usePolling P3-6: visibilitychange', () => {
  beforeEach(() => {
    vi.useFakeTimers()
  })
  afterEach(() => {
    vi.useRealTimers()
    vi.restoreAllMocks()
  })

  it('calls callback immediately on mount when enabled', () => {
    const cb = vi.fn()
    renderPollingHook(cb, 1000, true)
    expect(cb).toHaveBeenCalledTimes(1)
  })

  it('does not call callback when disabled', () => {
    const cb = vi.fn()
    renderPollingHook(cb, 1000, false)
    expect(cb).not.toHaveBeenCalled()
  })

  it('calls callback on interval ticks', () => {
    const cb = vi.fn()
    renderPollingHook(cb, 1000, true)
    expect(cb).toHaveBeenCalledTimes(1)

    act(() => {
      vi.advanceTimersByTime(3000)
    })
    expect(cb).toHaveBeenCalledTimes(4)
  })

  it('pauses polling when document becomes hidden', () => {
    const cb = vi.fn()
    renderPollingHook(cb, 1000, true)
    expect(cb).toHaveBeenCalledTimes(1)

    act(() => {
      vi.advanceTimersByTime(1000)
    })
    const countBeforeHidden = cb.mock.calls.length

    Object.defineProperty(document, 'visibilityState', {
      value: 'hidden',
      writable: true,
      configurable: true,
    })
    document.dispatchEvent(new Event('visibilitychange'))

    act(() => {
      vi.advanceTimersByTime(5000)
    })

    expect(cb.mock.calls.length).toBe(countBeforeHidden)
  })

  it('resumes polling when document becomes visible again', () => {
    const cb = vi.fn()
    renderPollingHook(cb, 1000, true)

    Object.defineProperty(document, 'visibilityState', {
      value: 'hidden',
      writable: true,
      configurable: true,
    })
    act(() => {
      document.dispatchEvent(new Event('visibilitychange'))
      vi.advanceTimersByTime(5000)
    })
    const countAfterHidden = cb.mock.calls.length

    Object.defineProperty(document, 'visibilityState', {
      value: 'visible',
      writable: true,
      configurable: true,
    })
    act(() => {
      document.dispatchEvent(new Event('visibilitychange'))
    })

    expect(cb.mock.calls.length).toBeGreaterThan(countAfterHidden)
  })

  it('fires immediate tick when tab becomes visible after being hidden', () => {
    const cb = vi.fn()
    renderPollingHook(cb, 5000, true)

    vi.clearAllMocks()
    cb.mockClear()

    Object.defineProperty(document, 'visibilityState', {
      value: 'hidden',
      writable: true,
      configurable: true,
    })
    act(() => {
      document.dispatchEvent(new Event('visibilitychange'))
      vi.advanceTimersByTime(10000)
    })

    expect(cb).not.toHaveBeenCalled()

    Object.defineProperty(document, 'visibilityState', {
      value: 'visible',
      writable: true,
      configurable: true,
    })
    act(() => {
      document.dispatchEvent(new Event('visibilitychange'))
    })

    expect(cb).toHaveBeenCalledTimes(1)
  })

  it('cleans up visibilitychange listener on unmount', () => {
    const cb = vi.fn()
    const removeSpy = vi.spyOn(document, 'removeEventListener')
    const { unmount } = renderPollingHook(cb, 1000, true)

    unmount()
    expect(removeSpy).toHaveBeenCalledWith('visibilitychange', expect.any(Function))
  })
})
