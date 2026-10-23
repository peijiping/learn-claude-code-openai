import { useCallback, useEffect, useRef, useState } from 'react'
import { Icon } from '@components/common/Icon'
import { isPlanDocOpenable, openPlanDocInPanel, useAgentStore, type PlanState } from '@store/agentStore'

/** 四个**固定**选项（2026-09-29 用户拍板）—— 刻意不做成可配置：它们是"拿到计划之后
 *  能做的全部动作"，多一项就是多一条没人维护的分支。 */
type PlanChoice = 'run' | 'revise' | 'other' | 'later'

interface ChoiceSpec {
  key: PlanChoice
  label: string
  desc: string
  /** 需要用户写点什么（就地展开输入框） */
  input: boolean
}

const CHOICES: ChoiceSpec[] = [
  { key: 'run', label: '开始执行', desc: '批准这份计划，计划模式结束并立即开始改代码', input: false },
  { key: 'revise', label: '继续修改', desc: '说一句要改哪里，模型据此重新产出计划', input: true },
  { key: 'other', label: '其他', desc: '自己描述下一步（同样留在计划模式里）', input: true },
  { key: 'later', label: '暂不执行先看方案', desc: '收起本栏、恢复输入框；计划模式仍然生效', input: false }
]

interface PlanActionBarProps {
  plan: PlanState
  /** 「开始执行」：fire-and-forget 发 `plan_approve`。**不乐观转只读** ——
   *  状态由随后的 `execution_mode_changed`（带 `plan_status='approved'`）校正；
   *  后端此时还会自动起一轮执行（忙碌/有待答提问时只落状态并 toast）。 */
  onApprove: () => void
  /** 「继续修改」/「其他」提交：把用户写的文字作为**一条用户消息**发进本会话。
   *  计划模式保持生效（不发 `session_exec_mode`），模型据此重新产出文书（覆盖）。 */
  onSendText: (text: string) => void
  /** **收起本栏**（`ChatPanel` 据此把输入框还回来）。两条触发路径：
   *  - 「暂不执行先看方案」：用户在选项里主动放弃本次决策 —— 计划模式**不受影响**，
   *    重新展开的入口是消息流里计划卡片的「选择操作」；
   *  - **任何一项点了「确定」**（2026-09-29）：决策已作出，本栏立刻让位 ——
   *    "点了确定操作框还杵着、像是没生效"是用户实测报的体验问题。
   *  它只改**本栏是否显示**，不改计划状态（批准与否仍由后端广播校正）。 */
  onDismiss: () => void
}

/**
 * 计划操作栏（`PlanActionBar.tsx`）—— 2026-09-29 二次改版。
 *
 * ── 位置：**输入框的位置**，与自由输入互斥 ────────────────────────────────
 * 上一版把选项放在消息流末尾的计划卡片里，输入框照常可用 —— 于是"接下来怎么做"
 * 有两个入口（卡片里的选项、输入框里的自由打字），用户既可能在输入框里打一半又去
 * 点选项，两边都像"已经说了"。用户拍板的形态是：**待批准期间，操作选择框直接顶替
 * 输入框**（`ChatPanel` 的 `.chat--plan`，InputBox 只隐藏不卸载，草稿不丢）。
 *
 * 因此 `ChatPanel` 的渲染条件是 `plan.status === 'ready'` **且会话空闲**（`!isSending`，
 * 2026-09-29 五次改版）：批准（`approved`）、计划模式退出、**智能体正在跑一轮** —— 三种
 * 情形下本栏都不在，输入框原样回来。
 *
 * ── 收起态（两条入口）────────────────────────────────────────────────
 * 1. 「暂不执行先看方案」（点选项即生效）；
 * 2. **点了「确定」**（2026-09-29 追加）—— 四个选项一律先收栏再执行，见 `submit`。
 *
 * 它**不再**只是"打开右栏 + 留个说明"，因为本栏占着输入框位置 —— 留在原地等于把
 * 用户锁死在四个选项里（连"退出计划模式"的胶囊 ×、模型切换都在被隐藏的输入区）。
 * 语义是：**收起本栏、把输入框还回来**，计划模式照旧生效。想再决策就点消息流里
 * 计划卡片的「选择操作」；而**新一版计划到达**（重新产出 / 换一份文书）会让本栏
 * 自动回来 —— 那一刻正是新的决策点（复位判据在 `ChatPanel`）。
 *
 * ── 为什么现在**不再**自带一粒「停止」（2026-09-29 五次改版）──────────────
 * 上一版自带停止，理由是"占位期间输入区的停止按钮被 `.chat--plan` 一起藏了"。这条前提
 * 现在没了：`ChatPanel` 把 `isSending` 纳入渲染条件（`planBarShown`）—— 本栏**只在会话
 * 空闲时出现**，出现时永远不需要停止；而它不出现的执行期，输入区照常显示（`.composer`
 * 没被藏），那粒停止按钮也照常在。留着这支分支就是死代码，删掉。
 */
export default function PlanActionBar({
  plan, onApprove, onSendText, onDismiss
}: PlanActionBarProps): JSX.Element {
  const lost = !!plan.reason
  const sid = useAgentStore((s) => s.activeSession)
  /** 文书能否进右栏（存量会话的绝对路径不行，判据在 store，与卡片同源）。 */
  const openable = isPlanDocOpenable(plan.path)

  const [choice, setChoice] = useState<PlanChoice | null>(null)
  const [text, setText] = useState('')
  const inputRef = useRef<HTMLInputElement>(null)

  const spec = CHOICES.find((c) => c.key === choice) || null
  const needsText = !!spec?.input
  const trimmed = text.trim()

  // 换一份文书（重新产出 / 切会话换壳）→ 清掉上一条的选择与输入，
  // 否则"上一版的修改意见"会莫名挂在新一版上（与服务端 plan 状态单份覆盖同源）。
  const docKey = `${plan.path}|${plan.status}`
  useEffect(() => {
    setChoice(null)
    setText('')
  }, [docKey])

  /** 选中一个选项。需要输入的选项就地聚焦（**不预填文字** —— 预置提示等于替用户
   *  写好了要说的话，很容易被直接发出去）。 */
  const pick = useCallback(
    (key: PlanChoice) => {
      if (key === 'later') {
        // 立即生效：收起本栏（输入框回来）+ 保证右栏有这份文书可看。
        setChoice(null)
        setText('')
        if (openable) openPlanDocInPanel(sid ?? '', plan.path)
        onDismiss()
        return
      }
      setChoice(key)
      const s = CHOICES.find((c) => c.key === key)
      if (s?.input) setTimeout(() => inputRef.current?.focus(), 0)
    },
    [onDismiss, openable, plan.path, sid]
  )

  const submit = useCallback(() => {
    if (!choice) return
    // ── 「确定」= **先收起本栏，再执行动作**（2026-09-29 用户拍板）────────
    // 上一版只有「继续修改 / 其他」清了选中态，而本栏的渲染条件（`plan.status ===
    // 'ready'`）要等后端广播才变 —— 于是点了「确定」之后，四个选项还原地杵着，
    // 看起来像"没生效"（尤其是「开始执行」：批准与否由广播校正，中间那段只有静止）。
    // 四个选项一律走这条，收栏即把输入框还回来，动作在后台照常发生。
    // （`choice` 只在本地，收栏后本组件卸载，选中态随之消失。）
    const key = choice
    const value = trimmed
    onDismiss()
    if (key === 'run') {
      if (lost) return
      onApprove()
      return
    }
    if (!value) return
    // 「继续修改」/「其他」：作为一条用户消息发出去（不发 session_exec_mode ——
    // 计划模式要保持生效，模型据此重新产出文书）。
    onSendText(value)
  }, [choice, lost, onApprove, onDismiss, onSendText, trimmed])

  const confirmDisabled = choice === 'run' ? lost : needsText ? !trimmed : true

  return (
    <div className="plan-bar" data-plan-status={plan.status}>
      <div className="plan-bar__card">
        <div className="plan-bar__head">
          <span className="plan-bar__icon">
            <Icon name="listTodo" size={14} />
          </span>
          <span className="plan-bar__title">执行计划已就绪</span>
          <span className="plan-bar__hint">正文已在右侧面板打开</span>
          <span className="plan-bar__spacer" />
          {!lost && openable && (
            <button
              type="button"
              className="plan-card-open"
              title="在右侧面板打开计划文档"
              onClick={() => openPlanDocInPanel(sid ?? '', plan.path)}
            >
              <Icon name="externalLink" size={12} />
              打开文档
            </button>
          )}
        </div>

        {lost && (
          // 读不到是**常规降级**（文书被清理 / 空间目录不可用 / 超 512KB 整份拒绝）——
          // 在这里说清原因，「开始执行」随之为禁用（批准一份读不到的文书没有意义）。
          <div className="plan-bar__lost">
            <Icon name="fileText" size={13} />
            <span>{plan.reason}</span>
          </div>
        )}

        <div className="plan-opts" role="radiogroup" aria-label="接下来怎么做">
          {CHOICES.map((c) => {
            const on = choice === c.key
            const disabled = c.key === 'run' && lost
            return (
              <button
                key={c.key}
                type="button"
                role="radio"
                aria-checked={on}
                disabled={disabled}
                title={disabled ? '计划文书读不到，无法批准' : undefined}
                className={`plan-opt${on ? ' plan-opt--on' : ''}`}
                onClick={() => pick(c.key)}
              >
                <span className="plan-opt__mark">{on && <Icon name="check" size={11} />}</span>
                <span className="plan-opt__body">
                  <span className="plan-opt__label">{c.label}</span>
                  <span className="plan-opt__desc">{c.desc}</span>
                </span>
              </button>
            )
          })}
        </div>

        {needsText && (
          <input
            ref={inputRef}
            className="plan-opt-input"
            value={text}
            placeholder={
              choice === 'revise' ? '要调整哪里？例如「第 3 步拆成两轮」…' : '说说你的想法…'
            }
            onChange={(e) => setText(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && !e.nativeEvent.isComposing && trimmed) {
                e.preventDefault()
                submit()
              }
            }}
          />
        )}

        <div className="plan-bar__foot">
          <span className="plan-bar__tip">选一项后点「确定」</span>
          <span className="plan-bar__spacer" />
          <button
            type="button"
            className="plan-bar__btn primary"
            disabled={confirmDisabled}
            onClick={submit}
          >
            确定
          </button>
        </div>
      </div>
    </div>
  )
}
