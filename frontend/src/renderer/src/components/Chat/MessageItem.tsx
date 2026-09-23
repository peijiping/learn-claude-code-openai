import { useEffect, useMemo, useRef, useState, type MouseEvent as ReactMouseEvent } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { Icon } from '@components/common/Icon'
import { renderRefText } from '@lib/refTokens'
import type { TurnModelInfo, UsageStats } from '@protocols/agentProtocol'
import {
  attachmentUrl,
  useAgentStore,
  type ApprovalInteraction,
  type AskUserMsg,
  type Message,
  type SubAgentMsg,
  type ToolCallMsg
} from '@store/agentStore'
import MessageMenu from './MessageMenu'
import AttachmentBar, { isDegraded } from './AttachmentBar'
import AskUserBlock from './AskUserBlock'
import ApprovalCard from './ApprovalCard'
import RefBar from './RefBar'
import RefText from './RefText'
import { useRightPanelStore } from '@store/rightPanelStore'

/** token 数字格式化：≥10000 用 k 缩写（如 45.6k），否则千位逗号 */
function fmtTokens(n: number): string {
  if (n >= 10000) {
    const k = n / 1000
    return `${k >= 100 ? Math.round(k) : k.toFixed(1).replace(/\.0$/, '')}k`
  }
  return n.toLocaleString()
}

/** 缓存命中率 = cached_tokens / prompt_tokens（输入侧）；
 *  provider 未返回缓存信息（cached=0 / prompt=0）时显示 — */
function cachePct(u: UsageStats): string {
  if (!u.cached_tokens || !u.prompt_tokens) return '—'
  return `${Math.round((u.cached_tokens / u.prompt_tokens) * 100)}%`
}

/** 思考强度档位展示映射（与输入区悬浮面板 THINKING_LABELS 一致） */
const THINKING_LABELS: Record<string, string> = {
  low: '轻',
  high: '高',
  very_high: '极高'
}

/** 本轮模型快照 → footer 模型段文本：`deepseek-flash · 128K · 思考 高`。
 *  模型名/窗口都缺时不渲染；思考档位缺省（空串）不显示该节。 */
function modelInfoText(mi: TurnModelInfo): string | null {
  const parts: string[] = []
  if (mi.model_name) parts.push(mi.model_name)
  if (mi.max_context_label) parts.push(mi.max_context_label)
  const strength = THINKING_LABELS[mi.reasoning_effort]
  if (strength) parts.push(`思考 ${strength}`)
  return parts.length ? parts.join(' · ') : null
}

/** 审批结算徽标（2026-09-22 权限管控）：approval_resolved 后旁挂在工具行上。
 *  allowed_* → 绿「已允许」、denied/timeout → 红「已拒绝」、stopped → 灰「已停止」。
 *  回放路径只有拒绝系（后端不落盘允许结局，见 docs/frontend/17 §4.4）。 */
const APPROVAL_BADGE: Record<string, { cls: string; text: string }> = {
  allowed_once: { cls: 'ok', text: '已允许' },
  allowed_session: { cls: 'ok', text: '已允许' },
  denied: { cls: 'deny', text: '已拒绝' },
  timeout: { cls: 'deny', text: '已拒绝' },
  stopped: { cls: 'stop', text: '已停止' }
}

function ToolCallBar({ tool }: { tool: ToolCallMsg }): JSX.Element {
  const badge = tool.approval ? APPROVAL_BADGE[tool.approval.decision] : undefined
  return (
    <div className={`toolbar-call ${tool.status}`}>
      <span className="toolbar-icon">
        <Icon name="terminal" size={13} />
      </span>
      <span className="toolbar-name">{tool.name || '(工具)'}</span>
      <span className="toolbar-args">({tool.args.slice(0, 120)}{tool.args.length > 120 ? '…' : ''})</span>
      {badge && (
        <span className={`approval-badge ${badge.cls}`} title={tool.approval?.trigger || undefined}>
          {badge.text}
        </span>
      )}
      {tool.status === 'running' ? (
        <span className="toolbar-status spinner" />
      ) : (
        <span className="toolbar-status done">
          <Icon name="check" size={12} />
        </span>
      )}
    </div>
  )
}

/** 思考过程折叠块：思考中（active=true）自动展开，用固定高度框实时展示流式思考输出；
 * 思考结束后自动折叠回标题行；点击标题可重新展开为同款固定高度可滚动框。 */
function ThinkingBox({ text, open, active }: { text: string; open: boolean; active?: boolean }): JSX.Element {
  const [expanded, setExpanded] = useState(open)
  const contentRef = useRef<HTMLDivElement>(null)
  // 流式输出期间固定高度框自动滚到底，保证最新思考内容可见
  useEffect(() => {
    if (active && contentRef.current) {
      contentRef.current.scrollTop = contentRef.current.scrollHeight
    }
  }, [text, active])
  const showContent = active || expanded
  if (!text) return <></>
  return (
    <div className="thinking-box">
      <button className="thinking-toggle" onClick={() => setExpanded((v) => !v)}>
        <Icon name="brain" size={13} />
        <span>深度思考</span>
        {active && <span className="toolbar-status spinner" />}
        <Icon name="chevronRight" size={12} className={active || expanded ? 'rot' : ''} />
      </button>
      {showContent && (
        <div className="thinking-content" ref={contentRef}>
          {text}
        </div>
      )}
    </div>
  )
}

/** 子智能体执行块：机器人头图标标识，思考过程与工具执行折叠在块下（可展开/收起）。
 * 状态：执行中（转圈）/ 已完成（对勾 + 耗时）/ 失败（红叉 + 原因）/ 已中断
 *（进程被强杀，仅剩启动占位记录）。子智能体返回给主智能体的正文不在此展示
 * ——那是给主智能体的结果，不是给用户的。 */
function SubAgentBlock({ block }: { block: SubAgentMsg }): JSX.Element {
  const [expanded, setExpanded] = useState(false)
  const state: 'running' | 'done' | 'error' | 'aborted' = block.error
    ? 'error'
    : block.status === 'aborted'
      ? 'aborted'
      : block.streaming || block.status === 'running'
        ? 'running'
        : block.status ?? 'done'
  const seconds =
    typeof block.durationMs === 'number' ? Math.max(1, Math.round(block.durationMs / 1000)) : null
  return (
    <div className={`subagent-box ${state}`}>
      <button className="subagent-header" onClick={() => setExpanded((v) => !v)}>
        <span className="subagent-icon">
          <Icon name="bot" size={14} />
        </span>
        <span className="subagent-name">{block.name || '子智能体'}</span>
        {state === 'running' && <span className="subagent-badge running">执行中</span>}
        {state === 'error' && <span className="subagent-badge error">失败</span>}
        {state === 'aborted' && <span className="subagent-badge aborted">已中断</span>}
        {seconds !== null && state !== 'running' && (
          <span className="subagent-duration">{seconds}s</span>
        )}
        <span className="subagent-status">
          {state === 'running' ? (
            <span className="toolbar-status spinner" />
          ) : state === 'error' ? (
            <Icon name="close" size={12} />
          ) : state === 'aborted' ? (
            <Icon name="stop" size={12} />
          ) : (
            <Icon name="check" size={12} />
          )}
        </span>
        <Icon name="chevronRight" size={12} className={expanded ? 'rot' : ''} />
      </button>
      {expanded && (
        <div className="subagent-content">
          {block.error && <div className="subagent-error">{block.error}</div>}
          <ThinkingBox text={block.thinking} open={false} active={block.thinkingActive} />
          {block.toolCalls.map((t) => (
            <ToolCallBar key={t.id} tool={t} />
          ))}
          {state === 'running' && !block.thinking && block.toolCalls.length === 0 && (
            <div className="subagent-empty">子智能体正在准备…</div>
          )}
        </div>
      )}
    </div>
  )
}

/** 正文段（Markdown 渲染）。提问块把正文切成前后两段时各渲染一段，
 *  样式与改造前的单块完全一致（同一个 `.markdown-body` 容器 + 同一条表格包裹规则）。
 *  `cursor` = 流式光标，只在**最后一段**上出现，位置与改造前相同（正文末尾）。 */
function MarkdownBody({ text, cursor }: { text: string; cursor?: boolean }): JSX.Element {
  return (
    <div className="markdown-body">
      {text ? (
        // 表格外包一层横向滚动框（.table-scroll）：列多时表格自身横向滚动，
        // 不把整块对话区撑宽（外层 .msgscroll 为 overflow-x: hidden）。
        <ReactMarkdown
          remarkPlugins={[remarkGfm]}
          components={{
            table: ({ children }) => (
              <div className="table-scroll">
                <table>{children}</table>
              </div>
            )
          }}
        >
          {text}
        </ReactMarkdown>
      ) : null}
      {cursor && <span className="cursor" />}
    </div>
  )
}

/** 消息时间展示：jsonl 秒级 ISO（2026-09-18T10:30:00）→ 年月日时分秒（2026-09-18 10:30:00）；
 *  空值（老会话行无 created_at）不渲染 */
function fmtMsgTime(iso?: string): string {
  if (!iso) return ''
  return iso.replace('T', ' ').slice(0, 19)
}

export default function MessageItem({ msg }: { msg: Message }): JSX.Element {
  const [menu, setMenu] = useState<{ x: number; y: number } | null>(null)
  // 附件缩略图按 (空间, att_id) 寻址：空间从当前会话的元数据取（会话建成即锁空间，
  // 归属不会变）。新会话尚未进 sessions 列表时回落到当前活动空间。
  const projectId = useAgentStore((s) => {
    const sid = s.activeSession
    if (!sid) return s.pendingProjectId ?? s.activeProject
    return s.sessions.find((x) => x.id === sid)?.project ?? s.activeProject
  })

  /** 会话内点引用（正文里的 `@文件` 胶囊 / RefBar 兜底行）该落到哪里
   *  （2026-09-23，docs/frontend/19 §6 第 20 条）：
   *  - **文件** → 右栏的**预览位**（全场唯一、再点别的文件就地顶替）；
   *  - **目录** → 仍然去系统文件管理器。右栏没有"目录预览"这回事，
   *    开一枚只会显示"这是一个目录，无法预览"的标签是纯噪音。
   *  - **附件**不在此列：附件是会话副本、不在工作区树里，保持 `openInFinder` 不变。
   *
   *  这里读 `activeSession` 而不是走 props：MessageItem 只会渲染**当前会话**的消息
   *  （`messages` 就是按 activeSession 做的投影），不存"渲染着 A 的消息、
   *  当前会话却是 B"的中间态。 */
  const activeSession = useAgentStore((s) => s.activeSession)
  const revealFile = useRightPanelStore((s) => s.revealFile)
  const openRef = (path: string, isDir: boolean): void => {
    if (isDir || !activeSession) {
      void window.agent.openInFinder(path)
      return
    }
    revealFile(activeSession, path)
  }

  /** 本条消息上需要锚定的在途审批卡片（2026-09-22 权限管控）：按 toolCallId
   *  匹配到本消息的工具行。主工具条匹配的渲染在工具条后；子智能体工具匹配的
   *  渲染在子智能体块**外**（块默认折叠，审批是"必须现在做决定"的交互，
   *  藏在折叠块里等于没问）。未被任何工具条配对的由 MessageList 末尾兜底。
   *  zustand v5 的 selector 返回值即 getSnapshot，不能在这里新建对象/数组
   *  （会触发 "Maximum update depth exceeded" 死循环）—— 只取 store 里的
   *  审批表引用（缺条目时 undefined），配对计算放 useMemo。 */
  const approvalTable = useAgentStore((s) =>
    s.activeSession ? s.approvalBySession[s.activeSession] : undefined
  )
  const { mainApprovals, subApprovals } = useMemo(() => {
    if (msg.role !== 'assistant' || !approvalTable) {
      return { mainApprovals: [] as ApprovalInteraction[], subApprovals: [] as ApprovalInteraction[] }
    }
    const mainIds = new Set(msg.toolCalls.map((t) => t.id))
    const subIds = new Set(msg.subagents.flatMap((x) => x.toolCalls.map((t) => t.id)))
    const main: ApprovalInteraction[] = []
    const sub: ApprovalInteraction[] = []
    for (const a of Object.values(approvalTable)) {
      if (!a.toolCallId) continue
      if (mainIds.has(a.toolCallId)) main.push(a)
      else if (subIds.has(a.toolCallId)) sub.push(a)
    }
    return { mainApprovals: main, subApprovals: sub }
  }, [msg, approvalTable])

  /** 正文里的 `@相对路径` token → 内联胶囊（与输入区同款视觉，见 lib/refTokens）。
   *
   *  `segments` 给正文用；`leftover` 是"没能内联渲染"的引用（正文里找不到对应 token，
   *  例如只有引用块的老数据）—— 只把它们交给下方那行 RefBar 兜底，
   *  正常路径下 RefBar 为空、不再重复列一遍。 */
  const refRender = useMemo(() => {
    const r = renderRefText(msg.content || '', msg.refs)
    const inline = new Set(r.inlinePaths)
    return { segments: r.segments, leftover: (msg.refs ?? []).filter((x) => x?.path && !inline.has(x.path)) }
  }, [msg.content, msg.refs])

  /** 正文 × 提问小结块的**交错序列**（2026-09-22）。
   *
   *  模型的话总在提问之前（"先跟你确认几个关键项"，然后才问），所以卡片必须插在
   *  正文里**发起提问那一刻**的位置：之前的正文在卡片上方，之后的留在下方。
   *  切点取自 `AskUserMsg.contentOffset`（实时在 `tool_call_start` 记录）；
   *  回放缺省 → 视作全量正文在卡片上方（jsonl 每次 LLM 调用一行，正文必在提问之前）。
   *
   *  只在有**已结算**的提问块时才切（`pending` 不渲染，见下面的过滤规则）。 */
  const parts = useMemo(() => {
    const content = msg.content ?? ''
    const asks = (msg.askUsers ?? []).filter((a) => a.status !== 'pending')
    const out: Array<{ kind: 'text'; text: string } | { kind: 'ask'; block: AskUserMsg }> = []
    let cursor = 0
    for (const block of asks) {
      // 偏移可能越界（老数据 / 回放缺省）：夹到 [cursor, 正文长度]，保证单调不减
      const off =
        typeof block.contentOffset === 'number'
          ? Math.min(Math.max(block.contentOffset, cursor), content.length)
          : content.length
      if (off > cursor) out.push({ kind: 'text', text: content.slice(cursor, off) })
      out.push({ kind: 'ask', block })
      cursor = off
    }
    if (cursor < content.length) out.push({ kind: 'text', text: content.slice(cursor) })
    return out
  }, [msg.content, msg.askUsers])
  // 光标只挂在最后一段正文上（正文段为空的纯文字流：单独渲染一段空正文兜住光标）
  const lastText = parts.length > 0 && parts[parts.length - 1].kind === 'text'

  /** 右键打开消息菜单 */
  const openMenu = (e: ReactMouseEvent): void => {
    e.preventDefault()
    setMenu({ x: e.clientX, y: e.clientY })
  }

  /** 复制消息正文（原始纯文本） */
  const copyContent = (): void => {
    void navigator.clipboard.writeText(msg.content || '')
  }

  if (msg.role === 'user') {
    return (
      <>
        <div className="msg-row user" onContextMenu={openMenu}>
          <div className="msg-bubble user">
            {/* 附件（2026-09-20）：实时消息来自草稿项、回放消息来自后端 harvest，
                形状一致 → 图片显示缩略图、其它显示文件 chip；点击在 Finder 中定位原文件 */}
            {msg.attachments && msg.attachments.length > 0 && (
              <AttachmentBar
                compact
                items={msg.attachments.map((a) => ({
                  key: a.id,
                  // 解析不完整 → degraded（琥珀）。旧 jsonl 行的统计字段缺失，
                  // 取默认值后不会误标降级。
                  status: a.missing
                    ? ('failed' as const)
                    : isDegraded(a)
                      ? ('degraded' as const)
                      : ('ready' as const),
                  kind: a.kind,
                  name: a.name,
                  size: a.size,
                  url: a.kind === 'image' ? attachmentUrl(projectId, a.id) : null,
                  stats: {
                    text_chars: a.text_chars ?? 0,
                    text_truncated: a.text_truncated ?? false,
                    pages: a.pages ?? null,
                    images: a.images ?? 0,
                    tables: a.tables ?? 0,
                    converter: a.converter ?? '',
                    warnings: a.warnings ?? []
                  },
                  missing: a.missing,
                  sourcePath: a.source_path,
                  storedPath: a.stored_path
                }))}
                onOpen={(v) => v.sourcePath && void window.agent.openInFinder(v.sourcePath)}
              />
            )}
            {/* 正文：`@相对路径` token 就地渲染成胶囊（只显示文件名，与输入区一致） */}
            <RefText segments={refRender.segments} onOpen={openRef} />
            {/* 引用（@-mention）兜底行：只列"正文里没能内联渲染"的引用（正常为空）。
                引用与附件是**两条独立通道**（形状与语义都不同）—— 引用零复制、
                只指向工作空间里的路径，故用独立组件与独立样式渲染 */}
            <RefBar refs={refRender.leftover} onOpen={openRef} />
            <div className="msg-meta">
              {msg.created_at && <span className="msg-time">{fmtMsgTime(msg.created_at)}</span>}
              <button
                className="msg-copy-btn"
                title="复制"
                onClick={(e) => {
                  e.stopPropagation()
                  copyContent()
                }}
              >
                <Icon name="copy" size={12} />
              </button>
            </div>
          </div>
        </div>
        {menu && <MessageMenu x={menu.x} y={menu.y} onCopy={copyContent} onClose={() => setMenu(null)} />}
      </>
    )
  }

  // token footer 两段式：本轮消耗（主 + 子智能体，usage_stats 事件 / 回放 jsonl usage 字段）
  // + turn 收尾时的会话级累计快照（回放缺省，只显示第一段）
  // + 本轮模型与参数（usage_stats 事件 model 字段 / 回放 jsonl model_info 节点；老轮次缺省）
  const modelText = msg.usage?.model ? modelInfoText(msg.usage.model) : null
  // 模型切换提示：挂到「切换发生时」的那条 assistant 消息上——
  // 空闲期切换即时显示（先于用户下一条指令），本轮执行中切换显示在本轮答复末尾。
  const turnSwitch = msg.switch
  const switchText =
    turnSwitch && turnSwitch.from_name !== turnSwitch.to_name
      ? `模型已从 ${turnSwitch.from_name} 更改为 ${turnSwitch.to_name}`
      : null
  const usage = msg.usage && msg.usage.turn.total_tokens ? (
    <span className="msg-usage">
      本轮 {fmtTokens(msg.usage.turn.total_tokens)} tokens · 缓存命中 {cachePct(msg.usage.turn)}
      {modelText ? <>（{modelText}）</> : null}
      {msg.usage.session && msg.usage.session.total_tokens ? (
        <>
          <span className="msg-usage-sep" />
          本会话累计 {fmtTokens(msg.usage.session.total_tokens)} tokens · 命中 {cachePct(msg.usage.session)}
        </>
      ) : null}
    </span>
  ) : null

  return (
    <>
      <div className="msg-row assistant" onContextMenu={openMenu}>
        <div className="assistant-body">
          <ThinkingBox text={msg.thinking} open={false} active={msg.thinkingActive} />
          {msg.toolCalls.map((t) => (
            <ToolCallBar key={t.id} tool={t} />
          ))}
          {/* 在途审批卡片（主工具）：锚定到触发的工具条下方 */}
          {mainApprovals.map((a) => (
            <ApprovalCard key={a.requestId} approval={a} />
          ))}
          {msg.subagents.map((s) => (
            <SubAgentBlock key={s.id} block={s} />
          ))}
          {/* 在途审批卡片（子智能体工具）：渲染在折叠块外，保证可见 */}
          {subApprovals.map((a) => (
            <ApprovalCard key={a.requestId} approval={a} />
          ))}
          {/* 正文与结构化提问小结块（ask_user）按**当时的先后**交错渲染：
              提问之前说的话在卡片上方，提问之后续写的正文（实时路径整轮合并进
              一条消息）留在卡片下方 —— 见上面 `parts` 的切分规则。
              `pending` 的那条**不渲染**：此刻输入区上方的面板正承载交互，
              这里再挂一条空小结就是重复表达。结算后（answered/cancelled/stopped）
              与回放路径都走到这里，展示的是后端生成的同一份 result_text
              （前端不解析、只原样展示）。 */}
          {parts.map((p, i) =>
            p.kind === 'ask' ? (
              <AskUserBlock key={`ask-${p.block.toolCallId || i}`} block={p.block} />
            ) : (
              <MarkdownBody
                key={`md-${i}`}
                text={p.text}
                cursor={msg.streaming && i === parts.length - 1}
              />
            )
          )}
          {/* 全文都是提问、没有正文（或正文还没开吐）时，光标单独兜一块空正文：
              与改造前的 `content` 为空时的渲染一致 */}
          {msg.streaming && !lastText && <MarkdownBody text="" cursor />}
          {!msg.streaming && usage}
          {!msg.streaming && switchText && (
            <div className="model-switch-notice" title="已切换模型">
              <span>{switchText}</span>
              <span className="model-switch-info" aria-label="模型已切换">ⓘ</span>
            </div>
          )}
          <div className="msg-meta">
            {msg.created_at && <span className="msg-time">{fmtMsgTime(msg.created_at)}</span>}
            <button
              className="msg-copy-btn"
              title="复制"
              onClick={(e) => {
                e.stopPropagation()
                copyContent()
              }}
            >
              <Icon name="copy" size={12} />
            </button>
          </div>
        </div>
      </div>
      {menu && <MessageMenu x={menu.x} y={menu.y} onCopy={copyContent} onClose={() => setMenu(null)} />}
    </>
  )
}