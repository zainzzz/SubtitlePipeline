import { Component, ErrorInfo, ReactNode } from 'react'

interface Props {
  children: ReactNode
}

interface State {
  hasError: boolean
  message: string
}

// ponytail: 最小错误边界，防止子树抛错导致白屏；复用现有 .alert 样式
export class ErrorBoundary extends Component<Props, State> {
  state: State = { hasError: false, message: '' }

  static getDerivedStateFromError(error: Error): State {
    return { hasError: true, message: error.message }
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error('Unhandled render error:', error, info.componentStack)
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
