import { useState } from 'react'
import { Icon } from '@components/common/Icon'
import type { AskUserMsg } from '@store/agentStore'

/** 消息下的**只读小结块**：模型提问 `ask_user` 的落档展示（作答后留在原地）。
 *
 *  为什么是只读：提问的实时交互由输入框上方的面板承载（一题一屏）；
 *  一旦结算，消息里只留一条"当时问了什么、用户选了什么"的凭证 ——
 *  与普通消息文本一样是**历史**，不能再改（改了就与回填给模型的 tool_result
 *  不一致了，模型和用户看到的两份事实会分叉）。
 *
 *  **正文原样展示 `resultText`，前端不做任何解析**：这份文本由后端 broker 生成，
 *  与回填给模型的 `tool_result` 逐字节相同（实时路径给 `ask_resolved.result_text`，
 *  回放路径给配对上的 tool 行 content）—— 所以"实时看到的"与"重开会话看到的"
 *  必然一致，不存在两套渲染逻辑走偏的可能。
 *
 *  `status === 'pending'`（实时在途）时**不渲染**：此刻面板正承担交互，
 *  再在消息下挂一个空小结就是重复表达（由调用方过滤，见 MessageItem）。
 */

const STATUS_META: Record<string, { cls: string; label: string; icon: string }> = {
  answered: { cls: 'answered', label: '已确认选项', icon: 'check' },
  cancelled: { cls: 'cancelled', label: '已取消提问', icon: 'close' },
  stopped: { cls: 'stopped', label: '提问已中断', icon: 'stop' },
  incomplete: { cls: 'incomplete', label: '提问未完成', icon: 'clock' }
}

export default function AskUserBlock({ block }: { block: AskUserMsg }): JSX.Element | null {
  const [expanded, setExpanded] = useState(true)
  const meta = STATUS_META[block.status] ?? STATUS_META.answered
  const lines = block.resultText ? block.resultText.split('\n').length : 0
  // 未完成的提问没有任何结果文本，展开也没有内容 —— 直接收起
  const collapsible = lines > 0

  return (
    <div className={`ask-block ask-block--${meta.cls}`}>
      <button
        className="ask-block__head"
        onClick={() => collapsible && setExpanded((v) => !v)}
        // 没有正文时别把标题行做成"可点但没反应"的假按钮
        style={{ cursor: collapsible ? 'pointer' : 'default' }}
      >
        <span className="ask-block__icon">
          <Icon name={meta.icon} size={12} />
        </span>
        <span className="ask-block__title">{meta.label}</span>
        {block.questions.length > 0 && (
          <span className="ask-block__meta">{block.questions.length} 个问题</span>
        )}
        <span className="ask-block__spacer" />
        {collapsible && (
          <Icon name="chevronRight" size={12} className={expanded ? 'rot' : ''} />
        )}
      </button>
      {expanded && block.resultText && <div className="ask-block__body">{block.resultText}</div>}
      {!block.resultText && (
        <div className="ask-block__empty">
          本次提问没有收到作答（会话在提问等待期间被结束或进程退出）。
        </div>
      )}
    </div>
  )
}
