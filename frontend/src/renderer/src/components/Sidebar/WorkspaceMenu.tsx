import { useEffect, useRef, useState } from 'react'
import { Icon } from '@components/common/Icon'
import type { ProjectMeta } from '@protocols/agentProtocol'

interface WorkspaceMenuProps {
  /** 视口坐标（右键光标位置），fixed 定位 */
  x: number
  y: number
  project: ProjectMeta
  /** 该空间下的会话数（删除前要让人知道会连带删掉什么） */
  sessionCount: number
  onRename: () => void
  onRemove: () => void
  onReveal: () => void
  onClose: () => void
}

/** 二次确认的窗口时长（与设置页回收站一致：3 秒内没点第二次就撤回） */
const ARM_TIMEOUT_MS = 3000

/**
 * 工作空间项右键菜单：重命名 / 删除 / 在 Finder 中打开。
 *
 * **删除要二次确认**（工作空间删除没有回收站）：第一次点击把按钮变成
 * "确认删除？"，3 秒内再点一次才真删 —— 与「设置 → 回收站」的
 * arm-confirm 交互同款，避免误触把一整空间的会话/任务/记忆删掉。
 * 同时把"只删元数据、真实目录保留"写在菜单里，让人知道删的是什么。
 *
 * 默认空间（`system`）不提供重命名/删除（调用方不会为它打开菜单，这里再兜一层）；
 * 没有真实目录（`path` 为空）就没有"在 Finder 中打开"这一项。
 */
export default function WorkspaceMenu({
  x,
  y,
  project,
  sessionCount,
  onRename,
  onRemove,
  onReveal,
  onClose
}: WorkspaceMenuProps): JSX.Element {
  const ref = useRef<HTMLDivElement>(null)
  const [armed, setArmed] = useState(false)
  const timer = useRef<number | undefined>(undefined)

  useEffect(() => () => window.clearTimeout(timer.current), [])

  useEffect(() => {
    const onDocMouseDown = (e: MouseEvent): void => {
      if (ref.current && !ref.current.contains(e.target as Node)) onClose()
    }
    const onKey = (e: KeyboardEvent): void => {
      if (e.key === 'Escape') onClose()
    }
    document.addEventListener('mousedown', onDocMouseDown)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onDocMouseDown)
      document.removeEventListener('keydown', onKey)
    }
  }, [onClose])

  const arm = (): void => {
    window.clearTimeout(timer.current)
    setArmed(true)
    timer.current = window.setTimeout(() => setArmed(false), ARM_TIMEOUT_MS)
  }

  // 靠近视口边缘时收回来，避免菜单被裁剪（三项菜单高度约 120px）
  const left = Math.min(x, window.innerWidth - 200)
  const top = Math.min(y, window.innerHeight - 140)

  return (
    <div className="session-menu ws-menu" style={{ left, top }} ref={ref}>
      {!project.system && (
        <button
          className="session-menu-item"
          onClick={() => {
            onRename()
            onClose()
          }}
        >
          <Icon name="edit" size={14} />
          <span>重命名</span>
        </button>
      )}
      {project.path && (
        <button
          className="session-menu-item"
          onClick={() => {
            onReveal()
            onClose()
          }}
        >
          <Icon name="folder" size={14} />
          <span>在 Finder 中打开</span>
        </button>
      )}
      {!project.system && (
        <>
          <div className="ws-menu-sep" />
          <button
            className={`session-menu-item danger ${armed ? 'armed' : ''}`}
            onClick={() => {
              if (!armed) {
                arm()
                return
              }
              onRemove()
              onClose()
            }}
          >
            <Icon name="trash" size={14} />
            <span>{armed ? '确认删除？' : '删除工作空间'}</span>
          </button>
          <div className="ws-menu-hint">
            {armed
              ? `将删除该空间的全部元数据${sessionCount ? `（含 ${sessionCount} 条会话）` : ''}，不可恢复`
              : '仅删除元数据与其中的会话记录'}
            <br />
            真实目录 <span className="ws-menu-path">{project.path ?? ''}</span> 会保留
          </div>
        </>
      )}
    </div>
  )
}
