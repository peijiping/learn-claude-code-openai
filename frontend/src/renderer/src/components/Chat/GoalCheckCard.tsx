import { Icon } from '@components/common/Icon'
import type { GoalAction, GoalMarker } from '@protocols/agentProtocol'

/** 结论的中文文案 + 视觉档位。`data-goal-action` 供交互验证与配色定位。 */
const ACTION_LABEL: Record<GoalAction, { text: string; tone: string }> = {
  block: { text: '未达成', tone: 'warn' },
  achieved: { text: '已达成', tone: 'ok' },
  failed: { text: '无法完成', tone: 'bad' },
  limit: { text: '已达连续轮次上限', tone: 'warn' },
  error: { text: '评估出错', tone: 'bad' },
  defer: { text: '暂缓判定', tone: 'muted' }
}

/** 秒 → 人读时长（「90 秒」「12 分 5 秒」「1 小时 3 分」）。 */
function fmtDuration(seconds: number): string {
  const s = Math.max(0, Math.round(seconds))
  if (s < 60) return `${s} 秒`
  const m = Math.floor(s / 60)
  if (m < 60) return `${m} 分 ${s % 60} 秒`
  return `${Math.floor(m / 60)} 小时 ${m % 60} 分`
}

/** token 数：≥10000 用 k 缩写（与 MessageItem 的 fmtTokens 同口径）。 */
function fmtTokens(n: number): string {
  if (n >= 10000) {
    const k = n / 1000
    return `${k >= 100 ? Math.round(k) : k.toFixed(1).replace(/\.0$/, '')}k`
  }
  return n.toLocaleString()
}

/** `at`（秒级 ISO 本地时间）→ `HH:MM`；解析不出就不显示。 */
function fmtClock(at?: string): string {
  if (!at) return ''
  const d = new Date(at)
  if (Number.isNaN(d.getTime())) return ''
  const p = (n: number): string => String(n).padStart(2, '0')
  return `${p(d.getHours())}:${p(d.getMinutes())}`
}

interface GoalCheckCardProps {
  /** `kind === 'check'`（每轮裁决结果）或 `kind === 'set'`（目标已设定）。 */
  marker?: GoalMarker
}

/**
 * 目标卡片（2026-09-30 目标可见化，docs/frontend/22 §7.6）。
 *
 * 承担两件事，按 `kind` 分流：
 *  - `check` 每一轮 Stop 裁决的结果 —— 用户的原话诉求：「目标的每一轮执行完之后
 *    的目标检查结果也要输出到对话界面上……要不然用户也感知不到每轮执行完的偏差
 *    和结果是什么」；
 *  - `set`  目标设定 —— 后端 `_append_goal_set_message` 落的那条 `[Goal set]`
 *    消息，正文是给模型看的英文指令。给用户看不该是那段英文，转成本卡片。
 *
 * ── 实时与回放共用这一个组件 ──────────────────────────────────────────
 * 实时由 `goal_check` 信封 append 一条 `role: 'goal_check'` 消息，回放由
 * `_history_to_ui` + `historyToMessage` 从 jsonl 还原**同一条消息对象**（`goal`
 * 标记的形状一致，都由后端 `GoalState.snapshot()` 组装）—— 所以这里不做任何
 * "我是实时还是回放"的分支。
 *
 * ── 为什么每轮都出、不做折叠 ──────────────────────────────────────────
 * `block`（未达成）在目标模式里是**常态**：一轮没做完就被打回重做。用户要看的
 * 正是"这轮偏在哪"，所以理由必须直接可见、按时间顺序排在对话流里。真正需要
 * 降噪的是"目标已达成"那种一锤定音的消息 —— 但它本来就只出现一次。
 *
 * ── 轮次口径 ────────────────────────────────────────────────────────
 * `round` 是后端 `active.iterations`（**已完成的评估次数**）。卡片显示的就是它；
 * 常驻目标条显示的是 `round + 1`（当前进行/即将进行的一轮）—— 两者不同义，
 * 见 store 的 `goalBarRoundLabel`。
 */
export default function GoalCheckCard({ marker }: GoalCheckCardProps): JSX.Element | null {
  if (!marker) return null

  if (marker.kind === 'set') {
    const condition = (marker.condition ?? '').trim()
    return (
      <div className="goal-check" data-goal-action="set">
        <div className="goal-check-head">
          <Icon name="target" size={13} />
          <span className="goal-check-title">目标已设定</span>
          <span className="goal-check-badge" data-tone="ok">
            执行中
          </span>
        </div>
        {condition && <div className="goal-check-reason">{condition}</div>}
        <div className="goal-check-meta">
          <span>每轮结束时会自动检查是否达成</span>
        </div>
      </div>
    )
  }

  const action = marker.action ?? 'block'
  const info = ACTION_LABEL[action] ?? ACTION_LABEL.block
  const clock = fmtClock(marker.at)
  return (
    <div className="goal-check" data-goal-action={action}>
      <div className="goal-check-head">
        <Icon name="target" size={13} />
        <span className="goal-check-title">目标检查</span>
        <span className="goal-check-badge" data-tone={info.tone}>
          {info.text}
        </span>
        {clock && <span className="goal-check-time">{clock}</span>}
      </div>
      {/* 偏差理由：评估器给的原文。block 是"还差什么"，achieved/failed 是"依据是什么" */}
      {marker.reason && <div className="goal-check-reason">{marker.reason}</div>}
      <div className="goal-check-meta">
        <span>第 {marker.round ?? 0} 轮</span>
        <span className="goal-check-dot">·</span>
        <span>用时 {fmtDuration(marker.elapsed ?? 0)}</span>
        <span className="goal-check-dot">·</span>
        <span>目标期间 {fmtTokens(marker.tokens ?? 0)} tokens</span>
      </div>
    </div>
  )
}
