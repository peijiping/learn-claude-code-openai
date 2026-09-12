import { Component, type ErrorInfo, type ReactNode } from 'react'

interface Props {
  children: ReactNode
}

interface State {
  error: Error | null
}

/**
 * ErrorBoundary - 兜底错误边界。
 * 渲染树任何未捕获异常不再导致整窗白屏，而是给出可见的错误提示。
 */
export default class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null }

  static getDerivedStateFromError(error: Error): State {
    return { error }
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    console.error('[ErrorBoundary]', error, info.componentStack)
  }

  render(): ReactNode {
    if (this.state.error) {
      return (
        <div
          style={{
            height: '100vh',
            display: 'flex',
            flexDirection: 'column',
            alignItems: 'center',
            justifyContent: 'center',
            gap: 12,
            padding: 24,
            background: '#1f1f1f',
            color: '#e0e0e0',
            fontFamily: '-apple-system, PingFang SC, sans-serif'
          }}
        >
          <div style={{ fontSize: 16, fontWeight: 600 }}>界面出错了</div>
          <div style={{ fontSize: 13, color: '#f08080', fontFamily: 'Menlo, monospace', textAlign: 'center' }}>
            {this.state.error.message}
          </div>
          <button
            onClick={() => window.location.reload()}
            style={{
              padding: '6px 18px',
              borderRadius: 6,
              border: '1px solid #555',
              background: '#2d2d2d',
              color: '#e0e0e0',
              cursor: 'pointer'
            }}
          >
            重新加载
          </button>
        </div>
      )
    }
    return this.props.children
  }
}
