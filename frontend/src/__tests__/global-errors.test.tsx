import { render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { ErrorBoundary } from '../components/ErrorBoundary'
import { installGlobalErrorHandlers } from '../main'

function ThrowOnRender({ message }: { message: string }): never {
  throw new Error(message)
}

describe('P2-18: ErrorBoundary + global error handlers', () => {
  let cleanup: (() => void) | undefined
  let errorSpy: ReturnType<typeof vi.spyOn>
  let customListener: ((e: Event) => void) | undefined

  beforeEach(() => {
    errorSpy = vi.spyOn(console, 'error').mockImplementation(() => {})
  })

  afterEach(() => {
    if (customListener) {
      window.removeEventListener('app:fatal-error', customListener)
      customListener = undefined
    }
    cleanup?.()
    cleanup = undefined
    errorSpy.mockRestore()
  })

  it('test_error_boundary_catches_render_error: render-phase throw is caught and fallback UI shown', () => {
    render(
      <ErrorBoundary>
        <ThrowOnRender message="boom-render" />
      </ErrorBoundary>,
    )
    expect(screen.getByText(/页面渲染出错/)).toBeInTheDocument()
    expect(screen.getByText(/boom-render/)).toBeInTheDocument()
  })

  it('test_error_boundary_does_not_catch_async: unhandled promise rejection does not trigger boundary fallback', async () => {
    render(
      <ErrorBoundary>
        <button
          onClick={() => {
            void Promise.reject(new Error('async-boom')).catch(() => {
              /* swallow to keep vitest quiet; the assertion is the boundary UI
                 must NOT appear for async errors outside render */
            })
          }}
        >
          trigger
        </button>
      </ErrorBoundary>,
    )
    screen.getByText('trigger').click()
    await new Promise((r) => setTimeout(r, 5))
    expect(screen.queryByText(/页面渲染出错/)).toBeNull()
  })

  it('test_global_error_handler_catches_window_error: installGlobalErrorHandlers logs and dispatches CustomEvent', () => {
    const onFatal = vi.fn()
    const onCustomEvent = vi.fn()
    customListener = onCustomEvent as EventListener
    window.addEventListener('app:fatal-error', customListener)

    cleanup = installGlobalErrorHandlers(onFatal)

    const errorEvent = new ErrorEvent('error', {
      error: new Error('window-boom'),
      message: 'window-boom',
    })
    window.dispatchEvent(errorEvent)

    expect(errorSpy).toHaveBeenCalledWith(
      '[GlobalErrorHandler] Uncaught error:',
      expect.any(Error),
    )
    expect(onFatal).toHaveBeenCalledWith(
      expect.objectContaining({ source: 'window-error', message: 'window-boom' }),
    )
    expect(onCustomEvent).toHaveBeenCalledTimes(1)
    const detail = (onCustomEvent.mock.calls[0][0] as CustomEvent).detail
    expect(detail.source).toBe('window-error')
    expect(detail.message).toBe('window-boom')
  })

  it('test_global_error_handler_catches_unhandled_rejection: PromiseRejectionEvent is logged', () => {
    const win = window as unknown as {
      PromiseRejectionEvent?: typeof PromiseRejectionEvent
    }
    if (typeof win.PromiseRejectionEvent === 'undefined') {
      class PromiseRejectionEventPolyfill extends Event {
        readonly reason: unknown
        readonly promise: Promise<unknown>
        constructor(
          type: string,
          init: { reason?: unknown; promise?: Promise<unknown> },
        ) {
          super(type, { cancelable: true })
          this.reason = init.reason
          this.promise = init.promise ?? Promise.resolve()
        }
      }
      Object.defineProperty(window, 'PromiseRejectionEvent', {
        configurable: true,
        writable: true,
        value: PromiseRejectionEventPolyfill,
      })
    }

    const onFatal = vi.fn()
    cleanup = installGlobalErrorHandlers(onFatal)

    const reason = new Error('rejection-boom')
    const rejectionEvent = new (window as unknown as {
      PromiseRejectionEvent: typeof PromiseRejectionEvent
    }).PromiseRejectionEvent('unhandledrejection', {
      promise: Promise.resolve(),
      reason,
    })
    window.dispatchEvent(rejectionEvent)

    expect(errorSpy).toHaveBeenCalledWith(
      '[GlobalErrorHandler] Unhandled rejection:',
      reason,
    )
    expect(onFatal).toHaveBeenCalledWith(
      expect.objectContaining({
        source: 'unhandled-rejection',
        message: 'rejection-boom',
      }),
    )
  })

  it('test_error_boundary_does_not_block_global_handler: both mechanisms fire independently', async () => {
    const onFatal = vi.fn()
    const onCustomEvent = vi.fn()
    customListener = onCustomEvent as EventListener
    window.addEventListener('app:fatal-error', customListener)

    cleanup = installGlobalErrorHandlers(onFatal)

    render(
      <ErrorBoundary>
        <ThrowOnRender message="boundary-boom" />
      </ErrorBoundary>,
    )

    expect(screen.getByText(/页面渲染出错/)).toBeInTheDocument()

    await waitFor(() => {
      const renderEvents = onCustomEvent.mock.calls.filter(
        ([e]) => (e as CustomEvent).detail?.source === 'render',
      )
      expect(renderEvents.length).toBeGreaterThanOrEqual(1)
    })

    const errorEvent = new ErrorEvent('error', {
      error: new Error('global-after'),
      message: 'global-after',
    })
    window.dispatchEvent(errorEvent)

    expect(onFatal).toHaveBeenCalledWith(
      expect.objectContaining({ source: 'window-error', message: 'global-after' }),
    )
    const windowErrorCalls = onFatal.mock.calls.filter(
      ([e]) => (e as { source?: string }).source === 'window-error' && (e as { message?: string }).message === 'global-after',
    )
    expect(windowErrorCalls.length).toBe(1)
  })
})
