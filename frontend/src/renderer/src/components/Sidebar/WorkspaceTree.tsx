import { useState } from 'react'
import type { MouseEvent } from 'react'
import { Icon } from '@components/common/Icon'
import WorkspaceMenu from './WorkspaceMenu'
import WorkspaceNode, { sessionsOf } from './WorkspaceNode'
import type { ProjectMeta } from '@protocols/agentProtocol'
import { useAgentStore } from '@store/agentStore'

/**
 * 任务列表（工作空间树）：默认工作空间固定第一，其下按「已经打开过的工作空间」排列，
 * 会话列表挂在各自空间节点下。
 *
 * 与改造前的区别（2026-09-18 多工作空间）：以前只有一个写死的「默认」分区，
 * 现在是「default + 用户添加的空间」的树；每个空间可折叠、可右键管理、
 * 行内「+」在该空间新建任务。会话按 `project` 字段分组（后端 `sessions` 信封）。
 */
export default function WorkspaceTree(): JSX.Element {
  const projects = useAgentStore((s) => s.projects)
  const sessions = useAgentStore((s) => s.sessions)
  const newSession = useAgentStore((s) => s.newSession)
  const renameProject = useAgentStore((s) => s.renameProject)
  const removeProject = useAgentStore((s) => s.removeProject)
  const revealProject = useAgentStore((s) => s.revealProject)

  const [menu, setMenu] = useState<{ x: number; y: number; p: ProjectMeta } | null>(null)
  const [renaming, setRenaming] = useState<string | null>(null)
  const [draft, setDraft] = useState('')

  const openMenu = (e: MouseEvent, p: ProjectMeta): void => {
    e.preventDefault()
    e.stopPropagation()
    // 默认空间没有可管理的项（不可重命名/删除，也没有真实目录）→ 不弹菜单
    if (p.system) return
    setMenu({ x: e.clientX, y: e.clientY, p })
  }

  /** 空间列表为空（projects 信封尚未到达）时也要能新建任务：归到 default */
  const empty = projects.length === 0

  return (
    <div className="sidebar-block tasktree">
      <div className="tasktree-header">
        <span className="tasktree-title">任务列表</span>
        <div className="tasktree-actions">
          <button title="新建任务" className="mini-btn" onClick={() => void newSession()}>
            <Icon name="asterisk" size={14} />
          </button>
          <button title="筛选/排序" className="mini-btn">
            <Icon name="filter" size={14} />
          </button>
        </div>
      </div>

      <div className="tasktree-list">
        {empty && <div className="tasktree-empty">暂无工作空间</div>}
        {projects.map((p) => (
          <WorkspaceNode
            key={p.id}
            project={p}
            sessions={sessionsOf(sessions, p.id)}
            onContextMenu={openMenu}
            renaming={renaming === p.id}
            onRenameCommit={(name) => {
              void renameProject(p.id, name)
              setRenaming(null)
            }}
            onRenameCancel={() => setRenaming(null)}
            draft={draft}
            onDraftChange={setDraft}
          />
        ))}
      </div>

      {menu && (
        <WorkspaceMenu
          x={menu.x}
          y={menu.y}
          project={menu.p}
          sessionCount={sessionsOf(sessions, menu.p.id).length}
          onRename={() => {
            setRenaming(menu.p.id)
            setDraft(menu.p.name)
          }}
          onRemove={() => void removeProject(menu.p.id)}
          onReveal={() => void revealProject(menu.p.id)}
          onClose={() => setMenu(null)}
        />
      )}
    </div>
  )
}
