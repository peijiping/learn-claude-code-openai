import { useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { Icon } from '@components/common/Icon'

/** 目标条件长度上限 —— 与后端 `agents/goal.py` 的 `MAX_GOAL_LENGTH` **同值**。
 *
 *  这里只是"先于操作的反馈"（超长就禁用确定，不让用户白跑一次往返）；
 *  真正的校验在后端 `GoalController.set_goal`（超限抛 `GoalError`，由桥层转
 *  `error` 信封 toast）。两处都要有：只留前端会被绕过，只留后端体验差。 */
export const MAX_GOAL_LENGTH = 4000

interface GoalInputDialogProps {
  open: boolean
  onCancel: () => void
  onSubmit: (condition: string) => void
}

/**
 * 目标条件输入弹层（2026-09-25，docs/frontend/22 §6.2）。
 *
 * 触发：`+` 菜单点「目标模式」。受控挂载（`open=false` 不渲染任何节点）。
 * 交互：Enter 提交 / Shift+Enter 换行 / Esc 取消。
 *
 * 为什么用 Portal 而不是就地绝对定位：输入区 `.composer` 内有 `overflow` 与
 * 层叠上下文，就地弹层会被裁掉；居中轻量弹层挂到 body 顶层最省心（与
 * `InputBox` 里模型参数悬浮面板同款理由）。
 */
export default function GoalInputDialog({
  open,
  onCancel,
  onSubmit
}: GoalInputDialogProps): JSX.Element | null {
  const [text, setText] = useState('')
  const taRef = useRef<HTMLTextAreaElement>(null)

  // 每次打开都从空白开始：目标是一次性的，残留上一次的条件容易让人误提交
  // 上一个任务的目标（比"少了自动填充"严重得多）。Hooks 必须在提前 return 之前。
  useEffect(() => {
    if (!open) return
    setText('')
    // 挂载 → 聚焦，键盘流一路到底（点菜单 → 打字 → Enter）
    const t = window.setTimeout(() => taRef.current?.focus(), 0)
    return () => window.clearTimeout(t)
  }, [open])

  if (!open) return null
  const trimmed = text.trim()
  const tooLong = text.length > MAX_GOAL_LENGTH
  const canSubmit = trimmed.length > 0 && !tooLong
  const submit = (): void => {
    if (canSubmit) onSubmit(trimmed)
  }

  return createPortal(
    <>
      <div className="goal-dialog-mask" onClick={onCancel} />
      <div className="goal-dialog" role="dialog" aria-modal="true" aria-label="设置目标模式">
        <div className="goal-dialog-title">
          <Icon name="target" size={14} />
          <span>设置完成条件</span>
        </div>
        <div className="goal-dialog-desc">
          模型会朝这个条件反复推进，直到达成、或你手动点胶囊 × 关闭目标模式。
        </div>
        <textarea
          ref={taRef}
          className="goal-dialog-input"
          placeholder="例如：把 tests/ 下所有失败用例修到全绿，且不新增 skip"
          rows={3}
          value={text}
          aria-label="目标完成条件"
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            // stopPropagation：右下栏（RightPanel）在 window 上监听 Esc 关面板，
            // 不拦住的话"取消弹层"会顺手把右栏也关了（Esc 分层，见 19 篇）。
            if (e.key === 'Enter' && !e.shiftKey) {
              e.preventDefault()
              e.stopPropagation()
              submit()
            } else if (e.key === 'Escape') {
              e.preventDefault()
              e.stopPropagation()
              onCancel()
            }
          }}
        />
        {tooLong && (
          <div className="goal-dialog-hint error">
            条件过长（{text.length}/{MAX_GOAL_LENGTH} 字），请精简后再提交
          </div>
        )}
        <div className="goal-dialog-actions">
          <button type="button" className="goal-dialog-btn" onClick={onCancel}>
            取消
          </button>
          <button type="button" className="goal-dialog-btn primary" disabled={!canSubmit} onClick={submit}>
            确定
          </button>
        </div>
      </div>
    </>,
    document.body
  )
}
