import { useEffect, useState } from 'react'
import { Icon } from '@components/common/Icon'
import { useAgentStore } from '@store/agentStore'

/** 秒 → 人读时长（「42 秒」「12 分」「1 小时 3 分」）。 */
function fmtElapsed(seconds: number): string {
  const s = Math.max(0, Math.round(seconds))
  if (s < 60) return `${s} 秒`
  const m = Math.floor(s / 60)
  if (m < 60) return `${m} 分`
  return `${Math.floor(m / 60)} 小时 ${m % 60} 分`
}

/**
 * 常驻目标条（2026-09-30 目标可见化，docs/frontend/22 §7.6）。
 *
 * 目标全程激活期间，固定在输入区上方显示一行「🎯 当前目标：xxx · 第 N 轮」。
 *
 * ── 为什么需要它 ──────────────────────────────────────────────────────
 * 目标模式可能连跑很多轮（`block` → 回环 → 再评估），期间用户很可能去干别的、
 * 或者过了十几分钟才回来看。没有这条，唯一能看出"还处于目标模式"的就是输入区
 * 工具栏末尾那枚小胶囊 —— 而它只说明模式、不说明**目标是啥、跑到第几轮了**。
 * 用户的原话：「目标全程激活期间……」。这条就是那个"一眼可见"。
 *
 * ── 位置与让位关系 ────────────────────────────────────────────────────
 * 放在 `<TaskBoard />` 之后、`.composer-wrap` **之前** —— 即"输入区上方"，但它
 * **不占输入框的位置**，因此与两条让位互不干扰：ask 面板（`chat--asking`，隐整个
 * `.composer-wrap`）与计划操作栏（`chat--plan`，换掉 `.composer`）都只动 composer
 * 区域，目标条照常在上方显示 —— 目标模式与 ask 同时发生时才不至于丢掉目标上下文。
 *
 * ── 零占位（硬约束）───────────────────────────────────────────────────
 * 桶里没有本会话条目 → `return null`：不渲染任何 DOM、不占任何高度。存亡判据在
 * store 的 `goalBarAlive`（**四条来源共用**：广播 / 回放 / 列表重建 / 实时轮次），
 * 组件**不自己判**，只读桶 —— 否则又多出一份会漂移的口径。
 *
 * ── 轮次与时长 ────────────────────────────────────────────────────────
 * `round` 是后端**已完成**的评估次数（`active.iterations`），条上显示 `round + 1`
 * （当前进行/即将进行的那一轮），+1 只由 store 的 `goalBarRoundLabel` 做一次。
 * 时长按 `startedAt` 本地每秒 tick —— 它是"目标开始到现在"，不需要后端推送。
 */
export default function GoalBar(): JSX.Element | null {
  const bar = useAgentStore((s) =>
    s.activeSession ? s.goalBarBySession[s.activeSession] : undefined
  )
  const switchExecMode = useAgentStore((s) => s.switchExecMode)
  /** 执行期不给操作入口（与 `PlanActionBar` 的 `!isSending` 同源纪律）：
   *  后端在 `rt.busy` 时**拒绝** goal 的进入/退出（`GoalController` 无锁），
   *  这里若不设防，用户点 × 只会换来一句 error toast。用禁用 + title 说清
   *  "想结束就先停止本轮"，比让他点一个必然失败的按钮好。 */
  const isSending = useAgentStore((s) => s.isSending)

  // 本地 tick：只有条存在时才跑。**必须立刻校准一次 `now`** —— `useState` 的初值
  // 是组件首次渲染（本组件在无目标时也挂载，只 return null）那一刻取的，等到目标
  // 真的设上来时它已经旧了几秒甚至几分钟，不校准会先显示一个偏小的"已运行"再被
  // 第一秒的 tick 跳一下。
  const [now, setNow] = useState(() => Date.now())
  const tick = !!bar?.startedAt
  useEffect(() => {
    if (!tick) return
    setNow(Date.now())
    const id = window.setInterval(() => setNow(Date.now()), 1000)
    return () => window.clearInterval(id)
  }, [tick])

  if (!bar) return null

  const round = bar.round + 1
  const elapsedSec = bar.startedAt ? (now - bar.startedAt * 1000) / 1000 : null

  return (
    <div className="goal-bar" data-goal-round={round}>
      <Icon name="target" size={13} />
      <span className="goal-bar-label">当前目标</span>
      <span className="goal-bar-text" title={bar.condition}>
        {bar.condition}
      </span>
      <span className="goal-bar-round">
        第 {round} 轮
        {elapsedSec !== null && (
          <>
            <span className="goal-bar-dot">·</span>
            已运行 {fmtElapsed(elapsedSec)}
          </>
        )}
      </span>
      <button
        type="button"
        className="goal-bar-close"
        disabled={isSending}
        title={isSending ? '智能体正在执行，停止本轮后可结束目标模式' : '结束目标模式'}
        aria-label="结束目标模式"
        onClick={() => switchExecMode('normal')}
      >
        <Icon name="close" size={11} />
      </button>
    </div>
  )
}
