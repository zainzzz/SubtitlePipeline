import { Component, ErrorInfo, ReactNode } from 'react'

interface Props {
  children: ReactNode
}

interface State {
  hasError: boolean
  message: string
}

/**
 * ErrorBoundary — React render-phase error boundary.
 *
 * **Scope (P2-18).**
 *
 * This boundary catches errors thrown **during the React render phase** —
 * i.e. while rendering a descendant component, during a lifecycle method
 * (`componentWillUnmount` is the exception), or inside a class component
 * constructor. When such an error occurs it renders a fallback UI instead of
 * unmounting the whole tree (white screen).
 *
 * It does **NOT** catch:
 *
 * - Errors in event handlers (`onClick`, `onSubmit`, …). These are not part
 *   of rendering and must be handled with `try/catch` inside the handler.
 * - Errors in asynchronous code: `setTimeout`, `setInterval`, `requestIdle`
 *   callbacks, `await`-ed promises, or `fetch` handlers. These run after the
 *   render commit and escape React's reconciliation. The global
 *   `window.error` / `window.unhandledrejection` handlers installed in
 *   `main.tsx` cover this class of failures.
 * - Errors thrown in the boundary itself (this component).
 * - Errors thrown during server-side rendering (not applicable here — Vite SPA).
 * - Errors in Web Workers, service workers, or cross-origin scripts.
 *
 * Because of these limits, the global error handlers in `main.tsx` are a
 * required complement, not an optional extra.
 *
 * When a render error is caught, the boundary logs to `console.error` and
 * dispatches a `app:fatal-error` CustomEvent so the App-level banner can also
 * surface it. The banner is the user-visible signal; the boundary fallback is
 * the recovery affordance.
 */
// ponytail: 最小错误边界，防止子树抛错导致白屏；复用现有 .alert 样式
export class ErrorBoundary extends Component<Props, State> {
  state: State = { hasError: false, message: '' }

  static getDerivedStateFromError(error: Error): State {
    return { hasError: true, message: error.message }
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error('Unhandled render error:', error, info.componentStack)
    if (typeof window !== 'undefined' && typeof CustomEvent !== 'undefined') {
      window.dispatchEvent(
        new CustomEvent('app:fatal-error', {
          detail: { source: 'render', message: error.message, error },
        }),
      )
    }
  }

  render() {
    if (this.state.hasError) {
      return (
        <div className="app-loading">
          <div className="alert error">
            页面渲染出错：{this.state.message}
          </div>
          <button onClick={() => this.setState({ hasError: false, message: '' })}>
            重试
          </button>
        </div>
      )
    }
    return this.props.children
  }
}
