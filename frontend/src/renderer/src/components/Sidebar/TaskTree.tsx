import { useState } from 'react'
import type { MouseEvent } from 'react'
import { Icon } from '@components/common/Icon'
import SessionMenu from './SessionMenu'
import { sessionDisplayName, useAgentStore } from '@store/agentStore'
import type { SessionMeta } from '@protocols/agentProtocol'

/** 任务树：以后端会话列表驱动，点击切换会话；标题来自后端元数据（无标题回退 session_N） */
export default function TaskTree(): JSX.Element {
  const sessions = useAgentStore((s) => s.sessions)
  const activeSession = useAgentStore((s) => s.activeSession)
  const isSending = useAgentStore((s) => s.isSending)
  const runningSessions = useAgentStore((s) => s.runningSessions)
  const bgSessions = useAgentStore((s) => s.bgSessions)
  const completedBg = useAgentStore((s) => s.completedBg)
  const switchSession = useAgentStore((s) => s.switchSession)
  const newSession = useAgentStore((s) => s.newSession)
  const renameSession = useAgentStore((s) => s.renameSession)
  const trashSession = useAgentStore((s) => s.trashSession)

  // 弹出菜单（三点按钮与右键菜单共用）：null = 关闭
  const [menu, setMenu] = useState<{ x: number; y: number; s: SessionMeta } | null>(null)
  // 行内重命名中的会话编号
  const [renaming, setRenaming] = useState<number | null>(null)
  const [draft, setDraft] = useState('')

  const openMenu = (e: MouseEvent, s: SessionMeta): void => {
    e.preventDefault()
    e.stopPropagation()
    setMenu({ x: e.clientX, y: e.clientY, s })
  }

  const startRename = (s: SessionMeta): void => {
    setRenaming(s.num)
    setDraft(s.title?.trim() || `session_${s.num}`)
  }

  const commitRename = (): void => {
    if (renaming !== null) {
      const t = draft.trim()
      const current = sessions.find((x) => x.num === renaming)
      const displayName = current ? sessionDisplayName(current) : ''
      // 空值或与现显示名相同不提交
      if (t && t !== displayName) void renameSession(renaming, t)
    }
    setRenaming(null)
  }

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
      <div className="tasktree-project">默认</div>

      <div className="tasktree-list">
        {sessions.length === 0 && <div className="tasktree-empty">暂无任务</div>}
        {sessions.map((s) => (
          <div
            key={s.num}
            className={`tree-node ${s.num === activeSession ? 'active' : ''}`}
            onClick={() => {
              if (renaming === null) void switchSession(s.num)
            }}
            onContextMenu={(e) => openMenu(e, s)}
          >
            <Icon name="chevronRight" size={12} className="tree-chevron" />
            {runningSessions.includes(s.num) ? (
              <span className="tree-dot running" title="执行中" />
            ) : bgSessions.includes(s.num) ? (
              <span className="tree-dot running" title="后台任务执行中" />
            ) : completedBg.includes(s.num) ? (
              <span className="tree-dot done" title="已完成，点击查看" />
            ) : null}
            {renaming === s.num ? (
              <input
                className="tree-rename-input"
                value={draft}
                autoFocus
                onClick={(e) => e.stopPropagation()}
                onChange={(e) => setDraft(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') commitRename()
                  else if (e.key === 'Escape') setRenaming(null)
                }}
                onBlur={commitRename}
              />
            ) : (
              <span className="tree-label" title={sessionDisplayName(s)}>
                {sessionDisplayName(s)}
              </span>
            )}
            <span className="tree-count">{s.message_count}</span>
            {renaming !== s.num && (
              <button
                title="更多操作"
                className="tree-more mini-btn"
                onClick={(e) => openMenu(e, s)}
              >
                <Icon name="more" size={14} />
              </button>
            )}
          </div>
        ))}
      </div>

      {menu && (
        <SessionMenu
          x={menu.x}
          y={menu.y}
          disabled={isSending}
          onRename={() => startRename(menu.s)}
          onDelete={() => void trashSession(menu.s.num)}
          onClose={() => setMenu(null)}
        />
      )}
    </div>
  )
}
