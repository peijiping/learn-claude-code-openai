import { Icon } from '@components/common/Icon'
import { useAgentStore } from '@store/agentStore'

/** 任务树：本期以后端会话列表近似，点击可切换会话；真实 task/todo 树列增量 */
export default function TaskTree(): JSX.Element {
  const sessions = useAgentStore((s) => s.sessions)
  const activeSession = useAgentStore((s) => s.activeSession)
  const switchSession = useAgentStore((s) => s.switchSession)
  const newSession = useAgentStore((s) => s.newSession)

  return (
    <div className="sidebar-block tasktree">
      <div className="tasktree-header">
        <span className="tasktree-title">任务列表</span>
        <div className="tasktree-actions">
          <button title="新建会话" className="mini-btn" onClick={() => void newSession()}>
            <Icon name="asterisk" size={14} />
          </button>
          <button title="筛选/排序" className="mini-btn">
            <Icon name="filter" size={14} />
          </button>
        </div>
      </div>
      <div className="tasktree-project">{'{ learn-claude-code-main }'}</div>

      <div className="tasktree-list">
        {sessions.length === 0 && <div className="tasktree-empty">暂无任务</div>}
        {sessions.map((s) => (
          <button
            key={s.num}
            className={`tree-node ${s.num === activeSession ? 'active' : ''}`}
            onClick={() => void switchSession(s.num)}
          >
            <Icon name="chevronRight" size={12} className="tree-chevron" />
            <span className="tree-label">session_{s.num}</span>
            <span className="tree-count">{s.message_count}</span>
          </button>
        ))}
      </div>
    </div>
  )
}