import { useEffect, useState } from 'react'
import { Icon } from '@components/common/Icon'
import { useAgentStore } from '@store/agentStore'
import type { TaskItem } from '@protocols/agentProtocol'

/** 任务面板：固定在输入框上方，展示当前会话「正在执行的那一组任务」的实时进度。
 *
 * 数据源是后端推来的**整份快照**（`task_board` 事件，幂等替换，不做增量），
 * 所以本组件不需要任何增量合并逻辑 —— 每次事件重渲染即可。
 *
 * 交互约定（需求）：
 * - 有未完成任务组时才出现；固定高度（超出滚动）
 * - 可收起 / 展开；**执行完之前不能关闭**（running 态不渲染任何关闭入口）
 * - 全部完成后自动收起，并出现「关闭」
 *
 * 已移除「继续执行」按钮（2026-09-16，用户拍板）：面板在有活 + 没人跑时不再提供续跑入口，
 * 用户直接在输入框发一条消息（如「请继续」）即可续跑 —— 后端 `_sync_task_board()` 的尾部注入
 * 与 `release_stale_in_progress()` 归一都与「谁触发的这一轮」无关，手动发消息完全等效。
 * 原按钮还踩了个样式坑：复用了侧边栏的 `.mini-btn`（固定 24×24 纯图标按钮），
 * 塞进 4 个汉字后被挤成竖排并溢出面板，即用户看到的"跑位"。若将来要恢复，
 * 必须另起一个自适应宽度 + `white-space: nowrap` 的类，不要复用 `.mini-btn`。
 */

/** 状态 → 展示样式/文案。`blocked` 是后端**派生**出来的状态（依赖未完成），
 *  不落盘，与落盘的 status 分开对待。 */
const STATUS_META: Record<string, { cls: string; label: string }> = {
  pending: { cls: 'pending', label: '待办' },
  in_progress: { cls: 'running', label: '进行中' },
  blocked: { cls: 'blocked', label: '等待依赖' },
  completed: { cls: 'done', label: '已完成' }
}

function TaskRow({ t }: { t: TaskItem }): JSX.Element {
  const meta = STATUS_META[t.derived_status] ?? STATUS_META.pending
  return (
    <div
      className={`task-row task-row--${meta.cls}`}
      style={{ paddingLeft: 8 + Math.max(0, t.depth) * 14 }}
    >
      <span className={`task-dot task-dot--${meta.cls}`} title={meta.label} />
      <span className="task-row__subject" title={t.subject}>
        {t.subject}
      </span>
      {t.child_total > 0 && (
        <span className="task-row__meta" title="子任务完成度">
          {t.child_completed}/{t.child_total}
        </span>
      )}
      {t.derived_status === 'blocked' && (
        <span className="task-row__meta">等 {t.blockedBy.length} 项</span>
      )}
      {t.owner && (
        <span className="task-row__owner" title={`认领者：${t.owner}`}>
          {t.owner}
        </span>
      )}
      {t.result && (
        <span className="task-row__result" title={t.result}>
          {t.result}
        </span>
      )}
    </div>
  )
}

export default function TaskBoard(): JSX.Element | null {
  const activeSession = useAgentStore((s) => s.activeSession)
  const board = useAgentStore((s) =>
    s.activeSession ? s.taskBoardBySession[s.activeSession] : null
  )
  const [collapsed, setCollapsed] = useState(false)
  // 用户点「关闭」后本地忽略的组（换组后自动重新出现）
  const [dismissedGroup, setDismissedGroup] = useState<string | null>(null)

  const groupId = board?.group_id ?? null
  const done = board?.status === 'done'

  // 折叠状态按「组」记忆：快照是整份替换且每轮任务变化都会重推，
  // 不按组重置的话用户手动展开/收起会被反复打断。
  useEffect(() => {
    setCollapsed(false)
  }, [groupId])

  // 全部完成 → 自动收起（但保留面板，用户可能想回看本轮做了什么）
  useEffect(() => {
    if (done) setCollapsed(true)
  }, [done])

  if (!activeSession || !board || dismissedGroup === board.group_id) return null

  const { counts } = board
  const percent = counts.total > 0 ? Math.round((counts.completed / counts.total) * 100) : 0

  // 刻意不在这里做任何「是否需要续跑」的判据（原先用
  // `board.status === 'running' && !runningSessions.includes(sid) && !bgSessions.includes(sid)`
  // 驱动一个「继续执行」按钮，2026-09-16 已连同按钮一起移除）。
  // 续跑完全交给用户手打消息：`_sync_task_board()` 的注入与 `release_stale_in_progress()`
  // 归一都在 `run_turn()` 里，跟这一轮由谁触发无关，所以手动发消息与点按钮等效。

  return (
    <div
      className={`task-card${collapsed ? ' task-card--collapsed' : ''}${
        done ? ' task-card--done' : ''
      }`}
    >
      <div className="task-card__head" onClick={() => setCollapsed((v) => !v)}>
        <Icon name={collapsed ? 'chevronRight' : 'chevronDown'} size={14} />
        <span className="task-card__title">任务进度</span>
        <span className="task-card__count">
          {counts.completed}/{counts.total}
        </span>
        {board.status === 'running' ? (
          <span className="task-card__badge task-card__badge--running">
            <span className="task-card__pulse" />
            执行中
          </span>
        ) : (
          <span className="task-card__badge task-card__badge--done">
            <Icon name="check" size={12} />
            全部完成
          </span>
        )}
        {counts.blocked > 0 && (
          <span className="task-card__meta">等待依赖 {counts.blocked}</span>
        )}
        <span className="task-card__spacer" />
        {/* 关闭入口只在 done 时渲染 —— 需求要求"执行完之前不能关闭" */}
        {done && (
          <button
            className="mini-btn"
            title="关闭"
            onClick={(e) => {
              e.stopPropagation()
              setDismissedGroup(board.group_id)
            }}
          >
            <Icon name="close" size={13} />
          </button>
        )}
      </div>

      <div className="task-card__bar">
        <span className="task-card__bar-fill" style={{ width: `${percent}%` }} />
      </div>

      <div className="task-card__body">
        {board.tasks.map((t) => (
          <TaskRow key={t.id} t={t} />
        ))}
      </div>
    </div>
  )
}
