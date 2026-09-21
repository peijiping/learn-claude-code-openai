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
 * - 可收起 / 展开；**有人真在跑时不能关闭**（不给关闭入口）
 * - 全部完成后自动收起，并出现「关闭」
 * - **停滞时也出现「关闭」**（2026-09-18 新增）：有未完成项但没有任何 in_progress
 *   （无人认领的残留 / 依赖卡住），此时没有任何东西在跑，若还锁着关闭入口，
 *   用户就被永久困在"执行中"面板里 —— 这正是本次事故的观感
 *
 * 徽标必须以 `board.has_in_progress` 为准，不能只看 `board.status`：
 * `status === 'running'` 只表示"这组活没干完"，里面可能一条 in_progress 都没有
 * （残留未认领项），直接显示「执行中」就是在说假话。三态：
 *   有人跑 → 执行中（脉冲）／有活没人跑 → 待继续（灰）／全干完 → 全部完成（对勾）
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
  // 「有没有人真在跑」：以后端派生字段为准；缺字段（旧版后端）回退用 counts 近似
  const active = !!board && !done && (board.has_in_progress ?? board.counts.in_progress > 0)
  // 有活但没人跑 = 停滞（残留未认领项 / 依赖卡死）：可以关闭，不再把用户锁死
  const stalled = !!board && !done && !active

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
      }${stalled ? ' task-card--stalled' : ''}`}
    >
      <div className="task-card__head" onClick={() => setCollapsed((v) => !v)}>
        <Icon name={collapsed ? 'chevronRight' : 'chevronDown'} size={14} />
        <span className="task-card__title">任务进度</span>
        <span className="task-card__count">
          {counts.completed}/{counts.total}
        </span>
        {active ? (
          <span className="task-card__badge task-card__badge--running">
            <span className="task-card__pulse" />
            执行中
          </span>
        ) : done ? (
          <span className="task-card__badge task-card__badge--done">
            <Icon name="check" size={12} />
            全部完成
          </span>
        ) : (
          <span
            className="task-card__badge task-card__badge--stalled"
            title="有未完成的任务，但当前没有任何任务在执行（发一条消息即可继续）"
          >
            待继续
          </span>
        )}
        {counts.blocked > 0 && (
          <span className="task-card__meta">等待依赖 {counts.blocked}</span>
        )}
        <span className="task-card__spacer" />
        {/* 关闭入口：全部完成，或**没人跑**（停滞）时提供。
            有人真在跑时不给 —— 需求要求"执行完之前不能关闭"，
            但"停滞"不等于"在执行"，那时锁着入口只会把用户永久困住。 */}
        {(done || stalled) && (
          <button
            className="mini-btn"
            title={done ? '关闭' : '关闭（任务仍未完成，可稍后发消息继续）'}
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
