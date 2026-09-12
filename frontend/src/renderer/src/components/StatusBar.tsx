import { useAgentStore } from '@store/agentStore'

/** 底部状态条：连接状态 / token / 会话编号 */
export default function StatusBar(): JSX.Element {
  const connection = useAgentStore((s) => s.connection)
  const python = useAgentStore((s) => s.python)
  const activeSession = useAgentStore((s) => s.activeSession)
  const isSending = useAgentStore((s) => s.isSending)

  const connLabel: Record<string, { text: string; badge: string }> = {
    connected: { text: '已连接', badge: 'ok' },
    connecting: { text: '连接中…', badge: 'warn' },
    disconnected: { text: '后端未连接', badge: 'off' }
  }
  const pyBadge = python === 'crashed' ? ' · 待重启' : ''

  const conn = connLabel[connection] ?? connLabel.disconnected

  return (
    <footer className="statusbar">
      <div className="statusbar-left">
        <span className={`dot dot-${conn.badge}`} />
        <span className="statusbar-text">
          {conn.text}
          {pyBadge}
        </span>
        {connection === 'disconnected' && (
          <span className="statusbar-link" onClick={() => window.location.reload()}>
            重新连接
          </span>
        )}
      </div>
      <div className="statusbar-right">
        {isSending && <span className="statusbar-text">生成中…</span>}
        {activeSession != null && <span className="statusbar-text">session {activeSession}</span>}
      </div>
    </footer>
  )
}