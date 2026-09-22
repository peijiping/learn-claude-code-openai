import { useEffect, useState } from 'react'
import { Icon } from '@components/common/Icon'
import type { ApprovalDecision } from '@protocols/agentProtocol'
import { useAgentStore, type ApprovalInteraction } from '@store/agentStore'

/** 消息流中的「权限审批卡片」（2026-09-22 权限管控，docs/frontend/17 §4）。
 *
 *  工具执行前的 PreToolUse 判定为 ask 时弹出，锚定到对应工具折叠条下方；
 *  数据源是 `approvalBySession[activeSession]`（纯 UI 态，不落盘）。
 *
 *  与 ask_user 面板的关键差异（刻意为之）：
 *  - 审批**不整块让位输入区**：只禁用发送按钮（title=「等待权限审批」）——
 *    停止按钮在输入区，审批挂起时必须仍可停止本轮；
 *  - 提交后按钮禁用（submitted），卡片由 `approval_resolved` 广播收掉并折叠成
 *    工具条上的结算徽标（已允许 / 已拒绝 / 已停止）；
 *  - 倒计时归零后三按钮禁用（等后端 timeout 自结算的广播收卡）。
 */

/** 触发原因 chip 文案（§3.6 的展示映射；未知 trigger 原样显示，不猜） */
const TRIGGER_LABELS: Record<string, string> = {
  dangerous_pattern: '危险命令',
  bash_not_allowed: 'Bash 未放行',
  outside_workspace: '工作区外路径',
  mcp_destructive: 'MCP 破坏性操作',
  custom_rule: '自定义规则'
}

/** 工具参数 → 一行命令摘要：bash 显示 command、文件工具显示 path、
 *  glob 显示 pattern，其余（含 MCP 工具）显示 JSON 摘要（后端已截断超长值）。 */
function commandOf(toolName: string, args: Record<string, unknown>): string {
  const pick = (k: string): string => (typeof args[k] === 'string' ? (args[k] as string) : '')
  if (toolName === 'run_bash') return pick('command')
  if (pick('path')) return pick('path')
  if (pick('pattern')) return pick('pattern')
  try {
    const s = JSON.stringify(args)
    return s && s !== '{}' ? s : ''
  } catch {
    return ''
  }
}

/** 剩余秒数 → mm:ss */
function fmtRemain(sec: number): string {
  const s = Math.max(0, Math.floor(sec))
  const m = Math.floor(s / 60)
  return `${String(m).padStart(2, '0')}:${String(s % 60).padStart(2, '0')}`
}

export default function ApprovalCard({ approval }: { approval: ApprovalInteraction }): JSX.Element {
  const answerApproval = useAgentStore((s) => s.answerApproval)
  const [now, setNow] = useState((): number => Date.now())

  // 倒计时心跳：每秒一跳（卡片卸载 / 结算收卡时自然停止）
  useEffect(() => {
    const t = window.setInterval(() => setNow(Date.now()), 1000)
    return () => window.clearInterval(t)
  }, [])

  // 后端 created_at 是秒级时间戳
  const deadline = (approval.createdAt + approval.timeoutSeconds) * 1000
  const remainMs = deadline - now
  const remainSec = Math.ceil(remainMs / 1000)
  const expired = remainMs <= 0
  const urgent = !expired && remainSec <= 60

  // 提交过（fire-and-forget 已发出）或倒计时归零（等后端 timeout 结算）→ 三按钮禁用
  const disabled = approval.submitted || expired
  const cmd = commandOf(approval.toolName, approval.args)

  const answer = (decision: ApprovalDecision): void => {
    if (disabled) return
    answerApproval(approval.requestId, decision)
  }

  return (
    <div className={`approval-card${approval.submitted ? ' submitted' : ''}`}>
      <div className="approval-card__head">
        <span className="approval-card__icon">
          <Icon name="shieldCheck" size={14} />
        </span>
        <span className="approval-card__title">需要你批准</span>
        <span className="approval-card__tool">{approval.toolName}</span>
        {approval.trigger && (
          <span className="approval-card__trigger">
            {TRIGGER_LABELS[approval.trigger] ?? approval.trigger}
          </span>
        )}
        <span className="approval-card__spacer" />
        <span
          className={`approval-card__timer${urgent ? ' urgent' : ''}${expired ? ' expired' : ''}`}
          title="超时未裁决将由后端自动拒绝"
        >
          <Icon name="clock" size={12} />
          {expired ? '结算中…' : fmtRemain(remainSec)}
        </span>
      </div>

      {approval.reason && <div className="approval-card__reason">{approval.reason}</div>}

      {cmd && (
        <div className="approval-card__cmd" title={cmd}>
          {cmd}
        </div>
      )}

      <div className="approval-card__foot">
        <button className="approval-btn" disabled={disabled} onClick={() => answer('allow_once')}>
          允许一次
        </button>
        <button
          className="approval-btn allow-session"
          disabled={disabled}
          onClick={() => answer('allow_session')}
        >
          <span className="approval-btn__label">本次会话内允许</span>
          {approval.sessionScopeHint && (
            <span className="approval-btn__hint">{approval.sessionScopeHint}</span>
          )}
        </button>
        <button className="approval-btn danger" disabled={disabled} onClick={() => answer('deny')}>
          拒绝
        </button>
      </div>

      {approval.submitted && <div className="approval-card__pending">已提交，等待结算…</div>}
    </div>
  )
}
