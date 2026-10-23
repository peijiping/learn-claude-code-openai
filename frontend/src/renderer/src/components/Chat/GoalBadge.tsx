import { Icon } from '@components/common/Icon'
import type { GoalMarker } from '@protocols/agentProtocol'

interface GoalBadgeProps {
  /** `kind === 'instruction'` 的目标标记（挂在用户那条目标指令消息上）。 */
  marker: GoalMarker
}

/**
 * 「已设为执行目标」徽标（2026-09-30 目标可见化，docs/frontend/22 §7.6）。
 *
 * 挂在那条**被设为目标**的用户指令消息的气泡头部，回答用户的原话诉求：
 * 「用户被设为目标的指令消息要标出来，已设为执行目标」。
 *
 * ── 为什么是徽标、不是卡片 ────────────────────────────────────────────
 * 这条消息**本身仍是用户说的话**，形态不该被改写 —— 改写它等于抹掉"这是我发的"。
 * 目标设定这件事的完整交代由紧随其后的「目标已设定」卡片承担（`GoalCheckCard`
 * 的 `set` 分支），徽标只负责"这条就是那条指令"这一件事，所以它很轻。
 *
 * ── 条件原文放 title，不铺在气泡里 ────────────────────────────────────
 * 条件通常**就等于这条消息的正文**（「首条指令即目标」，§2.6），再铺一遍是重复。
 * 例外是带附件 / 引用时正文与条件不完全一致，或显式传过 `exec_condition`
 * （旧客户端的入口）—— 那时 title 里的原文才有额外信息量。
 *
 * ── 乐观态（`pending`）────────────────────────────────────────────────
 * 后端要等 `chat` 到达才算得出"这条是目标指令"，所以徽标是**前端乐观**打上的
 * （见 store 的 `settleGoalBadges`）。`pending` 为真时说明还没被确认 —— 视觉上
 * 不做区分（区分了反而让"正在确认"这种毫秒级状态变成噪音），但 `data-goal-pending`
 * 留给交互验证与将来可能的弱化样式用。
 */
export default function GoalBadge({ marker }: GoalBadgeProps): JSX.Element {
  const condition = (marker.condition ?? '').trim()
  return (
    <span
      className="goal-badge"
      data-goal-pending={marker.pending ? '1' : undefined}
      title={condition ? `执行目标：${condition}` : '已设为执行目标'}
    >
      <Icon name="target" size={11} />
      <span className="goal-badge-label">已设为执行目标</span>
    </span>
  )
}
