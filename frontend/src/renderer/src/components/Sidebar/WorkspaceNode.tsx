import { useState } from 'react'
import type { MouseEvent } from 'react'
import { Icon } from '@components/common/Icon'
import SessionMenu from './SessionMenu'
import SessionTooltip from './SessionTooltip'
import type { ProjectMeta, SessionMeta } from '@protocols/agentProtocol'
import {
  SESSION_PREVIEW_LIMIT,
  sessionDisplayName,
  sessionProjectId,
  useAgentStore
} from '@store/agentStore'

interface WorkspaceNodeProps {
  project: ProjectMeta
  /** 该空间的会话（已按最后修改时间倒序，由后端 `sessions` 信封保证） */
  sessions: SessionMeta[]
  /** 打开空间级右键菜单（坐标 + 空间） */
  onContextMenu: (e: MouseEvent, project: ProjectMeta) => void
  /** 是否处于行内重命名态（状态由父级 WorkspaceTree 持有，一次只重命名一个） */
  renaming?: boolean
  onRenameCommit?: (name: string) => void
  onRenameCancel?: () => void
  draft?: string
  onDraftChange?: (v: string) => void
}

/**
 * 侧边栏的工作空间节点：文件夹图标 + 名称 + 折叠箭头 + 行内「+」，下面挂该空间的会话列表。
 *
 * 四条交互约定（见 docs/frontend/11）：
 * - 点整行 = 展开/折叠（不切换会话，避免误触把正在跑的任务切走）；
 * - 行内「+」= **在该空间新建任务**（首条消息带该空间的 project_id）；
 * - 会话超过 15 条默认折叠，末尾给「展开全部 N 条」入口（否则旧会话永远看不到）；
 * - 空间名可双击/由右键菜单进入行内重命名（默认空间除外，父级已拦）。
 */
export default function WorkspaceNode({
  project,
  sessions,
  onContextMenu,
  renaming,
  onRenameCommit,
  onRenameCancel,
  draft,
  onDraftChange
}: WorkspaceNodeProps): JSX.Element {
  const activeSession = useAgentStore((s) => s.activeSession)
  const isSending = useAgentStore((s) => s.isSending)
  const runningSessions = useAgentStore((s) => s.runningSessions)
  const bgSessions = useAgentStore((s) => s.bgSessions)
  const expandedMap = useAgentStore((s) => s.expandedProjects)
  const previewMap = useAgentStore((s) => s.previewExpanded)
  const activeProject = useAgentStore((s) => s.activeProject)
  const pendingProjectId = useAgentStore((s) => s.pendingProjectId)
  const toggleProject = useAgentStore((s) => s.toggleProject)
  const toggleSessionPreview = useAgentStore((s) => s.toggleSessionPreview)
  const switchSession = useAgentStore((s) => s.switchSession)
  const newSession = useAgentStore((s) => s.newSession)
  const renameSession = useAgentStore((s) => s.renameSession)
  const trashSession = useAgentStore((s) => s.trashSession)

  // 折叠态默认"展开"（未记录即展开；折叠过才有 false）
  const expanded = expandedMap[project.id] !== false
  const showAll = previewMap[project.id] === true
  const visible = showAll ? sessions : sessions.slice(0, SESSION_PREVIEW_LIMIT)
  const hidden = sessions.length - visible.length

  const [menu, setMenu] = useState<{ x: number; y: number; s: SessionMeta } | null>(null)
  const [tip, setTip] = useState<{ x: number; y: number; s: SessionMeta } | null>(null)
  // 会话行内重命名态（与空间级重命名 props 区分命名，避免混淆）
  const [renamingSession, setRenamingSession] = useState<string | null>(null)
  const [sessionDraft, setSessionDraft] = useState('')

  const openMenu = (e: MouseEvent, s: SessionMeta): void => {
    e.preventDefault()
    e.stopPropagation()
    setTip(null)
    setMenu({ x: e.clientX, y: e.clientY, s })
  }

  /** 悬停会话行：信息卡定位于该行右缘右侧（菜单/右键不触发） */
  const showTip = (e: MouseEvent, s: SessionMeta): void => {
    if (menu) return
    const r = e.currentTarget.getBoundingClientRect()
    setTip({ x: r.right + 10, y: r.top - 4, s })
  }

  const startRename = (s: SessionMeta): void => {
    setRenamingSession(s.id)
    setSessionDraft(s.title?.trim() || `session_${s.id}`)
  }

  const commitRename = (): void => {
    if (renamingSession !== null) {
      const t = sessionDraft.trim()
      const current = sessions.find((x) => x.id === renamingSession)
      const displayName = current ? sessionDisplayName(current) : ''
      // 空值或与现显示名相同不提交
      if (t && t !== displayName) void renameSession(renamingSession, t)
    }
    setRenamingSession(null)
  }

  // 空间级状态上浮：折叠时也要能看出"这个空间里有活在跑 / 有未读"
  const runningCount = sessions.filter(
    (s) => runningSessions.includes(s.id) || bgSessions.includes(s.id)
  ).length
  const unreadCount = sessions.filter((s) => s.unread).length
  const isActiveSpace = project.id === activeProject || project.id === pendingProjectId
  // 路径失效（目录被删/改名/移动硬盘未挂载）：禁止在该空间新建任务
  const disabled = !project.exists

  return (
    <div className="ws-group">
      <div
        className={`ws-node ${isActiveSpace ? 'active' : ''} ${disabled ? 'missing' : ''} ${renaming ? 'renaming' : ''}`}
        title={project.path ? `${project.name}\n${project.path}` : project.name}
        onClick={() => {
          if (!renaming) toggleProject(project.id)
        }}
        onContextMenu={(e) => onContextMenu(e, project)}
      >
        <span className="ws-chevron">
          <Icon name={expanded ? 'chevronDown' : 'chevronRight'} size={12} />
        </span>
        <span className="ws-folder">
          <Icon name="folder" size={14} />
        </span>
        {renaming ? (
          <input
            className="ws-rename-input"
            value={draft ?? ''}
            autoFocus
            onClick={(e) => e.stopPropagation()}
            onChange={(e) => onDraftChange?.(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') onRenameCommit?.((draft ?? '').trim())
              else if (e.key === 'Escape') onRenameCancel?.()
            }}
            onBlur={() => onRenameCommit?.((draft ?? '').trim())}
          />
        ) : (
          <span className={`ws-name ${project.system ? 'system' : ''}`}>{project.name}</span>
        )}
        {disabled && <span className="ws-badge missing" title="目录不可用">!</span>}
        {!disabled && runningCount > 0 && (
          <span className="ws-dot running" title={`${runningCount} 个会话执行中`} />
        )}
        {unreadCount > 0 && (
          <span className="ws-badge unread" title={`${unreadCount} 条未读`}>
            {unreadCount > 99 ? '99+' : unreadCount}
          </span>
        )}
        {!renaming && (
          <button
            title={disabled ? '目录不可用，无法新建任务' : `在「${project.name}」中新建任务`}
            className="ws-add mini-btn"
            disabled={disabled}
            onClick={(e) => {
              e.stopPropagation()
              void newSession(project.id)
            }}
          >
            <Icon name="plus" size={14} />
          </button>
        )}
      </div>

      {expanded && (
        <div className="ws-sessions">
          {sessions.length === 0 && <div className="ws-empty">暂无任务</div>}
          {visible.map((s) => (
            <div
              key={s.id}
              className={`tree-node ${s.id === activeSession ? 'active' : ''}`}
              onClick={() => {
                if (renamingSession === null) void switchSession(s.id)
              }}
              onMouseEnter={(e) => showTip(e, s)}
              onMouseLeave={() => setTip(null)}
              onContextMenu={(e) => openMenu(e, s)}
            >
              {runningSessions.includes(s.id) ? (
                <span className="tree-dot running" title="执行中" />
              ) : bgSessions.includes(s.id) ? (
                <span className="tree-dot running" title="后台任务执行中" />
              ) : (
                <span
                  className={`tree-dot ${s.unread ? 'unread' : ''}`}
                  title={s.unread ? '有新消息，未读' : '已读'}
                />
              )}
              {renamingSession === s.id ? (
                <input
                  className="tree-rename-input"
                  value={sessionDraft}
                  autoFocus
                  onClick={(e) => e.stopPropagation()}
                  onChange={(e) => setSessionDraft(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter') commitRename()
                    else if (e.key === 'Escape') setRenamingSession(null)
                  }}
                  onBlur={commitRename}
                />
              ) : (
                <span className="tree-label" title={sessionDisplayName(s)}>
                  {sessionDisplayName(s)}
                </span>
              )}
              {renamingSession !== s.id && (
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

          {hidden > 0 && (
            <button
              className="ws-more-sessions"
              onClick={(e) => {
                e.stopPropagation()
                toggleSessionPreview(project.id)
              }}
            >
              展开全部 {sessions.length} 条
            </button>
          )}
          {hidden === 0 && showAll && sessions.length > SESSION_PREVIEW_LIMIT && (
            <button
              className="ws-more-sessions"
              onClick={(e) => {
                e.stopPropagation()
                toggleSessionPreview(project.id)
              }}
            >
              收起
            </button>
          )}
        </div>
      )}

      {tip && <SessionTooltip x={tip.x} y={tip.y} session={tip.s} />}

      {menu && (
        <SessionMenu
          x={menu.x}
          y={menu.y}
          disabled={isSending}
          onRename={() => startRename(menu.s)}
          onDelete={() => void trashSession(menu.s.id)}
          onClose={() => setMenu(null)}
        />
      )}
    </div>
  )
}

/** 该空间下应展示的会话（保持后端给出的顺序：最后修改时间倒序）。
 *  独立导出便于复用时口径一致（前端不再二次排序，避免打乱组内顺序）。 */
export function sessionsOf(sessions: SessionMeta[], projectId: string): SessionMeta[] {
  return sessions.filter((s) => sessionProjectId(s) === projectId)
}
