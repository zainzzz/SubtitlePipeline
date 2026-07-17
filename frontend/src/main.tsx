import React from 'react'
import ReactDOM from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'
import App from './App'
import { ErrorBoundary } from './components/ErrorBoundary'
import './styles.css'

/**
 * Shape of the custom event dispatched when a fatal error reaches the user.
 * `App.tsx` listens for `app:fatal-error` CustomEvents and shows a dismissible
 * banner.
 */
export interface AppFatalError {
  source: 'render' | 'window-error' | 'unhandled-rejection'
  message: string
  error?: unknown
}

/**
 * Install global `error` and `unhandledrejection` listeners on `window`.
 *
 * React's ErrorBoundary (see `components/ErrorBoundary.tsx`) only catches
 * errors thrown during the render phase — async errors, event-handler errors,
 * and unhandled promise rejections escape it. These handlers close that gap:
 *
 * - `window.error` catches uncaught exceptions outside React's render tree
 *   (event handlers, setTimeout callbacks, etc.).
 * - `window.unhandledrejection` catches promises without a `.catch()`.
 *
 * Each handler logs to `console.error` (preserving the stack via `event.error`
 * when present) and dispatches an `app:fatal-error` CustomEvent so the
 * App-level banner can surface the failure to the user.
 *
 * Exported (and the boot auto-install gated on a `#root` element existing) so
 * unit tests can import the helper without triggering React boot.
 *
 * @returns a cleanup function that removes both listeners.
 */
export function installGlobalErrorHandlers(
  onFatal?: (event: AppFatalError) => void,
): () => void {
  const handleError = (event: ErrorEvent) => {
    const cause = event.error ?? event.message
    const message = event.error instanceof Error ? event.error.message : event.message
    console.error('[GlobalErrorHandler] Uncaught error:', cause)
    const payload: AppFatalError = { source: 'window-error', message: message ?? 'unknown error', error: event.error }
    onFatal?.(payload)
    window.dispatchEvent(new CustomEvent('app:fatal-error', { detail: payload }))
  }

  const handleRejection = (event: PromiseRejectionEvent) => {
    const reason = event.reason
    const message = reason instanceof Error ? reason.message : String(reason)
    console.error('[GlobalErrorHandler] Unhandled rejection:', reason)
    const payload: AppFatalError = { source: 'unhandled-rejection', message, error: reason }
    onFatal?.(payload)
    window.dispatchEvent(new CustomEvent('app:fatal-error', { detail: payload }))
  }

  window.addEventListener('error', handleError)
  window.addEventListener('unhandledrejection', handleRejection)

  return () => {
    window.removeEventListener('error', handleError)
    window.removeEventListener('unhandledrejection', handleRejection)
  }
}

const root = document.getElementById('root')
// Boot guard: only mount React when a #root element exists. This also makes
// the module safe to import from unit tests (jsdom has no #root by default)
// without triggering `createRoot(null)`.
if (root) {
  installGlobalErrorHandlers()
  ReactDOM.createRoot(root).render(
    <React.StrictMode>
      <BrowserRouter>
        <ErrorBoundary>
          <App />
        </ErrorBoundary>
      </BrowserRouter>
    </React.StrictMode>,
  )
}
