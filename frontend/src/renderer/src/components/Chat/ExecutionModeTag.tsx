import { Icon } from '@components/common/Icon'
import type { ExecutionMode, PlanStatus } from '@protocols/agentProtocol'

interface ExecutionModeTagProps {
  /** 当前执行模式。`'normal'` 恒 `return null` —— **零占位**（工具栏尺寸、
   *  DOM 顺序都不受影响，不需要任何"空槽"补偿）。 */
  mode: ExecutionMode
  /** goal 模式的目标条件（进 title 提示；null/空 = 不展示条件） */
  goalCondition?: string | null
  /** plan 模式下的文书状态：ready = 待批准 / approved = 已批准 */
  planStatus?: PlanStatus | null
  /** 点胶囊右侧的 `×` → 回到 normal。
   *  **fire-and-forget**：这里不乐观清态，胶囊由随后的 `execution_mode_changed`
   *  广播撤掉（与权限 chip 同口径：传输丢失时乐观 UI 会说谎）。 */
  onClose: () => void
}

/**
 * 执行模式胶囊（2026-09-25，docs/frontend/22 §6.1）。
 *
 * 位置：输入区工具栏 chip 行、**工作空间 chip 右侧**（`InputBox` 的
 * `.toolbar-left` 末位；其右紧跟 `.toolbar-right`，结构合理）。
 *
 * 与权限盾牌 chip 是**两条独立的轴**（权限 = 允不允许 / 要不要审批；执行模式 =
 * 以什么方式干），所以视觉上刻意不同形态：这里是**胶囊**，权限那边是 chip。
 * 互斥勾选（plan ↔ goal 二选一）由后端强校验，这里只做展示 + 关闭。
 *
 * 视觉/交互照 `.ref-capsule`：`×` **悬浮才浮出**（用 `.att-chip` 的常显 × 会
 * 让胶囊一直"带着一个删除按钮"，视觉噪音大）。
 */
export default function ExecutionModeTag({
  mode,
  goalCondition,
  planStatus,
  onClose
}: ExecutionModeTagProps): JSX.Element | null {
  if (mode === 'normal') return null
  const isPlan = mode === 'plan'
  const label = isPlan ? '计划模式' : '目标模式'
  const hint = isPlan
    ? planStatus === 'ready'
      ? '计划模式：计划文书待批准，修改系统前必须经你确认。点 × 关闭'
      : planStatus === 'approved'
        ? '计划模式：计划已批准，正在执行。点 × 关闭'
        : '计划模式：先出方案，未经批准不得改动系统。点 × 关闭'
    : goalCondition
      ? `目标模式：${goalCondition}。点 × 关闭`
      : '目标模式：朝一个明确条件反复推进直到达成。点 × 关闭'
  return (
    // data-exec-mode：交互验证（CDP）与后续样式扩展都靠它定位，不放 class 里
    <span className={`exec-chip ${isPlan ? 'plan' : 'goal'}`} data-exec-mode={mode} title={hint}>
      <Icon name={isPlan ? 'listTodo' : 'target'} size={13} />
      <span className="exec-chip-label">{label}</span>
      <button
        type="button"
        className="exec-chip-close"
        title="关闭"
        aria-label={`关闭${label}`}
        onClick={onClose}
      >
        <Icon name="close" size={11} />
      </button>
    </span>
  )
}
